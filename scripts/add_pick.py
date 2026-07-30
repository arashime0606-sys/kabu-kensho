#!/usr/bin/env python3
"""
掲載記録を picks.json に追記する。

このスクリプトは追記しかしない。既存レコードの書き換え・削除は行わない。
手で picks.json を編集すると過去の記録を壊す事故が起きるので、
掲載はこのスクリプト経由に統一する。

使い方:
    python scripts/add_pick.py --code 7203 --name "トヨタ自動車" \
        --rule "PBR1.0倍以下 かつ 配当利回り3.5%以上"

    # 掲載日を指定する場合（既定は今日）
    python scripts/add_pick.py --code 7203 --name "…" --rule "…" --date 2026-07-30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PICKS = ROOT / "data" / "picks.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="銘柄コード（4桁）")
    ap.add_argument("--name", required=True, help="銘柄名")
    ap.add_argument("--rule", required=True, help="どの条件で抽出したか")
    ap.add_argument("--date", default=None, help="掲載日 YYYY-MM-DD（既定は今日）")
    ap.add_argument("--note", default="", help="補足")
    args = ap.parse_args()

    posted = (
        dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    )
    if posted > dt.date.today():
        raise SystemExit("未来の日付では掲載できません")

    code = str(args.code).strip()
    if not (code.isdigit() and len(code) in (4, 5)):
        raise SystemExit(f"銘柄コードが不正です: {code!r}")

    picks = json.loads(PICKS.read_text(encoding="utf-8")) if PICKS.exists() else []
    before = len(picks)

    # 同日・同銘柄の重複を防ぐ
    base = f"{posted:%Y-%m-%d}-{code}"
    existing = {p["id"] for p in picks}
    if base in existing:
        raise SystemExit(f"同じ掲載日・銘柄の記録がすでにあります: {base}")

    record = {
        "id": base,
        "code": code,
        "name": args.name,
        "posted_at": posted.isoformat(),
        "screening_rule": args.rule,
        "note": args.note,
    }
    picks.append(record)

    # 追記であることを機械的に確認してから書き出す
    assert len(picks) == before + 1, "追記以外の変更が発生しました"

    PICKS.write_text(
        json.dumps(picks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"追記しました: {record['id']} {record['name']}")
    print(f"掲載総数: {len(picks)} 件")
    print()
    print("次のコマンドでコミットしてください（履歴がタイムスタンプの証拠になります）:")
    print(f'  git add data/picks.json && git commit -m "掲載: {record["id"]} {args.name}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
