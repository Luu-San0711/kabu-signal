# -*- coding: utf-8 -*-
"""
株シグナル: バックテスト用データ一括取得スクリプト
- 東証全銘柄（プライム/スタンダード/グロースの内国株式）
- 米国株（NYSE/NASDAQ/AMEX の普通株、ETF除外）
- 主要指数
出力: data/ フォルダに parquet 形式で保存。再実行すると続きから再開します。
"""
import io
import json
import os
import re
import sys
import time
import traceback

import pandas as pd
import requests

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_PATH = os.path.join(DATA_DIR, "fetch.log")

START_DATE = os.environ.get("KABU_START_DATE", "2018-01-01")
CHUNK = 200  # yfinance一括ダウンロードの銘柄数/バッチ

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _fallback_universe(out):
    """取得元サイトに繋がらない環境用: リポジトリ同梱の銘柄リスト(tool/universe/)を使う"""
    if os.path.exists(out):
        return
    src = os.path.join(os.path.dirname(DATA_DIR), "tool", "universe", os.path.basename(out))
    if os.path.exists(src):
        import shutil
        shutil.copy(src, out)
        log(f"同梱の銘柄リストを使用: {os.path.basename(out)}")


# ---------------------------------------------------------------- JP universe
def fetch_jp_universe():
    out = os.path.join(DATA_DIR, "jp_universe.csv")
    _fallback_universe(out)
    if os.path.exists(out):
        df = pd.read_csv(out, dtype={"code": str})
        log(f"日本株リスト: 既存ファイル使用 ({len(df)}銘柄)")
        return df
    log("JPXから東証上場銘柄一覧を取得中...")
    r = None
    for url in ("https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
                "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"):
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code == 200:
            break
    r.raise_for_status()
    df = None
    for engine in ("openpyxl", "xlrd"):
        try:
            df = pd.read_excel(io.BytesIO(r.content), dtype=str, engine=engine)
            break
        except Exception:
            continue
    if df is None:
        raise RuntimeError("JPX一覧の解析に失敗しました")
    # 列名: 日付, コード, 銘柄名, 市場・商品区分, 33業種コード, 33業種区分, ...
    col_code = [c for c in df.columns if "コード" in c][0]
    col_name = [c for c in df.columns if "銘柄名" in c][0]
    col_mkt = [c for c in df.columns if "区分" in c and "市場" in c][0]
    col_sec = [c for c in df.columns if c.strip() == "33業種区分"]
    col_sec = col_sec[0] if col_sec else None
    m = df[col_mkt].fillna("")
    keep = m.str.contains("内国株式") & ~m.str.contains("プロ|PRO", regex=True)
    df = df[keep].copy()
    uni = pd.DataFrame({
        "code": df[col_code].str.strip(),
        "name": df[col_name].str.strip(),
        "market": df[col_mkt].str.strip(),
        "sector": df[col_sec].str.strip() if col_sec else "",
    })
    uni["ticker"] = uni["code"] + ".T"
    uni.to_csv(out, index=False)
    log(f"日本株リスト取得完了: {len(uni)}銘柄 -> {out}")
    return uni


# ---------------------------------------------------------------- US universe
BAD_NAME = re.compile(r"Warrant|Right(s)?\b|\bUnit(s)?\b|Preferred|Depositary|Notes? due|% ", re.I)


def fetch_us_universe():
    out = os.path.join(DATA_DIR, "us_universe.csv")
    if os.path.exists(out):
        df = pd.read_csv(out)
        log(f"米国株リスト: 既存ファイル使用 ({len(df)}銘柄)")
        return df
    log("NASDAQ Traderから米国上場銘柄一覧を取得中...")
    rows = []
    r = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", headers=UA, timeout=60)
    r.raise_for_status()
    nas = pd.read_csv(io.StringIO(r.text), sep="|")
    nas = nas[nas["Symbol"].notna() & (nas["Test Issue"] == "N") & (nas["ETF"] == "N")]
    for _, x in nas.iterrows():
        rows.append((str(x["Symbol"]).strip(), str(x["Security Name"]), "NASDAQ"))
    r = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", headers=UA, timeout=60)
    r.raise_for_status()
    oth = pd.read_csv(io.StringIO(r.text), sep="|")
    oth = oth[oth["ACT Symbol"].notna() & (oth["Test Issue"] == "N") & (oth["ETF"] == "N")]
    exch_map = {"N": "NYSE", "A": "AMEX", "P": "ARCA", "Z": "BATS", "V": "IEX"}
    for _, x in oth.iterrows():
        rows.append((str(x["ACT Symbol"]).strip(), str(x["Security Name"]), exch_map.get(str(x["Exchange"]), str(x["Exchange"]))))
    uni = pd.DataFrame(rows, columns=["symbol", "name", "exchange"]).drop_duplicates("symbol")
    uni = uni[~uni["name"].fillna("").str.contains(BAD_NAME)]
    uni = uni[~uni["symbol"].str.contains(r"[\$=]")]
    uni["ticker"] = uni["symbol"].str.replace(".", "-", regex=False)
    uni = uni[uni["ticker"].str.len() <= 6]
    uni.to_csv(out, index=False)
    log(f"米国株リスト取得完了: {len(uni)}銘柄 -> {out}")
    return uni


# ---------------------------------------------------------------- price fetch
def download_chunk(tickers):
    import yfinance as yf
    df = yf.download(
        tickers=tickers, start=START_DATE, interval="1d",
        group_by="ticker", auto_adjust=True, threads=True,
        progress=False,
    )
    frames = []
    if df is None or len(df) == 0:
        return pd.DataFrame()
    if not isinstance(df.columns, pd.MultiIndex):
        df = pd.concat({tickers[0]: df}, axis=1)
    for t in tickers:
        if t not in df.columns.get_level_values(0):
            continue
        sub = df[t].dropna(how="all")
        if len(sub) == 0:
            continue
        sub = sub.reset_index()
        sub.columns = [str(c).lower() for c in sub.columns]
        sub = sub.rename(columns={"index": "date"})
        need = ["date", "open", "high", "low", "close", "volume"]
        if not all(c in sub.columns for c in need):
            continue
        sub = sub[need].copy()
        sub["ticker"] = t
        frames.append(sub)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    for c in ["open", "high", "low", "close"]:
        out[c] = out[c].astype("float32")
    out["volume"] = out["volume"].fillna(0).astype("float64")
    return out


def fetch_prices(tickers, prefix):
    tickers = sorted(set(tickers))
    n_parts = (len(tickers) + CHUNK - 1) // CHUNK
    log(f"{prefix}: {len(tickers)}銘柄を{n_parts}バッチで取得します")
    failed_parts = []
    for i in range(n_parts):
        part_path = os.path.join(DATA_DIR, f"prices_{prefix}_part{i:03d}.parquet")
        if os.path.exists(part_path):
            continue
        batch = tickers[i * CHUNK:(i + 1) * CHUNK]
        ok = False
        for attempt in (1, 2, 3):
            try:
                out = download_chunk(batch)
                if len(out) > 0:
                    out.to_parquet(part_path, index=False)
                    log(f"{prefix} バッチ{i+1}/{n_parts}: {out['ticker'].nunique()}銘柄 {len(out)}行 保存")
                else:
                    log(f"{prefix} バッチ{i+1}/{n_parts}: データなし（上場廃止等の可能性）")
                    pd.DataFrame().to_parquet(part_path)
                ok = True
                break
            except Exception as e:
                log(f"{prefix} バッチ{i+1}/{n_parts} 試行{attempt}失敗: {str(e)[:200]}")
                time.sleep(15 * attempt)
        if not ok:
            failed_parts.append(i)
        time.sleep(1.0)
    return failed_parts


def fetch_indices():
    path = os.path.join(DATA_DIR, "indices.parquet")
    if os.path.exists(path):
        log("指数データ: 既存ファイル使用")
        return
    idx = ["^N225", "^GSPC", "^IXIC", "^NDX", "^RUT", "1306.T", "1591.T", "^VIX", "JPY=X"]
    out = download_chunk(idx)
    if len(out) > 0:
        out.to_parquet(path, index=False)
        log(f"指数データ保存: {out['ticker'].nunique()}系列")


def main():
    log("===== データ取得開始 =====")
    try:
        jp = fetch_jp_universe()
    except Exception:
        log("日本株リスト取得に失敗:\n" + traceback.format_exc())
        jp = None
    try:
        us = fetch_us_universe()
    except Exception:
        log("米国株リスト取得に失敗:\n" + traceback.format_exc())
        _fallback_universe(os.path.join(DATA_DIR, "us_universe.csv"))
        us = fetch_us_universe() if os.path.exists(os.path.join(DATA_DIR, "us_universe.csv")) else None
    if jp is None:
        _fallback_universe(os.path.join(DATA_DIR, "jp_universe.csv"))
        if os.path.exists(os.path.join(DATA_DIR, "jp_universe.csv")):
            jp = fetch_jp_universe()

    failed = {}
    try:
        fetch_indices()
    except Exception:
        log("指数データ取得に失敗:\n" + traceback.format_exc())
    if jp is not None:
        failed["jp"] = fetch_prices(jp["ticker"].tolist(), "jp")
    if us is not None:
        failed["us"] = fetch_prices(us["ticker"].tolist(), "us")

    # サマリー
    summary = {"finished_at": time.strftime("%Y-%m-%d %H:%M:%S"), "failed_parts": failed}
    for prefix in ["jp", "us"]:
        import glob
        parts = sorted(glob.glob(os.path.join(DATA_DIR, f"prices_{prefix}_part*.parquet")))
        n_rows, n_tk, dmin, dmax = 0, 0, None, None
        for p in parts:
            try:
                d = pd.read_parquet(p, columns=["date", "ticker"])
            except Exception:
                continue
            if len(d) == 0:
                continue
            n_rows += len(d)
            n_tk += d["ticker"].nunique()
            lo, hi = d["date"].min(), d["date"].max()
            dmin = lo if dmin is None or lo < dmin else dmin
            dmax = hi if dmax is None or hi > dmax else dmax
        summary[prefix] = {"parts": len(parts), "rows": int(n_rows), "tickers": int(n_tk),
                           "from": str(dmin), "to": str(dmax)}
    with open(os.path.join(DATA_DIR, "SUMMARY.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log("サマリー: " + json.dumps(summary, ensure_ascii=False))
    if any(v for v in failed.values()):
        log("一部バッチが失敗しています。もう一度実行すると失敗分だけ再取得します。")
        sys.exit(1)
    log("===== 全データ取得完了 =====")


if __name__ == "__main__":
    main()
