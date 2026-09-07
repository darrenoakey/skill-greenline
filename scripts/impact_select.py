#!/usr/bin/env python3
"""Select the test files a candidate can actually break.

DOCTRINE.md, "The release time budget": validation is chosen by change impact,
not by running an ever-growing product suite on every release. This script maps
the candidate's changed paths through an explicit ownership/dependency map and
prints the pytest targets to run, one per line. Empty output means the change
provably cannot break any test (documentation, images) and only lint applies.

It fails CLOSED: an unknown path, a missing base ref, or any git error selects
the entire suite. Adding a file without a mapping therefore over-tests rather
than under-tests, which is the only safe direction for a release gate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

# Paths whose change can break anything, so they select the whole suite.
GLOBAL_IMPACT = {"greenline", "run", "greenline.toml", "pytest.ini", "pyproject.toml"}

# Non-test sources that own a bounded slice of the suite.
OWNED_IMPACT = {
    "greenline-speedup": ("tests/test_greenline_speedup.py",),
    "scripts/greenline-wait.sh": ("tests/test_waiter.py",),
    "scripts/impact_select.py": ("tests/test_impact_select.py",),
    # Ignore rules decide which paths this selector ever sees, so they are part
    # of selection behaviour, not inert configuration.
    ".gitignore": ("tests/test_impact_select.py",),
}

# Test modules other test modules import: changing one re-tests its dependents.
TEST_HUBS = {
    "tests/test_greenline.py": (
        "tests/test_greenline.py",
        "tests/test_deadline_cleanup.py",
        "tests/test_greenline_speedup.py",
        "tests/test_waiter.py",
    ),
}

# Suffixes that cannot change behaviour under test.
INERT_SUFFIXES = {".md", ".jpg", ".jpeg", ".png"}
INERT_NAMES = {"LICENSE", ".publish_config"}


def all_tests() -> list[str]:
    return sorted(str(p.relative_to(ROOT)) for p in TESTS_DIR.glob("test_*.py"))


def changed_paths(base: str, head: str) -> list[str] | None:
    """Files this tree differs by, or None if git cannot tell us.

    Two sources, unioned: commits between base and head (what the gate sees in a
    candidate) and anything still dirty in the working tree (what a developer
    sees before committing). Missing the second would let a local `./run check`
    validate nothing at all.
    """
    paths: set[str] = set()
    try:
        committed = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "--name-only", f"{base}...{head}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    paths.update(line.strip() for line in committed.splitlines() if line.strip())
    for line in dirty.splitlines():
        entry = line[3:].strip()
        if not entry:
            continue
        # Renames report "old -> new"; both sides matter.
        paths.update(part.strip().strip('"') for part in entry.split(" -> "))
    return sorted(p for p in paths if p)


def select(paths: list[str]) -> list[str]:
    """Map changed paths to test targets, failing closed on anything unknown."""
    selected: set[str] = set()
    for path in paths:
        suffix = Path(path).suffix
        if path in GLOBAL_IMPACT:
            return all_tests()
        if path in OWNED_IMPACT:
            selected.update(OWNED_IMPACT[path])
            continue
        if path in TEST_HUBS:
            selected.update(TEST_HUBS[path])
            continue
        if path.startswith("tests/") and Path(path).name.startswith("test_"):
            selected.add(path)
            continue
        if suffix in INERT_SUFFIXES or Path(path).name in INERT_NAMES:
            continue
        if path.startswith("templates/"):
            continue
        # Unknown path: no proof it is harmless, so validate everything.
        return all_tests()
    existing = set(all_tests())
    return sorted(t for t in selected if t in existing)


def main(argv: list[str]) -> int:
    base = argv[0] if argv else "refs/greenline/last-green"
    head = argv[1] if len(argv) > 1 else "HEAD"
    paths = changed_paths(base, head)
    if paths is None:
        print("\n".join(all_tests()))
        return 0
    print("\n".join(select(paths)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
