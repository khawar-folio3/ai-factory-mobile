from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakePlatform, git, load

from mobile_factory import config, workflow
from mobile_factory.errors import ConfigError
from mobile_factory.pipeline import Engine, route


def ticket(repo: Path, key: str, type_: str, title: str, body: str = "Details.") -> None:
    (config.state_dir(repo) / "tickets" / f"{key}.md").write_text(f"---\ntype: {type_}\n---\n# {title}\n\n{body}\n")


def approve(eng: Engine, gate: str) -> None:
    assert eng.st.status == "waiting_gate" and eng.node().gate == gate, eng.instructions()
    eng.decide(gate, True, "lead", eng.st.gates[gate].code)


# ---------- the definitions ----------


def test_every_workflow_is_well_formed() -> None:
    wfs = workflow.all_workflows()
    assert set(wfs) == {"bugfix", "task", "feature", "spike", "epic", "new-app"}
    from mobile_factory.outputs import EXAMPLES, MODELS
    from mobile_factory.pipeline import AUTO, POST

    for wf in wfs.values():
        assert wf.steps[0].name == "preflight" and wf.steps[-1].name == "handoff"
        for s in wf.steps:
            if s.kind == "agent":
                assert s.type in POST and s.type in MODELS and s.type in EXAMPLES, (wf.name, s.name)
                assert (Path(__file__).parents[1] / "src/mobile_factory/skills" / f"{s.skill}.md").is_file(), s.skill
            else:
                assert s.type in AUTO, (wf.name, s.name)
        if wf.outcome == "pr":
            for role in ("plan", "change"):
                assert wf.artifact(role), (wf.name, role)


def test_bad_definitions_are_rejected() -> None:
    with pytest.raises(ConfigError, match="retries to unknown step"):
        workflow._parse("x", "steps:\n  - {name: a, retry_to: nope}\n")
    with pytest.raises(ConfigError, match="needs a task"):
        workflow._parse("x", "steps:\n  - {name: a, kind: agent}\n")


def test_ticket_types_route_to_workflows(repo: Path) -> None:
    tc = load(repo).cfg.tracker
    assert [route(tc, t) for t in ("Bug", "Task", "Story", "Spike", "Epic", "App", "")] == [
        "bugfix",
        "task",
        "feature",
        "spike",
        "epic",
        "new-app",
        "bugfix",
    ]
    assert route(tc, "Sub-task", "Story") == "feature" and route(tc, "Sub-task", "Bug") == "bugfix"
    with pytest.raises(Exception, match="no parent with a workflow"):
        route(tc, "Sub-task", "")


def test_unmapped_type_stops_with_the_reason(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-9", "Incident", "Prod is down")
    eng = Engine.start(load(repo), "APP-9")
    eng.advance()
    assert eng.st.status == "stopped" and "no workflow for ticket type Incident" in eng.st.stop_reason


# ---------- feature ----------

PLAN = {
    "verdict": "eligible",
    "reason": "one screen",
    "summary": "Taller avatar on profile",
    "acceptance_criteria": ["Avatar is 64dp tall", "Profile still opens"],
    "plan": ["change height"],
    "screens": ["profile"],
    "estimated_files": 1,
}


def test_feature_goes_from_criteria_to_a_draft_pr(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-2", "Story", "Taller avatar")
    eng = Engine.start(load(repo), "APP-2")
    assert eng.st.pipeline == "feature"
    eng.advance()
    assert eng.st.node == "plan" and "SCOUT" in eng.instructions()
    eng.submit("plan", PLAN)
    assert eng.st.node == "baseline" and "ALONGSIDE start `factory-history`" in eng.instructions()
    fake.snapshot(eng.dir / "snapshots", "before", "profile")
    eng.submit("baseline", {"snapshots": ["profile"], "steps": ["open Profile"]})
    assert eng.st.node == "implement"
    (repo / "app/src/main/java/Profile.kt").write_text("class Profile {\n    val height = 64\n}\n")
    eng.submit(
        "implement", {"summary": "Make the profile avatar 64dp", "changes": "height 48 -> 64", "tests_added": True}
    )
    assert eng.st.node == "accept"
    fake.snapshot(eng.dir / "snapshots", "after", "profile")
    eng.submit(
        "accept",
        {
            "criteria": [
                {"criterion": "Avatar is 64dp tall", "met": True, "evidence": "snap profile"},
                {"criterion": "Profile still opens", "met": True, "evidence": "snap profile"},
            ],
            "snapshots": ["profile"],
        },
    )
    assert eng.st.node == "review"
    eng.submit("review", {"findings": []})
    assert eng.st.status == "done" and eng.st.outcome == "draft-pr", eng.instructions()
    assert eng.st.branch.startswith("feature/APP-2-")
    assert git(repo, "log", "-1", "--format=%s") == "APP-2: Make the profile avatar 64dp"
    body = Path(eng.out("pr_preview")["file"]).read_text()
    assert "## Acceptance criteria" in body and "- [x] Avatar is 64dp tall" in body and "Root cause" not in body


def test_feature_rejects_unchecked_criteria_and_retries_failed_ones(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-3", "Story", "Taller avatar")
    eng = Engine.start(load(repo), "APP-3")
    eng.advance()
    eng.submit("plan", PLAN)
    fake.snapshot(eng.dir / "snapshots", "before", "profile")
    eng.submit("baseline", {"snapshots": ["profile"]})
    (repo / "app/src/main/java/Profile.kt").write_text("class Profile {\n    val height = 64\n}\n")
    eng.submit("implement", {"summary": "Make the profile avatar 64dp", "changes": "height 48 -> 64"})
    one = {"criteria": [{"criterion": "Avatar is 64dp tall", "met": True, "evidence": "snap"}]}
    with pytest.raises(Exception, match="acceptance criteria not checked: Profile still opens"):
        eng.submit("accept", one)
    both_bad = {
        "criteria": [
            {"criterion": "Avatar is 64dp tall", "met": False, "evidence": "still 48dp"},
            {"criterion": "Profile still opens", "met": True, "evidence": "snap"},
        ]
    }
    eng.submit("accept", both_bad)
    assert eng.st.node == "implement" and "acceptance failed: Avatar is 64dp tall" in eng.instructions()


def test_too_big_story_stops_with_proposed_subtasks(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-4", "Story", "Redo the whole app")
    eng = Engine.start(load(repo), "APP-4")
    eng.advance()
    eng.submit("plan", {**PLAN, "verdict": "too-big", "subtasks": ["part one", "part two"]})
    assert eng.st.outcome == "too-big" and (eng.dir / "subtasks.md").read_text() == "- part one\n- part two\n"


# ---------- spike ----------


def test_spike_posts_an_approved_report_and_touches_no_code(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-5", "Spike", "Can we drop the legacy map SDK?")
    head = git(repo, "rev-parse", "HEAD")
    eng = Engine.start(load(repo), "APP-5")
    assert eng.st.pipeline == "spike"
    eng.advance()
    eng.submit(
        "research",
        {
            "question": "Can we drop the legacy map SDK?",
            "answer": "Yes, after two screens move to MapUIKit.",
            "findings": ["LegacyMap.kt:40 is the last user"],
            "options": [{"name": "migrate", "pros": ["one SDK"], "cons": ["2 days"]}],
            "recommendation": "migrate",
        },
    )
    approve(eng, "report")  # posting to the ticket always waits for a human
    assert eng.st.status == "done" and eng.st.outcome == "report-posted"
    posted = (config.state_dir(repo) / "tickets/APP-5.comments.md").read_text()
    assert "**Answer:** Yes, after two screens move" in posted and "**Option: migrate**" in posted
    assert git(repo, "rev-parse", "HEAD") == head and not eng.st.branch and not fake.published


# ---------- epic ----------


def test_epic_creates_approved_stories(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-6", "Epic", "Room booking v2")
    eng = Engine.start(load(repo), "APP-6")
    eng.advance()
    stories = [
        {"summary": "Show room capacity", "acceptance_criteria": ["capacity on card"]},
        {"summary": "Filter by capacity", "acceptance_criteria": ["filter works"], "description": "chip"},
    ]
    eng.submit("split", {"summary": "Room booking v2", "stories": stories})
    approve(eng, "tickets")
    assert eng.st.status == "done" and eng.st.outcome == "tickets-created"
    assert eng.out("create_tickets")["created"] == ["APP-7", "APP-8"]
    created = (config.state_dir(repo) / "tickets/APP-8.md").read_text()
    assert "type: Story" in created and "parent: APP-6" in created and "- filter works" in created


# ---------- new app ----------


def test_new_app_goes_spec_architecture_first_slice_then_backlog(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-10", "App", "Visitor check-in app")
    eng = Engine.start(load(repo), "APP-10")
    assert eng.st.pipeline == "new-app" and eng.st.ceiling == 0  # capped by the workflow
    eng.advance()
    eng.submit(
        "spec",
        {
            "summary": "Visitor check-in for front desks",
            "screens": ["home"],
            "acceptance_criteria": ["Home lists today's visitors"],
            "stories": [{"summary": "Print a visitor badge", "acceptance_criteria": ["badge prints"]}],
        },
    )
    approve(eng, "plan")
    eng.submit(
        "architecture",
        {
            "summary": "Compose app",
            "platform": "android-kotlin-compose",
            "modules": ["app"],
            "decisions": [{"topic": "DI", "choice": "Hilt"}],
        },
    )
    approve(eng, "architecture")
    assert eng.st.node == "scaffold"
    (repo / "app/src/main/java/Home.kt").write_text("class Home\n")
    eng.submit("scaffold", {"summary": "Create the check-in app with its home screen", "changes": "project + home"})
    approve(eng, "diff")
    fake.snapshot(eng.dir / "snapshots", "after", "home")
    eng.submit(
        "accept",
        {
            "criteria": [{"criterion": "Home lists today's visitors", "met": True, "evidence": "snap home"}],
            "snapshots": ["home"],
        },
    )
    eng.submit("review", {"findings": []})
    approve(eng, "review")
    approve(eng, "pr")
    assert eng.st.status == "done" and eng.st.outcome == "draft-pr", eng.instructions()
    assert eng.out("create_tickets")["created"] == ["APP-11"]  # the rest of the spec, as stories
    assert "Print a visitor badge" in (config.state_dir(repo) / "tickets/APP-11.md").read_text()


def test_task_checks_done_criteria_without_a_device_step(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-20", "Task", "Clean up Profile")
    eng = Engine.start(load(repo), "APP-20")
    assert eng.st.pipeline == "task"
    eng.advance()
    eng.submit(
        "plan",
        {
            **PLAN,
            "summary": "Bump OkHttp to 5.1",
            "acceptance_criteria": ["OkHttp is 5.1", "Unit tests pass"],
            "screens": [],
        },
    )
    assert eng.st.node == "implement"  # no baseline or reproduce for technical work
    (repo / "app/src/main/java/Profile.kt").write_text("class Profile {\n    val height = 48 // cleaned up\n}\n")
    eng.submit("implement", {"summary": "Bump OkHttp to 5.1", "changes": "version catalog"})
    eng.submit(
        "accept",
        {
            "criteria": [
                {"criterion": "OkHttp is 5.1", "met": True, "evidence": "gradle dependencies"},
                {"criterion": "Unit tests pass", "met": True, "evidence": "checks step"},
            ]
        },
    )
    eng.submit("review", {"findings": []})
    assert eng.st.status == "done" and eng.st.outcome == "draft-pr", eng.instructions()
    assert eng.st.branch.startswith("task/APP-20-")


@pytest.mark.parametrize(
    "case",
    [
        (
            "Task",
            "Crash when opening Profile",
            "Steps to reproduce: open Profile. Expected: opens. Actual: crash.",
            "bugfix",
            "text",
        ),
        ("Story", "Investigate: should we drop the legacy map SDK?", "Evaluate the options.", "spike", "text"),
        ("Task", "Bump OkHttp to 5.1", "Upgrade okhttp from 4.12 to 5.1.", "task", "jira"),
        (
            "Story",
            "Favourites filter",
            "As a user, I want to filter spaces. Acceptance criteria: chip shows.",
            "feature",
            "jira",
        ),
        ("", "App crashes on launch", "Steps to reproduce: launch. Actual: crash.", "bugfix", "text"),
    ],
)
def test_workflow_is_detected_from_the_text(repo: Path, case: tuple[str, str, str, str, str]) -> None:
    jira, summary, body, want, source = case
    from mobile_factory.integrations.tracker import Ticket
    from mobile_factory.pipeline import detect_workflow

    d = detect_workflow(load(repo).cfg.tracker, Ticket(key="APP-1", type=jira, summary=summary, description=body))
    assert (d.workflow, d.source) == (want, source), d


def test_a_bug_filed_as_a_task_runs_the_bug_workflow_and_says_why(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-30", "Task", "Crash when opening Profile", "Steps to reproduce: open Profile. Actual: crash.")
    eng = Engine.start(load(repo), "APP-30")
    eng.advance()
    assert eng.st.pipeline == "bugfix" and eng.st.node == "triage"
    assert eng.st.workflow_source == "text" and "reads like bugfix, not task" in eng.st.workflow_reason


def test_workflow_can_be_forced(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-31", "Task", "Crash when opening Profile", "Steps to reproduce: open Profile.")
    eng = Engine.start(load(repo), "APP-31", workflow_name="task")
    eng.advance()
    assert eng.st.pipeline == "task" and eng.st.node == "plan" and eng.st.workflow_source == "override"
