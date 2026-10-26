from __future__ import annotations

import hashlib

import forven
from forven import frontend_deps


def _frontend(tmp_path, lock=b'{"lockfileVersion": 3}'):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package-lock.json").write_bytes(lock)
    return frontend


def test_missing_node_modules_needs_install(tmp_path):
    assert frontend_deps.needs_install(_frontend(tmp_path)) is True


def test_unstamped_node_modules_needs_one_install(tmp_path):
    frontend = _frontend(tmp_path)
    (frontend / "node_modules").mkdir()
    assert frontend_deps.needs_install(frontend) is True


def test_stamp_matching_the_lockfile_is_up_to_date_until_the_lockfile_changes(tmp_path):
    frontend = _frontend(tmp_path)
    (frontend / "node_modules").mkdir()
    lock = (frontend / "package-lock.json").read_bytes()
    (frontend / "node_modules" / ".forven-lock.sha256").write_text(hashlib.sha256(lock).hexdigest() + "\n")
    assert frontend_deps.needs_install(frontend) is False

    (frontend / "package-lock.json").write_bytes(b'{"lockfileVersion": 3, "packages": {"x": {}}}')
    assert frontend_deps.needs_install(frontend) is True


def test_install_records_the_lockfile_hash(tmp_path, monkeypatch):
    frontend = _frontend(tmp_path)
    calls = []

    def fake_call(cmd, cwd, timeout, creationflags):
        calls.append((cmd[1:], cwd))
        (frontend / "node_modules").mkdir(exist_ok=True)
        return 0

    monkeypatch.setattr(frontend_deps, "_npm", lambda: "npm")
    monkeypatch.setattr(frontend_deps.subprocess, "call", fake_call)

    assert frontend_deps.install(frontend) == 0
    assert calls == [(["install", "--no-audit", "--no-fund"], str(frontend))]
    assert frontend_deps.needs_install(frontend) is False


def test_install_without_npm_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(frontend_deps, "_npm", lambda: None)
    assert frontend_deps.install(_frontend(tmp_path)) == 1


def test_archive_version_reads_a_filled_in_release(tmp_path):
    stamp = tmp_path / "_release_version.txt"
    stamp.write_text("v0.3.0\n", encoding="utf-8")
    assert forven._archive_version(stamp) == "0.3.0"
    stamp.write_text("v0.3.0-2-gabc1234\n", encoding="utf-8")
    assert forven._archive_version(stamp) == "0.3.0-2-gabc1234"


def test_archive_version_ignores_the_checkout_placeholder(tmp_path):
    stamp = tmp_path / "_release_version.txt"
    stamp.write_text("$Format:%(describe:tags=true,match=v[0-9]*)$\n", encoding="utf-8")
    assert forven._archive_version(stamp) is None
    assert forven._archive_version(tmp_path / "missing.txt") is None
