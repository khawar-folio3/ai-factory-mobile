from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import FakePlatform, git, load

from mobile_factory.errors import FactoryError, Refused
from mobile_factory.pipeline import Engine, _lint_tests

CRIT = "Avatar is 64dp tall"
DEBUG = 'class Profile {\n    val height = 64\n    init { println("debug") }\n}\n'


def edit(repo: Path, body: str = "class Profile {\n    val height = 64\n}\n") -> None:
    (repo / "app/src/main/java/Profile.kt").write_text(body)


def work(eng: Engine, **kw: Any) -> dict[str, Any]:
    flow = eng.flow("work")
    flow.parent.mkdir(parents=True, exist_ok=True)
    flow.write_text(f"appId: com.x\n---\n# criterion 1: {CRIT}\n- assertVisible: avatar\n")
    crit = [{"criterion": CRIT, "met": True, "evidence": "criterion 1 passed"}]
    return {"summary": "Make the profile avatar 64dp", "acceptance_criteria": crit, "flow": str(flow), **kw}


def to_work(eng: Engine) -> None:
    eng.advance()
    assert eng.st.node == "work" and eng.st.status == "waiting_agent", eng.instructions()


def body(eng: Engine) -> str:
    return Path(eng.out("pr_preview")["file"]).read_text()


def test_full_autonomy_reaches_draft_pr_in_order(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    assert eng.st.branch.startswith("bugfix/APP-1-avatar-is-clipped-on-the-profile-screen")  # kind + ticket title
    edit(repo)
    eng.submit("work", work(eng))
    assert eng.st.status == "done" and eng.st.outcome == "draft-pr", eng.instructions()
    assert eng.st.history == [
        "intake", "branch", "work", "checks", "review", "commit", "pr_preview", "publish", "handoff"
    ]  # fmt: skip
    assert eng.st.pr_url.endswith("/pull/7") and eng.st.level == 4
    assert all(g.decision == "auto" for g in eng.st.gates.values())
    assert git(repo, "log", "-1", "--format=%s") == "APP-1: Make the profile avatar 64dp"
    assert git(repo, "rev-list", "--count", f"{eng.st.checkpoint}..HEAD") == "1"
    assert f"- [x] {CRIT}" in body(eng) and f"- {CRIT}: criterion 1 passed" in body(eng)


def test_the_draft_pr_is_the_only_gate(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=0), "APP-1")
    to_work(eng)
    edit(repo)
    eng.submit("work", work(eng))
    assert list(eng.st.gates) == ["pr"] and eng.st.status == "waiting_gate"
    assert "WAITING ON A HUMAN" in eng.instructions()
    with pytest.raises(Refused):
        eng.decide("pr", True, by="me", code="nope")


def test_the_work_step_runs_in_the_session_with_a_criteria_flow(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    text = eng.instructions()
    assert "AGENT    do this step yourself in this session: no subagents" in text and "delegate" not in text
    assert "KIND  bugfix: the flow shows the defect before the fix" in text
    assert f"FLOW     write {eng.flow('work')}: one `# criterion N: <text>` section" in text
    assert "maestro --device <serial> test" in text and "UNIT" not in text


@pytest.mark.parametrize("how", ["run", "config"])
def test_unit_tests_are_added_by_the_work_step_when_turned_on(repo: Path, fake: FakePlatform, how: str) -> None:
    lc = load(repo)
    if how == "config":
        lc.cfg.steps["unit_tests"] = True
    eng = Engine.start(lc, "APP-1", enable=["unit_tests"] if how == "run" else [])
    to_work(eng)
    assert "UNIT  Also add unit tests for the new or changed pure logic only" in eng.instructions()


def test_progress_lines_around_automatic_steps(repo: Path, fake: FakePlatform) -> None:
    said: list[str] = []
    eng = Engine.start(load(repo), "APP-1")
    eng.notify = said.append
    to_work(eng)
    edit(repo)
    eng.submit("work", work(eng))
    assert "▸ Lint, tests, build…" in said and any(s.startswith("✓ Lint, tests, build") for s in said)


def test_build_context_reaches_the_work_step(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    build = eng.dir / "context" / "build.md"
    assert eng.st.outputs["_build"]["module"] == ":app:"
    assert "unit_tests :app:testDebugUnitTest" in build.read_text()
    assert "`:<module>:testDebugUnitTest`" in build.read_text()
    assert f"BUILD {build}" in eng.instructions()


def test_unit_tests_rerun_only_when_sources_change(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=0), "APP-1")
    to_work(eng)
    edit(repo)
    eng.submit("work", work(eng))
    assert fake.check_calls == 1
    summary, failed = _lint_tests(eng)
    assert fake.check_calls == 1 and "reused" in summary and not failed  # same sources: the recorded pass
    edit(repo, "class Profile {\n    val height = 72\n}\n")
    fake.fail_checks = True
    assert _lint_tests(eng)[1] and _lint_tests(eng)[1] and fake.check_calls == 3  # a failure is never reused


def test_reject_stops_run(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=0), "APP-1")
    to_work(eng)
    edit(repo)
    eng.submit("work", work(eng))
    eng.decide("pr", False, by="me", reason="wrong screen")
    assert eng.st.status == "stopped" and eng.st.outcome == "rejected"


def test_failing_checks_retry_the_work_then_roll_back(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    fake.fail_checks = True
    edit(repo)
    eng.submit("work", work(eng))
    assert eng.st.node == "work" and eng.st.fix_attempts == 1 and "LAST FAILURE" in eng.instructions()
    for _ in range(2):
        edit(repo)
        eng.submit("work", work(eng))
    assert eng.st.status == "stopped" and eng.st.outcome == "verify-failed" and fake.check_calls == 3
    assert "val height = 48" in (repo / "app/src/main/java/Profile.kt").read_text()
    assert (eng.dir / "failed-attempt-3.patch").is_file()


def test_forbidden_path_stops(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    (repo / "app/build.gradle.kts").write_text("// changed\n")
    eng.submit("work", work(eng))
    assert eng.st.outcome == "forbidden-path"


def test_work_output_is_validated(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    with pytest.raises(FactoryError, match="work output invalid"):
        eng.submit("work", {**work(eng), "extra": 1})
    with pytest.raises(FactoryError, match="summary"):
        eng.submit("work", work(eng, acceptance_criteria=[]))
    with pytest.raises(FactoryError, match="flow not found"):
        eng.submit("work", work(eng, flow="/nope.yaml"))
    with pytest.raises(FactoryError, match="no changes found"):
        eng.submit("work", work(eng))
    edit(repo)
    bad = [{"criterion": CRIT, "met": False, "evidence": "still 48dp"}]
    eng.submit("work", work(eng, acceptance_criteria=bad))
    assert eng.st.node == "work" and f"criteria not met: {CRIT} (still 48dp)" in eng.instructions()
    eng.submit("work", work(eng, stop="three features in one ticket"))
    assert eng.st.outcome == "ineligible" and eng.st.stop_reason == "three features in one ticket"


def test_unclear_work_asks_the_developer_then_runs_again(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    eng.submit("work", work(eng, questions=["which device?"]))
    assert eng.st.status == "waiting_answers" and not eng.st.finished
    assert "QUESTIONS FOR THE USER" in eng.instructions() and "which device?" in eng.instructions()
    eng.answer(["Pixel 7, Android 14"])
    assert eng.st.status == "waiting_agent" and eng.st.node == "work"
    assert "Pixel 7, Android 14" in eng.answers_file.read_text()
    assert f"ANSWERS {eng.answers_file}" in eng.instructions()


def test_answers_written_from_the_chat_resume_the_step(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    eng.submit("work", work(eng, questions=["which device?"]))
    eng.answers_file.write_text("- Q: which device?\n  A: Pixel 7\n")
    later = time.time() + 5
    os.utime(eng.answers_file, (later, later))
    eng.advance()  # `factory resume`
    assert eng.st.status == "waiting_agent" and eng.st.node == "work"


def test_questions_stop_after_three_rounds(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    for _ in range(3):
        eng.submit("work", work(eng, questions=["which device?"]))
        eng.answer([""])
    eng.submit("work", work(eng, questions=["which device?"]))
    assert eng.st.outcome == "needs-info" and "3 rounds" in eng.st.stop_reason


def test_detector_findings_go_to_the_pr_notes_and_never_stop_the_run(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    edit(repo, DEBUG)
    eng.submit("work", work(eng))
    findings = eng.out("review")["findings"]
    assert [(f["rule"], f["outcome"]) for f in findings] == [("S004", "open")]  # no agent, no fixes
    assert eng.st.status == "waiting_gate" and eng.node().gate == "pr"  # an open major always gets human eyes
    assert "## Open review notes\n- " in body(eng) and "Profile.kt:3" in body(eng)


def test_blocked_criteria_go_to_the_pr_not_the_checklist(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    edit(repo)
    blocked = {"criterion": "Synced", "met": False, "evidence": "no data", "blocked": True, "reason": "stage data"}
    crit = [{"criterion": CRIT, "met": True, "evidence": "criterion 1 passed"}, blocked]
    eng.submit("work", work(eng, acceptance_criteria=crit))
    assert eng.st.outcome == "draft-pr"
    assert "## Blocked\n- Synced: stage data" in body(eng) and "[ ] Synced" not in body(eng)


def test_publish_refuses_edited_preview(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=3), "APP-1")
    to_work(eng)
    edit(repo)
    eng.submit("work", work(eng))
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
    to_work(Engine.start(lc, "APP-1"))
    again = Engine.load(lc)
    assert again.st.node == "work" and again.st.history == ["intake", "branch", "work"]


def test_factory_owned_files_never_block_or_enter_the_change(repo: Path, fake: FakePlatform) -> None:
    from mobile_factory import adapters

    lc = load(repo)
    adapters.install(lc, "claude")
    (repo / ".cursor/rules").mkdir(parents=True)
    (repo / ".cursor/rules/factory-work.mdc").write_text("x")
    eng = Engine.start(lc, "APP-1")
    to_work(eng)  # preflight passed despite the untracked factory files
    edit(repo)
    eng.submit("work", work(eng))
    assert eng.out("commit")["files"] == ["app/src/main/java/Profile.kt"]


def test_a_new_run_builds_on_the_stopped_one(repo: Path, fake: FakePlatform) -> None:
    lc = load(repo)
    first = Engine.start(lc, "APP-1")
    to_work(first)
    first.submit("work", work(first, questions=["which device?"]))
    first.answer(["Pixel 7"])
    first.finish("stopped", "needs-info", "types endpoint returns one type for all spaces")
    first.save()
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", first.st.branch)  # the stopped run's branch, as `factory` asks
    again = Engine.start(lc, "APP-1")
    to_work(again)
    prev = again.dir / "context/previous.md"
    text = prev.read_text()
    assert "types endpoint returns one type" in text and "which device?" in text and "Pixel 7" in text
    assert f"HINT     {prev}" in again.instructions() and "Pixel 7" in again.answers_file.read_text()


def test_resume_reopens_a_run_stopped_by_a_failure(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    eng.finish("stopped", "failed", "tool crashed")
    eng.reopen()
    eng.advance()
    assert eng.st.node == "work" and eng.st.status == "waiting_agent" and not eng.st.stop_reason
    eng.finish("stopped", "rejected", "no")
    with pytest.raises(FactoryError, match="only"):
        eng.reopen()


def test_commit_stages_only_the_reviewed_diff(repo: Path, fake: FakePlatform) -> None:
    said: list[str] = []

    def note(msg: str) -> None:
        said.append(msg)
        if msg.startswith("▸ Commit"):  # after the detector review
            (repo / "gradle.properties").write_text("COMPANY_FLAVORS=acme\n")  # a local client switch
            (repo / "scratch.txt").write_text("notes\n")

    eng = Engine.start(load(repo), "APP-1")
    eng.notify = note
    to_work(eng)
    edit(repo)
    (repo / "app/New.kt").write_text("class New\n")
    eng.submit("work", work(eng))
    assert eng.out("commit")["files"] == ["app/New.kt", "app/src/main/java/Profile.kt"]
    assert eng.out("commit")["left_out"] == ["gradle.properties", "scratch.txt"]
    assert any(s.startswith("committed ") for s in said) and any("left out" in s for s in said)


def test_commit_refuses_a_temp_marker(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    edit(repo, "class Profile {\n    val height = 64 // TEMP until the API is fixed\n}\n")
    with pytest.raises(FactoryError, match="TEMP marker"):
        eng.submit("work", work(eng))
    assert git(repo, "rev-list", "--count", f"{eng.st.checkpoint}..HEAD") == "0"


def test_risk_skips_tests_and_counts_new_files(repo: Path, fake: FakePlatform) -> None:
    eng = Engine.start(load(repo, ceiling=0), "APP-1")
    to_work(eng)
    (repo / "app/New.kt").write_text("a\nb\nc\n")
    (repo / "app/src/test").mkdir(parents=True)
    (repo / "app/src/test/NewTest.kt").write_text("x\n" * 80)
    eng.submit("work", work(eng))
    s = eng.st.signals
    assert s.actual_files == 1 and s.lines_changed == 3 and s.tests_added


def test_direction_reaches_the_work_step(repo: Path, fake: FakePlatform) -> None:
    say = "Stage data returns one type for all spaces; mock types/v3 with `factory android mock`."
    eng = Engine.start(load(repo), "APP-1", direction=say)
    to_work(eng)
    assert say in eng.direction_file.read_text() and eng.direction() == say
    assert f"DIRECTION {eng.direction_file}  (HIGH PRIORITY" in eng.instructions()
    eng.direct("Only the Project Room type is wrong.")
    assert eng.direction() == "Only the Project Room type is wrong."
    assert eng.direction_file.read_text().count("## ") == 2  # timestamped entries, appended


def open_pr_branch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    git(repo, "switch", "-q", "-c", "feature/APP-1-earlier")
    edit(repo)
    git(repo, "commit", "-qam", "APP-1: Make the profile avatar 64dp")
    git(repo, "push", "-q", "origin", "feature/APP-1-earlier")
    git(repo, "switch", "-q", "main")
    git(repo, "branch", "-q", "-D", "feature/APP-1-earlier")
    pr = {"url": "https://github.com/acme/demo/pull/3", "headRefName": "feature/APP-1-earlier", "baseRefName": "main"}
    monkeypatch.setattr("mobile_factory.pipeline.github.open_pr", lambda root, key: pr)
    return git(repo, "rev-parse", "origin/feature/APP-1-earlier")


def test_an_open_pr_is_adopted_and_verified_without_a_new_pr(
    repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    tip = open_pr_branch(repo, monkeypatch)
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    assert eng.st.adopted and eng.st.branch == "feature/APP-1-earlier" and eng.st.checkpoint == tip
    assert "ADOPTED https://github.com/acme/demo/pull/3" in eng.instructions()
    eng.submit("work", work(eng))
    assert eng.st.status == "done" and eng.st.pr_url.endswith("/pull/3"), eng.instructions()
    assert git(repo, "rev-parse", "HEAD") == tip and "title" not in fake.published and "branch" not in fake.published


def test_a_fix_on_an_adopted_pr_is_pushed_to_it(
    repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_pr_branch(repo, monkeypatch)
    eng = Engine.start(load(repo), "APP-1")
    to_work(eng)
    edit(repo, "class Profile {\n    val height = 72\n}\n")
    eng.submit("work", work(eng))
    assert eng.st.status == "done", eng.instructions()
    assert fake.published == {"branch": "feature/APP-1-earlier"}
    assert git(repo, "rev-list", "--count", f"{eng.st.checkpoint}..HEAD") == "1"
