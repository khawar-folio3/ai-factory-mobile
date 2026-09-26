from __future__ import annotations

from pathlib import Path

import pytest

from mobile_factory.config import ProjectConfig
from mobile_factory.globs import glob_to_re, matches
from mobile_factory.guardrail import diff as difflib
from mobile_factory.guardrail import limits
from mobile_factory.guardrail import rules as rl

PATCH = """diff --git a/app/src/main/java/ui/ProfileViewModel.kt b/app/src/main/java/ui/ProfileViewModel.kt
--- a/app/src/main/java/ui/ProfileViewModel.kt
+++ b/app/src/main/java/ui/ProfileViewModel.kt
@@ -10,3 +10,6 @@ class ProfileViewModel {
     val a = 1
+    // Step 1: load the user
+    val user = repo.user()!!
+    var isLoading: Boolean = false
     val b = 2
-    val old = 3
diff --git a/local.properties b/local.properties
--- a/local.properties
+++ b/local.properties
@@ -1 +1 @@
-sdk.dir=/a
+sdk.dir=/b
"""

TASTE = """# taste
### R001 · Prefer sealed UI state over boolean flags   [major]
applies: **/*ViewModel.kt
keywords: isLoading|Boolean
why: exhaustive when.
evidence: 3 comments · PRs #1 #2

### R002 · Keep network out of UI   [blocker]
applies: **/ui/**/*.kt
keywords: Retrofit
why: testability.

### R003 · Small PRs   [nit]
applies: **
why: scope.

## Accepted exceptions
- debug screens.
"""


@pytest.mark.parametrize(
    ("glob", "path", "ok"),
    [
        ("**/build.gradle*", "build.gradle.kts", True),
        ("**/build.gradle*", "app/build.gradle", True),
        ("gradle/**", "gradle/libs.versions.toml", True),
        ("*.kt", "app/A.kt", False),
        ("**/ui/**/*.kt", "app/src/ui/x/A.kt", True),
        ("**/ui/**/*.kt", "app/src/ui/A.kt", True),
    ],
)
def test_globs(glob: str, path: str, ok: bool) -> None:
    assert bool(glob_to_re(glob).match(path)) is ok


def test_diff_parse_tracks_new_line_numbers() -> None:
    files = difflib.parse(PATCH)
    vm = files[0]
    assert [n for n, _ in vm.added] == [11, 12, 13]
    assert vm.removed == ["    val old = 3"]


def test_secret_files_are_stripped() -> None:
    clean, skipped = difflib.strip_secrets(PATCH)
    assert skipped == ["local.properties"] and "sdk.dir" not in clean


def test_rules_select_by_glob_and_keyword() -> None:
    rules, tails = rl.parse_rules(TASTE, "taste.md")
    assert [r.id for r in rules] == ["R001", "R002", "R003"]
    assert rules[0].keywords == "isLoading|Boolean" and "keywords" not in rules[0].body
    loaded, skipped = rl.select(rules, difflib.parse(PATCH))
    assert [r.id for r in loaded] == ["R001", "R003"]
    assert [r.id for r in skipped] == ["R002"]
    assert "Accepted exceptions" in tails


def test_slop_detectors_flag_added_lines_only() -> None:
    found = rl.detect(difflib.parse(PATCH), rl.slop_rules())
    assert {(f.rule, f.line) for f in found} == {("S001", 11), ("S003", 12)}
    assert all(f.source == "detector" for f in found)


def test_slop_override_disables_rule(tmp_path: Path) -> None:
    o = tmp_path / "slop.yaml"
    o.write_text("- id: S003\n  enabled: false\n")
    assert "S003" not in {r.id for r in rl.slop_rules(o)}


def test_verdict_and_reasons() -> None:
    ok = rl.Finding(file="a", rule="R1", severity="major", suggestion="x", outcome="applied")
    open_ = rl.Finding(file="a", rule="R1", severity="major", suggestion="x")
    nit = rl.Finding(file="a", rule="R1", severity="nit", suggestion="x")
    assert rl.verdict([ok, nit]) == "READY FOR HUMAN REVIEW"
    assert rl.verdict([ok, open_]) == "NEEDS WORK"
    with pytest.raises(ValueError, match="needs a reason"):
        rl.Finding(file="a", rule="R1", severity="major", suggestion="x", outcome="dismissed")


def test_limits() -> None:
    cfg = ProjectConfig(name="d")
    diffs = difflib.parse(PATCH.replace("var isLoading", '@Suppress("X") var isLoading'))
    rep = limits.check(cfg, ["app/build.gradle.kts", "app/A.kt"], diffs, ["app/src/test/ATest.kt"])
    assert rep.forbidden == ["app/build.gradle.kts"]
    assert len(rep.suppressions) == 1
    assert rep.deleted_tests == ["app/src/test/ATest.kt"]
    assert not rep.ok


def test_matches_ignores_blank_globs() -> None:
    assert not matches("a.kt", ["", "  "])
