from __future__ import annotations

import json
from pathlib import Path

import pytest

from mobile_factory.config import ProjectConfig
from mobile_factory.errors import FactoryError
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


def test_reviews_split_into_parallel_chunks(tmp_path: Path) -> None:
    from mobile_factory.guardrail import harvest

    data = tmp_path / ".factory/data"
    data.mkdir(parents=True)
    (data / "reviews.jsonl").write_text("{}\n" * 1000)
    assert harvest.chunks(data) == [(i, i + 124) for i in range(1, 1001, 125)]  # 8 subagents
    (data / "reviews.jsonl").write_text("{}\n" * 150)
    assert harvest.chunks(data) == [(1, 100), (101, 150)]  # never below 100 lines each
    plan = harvest.parallel_plan(data)
    assert f"factory-learn-tally  lines 101-150  ->  {data}/tally-2.json  (description: tally 2)" in plan


def test_harvest_skips_processed_prs_and_full_starts_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mobile_factory.guardrail import harvest

    listed = [
        {"number": n, "title": "t", "author": {"login": "a"}, "mergedAt": f"2026-09-{n:02d}T00:00:00Z"} for n in (1, 2)
    ]
    fetched: list[int] = []

    def comments(root: Path, repo: str, pr: dict[str, object]) -> list[dict[str, object]]:
        n = int(str(pr["number"]))
        fetched.append(n)
        return [
            {"id": f"c{n}", "pr": n, "reviewer": "owner", "kind": "review", "state": "COMMENTED", "created_at": str(n)}
        ]

    monkeypatch.setattr(harvest, "gh_json", lambda root, *args: listed)
    monkeypatch.setattr(harvest, "_pr_comments", comments)
    monkeypatch.setattr(harvest, "codeowners", lambda root: ["owner"])
    data = tmp_path / "data"
    run = lambda **kw: harvest.harvest(tmp_path, "o/r", data, min_prs=1, **kw)  # noqa: E731

    assert run()["prs_new"] == 2 and sorted(fetched) == [1, 2]
    listed.append({"number": 3, "title": "t", "author": {"login": "a"}, "mergedAt": "2026-09-03T00:00:00Z"})
    meta = run()
    assert sorted(fetched) == [1, 2, 3] and meta["prs_already_processed"] == 2 and meta["comments"] == 3  # only #3 read
    assert set(json.loads((data / "processed_prs.json").read_text())["prs"]) == {"1", "2", "3"}

    (data / "tally-1.json").write_text("{}")
    meta = run(full=True)
    assert (
        sorted(fetched[3:]) == [1, 2, 3]
        and meta["full"]
        and meta["comments"] == 3
        and not (data / "tally-1.json").exists()
    )

    with pytest.raises(FactoryError, match="only 0 PRs"):  # different owners: kept comments are useless, start over
        run(owners=["someone-else"])
    assert sorted(fetched[6:]) == [1, 2, 3] and json.loads((data / "harvest_meta.json").read_text())["full"]
