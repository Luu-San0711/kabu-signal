# -*- coding: utf-8 -*-
"""定時ジョブ（GitHub Actions から呼ばれる）

  python tool/jobs.py monday     月曜 9:05  今週の指示（米国コアの調整・日本株の新規/手仕舞い）をLINE
  python tool/jobs.py evening    平日 19:35 保有の手仕舞い判定。売りが出た時だけLINE
  python tool/jobs.py saturday   土曜 9:05  今週のまとめをLINE
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
        o = pf.add_order(port, "sell", "jp", e["ticker"], e["name"], e["shares"], e["close"],
                         e["reason"], position_id=e["position_id"], date=today)
        sells.append(o)
    return sells


def _quarter_start(d):
    return d.month in (1, 4, 7, 10) and d.day <= 7


# ---------------------------------------------------------------- 月曜
def monday(cfg, today=None, notify=True):
    today = today or dt.date.today()
    sl.log("=== 月曜ジョブ ===")
    _update_all()
    port = pf.load()
    pf.apply_monthly_add(port, today)
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
    us = st.us_core(cfg, port, fx)
    us_pos = [x for x in pf.open_positions(port, "fund") if x["ticker"] == us["ticker"]]
    cur_yen = sum(x["shares"] for x in us_pos) * us["unit_yen"]
    sleeve = float(port["alloc_us"]) * total
    target_yen = sleeve * us["exposure"]
    diff_yen = int(round((target_yen - cur_yen) / 100.0)) * 100
    min_trade = int(cfg.get("us_core", {}).get("min_trade_yen", 3000))
    cash = _cash_for(port, fx)
    us_note = ""
    fund_kw = dict(date=ts, fx=fx, proxy=us["proxy"])
    if diff_yen >= min_trade:
        amt = min(diff_yen, int(cash // 100) * 100)
        if amt >= min_trade:
            o = pf.add_order(port, "buy", "fund", us["ticker"], us["name"], amt / us["unit_yen"], us["unit_yen"],
                             ("露出を上げる" if us["changed"] else "配分に合わせて買い増し") + f"（露出{us['exposure']*100:.0f}%）", **fund_kw)
            o["amount_yen"] = amt
            orders.append(o)
        else:
            us_note = f"現金不足で米国コアの買い増しを見送り（あと{diff_yen:,}円）"
    elif diff_yen <= -min_trade and (us["changed"] or _quarter_start(today)):
        amt = min(-diff_yen, int(cur_yen // 100) * 100)
        o = pf.add_order(port, "sell", "fund", us["ticker"], us["name"], amt / us["unit_yen"], us["unit_yen"],
                         ("露出を下げる（値動きが荒い）" if us["changed"] else "四半期の配分調整") + f"（露出{us['exposure']*100:.0f}%）",
                         position_id=us_pos[0]["id"] if us_pos else None, **fund_kw)
        o["amount_yen"] = amt
        orders.append(o)
    if us["changed"]:
        port["us"]["exposure"] = us["exposure"]
        port["us"]["exposure_date"] = ts
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
            sh = int(budget // cnd["close"])
            if sh < 1:
                continue
            o = pf.add_order(port, "buy", "jp", cnd["ticker"], cnd["name"], sh, cnd["close"],
                             f"55日高値を更新（{'★★★' if cnd['score']==3 else '★★☆'}）", stop=cnd["stop"], date=ts)
            jp_buys.append(o)
            cash -= sh * cnd["close"]
    orders += jp_buys

    pf.save(port)
    sl.save_state(SIGNALS, {"date": ts, "data_date": sig["date"], "regime": sig["regime"],
                            "candidates": sig["candidates"][:20], "jp_status": sig["status"],
                            "us": us, "fx": fx, "total_yen": total, "kind": "monday",
                            "auto_filled": n_auto, "us_note": us_note})

    # 4) LINE
    lines = [f"{today.month}/{today.day}（{_wd(today)}）今日やること {len(orders)}件"]
    for o in orders:
        side = "買" if o["side"] == "buy" else "売"
        if o["market"] == "fund":
            lines.append(f"{side} {o['name']} {o['amount_yen']:,}円（金額指定）{o['reason']}")
        elif o["market"] == "us":
            lines.append(f"{side} {o['ticker']} {o['shares']}株（約${o['ref_price']:,.0f}）{o['reason']}")
        else:
            amt = o["ref_price"] * o["shares"]
            extra = f" 損切り{o['stop']:,.0f}円" if o["side"] == "buy" else f" {o['reason']}"
            lines.append(f"{side} {o['code']} {o['name']} {o['shares']}株（約{amt/10000:.1f}万円）{extra}")
    if not orders:
        lines.append("今日は何もしません。保有はそのまま")
    if not sig["regime"]["ok"]:
        lines.append("地合い悪化中のため日本株の新規買いは停止")
    if us_note:
        lines.append(us_note)
    if any(o["market"] == "jp" for o in orders):
        lines.append("日本株はかぶミニの寄付取引（成行）。損切りは終値で判定して知らせます")
    if any(o["market"] == "fund" for o in orders):
        lines.append("投資信託は15:30までに金額指定で注文")
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
    pf.apply_monthly_add(port, today)
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
    lines = [f"{today.month}/{today.day}（{_wd(today)}）売り {len(sells)}件（明日の寄付）"]
    for o in sells:
        lines.append(f"売 {o['code']} {o['name']} {o['shares']}株 {o['reason']}")
    lines.append("かぶミニの寄付取引（成行）で売り。記録はアプリで")
    url = sl.dashboard_url(cfg)
    if url:
        lines.append(url)
    text = "\n".join(lines)
    if notify:
        sl.line_broadcast(text, cfg)
    return text


# ---------------------------------------------------------------- 土曜
def saturday(cfg, today=None, notify=True):
    today = today or dt.date.today()
    sl.log("=== 土曜まとめ ===")
    _update_all()
    port = pf.load()
    pf.apply_monthly_add(port, today)
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
    prev.update({"week_total_yen": val["total_yen"], "fx": fx})
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
        monday(cfg)
    elif cmd == "evening":
        evening(cfg)
    elif cmd == "saturday":
        saturday(cfg)
    elif cmd == "record":
        record(cfg)
    elif cmd == "dashboard":
        _refresh_dashboard(cfg)
    elif cmd == "auto":
        d = dt.date.today()
        if d.weekday() == 0:
            monday(cfg)
        elif d.weekday() == 5:
            saturday(cfg)
        else:
            sl.log(f"本日({d})は朝の実行対象日ではありません")
            _update_all(jp=False)
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
