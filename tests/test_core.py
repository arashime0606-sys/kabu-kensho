"""
合成データによるロジック検証。実APIには一切繋がない。

ここで検査するのは主に「全件性が壊れる経路」:
  - 上場廃止で価格が消えた銘柄が、黙って消えないか
  - 判定時点が未到来のものが、誤って評価されないか
  - 人の確認なしに data_missing が握り潰されないか
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core import (  # noqa: E402
    HORIZONS,
    Pick,
    Verifier,
    add_months,
    price_on_or_after,
    summarize,
)

D = dt.date


def make_series(start: D, days: int, price_fn) -> dict:
    """土日を除いた連続営業日の系列を作る（祝日は無視。テスト目的には十分）。"""
    out = {}
    d = start
    i = 0
    while i < days:
        if d.weekday() < 5:
            out[d] = float(price_fn(i))
            i += 1
        d += dt.timedelta(days=1)
    return out


def flat(v):
    return lambda i: v


# ---------------------------------------------------------------- add_months

def test_add_months_month_end():
    assert add_months(D(2025, 1, 31), 1) == D(2025, 2, 28)
    assert add_months(D(2024, 1, 31), 1) == D(2024, 2, 29)  # 閏年
    assert add_months(D(2025, 3, 15), 12) == D(2026, 3, 15)
    assert add_months(D(2025, 12, 31), 3) == D(2026, 3, 31)
    print("OK: add_months 月末繰り上がり")


# --------------------------------------------------- price_on_or_after

def test_price_on_or_after_skips_holiday():
    series = {D(2025, 5, 7): 100.0}  # 5/3-5/6 が休みという想定
    got = price_on_or_after(series, D(2025, 5, 3))
    assert got == (D(2025, 5, 7), 100.0), got
    # 許容日数を超えると None
    assert price_on_or_after(series, D(2025, 4, 1)) is None
    print("OK: 非営業日は翌営業日に送られ、離れすぎるとNone")


# ------------------------------------------------------------ 基本の判定

def test_basic_return_and_excess():
    posted = D(2025, 1, 6)
    # 銘柄: 1000円スタート、以後ずっと1200円（+20%）
    stock = make_series(posted - dt.timedelta(days=10), 400,
                        lambda i: 1000 if i < 8 else 1200)
    # ベンチマーク: 2000円スタート、以後2200円（+10%）
    bench = make_series(posted - dt.timedelta(days=10), 400,
                        lambda i: 2000 if i < 8 else 2200)

    def fetch(code, frm, to):
        return stock if code == "7203" else bench

    v = Verifier(fetch, benchmark_code="1306", latest_data_date=D(2026, 6, 30))
    r = v.verify_one(Pick("p1", "7203", "テスト社", posted, "PBR1.0以下"))

    assert r.status == "ok"
    h12 = [h for h in r.horizons if h.horizon == "12m"][0]
    assert h12.status == "ok", h12
    assert h12.return_pct == 20.0, h12.return_pct
    assert h12.benchmark_return_pct == 10.0, h12.benchmark_return_pct
    assert h12.excess_return_pct == 10.0, h12.excess_return_pct
    print(f"OK: リターン{h12.return_pct}% / ベンチ{h12.benchmark_return_pct}% "
          f"/ 超過{h12.excess_return_pct}%")


def test_down_market_excess_positive():
    """下落相場で、銘柄も下げたがベンチより下げ幅が小さいケース。
    素の騰落だけ見ると『負け』だが、超過リターンはプラスになる。"""
    posted = D(2025, 1, 6)
    stock = make_series(posted - dt.timedelta(days=10), 400,
                        lambda i: 1000 if i < 8 else 950)   # -5%
    bench = make_series(posted - dt.timedelta(days=10), 400,
                        lambda i: 2000 if i < 8 else 1800)  # -10%

    def fetch(code, frm, to):
        return stock if code == "9999" else bench

    v = Verifier(fetch, "1306", latest_data_date=D(2026, 6, 30))
    r = v.verify_one(Pick("p2", "9999", "下落社", posted, "高配当"))
    h = [x for x in r.horizons if x.horizon == "12m"][0]
    assert h.return_pct == -5.0
    assert h.excess_return_pct == 5.0, h.excess_return_pct
    print(f"OK: 下落相場 リターン{h.return_pct}% だが超過{h.excess_return_pct}%")


# ------------------------------------------------- pending（12週遅延）

def test_pending_when_data_not_yet_available():
    posted = D(2026, 5, 1)
    series = make_series(posted - dt.timedelta(days=10), 60, flat(1000))

    def fetch(code, frm, to):
        return series

    # データ最終日 = 掲載の約2ヶ月後（無料プランの12週遅延を模す）
    v = Verifier(fetch, "1306", latest_data_date=D(2026, 6, 20))
    r = v.verify_one(Pick("p3", "1234", "新規社", posted, "低PER"))

    statuses = {h.horizon: h.status for h in r.horizons}
    assert statuses["1m"] == "ok", statuses
    assert statuses["3m"] == "pending", statuses
    assert statuses["6m"] == "pending", statuses
    assert statuses["12m"] == "pending", statuses
    print(f"OK: 判定時点未到来は pending -> {statuses}")


# ----------------------------------------- 上場廃止（握り潰されないこと）

def test_delisted_becomes_data_missing_not_skipped():
    posted = D(2025, 1, 6)
    # 3ヶ月目までは値があるが、その後データが途絶える（上場廃止想定）
    stock = make_series(posted - dt.timedelta(days=10), 70, flat(1000))
    bench = make_series(posted - dt.timedelta(days=10), 400, flat(2000))

    def fetch(code, frm, to):
        return stock if code == "8888" else bench

    v = Verifier(fetch, "1306", latest_data_date=D(2026, 6, 30))
    r = v.verify_one(Pick("p4", "8888", "廃止社", posted, "割安"))

    statuses = {h.horizon: h.status for h in r.horizons}
    assert statuses["1m"] == "ok", statuses
    assert statuses["6m"] == "data_missing", statuses
    assert statuses["12m"] == "data_missing", statuses
    # 重要: レコード自体は消えていない
    assert len(r.horizons) == len(HORIZONS)
    print(f"OK: 上場廃止銘柄は消えず data_missing として残る -> {statuses}")


def test_fetch_exception_becomes_error_not_silent():
    posted = D(2025, 1, 6)

    def fetch(code, frm, to):
        if code == "7777":
            raise ConnectionError("接続断")
        return make_series(posted - dt.timedelta(days=10), 400, flat(2000))

    v = Verifier(fetch, "1306", latest_data_date=D(2026, 6, 30))
    r = v.verify_one(Pick("p5", "7777", "取得失敗社", posted, "テスト"))

    assert r.status == "error"
    assert all(h.status == "error" for h in r.horizons)
    assert len(r.horizons) == len(HORIZONS)
    print("OK: 取得例外は握り潰されず error として全時点に残る")


# --------------------------------------- resolutions（人の確認がある時だけ）

def test_resolution_required_to_clear_data_missing():
    posted = D(2025, 1, 6)
    stock = make_series(posted - dt.timedelta(days=10), 70, flat(1000))
    bench = make_series(posted - dt.timedelta(days=10), 400, flat(2000))

    def fetch(code, frm, to):
        return stock if code == "8888" else bench

    resolutions = {
        ("p4", "*"): {
            "status": "delisted",
            "resolved_at": "2026-07-30",
            "note": "2025-04-30 TOB成立により上場廃止",
        }
    }
    v = Verifier(fetch, "1306", D(2026, 6, 30), resolutions=resolutions)
    r = v.verify_one(Pick("p4", "8888", "廃止社", posted, "割安"))

    statuses = {h.horizon: h.status for h in r.horizons}
    assert statuses["12m"] == "delisted", statuses
    # 確認の記録が detail に残る
    assert "TOB成立" in [h for h in r.horizons if h.horizon == "12m"][0].detail
    print(f"OK: 確認記録がある場合のみ delisted に確定 -> {statuses}")


# ------------------------------------------------------------- 集計

def test_summary_keeps_denominator():
    posted = D(2025, 1, 6)
    bench = make_series(posted - dt.timedelta(days=10), 400, flat(2000))

    up = make_series(posted - dt.timedelta(days=10), 400,
                     lambda i: 1000 if i < 8 else 1300)
    down = make_series(posted - dt.timedelta(days=10), 400,
                       lambda i: 1000 if i < 8 else 800)
    gone = make_series(posted - dt.timedelta(days=10), 70, flat(1000))

    table = {"1111": up, "2222": down, "3333": gone}

    def fetch(code, frm, to):
        return table.get(code, bench)

    v = Verifier(fetch, "1306", D(2026, 6, 30))
    results = v.verify_all([
        Pick("a", "1111", "上昇社", posted, "r"),
        Pick("b", "2222", "下落社", posted, "r"),
        Pick("c", "3333", "廃止社", posted, "r"),
    ])
    s = summarize(results)

    assert s["掲載総数"] == 3
    h12 = s["判定時点別"]["12m"]
    assert h12["掲載総数"] == 3, h12          # 分母は必ず3のまま
    assert h12["評価済"] == 2, h12            # 評価できたのは2件
    assert h12["ステータス内訳"]["data_missing"] == 1, h12
    assert h12["勝率_上昇"] == 50.0, h12
    assert s["要確認件数"] > 0
    print(f"OK: 分母保持 掲載{h12['掲載総数']}件/評価{h12['評価済']}件 "
          f"勝率{h12['勝率_上昇']}% 要確認{s['要確認件数']}件")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n=== {len(fns)} 件のテストがすべて通りました ===")
