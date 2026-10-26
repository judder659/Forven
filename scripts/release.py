"""Cut a Forven release: tag a tested commit of main, publish notes, post to Discord.

Run by .github/workflows/release.yml (weekly on Monday, or by hand for an
urgent hotfix). Standard library only, so it needs no install step.

A release is an annotated ``vX.Y.Z`` tag whose message carries the notes the
in-app updater shows (see ``forven.self_update.parse_release_notes``)::

    Forven 0.3.0
    Urgent: no

    New:
    - Strategies show what they wait on
    Fixes:
    - ...

Weekly releases bump the minor version and only tag a commit that passed both
the normal CI and the nightly Windows full suite. Hotfixes bump the patch
version and need only the normal CI.

Notes come from, in order: a hand-written ``docs/release-notes/vX.Y.Z.md``;
notes an AI model writes from the merged PRs when RELEASE_NOTES_API_KEY is set
(any OpenAI-compatible endpoint, OpenRouter by default); or the PR titles.

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY, and optionally
DISCORD_RELEASE_WEBHOOK, RELEASE_NOTES_API_KEY, RELEASE_NOTES_BASE_URL and
RELEASE_NOTES_MODEL. ``--dry-run`` prints what it would do.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MERGE_RE = re.compile(r"^Merge pull request #(\d+) from ")
SQUASH_RE = re.compile(r"^(?P<title>.+?) \(#(?P<number>\d+)\)$")

# How a PR title is sorted into the notes. Order matters: Safety wins.
SAFETY_WORDS = re.compile(
    r"\b(live|mainnet|real money|risk|halt|kill switch|orders|probation|security|secret|leak)\b",
    re.IGNORECASE,
)
FIX_WORDS = re.compile(
    r"^(fix|fixes|fixed|stop|stops|keep|keeps|don't|do not|repair|restore|handle|guard|prevent|correct)\b",
    re.IGNORECASE,
)
SECTIONS = ("New", "Fixes", "Safety")

CI_WORKFLOW = "ci.yml"
WINDOWS_WORKFLOW = "windows-full.yml"
DISCORD_DESCRIPTION_LIMIT = 4000

NOTES_BASE_URL = "https://openrouter.ai/api/v1"
NOTES_MODEL = "anthropic/claude-sonnet-4.5"
NOTES_TIMEOUT_SECONDS = 180
NOTES_BODY_LIMIT = 1200
NOTES_MAX_ITEMS = 20
NOTES_ITEM_LIMIT = 300
NOTES_DIR = os.path.join("docs", "release-notes")
NOTES_INSTRUCTIONS = """\
You write the release notes for Forven, a self-hosted crypto trading research app.
Its users are traders, not developers. Below are the pull requests merged since the
last release. Write notes in this exact format and nothing else:

New:
- one line per change
Fixes:
- one line per change
Safety:
- one line per change

Rules:
- Plain words a trader understands. Say what changes for the user, not how the code changed.
- Merge related PRs into one line. Leave out PRs users would never notice (tests, CI,
  refactors, docs, internal tooling) unless they change what users see.
- Safety is for anything touching real money, live trading, risk limits, keys or security.
- Use only facts stated in the PRs. Never invent features, numbers or settings.
- At most {max_items} lines in total, each under {item_limit} characters. Omit an empty section.
"""


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def parse_version(tag: str) -> tuple[int, int, int] | None:
    match = TAG_RE.match(tag.strip())
    return tuple(int(n) for n in match.groups()) if match else None  # type: ignore[return-value]


def next_version(last: tuple[int, int, int] | None, kind: str) -> str:
    """Weekly releases bump the minor version, hotfixes the patch."""
    if last is None:
        return "0.2.0"
    major, minor, patch = last
    if kind == "hotfix":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


def classify(title: str) -> str:
    if SAFETY_WORDS.search(title):
        return "Safety"
    if FIX_WORDS.search(title):
        return "Fixes"
    return "New"


def group_titles(titles: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {name: [] for name in SECTIONS}
    for title in titles:
        clean = title.strip().rstrip(".")
        if clean:
            grouped[classify(clean)].append(clean)
    return grouped


def tag_message(version: str, grouped: dict[str, list[str]], urgent: bool) -> str:
    lines = [f"Forven {version}", f"Urgent: {'yes' if urgent else 'no'}", ""]
    for name in SECTIONS:
        if grouped.get(name):
            lines.append(f"{name}:")
            lines.extend(f"- {item}" for item in grouped[name])
    return "\n".join(lines).rstrip() + "\n"


def release_markdown(version: str, grouped: dict[str, list[str]], urgent: bool) -> str:
    lines: list[str] = []
    if urgent:
        lines += ["**Urgent update.** Install it as soon as you can.", ""]
    for name in SECTIONS:
        if grouped.get(name):
            lines.append(f"### {name}")
            lines.extend(f"- {item}" for item in grouped[name])
            lines.append("")
    lines.append("Update from the pill in Forven's top bar, or Settings > System > Updates.")
    return "\n".join(lines)


def discord_payload(version: str, grouped: dict[str, list[str]], urgent: bool, url: str) -> dict[str, Any]:
    parts: list[str] = []
    for name in SECTIONS:
        if grouped.get(name):
            parts.append(f"**{name}**\n" + "\n".join(f"• {item}" for item in grouped[name]))
    description = "\n\n".join(parts) or "Maintenance release."
    if len(description) > DISCORD_DESCRIPTION_LIMIT:
        description = description[: DISCORD_DESCRIPTION_LIMIT - 30].rsplit("\n", 1)[0] + "\n… and more in the full notes"
    return {
        "username": "Forven releases",
        "embeds": [
            {
                "title": f"{'Urgent update: ' if urgent else ''}Forven {version}",
                "url": url,
                "description": description,
                "color": 0xF59E0B if urgent else 0x34D399,
                "footer": {"text": "Update from the pill in Forven's top bar"},
            }
        ],
    }


def parse_notes_file(text: str) -> dict[str, list[str]]:
    """Read hand-written notes: ``New:`` / ``Fixes:`` / ``Safety:`` headers, ``- `` items."""
    grouped: dict[str, list[str]] = {name: [] for name in SECTIONS}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = line.rstrip(":").strip().title()
        if line.endswith(":") and header in grouped:
            current = header
        elif line[:1] in {"-", "*"} and current is not None:
            item = line[1:].strip()
            if item:
                grouped[current].append(item)
    return grouped


def notes_prompt(prs: list[tuple[str, str]], example: str | None) -> str:
    parts = [NOTES_INSTRUCTIONS.format(max_items=NOTES_MAX_ITEMS, item_limit=NOTES_ITEM_LIMIT)]
    if example:
        parts.append("Notes from an earlier release, for tone and length:\n\n" + example.strip())
    parts.append("Merged pull requests:")
    for title, body in prs:
        body = body.split("\n---\n")[0].strip()[:NOTES_BODY_LIMIT]
        parts.append(f"## {title}\n{body}".rstrip())
    return "\n\n".join(parts)


def accept_ai_notes(text: str) -> dict[str, list[str]] | None:
    """Parse a model's notes; None unless they are well formed and within limits."""
    grouped = parse_notes_file(text)
    items = [item for name in SECTIONS for item in grouped[name]]
    if not items or len(items) > NOTES_MAX_ITEMS:
        return None
    if any(len(item) > NOTES_ITEM_LIMIT for item in items):
        return None
    return grouped


def pr_number(subject: str) -> int | None:
    for regex, group in ((MERGE_RE, 1), (SQUASH_RE, "number")):
        match = regex.match(subject)
        if match:
            return int(match.group(group))
    return None


def squash_title(subject: str) -> str:
    match = SQUASH_RE.match(subject)
    return match.group("title") if match else subject


# ---------------------------------------------------------------------------
# git + GitHub
# ---------------------------------------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def is_ancestor(older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], capture_output=True).returncode == 0


def last_release() -> tuple[str, tuple[int, int, int]] | None:
    tags = [(t, parse_version(t)) for t in git("tag", "--list", "v*").splitlines()]
    tags = [(t, v) for t, v in tags if v is not None]
    if not tags:
        return None
    return max(tags, key=lambda item: item[1])  # type: ignore[arg-type,return-value]


def github(path: str, method: str = "GET", body: dict | None = None) -> Any:
    repo = os.environ["GITHUB_REPOSITORY"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"null")


def green_shas(workflow: str, event: str | None = None, limit: int = 30) -> list[str]:
    """Head commits of the workflow's successful runs on main, newest first."""
    query = f"?branch=main&status=success&per_page={limit}"
    if event:
        query += f"&event={event}"
    runs = github(f"/actions/workflows/{workflow}/runs{query}").get("workflow_runs", [])
    return [run["head_sha"] for run in runs]


def pick_commit(kind: str, after: str | None) -> str | None:
    """Newest main commit that passed the checks this kind of release needs."""
    ci_green = set(green_shas(CI_WORKFLOW, event="push", limit=60))
    if kind == "hotfix":
        candidates = [sha for sha in git("rev-list", "--first-parent", "-60", "origin/main").splitlines()]
        candidates = [sha for sha in candidates if sha in ci_green]
    else:
        candidates = [sha for sha in green_shas(WINDOWS_WORKFLOW, limit=10) if sha in ci_green]
    for sha in candidates:
        if after is None or (sha != git("rev-parse", f"{after}^{{commit}}") and not is_ancestor(sha, after)):
            return sha
    return None


def merged_prs(since: str | None, until: str) -> list[tuple[str, str]]:
    """(title, description) of each change merged into main since the last release."""
    if since:
        subjects = git("log", "--first-parent", "--format=%s", f"{since}..{until}").splitlines()
    else:
        # The first release: cover the last week rather than all history.
        subjects = git("log", "--first-parent", "--since=7 days ago", "--format=%s", until).splitlines()
    prs: list[tuple[str, str]] = []
    for subject in subjects:
        number = pr_number(subject)
        if number is not None:
            try:
                pull = github(f"/pulls/{number}")
                prs.append((pull["title"], pull.get("body") or ""))
                continue
            except (urllib.error.URLError, KeyError):
                pass
        prs.append((squash_title(subject), ""))
    return prs


def latest_notes_example() -> str | None:
    """The newest hand-written notes file, shown to the model as a style example."""
    if not os.path.isdir(NOTES_DIR):
        return None
    files = [(parse_version(name[:-3]), name) for name in os.listdir(NOTES_DIR) if name.endswith(".md")]
    files = [(v, name) for v, name in files if v is not None]
    if not files:
        return None
    with open(os.path.join(NOTES_DIR, max(files)[1]), encoding="utf-8") as fh:
        return fh.read()


def ai_notes(prs: list[tuple[str, str]]) -> dict[str, list[str]] | None:
    """Ask an OpenAI-compatible model for plain-English notes; None when unset or unusable."""
    key = os.environ.get("RELEASE_NOTES_API_KEY", "").strip()
    if not key:
        return None
    base_url = (os.environ.get("RELEASE_NOTES_BASE_URL") or NOTES_BASE_URL).strip().rstrip("/")
    model = (os.environ.get("RELEASE_NOTES_MODEL") or NOTES_MODEL).strip()
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        method="POST",
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": notes_prompt(prs, latest_notes_example())}],
                "temperature": 0.2,
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "forven-release",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=NOTES_TIMEOUT_SECONDS) as response:
            text = json.loads(response.read())["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"::warning::AI release notes failed ({model}): {exc}; using PR titles.")
        return None
    grouped = accept_ai_notes(text or "")
    if grouped is None:
        print(f"::warning::AI release notes from {model} were not usable; using PR titles.")
    return grouped


def post_discord(webhook: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        webhook,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "forven-release"},
    )
    urllib.request.urlopen(request, timeout=30).close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", choices=("weekly", "hotfix"), default="weekly")
    parser.add_argument("--urgent", action="store_true", help="Mark the release urgent (hotfixes)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    git("fetch", "--quiet", "--tags", "origin", "main")
    previous = last_release()
    previous_tag = previous[0] if previous else None

    sha = pick_commit(args.kind, previous_tag)
    if sha is None:
        print("No new commit has passed the required checks since the last release; nothing to release.")
        return 0

    prs = merged_prs(previous_tag, sha)
    if not prs:
        print("Nothing merged since the last release; skipping.")
        return 0

    version = next_version(previous[1] if previous else None, args.kind)
    urgent = args.urgent
    # A hand-written docs/release-notes/vX.Y.Z.md, when one is committed for
    # this version, replaces the AI-written or PR-title notes.
    notes_file = os.path.join(NOTES_DIR, f"v{version}.md")
    if os.path.isfile(notes_file):
        with open(notes_file, encoding="utf-8") as fh:
            grouped = parse_notes_file(fh.read())
        print(f"Using hand-written notes from {notes_file}")
    elif (written := ai_notes(prs)) is not None:
        grouped = written
        print("Using notes written by the AI model from the merged PRs")
    else:
        grouped = group_titles([title for title, _ in prs])
    message = tag_message(version, grouped, urgent)
    print(f"Releasing v{version} at {sha[:9]} (previous: {previous_tag or 'none'})\n")
    print(message)
    if args.dry_run:
        return 0

    git("tag", "-a", f"v{version}", sha, "-m", message)
    git("push", "origin", f"v{version}")
    release = github(
        "/releases",
        method="POST",
        body={
            "tag_name": f"v{version}",
            "name": f"Forven {version}{' (urgent)' if urgent else ''}",
            "body": release_markdown(version, grouped, urgent),
        },
    )
    print(f"Published {release.get('html_url')}")

    webhook = os.environ.get("DISCORD_RELEASE_WEBHOOK", "").strip()
    if webhook:
        try:
            post_discord(webhook, discord_payload(version, grouped, urgent, release.get("html_url", "")))
            print("Posted to Discord.")
        except urllib.error.URLError as exc:
            # The release is out; a missed Discord post shouldn't fail the run.
            print(f"::warning::Discord post failed: {exc}")
    else:
        print("DISCORD_RELEASE_WEBHOOK not set; skipped the Discord post.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
