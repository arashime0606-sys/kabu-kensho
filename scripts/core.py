"""
検証の中核ロジック。

設計上の約束（ここを崩すと記録の価値が消える）:
  1. picks.json の全レコードを必ず走査する。除外リストもスキップ機構も持たない。
  2. 判定できなかったものは「除外」ではなく status として結果に残す。
  3. 価格は必ず調整後終値を使う（分割・併合の誤判定を防ぐ）。
  4. 判定時点は掲載日から機械的に決まる。事後に動かせない。
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable

# 判定時点（掲載日からの月数）。ここを事後に変えないこと。
HORIZONS: dict[str, int] = {"1m": 1, "3m": 3, "6m": 6, "12m": 12}

# 目標日が非営業日のとき、何日後まで探しにいくか
TRADING_DAY_TOLERANCE = 10


# ---------- ステータス ----------
# ok            : 判定できた
# pending       : 判定時点がまだ来ていない（正常。無料プランの12週遅延もここ）
# delisted      : 上場廃止等で判定不能と人が確認済み（resolutions.json による）
# data_missing  : 価格が取れなかった。人の確認が必要 → ジョブを失敗させる
# error         : 想定外の例外。人の確認が必要 → ジョブを失敗させる

NEEDS_ATTENTION = {"data_missing", "error"}


def add_months(d: dt.date, months: int) -> dt.date:
    """月末を超えないように月を加算する。1/31 + 1ヶ月 = 2/28(29)。"""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def price_on_or_after(
    series: dict[dt.date, float], target: dt.date, tolerance: int = TRADING_DAY_TOLERANCE
) -> tuple[dt.date, float] | None:
    """目標日以降で最初に値がある営業日の (日付, 調整後終値) を返す。"""
    for offset in range(tolerance + 1):
        d = target + dt.timedelta(days=offset)
        if d in series:
            return d, series[d]
    return None


@dataclass
class Pick:
    """掲載記録。picks.json は追記のみで、一度書いたら書き換えない。"""

    id: str
    code: str
    name: str
    posted_at: dt.date
    screening_rule: str
    note: str = ""

    @staticmethod
    def from_dict(raw: dict) -> "Pick":
        return Pick(
            id=raw["id"],
            code=str(raw["code"]),
            name=raw.get("name", ""),
            posted_at=dt.date.fromisoformat(raw["posted_at"]),
            screening_rule=raw.get("screening_rule", ""),
            note=raw.get("note", ""),
        )


@dataclass
class HorizonResult:
    horizon: str
    target_date: str | None = None
    eval_date: str | None = None
    status: str = "pending"
    entry_price: float | None = None
    exit_price: float | None = None
    return_pct: float | None = None
    benchmark_return_pct: float | None = None
    excess_return_pct: float | None = None
    detail: str = ""


@dataclass
class PickResult:
    id: str
    code: str
    name: str
    posted_at: str
    screening_rule: str
    entry_date: str | None = None
    entry_price: float | None = None
    status: str = "ok"
    detail: str = ""
    horizons: list[HorizonResult] = field(default_factory=list)


# 価格取得関数の型: (銘柄コード, 開始日, 終了日) -> {日付: 調整後終値}
PriceFetcher = Callable[[str, dt.date, dt.date], dict[dt.date, float]]


class Verifier:
    def __init__(
        self,
        fetch_prices: PriceFetcher,
        benchmark_code: str,
        latest_data_date: dt.date,
        resolutions: dict[tuple[str, str], dict] | None = None,
    ):
        self._fetch = fetch_prices
        self._benchmark_code = benchmark_code
        self._latest = latest_data_date
        self._resolutions = resolutions or {}
        self._bench_cache: dict[tuple[dt.date, dt.date], dict[dt.date, float]] = {}

    def _benchmark_series(self, frm: dt.date, to: dt.date) -> dict[dt.date, float]:
        key = (frm, to)
        if key not in self._bench_cache:
            self._bench_cache[key] = self._fetch(self._benchmark_code, frm, to)
        return self._bench_cache[key]

    def _resolution_for(self, pick_id: str, horizon: str) -> dict | None:
        """個別の判定時点、または全時点('*')に対する人の確認結果を引く。"""
        return self._resolutions.get((pick_id, horizon)) or self._resolutions.get(
            (pick_id, "*")
        )

    def verify_one(self, pick: Pick) -> PickResult:
        res = PickResult(
            id=pick.id,
            code=pick.code,
            name=pick.name,
            posted_at=pick.posted_at.isoformat(),
            screening_rule=pick.screening_rule,
        )

        # 取得範囲は掲載日の少し前から、最終判定時点の余裕分まで
        frm = pick.posted_at - dt.timedelta(days=10)
        to = add_months(pick.posted_at, max(HORIZONS.values())) + dt.timedelta(days=30)

        try:
            series = self._fetch(pick.code, frm, to)
            bench = self._benchmark_series(frm, to)
        except Exception as exc:  # 取得失敗も「結果」として残す。握り潰さない。
            res.status = "error"
            res.detail = f"価格取得に失敗: {type(exc).__name__}: {exc}"
            res.horizons = [
                HorizonResult(horizon=h, status="error", detail=res.detail)
                for h in HORIZONS
            ]
            return self._apply_resolutions(pick, res)

        entry = price_on_or_after(series, pick.posted_at)
        bench_entry = price_on_or_after(bench, pick.posted_at)

        if entry is None or bench_entry is None:
            which = "銘柄" if entry is None else "ベンチマーク"
            res.status = "data_missing"
            res.detail = f"掲載日({pick.posted_at})付近の{which}価格が取得できません"
            res.horizons = [
                HorizonResult(horizon=h, status="data_missing", detail=res.detail)
                for h in HORIZONS
            ]
            return self._apply_resolutions(pick, res)

        entry_date, entry_price = entry
        _, bench_entry_price = bench_entry
        res.entry_date = entry_date.isoformat()
        res.entry_price = entry_price

        for name, months in HORIZONS.items():
            res.horizons.append(
                self._verify_horizon(
                    name, months, pick, series, bench, entry_price, bench_entry_price
                )
            )

        return self._apply_resolutions(pick, res)

    def _verify_horizon(
        self,
        name: str,
        months: int,
        pick: Pick,
        series: dict[dt.date, float],
        bench: dict[dt.date, float],
        entry_price: float,
        bench_entry_price: float,
    ) -> HorizonResult:
        target = add_months(pick.posted_at, months)
        hr = HorizonResult(horizon=name, target_date=target.isoformat())
        hr.entry_price = entry_price

        # 無料プランの12週遅延により、直近はここに落ちる。これは正常。
        if target > self._latest:
            hr.status = "pending"
            hr.detail = f"判定時点が未到来（データ最終日 {self._latest}）"
            return hr

        exit_ = price_on_or_after(series, target)
        bench_exit = price_on_or_after(bench, target)

        if exit_ is None or bench_exit is None:
            which = "銘柄" if exit_ is None else "ベンチマーク"
            hr.status = "data_missing"
            hr.detail = (
                f"{target} 以降 {TRADING_DAY_TOLERANCE} 日以内に"
                f"{which}の調整後終値が存在しません（上場廃止の可能性）"
            )
            return hr

        exit_date, exit_price = exit_
        _, bench_exit_price = bench_exit

        hr.eval_date = exit_date.isoformat()
        hr.exit_price = exit_price
        hr.return_pct = round((exit_price / entry_price - 1) * 100, 2)
        hr.benchmark_return_pct = round(
            (bench_exit_price / bench_entry_price - 1) * 100, 2
        )
        hr.excess_return_pct = round(hr.return_pct - hr.benchmark_return_pct, 2)
        hr.status = "ok"
        return hr

    def _apply_resolutions(self, pick: Pick, res: PickResult) -> PickResult:
        """人が確認済みの案件だけ、data_missing/error を確定ステータスに置き換える。

        置き換えは resolutions.json に記録が残っている場合に限る。
        つまり「黙ってスキップする」経路は存在しない。
        """
        for hr in res.horizons:
            if hr.status not in NEEDS_ATTENTION:
                continue
            r = self._resolution_for(pick.id, hr.horizon)
            if r:
                hr.status = r["status"]
                hr.detail = f"{hr.detail} / 確認済({r['resolved_at']}): {r.get('note', '')}"
        if res.status in NEEDS_ATTENTION:
            r = self._resolution_for(pick.id, "*")
            if r:
                res.status = r["status"]
                res.detail = f"{res.detail} / 確認済({r['resolved_at']}): {r.get('note', '')}"
        return res

    def verify_all(self, picks: Iterable[Pick]) -> list[PickResult]:
        return [self.verify_one(p) for p in picks]


def summarize(results: list[PickResult]) -> dict:
    """判定時点ごとに勝率・平均リターン・平均超過リターンを集計する。

    分母(掲載総数)は必ず出す。評価できた件数だけを見せない。
    """
    total = len(results)
    by_horizon: dict[str, dict] = {}

    for name in HORIZONS:
        rows = [h for r in results for h in r.horizons if h.horizon == name]
        ok = [h for h in rows if h.status == "ok"]
        status_counts: dict[str, int] = {}
        for h in rows:
            status_counts[h.status] = status_counts.get(h.status, 0) + 1

        def _mean(vals: list[float]) -> float | None:
            return round(sum(vals) / len(vals), 2) if vals else None

        rets = [h.return_pct for h in ok if h.return_pct is not None]
        exs = [h.excess_return_pct for h in ok if h.excess_return_pct is not None]

        by_horizon[name] = {
            "掲載総数": total,
            "評価済": len(ok),
            "ステータス内訳": status_counts,
            "勝率_上昇": _rate([r > 0 for r in rets]),
            "勝率_ベンチマーク超過": _rate([e > 0 for e in exs]),
            "平均リターン_pct": _mean(rets),
            "中央値リターン_pct": _median(rets),
            "平均超過リターン_pct": _mean(exs),
            "中央値超過リターン_pct": _median(exs),
        }

    attention = [
        {"id": r.id, "code": r.code, "horizon": h.horizon, "detail": h.detail}
        for r in results
        for h in r.horizons
        if h.status in NEEDS_ATTENTION
    ]

    return {
        "掲載総数": total,
        "要確認件数": len(attention),
        "要確認一覧": attention,
        "判定時点別": by_horizon,
    }


def _rate(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags) * 100, 1) if flags else None


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return round(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2, 2)


def result_to_dict(r: PickResult) -> dict:
    d = asdict(r)
    return d
