# -*- coding: utf-8 -*-
"""GitHub Actions 運用用: 価格データの確保とバックアップ（外部サービス不要）

  python tool/cloud_sync.py ensure-data
      data/ にparquetが無ければ、①GitHub Release "data-backup" から復元
      ②それも無ければ Yahoo Finance から直近分（約2.5年）を新規取得
  python tool/cloud_sync.py backup
      data/ を data-backup.tar.gz にまとめて GitHub Release "data-backup" にアップロード（上書き）

監視状態(tool/state/*.json)とダッシュボード用 data.json はワークフロー側で git commit して保存します。
"""
import datetime as dt
import glob
import os
import subprocess
import sys
import tarfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
RELEASE_TAG = "data-backup"
ASSET = "data-backup.tar.gz"
KEEP_DAYS = 900  # 新規取得時に遡る日数（12ヶ月モメンタム＋200日線に十分）


def _has_data():
    return bool(glob.glob(os.path.join(DATA, "prices_jp_part*.parquet"))
                and glob.glob(os.path.join(DATA, "prices_us_part*.parquet"))
                and os.path.exists(os.path.join(DATA, "indices.parquet")))


def _gh(*args, check=False):
    print("$ gh", " ".join(args), flush=True)
    return subprocess.run(["gh", *args], check=check, text=True,
                          capture_output=True)


def restore_from_release():
    r = _gh("release", "download", RELEASE_TAG, "--pattern", ASSET, "--dir", "/tmp", "--clobber")
    if r.returncode != 0:
        print("バックアップなし:", (r.stderr or "").strip()[:200])
        return False
    os.makedirs(DATA, exist_ok=True)
    with tarfile.open(os.path.join("/tmp", ASSET), "r:gz") as t:
        t.extractall(BASE)
    print("バックアップから復元OK")
    return _has_data()


def full_fetch():
    start = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    os.environ["KABU_START_DATE"] = start
    print(f"価格データを新規取得します（{start}〜）。30分前後かかります", flush=True)
    r = subprocess.run([sys.executable, os.path.join(BASE, "scripts", "data_fetch.py")])
    return r.returncode == 0 and _has_data()


def ensure_data():
    if _has_data():
        print("価格データあり（キャッシュ）")
        return
    if restore_from_release():
        return
    if not full_fetch():
        print("価格データを用意できませんでした")
        sys.exit(1)


def backup():
    if not _has_data():
        print("データが無いためバックアップをスキップ")
        return
    path = os.path.join("/tmp", ASSET)
    with tarfile.open(path, "w:gz") as t:
        for p in sorted(glob.glob(os.path.join(DATA, "*"))):
            if p.endswith(".log"):
                continue
            t.add(p, arcname=os.path.join("data", os.path.basename(p)))
    size = os.path.getsize(path) / 1e6
    r = _gh("release", "view", RELEASE_TAG)
    if r.returncode != 0:
        _gh("release", "create", RELEASE_TAG, "--title", "価格データ バックアップ（自動）",
            "--notes", "GitHub Actions が毎回上書き保存する価格データ。手で触る必要はありません。")
    r = _gh("release", "upload", RELEASE_TAG, path, "--clobber")
    print(f"バックアップ {'OK' if r.returncode == 0 else '失敗: ' + (r.stderr or '')[:200]}（{size:.0f}MB）")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "ensure-data":
        ensure_data()
    elif cmd == "backup":
        backup()
    else:
        print(__doc__)
        sys.exit(1)
