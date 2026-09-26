# -*- coding: utf-8 -*-
"""日次ジョブ（平日19:30想定）: 日本株スキャン＋監視銘柄の手仕舞い判定をLINE通知"""
import sys
import traceback

import signal_lib as sl

STAR = {3: "★★★", 2: "★★☆"}


def main():
    cfg = sl.load_config()
    if not cfg.get("jp", {}).get("enabled", True):
        return
    sl.log("=== 日次JPジョブ開始 ===")
    updated = False
    try:
        updated = bool(sl.update_prices("jp", period="1mo"))
        sl.update_indices()
    except Exception:
        sl.log("データ更新エラー:\n" + traceback.format_exc())

    reg = sl.jp_regime()
    state = sl.load_state("state_jp.json", {"last_regime_ok": None, "last_date": "", "watch": []})
    watch = state.get("watch", [])
    picks, watch_report, still_watch, data_date = sl.jp_daily(cfg, watch)

    if state.get("last_date") == data_date:
        sl.log(f"新しい取引日データなし（{data_date}）→ 通知スキップ（休場日）")
        if not updated:
            # 価格取得に失敗して古いデータのままなら、その旨だけ知らせる（1日1回）
            sl.line_broadcast(
                f"⚠️ 株シグナル: 本日の価格データを取得できませんでした（最新 {data_date}）。\n"
                "次回の定時実行で自動再試行します。", cfg)
        return

    d = data_date[5:].replace("-", "/")
    lines = [f"📊 株シグナル 日本株 {d}分"]

    if state.get("last_regime_ok") is not None and state["last_regime_ok"] != reg["ok"]:
        lines.append("⚠️ " + ("🟢 地合いが好転しました（新規買い再開）" if reg["ok"]
                              else "🔴 地合いが悪化に転じました（新規買い停止）"))

    if reg["ok"]:
        lines.append(f"地合い: 🟢良好（日経 {reg['n225']:,.0f} > 200日線 {reg['sma200']:,.0f}）")
        if picks:
            lines.append("")
            lines.append("▼ 新規買い候補（翌営業日 寄付成行）")
            for i, p in enumerate(picks, 1):
                unit = "現物" if p["shares"] >= 100 else "かぶミニ(1株単位)"
                lines.append(f"{i}. {p['code']} {p['name']} {STAR[p['score']]}")
                lines.append(f"   {p['close']:,.0f}円 × {p['shares']}株 ≒ {p['amount']/10000:.1f}万円（{unit}）")
                lines.append(f"   損切り: {p['stop']:,.0f}円（逆指値 -10%）")
                lines.append(f"   手仕舞い: デッドクロス通知が来るまで保有（最長60営業日・平均40日）")
            new_watch = still_watch + [
                {"ticker": p["ticker"], "code": p["code"], "name": p["name"],
                 "added": data_date, "entry_close": p["close"], "stop": p["stop"]}
                for p in picks]
        else:
            lines.append("本日の新規候補: なし（基準を満たす銘柄なし）")
            new_watch = still_watch
    else:
        reason = "日経平均が200日線割れ" if not reg["bull"] else "指数が急落中（5日で-3%超）"
        lines.append(f"地合い: 🔴悪化（{reason}）")
        lines.append("→ 新規買いは停止。以下の保有監視のみ継続します。")
        new_watch = still_watch

    if watch_report:
        lines.append("")
        lines.append(f"▼ 監視中銘柄（{len(watch_report)}）")
        for w in watch_report:
            lines.append(f"・{w['code']} {w['name']} {w['chg']:+.1%}（{w['held']}日目）")
            lines.append(f"   {w['action']}")

    if picks and reg["ok"]:
        lines.append("")
        lines.append("[根拠] 過去8年検証: ★★★=勝率47%・平均+1.8%/回")
        lines.append("★★☆=勝率42%・平均+1.8%/回・勝ち平均+16%/負け平均-8%")
        lines.append("勝率4割でも利大損小で勝つ型。損切りだけは機械的に。")

    sl.save_state("state_jp.json", {"last_regime_ok": reg["ok"], "last_date": data_date,
                                    "watch": new_watch[-20:],
                                    "regime": reg, "last_picks": picks if reg["ok"] else [],
                                    "last_watch_report": watch_report})
    try:
        import dashboard
        dashboard.refresh(cfg)
    except Exception:
        sl.log("ダッシュボード生成エラー:\n" + traceback.format_exc())
    url = sl.dashboard_url(cfg)
    if url:
        lines.append("")
        lines.append(f"📱 詳細ダッシュボード: {url}")
    sl.line_broadcast("\n".join(lines), cfg)
    sl.log(f"=== 日次JPジョブ完了（候補{len(picks)} 監視{len(new_watch)}） ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sl.log("日次JPジョブ致命的エラー:\n" + traceback.format_exc())
        sys.exit(1)
