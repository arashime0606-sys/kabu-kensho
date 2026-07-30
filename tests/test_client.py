"""
J-Quants V2 クライアントの検証。実APIには繋がず、応答を偽装して確認する。

確認する内容:
  - x-api-key ヘッダーで認証しているか
  - /v2/equities/bars/daily を叩いているか
  - 日付を YYYYMMDD で送っているか
  - 短縮項目名 AdjC を調整後終値として読めているか
  - 想定外の項目名だったとき、何が返ってきたかを明示して落ちるか
"""

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

os.environ.setdefault("JQUANTS_API_KEY", "dummy-key-for-test")

import jquants_client as jc  # noqa: E402

D = dt.date


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """requests.Session の代わり。呼ばれた内容を記録する。"""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return FakeResponse(self.payload, self.status)


def make_client(payload, status=200):
    c = jc.JQuantsClient(use_cache=False)
    fake = FakeSession(payload, status)
    c._session = fake
    return c, fake


# V2の実際の応答形式（項目名は短縮形）
V2_PAYLOAD = {
    "data": [
        {"Date": "20250106", "C": 1000.0, "AdjC": 1000.0, "Vo": 12345},
        {"Date": "20250107", "C": 1100.0, "AdjC": 1100.0, "Vo": 23456},
        {"Date": "20250108", "C": 550.0, "AdjC": 1100.0, "Vo": 34567},
    ]
}


def test_uses_x_api_key_header():
    c, fake = make_client(V2_PAYLOAD)
    c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    headers = fake.calls[0]["headers"]
    assert "x-api-key" in headers, headers
    assert "Authorization" not in headers, "V1のBearer認証が残っています"
    print("OK: x-api-key ヘッダーで認証している")


def test_uses_v2_endpoint():
    c, fake = make_client(V2_PAYLOAD)
    c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    url = fake.calls[0]["url"]
    assert url == "https://api.jquants.com/v2/equities/bars/daily", url
    assert "/v1/" not in url
    print(f"OK: V2エンドポイント {url}")


def test_date_format_is_yyyymmdd():
    c, fake = make_client(V2_PAYLOAD)
    c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    p = fake.calls[0]["params"]
    assert p["from"] == "20250101", p
    assert p["to"] == "20250131", p
    assert p["code"] == "72030", p  # 4桁は5桁に正規化
    print(f"OK: 日付 {p['from']}〜{p['to']} / コード {p['code']}")


def test_reads_adjc_not_raw_close():
    """3行目は分割で素の終値(C)が半分になっているが、AdjC は据え置き。
    AdjC を読めていれば『50%下落』の誤判定は起きない。"""
    c, _ = make_client(V2_PAYLOAD)
    s = c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    assert s[D(2025, 1, 8)] == 1100.0, s
    assert s[D(2025, 1, 8)] != 550.0, "素の終値(C)を読んでしまっています"
    assert len(s) == 3, s
    print(f"OK: 分割日も AdjC={s[D(2025, 1, 8)]} を採用（素のCは550）")


def test_hyphenated_date_also_works():
    payload = {"data": [{"Date": "2025-01-06", "AdjC": 999.0}]}
    c, _ = make_client(payload)
    s = c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    assert s[D(2025, 1, 6)] == 999.0
    print("OK: YYYY-MM-DD 形式の日付も読める")


def test_unknown_field_names_fail_loudly():
    """項目名が想定と違ったとき、黙って空を返さず、実際の項目名を出して落ちること。"""
    payload = {"data": [{"TradeDate": "20250106", "ClosePrice": 100.0}]}
    c, _ = make_client(payload)
    try:
        c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    except jc.JQuantsError as e:
        assert "ClosePrice" in str(e), str(e)
        print(f"OK: 想定外の項目名で明示的に失敗 -> {str(e)[:60]}...")
        return
    raise AssertionError("エラーになるべき場面で通ってしまいました")


def test_auth_error_is_clear():
    c, _ = make_client({"message": "Forbidden"}, status=403)
    try:
        c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    except jc.JQuantsError as e:
        assert "認証に失敗" in str(e)
        print("OK: 認証エラーは原因が分かる文言で落ちる")
        return
    raise AssertionError("エラーになるべき場面で通ってしまいました")


def test_empty_result_returns_empty_dict():
    c, _ = make_client({"data": []})
    s = c.adjusted_close_series("7203", D(2025, 1, 1), D(2025, 1, 31))
    assert s == {}
    print("OK: データ0件は空の辞書を返す（例外にしない）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\n=== {len(fns)} 件のテストがすべて通りました ===")
