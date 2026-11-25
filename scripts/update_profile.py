#!/usr/bin/env python3
"""Update the profile's public GitHub snapshot; Python standard library only."""

from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

USER = "calavera16"
START = "<!-- analytics:start -->"
END = "<!-- analytics:end -->"
COLORS = ("#c88b2b", "#477f9e", "#7397b4", "#697887", "#b29b6c", "#89929b")


class GitHubError(RuntimeError):
    """A public GitHub read failed; keep the previous snapshot."""


class GitHub:
    def __init__(self, token: str = "") -> None:
        self.token = token

    def get(self, path: str, params: dict | None = None):
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "calavera16-profile",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            # GitHub returns 409 for a genuinely empty repository's commit list.
            try:
                message = json.loads(exc.read()).get("message", "")
            except (ValueError, AttributeError):
                message = ""
            if exc.code == 409 and path.endswith("/commits") and message == "Git Repository is empty.":
                return []
            raise GitHubError(f"GitHub returned HTTP {exc.code} for {path}") from None
        except (URLError, TimeoutError, ValueError) as exc:
            raise GitHubError(f"Could not read GitHub response for {path} ({type(exc).__name__})") from None


def pages(client, path: str, **params):
    page = 1
    while True:
        rows = client.get(path, {**params, "per_page": 100, "page": page})
        if not isinstance(rows, list):
            raise GitHubError(f"Expected a list from {path}")
        yield from rows
        if len(rows) < 100:
            return
        page += 1


def collect(client, today: dt.date | None = None, user: str = USER) -> dict:
    """Collect all reads before rendering/writing; inject a client/date for tests."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    first = today - dt.timedelta(days=27)
    end = today + dt.timedelta(days=1)
    days = {str(first + dt.timedelta(days=i)): 0 for i in range(28)}
    repositories = []
    languages: dict[str, int] = {}
    worked = 0
    for repo in pages(client, f"/users/{quote(user, safe='')}/repos", type="owner"):
        if (repo["owner"]["login"].lower() != user.lower() or repo["private"]
                or repo["fork"] or repo["name"].lower() == user.lower()):
            continue
        repositories.append(repo)
    for repo in repositories:
        path = f"/repos/{quote(user, safe='')}/{quote(repo['name'], safe='')}"
        data = client.get(path + "/languages")
        if not isinstance(data, dict) or any(
                not isinstance(k, str) or type(v) is not int or v < 0 for k, v in data.items()):
            raise GitHubError(f"Invalid language data from {path}")
        for language, size in data.items():
            languages[language] = languages.get(language, 0) + size
        seen = set()
        commits = 0
        for row in pages(client, path + "/commits", sha=repo["default_branch"], author=user,
                         since=f"{first}T00:00:00Z", until=f"{end}T00:00:00Z"):
            timestamp = dt.datetime.fromisoformat(row["commit"]["committer"]["date"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise GitHubError(f"Missing commit timezone from {path}")
            day = str(timestamp.astimezone(dt.timezone.utc).date())
            if day in days and row["sha"] not in seen:
                seen.add(row["sha"])
                days[day] += 1
                commits += 1
        worked += bool(commits)
    return {"today": str(today), "first": str(first), "days": days,
            "commits": sum(days.values()), "active_days": sum(v > 0 for v in days.values()),
            "worked": worked, "repositories": len(repositories), "languages": languages}


def language_segments(snapshot: dict) -> list[tuple[str, int]]:
    ordered = sorted(((k, v) for k, v in snapshot["languages"].items() if v), key=lambda x: (-x[1], x[0]))
    if len(ordered) > 5:
        return ordered[:5] + [("Other", sum(v for _, v in ordered[5:]))]
    return ordered


def svg_start(title: str, description: str, height: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="840" height="{height}" viewBox="0 0 840 {height}" role="img" '
            f'aria-labelledby="title desc"><title id="title">{html.escape(title)}</title>'
            f'<desc id="desc">{html.escape(description)}</desc>')


def render_activity(snapshot: dict) -> str:
    days = list(snapshot["days"].items())
    maximum = max(1, max(v for _, v in days))
    out = [svg_start("Public GitHub commits over 28 UTC days",
                     f"{snapshot['commits']} authored commits; {snapshot['active_days']} active days. "
                     f"{snapshot['first']} through {snapshot['today']}. Daily counts follow.", 148)]
    out.append(f'<text x="14" y="16" fill="#89929b" font-family="sans-serif" font-size="14">'
               f'Peak {max(v for _, v in days)} commits / day</text>')
    out.append('<path d="M14 111H826" stroke="#89929b" stroke-opacity=".45"/>')
    for i, (day, count) in enumerate(days):
        x = 17 + i * 29
        height = count / maximum * 76
        # Empty days are visible baseline ticks, not fabricated bars.
        out.append(f'<rect x="{x}" y="{110-height:.2f}" width="19" height="{max(height, 1):.2f}" '
                   f'fill="{COLORS[0] if count else COLORS[5]}" opacity="{1 if count else .4}">'
                   f'<title>{day}: {count} commits</title></rect>')
    out.append(f'<g fill="#89929b" font-family="sans-serif" font-size="14">'
               f'<text x="14" y="138">{snapshot["first"]}</text>'
               f'<text x="826" y="138" text-anchor="end">{snapshot["today"]} · UTC</text></g></svg>')
    return "".join(out) + "\n"


def percentage(value: int, total: int) -> str:
    return "<0.1%" if 0 < value / total < .001 else f"{value / total:.1%}"


def render_languages(snapshot: dict) -> str:
    segments = language_segments(snapshot)
    total = sum(v for _, v in segments)
    description = "; ".join(f"{name}: {percentage(value, total)}" for name, value in segments) if total else "No language bytes reported."
    out = [svg_start("Repository code composition by language bytes", description, 24)]
    x = 0.0
    for i, (name, value) in enumerate(segments):
        width = value / total * 840
        out.append(f'<rect x="{x:.3f}" y="4" width="{width:.3f}" height="16" fill="{COLORS[i]}">'
                   f'<title>{html.escape(name)}: {html.escape(percentage(value, total))}</title></rect>')
        x += width
    if not total:
        out.append('<path d="M0 12H840" stroke="#89929b" stroke-opacity=".45"/>')
    return "".join(out) + "</svg>\n"


def markdown_text(text: str) -> str:
    return html.escape(text).replace("[", "&#91;").replace("]", "&#93;").replace("|", "&#124;").replace("*", "&#42;").replace("_", "&#95;").replace("`", "&#96;")


def render_markdown(snapshot: dict) -> str:
    segments = language_segments(snapshot)
    total = sum(v for _, v in segments)
    legend = " · ".join(f"{markdown_text(name)} {html.escape(percentage(value, total))}" for name, value in segments) if total else "GitHub reports no language bytes for these repositories."
    return ("### Activity\n\n"
            f"**{snapshot['commits']}** authored commits across **{snapshot['worked']}** repositories "
            f"on **{snapshot['active_days']}** active days.\n\n"
            f"{snapshot['first']} – {snapshot['today']} · 28 UTC calendar days, including today.\n\n"
            "![Daily authored commits across public repositories over the last 28 UTC days](assets/activity.svg)\n\n"
            "### Languages in my repositories\n\n"
            "![Language byte distribution across eligible public repositories; percentages below](assets/languages.svg)\n\n"
            f"{legend}\n\n"
            f"Updated {snapshot['today']} UTC · Refresh scheduled twice hourly · "
            f"{snapshot['repositories']} public repositories, excluding forks and this profile.\n\n"
            "<details>\n<summary>How this is counted</summary>\n\n"
            "Commits match my GitHub author identity on each repository's default branch, grouped by commit timestamp. "
            "Language percentages show GitHub's code bytes across those same default branches.\n\n</details>\n")


def replace_section(readme: str, content: str) -> str:
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError("README must contain exactly one analytics marker pair")
    start = readme.index(START) + len(START)
    end = readme.index(END)
    if start > end:
        raise ValueError("README analytics markers are out of order")
    newline = "\r\n" if "\r\n" in readme else "\n"
    return readme[:start] + newline * 2 + content.replace("\n", newline) + newline + readme[end:]


def update(root: Path, client, today: dt.date | None = None) -> list[str]:
    readme_path = root / "README.md"
    original = readme_path.read_bytes().decode("utf-8")
    replace_section(original, "")  # Validate markers before any network reads or writes.
    snapshot = collect(client, today)
    outputs = {"README.md": replace_section(original, render_markdown(snapshot)),
               "assets/activity.svg": render_activity(snapshot),
               "assets/languages.svg": render_languages(snapshot)}
    # Catch concurrent human edits before replacing the generated section.
    if readme_path.read_bytes().decode("utf-8") != original:
        raise RuntimeError("README changed during collection; retry with the latest file")
    pending = []
    try:
        for name, content in outputs.items():
            target = root / name
            data = content.encode("utf-8")
            if target.exists() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                pending.append((Path(temp.name), target, name))
                temp.write(data)
        for temporary, target, _ in pending:
            os.replace(temporary, target)
    finally:
        for temporary, _, _ in pending:
            temporary.unlink(missing_ok=True)
    return [name for _, _, name in pending]


def main() -> int:
    try:
        changed = update(Path(__file__).resolve().parents[1], GitHub(os.environ.get("GH_TOKEN", "")))
        print("Updated: " + ", ".join(changed) if changed else "Snapshot unchanged")
        return 0
    except (GitHubError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"Profile update failed; no partial API snapshot published: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
