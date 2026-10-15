"""The Settings page's number-field ranges must match the backend rails.

frontend/src/lib/settings/bounds.ts marks some ranges as enforced; for the risk
section those come from settings_apply._SETTINGS_SECTION_NUMERIC_BOUNDS, which
refuses an out-of-range save. A page that shows a different range than the one
the save enforces would mislead the operator, so pin them together.
"""
from __future__ import annotations

import re
from pathlib import Path

from forven.dataeng import collector
from forven.settings_apply import _SETTINGS_SECTION_NUMERIC_BOUNDS

BOUNDS_TS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "settings" / "bounds.ts"
_ROW = re.compile(r"^\s*'([^']+)':\s*\[([^,\]]+),\s*([^,\]]+),\s*([^,\]]+)(?:,\s*(E))?\]", re.M)


def _frontend_specs() -> dict[str, tuple[float | None, float | None, bool]]:
    out: dict[str, tuple[float | None, float | None, bool]] = {}
    for key, lo, hi, _step, enforced in _ROW.findall(BOUNDS_TS.read_text(encoding="utf-8")):
        def num(raw: str) -> float | None:
            raw = raw.strip()
            return None if raw == "null" else float(raw)
        out[key] = (num(lo), num(hi), bool(enforced))
    return out


def test_bounds_file_parses() -> None:
    assert len(_frontend_specs()) > 100


def test_risk_rails_match_backend() -> None:
    specs = _frontend_specs()
    for section, bounds in _SETTINGS_SECTION_NUMERIC_BOUNDS.items():
        for key, (low, high) in bounds.items():
            field_id = f"{section}.{key}"
            if field_id not in specs:
                continue  # not every rail has a Settings field
            lo, hi, enforced = specs[field_id]
            assert enforced, f"{field_id} is a backend rail; mark it enforced"
            assert (lo, hi) == (low, high), f"{field_id}: page says {lo}..{hi}, backend {low}..{high}"


def test_collector_bounds_match_backend() -> None:
    specs = _frontend_specs()
    for key, (low, high) in collector._SETTINGS_BOUNDS.items():
        lo, hi, enforced = specs[f"data-collector.{key}"]
        assert enforced and (lo, hi) == (low, high), key
