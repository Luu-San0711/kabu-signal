# -*- coding: utf-8 -*-
"""定時ジョブ（GitHub Actions から呼ばれる）

  python tool/jobs.py auto       予約実行。日曜夜〜月曜朝=今週の指示 / 平日夕方=手仕舞い判定 / 土曜=まとめ
  python tool/jobs.py monday     今週の指示（米国コアの調整・日本株の新規/手仕舞い）をLINE（手動・強制）
  python tool/jobs.py evening    保有の手仕舞い判定。売りが出た時だけLINE
  python tool/jobs.py saturday   今週のまとめをLINE（手動・強制）
  python tool/jobs.py record     アプリからの記録（GitHub Issue）を状態に反映
  python tool/jobs.py dashboard  data.json だけ作り直す
"""
import datetime as dt
import json
import math
import os
import sys
import traceback

import signal_lib as sl
import portfolio as pf
import strategy as st

SIGNALS = "signals.json"


def _d(s):
    return s[5:].replace("-", "/")


def _wd(d):
    return "月火水木金土日"[d.weekday()]


def _update_all(jp=True, etf=True):
    try:
        if jp:
            sl.update_prices("jp", period="1mo")
            sl.update_indices()
    except Exception:
        sl.log("JP価格更新エラー:\n" + traceback.format_exc())
    try:
        if etf:
            st.update_etf()
    except Exception:
        sl.log("ETF価格更新エラー:\n" + traceback.format_exc())


def _cash_for(port, fx):
    """未記録の買い指示を差し引いた、これから使える現金"""
    reserved = 0.0
    for o in pf.pending_orders(port):
        if o["side"] == "buy":
            reserved += o.get("amount_yen") or o["ref_price"] * o["shares"] * (fx if o["market"] == "us" else 1.0)
    return float(port["cash_yen"]) - reserved


def _handle_jp_exits(port, sig, today):
    """手仕舞い（損切り・25日線<75日線・期限）はすべて翌朝寄付の売り指示。
    かぶミニは逆指値を置けないため、損切りも終値で判定して翌朝に売る"""
    sells = []
    pend = {(o["ticker"], o["side"]) for o in pf.pending_orders(port)}
    for e in sig["exits"]:
        if (e["ticker"], "sell") in pend:
            continue  # 既に売り指示が出ていて未記録
        if e["exit"] == "stop_order":
            o = pf.add_order(port, "sell", "jp", e["ticker"], e["name"], e["shares"], e["fill"],
                             "逆指値で売却", position_id=e["position_id"], date=today)
            pf.record_fill(port, o["id"], fill_price=e["fill"], auto=True)
            o["note"] = "逆指値が約定したものとして記録。違う場合は保有画面で直してください"
        else:
            o = pf.add_order(port, "sell", "jp", e["ticker"], e["name"], e["shares"], e["close"],
                             e["reason"], position_id=e["position_id"], date=today)
        sells.append(o)
    return sells


def _quarter_start(d):
    return d.month in (1, 4, 7, 10) and d.day <= 7


def _nisa(port, cfg):
    return bool(port.get("nisa", cfg.get("nisa_default", False)))


def _monthly(port, cfg, today, fx=None):
    """毎月の積み増し。米国コアの分はiGrowの積立で自動購入される前提で、自動で記録する"""
    if not pf.apply_monthly_add(port, today):
        return
    cu = cfg.get("us_core", {})
    amt = int(float(port.get("monthly_add_yen", 0)) * float(port.get("alloc_us", 0.5)) // 100) * 100
    has_fund = bool(pf.open_positions(port, "fund"))
    unit = float(port.get("us", {}).get("unit_yen") or 0)
    if cu.get("auto_invest", True) and has_fund and amt > 0 and unit > 0:
        o = pf.add_order(port, "buy", "fund", port["us"].get("ticker", "NDX100"), cu.get("fund_name", "楽天・プラス・NASDAQ-100"),
                         amt / unit, unit, "毎月の積立（iGrow）", date=today.isoformat(), fx=fx, proxy=cu.get("proxy", "QQQ"))
        o["amount_yen"] = amt
        pf.record_fill(port, o["id"], auto=True)
        o["note"] = "iGrowの積立で買えたものとして記録"


# ---------------------------------------------------------------- 月曜
def _trade_monday(d):
    """この指示で発注する月曜日（日曜なら翌日、月曜ならその日）"""
    return d + dt.timedelta(days=(7 - d.weekday()) % 7)


def monday(cfg, today=None, notify=True, force=False):
    today = today or dt.date.today()
    target = _trade_monday(today)
    prev = sl.load_state(SIGNALS, {})
    if not force and prev.get("weekly_for") == target.isoformat():
        sl.log(f"今週（{target}）の指示は作成済み → スキップ")
        return ""
    sl.log("=== 週次の指示 ===")
    _update_all()
    port = pf.load()
    # 同じ週の指示を作り直す場合は、前回の未記録分を取り消してから作る
    wk0 = (target - dt.timedelta(days=6)).isoformat()
    port["orders"] = [o for o in port["orders"] if not (o["status"] == "pending" and o["side"] == "buy"
                      and (o.get("week") == target.isoformat() or (not o.get("week") and o["date"] >= wk0)))]
    _monthly(port, cfg, today)
    n_auto = pf.auto_fill_stale(port, days=int(cfg.get("auto_fill_days", 7)), today=today)
    fx = sl.usdjpy()
    prices = st.latest_prices(port, fx)
    val = st.valuation(port, prices, fx)
    total = val["total_yen"]
    sig = st.jp_signals(cfg, port)
    ts = today.isoformat()

    orders = []
    # 1) 日本株の手仕舞い
    orders += _handle_jp_exits(port, sig, ts)

    # 2) 米国コア（NASDAQ-100 投資信託。金額で指示、100円単位）
    try:
        us = st.us_core(cfg, port, fx)
    except Exception:
        sl.log("米国コアの判定に失敗（価格データなし）:\n" + traceback.format_exc())
        us = None
    if us is None:
        us = {"ticker": port["us"].get("ticker", "NDX100"), "name": "楽天・プラス・NASDAQ-100", "proxy": "QQQ",
              "unit_yen": 1.0, "exposure": float(port["us"].get("exposure", 1.0)), "changed": False, "vol": None, "skip": True}
    us_pos = [x for x in pf.open_positions(port, "fund") if x["ticker"] == us["ticker"]]
    cur_yen = sum(x["shares"] for x in us_pos) * us["unit_yen"]
    sleeve = float(port["alloc_us"]) * total
    target_yen = sleeve * us["exposure"]
    diff_yen = int(round((target_yen - cur_yen) / 100.0)) * 100
    min_trade = int(cfg.get("us_core", {}).get("min_trade_yen", 3000))
    hold = cfg.get("us_core", {}).get("mode", "hold") == "hold"
    initial = not us_pos
    # 持ちっぱなし運用: 最初の購入と、四半期ごとの配分調整（5%以上ずれた時）だけ
    if hold and not initial:
        band = max(min_trade, 0.05 * total)
        if not _quarter_start(target) or abs(diff_yen) < band:
            diff_yen = 0
    cash = _cash_for(port, fx)
    us_note = ""
    fund_kw = dict(date=ts, fx=fx, proxy=us["proxy"])
    if us.get("skip"):
        diff_yen = 0
        us_note = "米国コアの価格を取得できず、今週の米国の指示はお休み"
    if diff_yen >= min_trade:
        amt = min(diff_yen, int(cash // 100) * 100)
        if amt >= min_trade:
            o = pf.add_order(port, "buy", "fund", us["ticker"], us["name"], amt / us["unit_yen"], us["unit_yen"],
                             ("最初の購入" if initial else "四半期の配分調整" if hold else "露出を上げる" if us["changed"] else "配分に合わせて買い増し"), **fund_kw)
            o["amount_yen"] = amt
            orders.append(o)
        else:
            us_note = f"現金不足で米国コアの買い増しを見送り（あと{diff_yen:,}円）"
    elif diff_yen <= -min_trade and (us["changed"] or _quarter_start(target)):
        amt = min(-diff_yen, int(cur_yen // 100) * 100)
        o = pf.add_order(port, "sell", "fund", us["ticker"], us["name"], amt / us["unit_yen"], us["unit_yen"],
                         ("四半期の配分調整" if hold else "露出を下げる（値動きが荒い）" if us["changed"] else "四半期の配分調整"),
                         position_id=us_pos[0]["id"] if us_pos else None, **fund_kw)
        o["amount_yen"] = amt
        orders.append(o)
    if us["changed"]:
        port["us"]["exposure"] = us["exposure"]
        port["us"]["exposure_date"] = ts
    if not us.get("skip"):
        port["us"].update({"ticker": us["ticker"], "name": us["name"], "proxy": us["proxy"],
                           "target_yen": round(target_yen), "unit_yen": us["unit_yen"], "vol": us["vol"]})

    # 3) 日本株の新規
    cj = cfg.get("jp", {})
    max_pos = int(cj.get("max_positions", 5))
    open_jp = pf.open_positions(port, "jp")
    pend_buy = [o for o in pf.pending_orders(port) if o["market"] == "jp" and o["side"] == "buy" and o["date"] != ts]
    for o in pend_buy:  # 先週の未記録の買いは取り下げ（古い候補で買わせない）
        o["status"] = "expired"
    free = max(0, max_pos - len(open_jp))  # 今日手仕舞う分の枠は来週から使う
    cash = _cash_for(port, fx)
    per_slot = (1 - float(port["alloc_us"])) * total / max_pos
    jp_buys = []
    if sig["regime"]["ok"]:
        for cnd in sig["candidates"]:
            if len(jp_buys) >= free:
                break
            budget = min(per_slot, cash)
            lot = int(budget // (cnd["close"] * 100 * 1.02)) * 100   # 単元（100株）で買えるか（値幅の余裕2%）
            sh = lot if lot >= 100 else int(budget // cnd["close"])
            if sh < 1:
                continue
            o = pf.add_order(port, "buy", "jp", cnd["ticker"], cnd["name"], sh, cnd["close"],
                             f"55日高値を更新（{'★★★' if cnd['score']==3 else '★★☆'}）", stop=cnd["stop"], date=ts)
            o["lot"] = lot >= 100
            jp_buys.append(o)
            cash -= sh * cnd["close"]
    orders += jp_buys

    for o in orders:
        if o["status"] == "pending":
            o["week"] = target.isoformat()
    pf.save(port)
    sl.save_state(SIGNALS, {"date": ts, "data_date": sig["date"], "regime": sig["regime"],
                            "candidates": sig["candidates"][:20], "jp_status": sig["status"],
                            "us": us, "fx": fx, "total_yen": total, "kind": "monday",
                            "auto_filled": n_auto, "us_note": us_note, "weekly_for": target.isoformat(),
                            "week_total_yen": prev.get("week_total_yen"), "sat_date": prev.get("sat_date")})

    # 4) LINE
    if target != today:
        when = f"{target.month}/{target.day}（月）"
    elif today == dt.date.today() and dt.datetime.now().hour >= 9:
        nd = today + dt.timedelta(days=1)
        when = f"寄付に間に合わないため {nd.month}/{nd.day}（{_wd(nd)}）"
    else:
        when = "今日"
    lines = [f"{when}の寄付で発注 {len(orders)}件（{sig['date'][5:].replace('-', '/')}終値で判定）"]
    for o in orders:
        side = "買" if o["side"] == "buy" else "売"
        if o["market"] == "fund":
            lines.append(f"{side} {o['name']} {o['amount_yen']:,}円 {o['reason']}")
        elif o["market"] == "us":
            lines.append(f"{side} {o['ticker']} {o['shares']}株（約${o['ref_price']:,.0f}）{o['reason']}")
        else:
            amt = o["ref_price"] * o["shares"]
            if o["side"] == "buy":
                extra = f" 逆指値{o['stop']:,.0f}円も同時に" if o.get("lot") else f" 損切り{o['stop']:,.0f}円"
            else:
                extra = f" {o['reason']}"
            lines.append(f"{side} {o['code']} {o['name']} {o['shares']}株（約{amt/10000:.1f}万円）{extra}")
    if not orders:
        lines.append("今日は何もしません。保有はそのまま")
    if not sig["regime"]["ok"]:
        lines.append("地合い悪化中のため日本株の新規買いは停止")
    if us_note:
        lines.append(us_note)
    acct = "NISA成長投資枠" if _nisa(port, cfg) else "特定口座"
    if any(o["market"] == "jp" and not o.get("lot") for o in orders):
        lines.append(f"日本株はiSPEEDのかぶミニ・寄付取引（成行）・{acct}。損切りは終値で判定して知らせます")
    if any(o["market"] == "jp" and o.get("lot") for o in orders):
        lines.append(f"100株の銘柄はiSPEEDで寄付成行＋逆指値（{acct}）")
    if any(o["market"] == "fund" for o in orders):
        lines.append(f"投資信託はiGrowで15:30までに金額指定（{acct}）")
    if any(o["market"] == "fund" and o["reason"] == "最初の購入" for o in orders):
        mi = int(float(port.get("monthly_add_yen", 0)) * float(port.get("alloc_us", 0.5)))
        lines.append(f"あわせてiGrowで毎月{mi:,}円の積立設定を（{acct}）。以後は自動で記録します")
    if orders:
        lines.append("記録はアプリで")
    if n_auto:
        lines.append(f"先週の未記録{n_auto}件は指示どおり約定として自動記録しました")
    if not port.get("cash_confirmed"):
        lines.append(f"現金は推定{port['cash_yen']:,}円。アプリの設定で実額に直してください")
    url = sl.dashboard_url(cfg)
    if url:
        lines.append(url)
    text = "\n".join(lines)
    _refresh_dashboard(cfg)
    if notify:
        sl.line_broadcast(text, cfg)
    return text


# ---------------------------------------------------------------- 平日夜
def evening(cfg, today=None, notify=True):
    today = today or dt.date.today()
    sl.log("=== 平日夜ジョブ ===")
    _update_all(etf=False)
    port = pf.load()
    _monthly(port, cfg, today)
    sig = st.jp_signals(cfg, port)
    prev = sl.load_state(SIGNALS, {})
    if prev.get("kind") == "evening" and prev.get("data_date") == sig["date"]:
        sl.log("新しい取引日データなし → 判定スキップ")
        _refresh_dashboard(cfg)
        return ""
    ts = today.isoformat()
    sells = _handle_jp_exits(port, sig, ts)
    pf.save(port)
    fx = sl.usdjpy()
    sl.save_state(SIGNALS, {**prev, "kind": "evening", "date": ts, "data_date": sig["date"],
                            "regime": sig["regime"], "jp_status": sig["status"], "fx": fx,
                            "candidates": prev.get("candidates", [])})
    _refresh_dashboard(cfg)
    if not sells:
        sl.log("売りなし → 通知なし")
        return ""
    npend = sum(1 for o in sells if o["status"] == "pending")
    head = f"売り {npend}件（明日の寄付）" if npend else "逆指値の約定"
    lines = [f"{today.month}/{today.day}（{_wd(today)}）{head}"]
    for o in sells:
        if o["status"] == "auto":
            lines.append(f"済 {o['code']} {o['name']} {o['shares']}株 逆指値で売却済みのはず（{o['ref_price']:,.0f}円）")
        else:
            lines.append(f"売 {o['code']} {o['name']} {o['shares']}株 {o['reason']}")
    if any(o["status"] == "pending" for o in sells):
        lines.append("iSPEEDで寄付成行の売り（100株の銘柄は逆指値を取り消してから）。記録はアプリで")
    url = sl.dashboard_url(cfg)
    if url:
        lines.append(url)
    text = "\n".join(lines)
    if notify:
        sl.line_broadcast(text, cfg)
    return text


# ---------------------------------------------------------------- 土曜
def saturday(cfg, today=None, notify=True, force=False):
    today = today or dt.date.today()
    if not force and sl.load_state(SIGNALS, {}).get("sat_date") == today.isoformat():
        sl.log("今週のまとめは送信済み → スキップ")
        return ""
    sl.log("=== 土曜まとめ ===")
    _update_all()
    port = pf.load()
    _monthly(port, cfg, today)
    fx = sl.usdjpy()
    prices = st.latest_prices(port, fx)
    val = st.valuation(port, prices, fx)
    prev = sl.load_state(SIGNALS, {})
    last_total = prev.get("week_total_yen")
    port.setdefault("history", [])
    port["history"] = [h for h in port["history"] if h["date"] != today.isoformat()]
    port["history"].append({"date": today.isoformat(), "total_yen": val["total_yen"], "cash_yen": port["cash_yen"]})
    port["history"] = port["history"][-260:]
    pf.save(port)
    prev.update({"week_total_yen": val["total_yen"], "fx": fx, "sat_date": today.isoformat()})
    sl.save_state(SIGNALS, prev)
    _refresh_dashboard(cfg)
    pend = pf.pending_orders(port)
    lines = [f"今週のまとめ {today.month}/{today.day}", f"資産 {val['total_yen']:,}円"
             + (f"（前週比 {val['total_yen']-last_total:+,}円）" if last_total else "")]
    lines.append(f"保有 {len(pf.open_positions(port))} · 現金 {port['cash_yen']:,}円 · 未記録 {len(pend)}件")
    if pend:
        lines.append("未記録の指示は月曜に「指示どおり約定」として自動記録されます")
    text = "\n".join(lines)
    if notify:
        sl.line_broadcast(text, cfg)
    return text


# ---------------------------------------------------------------- 記録（GitHub Issue）
def record(cfg, body=None):
    """Issue 本文の JSON 1行を状態に反映"""
    body = body if body is not None else os.environ.get("ISSUE_BODY", "")
    data = None
    for line in body.splitlines():
        line = line.strip().strip("`")
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except Exception:
                continue
    if not data:
        return "記録の形式を読み取れませんでした"
    port = pf.load()
    a = data.get("action")
    msg = ""
    if a == "fill":
        o = pf.record_fill(port, data.get("order"), data.get("price"), data.get("shares"), data.get("date"))
        if not o:
            msg = "対象の指示が見つかりません（記録済みか期限切れ）"
        elif o["market"] == "fund":
            msg = f"記録しました: {'買' if o['side']=='buy' else '売'} {o['name']} {o['fill_shares'] * o['fill_price']:,.0f}円"
        else:
            msg = f"記録しました: {'買' if o['side']=='buy' else '売'} {o['name']} {o['fill_shares']}株 @{o['fill_price']:,.2f}"
    elif a == "skip":
        o = pf.record_skip(port, data.get("order"))
        msg = f"見送りとして記録しました: {o['name']}" if o else "対象の指示が見つかりません"
    elif a == "cash":
        pf.set_cash(port, data.get("yen"), data.get("note", "手動で設定"))
        msg = f"購入可能金額を {int(data.get('yen')):,}円 にしました"
    elif a == "setting":
        if "monthly_add_yen" in data:
            port["monthly_add_yen"] = int(data["monthly_add_yen"])
        if "alloc_us" in data:
            port["alloc_us"] = float(data["alloc_us"])
        if "nisa" in data:
            port["nisa"] = bool(data["nisa"])
        pf.event(port, f"設定変更: 積み増し {port['monthly_add_yen']:,}円 / 米国配分 {port['alloc_us']*100:.0f}%")
        msg = "設定を更新しました"
    elif a == "position_edit":
        x = pf.edit_position(port, data.get("position"), data.get("shares"), data.get("price"), data.get("stop"))
        msg = f"保有を修正しました: {x['name']}" if x else "対象の保有が見つかりません"
    elif a == "position_remove":
        x = pf.remove_position(port, data.get("position"))
        msg = f"保有から外しました: {x['name']}" if x else "対象の保有が見つかりません"
    elif a == "position_add":
        x = pf.add_position(port, data.get("market", "jp"), data.get("ticker"), data.get("name", data.get("ticker")),
                            data.get("shares"), data.get("price"), data.get("date"), fx=data.get("fx"))
        msg = f"保有に追加しました: {x['name']}"
    else:
        msg = f"不明な操作: {a}"
    pf.save(port)
    _refresh_dashboard(cfg)
    sl.log("記録: " + msg)
    with open(os.path.join(sl.STATE, "record_result.txt"), "w") as f:
        f.write(msg)
    return msg


def _refresh_dashboard(cfg):
    try:
        import dashboard
        dashboard.refresh(cfg)
    except Exception:
        sl.log("ダッシュボード生成エラー:\n" + traceback.format_exc())


def main():
    cfg = sl.load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "monday":
        monday(cfg, force=True)
    elif cmd == "evening":
        evening(cfg)
    elif cmd == "saturday":
        saturday(cfg, force=True)
    elif cmd == "record":
        record(cfg)
    elif cmd == "dashboard":
        _refresh_dashboard(cfg)
    elif cmd == "auto":
        # 予約実行は遅れたり抜けたりするので、複数の時刻に予約し、ここで「今やるべき処理」を判定する。
        # 各処理は二重に送らないよう、済んでいればスキップする。
        now = dt.datetime.now()
        d, h = now.date(), now.hour
        if d.weekday() == 6 or (d.weekday() == 0 and h < 12):
            monday(cfg)                      # 日曜夜〜月曜昼: 今週の指示
        elif d.weekday() == 5:
            saturday(cfg)                    # 土曜: まとめ
        elif d.weekday() <= 4 and h >= 16:
            evening(cfg)                     # 平日夕方以降: 手仕舞い判定
        else:
            sl.log(f"{now:%m/%d %H:%M} は実行対象の時間帯ではありません")
            _refresh_dashboard(cfg)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sl.log("ジョブ致命的エラー:\n" + traceback.format_exc())
        sys.exit(1)
