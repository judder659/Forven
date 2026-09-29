"""Source contracts, registry, and health/circuit state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import AsyncIterator, Protocol

import pandas as pd


class Stream(str, Enum):
    CANDLES = "candles"
    FUNDING = "funding"
    OI = "oi"
    LSR = "lsr"
    TAKER = "taker"
    LIQUIDATIONS = "liquidations"
    FEAR_GREED = "fear_greed"
    MACRO = "macro"
    TRADES = "trades"
    ORDERBOOK = "orderbook"
    ONCHAIN = "onchain"


@dataclass(frozen=True)
class SourceHealth:
    source: str
    status: str = "unknown"
    consecutive_failures: int = 0
    last_success_at: str | None = None
    last_failure_at: str | None = None
    message: str = ""

    @property
    def available(self) -> bool:
        return self.status not in {"disabled", "open"}


class Source(Protocol):
    id: str
    capabilities: set[Stream]

    def fetch(
        self,
        ref: object,
        stream: Stream,
        since: object | None = None,
        until: object | None = None,
    ) -> pd.DataFrame:
        ...

    async def stream(self, ref: object, stream: Stream) -> AsyncIterator[pd.DataFrame]:
        ...

    def health(self) -> SourceHealth:
        ...


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_successes: int = 1,
        recovery_timeout_seconds: float = 300.0,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_successes = max(1, int(recovery_successes))
        # After this long open, allow ONE half-open trial so a transient source
        # outage self-heals instead of latching "open" until the process restarts.
        self.recovery_timeout_seconds = max(0.0, float(recovery_timeout_seconds))
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.status = "closed"
        self._opened_at_monotonic: float | None = None

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.consecutive_successes += 1
        if self.status in {"degraded", "open", "half_open"} and self.consecutive_successes >= self.recovery_successes:
            self.status = "closed"
            self._opened_at_monotonic = None

    def record_failure(self) -> None:
        self.consecutive_successes = 0
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            if self.status != "open":
                self._opened_at_monotonic = time.monotonic()
            self.status = "open"
        else:
            self.status = "degraded"

    def allow_request(self) -> bool:
        """Whether a request may be attempted now. A closed/degraded breaker always
        allows. An open breaker transitions to half-open (one trial allowed) once the
        recovery timeout has elapsed; the trial's record_success/record_failure then
        closes or re-opens it. This is what lets the source recover without a restart."""
        if self.status != "open":
            return True
        if self._opened_at_monotonic is None:
            return True
        if time.monotonic() - self._opened_at_monotonic >= self.recovery_timeout_seconds:
            self.status = "half_open"
            return True
        return False


class SourceRegistry:
    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._last_success: dict[str, str] = {}
        self._last_failure: dict[str, str] = {}
        self._messages: dict[str, str] = {}

    def register(self, source: Source) -> None:
        source_id = str(source.id).strip().lower()
        if not source_id:
            raise ValueError("source id is required")
        self._sources[source_id] = source
        self._breakers.setdefault(source_id, CircuitBreaker())

    def get(self, source_id: str) -> Source:
        normalized = str(source_id or "").strip().lower()
        if normalized not in self._sources:
            raise KeyError(f"unknown source: {source_id}")
        return self._sources[normalized]

    def supports(self, source_id: str, stream: Stream) -> bool:
        return stream in self.get(source_id).capabilities

    def sources_for(self, stream: Stream) -> list[Source]:
        return [source for source in self._sources.values() if stream in source.capabilities]

    def resolve(self, stream: Stream, priority: list[str] | None = None) -> Source:
        candidates = priority or list(self._sources)
        for source_id in candidates:
            normalized = str(source_id or "").strip().lower()
            source = self._sources.get(normalized)
            breaker = self._breakers.get(normalized)
            if source is not None and stream in source.capabilities and breaker is not None and breaker.allow_request():
                return source
        raise KeyError(f"no available source for stream: {stream.value}")

    def record_success(self, source_id: str) -> None:
        normalized = str(source_id or "").strip().lower()
        self._breakers.setdefault(normalized, CircuitBreaker()).record_success()
        self._last_success[normalized] = _now_iso()
        self._messages[normalized] = ""

    def record_failure(self, source_id: str, message: str = "") -> None:
        normalized = str(source_id or "").strip().lower()
        self._breakers.setdefault(normalized, CircuitBreaker()).record_failure()
        self._last_failure[normalized] = _now_iso()
        self._messages[normalized] = str(message or "")

    def source_ids(self) -> list[str]:
        """Every source with a registered adapter or a circuit breaker (a source
        that recorded a success or failure without registering)."""
        return sorted(set(self._sources) | set(self._breakers))

    def health(self, source_id: str) -> SourceHealth:
        normalized = str(source_id or "").strip().lower()
        if normalized not in self._sources and normalized not in self._breakers:
            raise KeyError(f"unknown source: {source_id}")
        source = self._sources.get(normalized)
        breaker = self._breakers.setdefault(normalized, CircuitBreaker())
        return SourceHealth(
            source=source.id if source is not None else normalized,
            status=breaker.status,
            consecutive_failures=breaker.consecutive_failures,
            last_success_at=self._last_success.get(normalized),
            last_failure_at=self._last_failure.get(normalized),
            message=self._messages.get(normalized, ""),
        )


_REGISTRY: SourceRegistry | None = None


def get_source_registry() -> SourceRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SourceRegistry()
    return _REGISTRY


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
