# -*- coding: utf-8 -*-
"""米国株ジョブ（毎日09:00に起動し、日付で役割を判定）
- 月初の最初の平日: 月次リバランス通知（予算内で組める推奨ポートフォリオ）
- 土曜日: 週次ヘルスチェック
"""
import datetime as dt
import sys
import traceback

import signal_lib as sl


def is_first_weekday_of_month(d: dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    first = d.replace(day=1)
    while first.weekday() >= 5:
        first += dt.timedelta(days=1)
    return d == first


def build_portfolio(cfg):
    """モメンタム上位から、予算内で買える推奨ポートフォリオを構成"""
    cfgus = dict(cfg.get("us", {}))
    top_n = int(cfgus.get("top_n", 20))
    cfgus["top_n"] = max(top_n, int(cfgus.get("buffer_rank", 60)))
    cand, ranks, data_date = sl.us_momentum_top({"us": cfgus})
    fx = sl.usdjpy()
    budget_usd = float(cfgus.get("total_budget_yen", 100000)) / fx
    n_names = min(top_n, max(5, int(budget_usd // 500)))
    per_cap = budget_usd / n_names
    port, skipped = [], []
    remaining = budget_usd
    for p in cand:
        if len(port) >= n_names:
            break
        sh = int(min(per_cap, remaining) // p["close"])
        if sh >= 1:
            cost = sh * p["close"]
            port.append({**p, "shares": sh, "cost": cost})
            remaining -= cost
        elif p["rank"] <= top_n:
            skipped.append(f"{p['ticker']}(${p['close']:,.0f})")
    return port, skipped, ranks, data_date, fx, budget_usd, n_names


def fmt_pct(x):
    return f"{x*100:+.0f}%"


def rebalance(cfg):
    sl.log("=== 月次USリバランス ===")
    sl.update_prices("us", period="2mo")
    sl.update_indices()
    port, skipped, ranks, data_date, fx, budget_usd, n_names = build_portfolio(cfg)
    state = sl.load_state("state_us.json", {"holdings": [], "last_rebalance": ""})
    cur = set(state.get("holdings", []))
    new = [p["ticker"] for p in port]
    sells = sorted(cur - set(new))
    buys = [p for p in port if p["ticker"] not in cur]
    d = data_date[5:].replace("-", "/")
    lines = [f"🇺🇸 株シグナル 月次リバランス（{d}時点）",
             f"予算: {fx*budget_usd/10000:,.0f}万円 ≒ ${budget_usd:,.0f}（{fx:.0f}円/$）"]
    if sells:
        lines.append("")
        lines.append("▼ 売り（全株・寄付成行）")
        for t in sells:
            r = ranks.get(t)
            lines.append(f"・{t}（現在 {'圏外' if r is None else str(r)+'位'}）")
    lines.append("")
    lines.append(f"▼ 今月の推奨ポートフォリオ（{len(port)}銘柄）")
    total = 0.0
    for p in port:
        mark = "🆕 " if p["ticker"] not in cur else ""
        lines.append(f"・{mark}{p['ticker']} {p['name']}")
        lines.append(f"   ${p['close']:,.2f} × {p['shares']}株 = ${p['cost']:,.0f}"
                     f"（勢い{p['rank']}位 / 12ヶ月{fmt_pct(p['mom'])}）")
        total += p["cost"]
    lines.append(f"合計 ${total:,.0f} / 待機 ${budget_usd-total:,.0f}")
    if skipped:
        lines.append("")
        lines.append(f"⚠️ 予算不足でスキップした上位銘柄: {', '.join(skipped[:6])}")
        lines.append("→ 検証の前提（上位20銘柄等金額）に近づけるには$5,000以上を推奨")
    lines.append("")
    lines.append("保有期間: 次回月初まで（途中の損切りは原則なし・月次入替で自動退出）")
    lines.append("土曜の週次チェックで勢い圏外に落ちた銘柄は早期売却を検討")
    lines.append("[根拠] 過去8年検証: 年率+24〜26%・最大下落-52%（上位20等金額）")
    sl.save_state("state_us.json", {"holdings": new,
                                    "last_rebalance": dt.date.today().isoformat(),
                                    "portfolio": port,
                                    "meta": {"budget_usd": budget_usd, "fx": fx,
                                             "skipped": skipped, "data_date": data_date}})
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


def health_check(cfg):
    sl.log("=== 週次USヘルスチェック ===")
    sl.update_prices("us", period="1mo")
    cfgus = dict(cfg.get("us", {}))
    cfgus["top_n"] = int(cfgus.get("buffer_rank", 60))
    _, ranks, data_date = sl.us_momentum_top({"us": cfgus})
    state = sl.load_state("state_us.json", {"holdings": []})
    holdings = state.get("holdings", [])
    if not holdings:
        sl.log("保有リストなし（初回リバランス前）→ 通知スキップ")
        return
    buffer_rank = int(cfg.get("us", {}).get("buffer_rank", 60))
    d = data_date[5:].replace("-", "/")
    lines = [f"🇺🇸 週次チェック（{d}時点）"]
    warn = []
    for t in holdings:
        r = ranks.get(t)
        if r is None or r > buffer_rank:
            warn.append((t, r))
    if warn:
        lines.append(f"⚠️ 勢い圏外（{buffer_rank}位以下）に落ちた保有銘柄:")
        for t, r in warn:
            lines.append(f"・{t}（現在 {'圏外' if r is None else str(r)+'位'}）→ 早期売却を検討")
        lines.append("それ以外は月初の入替まで保有継続でOKです。")
    else:
        ok_ranks = [f"{t}:{ranks.get(t,'-')}位" for t in holdings[:10]]
        lines.append(f"保有{len(holdings)}銘柄すべて勢い圏内。継続保有でOK。")
        lines.append("（" + " / ".join(ok_ranks) + "）")
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


def main():
    cfg = sl.load_config()
    if not cfg.get("us", {}).get("enabled", True):
        return
    today = dt.date.today()
    force = sys.argv[1] if len(sys.argv) > 1 else ""
    if force == "rebalance" or is_first_weekday_of_month(today):
        rebalance(cfg)
    elif force == "health" or today.weekday() == 5:
        health_check(cfg)
    else:
        sl.log(f"USジョブ: 本日({today})は実行対象日ではありません")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sl.log("USジョブ致命的エラー:\n" + traceback.format_exc())
        sys.exit(1)
