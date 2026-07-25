"""Regression coverage for HyperLiquid Info bootstrap fallbacks."""

from __future__ import annotations

import json
import types

import pytest

from tests.hl_sdk_sandbox import swapped_hyperliquid_sdk


class _DummyHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


_FAKE_SDK_MODULE_NAMES = (
    "hyperliquid",
    "hyperliquid.exchange",
    "hyperliquid.info",
    "hyperliquid.utils",
    "hyperliquid.utils.constants",
    "hyperliquid.utils.types",
)


def _build_fake_sdk() -> dict[str, types.ModuleType]:
    """A stand-in `hyperliquid` SDK whose Info() bootstrap always blows up.

    Must mirror EVERY symbol forven/exchange/hyperliquid.py imports at module
    scope, or the reload below dies with ModuleNotFoundError before any test
    body runs. `Cloid` (hyperliquid.utils.types) was added to the connector for
    idempotent client order ids and is easy to miss here — if this fixture ever
    errors at setup again, diff its imports against the connector's.
    """
    root = types.ModuleType("hyperliquid")
    root.__path__ = []

    exchange_mod = types.ModuleType("hyperliquid.exchange")

    class _DummyExchange:
        def __init__(self, account, url, **kwargs):
            self.wallet = account
            self.base_url = url
            self.kwargs = kwargs

    exchange_mod.Exchange = _DummyExchange

    info_mod = types.ModuleType("hyperliquid.info")

    class _BrokenInfo:
        def __init__(self, _url, skip_ws=True, **_kwargs):
            assert skip_ws is True
            raise IndexError("list index out of range")

    info_mod.Info = _BrokenInfo

    utils_mod = types.ModuleType("hyperliquid.utils")
    utils_mod.__path__ = []
    constants_mod = types.ModuleType("hyperliquid.utils.constants")
    constants_mod.TESTNET_API_URL = "https://test.hyperliquid.local"
    constants_mod.MAINNET_API_URL = "https://main.hyperliquid.local"

    types_mod = types.ModuleType("hyperliquid.utils.types")

    class _DummyCloid:
        def __init__(self, raw: str):
            self._raw = str(raw)

        @classmethod
        def from_str(cls, raw: str) -> "_DummyCloid":
            return cls(raw)

        def to_raw(self) -> str:
            return self._raw

        def __eq__(self, other) -> bool:
            return isinstance(other, _DummyCloid) and other._raw == self._raw

        def __repr__(self) -> str:  # pragma: no cover - debugging aid
            return f"Cloid({self._raw!r})"

    types_mod.Cloid = _DummyCloid

    utils_mod.constants = constants_mod
    utils_mod.types = types_mod
    root.exchange = exchange_mod
    root.info = info_mod
    root.utils = utils_mod

    return {
        "hyperliquid": root,
        "hyperliquid.exchange": exchange_mod,
        "hyperliquid.info": info_mod,
        "hyperliquid.utils": utils_mod,
        "hyperliquid.utils.constants": constants_mod,
        "hyperliquid.utils.types": types_mod,
    }


@pytest.fixture
def hl_module():
    """The connector imported against a broken-SDK stand-in, fully restored after.

    Restoration is delegated to swapped_hyperliquid_sdk because getting it wrong
    is invisible here and fatal elsewhere — see that module's docstring.
    """
    with swapped_hyperliquid_sdk(_build_fake_sdk()) as module:
        yield module


def test_get_account_value_uses_direct_info_fallback_when_sdk_bootstrap_breaks(hl_module, monkeypatch):
    hl = hl_module

    def _kv_get(key, default=None):
        if key == "forven:settings":
            return {
                "hyperliquid_wallet": "0xabc123",
                "hyperliquid_testnet": True,
            }
        if key == "forven:settings:secrets":
            return {}
        return default

    def _fake_urlopen(request, timeout=15):
        assert timeout == 15
        payload = json.loads(request.data.decode("utf-8"))
        if payload["type"] == "clearinghouseState":
            return _DummyHttpResponse(
                {
                    "marginSummary": {
                        "accountValue": "0",
                        "totalMarginUsed": "0",
                        "totalNtlPos": "0",
                        "totalRawUsd": "0",
                    }
                }
            )
        if payload["type"] == "spotClearinghouseState":
            return _DummyHttpResponse(
                {"balances": [{"coin": "USDC", "total": "1002.68", "hold": "0"}]}
            )
        if payload["type"] == "spotMeta":
            return _DummyHttpResponse({"tokens": [], "universe": []})
        raise AssertionError(f"Unexpected HyperLiquid info payload: {payload}")

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "kv_get", _kv_get)
    monkeypatch.setattr(hl, "_with_breaker", lambda _name, _breaker, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(hl.urllib.request, "urlopen", _fake_urlopen)

    account = hl.get_account_value(testnet=True, require_connection=True)

    assert account["accountValue"] == 1002.68
    assert account["totalRawUsd"] == 1002.68
    assert account["withdrawable"] == 1002.68


def test_build_info_client_logs_fallback_notice_once_per_process(hl_module, monkeypatch):
    hl = hl_module
    infos: list[str] = []
    warnings: list[str] = []

    def _record(sink):
        def _log(message, *args):
            sink.append(message % args if args else str(message))

        return _log

    monkeypatch.setattr(hl.log, "info", _record(infos))
    monkeypatch.setattr(hl.log, "warning", _record(warnings))

    first = hl._build_info_client("https://test.hyperliquid.local")
    second = hl._build_info_client("https://test.hyperliquid.local")

    assert first is second
    assert first.__class__.__name__ == "_HyperliquidDirectInfoClient"

    # The once-per-process contract: the SDK bootstrap failure is announced on
    # the FIRST build and suppressed thereafter (_warn_once keys on the url).
    fallback_notices = [m for m in infos if "direct /info fallback client" in m]
    assert len(fallback_notices) == 1

    # Deliberately INFO, not WARNING: the testnet spot-meta "list index out of
    # range" quirk is transparently handled by the direct /info client, and
    # logging it at WARNING made operators think a healthy exchange was broken.
    assert warnings == []
