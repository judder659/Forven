"""A bot's own database writes and reads, routed through the API.

Inside a bot subprocess (it holds a spawn token), every write a paper bot
makes goes to ``POST /api/bot-factory/internal/bots/{id}/write`` with that
token, and the API checks the bot only touches its own rows. Anywhere else
(the API process, tests, the CLI) the same calls go straight to
``forven.db``.

When the API can't be reached (it is restarting), bookkeeping writes wait in
an in-memory queue and are sent in order once it is back; trade writes
return None instead, so the runner skips the entry or retries the close on
its next tick. A live order's trade rows are written by the live path that
places it (below), not through ``write``.

Reads (the bot's own config, status, positions and equity, and the market
data its inputs use) go to ``POST /api/bot-factory/internal/bots/{id}/read``
the same way. When the API can't be reached a read falls back to the
database, so a bot keeps working while Forven restarts.

A bot's AI calls go to ``POST /api/bot-factory/internal/bots/{id}/ai``:
Forven asks the model with its own login, so the bot needs none.

A live bot's orders go to ``POST /api/bot-factory/internal/bots/{id}/live``:
Forven places them through its own live gate stack with its own exchange
keys, so the bot holds none. An order is sent once and never retried here;
the runner re-reads its positions every tick, so an answer lost to a
timeout is picked up from the database.

Set ``FORVEN_BOT_WRITES_VIA_API=0`` in Forven's environment to send bot
writes, reads and AI calls straight to the database and the model as before.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any

from forven.bot_factory.broker_client import (
    BOT_ID_ENV,
    BOT_TOKEN_ENV,
    BOT_TOKEN_HEADER,
    BROKER_HOST_ENV,
    is_brokered_process,
)

logger = logging.getLogger(__name__)

VIA_API_ENV = "FORVEN_BOT_WRITES_VIA_API"
# Trade writes are never queued: an entry that can't be recorded is skipped,
# a close is retried by the runner on its next tick.
TRADE_OPS = frozenset({"execute_bot_trade", "close_bot_trade"})
BOOKKEEPING_OPS = frozenset({
    "set_bot_status", "heartbeat_bot", "reconcile_bot_realized_pnl", "update_bot_equity_state",
    "accrue_bot_funding", "log_bot_decision", "reset_bot_errors", "increment_bot_errors",
    "increment_bot_llm_calls", "add_bot_llm_cost", "log_activity",
})
OPS = TRADE_OPS | BOOKKEEPING_OPS
# Reads a bot may make, and the module each one lives in.
READ_OPS = {
    "get_bot": "forven.db",
    "get_bot_status": "forven.db",
    "get_bot_run_id": "forven.db",
    "get_open_bot_positions": "forven.db",
    "get_bot_equity_state": "forven.db",
    "get_open_followed_trades": "forven.db",
    "get_recent_strategy_signals": "forven.db",
    "get_funding_rate_series": "forven.market_data_collector",
    "get_open_interest_series": "forven.market_data_collector",
}
# Live orders a bot may ask Forven to place for it.
LIVE_OPS = frozenset({"open_live", "close_live", "flatten_live_positions"})
# Heartbeats are dropped rather than queued; only the next one matters.
_NOT_QUEUED = frozenset({"heartbeat_bot"})
# What a queued write answers while the API is away: the counters read as
# zero, so a cap check can't trip on a missing number.
_QUEUED_RESULT = {"increment_bot_errors": 0, "increment_bot_llm_calls": 0, "add_bot_llm_cost": 0.0}
_QUEUE_MAX = 500
_TIMEOUT_SECONDS = 10
# A model can take a while to answer.
_AI_TIMEOUT_SECONDS = 300

# Placing an order includes the exchange round trips and the gate checks.
_LIVE_TIMEOUT_SECONDS = 120

# How long a read or model call waits for the API to come back (a restart)
# when the database is closed to the bot or the model can't be asked here.
_API_WAIT_SECONDS = 90

_queue: deque[tuple[str, dict]] = deque(maxlen=_QUEUE_MAX)
_lock = threading.Lock()


class BotWriteRefused(RuntimeError):
    """The API answered and refused the write (bad token, not this bot's row)."""


def via_api() -> bool:
    return is_brokered_process() and os.environ.get(VIA_API_ENV, "1").strip() != "0"


def _url(bot_id: str, kind: str = "write") -> str:
    port = os.environ.get("FORVEN_PORT", "8003")
    host = os.environ.get(BROKER_HOST_ENV, "").strip().strip("[]") or "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/api/bot-factory/internal/bots/{bot_id}/{kind}"


def _post(op: str, args: dict, kind: str = "write", timeout: float = _TIMEOUT_SECONDS) -> Any:
    bot_id = os.environ.get(BOT_ID_ENV, "")
    body = json.dumps({"op": op, "args": args}, default=str).encode("utf-8")
    request = urllib.request.Request(
        _url(bot_id, kind), data=body, method="POST",
        headers={BOT_TOKEN_HEADER: os.environ.get(BOT_TOKEN_ENV, ""), "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 403, 404, 409):
            raise BotWriteRefused(f"{op} refused (HTTP {exc.code})") from exc
        raise ConnectionError(f"API answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ConnectionError(str(exc)) from exc


def _flush() -> bool:
    """Send queued writes in order; True when the queue is empty."""
    while True:
        with _lock:
            if not _queue:
                return True
            op, args = _queue[0]
        try:
            _post(op, args)
        except ConnectionError:
            return False
        except BotWriteRefused as exc:
            logger.warning("Dropped a queued bot write: %s", exc)
        with _lock:
            if _queue and _queue[0] == (op, args):
                _queue.popleft()


def write(op: str, _direct: Any, **args: Any) -> Any:
    """Send one write through the API, or call ``_direct(**args)`` outside a
    bot process."""
    if op not in OPS:
        raise ValueError(f"unknown bot write {op!r}")
    if not via_api():
        return _direct(**args)
    if not _flush():
        return _unavailable(op, args)
    try:
        return _post(op, args)
    except ConnectionError as exc:
        logger.warning("API unreachable for bot write %s: %s", op, exc)
        return _unavailable(op, args)


def _unavailable(op: str, args: dict) -> Any:
    if op in TRADE_OPS or op in _NOT_QUEUED:
        return None
    with _lock:
        if len(_queue) == _queue.maxlen:
            logger.warning("Bot write queue full; dropping the oldest write")
        _queue.append((op, args))
    return _QUEUED_RESULT.get(op)


def read(op: str, _direct: Any, **args: Any) -> Any:
    """Read through the API, or call ``_direct(**args)`` outside a bot
    process or while the API is away."""
    if op not in READ_OPS:
        raise ValueError(f"unknown bot read {op!r}")
    if not via_api():
        return _direct(**args)
    # Send queued writes first, so the bot reads what it wrote.
    _flush()
    try:
        return _post(op, args, kind="read")
    except ConnectionError as exc:
        logger.warning("API unreachable for bot read %s, reading the database: %s", op, exc)
    try:
        return _direct(**args)
    except Exception as exc:
        # The database is closed to this bot: wait for the API instead.
        logger.warning("Bot read %s can't use the database (%s); waiting for the API", op, exc)
        return _wait_for_api(op, args, kind="read")


def _wait_for_api(op: str, args: dict, *, kind: str, timeout: float = _TIMEOUT_SECONDS) -> Any:
    deadline = time.monotonic() + _API_WAIT_SECONDS
    delay = 1.0
    while True:
        try:
            return _post(op, args, kind=kind, timeout=timeout)
        except ConnectionError:
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 15.0)


def ask_model(model: str, messages: list[dict], *, max_tokens: int, temperature: float) -> dict | None:
    """Have Forven ask the bot's model, waiting out an API restart. None when
    the API stays unreachable (the caller then asks the model itself);
    RuntimeError when the model call failed."""
    try:
        answer = _wait_for_api(
            "ask_model",
            {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            kind="ai", timeout=_AI_TIMEOUT_SECONDS,
        )
    except ConnectionError as exc:
        logger.warning("API unreachable for the bot's model call, calling it here: %s", exc)
        return None
    except BotWriteRefused as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(answer, dict) or answer.get("error"):
        raise RuntimeError((answer or {}).get("error") or "the model call failed")
    return answer


def live(op: str, _direct: Any, **args: Any) -> Any:
    """Have Forven place a live order for this bot, or call ``_direct(**args)``
    outside a bot process. Sent once: an unreachable API or a refusal comes
    back as the op's own "not done" answer, never as an exception."""
    if op not in LIVE_OPS:
        raise ValueError(f"unknown bot live op {op!r}")
    if not via_api():
        return _direct(**args)
    _flush()
    try:
        result = _post(op, args, kind="live", timeout=_LIVE_TIMEOUT_SECONDS)
    except (ConnectionError, BotWriteRefused) as exc:
        logger.warning("Live %s not placed through the API: %s", op, exc)
        return _live_not_done(op, f"Forven could not place it: {exc}")
    if op == "open_live":
        if isinstance(result, list) and len(result) == 2:
            return result[0], str(result[1])
        return None, "blocked: no answer from Forven"
    return result


def _live_not_done(op: str, message: str) -> Any:
    if op == "open_live":
        return None, f"blocked: {message}"
    if op == "close_live":
        return {"state": "failed", "message": message}
    return [{"state": "failed", "message": message}]


def queued() -> int:
    with _lock:
        return len(_queue)


def _reset_for_tests() -> None:
    with _lock:
        _queue.clear()


def _bound(fn: Any, args: tuple, kwargs: dict) -> dict:
    return dict(inspect.signature(fn).bind(*args, **kwargs).arguments)


def routed(db_module: Any, name: str):
    """A stand-in for ``db_module.<name>`` that routes through ``write``.

    Callers pass ``forven.db`` themselves, so this module never imports it
    and stays out of the import graph's cycles. The database function is
    looked up at call time.
    """
    if name not in OPS:
        raise ValueError(f"unknown bot write {name!r}")

    def call(*args: Any, **kwargs: Any) -> Any:
        fn = getattr(db_module, name)
        return write(name, _direct=fn, **_bound(fn, args, kwargs))

    call.__name__ = name
    call.__doc__ = f"forven.db.{name}, routed through the API inside a bot process."
    return call


def routed_read(module: Any, name: str):
    """Like ``routed``, for one of ``READ_OPS``."""
    if name not in READ_OPS:
        raise ValueError(f"unknown bot read {name!r}")

    def call(*args: Any, **kwargs: Any) -> Any:
        fn = getattr(module, name)
        return read(name, _direct=fn, **_bound(fn, args, kwargs))

    call.__name__ = name
    call.__doc__ = f"{READ_OPS[name]}.{name}, read through the API inside a bot process."
    return call
