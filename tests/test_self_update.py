from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from forven import config as forven_config
from forven import self_update


def test_git_uses_configured_creation_flags(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(self_update.subprocess, "run", fake_run)

    result = self_update._git("rev-parse", "HEAD")

    assert result.stdout == "ok\n"
    assert captured["shell"] is False
    assert captured["creationflags"] == self_update._GIT_CREATION_FLAGS


def test_windows_git_creation_flags_hide_console():
    if self_update.os.name != "nt":
        return

    assert self_update._GIT_CREATION_FLAGS & getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
    )


# ---------------------------------------------------------------------------
# Channels and releases, against real throwaway git repos
# ---------------------------------------------------------------------------

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, name):
    (repo / f"{name}.txt").write_text(name, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", f"Add {name}")
    return _run(repo, "rev-parse", "HEAD")


def _release(repo, version, notes="New:\n- Something new", urgent=False):
    message = f"Forven {version}\nUrgent: {'yes' if urgent else 'no'}\n\n{notes}\n"
    _run(repo, "tag", "-a", f"v{version}", "-m", message)
    _run(repo, "push", "-q", "origin", "main", f"v{version}")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """An upstream repo (where releases are cut) and a user's clone of it."""
    for key, value in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n[tag]\n\tgpgSign = false\n", encoding="utf-8")
    remote = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(remote))
    upstream = tmp_path / "upstream"
    _run(tmp_path, "clone", "-q", str(remote), str(upstream))
    _commit(upstream, "first")
    _run(upstream, "push", "-q", "origin", "main")
    user = tmp_path / "user"
    _run(tmp_path, "clone", "-q", str(remote), str(user))

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(self_update, "_repo_root", lambda: user)
    monkeypatch.setattr(forven_config, "FORVEN_HOME", home)
    monkeypatch.delenv("FORVEN_UPDATE_CHANNEL", raising=False)
    monkeypatch.delenv("FORVEN_UPDATE_REMOTE", raising=False)
    monkeypatch.delenv("FORVEN_UPDATE_BRANCH", raising=False)
    monkeypatch.setattr(self_update, "write_restart_sentinel", lambda reason: "sentinel")
    return upstream, user, home


@pytestmark_git
def test_channel_defaults_to_stable_and_saves(repos):
    _, _, home = repos
    assert self_update.get_channel() == {"channel": "stable", "locked": False}
    assert self_update.set_channel("latest") == {"channel": "latest", "locked": False}
    assert json.loads((home / "update_channel.json").read_text(encoding="utf-8")) == {"channel": "latest"}
    with pytest.raises(ValueError):
        self_update.set_channel("nightly")


@pytestmark_git
def test_env_channel_wins_and_locks(repos, monkeypatch):
    self_update.set_channel("stable")
    monkeypatch.setenv("FORVEN_UPDATE_CHANNEL", "latest")
    assert self_update.get_channel() == {"channel": "latest", "locked": True}


@pytestmark_git
def test_stable_ignores_commits_until_a_release(repos):
    upstream, _, _ = repos
    _commit(upstream, "second")
    _run(upstream, "push", "-q", "origin", "main")

    status = self_update.check_for_update()

    assert status["channel"] == "stable"
    assert status["update_available"] is False
    assert status["reason"] == "No releases have been published yet."


@pytestmark_git
def test_stable_offers_release_with_notes_and_applies_it(repos):
    upstream, user, _ = repos
    _commit(upstream, "second")
    _release(upstream, "0.2.0", notes="New:\n- Weekly releases\nFixes:\n- Banner spam")
    _commit(upstream, "third")
    _run(upstream, "push", "-q", "origin", "main")

    status = self_update.check_for_update()

    assert status["update_available"] is True
    assert status["can_apply"] is True
    assert status["target_version"] == "0.2.0"
    assert status["urgent"] is False
    latest = status["latest_release"]
    assert latest["title"] == "Forven 0.2.0"
    assert latest["sections"] == [
        {"title": "New", "items": ["Weekly releases"]},
        {"title": "Fixes", "items": ["Banner spam"]},
    ]

    result = self_update.apply_update()

    assert result["status"] == "updated"
    assert result["restart_pending"] is True
    assert result["to_version"] == "0.2.0"
    # Stops at the release, not at the newer unreleased commit.
    assert _run(user, "rev-parse", "HEAD") == _run(upstream, "rev-parse", "v0.2.0^{commit}")
    after = self_update.check_for_update(fetch=False)
    assert after["current_version"] == "0.2.0"
    assert after["update_available"] is False


@pytestmark_git
def test_urgent_flag_carries_through_skipped_releases(repos):
    upstream, _, _ = repos
    _commit(upstream, "fix")
    _release(upstream, "0.2.1", notes="Safety:\n- Stop a bad order", urgent=True)
    _commit(upstream, "more")
    _release(upstream, "0.3.0")

    status = self_update.check_for_update()

    assert status["target_version"] == "0.3.0"
    assert [r["version"] for r in status["pending_releases"]] == ["0.3.0", "0.2.1"]
    assert status["urgent"] is True


@pytestmark_git
def test_latest_channel_offers_every_commit(repos):
    upstream, user, _ = repos
    self_update.set_channel("latest")
    _commit(upstream, "second")
    _commit(upstream, "third")
    _run(upstream, "push", "-q", "origin", "main")

    status = self_update.check_for_update()

    assert status["update_available"] is True
    assert status["behind"] == 2
    assert status["pending_commits"] == ["Add third", "Add second"]
    assert self_update.apply_update()["status"] == "updated"
    assert _run(user, "rev-parse", "HEAD") == _run(upstream, "rev-parse", "HEAD")


@pytestmark_git
def test_version_shows_commits_past_release(repos):
    upstream, user, _ = repos
    _release(upstream, "0.2.0")
    _commit(upstream, "after")
    _run(upstream, "push", "-q", "origin", "main")
    _run(user, "pull", "-q", "--ff-only", "origin", "main")

    status = self_update.check_for_update()

    assert status["current_version"] == "0.2.0"
    assert status["commits_past_release"] == 1
    assert status["update_available"] is False


@pytestmark_git
def test_untracked_files_do_not_block_but_edits_do(repos):
    upstream, user, _ = repos
    _commit(upstream, "second")
    _release(upstream, "0.2.0")
    (user / "my-notes.md").write_text("mine", encoding="utf-8")

    assert self_update.check_for_update()["can_apply"] is True

    (user / "first.txt").write_text("edited", encoding="utf-8")
    status = self_update.check_for_update(fetch=False)
    assert status["can_apply"] is False
    assert "uncommitted changes" in status["blocked_reason"]
    assert self_update.apply_update(fetch=False)["status"] == "blocked"


def test_parse_release_notes_handles_plain_messages():
    notes = self_update.parse_release_notes("Quick fix for the banner\n\nFixed the thing.\n")
    assert notes == {
        "title": "Quick fix for the banner",
        "urgent": False,
        "sections": [{"title": "", "items": ["Fixed the thing."]}],
    }


def test_parse_release_notes_reads_markdown_headings():
    notes = self_update.parse_release_notes("Forven 0.4.0\nUrgent: yes\n\n## Safety\n- Cap order size\n* Halt on bad data\n")
    assert notes == {
        "title": "Forven 0.4.0",
        "urgent": True,
        "sections": [{"title": "Safety", "items": ["Cap order size", "Halt on bad data"]}],
    }
