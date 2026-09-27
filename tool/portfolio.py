# -*- coding: utf-8 -*-
"""口座の状態（現金・保有・注文記録）を1つのJSONで管理する

tool/state/portfolio.json
  cash_yen          : 購入可能金額（円）。記録に応じて自動で増減。設定画面から上書き可
  cash_note         : 現金の出どころ（推定 / 手動設定 など）
  monthly_add_yen   : 毎月1日に自動で加算する積み増し額
  last_monthly_add  : 最後に積み増しを加算した年月 "YYYY-MM"
  alloc_us          : 米国コアの目標配分（0.5 = 米50:日50）
  positions[]       : 保有（open）と決済済み（closed）
  orders[]          : 指示した注文。pending → done / skipped / auto
  us                : 米国コア（NASDAQ-100投信）の露出（exposure）と最終更新日
"""
import datetime as dt
import json
import os
import uuid

import signal_lib as sl

FILE = "portfolio.json"

DEFAULT = {
    "cash_yen": 218000,
    "cash_note": "推定（メタプラネット700株×約312円）。実際の金額に直してください",
    "cash_confirmed": False,
    "monthly_add_yen": 10000,
    "last_monthly_add": "",
    "alloc_us": 0.5,
    "positions": [],
    "orders": [],
    "us": {"ticker": "NDX100", "exposure": 1.0, "exposure_date": ""},
    "events": [],
}


def load():
    p = sl.load_state(FILE, None)
    if p is None:
        p = json.loads(json.dumps(DEFAULT))
    for k, v in DEFAULT.items():
        p.setdefault(k, json.loads(json.dumps(v)))
    return p


def save(p):
    p["events"] = p.get("events", [])[-200:]
    sl.save_state(FILE, p)


def _id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def event(p, text, date=None):
    p.setdefault("events", []).append({"date": date or dt.date.today().isoformat(), "text": text})


# ---------------------------------------------------------------- 現金
def apply_monthly_add(p, today=None):
    """毎月1日以降、その月にまだ加算していなければ積み増しを加算"""
    today = today or dt.date.today()
    ym = today.strftime("%Y-%m")
    add = int(p.get("monthly_add_yen", 0) or 0)
    if not p.get("last_monthly_add"):
        p["last_monthly_add"] = ym  # 初回はこの月から数える（今月分は加算しない）
        return False
    if p["last_monthly_add"] < ym and add > 0:
        p["cash_yen"] = int(p.get("cash_yen", 0)) + add
        p["last_monthly_add"] = ym
        event(p, f"毎月の積み増し +{add:,}円 → 現金 {p['cash_yen']:,}円", today.isoformat())
        return True
    return False


def set_cash(p, yen, note="手動で設定"):
    p["cash_yen"] = int(yen)
    p["cash_note"] = note
    p["cash_confirmed"] = True
    event(p, f"購入可能金額を {int(yen):,}円 に設定")


# ---------------------------------------------------------------- 注文
def open_positions(p, market=None):
    return [x for x in p["positions"] if x.get("status") == "open" and (market is None or x["market"] == market)]


def pending_orders(p):
    return [o for o in p["orders"] if o.get("status") == "pending"]


def find_order(p, oid):
    for o in p["orders"]:
        if o["id"] == oid:
            return o
    return None


def find_position(p, pid):
    for x in p["positions"]:
        if x["id"] == pid:
            return x
    return None


def add_order(p, side, market, ticker, name, shares, ref_price, reason, stop=None,
              position_id=None, date=None, fx=None, note="", proxy=None):
    """新しい指示を pending で追加。同じ銘柄・同じ側の pending があれば置き換える"""
    for o in list(p["orders"]):
        if o["status"] == "pending" and o["ticker"] == ticker and o["side"] == side:
            p["orders"].remove(o)
    o = {"id": _id("o"), "date": date or dt.date.today().isoformat(), "side": side,
         "market": market, "ticker": ticker, "code": ticker.replace(".T", ""),
         "name": name, "shares": _qty(market, shares), "ref_price": float(ref_price),
         "stop": (None if stop is None else float(stop)), "reason": reason,
         "status": "pending", "position_id": position_id, "fx": fx, "note": note, "proxy": proxy}
    p["orders"].append(o)
    return o


def _qty(market, q):
    """投信は口数（小数）、株は整数"""
    return round(float(q), 6) if market == "fund" else int(q)


def _yen(market, price, shares, fx):
    return price * shares * (fx if market == "us" else 1.0)


def record_fill(p, oid, fill_price=None, fill_shares=None, fill_date=None, auto=False):
    """「発注した」= 約定として記録。買いは保有を作り現金を減らす。売りは保有を閉じ現金を増やす"""
    o = find_order(p, oid)
    if not o or o["status"] not in ("pending",):
        return None
    price = float(fill_price if fill_price is not None else o["ref_price"])
    shares = _qty(o["market"], fill_shares if fill_shares is not None else o["shares"])
    date = fill_date or dt.date.today().isoformat()
    fx = o.get("fx") or 150.0
    if shares <= 0:
        return record_skip(p, oid)
    if o["side"] == "buy":
        stop = o.get("stop")
        if o["market"] == "jp" and stop is not None and fill_price is not None:
            stop = round(price * 0.90, 1)  # 実際の取得単価 −10%
        pos = {"id": _id("p"), "market": o["market"], "ticker": o["ticker"], "code": o["code"],
               "name": o["name"], "shares": shares, "entry_price": price, "entry_date": date,
               "stop": stop, "status": "open", "fx_entry": fx if o["market"] == "us" else None,
               "order_id": oid, "proxy": o.get("proxy")}
        # 同一銘柄の保有があれば合算（米国コアの買い増し）
        same = [x for x in open_positions(p, o["market"]) if x["ticker"] == o["ticker"]]
        if same:
            x = same[0]
            tot = x["shares"] + shares
            x["entry_price"] = (x["entry_price"] * x["shares"] + price * shares) / tot
            x["shares"] = _qty(o["market"], tot)
            pos = x
        else:
            p["positions"].append(pos)
        p["cash_yen"] = int(round(p["cash_yen"] - _yen(o["market"], price, shares, fx)))
        o.update({"status": "auto" if auto else "done", "fill_price": price, "fill_shares": shares,
                  "fill_date": date, "position_id": pos["id"]})
        event(p, f"{'自動記録' if auto else '記録'}: 買 {o['name']} " + (f"{shares * price:,.0f}円" if o["market"] == "fund" else f"{shares}株 @{price:,.2f}"), date)
    else:
        pos = find_position(p, o.get("position_id") or "")
        if pos is None:
            same = [x for x in open_positions(p, o["market"]) if x["ticker"] == o["ticker"]]
            pos = same[0] if same else None
        if pos is not None:
            sold = min(shares, pos["shares"])
            pos["shares"] = _qty(o["market"], pos["shares"] - sold)
            pnl = (price - pos["entry_price"]) * sold * (fx if o["market"] == "us" else 1.0)
            if pos["shares"] <= 1e-6:
                pos.update({"status": "closed", "exit_price": price, "exit_date": date,
                            "pnl_yen": round(pnl), "exit_reason": o["reason"]})
            p["cash_yen"] = int(round(p["cash_yen"] + _yen(o["market"], price, sold, fx)))
            shares = sold
        o.update({"status": "auto" if auto else "done", "fill_price": price, "fill_shares": shares,
                  "fill_date": date})
        event(p, f"{'自動記録' if auto else '記録'}: 売 {o['name']} " + (f"{shares * price:,.0f}円" if o["market"] == "fund" else f"{shares}株 @{price:,.2f}"), date)
    return o


def record_skip(p, oid):
    o = find_order(p, oid)
    if not o or o["status"] != "pending":
        return None
    o["status"] = "skipped"
    o["fill_date"] = dt.date.today().isoformat()
    event(p, f"見送り: {'買' if o['side']=='buy' else '売'} {o['name']} {o['shares']}株")
    return o


def auto_fill_stale(p, days=7, today=None):
    """一定日数を過ぎても未記録の指示は「指示どおり約定した」とみなして自動記録"""
    today = today or dt.date.today()
    n = 0
    for o in pending_orders(p):
        d = dt.date.fromisoformat(o["date"])
        if (today - d).days >= days:
            record_fill(p, o["id"], auto=True)
            n += 1
    return n


def remove_position(p, pid, note="手動で削除"):
    x = find_position(p, pid)
    if x and x["status"] == "open":
        x["status"] = "removed"
        x["exit_date"] = dt.date.today().isoformat()
        event(p, f"保有から削除: {x['name']}（{note}）")
        return x
    return None


def edit_position(p, pid, shares=None, entry_price=None, stop=None):
    x = find_position(p, pid)
    if not x:
        return None
    if shares is not None:
        x["shares"] = _qty(x["market"], shares)
    if entry_price is not None:
        x["entry_price"] = float(entry_price)
        if x["market"] == "jp":
            x["stop"] = round(float(entry_price) * 0.90, 1)
    if stop is not None:
        x["stop"] = float(stop)
    event(p, f"保有を修正: {x['name']} {x['shares']}株 @{x['entry_price']:,.2f}")
    return x


def add_position(p, market, ticker, name, shares, entry_price, entry_date=None, fx=None):
    """手動で保有を追加（システム外で買ったものなど）"""
    stop = round(float(entry_price) * 0.90, 1) if market == "jp" else None
    x = {"id": _id("p"), "market": market, "ticker": ticker, "code": ticker.replace(".T", ""),
         "name": name, "shares": _qty(market, shares), "entry_price": float(entry_price),
         "entry_date": entry_date or dt.date.today().isoformat(), "stop": stop, "status": "open",
         "fx_entry": fx, "order_id": None}
    p["positions"].append(x)
    event(p, f"保有を追加: {name} {shares}株 @{float(entry_price):,.2f}")
    return x
