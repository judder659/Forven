"""Agent write_file reliability pack (2026-07-06 reports).

Three failure modes reported by agents in one week:
1. Appends "returned success, file did not grow" — read_workspace resolves
   the LONGEST copy across canonical+legacy roots while appends hit each root
   independently (legacy failures swallowed); once diverged, appends landed on
   the shorter copy and reads returned the stale longer one.
2. ~4KB mid-sentence truncations — agent turns capped at 4096 output tokens;
   gateway-repaired JSON turned cut tool calls into valid-but-truncated
   content. The runner must refuse tool calls from truncated turns.
3. Bimodal [FAILED] ledger tags — the ledger classifier had no success
   marker for write_file, so EVERY successful write was stamped FAILED.
"""

from __future__ import annotations

from forven.agents.runner import _task_tool_output_shows_success
from forven.workspace import append_workspace, read_workspace


def test_append_lands_even_when_legacy_copy_was_longer(tmp_path, monkeypatch):
    import forven.workspace as ws

    canonical = tmp_path / "workspace"
    legacy = tmp_path / "legacy"
    canonical.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(ws, "WORKSPACE_DIR", canonical)
    monkeypatch.setattr(ws, "LEGACY_WORKSPACE_DIR", legacy)
    monkeypatch.setattr(ws, "ensure_dirs", lambda: None)

    # Diverged roots: legacy is LONGER (the exact trap — reads resolved to it)
    (canonical / "memory").mkdir()
    (legacy / "memory").mkdir()
    (canonical / "memory" / "2026-07-06.md").write_text("short canonical\n", encoding="utf-8")
    (legacy / "memory" / "2026-07-06.md").write_text(
        "much longer legacy copy with historical content\n", encoding="utf-8"
    )

    append_workspace("memory/2026-07-06.md", "## H01608 review entry\n")

    # The append must be visible through the SAME resolution the reader uses
    after = read_workspace("memory/2026-07-06.md")
    assert "## H01608 review entry" in after
    # and it based on the best (longest) copy, so nothing was lost
    assert "much longer legacy copy" in after
    # both roots converged to identical content
    a = (canonical / "memory" / "2026-07-06.md").read_text(encoding="utf-8")
    b = (legacy / "memory" / "2026-07-06.md").read_text(encoding="utf-8")
    assert a == b == after


def test_write_file_tool_verifies_and_reports_lengths(tmp_path, monkeypatch):
    import forven.workspace as ws

    canonical = tmp_path / "workspace"
    canonical.mkdir()
    monkeypatch.setattr(ws, "WORKSPACE_DIR", canonical)
    monkeypatch.setattr(ws, "LEGACY_WORKSPACE_DIR", canonical)
    monkeypatch.setattr(ws, "ensure_dirs", lambda: None)

    from forven.agents.tools_core import _tool_write_file

    out = _tool_write_file("notes/probe.md", "hello ledger\n", append=True)
    assert out.startswith("Appended 13 chars to notes/probe.md")
    assert "verified" in out

    out2 = _tool_write_file("notes/probe.md", "fresh content\n", append=False)
    assert out2.startswith("Wrote 14 chars to notes/probe.md")
    assert "verified" in out2


def test_ledger_classifies_write_file_outputs_correctly():
    ok = {"output_summary": "Appended 3500 chars to LESSONS.md (file now 91000 chars, verified)"}
    assert _task_tool_output_shows_success(ok) is True
    ok2 = {"output_summary": "Wrote 240 chars to notes/x.md (verified)"}
    assert _task_tool_output_shows_success(ok2) is True
    bad = {"output_summary": "Error: append to memory/x.md did not persist (post-write verification ...)"}
    assert _task_tool_output_shows_success(bad) is False
    denied = {"output_summary": "Error: SOUL.md is protected. Only the Brain can edit it."}
    assert _task_tool_output_shows_success(denied) is False


def test_truncated_turns_are_flagged_by_providers():
    from forven.agents.providers import ProviderResponse

    assert ProviderResponse().truncated is False
    assert ProviderResponse(truncated=True).truncated is True
