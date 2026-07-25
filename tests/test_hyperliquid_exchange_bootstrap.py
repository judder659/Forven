"""Regression coverage for HyperLiquid Exchange bootstrap fallbacks."""

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


@pytest.fixture
def hl_exchange_module(monkeypatch):
    root = types.ModuleType("hyperliquid")
    root.__path__ = []

    exchange_mod = types.ModuleType("hyperliquid.exchange")
    exchange_calls = []

    class _DummyExchange:
        def __init__(self, account, url, **kwargs):
            exchange_calls.append({"account": account, "url": url, "kwargs": dict(kwargs)})
            spot_meta = kwargs.get("spot_meta")
            if spot_meta is None:
                raise IndexError("list index out of range")
            token_count = len(spot_meta.get("tokens", []))
            for row in spot_meta.get("universe", []):
                base, quote = row["tokens"]
                if base >= token_count or quote >= token_count:
                    raise IndexError("list index out of range")
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
    utils_mod.__path__ = []  # mark as a package so submodule imports resolve
    constants_mod = types.ModuleType("hyperliquid.utils.constants")
    constants_mod.TESTNET_API_URL = "https://test.hyperliquid.local"
    constants_mod.MAINNET_API_URL = "https://main.hyperliquid.local"
    utils_mod.constants = constants_mod

    types_mod = types.ModuleType("hyperliquid.utils.types")

    class _DummyCloid:
        @staticmethod
        def from_str(value):
            return value

        @staticmethod
        def from_int(value):
            return value

    types_mod.Cloid = _DummyCloid
    utils_mod.types = types_mod

    root.exchange = exchange_mod
    root.info = info_mod
    root.utils = utils_mod

    # Restoration is delegated so BOTH bindings (sys.modules and the
    # forven.exchange package attribute) go back. The previous teardown popped
    # only sys.modules, leaving this fake-SDK module bound as
    # forven.exchange.hyperliquid for the rest of the session — which is what made
    # nine test_reconcile_sweep cases fail with "skipped_exchange_unreachable"
    # whenever the full suite ran, while both files passed in isolation.
    fake_sdk = {
        "hyperliquid": root,
        "hyperliquid.exchange": exchange_mod,
        "hyperliquid.info": info_mod,
        "hyperliquid.utils": utils_mod,
        "hyperliquid.utils.constants": constants_mod,
        "hyperliquid.utils.types": types_mod,
    }
    with swapped_hyperliquid_sdk(fake_sdk) as module:
        module._exchange_calls = exchange_calls
        yield module


def test_get_exchange_retries_with_sanitized_spot_meta(hl_exchange_module, monkeypatch):
    hl = hl_exchange_module

    def _fake_urlopen(request, timeout=15):
        assert timeout == 15
        payload = json.loads(request.data.decode("utf-8"))
        if payload["type"] == "meta":
            return _DummyHttpResponse({"universe": [{"name": "SOL", "szDecimals": 2}]})
        if payload["type"] == "spotMeta":
            return _DummyHttpResponse(
                {
                    "tokens": [
                        {"name": "USDC", "szDecimals": 6},
                        {"name": "PURR", "szDecimals": 2},
                    ],
                    "universe": [
                        {"name": "PURR/USDC", "tokens": [1, 0], "index": 0, "isCanonical": True},
                        {"name": "@bad", "tokens": [4, 0], "index": 1, "isCanonical": False},
                    ],
                }
            )
        raise AssertionError(f"Unexpected HyperLiquid info payload: {payload}")

    monkeypatch.setattr(hl.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(
        hl,
        "_get_creds",
        lambda: {
            "HL_API_SECRET": "secret",
            "HL_WALLET_ADDRESS": "0xabc123",
            "USE_TESTNET": True,
        },
    )
    monkeypatch.setattr(hl.Account, "from_key", lambda _pk: types.SimpleNamespace(address="0xagent"))

    exchange, info, address = hl.get_exchange(testnet=True)

    assert address == "0xabc123"
    assert exchange.base_url == "https://test.hyperliquid.local"
    assert info.__class__.__name__ == "_HyperliquidDirectInfoClient"
    assert len(hl._exchange_calls) == 2
    assert "spot_meta" not in hl._exchange_calls[0]["kwargs"]
    sanitized_spot_meta = hl._exchange_calls[1]["kwargs"]["spot_meta"]
    assert len(sanitized_spot_meta["universe"]) == 1
    assert sanitized_spot_meta["universe"][0]["name"] == "PURR/USDC"


def test_get_exchange_reuses_sanitized_bootstrap_without_repeating_warnings(hl_exchange_module, monkeypatch):
    hl = hl_exchange_module
    warnings: list[str] = []

    def _fake_warning(message, *args):
        warnings.append(message % args if args else str(message))

    def _fake_urlopen(request, timeout=15):
        assert timeout == 15
        payload = json.loads(request.data.decode("utf-8"))
        if payload["type"] == "meta":
            return _DummyHttpResponse({"universe": [{"name": "SOL", "szDecimals": 2}]})
        if payload["type"] == "spotMeta":
            return _DummyHttpResponse(
                {
                    "tokens": [
                        {"name": "USDC", "szDecimals": 6},
                        {"name": "PURR", "szDecimals": 2},
                    ],
                    "universe": [
                        {"name": "PURR/USDC", "tokens": [1, 0], "index": 0, "isCanonical": True},
                        {"name": "@bad", "tokens": [4, 0], "index": 1, "isCanonical": False},
                    ],
                }
            )
        raise AssertionError(f"Unexpected HyperLiquid info payload: {payload}")

    monkeypatch.setattr(hl.log, "warning", _fake_warning)
    monkeypatch.setattr(hl.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(
        hl,
        "_get_creds",
        lambda: {
            "HL_API_SECRET": "secret",
            "HL_WALLET_ADDRESS": "0xabc123",
            "USE_TESTNET": True,
        },
    )
    monkeypatch.setattr(hl.Account, "from_key", lambda _pk: types.SimpleNamespace(address="0xagent"))

    first_exchange, first_info, first_address = hl.get_exchange(testnet=True)
    warnings_after_first = len(warnings)
    second_exchange, second_info, second_address = hl.get_exchange(testnet=True)

    assert first_address == second_address == "0xabc123"
    assert first_info is second_info
    assert first_exchange.base_url == second_exchange.base_url == "https://test.hyperliquid.local"
    # Reuse is proven by the shared Info object above; the second call must not
    # repeat any bootstrap warnings emitted by the first.
    assert len(warnings) == warnings_after_first
