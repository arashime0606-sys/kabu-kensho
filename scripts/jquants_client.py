"""
J-Quants API V2 クライアント

V1は2026年6月1日に終了済み。V2のみ利用可能。

V1との違い（ここを間違えると動かない）:
  認証        : Authorization: Bearer  →  x-api-key ヘッダー
  エンドポイント: /v1/prices/daily_quotes  →  /v2/equities/bars/daily
  項目名      : AdjustmentClose  →  AdjC（短縮形）
  日付形式    : YYYY-MM-DD  →  YYYYMMDD（YYYY-MM-DDも可）

取得するのは調整後終値(AdjC)のみ。
株式分割・併合はここで吸収されるので、検証側では素の終値(C)を一切使わない。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

import requests

BASE = "https://api.jquants.com/v2"
DAILY_BARS = "/equities/bars/daily"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

# 調整後終値の項目名。仕様変更に備えて候補を順に探す。
ADJ_CLOSE_KEYS = ("AdjC", "AdjustmentClose", "AdjClose")
DATE_KEYS = ("Date", "D", "date")


class JQuantsError(RuntimeError):
    pass


def normalize_code(code: str) -> str:
    """J-Quants の銘柄コードは5桁。4桁で渡された場合は末尾に0を付ける。

    >>> normalize_code("7203")
    '72030'
    >>> normalize_code("72030")
    '72030'
    """
    code = str(code).strip()
    if len(code) == 4:
        return code + "0"
    if len(code) == 5:
        return code
    raise ValueError(f"銘柄コードの桁数が不正です: {code!r}")


def _pick(row: dict, candidates: tuple[str, ...]):
    for k in candidates:
        if k in row:
            return row[k]
    return None


class JQuantsClient:
    def __init__(self, use_cache: bool = True):
        self._session = requests.Session()
        self._api_key = os.environ.get("JQUANTS_API_KEY")
        if not self._api_key:
            raise JQuantsError(
                "環境変数 JQUANTS_API_KEY が設定されていません。"
                "J-Quantsのダッシュボードで発行したAPIキーを設定してください。"
            )
        self._use_cache = use_cache
        if use_cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- 取得 ----------

    def _get(self, path: str, params: dict) -> list[dict]:
        """ページネーション込みで取得し、レコードのリストを返す。"""
        headers = {"x-api-key": self._api_key}
        rows: list[dict] = []
        pagination_key = None

        for _ in range(200):  # 無限ループ防止
            p = dict(params)
            if pagination_key:
                p["pagination_key"] = pagination_key

            r = self._session.get(
                f"{BASE}{path}", params=p, headers=headers, timeout=60
            )

            if r.status_code == 429:  # レートリミット
                time.sleep(10)
                continue
            if r.status_code in (401, 403):
                raise JQuantsError(
                    f"認証に失敗しました (HTTP {r.status_code})。"
                    f"APIキーが正しいか、プラン選択が完了しているか確認してください。"
                    f" 応答: {r.text[:200]}"
                )
            if r.status_code != 200:
                raise JQuantsError(
                    f"GET {path} 失敗 (HTTP {r.status_code}): {r.text[:300]}"
                )

            body = r.json()
            # V2は "data" キー。念のため他の候補も探す。
            chunk = None
            if isinstance(body.get("data"), list):
                chunk = body["data"]
            else:
                for k, v in body.items():
                    if k != "pagination_key" and isinstance(v, list):
                        chunk = v
                        break
            if chunk is None:
                raise JQuantsError(
                    f"応答にレコードの配列が見つかりません。"
                    f"応答のキー: {list(body.keys())} / 抜粋: {str(body)[:200]}"
                )

            rows.extend(chunk)
            pagination_key = body.get("pagination_key")
            if not pagination_key:
                break
            time.sleep(0.3)  # レートリミット対策

        return rows

    def daily_bars(self, code: str, frm: dt.date, to: dt.date) -> list[dict]:
        code5 = normalize_code(code)
        cache_path = CACHE_DIR / f"v2_{code5}_{frm:%Y%m%d}_{to:%Y%m%d}.json"
        if self._use_cache and cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))

        rows = self._get(
            DAILY_BARS,
            {"code": code5, "from": f"{frm:%Y%m%d}", "to": f"{to:%Y%m%d}"},
        )
        if self._use_cache:
            cache_path.write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8"
            )
        return rows

    # ---------- 検証側が使う形に変換 ----------

    def adjusted_close_series(
        self, code: str, frm: dt.date, to: dt.date
    ) -> dict[dt.date, float]:
        """{日付: 調整後終値} を返す。値が None の日は除外する。"""
        rows = self.daily_bars(code, frm, to)
        if not rows:
            return {}

        # 項目名が想定と違う場合、何が返ってきたかを明示して落とす。
        sample = rows[0]
        if _pick(sample, ADJ_CLOSE_KEYS) is None:
            raise JQuantsError(
                f"調整後終値の項目が見つかりません（探した名前: {ADJ_CLOSE_KEYS}）。"
                f"実際に返ってきた項目名: {sorted(sample.keys())}"
            )
        if _pick(sample, DATE_KEYS) is None:
            raise JQuantsError(
                f"日付の項目が見つかりません（探した名前: {DATE_KEYS}）。"
                f"実際に返ってきた項目名: {sorted(sample.keys())}"
            )

        out: dict[dt.date, float] = {}
        for row in rows:
            close = _pick(row, ADJ_CLOSE_KEYS)
            raw_date = _pick(row, DATE_KEYS)
            if close is None or raw_date is None:
                continue
            s = str(raw_date)
            d = (
                dt.date.fromisoformat(s)
                if "-" in s
                else dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            )
            out[d] = float(close)
        return out
