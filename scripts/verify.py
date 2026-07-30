#!/usr/bin/env python3
"""
掲載銘柄の全件検証を実行し、results.json を生成する。

使い方:
    # 実データ（要 JQUANTS_API_KEY）
    python scripts/verify.py

    # オフラインの動作確認（APIに繋がない）
    python scripts/verify.py --mock

終了コード:
    0 : 全件が ok / pending / delisted のいずれかに確定した
    1 : 人の確認が必要な件（data_missing / error）が残っている
        → GitHub Actions を赤にして、確認を強制する
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import (  # noqa: E402
    NEEDS_ATTENTION,
    Pick,
    Verifier,
    summarize,
)

ROOT = Path(__file__).resolve().parent.parent
PICKS = ROOT / "data" / "picks.json"
RESOLUTIONS = ROOT / "data" / "resolutions.json"
RESULTS = ROOT / "data" / "results.json"

# ベンチマーク: TOPIX連動型上場投信(1306)。
# 指数そのものより、無料プランで確実に日足が取れるETFを既定にしている。
DEFAULT_BENCHMARK = "1306"


def load_picks() -> list[Pick]:
    raw = json.loads(PICKS.read_text(encoding="utf-8"))
    picks = [Pick.from_dict(r) for r in raw]
    ids = [p.id for p in picks]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SystemExit(f"picks.json に重複IDがあります: {sorted(dup)}")
    return picks


def load_resolutions() -> dict:
    if not RESOLUTIONS.exists():
        return {}
    raw = json.loads(RESOLUTIONS.read_text(encoding="utf-8"))
    out = {}
    for r in raw:
        out[(r["id"], r.get("horizon", "*"))] = r
    return out


# ------------------------------------------------------------ モック

def build_mock_fetcher(seed: int = 42):
    """APIに繋がずに動作確認するための擬似価格生成器。

    銘柄コードをシードにしてランダムウォークを作る。
    末尾が '9' の銘柄は途中でデータが途絶える（上場廃止を模す）。
    """
    def fetch(code, frm, to):
        rng = random.Random(seed + int(str(code)[:4]))
        series = {}
        price = rng.uniform(300, 3000)
        d = frm
        n = 0
        # ベンチマークは緩やかな上昇、個別株はボラを大きく
        drift = 0.0002 if str(code).startswith("1306") else 0.0004
        vol = 0.004 if str(code).startswith("1306") else 0.018
        while d <= to:
            if d.weekday() < 5:
                price *= (1 + rng.gauss(drift, vol))
                series[d] = round(price, 1)
                n += 1
                if str(code)[3] == "9" and n > 75:
                    break  # 上場廃止を模す
            d += dt.timedelta(days=1)
        return series

    return fetch


# ------------------------------------------------------------ 出力

def render_markdown(summary: dict, results: list) -> str:
    lines = [
        "# 掲載銘柄 全件検証レポート",
        "",
        f"生成日時: {dt.datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"**掲載総数 {summary['掲載総数']} 件 / 要確認 {summary['要確認件数']} 件**",
        "",
        "## 判定時点別の成績",
        "",
        "| 判定時点 | 掲載総数 | 評価済 | 上昇勝率 | 超過勝率 | 平均リターン | 平均超過リターン |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, h in summary["判定時点別"].items():
        def f(v, suffix="%"):
            return "—" if v is None else f"{v}{suffix}"
        lines.append(
            f"| {name} | {h['掲載総数']} | {h['評価済']} | {f(h['勝率_上昇'])} | "
            f"{f(h['勝率_ベンチマーク超過'])} | {f(h['平均リターン_pct'])} | "
            f"{f(h['平均超過リターン_pct'])} |"
        )

    lines += ["", "## ステータス内訳", ""]
    for name, h in summary["判定時点別"].items():
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(h["ステータス内訳"].items()))
        lines.append(f"- **{name}** — {parts}")

    if summary["要確認一覧"]:
        lines += ["", "## 要確認（人の確認が必要）", ""]
        for a in summary["要確認一覧"]:
            lines.append(f"- `{a['id']}` ({a['code']}) {a['horizon']}: {a['detail']}")

    lines += [
        "",
        "---",
        "",
        "掲載した銘柄は、結果の良し悪しにかかわらず全件をこの表に載せています。",
        "掲載記録は追記のみで、削除・書き換えを行いません（Gitのコミット履歴で検証できます）。",
        "上場廃止・データ欠損も件数に含めています。",
        "",
        "本レポートは過去の記録であり、将来の運用成果を示すものではありません。",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="APIに繋がず擬似データで動かす")
    ap.add_argument("--benchmark", default=DEFAULT_BENCHMARK)
    ap.add_argument("--latest", default=None,
                    help="データ最終日 YYYY-MM-DD。未指定なら自動判定")
    args = ap.parse_args()

    picks = load_picks()
    resolutions = load_resolutions()
    print(f"掲載記録: {len(picks)} 件を読み込みました")

    if args.mock:
        fetch = build_mock_fetcher()
        latest = dt.date.fromisoformat(args.latest) if args.latest else dt.date.today()
        print("モードは MOCK です（実データではありません）")
    else:
        from jquants_client import JQuantsClient

        client = JQuantsClient()
        fetch = client.adjusted_close_series
        if args.latest:
            latest = dt.date.fromisoformat(args.latest)
        else:
            # 無料プランは約12週遅延。ベンチマークの実データ最終日を採用する。
            probe = fetch(args.benchmark,
                          dt.date.today() - dt.timedelta(days=400),
                          dt.date.today())
            if not probe:
                raise SystemExit(
                    f"ベンチマーク({args.benchmark})の価格が0件でした。\n"
                    "考えられる原因:\n"
                    "  - J-Quantsでプラン(Free)の選択が完了していない\n"
                    "  - このコードが現在のプランで取得できない\n"
                    "  - 指定期間が取得可能範囲外（Freeプランは約2年・12週遅延）"
                )
            latest = max(probe)
        print(f"データ最終日: {latest}")

    verifier = Verifier(fetch, args.benchmark, latest, resolutions)
    results = verifier.verify_all(picks)
    summary = summarize(results)

    RESULTS.write_text(
        json.dumps(
            {
                "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "benchmark": args.benchmark,
                "latest_data_date": latest.isoformat(),
                "summary": summary,
                "results": [asdict(r) for r in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (ROOT / "REPORT.md").write_text(
        render_markdown(summary, results), encoding="utf-8"
    )

    # ----- 標準出力サマリ -----
    print()
    for name, h in summary["判定時点別"].items():
        print(
            f"  {name:>4}  掲載{h['掲載総数']:>3}件 / 評価{h['評価済']:>3}件 "
            f" 上昇勝率 {h['勝率_上昇']}%  超過勝率 {h['勝率_ベンチマーク超過']}%"
            f"  平均超過 {h['平均超過リターン_pct']}%"
        )

    attention = summary["要確認件数"]
    print()
    if attention:
        print(f"要確認が {attention} 件あります。data/resolutions.json に")
        print("確認結果を追記してから再実行してください。")
        for a in summary["要確認一覧"][:10]:
            print(f"  - {a['id']} ({a['code']}) {a['horizon']}: {a['detail']}")
        return 1

    print("全件が確定しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
