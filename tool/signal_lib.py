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
    for env, key in [("LINE_CHANNEL_ACCESS_TOKEN", "line_channel_access_token")]:
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


# ---------------------------------------------------------------- 日本株の地合い
def jp_regime():
    n225 = load_index_close("^N225")
    sma200 = n225.rolling(200, min_periods=200).mean()
    bull = bool(n225.iloc[-1] > sma200.iloc[-1])
    calm = bool(n225.pct_change(5).iloc[-1] > -0.03)
    return {"bull": bull, "calm": calm, "ok": bull and calm,
            "n225": float(n225.iloc[-1]), "sma200": float(sma200.iloc[-1]),
            "date": str(n225.index[-1].date())}


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
