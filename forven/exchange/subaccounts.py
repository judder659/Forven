"""Named-wallet management — Hyperliquid sub-account create/discover/fund/register.

The named-wallet registry (books.NAMED_WALLETS_SETTINGS_KEY inside
forven:settings) maps operator-chosen labels to funded Hyperliquid sub-account
addresses. A live trade stamped with a named-wallet label routes its orders to
that sub-account (master key signs, SDK vault_address routes — same mechanism
as direction books), giving e.g. Bot Factory bots a capital pool fully isolated
from the strategy pipeline's wallet.

Mutation happens ONLY through these functions (exposed at /api/wallets). The
generic settings PUT never touches the registry key but preserves it.

Exchange-action caveats surfaced to the operator rather than hidden:
  * createSubAccount requires ~$100k cumulative volume on a mainnet master.
  * subAccountTransfer moves funds and is protocol-barred for withdrawal-safe
    API/agent wallets — on that setup, fund the sub-account in the HL UI.
"""

from __future__ import annotations

import logging

from forven.db import kv_get, kv_set, log_activity
from forven.exchange import books

log = logging.getLogger(__name__)

# HL's subAccountTransfer `usd` field is an integer in micro-USD (1e6 == $1).
_USD_MICRO = 1_000_000


def _load_settings() -> dict:
    raw = kv_get("forven:settings", {})
    return raw if isinstance(raw, dict) else {}


def _save_registry(registry: dict[str, str]) -> None:
    settings = _load_settings()
    settings[books.NAMED_WALLETS_SETTINGS_KEY] = registry
    kv_set("forven:settings", settings)


def _trading_client():
    """The master-signed exchange client + info client + master address.

    Raises ValueError with an operator-facing message when credentials are
    not configured (surfaced as HTTP 400 by the router)."""
    from forven.exchange.hyperliquid import get_exchange, resolve_configured_testnet

    try:
        testnet = resolve_configured_testnet()
        exchange, info, address = get_exchange(testnet)
    except Exception as exc:
        raise ValueError(f"Hyperliquid client unavailable: {exc}")
    return exchange, info, address, testnet


def _action_error(response: object) -> str | None:
    """Extract an error message from an HL exchange-action response, or None on ok."""
    if not isinstance(response, dict):
        return f"unexpected exchange response: {response!r}"
    if str(response.get("status") or "").lower() == "ok":
        return None
    detail = response.get("response") or response.get("error") or response
    return str(detail)


def _open_trades_on_label(label: str) -> int:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE status = 'OPEN' AND LOWER(COALESCE(book, '')) = ?",
            (label,),
        ).fetchone()
        return int(row["n"] or 0) if row else 0


def _bots_on_label(label: str) -> list[str]:
    """Bots that actively ROUTE live orders to this wallet.

    Only LIVE-armed bots count: a paper bot merely remembers a wallet
    preference (see api_set_bot_wallet) and routes nothing there, so it must
    neither appear as a user of the wallet nor block its removal. Counting
    paper bots left a returned-to-paper bot's stale live_wallet permanently
    locking the wallet's Remove button."""
    from forven.db import get_db

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT name FROM bot_configs "
                "WHERE LOWER(COALESCE(live_wallet, '')) = ? "
                "AND LOWER(COALESCE(execution_mode, 'paper')) = 'live'",
                (label,),
            ).fetchall()
        return [str(r["name"]) for r in rows]
    except Exception:
        return []  # column predates migration on this DB — nothing references it


def _wallet_balances(info, address: str) -> dict:
    """Best-effort {perp_usd, spot_usd} split for one wallet address.

    Perp and spot USDC are SEPARATE Hyperliquid balances; the UI shows both so
    the operator can see where the collateral actually sits (and move it with
    a class transfer). spot_usd is the FREE spot balance: spot USDC on "hold"
    backs an open isolated perp position and is already inside perp_usd, so
    showing the total would display the same margin in both columns (and it
    isn't movable anyway). Each read degrades to None independently."""
    from forven.exchange.hyperliquid import _extract_spot_usdc_balance, _safe_float

    out: dict = {"perp_usd": None, "spot_usd": None}
    try:
        state = info.user_state(address)
        out["perp_usd"] = _safe_float(((state or {}).get("marginSummary") or {}).get("accountValue"))
    except Exception:
        pass
    try:
        _spot_total, spot_free = _extract_spot_usdc_balance(info, address)
        out["spot_usd"] = max(0.0, float(spot_free or 0.0))
    except Exception:
        pass
    return out


def can_transfer_funds(settings: dict | None = None) -> bool:
    """Whether the configured signing key can move balances at all.

    Hyperliquid protocol-bars API/agent wallets from every fund movement
    (sub-account funding, spot↔perp). Only when the signer IS the master
    wallet (no separate API address configured) can in-app transfers work —
    the UI hides the transfer controls entirely otherwise.
    """
    s = settings if isinstance(settings, dict) else _load_settings()
    master = str(s.get("hyperliquid_wallet") or "").strip().lower()
    api_address = str(s.get("hyperliquid_api_address") or "").strip().lower()
    if not master:
        return False
    return not api_address or api_address == master


def _resolve_transfer_address(label: str, settings: dict) -> tuple[str, str]:
    """(label, address) for a transferable wallet: named wallets AND the
    long/short direction-book sub-accounts (they're wallets too — the panel
    lists them, so funding them must work through the same buttons)."""
    clean = str(label or "").strip().lower()
    named = books.named_wallets(settings)
    if clean in named:
        return clean, named[clean]
    if clean in (books.LONG_BOOK, books.SHORT_BOOK):
        addr = books.book_address(clean, settings)
        if addr:
            return clean, addr
    raise ValueError(f"wallet {clean!r} is not registered")


def list_wallets(*, light: bool = False) -> dict:
    """Everything the wallets UI needs in one call.

    - master: the master wallet with a live perp/spot balance split.
    - registered: named wallets with the daemon snapshot equity plus a live
      perp/spot split (best-effort — a few wallets, on-demand call).
    - book_wallets: the configured long/short direction-book sub-accounts
      (they exist on the exchange too and must be visible/fundable).
    - books: direction-book status flags.
    - discovered: sub-accounts that exist on the exchange under the master
      wallet but are NOT registered/configured yet.
    - can_transfer: whether in-app balance moves are possible at all.

    light=True skips every exchange round-trip (no balances, no discovery) —
    used by pickers (e.g. the Bot Factory GO LIVE wallet select) that only
    need the label list and must never hang on a slow exchange read.
    """
    settings = _load_settings()
    registry = books.named_wallets(settings)

    if light:
        return {
            "master": (
                {"address": str(settings.get("hyperliquid_wallet") or "").strip(), "perp_usd": None, "spot_usd": None}
                if str(settings.get("hyperliquid_wallet") or "").strip()
                else None
            ),
            "registered": [
                {
                    "label": label,
                    "address": address,
                    "equity_usd": None,
                    "perp_usd": None,
                    "spot_usd": None,
                    "open_trades": 0,
                    "bots": [],
                }
                for label, address in sorted(registry.items())
            ],
            "book_wallets": _book_wallet_entries(settings, info=None),
            "discovered": [],
            "discovery_error": None,
            "books": books.live_books_status(settings),
            "master_wallet": str(settings.get("hyperliquid_wallet") or "") or None,
            "can_transfer": can_transfer_funds(settings),
        }

    # Per-wallet equity from the daemon snapshot (label -> equity).
    snapshot_books: dict = {}
    try:
        daemon_state = kv_get("daemon_state", {}) or {}
        account = daemon_state.get("exchange_account") or {}
        raw_books = account.get("books")
        if isinstance(raw_books, dict):
            snapshot_books = raw_books
    except Exception:
        pass

    # One client for master balances + per-wallet splits + discovery.
    client_error: str | None = None
    info = None
    master = None
    try:
        _exchange, info, master, _testnet = _trading_client()
    except Exception as exc:
        client_error = str(exc)

    registered = []
    for label, address in sorted(registry.items()):
        entry = {
            "label": label,
            "address": address,
            "equity_usd": _coerce_float(snapshot_books.get(label)),
            "perp_usd": None,
            "spot_usd": None,
            "open_trades": _open_trades_on_label(label),
            "bots": _bots_on_label(label),
        }
        if info is not None:
            entry.update(_wallet_balances(info, address))
        registered.append(entry)

    # Master address falls back to the configured settings value so a transient
    # client failure (breaker open, network blip) shows "balances unavailable"
    # rather than the misleading "not configured".
    master_address = master or str(settings.get("hyperliquid_wallet") or "").strip() or None
    master_block = None
    if master_address:
        master_block = {"address": master_address, "perp_usd": None, "spot_usd": None}
        if info is not None:
            master_block.update(_wallet_balances(info, master_address))

    book_wallets = _book_wallet_entries(settings, info=info)

    # Exchange-side discovery (best effort — the list must render without it).
    discovered: list[dict] = []
    discovery_error: str | None = client_error
    known_addresses = {a.lower() for a in registry.values()}
    for _label in (books.LONG_BOOK, books.SHORT_BOOK):
        addr = books.book_address(_label, settings)
        if addr:
            known_addresses.add(addr.lower())
    if info is not None and master:
        try:
            query = getattr(info, "query_sub_accounts", None)
            raw = query(master) if callable(query) else None
            for entry in raw or []:
                if not isinstance(entry, dict):
                    continue
                address = str(entry.get("subAccountUser") or "").strip()
                if not address or address.lower() in known_addresses:
                    continue
                equity = None
                try:
                    equity = float(
                        ((entry.get("clearinghouseState") or {}).get("marginSummary") or {}).get(
                            "accountValue"
                        )
                    )
                except (TypeError, ValueError):
                    pass
                discovered.append(
                    {
                        "name": str(entry.get("name") or ""),
                        "address": address,
                        "equity_usd": equity,
                    }
                )
        except Exception as exc:
            discovery_error = str(exc)

    return {
        "master": master_block,
        "registered": registered,
        "book_wallets": book_wallets,
        "discovered": discovered,
        "discovery_error": discovery_error,
        "books": books.live_books_status(settings),
        "master_wallet": str(settings.get("hyperliquid_wallet") or "") or None,
        "can_transfer": can_transfer_funds(settings),
    }


def _book_wallet_entries(settings: dict, *, info) -> list[dict]:
    """Rows for the configured long/short direction-book sub-accounts.

    These are real exchange sub-accounts holding pipeline exposure — the
    wallets panel must show them (with balances when a client is available)
    or the operator reasonably reports 'not all sub-accounts populate'."""
    entries: list[dict] = []
    for label in (books.LONG_BOOK, books.SHORT_BOOK):
        address = books.book_address(label, settings)
        if not address:
            continue
        entry = {"label": label, "address": address, "perp_usd": None, "spot_usd": None}
        if info is not None:
            entry.update(_wallet_balances(info, address))
        entries.append(entry)
    return entries


def _coerce_float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def register_wallet(label: str, address: str) -> dict:
    """Register an existing (already-created, operator-funded) sub-account."""
    clean_label = books.validate_wallet_label(label)
    clean_address = books.validate_wallet_address(address)

    settings = _load_settings()
    registry = books.named_wallets(settings)
    if clean_label in registry:
        raise ValueError(f"wallet label {clean_label!r} is already registered")
    taken = {a.lower(): lbl for lbl, a in registry.items()}
    if clean_address.lower() in taken:
        raise ValueError(
            f"address already registered as wallet {taken[clean_address.lower()]!r}"
        )
    for reserved_label in (books.LONG_BOOK, books.SHORT_BOOK):
        book_addr = books.book_address(reserved_label, settings)
        if book_addr and book_addr.lower() == clean_address.lower():
            raise ValueError(f"address is already the {reserved_label}-book sub-account")
    master = str(settings.get("hyperliquid_wallet") or "").strip()
    if master and master.lower() == clean_address.lower():
        raise ValueError("address is the master wallet — it needs no registration")

    registry[clean_label] = clean_address
    _save_registry(registry)
    log_activity(
        "info", "wallets",
        f"Named wallet '{clean_label}' registered ({clean_address})",
        {"label": clean_label, "address": clean_address},
    )
    return {"label": clean_label, "address": clean_address}


def remove_wallet(label: str) -> dict:
    """Unregister a named wallet. Fails closed while anything routes to it."""
    clean_label = str(label or "").strip().lower()
    registry = books.named_wallets(_load_settings())
    if clean_label not in registry:
        raise ValueError(f"wallet {clean_label!r} is not registered")
    open_count = _open_trades_on_label(clean_label)
    if open_count:
        raise ValueError(
            f"wallet {clean_label!r} has {open_count} OPEN trade(s) routed to it — "
            "close them first (removing the registry entry would strand the closes)"
        )
    bots = _bots_on_label(clean_label)
    if bots:
        raise ValueError(
            f"wallet {clean_label!r} is selected by bot(s): {', '.join(bots)} — "
            "switch them to another wallet (or paper) first"
        )
    registry.pop(clean_label)
    _save_registry(registry)
    # Clear stale wallet preferences on any (paper-mode) bots still pointing at
    # the now-unregistered label so GO LIVE never preselects a dead wallet.
    try:
        from forven.db import get_db

        with get_db() as conn:
            conn.execute(
                "UPDATE bot_configs SET live_wallet = NULL "
                "WHERE LOWER(COALESCE(live_wallet, '')) = ?",
                (clean_label,),
            )
    except Exception:
        pass  # cleanup is best-effort; a dangling pref is caught by GO LIVE validation
    log_activity(
        "info", "wallets", f"Named wallet '{clean_label}' removed", {"label": clean_label}
    )
    return {"label": clean_label, "removed": True}


def create_subaccount(name: str) -> dict:
    """Create a Hyperliquid sub-account and register it as a named wallet.

    The sub-account name doubles as the wallet label (validated first so we
    never create an exchange account we can't register). Exchange refusals —
    the ~$100k mainnet volume gate, agent-wallet restrictions — surface
    verbatim with a hint.
    """
    clean_label = books.validate_wallet_label(name)
    registry = books.named_wallets(_load_settings())
    if clean_label in registry:
        raise ValueError(f"wallet label {clean_label!r} is already registered")

    exchange, info, master, testnet = _trading_client()
    try:
        response = exchange.create_sub_account(clean_label)
    except Exception as exc:
        raise ValueError(f"sub-account creation failed: {exc}")
    error = _action_error(response)
    if error:
        raise ValueError(
            f"Hyperliquid refused the sub-account: {error} "
            f"(mainnet requires ~${books.SUBACCOUNT_VOLUME_REQUIREMENT_USD:,.0f} cumulative "
            "volume; some setups must create sub-accounts in the Hyperliquid UI)"
        )

    # The creation response's data field is the new address on current API
    # versions; fall back to discovery by name for older shapes.
    address = None
    if isinstance(response, dict):
        data = (response.get("response") or {}).get("data")
        candidate = str(data or "").strip()
        try:
            address = books.validate_wallet_address(candidate)
        except ValueError:
            address = None
    if address is None:
        try:
            query = getattr(info, "query_sub_accounts", None)
            for entry in (query(master) if callable(query) else None) or []:
                if isinstance(entry, dict) and str(entry.get("name") or "").strip().lower() == clean_label:
                    address = books.validate_wallet_address(entry.get("subAccountUser"))
                    break
        except Exception:
            address = None
    if address is None:
        raise ValueError(
            "sub-account was created but its address could not be resolved — "
            "use Discover to register it once it appears"
        )

    result = register_wallet(clean_label, address)
    log_activity(
        "info", "wallets",
        f"Hyperliquid sub-account '{clean_label}' created ({address}, testnet={testnet})",
        {"label": clean_label, "address": address, "testnet": testnet},
    )
    result["created"] = True
    return result


def transfer(label: str, amount_usd: float, *, deposit: bool) -> dict:
    """Move USD between the master wallet and a named sub-account.

    deposit=True moves master → sub-account; False moves it back. On the
    withdrawal-safe mainnet setup (API/agent wallet), Hyperliquid bars
    transfers by protocol — the refusal is surfaced with a UI hint.
    """
    settings = _load_settings()
    if not can_transfer_funds(settings):
        raise ValueError(
            "This setup signs with an API/agent wallet, which Hyperliquid bars "
            "from moving funds — deposit via the Hyperliquid app instead."
        )
    clean_label, address = _resolve_transfer_address(label, settings)
    try:
        amount = float(amount_usd)
    except (TypeError, ValueError):
        raise ValueError("amount_usd must be a number")
    if not (amount > 0) or amount != amount or amount == float("inf"):
        raise ValueError("amount_usd must be a positive finite dollar amount")

    micro_usd = int(round(amount * _USD_MICRO))
    if micro_usd <= 0:
        raise ValueError("amount_usd is below the minimum transferable unit")

    exchange, _info, _master, testnet = _trading_client()
    try:
        response = exchange.sub_account_transfer(
            sub_account_user=address, is_deposit=bool(deposit), usd=micro_usd
        )
    except Exception as exc:
        raise ValueError(f"transfer failed: {exc}")
    error = _action_error(response)
    if error:
        raise ValueError(
            f"Hyperliquid refused the transfer: {error} "
            "(API/agent wallets cannot move funds by protocol — fund the "
            "sub-account from the Hyperliquid UI instead)"
        )
    direction = "deposit to" if deposit else "withdrawal from"
    log_activity(
        "warning", "wallets",
        f"${amount:,.2f} {direction} sub-account '{clean_label}' ({address}, testnet={testnet})",
        {"label": clean_label, "address": address, "amount_usd": amount, "deposit": bool(deposit)},
    )
    return {
        "label": clean_label,
        "address": address,
        "amount_usd": amount,
        "deposit": bool(deposit),
        "status": "ok",
    }


def class_transfer(wallet: str | None, amount_usd: float, *, to_perp: bool) -> dict:
    """Move USDC between a wallet's SPOT and PERP balances (usdClassTransfer).

    wallet=None/"master" acts on the master wallet; a named-wallet label acts
    on that sub-account (the SDK tags the transfer with `subaccount:{addr}`).
    Amount is plain USD (usdClassTransfer takes a decimal amount, unlike
    subAccountTransfer's micro-USD int). Fund movements are protocol-barred
    for withdrawal-safe API/agent wallets — refusals surface with a hint.
    """
    settings = _load_settings()
    if not can_transfer_funds(settings):
        raise ValueError(
            "This setup signs with an API/agent wallet, which Hyperliquid bars "
            "from moving funds — use the Hyperliquid app's Spot/Perp transfer instead."
        )
    clean_label = str(wallet or "").strip().lower() or None
    if clean_label in (None, "master"):
        clean_label = None
        address = None
    else:
        clean_label, address = _resolve_transfer_address(clean_label, settings)
    try:
        amount = float(amount_usd)
    except (TypeError, ValueError):
        raise ValueError("amount_usd must be a number")
    if not (amount > 0) or amount != amount or amount == float("inf"):
        raise ValueError("amount_usd must be a positive finite dollar amount")

    from forven.exchange.hyperliquid import get_exchange, resolve_configured_testnet

    try:
        testnet = resolve_configured_testnet()
        exchange, _info, _master = get_exchange(testnet, vault_address=address)
    except Exception as exc:
        raise ValueError(f"Hyperliquid client unavailable: {exc}")
    try:
        response = exchange.usd_class_transfer(amount=amount, to_perp=bool(to_perp))
    except Exception as exc:
        raise ValueError(f"class transfer failed: {exc}")
    error = _action_error(response)
    if error:
        raise ValueError(
            f"Hyperliquid refused the spot/perp transfer: {error} "
            "(API/agent wallets cannot move funds by protocol — use the "
            "Hyperliquid UI instead)"
        )
    target = "perp" if to_perp else "spot"
    scope = clean_label or "master"
    log_activity(
        "warning", "wallets",
        f"${amount:,.2f} moved to {target} balance on '{scope}' (testnet={testnet})",
        {"wallet": scope, "amount_usd": amount, "to_perp": bool(to_perp)},
    )
    return {"wallet": scope, "amount_usd": amount, "to_perp": bool(to_perp), "status": "ok"}
