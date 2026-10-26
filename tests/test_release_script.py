from __future__ import annotations

import importlib.util
from pathlib import Path

from forven import self_update

_SPEC = importlib.util.spec_from_file_location(
    "release_script", Path(__file__).resolve().parents[1] / "scripts" / "release.py"
)
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


def test_next_version_bumps_minor_weekly_and_patch_for_hotfixes():
    assert release.next_version(None, "weekly") == "0.2.0"
    assert release.next_version((0, 2, 0), "weekly") == "0.3.0"
    assert release.next_version((0, 2, 3), "weekly") == "0.3.0"
    assert release.next_version((0, 2, 0), "hotfix") == "0.2.1"


def test_titles_sort_into_new_fixes_and_safety():
    grouped = release.group_titles(
        [
            "Offer weekly releases instead of every merge, with a new update UI",
            "Stop archiving strategies another check still owns",
            "Let probation decide demotion; judge live strategies on live trades",
            "Fix the Windows launcher path.",
        ]
    )
    assert grouped == {
        "New": ["Offer weekly releases instead of every merge, with a new update UI"],
        "Fixes": ["Stop archiving strategies another check still owns", "Fix the Windows launcher path"],
        "Safety": ["Let probation decide demotion; judge live strategies on live trades"],
    }


def test_tag_message_round_trips_through_the_app_parser():
    grouped = {"New": ["Weekly releases"], "Fixes": ["Banner spam"], "Safety": []}
    message = release.tag_message("0.3.0", grouped, urgent=True)

    notes = self_update.parse_release_notes(message)

    assert notes == {
        "title": "Forven 0.3.0",
        "urgent": True,
        "sections": [
            {"title": "New", "items": ["Weekly releases"]},
            {"title": "Fixes", "items": ["Banner spam"]},
        ],
    }
    # No "#" lines: git would strip them from a tag message.
    assert not any(line.startswith("#") for line in message.splitlines())


def test_pr_numbers_and_titles_from_merge_and_squash_subjects():
    assert release.pr_number("Merge pull request #267 from judder659/claude/x") == 267
    assert release.pr_number("Show what thin-edge paper strategies wait on (#278)") == 278
    assert release.pr_number("Claude Code connect: store nothing in auth.json") is None
    assert release.squash_title("Show what thin-edge paper strategies wait on (#278)") == (
        "Show what thin-edge paper strategies wait on"
    )


def test_discord_payload_is_one_embed_within_limits():
    grouped = {"New": [f"Change number {i} " + "x" * 80 for i in range(80)], "Fixes": [], "Safety": []}

    payload = release.discord_payload("0.3.0", grouped, urgent=False, url="https://example.invalid/r")

    (embed,) = payload["embeds"]
    assert embed["title"] == "Forven 0.3.0"
    assert len(embed["description"]) <= 4096
    assert embed["description"].endswith("and more in the full notes")


def test_hand_written_notes_file_is_parsed_by_section():
    text = "# comment\n\nNew:\n- A thing\n* Another\nFixes:\n- A fix\nSafety:\n\nstray line\n"

    grouped = release.parse_notes_file(text)

    assert grouped == {"New": ["A thing", "Another"], "Fixes": ["A fix"], "Safety": []}


def test_committed_release_notes_files_parse_cleanly():
    notes_dir = Path(__file__).resolve().parents[1] / "docs" / "release-notes"
    for path in sorted(notes_dir.glob("v*.md")):
        grouped = release.parse_notes_file(path.read_text(encoding="utf-8"))
        assert sum(len(items) for items in grouped.values()) > 0, path.name
        notes = self_update.parse_release_notes(release.tag_message(path.stem[1:], grouped, urgent=False))
        assert [s["title"] for s in notes["sections"]] == [k for k in release.SECTIONS if grouped[k]]
