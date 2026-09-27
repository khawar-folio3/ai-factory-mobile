from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakePlatform, git, load

from mobile_factory.errors import FactoryError, Refused
from mobile_factory.pipeline import Engine

TRIAGE = {
    "verdict": "eligible",
    "reason": "clear steps, one screen",
    "summary": "Avatar clipped on profile",
    "category": "ui_spacing",
    "plan": ["wrap height"],
    "estimated_files": 1,
}
REPRO = {"reproduced": True, "confidence": 0.9, "steps": ["open Profile"], "snapshots": ["profile"]}
FIX = {
    "summary": "Wrap avatar height on profile",
    "root_cause": "fixed height",
    "changes": "Profile.kt: height 48 -> 64",
}
VERIFY = {"defect_fixed": True, "adjacent_unchanged": True, "snapshots": ["profile"]}


def to_fix(eng: Engine, fake: FakePlatform) -> None:
    eng.advance()
    assert eng.st.node == "triage" and eng.st.status == "waiting_agent"
    eng.submit("triage", TRIAGE)
    assert eng.st.node == "reproduce", eng.instructions()
    assert "ALONGSIDE start `factory-locate` (sonnet) in the SAME message" in eng.instructions()
    triage_done = eng.instructions()
    assert "SCOUT    first start `factory-scout` (haiku)" not in triage_done  # reproduce is not scouted
    fake.snapshot(eng.dir / "snapshots", "before", "profile")
    eng.submit("reproduce", REPRO)


def edit(repo: Path, body: str = "class Profile {\n    val height = 64\n}\n") -> None:
    (repo / "app/src/main/java/Profile.kt").write_text(body)


def verify(eng: Engine, fake: FakePlatform) -> None:
    fake.state = "activity  Main\nlabels    avatar-ok\n"
    fake.snapshot(eng.dir / "snapshots", "after", "profile")
    eng.submit("verify", VERIFY)


def test_full_autonomy_reaches_draft_pr(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    assert eng.st.node == "fix"
    edit(repo)
    eng.submit("fix", FIX)
    assert eng.st.node == "verify"
    verify(eng, fake)
    assert eng.st.node == "review"
    eng.submit("review", {"findings": []})

    assert eng.st.status == "done", eng.instructions()
    assert eng.st.outcome == "draft-pr"
    assert eng.st.pr_url.endswith("/pull/7")
    assert eng.st.level == 4
    assert all(g.decision == "auto" for g in eng.st.gates.values())
    assert git(repo, "log", "-1", "--format=%s") == "APP-1: Wrap avatar height on profile"
    assert git(repo, "rev-list", "--count", f"{eng.st.checkpoint}..HEAD") == "1"
    body = Path(eng.out("pr_preview")["file"]).read_text()
    assert "Root cause" in body and "fixed height" in body


def test_supervised_level_waits_at_diff_gate_and_checks_code(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=2), "APP-1")
    to_fix(eng, fake)
    assert eng.st.gates["plan"].decision == "auto"
    assert eng.st.gates["repro"].decision == "auto"
    edit(repo)
    eng.submit("fix", FIX)
    assert eng.st.status == "waiting_gate" and eng.node().gate == "diff"
    assert "WAITING ON A HUMAN" in eng.instructions()
    with pytest.raises(Refused):
        eng.decide("diff", True, by="me", code="nope")
    eng.decide("diff", True, by="me", code=eng.st.gates["diff"].code)
    assert eng.st.node == "verify"


def test_reject_stops_run(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=0), "APP-1")
    eng.advance()
    eng.submit("triage", TRIAGE)
    assert eng.node().gate == "plan" and eng.st.status == "waiting_gate"
    eng.decide("plan", False, by="me", reason="wrong screen")
    assert eng.st.status == "stopped" and eng.st.outcome == "rejected"


def test_failing_checks_retry_then_rollback(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    fake.fail_checks = True
    for _ in range(3):
        edit(repo)
        eng.submit("fix", FIX)
    assert eng.st.status == "stopped" and eng.st.outcome == "verify-failed"
    assert fake.check_calls == 3
    assert "val height = 48" in (repo / "app/src/main/java/Profile.kt").read_text()
    assert (eng.dir / "failed-attempt-3.patch").is_file()


def test_retry_shows_last_failure(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    fake.fail_checks = True
    edit(repo)
    eng.submit("fix", FIX)
    assert eng.st.node == "fix" and eng.st.fix_attempts == 1
    assert "LAST FAILURE" in eng.instructions()


def test_forbidden_path_stops(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    (repo / "app/build.gradle.kts").write_text("// changed\n")
    eng.submit("fix", FIX)
    assert eng.st.outcome == "forbidden-path"


def test_ineligible_triage_stops(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    eng.advance()
    eng.submit("triage", {**TRIAGE, "verdict": "needs-info", "questions": ["which device?"]})
    assert eng.st.outcome == "needs-info"
    assert (eng.dir / "questions.md").read_text().strip() == "- which device?"


def test_detector_findings_must_be_answered_and_review_loops(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    edit(repo, 'class Profile {\n    val height = 64\n    init { println("debug") }\n}\n')
    eng.submit("fix", FIX)
    verify(eng, fake)
    detector = eng.out("_review_ctx")["detector"]
    assert [d["rule"] for d in detector] == ["S004"]
    text = eng.instructions()
    assert "PARALLEL start ALL of these in one message" in text
    for part in ("review-correctness (opus)", "review-taste (sonnet)", "review-detectors (haiku)"):
        assert f"factory-{part}" in text
    assert "delegate to the `factory-review` subagent (opus)" in text
    with pytest.raises(FactoryError, match="detector findings without an outcome"):
        eng.submit("review", {"findings": []})

    edit(repo)
    finding = {**detector[0], "outcome": "applied"}
    eng.submit("review", {"findings": [finding]})
    assert eng.st.node == "verify" and eng.st.review_rounds == 1
    verify(eng, fake)
    assert eng.out("commit")["amended"] is True
    eng.submit("review", {"findings": []})
    assert eng.st.outcome == "draft-pr"
    assert git(repo, "rev-list", "--count", f"{eng.st.checkpoint}..HEAD") == "1"


def test_dismissed_major_forces_human_review_gate(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    edit(repo, 'class Profile {\n    val height = 64\n    init { println("debug") }\n}\n')
    eng.submit("fix", FIX)
    verify(eng, fake)
    d = eng.out("_review_ctx")["detector"][0]
    eng.submit("review", {"findings": [{**d, "outcome": "dismissed", "reason": "sample app output"}]})
    assert eng.st.status == "waiting_gate" and eng.node().gate == "review"


def test_publish_refuses_edited_preview(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=3), "APP-1")
    to_fix(eng, fake)
    edit(repo)
    eng.submit("fix", FIX)
    verify(eng, fake)
    eng.submit("review", {"findings": []})
    assert eng.node().gate == "pr" and eng.st.status == "waiting_gate"
    Path(eng.out("pr_preview")["file"]).write_text("tampered\n")
    eng.decide("pr", True, by="me", code=eng.st.gates["pr"].code)
    assert eng.st.outcome == "refused"
    assert "branch" not in fake.published  # type: ignore[attr-defined]


def test_one_open_run_per_ticket(repo: Path, fake: FakePlatform) -> None:
    lc = load(repo)
    Engine.start(lc, "APP-1")
    with pytest.raises(FactoryError, match="already has an open run"):
        Engine.start(lc, "APP-1")


def test_state_survives_reload(repo: Path, fake: FakePlatform) -> None:
    lc = load(repo)
    eng = Engine.start(lc, "APP-1")
    eng.advance()
    eng.submit("triage", TRIAGE)
    again = Engine.load(lc)
    assert again.st.node == "reproduce" and again.st.risk is not None
    assert [e for e in again.st.history][:2] == ["intake", "triage"]


def test_factory_owned_files_never_block_or_enter_the_fix(repo: Path, fake: FakePlatform) -> None:
    from mobile_factory import adapters

    lc = load(repo)
    adapters.install(lc, "claude")  # untracked skills, .mcp.json, CLAUDE.md
    (repo / ".cursor/rules").mkdir(parents=True)
    (repo / ".cursor/rules/factory-fix.mdc").write_text("x")
    eng = Engine.start(lc, "APP-1")
    to_fix(eng, fake)  # preflight passed despite the untracked factory files
    edit(repo)
    eng.submit("fix", FIX)
    verify(eng, fake)
    assert eng.out("commit")["files"] == ["app/src/main/java/Profile.kt"]


def test_fix_gets_locate_hint_when_present(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    assert "HINT" not in eng.instructions()
    (eng.dir / "context").mkdir(exist_ok=True)
    (eng.dir / "context/locate.json").write_text('{"files": []}')
    assert "HINT     " in eng.instructions() and "locate.json" in eng.instructions()


def test_reasoning_steps_get_a_haiku_scout_first(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    text = eng.instructions()
    assert eng.st.node == "fix" and text.index("SCOUT") < text.index("AGENT")
    assert "`factory-scout` (haiku)" in text and "fix-scout.md" in text
    (eng.dir / "context").mkdir(exist_ok=True)
    (eng.dir / "context/fix-scout.md").write_text("facts\n")
    text = eng.instructions()
    assert "SCOUT" not in text and "fix-scout.md" in text and "HINT     " in text


def test_history_runs_alongside_reproduce_and_hints_the_fix(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    (eng.dir / "context").mkdir(exist_ok=True)
    (eng.dir / "context/history.json").write_text("{}")
    assert "history.json  (the code's past" in eng.instructions()


def test_reviewers_start_alongside_verify_and_their_parts_count_for_the_same_diff(
    repo: Path, fake: FakePlatform
) -> None:
    import time

    eng = Engine.start(load(repo), "APP-1")
    to_fix(eng, fake)
    edit(repo)
    eng.submit("fix", FIX)
    while eng.st.node != "verify":
        eng.resume()
    text = eng.instructions()
    assert "ALONGSIDE start `factory-review-correctness` (opus)" in text
    assert (eng.dir / "context/diff.patch").is_file()  # built after checks, before verify
    time.sleep(0.01)
    for p in ("review-correctness", "review-taste", "review-detectors"):
        (eng.dir / "context" / f"{p}.json").write_text('{"findings": []}')
    assert [
        str(f).rsplit("/", 1)[-1] for f in eng._parts_done(["review-correctness", "review-taste", "review-detectors"])
    ] == ["review-correctness.json", "review-taste.json", "review-detectors.json"]
    diff = eng.dir / "context/diff.patch"
    diff.touch()  # a new diff (e.g. a review round's fixes): the parts must run again
    assert eng._parts_done(["review-correctness", "review-taste", "review-detectors"]) == []
