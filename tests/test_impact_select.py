"""Real tests for change-impact test selection.

No mocks: the pure mapping is exercised directly, and the CLI is driven as a
subprocess against this repository's real git history, exactly as ./run calls it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "impact_select.py"


def load_selector():
    spec = importlib.util.spec_from_file_location("impact_select_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["impact_select_mod"] = module
    spec.loader.exec_module(module)
    return module


def run_cli(*args) -> list[str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_every_mapped_target_exists():
    """A renamed or deleted test must not silently stop being selected."""
    mod = load_selector()
    known = set(mod.all_tests())
    assert known, "the suite must not be empty"
    for owner, targets in list(mod.OWNED_IMPACT.items()) + list(mod.TEST_HUBS.items()):
        for target in targets:
            assert target in known, f"{owner} maps to missing test {target}"
    for path in mod.GLOBAL_IMPACT:
        assert path in {"pytest.ini", "pyproject.toml"} or (ROOT / path).exists(), path


def test_core_script_selects_the_whole_suite():
    mod = load_selector()
    assert mod.select(["greenline"]) == mod.all_tests()
    assert mod.select(["run"]) == mod.all_tests()


def test_unknown_path_fails_closed_to_the_whole_suite():
    mod = load_selector()
    assert mod.select(["src/brand_new_module.py"]) == mod.all_tests()
    assert mod.select(["DOCTRINE.md", "unknown.cfg"]) == mod.all_tests()


def test_documentation_only_change_selects_nothing():
    mod = load_selector()
    assert mod.select(["DOCTRINE.md", "README.md", "banner.jpg"]) == []


def test_owned_script_selects_only_its_own_tests():
    mod = load_selector()
    assert mod.select(["greenline-speedup"]) == ["tests/test_greenline_speedup.py"]
    assert mod.select(["scripts/greenline-wait.sh"]) == ["tests/test_waiter.py"]
    assert mod.select([".gitignore"]) == ["tests/test_impact_select.py"]


def test_build_and_cache_droppings_are_ignored_by_git():
    """Warm caches live in the persistent gate worktree by design (DOCTRINE).

    If git reported them as untracked, every release would see unknown paths and
    fail closed into the whole suite, silently disabling impact selection.
    """
    for junk in ("__pycache__/x.pyc", ".pytest_cache/v/x", ".ruff_cache/x", "local/x"):
        checked = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", junk],
            capture_output=True,
        )
        assert checked.returncode == 0, f"{junk} must be gitignored"


def test_shared_test_helper_module_reselects_its_importers():
    """test_greenline.py exports the fixtures the other suites import."""
    mod = load_selector()
    selected = mod.select(["tests/test_greenline.py"])
    assert "tests/test_deadline_cleanup.py" in selected
    assert "tests/test_waiter.py" in selected
    assert "tests/test_greenline_speedup.py" in selected


def test_leaf_test_change_selects_only_itself():
    mod = load_selector()
    assert mod.select(["tests/test_waiter.py"]) == ["tests/test_waiter.py"]


def test_cli_reports_nothing_for_an_empty_diff():
    """With no committed range and a clean tree the selection is empty.

    Run against a scratch repo so a dirty working copy of greenline itself
    cannot make this test pass or fail by accident.
    """
    mod = load_selector()
    assert mod.select([]) == []


def test_cli_sees_uncommitted_work_in_this_tree():
    """Selection must cover dirty files, or a local check would validate nothing."""
    mod = load_selector()
    paths = mod.changed_paths("HEAD", "HEAD")
    assert paths is not None
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    expected = {line[3:].strip().split(" -> ")[-1].strip('"') for line in dirty.splitlines() if line[3:].strip()}
    assert expected.issubset(set(paths))


def test_cli_fails_closed_when_the_base_ref_is_missing():
    mod = load_selector()
    assert run_cli("refs/greenline/definitely-not-a-ref", "HEAD") == mod.all_tests()
