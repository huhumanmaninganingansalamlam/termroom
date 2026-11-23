"""The repository entry map must point to real feature owners and checks."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _linked_paths(document: Path) -> list[Path]:
    content = document.read_text(encoding="utf-8")
    paths = []
    for raw in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
        if raw.startswith(("https://", "http://", "mailto:", "#")):
            continue
        paths.append((document.parent / raw.split("#", 1)[0]).resolve())
    return paths


def test_agent_map_links_resolve_to_real_files() -> None:
    map_file = ROOT / "AGENTS.md"
    assert map_file.is_file()
    links = _linked_paths(map_file)
    assert len(links) >= 25
    for target in links:
        assert target.is_relative_to(ROOT), f"agent map escaped repository: {target}"
        assert target.is_file(), f"agent map has stale owner/test link: {target}"

    table = map_file.read_text().split("## Feature map", 1)[1]
    table = table.split("## Checks", 1)[0]
    entries = [
        line for line in table.splitlines()
        if line.startswith("| ") and line.count("|") >= 4
        and "Change" not in line and "---" not in line
    ]
    assert len(entries) >= 7
    for entry in entries:
        owner, tests = entry.split("|")[2:4]
        assert "/termroom/" not in owner, "feature map paths must be repo-relative"
        assert "](tests/test_" in tests, f"feature missing a direct test: {entry}"

    assert "docs/architecture.md" in map_file.read_text()
    assert "PTY write" in map_file.read_text()
    assert "idempotency" in map_file.read_text().lower()
    assert "CAS" in map_file.read_text()
    assert "scripts/check_architecture.py" in map_file.read_text()
    assert (ROOT / "scripts/check_architecture.py").is_file()


def test_public_entry_docs_point_to_same_feature_map() -> None:
    for name in ("README.md", "README.ko.md", "CONTRIBUTING.md"):
        assert "](AGENTS.md)" in (ROOT / name).read_text(encoding="utf-8")


def test_prototype_is_labeled_historical_and_no_second_task_system() -> None:
    prototype = ROOT / "docs/prototypes/terminal-first/README.md"
    assert "Historical synthetic review" in prototype.read_text(encoding="utf-8")
    assert (ROOT / "AGENTS.md") in _linked_paths(prototype)
    assert not (ROOT / ".github/ISSUE_TEMPLATE").exists()
