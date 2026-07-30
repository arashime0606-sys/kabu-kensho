"""J-Quants API V2 クライアント"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path

import requests

BASE = "https://api.jquants.com/v2"
DAILY_BARS = "/equities/bars/daily"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

ADJ_CLOSE_KEYS = ("AdjC", "AdjustmentClose", "AdjClose")
DATE_KEYS = ("Date", "D", "date")


class JQuantsError(RuntimeError):
    pass


def normalize_code(code: str) -> str:
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
            raise JQuantsError("環境変数 JQUANTS_API_KEY が設定されていません。")
        self._use_cache = use_cache
        self._window: tuple[dt.date, dt.date] | None = None
        if use_cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def window(self) -> tuple[dt.date, dt.date] | None:
        return self._window

    @staticmethod
    def parse_window(message: str) -> tuple[dt.date, dt.date] | None:
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[~〜]\s*(\d{4}-\d{2}-\d{2})", message)
        if not m:
            return None
        return dt.date.fromisoformat(m.group(1)), dt.date.fromisoformat(m.group(2))

    def _clamp(self, params: dict) -> dict | None:
        if not self._window:
            return params
        lo, hi = self._window
        frm = dt.datetime.strptime(params["from"], "%Y%m%d").date()
        to = dt.datetime.strptime(params["to"], "%Y%m%d").date()
        frm = max(frm, lo)
        to = min(to, hi)
        if frm > to:
            return None
        out = dict(params)
        out["from"] = f"{frm:%Y%m%d}"
        out["to"] = f"{to:%Y%m%d}"
        return out

    def _get(self, path: str, params: dict) -> list[dict]:
        headers = {"x-api-key": self._api_key}
        rows: list[dict] = []
        pagination_key = None
        retried_for_window = False

        if "from" in params and "to" in params:
            clamped = self._clamp(params)
            if clamped is None:
                return []
            params = clamped

        for _ in range(200):
            p = dict(params)
            if pagination_key:
                p["pagination_key"] = pagination_key

            r = self._session.get(f"{BASE}{path}", params=p, headers=headers, timeout=60)

            if r.status_code == 429:
                time.sleep(10)
                continue

            if r.status_code == 400 and not retried_for_window:
                w = self.parse_window(r.text)
                if w and "from" in params and "to" in params:
                    self._window = w
                    retried_for_window = True
                    clamped = self._clamp(params)
                    if clamped is None:
                        return []
                    params = clamped
                    continue

            if r.status_code in (401, 403):
                raise JQuantsError(
                    f"認証に失敗しました (HTTP {r.status_code})。"
                    f"APIキーとプラン選択を確認してください。 応答: {r.text[:200]}"
                )
            if r.status_code != 200:
                raise JQuantsError(f"GET {path} 失敗 (HTTP {r.status_code}): {r.text[:300]}")

            body = r.json()
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
                    f"応答にレコードの配列が見つかりません。応答のキー: {list(body.keys())}"
                )

            rows.extend(chunk)
            pagination_key = body.get("pagination_key")
            if not pagination_key:
                break
            time.sleep(0.3)

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
            cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    def adjusted_close_series(
        self, code: str, frm: dt.date, to: dt.date
    ) -> dict[dt.date, float]:
        rows = self.daily_bars(code, frm, to)
        if not rows:
            return {}

        sample = rows[0]
        if _pick(sample, ADJ_CLOSE_KEYS) is None:
            raise JQuantsError(
                f"調整後終値の項目が見つかりません。実際の項目名: {sorted(sample.keys())}"
            )
        if _pick(sample, DATE_KEYS) is None:
            raise JQuantsError(
                f"日付の項目が見つかりません。実際の項目名: {sorted(sample.keys())}"
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