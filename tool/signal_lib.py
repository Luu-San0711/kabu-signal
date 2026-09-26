# -*- coding: utf-8 -*-
"""株シグナル 共通ライブラリ
データ増分更新 / 指標計算 / シグナル抽出 / LINE通知
"""
import gc
import glob
import json
import os
import time
import traceback
import urllib.request

# --- 実行環境の補正（macOSのFD上限とAnaconda証明書の混入対策） ---
try:
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if _soft < 8192:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(8192, _hard), _hard))
except Exception:
    pass
try:
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    os.environ["CURL_CA_BUNDLE"] = certifi.where()
except Exception:
    pass

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 株シグナル/
DATA = os.path.join(BASE, "data")
STATE = os.path.join(BASE, "tool", "state")
os.makedirs(STATE, exist_ok=True)
LOG = os.path.join(STATE, "tool.log")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_config():
    p = os.path.join(BASE, "tool", "config.json")
    with open(p) as f:
        cfg = json.load(f)
    # クラウド実行(GitHub Actions)では秘密情報を環境変数(Secrets)から受け取る
    for env, key in [("LINE_CHANNEL_ACCESS_TOKEN", "line_channel_access_token"),
                     ("SUPABASE_SERVICE_KEY", "supabase_service_key"),
                     ("SUPABASE_URL", "supabase_url")]:
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()
    return cfg


# ---------------------------------------------------------------- LINE通知
def line_broadcast(text, cfg):
    """LINE Messaging API broadcast（友だち全員=あなたに送信）"""
    token = cfg.get("line_channel_access_token", "").strip()
    ts = time.strftime("%m/%d %H:%M")
    with open(os.path.join(STATE, "last_notification.txt"), "w") as f:
        f.write(f"({ts})\n{text}\n")
    if not token:
        log("LINEトークン未設定のため送信スキップ（last_notification.txtに保存）")
        return False
    body = json.dumps({"messages": [{"type": "text", "text": text[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"LINE送信OK ({r.status})")
            return True
    except Exception as e:
        log(f"LINE送信失敗: {e}")
        return False


# ---------------------------------------------------------------- データ更新
def _part_files(prefix):
    return sorted(glob.glob(os.path.join(DATA, f"prices_{prefix}_part*.parquet")))


def update_prices(prefix, period="1mo"):
    """既存parquetパートに直近データを追記（バッチごと）"""
    import yfinance as yf
    if os.environ.get("KABU_SKIP_UPDATE"):
        log(f"{prefix} 価格更新スキップ（KABU_SKIP_UPDATE）")
        return True
    parts = _part_files(prefix)
    total_new = 0
    empty_streak = 0
    bad_batches = 0
    for p in parts:
        try:
            old = pd.read_parquet(p)
        except OSError as e:
            log(f"{os.path.basename(p)} 読込失敗（{e}）→ 更新中断")
            return False
        if len(old) == 0:
            continue
        tickers = sorted(old["ticker"].unique().tolist())
        try:
            df = yf.download(tickers=tickers, period=period, interval="1d",
                             group_by="ticker", auto_adjust=True, threads=6,
                             progress=False)
        except Exception as e:
            log(f"{os.path.basename(p)} 更新失敗: {str(e)[:150]}")
            bad_batches += 1
            if bad_batches >= 3:
                log(f"{prefix} 更新中断: 失敗が続くため既存データで続行します")
                return False
            continue
        if df is None or len(df) == 0:
            continue
        if not isinstance(df.columns, pd.MultiIndex):
            df = pd.concat({tickers[0]: df}, axis=1)
        frames = []
        for t in tickers:
            if t not in df.columns.get_level_values(0):
                continue
            sub = df[t].dropna(how="all")
            if len(sub) == 0:
                continue
            sub = sub.reset_index()
            sub.columns = [str(c).lower() for c in sub.columns]
            need = ["date", "open", "high", "low", "close", "volume"]
            if not all(c in sub.columns for c in need):
                continue
            sub = sub[need].copy()
            sub["ticker"] = t
            frames.append(sub)
        # バッチの取得率が低すぎる場合はネットワーク異常とみなす
        if len(frames) < max(1, int(len(tickers) * 0.2)):
            bad_batches += 1
            log(f"{os.path.basename(p)}: 取得率低（{len(frames)}/{len(tickers)}）")
            if bad_batches >= 3:
                log(f"{prefix} 更新中断: 接続異常が続くため既存データで続行します")
                return False
            if not frames:
                continue
        else:
            bad_batches = 0
        if not frames:
            empty_streak += 1
            if empty_streak >= 2 and total_new == 0:
                log(f"{prefix} 価格更新中断: ネットワーク接続に問題がある可能性（既存データで続行）")
                return False
            continue
        empty_streak = 0
        new = pd.concat(frames, ignore_index=True)
        new["date"] = pd.to_datetime(new["date"])
        if getattr(new["date"].dt, "tz", None) is not None:
            new["date"] = new["date"].dt.tz_localize(None)
        for c in ["open", "high", "low", "close"]:
            new[c] = new[c].astype("float32")
        old["date"] = pd.to_datetime(old["date"])
        merged = pd.concat([old, new], ignore_index=True)
        merged = merged.drop_duplicates(["date", "ticker"], keep="last")
        merged.to_parquet(p, index=False)
        total_new += len(new)
        del df, old, merged, frames
        gc.collect()
        time.sleep(0.5)
    log(f"{prefix} 価格更新: {len(parts)}パート, {total_new}行取り込み")
    return total_new > 0


def update_indices(period="3mo"):
    import yfinance as yf
    if os.environ.get("KABU_SKIP_UPDATE"):
        return
    p = os.path.join(DATA, "indices.parquet")
    old = pd.read_parquet(p)
    tickers = sorted(old["ticker"].unique().tolist())
    df = yf.download(tickers=tickers, period=period, interval="1d",
                     group_by="ticker", auto_adjust=True, threads=4, progress=False)
    frames = []
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
    if frames:
        new = pd.concat(frames, ignore_index=True)
        new["date"] = pd.to_datetime(new["date"])
        if getattr(new["date"].dt, "tz", None) is not None:
            new["date"] = new["date"].dt.tz_localize(None)
        old["date"] = pd.to_datetime(old["date"])
        merged = pd.concat([old, new], ignore_index=True).drop_duplicates(
            ["date", "ticker"], keep="last")
        merged.to_parquet(p, index=False)
        log("指数更新OK")


# ---------------------------------------------------------------- ロード
def load_wide(prefix, tail_days=420):
    frames = []
    for p in _part_files(prefix):
        d = pd.read_parquet(p)
        if len(d):
            frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    cutoff = df["date"].max() - pd.Timedelta(days=int(tail_days * 1.7))
    df = df[df["date"] >= cutoff]
    df = df.drop_duplicates(["date", "ticker"], keep="last")
    wide = {}
    for col in ["open", "high", "low", "close", "volume"]:
        wide[col] = df.pivot(index="date", columns="ticker", values=col).sort_index()
    return wide


def load_index_close(ticker):
    d = pd.read_parquet(os.path.join(DATA, "indices.parquet"))
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["ticker"] == ticker].drop_duplicates("date", keep="last")
    return d.set_index("date")["close"].sort_index()


# ---------------------------------------------------------------- 日本株スキャン
def jp_regime():
    n225 = load_index_close("^N225")
    sma200 = n225.rolling(200, min_periods=200).mean()
    bull = bool(n225.iloc[-1] > sma200.iloc[-1])
    calm = bool(n225.pct_change(5).iloc[-1] > -0.03)
    return {"bull": bull, "calm": calm, "ok": bull and calm,
            "n225": float(n225.iloc[-1]), "sma200": float(sma200.iloc[-1]),
            "date": str(n225.index[-1].date())}


def jp_daily(cfg, watch):
    """日次スキャン一式:
    - 地合い良好時のGC候補（実証済みスコア★2/★3のみ、株数・損切り価格つき）
    - 監視中銘柄の手仕舞い判定（損切り接近/デッドクロス/期限）
    """
    cfgjp = cfg.get("jp", {})
    budget = float(cfgjp.get("budget_per_trade_yen", 50000))
    stop_pct = float(cfgjp.get("stop_pct", 0.10))
    max_hold = int(cfgjp.get("max_hold_days", 60))

    wide = load_wide("jp")
    c, v = wide["close"], wide["volume"]
    low = wide["low"]
    sma25 = c.rolling(25, min_periods=25).mean()
    sma75 = c.rolling(75, min_periods=75).mean()
    sma200 = c.rolling(200, min_periods=200).mean()
    to20 = (c * v).rolling(20, min_periods=20).mean()
    vavg20 = v.rolling(20, min_periods=20).mean()
    last, prev = -1, -2
    data_date = c.index[-1]

    uni = pd.read_csv(os.path.join(DATA, "jp_universe.csv"), dtype={"code": str})
    names = dict(zip(uni["ticker"], uni["name"]))

    # --- 新規候補 ---
    gc_today = (sma25.iloc[last] > sma75.iloc[last]) & (sma25.iloc[prev] <= sma75.iloc[prev])
    ok = (
        gc_today
        & (c.iloc[last] > sma200.iloc[last])
        & (c.iloc[last] >= cfgjp.get("min_price", 100))
        & (to20.iloc[last] >= cfgjp.get("min_turnover", 5e7))
    )
    picks = []
    for tk in ok[ok.fillna(False)].index:
        px = float(c[tk].iloc[last])
        vr = float(v[tk].iloc[last] / vavg20[tk].iloc[last]) if vavg20[tk].iloc[last] > 0 else 9.9
        d200 = float(px / sma200[tk].iloc[last] - 1)
        to = float(to20[tk].iloc[last])
        # 実証済みスコア: vr>3は期待値マイナス(除外) / 乖離25%超は不安定(除外)
        if vr > 3 or d200 > 0.25:
            continue
        score = 3 if (d200 <= 0.10 and vr <= 1.5 and to >= 5e8) else 2
        shares = int(budget // px)
        if shares <= 0:
            continue
        picks.append({
            "ticker": tk, "code": tk.replace(".T", ""), "name": names.get(tk, tk),
            "close": px, "score": score, "turnover_oku": to / 1e8,
            "shares": shares, "amount": shares * px,
            "stop": round(px * (1 - stop_pct), 1),
        })
    picks.sort(key=lambda p: (-p["score"], -p["turnover_oku"]))
    picks = picks[: cfgjp.get("top_n", 5)]

    # --- 監視中銘柄の判定 ---
    watch_report, still_watch = [], []
    dates = c.index
    for w in watch:
        tk = w["ticker"]
        if tk not in c.columns or not np.isfinite(c[tk].iloc[last]):
            continue
        px = float(c[tk].iloc[last])
        entry = float(w["entry_close"])
        chg = px / entry - 1
        held = int((dates > pd.Timestamp(w["added"])).sum())
        dc = bool((sma25[tk].iloc[last] < sma75[tk].iloc[last])
                  and (sma25[tk].iloc[prev] >= sma75[tk].iloc[prev]))
        below_dc = bool(sma25[tk].iloc[last] < sma75[tk].iloc[last])
        stop_hit = bool(np.isfinite(low[tk].iloc[last]) and low[tk].iloc[last] <= w["stop"])
        item = {"ticker": tk, "code": w["code"], "name": w["name"], "chg": chg,
                "held": held, "close": px, "stop": w["stop"],
                "added": w.get("added", "")}
        if stop_hit or px <= w["stop"]:
            item["action"] = "🔴 損切りライン到達 → 売り（成行）"
        elif dc or below_dc:
            item["action"] = "🟠 デッドクロス → 手仕舞い（翌朝寄付成行）"
        elif held >= max_hold:
            item["action"] = f"🟠 {max_hold}営業日経過 → 手仕舞い（翌朝寄付成行）"
        elif px <= w["stop"] * 1.03:
            item["action"] = "⚠️ 損切りラインまで3%未満"
            still_watch.append(w)
        else:
            item["action"] = f"継続（損切り {w['stop']:,.0f}円）"
            still_watch.append(w)
        watch_report.append(item)
    return picks, watch_report, still_watch, str(data_date.date())


def chart_series(prefix, tickers, days=130, smas=(25, 75)):
    """ダッシュボード用のチャートデータ（ローソク足OHLC＋移動平均）"""
    if not tickers:
        return {}
    wide = load_wide(prefix, tail_days=days + 220)
    c = wide["close"]
    out = {}

    def col(df, tk, idx):
        v = df[tk].reindex(idx)
        return [None if not np.isfinite(x) else round(float(x), 2) for x in v.values]

    for tk in tickers:
        if tk not in c.columns:
            continue
        s = c[tk].dropna()
        if len(s) < 10:
            continue
        base = s.iloc[-days:]
        idx = base.index
        item = {"dates": [str(d.date()) for d in idx],
                "close": [round(float(v), 2) for v in base.values],
                "open": col(wide["open"], tk, idx),
                "high": col(wide["high"], tk, idx),
                "low": col(wide["low"], tk, idx)}
        for n in smas:
            ma = s.rolling(n, min_periods=n).mean().reindex(idx)
            item[f"sma{n}"] = [None if not np.isfinite(v) else round(float(v), 2)
                               for v in ma.values]
        out[tk] = item
    return out


# ---------------------------------------------------------------- 米国株モメンタム
def us_momentum_top(cfg):
    cfgus = cfg.get("us", {})
    wide = load_wide("us")
    c, v = wide["close"], wide["volume"]
    to20 = (c * v).rolling(20, min_periods=20).mean()
    lc = to20.iloc[-1].rank(ascending=False) <= cfgus.get("universe_liquidity_top", 500)
    # 直近260営業日のうち250日以上データがある銘柄のみ（新規上場・スピンオフ直後の歪み除外）
    seasoned = c.iloc[-260:].notna().sum() >= 250
    mom = c.shift(20).iloc[-1] / c.shift(250).iloc[-1] - 1
    mom = mom.where(lc & seasoned).dropna()
    mom = mom[mom > 0]
    top = mom.sort_values(ascending=False)
    uni = pd.read_csv(os.path.join(DATA, "us_universe.csv"))
    import re as _re
    def _clean(nm):
        nm = str(nm)
        nm = _re.split(r" - | Common Stock| Class [A-C]| Ordinary Share", nm)[0]
        return nm.strip()[:32]
    names = {t: _clean(n) for t, n in zip(uni["ticker"], uni["name"])}
    n = cfgus.get("top_n", 20)
    res = []
    for tk in top.index[:n]:
        res.append({"ticker": tk, "name": names.get(tk, tk),
                    "mom": float(top[tk]), "close": float(c[tk].iloc[-1]),
                    "rank": int(list(top.index).index(tk)) + 1})
    ranks = {tk: i + 1 for i, tk in enumerate(top.index)}
    return res, ranks, str(c.index[-1].date())


def dashboard_url(cfg):
    return cfg.get("github_pages_url") or None


def usdjpy():
    try:
        r = load_index_close("JPY=X")
        v = float(r.iloc[-1])
        if 50 < v < 500:
            return v
    except Exception:
        pass
    return 150.0


def load_state(name, default):
    p = os.path.join(STATE, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def save_state(name, obj):
    with open(os.path.join(STATE, name), "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
