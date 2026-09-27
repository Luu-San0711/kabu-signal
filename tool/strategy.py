# -*- coding: utf-8 -*-
"""売買ルール（検証済み・2019〜2026）

米国コア: 楽天・プラス・NASDAQ-100インデックス・ファンド（投資信託、円で1円単位）を保有。
          値動きの判定と評価は同じ指数のETF「QQQ」×ドル円で代用する。
          21日実現ボラで露出を調整（目標20%）。露出は月曜のみ更新、前回から15pt超ずれた時だけ変更。
日本株  : 55日高値ブレイクアウト（週内に更新・金曜引けも条件維持）。最大5銘柄。
          損切り −10%（かぶミニは逆指値不可のため、終値で判定して翌朝寄付で売る）、
          25日線<75日線で手仕舞い、最長60営業日。
          日経平均が200日線より上 かつ 5日で−3%超の急落中でない時だけ新規買い。
"""
import glob
import os

import numpy as np
import pandas as pd

import signal_lib as sl

ETF_FILE = os.path.join(sl.DATA, "etf.parquet")


# ---------------------------------------------------------------- ETF価格
def _download(tickers, period):
    import yfinance as yf
    df = yf.download(tickers=list(tickers), period=period, interval="1d",
                     group_by="ticker", auto_adjust=True, threads=2, progress=False)
    frames = []
    if df is None or len(df) == 0:
        return frames
    if not isinstance(df.columns, pd.MultiIndex):
        df = pd.concat({list(tickers)[0]: df}, axis=1)
    for t in tickers:
        if t not in df.columns.get_level_values(0):
            continue
        sub = df[t].dropna(how="all").reset_index()
        sub.columns = [str(c).lower() for c in sub.columns]
        need = ["date", "open", "high", "low", "close", "volume"]
        if not all(c in sub.columns for c in need):
            continue
        sub = sub[need].copy()
        sub["ticker"] = t
        frames.append(sub)
    return frames


def update_etf(tickers=("QQQ",), period="1mo"):
    """米国コア判定用ETFの日足を data/etf.parquet に増分保存（初めての銘柄は3年分取得）"""
    if os.environ.get("KABU_SKIP_UPDATE") and os.path.exists(ETF_FILE):
        return
    old = pd.read_parquet(ETF_FILE) if os.path.exists(ETF_FILE) else pd.DataFrame()
    have = set(old["ticker"].unique()) if len(old) else set()
    new_t = [t for t in tickers if t not in have]
    known = [t for t in tickers if t in have]
    frames = []
    if new_t:
        frames += _download(new_t, "3y")
    if known:
        frames += _download(known, period)
    if not frames:
        sl.log("ETF価格の取得に失敗（既存データで続行）")
        return
    new = pd.concat(frames, ignore_index=True)
    new["date"] = pd.to_datetime(new["date"])
    if getattr(new["date"].dt, "tz", None) is not None:
        new["date"] = new["date"].dt.tz_localize(None)
    if len(old):
        old["date"] = pd.to_datetime(old["date"])
        new = pd.concat([old, new], ignore_index=True)
    new = new.drop_duplicates(["date", "ticker"], keep="last").sort_values(["ticker", "date"])
    new.to_parquet(ETF_FILE, index=False)
    sl.log(f"ETF価格更新OK（{new['date'].max().date()}）")


def etf_close(ticker):
    d = pd.read_parquet(ETF_FILE)
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["ticker"] == ticker].drop_duplicates("date", keep="last")
    return d.set_index("date")["close"].sort_index().astype(float)


# ---------------------------------------------------------------- 米国コア
def us_core(cfg, port, fx):
    """米国コア（NASDAQ-100投信）の目標露出を返す。判定は代用ETF（QQQ）の値動き"""
    cu = cfg.get("us_core", {})
    ticker = cu.get("proxy", "QQQ")
    vt = float(cu.get("vol_target", 0.20))
    lb = int(cu.get("vol_lookback", 21))
    band = float(cu.get("change_band", 0.15))
    s = etf_close(ticker)
    r = s.pct_change().dropna()
    rv = float(r.iloc[-lb:].std() * np.sqrt(252)) if len(r) >= lb else np.nan
    raw = 1.0 if not np.isfinite(rv) or rv <= 0 else float(min(1.0, vt / rv))
    cur = float(port["us"].get("exposure", 1.0))
    new = raw if abs(raw - cur) > band else cur
    return {"ticker": cu.get("fund_id", "NDX100"), "name": cu.get("fund_name", "楽天・プラス・NASDAQ-100"),
            "proxy": ticker, "price_usd": float(s.iloc[-1]), "unit_yen": float(s.iloc[-1]) * fx,
            "price": float(s.iloc[-1]) * fx, "date": str(s.index[-1].date()),
            "vol": rv, "exposure_raw": raw, "exposure": new, "changed": new != cur,
            "sma200_ok": bool(s.iloc[-1] > s.rolling(200).mean().iloc[-1]) if len(s) >= 200 else True}


# ---------------------------------------------------------------- 日本株
def jp_signals(cfg, port):
    """週次（月曜朝）用: ブレイクアウト候補と、保有銘柄の手仕舞い判定"""
    cj = cfg.get("jp", {})
    wide = sl.load_wide("jp")
    c, v, low = wide["close"], wide["volume"], wide["low"]
    sma25 = c.rolling(25, min_periods=25).mean()
    sma75 = c.rolling(75, min_periods=75).mean()
    sma200 = c.rolling(200, min_periods=200).mean()
    to20 = (c * v).rolling(20, min_periods=20).mean()
    vavg20 = v.rolling(20, min_periods=20).mean()
    hi = c.rolling(int(cj.get("breakout_days", 55))).max().shift(1)
    win = int(cj.get("breakout_window", 5))
    last = -1
    data_date = str(c.index[last].date())
    vr = v / vavg20
    base = (c > sma200) & (c >= cj.get("min_price", 100)) & (to20 >= cj.get("min_turnover", 5e7)) & (vr <= 3)
    bo = (c > hi) & base
    sig = (bo.rolling(win).max() > 0) & base
    d200 = c / sma200 - 1
    uni = pd.read_csv(os.path.join(sl.DATA, "jp_universe.csv"), dtype={"code": str})
    names = dict(zip(uni["ticker"], uni["name"]))

    held = {x["ticker"] for x in port_open(port, "jp")}
    cands = []
    row = sig.iloc[last]
    for tk in row[row.fillna(False)].index:
        if tk in held:
            continue
        px = float(c[tk].iloc[last])
        to = float(to20[tk].iloc[last])
        dd = float(d200[tk].iloc[last])
        vv = float(vr[tk].iloc[last]) if np.isfinite(vr[tk].iloc[last]) else 9.9
        if dd > 0.25:
            continue
        star = 3 if (dd <= 0.10 and vv <= 1.5 and to >= 5e8) else 2
        cands.append({"ticker": tk, "code": tk.replace(".T", ""), "name": names.get(tk, tk),
                      "close": px, "score": star, "turnover_oku": to / 1e8, "d200": dd,
                      "sma25": float(sma25[tk].iloc[last]), "stop": round(px * 0.90, 1)})
    cands.sort(key=lambda x: (-x["score"], -x["turnover_oku"]))

    # 保有の判定
    exits, status = [], []
    for x in port_open(port, "jp"):
        tk = x["ticker"]
        if tk not in c.columns or not np.isfinite(c[tk].iloc[last]):
            continue
        px = float(c[tk].iloc[last])
        stop = float(x.get("stop") or 0)
        held_days = int((c.index > pd.Timestamp(x["entry_date"])).sum())
        below = bool(sma25[tk].iloc[last] < sma75[tk].iloc[last])
        item = {"position_id": x["id"], "ticker": tk, "code": x["code"], "name": x["name"],
                "close": px, "stop": stop, "held": held_days, "sma25": float(sma25[tk].iloc[last]),
                "chg": px / float(x["entry_price"]) - 1, "shares": x["shares"]}
        if stop and px <= stop:
            item["exit"] = "stop"
            item["reason"] = f"終値が損切りライン{stop:,.0f}円を割った"
        elif below:
            item["exit"] = "dc"
            item["reason"] = "25日線が75日線を下回った"
        elif held_days >= int(cj.get("max_hold_days", 60)):
            item["exit"] = "time"
            item["reason"] = f"{cj.get('max_hold_days', 60)}営業日経過"
        if item.get("exit"):
            exits.append(item)
        status.append(item)
    return {"date": data_date, "candidates": cands, "exits": exits, "status": status,
            "regime": sl.jp_regime()}


def port_open(port, market=None):
    return [x for x in port["positions"] if x.get("status") == "open" and (market is None or x["market"] == market)]


# ---------------------------------------------------------------- 評価
def latest_prices(port, fx=None):
    """保有銘柄の最新終値（日本株は円、米国株はドル、投信は代用ETF×ドル円の円建て）"""
    out = {}
    jp = [x["ticker"] for x in port_open(port, "jp")]
    if jp:
        c = sl.load_wide("jp", tail_days=30)["close"]
        for tk in jp:
            if tk in c.columns:
                s = c[tk].dropna()
                if len(s):
                    out[tk] = (float(s.iloc[-1]), str(s.index[-1].date()),
                               float(s.iloc[-2]) if len(s) > 1 else float(s.iloc[-1]))
    us = [x["ticker"] for x in port_open(port, "us")]
    if us and os.path.exists(ETF_FILE):
        for tk in us:
            try:
                s = etf_close(tk)
                out[tk] = (float(s.iloc[-1]), str(s.index[-1].date()),
                           float(s.iloc[-2]) if len(s) > 1 else float(s.iloc[-1]))
            except Exception:
                pass
    funds = port_open(port, "fund")
    if funds and os.path.exists(ETF_FILE):
        fx = fx or sl.usdjpy()
        for x in funds:
            try:
                s = etf_close(x.get("proxy") or "QQQ")
                out[x["ticker"]] = (float(s.iloc[-1]) * fx, str(s.index[-1].date()), float(s.iloc[-2]) * fx)
            except Exception:
                pass
    return out


def valuation(port, prices, fx):
    """総資産（円）・保有ごとの評価"""
    rows = []
    total = float(port["cash_yen"])
    prev_total = float(port["cash_yen"])
    for x in port_open(port):
        px, d, prev = prices.get(x["ticker"], (x["entry_price"], "", x["entry_price"]))
        mult = fx if x["market"] == "us" else 1.0
        val = px * x["shares"] * mult
        cost = x["entry_price"] * x["shares"] * (x.get("fx_entry") or fx if x["market"] == "us" else 1.0)
        rows.append({**x, "price": px, "price_date": d, "value_yen": round(val), "cost_yen": round(cost),
                     "pnl_yen": round(val - cost), "pnl_pct": (val / cost - 1) if cost else 0.0,
                     "day_pct": (px / prev - 1) if prev else 0.0})
        total += val
        prev_total += prev * x["shares"] * mult
    return {"total_yen": round(total), "prev_total_yen": round(prev_total), "rows": rows}
