# -*- coding: utf-8 -*-
"""株シグナル ダッシュボード生成・アップロード
state_jp.json / state_us.json から自己完結HTMLを生成し、
Supabase Storage にアップロード（設定があれば）。ローカルにも常に保存。
"""
import json
import os
import time
import urllib.request

import signal_lib as sl

OUT_LOCAL = os.path.join(sl.BASE, "dashboard.html")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html():
    jp = sl.load_state("state_jp.json", {})
    us = sl.load_state("state_us.json", {})
    reg = jp.get("regime", {})
    picks = jp.get("last_picks", [])
    watch = jp.get("last_watch_report", [])
    port = us.get("portfolio", [])
    us_meta = us.get("meta", {})
    updated = time.strftime("%Y-%m-%d %H:%M")
    data_date = jp.get("last_date", "-")

    reg_ok = reg.get("ok", False)
    reg_badge = ("<span class='badge ok'>🟢 地合い良好</span>" if reg_ok
                 else "<span class='badge ng'>🔴 地合い悪化・新規買い停止</span>")
    reg_detail = ""
    if reg:
        reg_detail = (f"日経平均 {reg.get('n225', 0):,.0f} ／ 200日線 {reg.get('sma200', 0):,.0f}"
                      f"（乖離 {(reg.get('n225', 1)/max(reg.get('sma200', 1), 1)-1)*100:+.1f}%）")

    # --- 日本株候補カード ---
    star = {3: "★★★", 2: "★★☆"}
    jp_cards = ""
    if reg_ok and picks:
        for p in picks:
            unit = "現物" if p["shares"] >= 100 else "かぶミニ"
            jp_cards += f"""
<div class="card">
  <div class="cardhead"><span class="code">{esc(p['code'])}</span>
    <span class="name">{esc(p['name'])}</span>
    <span class="stars">{star.get(p['score'], '★')}</span></div>
  <div class="row big">{p['close']:,.0f}円 × {p['shares']}株 ≒ <b>{p['amount']/10000:.1f}万円</b>
    <span class="unit">{unit}・寄付成行</span></div>
  <div class="row"><span class="lbl">損切り</span><b class="stopv">{p['stop']:,.0f}円</b>
    <span class="unit">逆指値 −10%</span></div>
  <div class="row"><span class="lbl">手仕舞い</span>デッドクロス通知まで保有
    <span class="unit">最長60営業日・平均40日</span></div>
  <div class="row sub">売買代金 {p['turnover_oku']:.1f}億円/日</div>
</div>"""
    elif not reg_ok:
        jp_cards = "<div class='empty'>地合い悪化中のため新規買いは停止しています</div>"
    else:
        jp_cards = "<div class='empty'>本日の新規候補はありません</div>"

    # --- 監視中テーブル ---
    watch_rows = ""
    for w in watch:
        chg = w.get("chg", 0)
        cls = "pos" if chg >= 0 else "neg"
        act = esc(w.get("action", ""))
        acls = "act-sell" if ("売り" in act or "手仕舞い" in act) else ("act-warn" if "⚠" in act else "")
        # 損切りまでの距離
        dist = (w["close"] / w["stop"] - 1) * 100 if w.get("stop") else None
        bar = ""
        if dist is not None:
            pct = max(0, min(100, dist / 25 * 100))
            bar = f"<div class='bar'><i style='width:{pct:.0f}%'></i></div><span class='sub'>損切りまで {dist:+.1f}%</span>"
        watch_rows += f"""
<tr><td><b>{esc(w['code'])}</b> {esc(w['name'])}</td>
<td class="{cls}">{chg:+.1%}</td><td>{w['held']}日</td>
<td class="{acls}">{act}<br>{bar}</td></tr>"""
    watch_html = (f"<table><tr><th>銘柄</th><th>損益</th><th>保有</th><th>状態</th></tr>{watch_rows}</table>"
                  if watch_rows else "<div class='empty'>監視中の銘柄はありません</div>")

    # --- 米国ポートフォリオ ---
    us_rows = ""
    total = 0.0
    for p in port:
        total += p.get("cost", 0)
        us_rows += f"""
<tr><td><span class="rank">{p.get('rank','-')}位</span> <b>{esc(p['ticker'])}</b>
<span class="sub">{esc(p.get('name',''))}</span></td>
<td>${p.get('close',0):,.2f} × {p.get('shares',0)}株</td>
<td>${p.get('cost',0):,.0f}</td><td class="pos">{p.get('mom',0)*100:+.0f}%</td></tr>"""
    us_html = "<div class='empty'>初回リバランス前です</div>"
    if us_rows:
        us_html = (f"<table><tr><th>銘柄（勢い順位）</th><th>数量</th><th>金額</th><th>12ヶ月</th></tr>{us_rows}</table>"
                   f"<div class='total'>合計 ${total:,.0f}"
                   + (f" ／ 予算 ${us_meta.get('budget_usd', 0):,.0f}" if us_meta.get("budget_usd") else "")
                   + f"<span class='sub'>　次回入替: 来月第1営業日（土曜に健全性チェック）</span></div>")
    skipped = us_meta.get("skipped", [])
    if skipped:
        us_html += f"<div class='note'>予算不足でスキップ中の上位銘柄: {esc(', '.join(skipped[:6]))}</div>"

    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>株シグナル</title>
<style>
:root{{--bg:#FBFAF6;--card:#fff;--ink:#22261F;--muted:#6a7064;--line:#E4E1D5;
--green:#059669;--red:#C2453C;--amber:#8A5A12;--amberbg:#FBF3E4;--chip:#F0F7F1}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14171A;--card:#1D2226;--ink:#E7E9E3;
--muted:#9BA196;--line:#2E343A;--green:#10B981;--red:#E07B72;--amber:#E3B36A;
--amberbg:#2C2417;--chip:#1B2A22}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;font-size:15px;line-height:1.7}}
.wrap{{max-width:640px;margin:0 auto;padding:20px 14px 60px}}
h1{{font-size:20px;margin:4px 0 2px}}
.upd{{font-size:12px;color:var(--muted)}}
.badge{{display:inline-block;padding:4px 12px;border-radius:99px;font-weight:700;
font-size:13.5px;background:var(--chip);margin:10px 0 2px}}
.badge.ng{{background:var(--amberbg);color:var(--red)}}
.regdetail{{font-size:12.5px;color:var(--muted)}}
h2{{font-size:15px;margin:26px 0 8px;border-left:4px solid var(--green);padding-left:8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:12px 14px;margin:10px 0}}
.cardhead{{display:flex;gap:8px;align-items:baseline}}
.code{{font-weight:800;font-size:16px}}
.name{{flex:1;font-size:14px}}
.stars{{color:var(--green);font-weight:700;letter-spacing:.1em}}
.row{{margin:4px 0;font-size:14px}}
.row.big{{font-size:15px}}
.lbl{{display:inline-block;background:var(--chip);border-radius:5px;padding:0 7px;
font-size:12px;margin-right:7px;color:var(--muted)}}
.stopv{{color:var(--red)}}
.unit,.sub{{font-size:12px;color:var(--muted);margin-left:6px}}
.empty{{color:var(--muted);background:var(--card);border:1px dashed var(--line);
border-radius:10px;padding:14px;text-align:center;font-size:13.5px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:12px;overflow:hidden;font-size:13.5px}}
th{{font-size:11.5px;color:var(--muted);font-weight:500;text-align:left;padding:8px 10px;
border-bottom:1px solid var(--line)}}
td{{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;
font-variant-numeric:tabular-nums}}
tr:last-child td{{border-bottom:0}}
.pos{{color:var(--green)}}.neg{{color:var(--red)}}
.act-sell{{color:var(--red);font-weight:700}}.act-warn{{color:var(--amber)}}
.rank{{display:inline-block;background:var(--chip);border-radius:5px;padding:0 6px;
font-size:11.5px;color:var(--muted)}}
.bar{{height:5px;background:var(--line);border-radius:3px;margin:4px 0 1px;max-width:140px}}
.bar i{{display:block;height:100%;background:var(--green);border-radius:3px}}
.total{{margin:8px 2px;font-weight:700}}
.note{{background:var(--amberbg);color:var(--amber);border-radius:8px;padding:8px 12px;
font-size:12.5px;margin-top:8px}}
.foot{{margin-top:34px;padding-top:12px;border-top:1px solid var(--line);
font-size:11.5px;color:var(--muted)}}
</style></head><body><div class="wrap">
<h1>📊 株シグナル</h1>
<div class="upd">更新 {updated} ／ データ {esc(data_date)} 時点</div>
{reg_badge}
<div class="regdetail">{reg_detail}</div>

<h2>日本株｜新規買い候補（翌営業日 寄付成行）</h2>
{jp_cards}

<h2>日本株｜監視中の保有銘柄</h2>
{watch_html}

<h2>米国株｜モメンタム・ポートフォリオ</h2>
{us_html}

<div class="foot">
シグナル根拠（過去8年検証）: ★★★=勝率47%・平均+1.8%/回 ／ ★★☆=勝率42%・平均+1.8%/回
／ 勝ち平均+16%・負け平均−8%（利大損小型）。米国株: 年率+24〜26%・最大DD−52%（上位20等金額時）。<br>
本ページはバックテストに基づく情報提供であり投資助言ではありません。発注はご自身の判断で。
</div>
</div></body></html>"""
    return html


def build_data():
    """GitHub Pages上のビューアが読むJSONデータ（チャート込み）"""
    jp = sl.load_state("state_jp.json", {})
    us = sl.load_state("state_us.json", {})
    picks = jp.get("last_picks", [])
    watch = jp.get("last_watch_report", [])
    port = us.get("portfolio", [])
    for w in watch:  # 旧形式の互換
        w.setdefault("ticker", w.get("code", "") + ".T")
    jp_tickers = sorted({p["ticker"] for p in picks} | {w["ticker"] for w in watch})
    us_tickers = [p["ticker"] for p in port]
    charts = {}
    try:
        charts.update(sl.chart_series("jp", jp_tickers, days=130, smas=(25, 75)))
    except Exception as e:
        sl.log(f"JPチャート生成スキップ: {str(e)[:120]}")
    try:
        charts.update(sl.chart_series("us", us_tickers, days=250, smas=()))
    except Exception as e:
        sl.log(f"USチャート生成スキップ: {str(e)[:120]}")
    return {
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "data_date": jp.get("last_date", "-"),
        "regime": jp.get("regime", {}),
        "picks": picks,
        "watch": watch,
        "port": port,
        "us_meta": us.get("meta", {}),
        "us_entry_date": us.get("last_rebalance", ""),
        "charts": charts,
    }


OUT_JSON = os.path.join(sl.BASE, "data.json")


def upload_supabase(cfg):
    body = json.dumps(build_data(), ensure_ascii=False).encode("utf-8")
    # GitHub Pages 用: リポジトリ直下の data.json（ワークフローが commit して公開）
    with open(OUT_JSON, "wb") as f:
        f.write(body)
    sl.log("ダッシュボード用 data.json 生成OK")
    url = cfg.get("supabase_url", "").rstrip("/")
    key = cfg.get("supabase_service_key", "")
    bucket = cfg.get("supabase_bucket", "dashboard")
    if not url or not key:
        return True
    endpoint = f"{url}/storage/v1/object/{bucket}/data.json"
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "apikey": key,
                 "Content-Type": "application/json",
                 "x-upsert": "true", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            sl.log(f"ダッシュボードデータ更新OK ({r.status})")
            return True
    except Exception as e:
        sl.log(f"ダッシュボードupload失敗: {str(e)[:200]}")
        return False


def refresh(cfg=None):
    cfg = cfg or sl.load_config()
    html = build_html()
    with open(OUT_LOCAL, "w", encoding="utf-8") as f:
        f.write(html)
    upload_supabase(cfg)
    return sl.dashboard_url(cfg)


if __name__ == "__main__":
    refresh()
