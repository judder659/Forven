"""The 2026_06_paper_book_go_live_stamp migration anchors the kernel paper-recording go-live
at upgrade time for EXISTING installs, so the first kernel scan after upgrading can't backfill
(flood) the whole pre-upgrade history into the book. It stamps the same KV key the operator
reset script writes, but only when unset, and is a no-op once stamped.
"""

from __future__ import annotations

import forven.migrations as m
import forven.scanner as sc
from forven.db import get_db, kv_get, kv_set
from forven.sim.clock import get_now


def _clear_reset_key():
    # init_db() runs the migration during fixture setup, so clear the stamp to exercise the
    # genuinely-unset (pre-migration) state.
    with get_db() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (sc.PAPER_BOOK_RESET_KV_KEY,))


def _seed_paper_strategy(sid="S-MIG-1"):
    # A promoted (paper-stage) strategy is the "book to protect" that arms the migration; an
    # existing paper trade would do equally. A fresh DB has neither, so the migration no-ops.
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO strategies (id, name, stage) VALUES (?, ?, 'paper')",
            (sid, sid),
        )


def test_reset_key_literal_in_sync_with_scanner():
    # The migration duplicates the literal to avoid importing the heavy scan stack at boot;
    # this guards the two from drifting apart.
    assert m._PAPER_BOOK_RESET_KV_KEY == sc.PAPER_BOOK_RESET_KV_KEY


def test_migration_registered():
    assert "2026_06_paper_book_go_live_stamp" in [mig.name for mig in m.MIGRATIONS]


def test_migration_noop_on_pristine_install(forven_db):
    """A fresh install (no paper trades, no promoted strategies) is left UNSTAMPED, so the
    migration never perturbs scanner behaviour on a clean DB (incl. every test fixture)."""
    _clear_reset_key()
    with get_db() as conn:
        m._m_2026_06_paper_book_go_live_stamp(conn)
    assert kv_get(sc.PAPER_BOOK_RESET_KV_KEY, None) is None


def test_migration_stamps_go_live_when_unset(forven_db):
    _clear_reset_key()
    _seed_paper_strategy()  # an existing paper-stage book to protect → arms the stamp
    assert kv_get(sc.PAPER_BOOK_RESET_KV_KEY, None) is None

    with get_db() as conn:
        m._m_2026_06_paper_book_go_live_stamp(conn)

    stamped = kv_get(sc.PAPER_BOOK_RESET_KV_KEY, None)
    assert stamped is not None
    assert sc._parse_iso_ts(stamped) is not None              # stored as a parseable ISO string
    # Resolves through the SAME path the scanner uses: with an OLD stage_changed_at, the fresh
    # reset stamp wins (max), so go-live = upgrade time (capped at now), NOT the old stage.
    go = sc._resolve_paper_go_live({"stage_changed_at": "2026-01-01T00:00:00+00:00"})
    assert go is not None
    assert go.isoformat() == sc._parse_iso_ts(stamped).isoformat()
    assert go <= get_now()


def test_migration_noop_when_already_stamped(forven_db):
    kv_set(sc.PAPER_BOOK_RESET_KV_KEY, "2099-01-01T00:00:00+00:00")  # operator/prior stamp
    with get_db() as conn:
        m._m_2026_06_paper_book_go_live_stamp(conn)
    assert kv_get(sc.PAPER_BOOK_RESET_KV_KEY, None) == "2099-01-01T00:00:00+00:00"  # untouched


def test_migration_idempotent_on_repeat(forven_db):
    _clear_reset_key()
    _seed_paper_strategy()
    with get_db() as conn:
        m._m_2026_06_paper_book_go_live_stamp(conn)
    first = kv_get(sc.PAPER_BOOK_RESET_KV_KEY, None)
    assert first is not None
    with get_db() as conn:
        m._m_2026_06_paper_book_go_live_stamp(conn)  # key now exists → no-op
    assert kv_get(sc.PAPER_BOOK_RESET_KV_KEY, None) == first  # unchanged, not re-stamped
