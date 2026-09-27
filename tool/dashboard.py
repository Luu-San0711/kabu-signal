# -*- coding: utf-8 -*-
"""ダッシュボード用 data.json を生成（GitHub Pages の index.html が読む）"""
import json
import os
import time

import numpy as np

import signal_lib as sl
import portfolio as pf
import strategy as st

OUT_JSON = os.path.join(sl.BASE, "data.json")


def _etf_chart(ticker, days=130):
    try:
        s = st.etf_close(ticker).iloc[-days:]
        return {"dates": [str(d.date()) for d in s.index], "close": [round(float(v), 2) for v in s.values]}
    except Exception:
        return None


def build_data(cfg):
    port = pf.load()
    sig = sl.load_state("signals.json", {})
    fx = float(sig.get("fx") or sl.usdjpy())
    prices = st.latest_prices(port, fx)
    val = st.valuation(port, prices, fx)
    pend = pf.pending_orders(port)
    today = time.strftime("%Y-%m-%d")

    # 今日やること: 未記録の指示（新しい順）＋ 今日の自動記録
    todo = [o for o in port["orders"] if o["status"] == "pending"]
    todo += [o for o in port["orders"] if o["status"] == "auto" and o.get("fill_date") == today]
    recent = sorted([o for o in port["orders"] if o["status"] != "pending"],
                    key=lambda o: o.get("fill_date") or o["date"], reverse=True)[:30]

    # チャート（保有＋指示の銘柄）
    charts = {}
    jp_t = sorted({x["ticker"] for x in pf.open_positions(port, "jp")} | {o["ticker"] for o in pend if o["market"] == "jp"})
    try:
        charts.update(sl.chart_series("jp", jp_t, days=130, smas=(25, 75)))
    except Exception as e:
        sl.log(f"JPチャート生成スキップ: {str(e)[:120]}")
    us_t = {x["ticker"] for x in pf.open_positions(port, "us")} | {o["ticker"] for o in pend if o["market"] == "us"}
    for t in us_t:
        ch = _etf_chart(t)
        if ch:
            charts[t] = ch
    fid = port["us"].get("ticker", "NDX100")
    ch = _etf_chart(port["us"].get("proxy") or "QQQ")
    if ch:
        ch["close"] = [round(v * fx, 0) for v in ch["close"]]  # 円換算の目安
        charts[fid] = ch

    sma = {s.get("position_id"): s.get("sma25") for s in sig.get("jp_status", [])}
    for r in val["rows"]:
        r["sma25"] = sma.get(r["id"])
    us_val = sum(r["value_yen"] for r in val["rows"] if r["market"] in ("us", "fund"))
    exposure_pct = (val["total_yen"] - port["cash_yen"]) / val["total_yen"] if val["total_yen"] else 0
    hist = port.get("history", [])
    ytd = None
    if hist:
        base = [h for h in hist if h["date"][:4] == today[:4]]
        if base:
            ytd = val["total_yen"] - base[0]["total_yen"]

    # 次の予定
    import datetime as dt
    d = dt.date.today()
    nxt_mon = d + dt.timedelta(days=(7 - d.weekday()) % 7 or 7)
    first = (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    nxt = [{"date": nxt_mon.isoformat(), "label": "週次判断"}, {"date": first.isoformat(), "label": f"+{port['monthly_add_yen']:,}円"}]
    nxt.sort(key=lambda x: x["date"])

    return {
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "data_date": sig.get("data_date", ""),
        "fx": fx,
        "cash_yen": port["cash_yen"],
        "cash_note": port.get("cash_note", ""),
        "cash_confirmed": port.get("cash_confirmed", False),
        "monthly_add_yen": port["monthly_add_yen"],
        "alloc_us": port["alloc_us"],
        "nisa": port.get("nisa", False),
        "total_yen": val["total_yen"],
        "prev_total_yen": val["prev_total_yen"],
        "ytd_yen": ytd,
        "exposure_pct": exposure_pct,
        "us_value_yen": us_val,
        "us": {**port["us"], **{k: sig.get("us", {}).get(k) for k in ("exposure_raw", "vol", "price", "date", "name")}},
        "todo": todo,
        "recent": recent,
        "positions": val["rows"],
        "closed": [x for x in port["positions"] if x["status"] in ("closed",)][-30:],
        "candidates": sig.get("candidates", []),
        "jp_status": sig.get("jp_status", []),
        "regime": sig.get("regime", {}),
        "events": port.get("events", [])[-30:],
        "history": hist[-60:],
        "next": nxt[:2],
        "charts": charts,
        "repo": cfg.get("github_repo", ""),
        "max_positions": cfg.get("jp", {}).get("max_positions", 5),
    }


def refresh(cfg=None):
    cfg = cfg or sl.load_config()
    body = json.dumps(build_data(cfg), ensure_ascii=False, default=_default)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        f.write(body)
    sl.log("ダッシュボード用 data.json 生成OK")
    return sl.dashboard_url(cfg)


def _default(o):
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


if __name__ == "__main__":
    refresh()
