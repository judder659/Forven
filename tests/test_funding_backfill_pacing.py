"""The scheduled funding backfill must not starve the live account reads.

On 2026-11 it sent dozens of fundingHistory pages a second every six hours.
Hyperliquid answered HTTP 429, the account circuit breaker opened, and the
daemon priced the live book from cached equity about 225 times in three days.
"""

import io
import urllib.error
from datetime import datetime, timedelta, timezone

from forven import market_data_collector as mdc

NOW = datetime.now(timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.hyperliquid.xyz/info", code, "x", {}, io.BytesIO(b""))


class _Kv:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


def _patch_ensure(monkeypatch, *, bounds, result, kv=None, records=10_000_000):
    kv = kv or _Kv()
    calls: list[dict] = []

    def fake_backfill(asset, days_back=365, **options):
        calls.append({"asset": asset, "days_back": days_back, **options})
        return dict(result)

    monkeypatch.setattr(mdc, "get_funding_coverage_bounds", lambda asset: bounds)
    monkeypatch.setattr(mdc, "get_funding_record_count", lambda *a, **k: records)
    monkeypatch.setattr(mdc, "backfill_funding_history", fake_backfill)
    monkeypatch.setattr(mdc, "kv_get", kv.get)
    monkeypatch.setattr(mdc, "kv_set", kv.set)
    return kv, calls


def test_stale_history_is_topped_up_from_its_newest_record(monkeypatch):
    start = _ms(NOW - timedelta(days=730))
    latest = _ms(NOW - timedelta(days=5))
    _, calls = _patch_ensure(
        monkeypatch,
        bounds=(start - 1000, latest),
        result={"total_stored": 120, "total_fetched": 120},
    )
    outcome = mdc.ensure_funding_history("BNB", start)
    assert outcome["action"] == "backfilled" and outcome["topped_up"] is True
    assert calls == [{"asset": "BNB", "days_back": calls[0]["days_back"], "start_ms": latest + 1}]


def test_stale_history_with_holes_is_downloaded_in_full(monkeypatch):
    start = _ms(NOW - timedelta(days=730))
    _, calls = _patch_ensure(
        monkeypatch,
        bounds=(start - 1000, _ms(NOW - timedelta(days=5))),
        result={"total_stored": 900, "total_fetched": 900},
        records=10,
    )
    mdc.ensure_funding_history("BNB", start)
    assert "start_ms" not in calls[0]


def test_a_young_coin_that_went_stale_is_topped_up_not_called_exhausted(monkeypatch):
    start = _ms(NOW - timedelta(days=730))
    listed = _ms(NOW - timedelta(days=200))
    latest = _ms(NOW - timedelta(days=4))
    kv = _Kv({mdc._FUNDING_BACKFILL_STATE_KEY: {"ENA": {
        "attempted_at": (NOW - timedelta(days=3)).isoformat(),
        "target_start_ms": start,
        "oldest_reachable_ms": listed,
    }}})
    kv, calls = _patch_ensure(
        monkeypatch, bounds=(listed, latest), kv=kv,
        result={"total_stored": 96, "total_fetched": 96},
    )
    outcome = mdc.ensure_funding_history("ENA", start)
    assert outcome["action"] == "backfilled"
    assert calls[0]["start_ms"] == latest + 1
    # The top-up keeps what the full backfill learned about the listing date.
    assert kv.store[mdc._FUNDING_BACKFILL_STATE_KEY]["ENA"]["oldest_reachable_ms"] == listed


def test_a_rate_limit_is_reported_and_not_held_against_the_asset(monkeypatch):
    start = _ms(NOW - timedelta(days=300))
    kv, _ = _patch_ensure(
        monkeypatch, bounds=(None, None),
        result={"total_stored": 0, "total_fetched": 0, "rate_limited": True},
    )
    assert mdc.ensure_funding_history("BCH", start)["action"] == "rate_limited"
    assert kv.store == {}


def test_a_failed_backfill_backs_off_and_never_marks_the_exchange_exhausted(monkeypatch):
    start = _ms(NOW - timedelta(days=300))
    existing_earliest = _ms(NOW - timedelta(days=30))
    kv, calls = _patch_ensure(
        monkeypatch, bounds=(existing_earliest, _ms(NOW)), records=10,
        result={"total_stored": 0, "total_fetched": 0, "error": "HTTP Error 500: Internal Server Error"},
    )
    first = mdc.ensure_funding_history("ALLO", start)
    assert first["action"] == "error" and first["failures"] == 1
    entry = kv.store[mdc._FUNDING_BACKFILL_STATE_KEY]["ALLO"]
    assert "oldest_reachable_ms" not in entry

    # 8 hours later: past the plain 6 h cooldown, inside the doubled one.
    entry["attempted_at"] = (NOW - timedelta(hours=8)).isoformat()
    again = mdc.ensure_funding_history("ALLO", start + 8 * 3600 * 1000)
    assert again["action"] == "cooldown" and again["failures"] == 1
    assert len(calls) == 1

    entry["attempted_at"] = (NOW - timedelta(hours=13)).isoformat()
    assert mdc.ensure_funding_history("ALLO", start)["action"] == "error"
    assert kv.store[mdc._FUNDING_BACKFILL_STATE_KEY]["ALLO"]["failures"] == 2


def _patch_reconcile(monkeypatch, outcomes, *, coins=None, state=None):
    ensured: list[dict] = []

    def fake_ensure(asset, start_ms, **options):
        ensured.append({"asset": asset, **options})
        return {"action": outcomes.get(asset, "covered"), "asset": asset}

    monkeypatch.setattr(mdc, "ensure_funding_history", fake_ensure)
    monkeypatch.setattr(mdc, "_hyperliquid_funding_coins", lambda assets: coins or {a: a for a in assets})
    monkeypatch.setattr(mdc, "_load_backfill_state", lambda: state or {})
    monkeypatch.setattr(mdc, "log_activity", lambda *a, **k: None)
    return ensured


def test_reconcile_stops_at_the_first_rate_limit(monkeypatch):
    ensured = _patch_reconcile(monkeypatch, {"ETH": "rate_limited"})
    summary = mdc.reconcile_funding_history(assets=["BTC", "ETH", "SOL", "BCH"])
    assert [call["asset"] for call in ensured] == ["BTC", "ETH"]
    assert summary["stopped"] == "rate_limited"
    assert summary["results"]["SOL"]["action"] == "deferred"
    assert summary["counts"] == {"covered": 1, "rate_limited": 1, "deferred": 2}


def test_reconcile_skips_unlisted_coins_and_uses_hyperliquid_names(monkeypatch):
    ensured = _patch_reconcile(
        monkeypatch, {},
        coins={"BTC": "BTC", "TSLA": None, "1000PEPE": "kPEPE"},
    )
    summary = mdc.reconcile_funding_history(assets=["BTC", "TSLA", "1000PEPE"])
    assert summary["results"]["TSLA"]["action"] == "skipped_unlisted"
    assert [(c["asset"], c["venue_coin"]) for c in ensured] == [("BTC", "BTC"), ("1000PEPE", "kPEPE")]
    assert all(c["page_interval_seconds"] == mdc._FUNDING_RECONCILE_PAGE_INTERVAL_SECONDS for c in ensured)
    assert all(c["deadline"] is not None for c in ensured)


def test_reconcile_takes_the_least_recently_attempted_asset_first(monkeypatch):
    state = {
        "BTC": {"attempted_at": "2026-11-10T09:00:00+00:00"},
        "ETH": {"attempted_at": "2026-11-09T09:00:00+00:00"},
    }
    ensured = _patch_reconcile(monkeypatch, {}, state=state)
    mdc.reconcile_funding_history(assets=["BTC", "ETH", "BCH"])
    assert [call["asset"] for call in ensured] == ["BCH", "ETH", "BTC"]


def test_reconcile_defers_everything_once_its_time_is_up(monkeypatch):
    ensured = _patch_reconcile(monkeypatch, {})
    summary = mdc.reconcile_funding_history(assets=["BTC", "ETH"], budget_seconds=0)
    assert ensured == []
    assert summary["stopped"] == "time_budget"
    assert summary["counts"] == {"deferred": 2}


def test_backfill_stops_on_429_after_one_request(monkeypatch):
    requests: list[dict] = []

    def post(body, **_k):
        requests.append(body)
        raise _http_error(429)

    monkeypatch.setattr(mdc, "post_hyperliquid_info", post)
    monkeypatch.setattr(mdc, "log_activity", lambda *a, **k: None)
    result = mdc.backfill_funding_history("BCH", days_back=30)
    assert len(requests) == 1
    assert result["rate_limited"] is True and result["error"] is None


def test_backfill_paces_pages_and_asks_for_the_venue_coin_from_the_start_given(monkeypatch):
    start = _ms(NOW - timedelta(days=3))
    pages = [
        [{"fundingRate": "0.0001", "time": start + i * 3_600_000} for i in range(500)],
        [{"fundingRate": "0.0001", "time": start + (500 + i) * 3_600_000} for i in range(3)],
    ]
    requests: list[dict] = []
    waits: list[float] = []
    stored: list[tuple] = []

    def post(body, **_k):
        requests.append(body)
        return pages[len(requests) - 1]

    monkeypatch.setattr(mdc, "post_hyperliquid_info", post)
    monkeypatch.setattr(mdc, "_pace_funding_page", waits.append)
    monkeypatch.setattr(mdc, "_store_batch", lambda rows: stored.extend(rows) or len(rows))
    monkeypatch.setattr(mdc, "log_activity", lambda *a, **k: None)
    monkeypatch.setattr(mdc, "_now_ms", lambda: start + 10_000 * 3_600_000)

    result = mdc.backfill_funding_history(
        "1000PEPE", start_ms=start, venue_coin="kPEPE", page_interval_seconds=5.0,
    )
    assert [r["coin"] for r in requests] == ["kPEPE", "kPEPE"]
    assert requests[0]["startTime"] == start
    assert waits == [5.0, 5.0]
    assert result["total_stored"] == 503 and result["rate_limited"] is False
    assert {row[0] for row in stored} == {"1000PEPE"}


def test_backfill_stops_between_pages_at_its_deadline(monkeypatch):
    monkeypatch.setattr(mdc, "post_hyperliquid_info", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    monkeypatch.setattr(mdc, "log_activity", lambda *a, **k: None)
    result = mdc.backfill_funding_history("BTC", days_back=30, deadline=0.0)
    assert result["deferred"] is True and result["pages"] == 0
