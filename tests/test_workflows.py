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
    assert set(wfs) == {"light", "spike", "epic", "new-app"}
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
    types = ("Bug", "Task", "Story", "Improvement", "Spike", "Epic", "App", "")
    assert [route(tc, t) for t in types] == ["light"] * 4 + ["spike", "epic", "new-app", "light"]
    assert route(tc, "Sub-task", "Story") == "light" and route(tc, "Sub-task", "Epic") == "epic"
    with pytest.raises(Exception, match="no parent with a workflow"):
        route(tc, "Sub-task", "")


def test_unmapped_type_stops_with_the_reason(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-9", "Incident", "Prod is down")
    eng = Engine.start(load(repo), "APP-9")
    eng.advance()
    assert eng.st.status == "stopped" and "no workflow for ticket type Incident" in eng.st.stop_reason


SPEC = {
    "summary": "Visitor check-in for front desks",
    "screens": ["home"],
    "acceptance_criteria": ["Home lists today's visitors"],
}
ARCH = {
    "summary": "Compose app",
    "platform": "android-kotlin-compose",
    "modules": ["app"],
    "decisions": [{"topic": "DI", "choice": "Hilt"}],
}


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
    assert eng.st.node == "scaffold" and "delegate to the `factory-scaffold` subagent (opus)" in eng.instructions()
    (repo / "app/src/main/java/Home.kt").write_text("class Home\n")
    flow = eng.flow("scaffold")
    flow.parent.mkdir(parents=True, exist_ok=True)
    flow.write_text("appId: com.x\n---\n# criterion 1: Home lists today's visitors\n- assertVisible: Today\n")
    crit = [{"criterion": "Home lists today's visitors", "met": True, "evidence": "criterion 1 passed"}]
    out = {"summary": "Create the check-in app with its home screen", "acceptance_criteria": crit, "flow": str(flow)}
    eng.submit("scaffold", out)
    approve(eng, "pr")
    assert eng.st.status == "done" and eng.st.outcome == "draft-pr", eng.instructions()
    assert eng.out("create_tickets")["created"] == ["APP-11"]  # the rest of the spec, as stories
    assert "Print a visitor badge" in (config.state_dir(repo) / "tickets/APP-11.md").read_text()


@pytest.mark.parametrize(
    "case",
    [
        ("Task", "Crash when opening Profile", "Steps to reproduce: open Profile. Actual: crash.", "light", "bugfix"),
        ("Story", "Investigate: should we drop the legacy map SDK?", "Evaluate the options.", "spike", "feature"),
        ("Task", "Bump OkHttp to 5.1", "Upgrade okhttp from 4.12 to 5.1.", "light", "task"),
        (
            "Story",
            "Favourites filter",
            "As a user, I want to filter spaces. Acceptance criteria: chip.",
            "light",
            "feature",
        ),
        ("", "App crashes on launch", "Steps to reproduce: launch. Actual: crash.", "light", "bugfix"),
    ],
)
def test_workflow_and_kind_are_detected_from_the_text(repo: Path, case: tuple[str, str, str, str, str]) -> None:
    jira, summary, body, want, kind = case
    from mobile_factory.integrations.tracker import Ticket
    from mobile_factory.pipeline import detect_workflow

    d = detect_workflow(load(repo).cfg.tracker, Ticket(key="APP-1", type=jira, summary=summary, description=body))
    assert (d.workflow, d.kind) == (want, kind), d


def test_code_tickets_run_light_with_their_kind(repo: Path, fake: FakePlatform) -> None:
    lc = load(repo)
    for key, type_, kind in (("APP-20", "Bug", "bugfix"), ("APP-21", "Task", "task"), ("APP-22", "Story", "feature")):
        ticket(repo, key, type_, "Taller avatar")
        eng = Engine.start(lc, key)
        assert (eng.st.pipeline, eng.st.kind) == ("light", kind)
    ticket(repo, "APP-30", "Task", "Crash when opening Profile", "Steps to reproduce: open Profile. Actual: crash.")
    eng = Engine.start(lc, "APP-30")
    eng.advance()
    assert (eng.st.pipeline, eng.st.kind, eng.st.node) == ("light", "bugfix", "work")
    assert eng.st.branch.startswith("bugfix/APP-30-")


def test_light_workflow_order_and_gates() -> None:
    wf = workflow.get("light")
    assert wf.names == [
        "preflight", "intake", "branch", "work", "checks", "review", "commit", "pr_preview", "publish", "handoff"
    ]  # fmt: skip
    assert [s.gate for s in wf.steps if s.gate] == ["pr"]
    assert [s.name for s in wf.steps if s.kind == "agent"] == ["work"] and wf.step("work").params["solo"]
    assert wf.step("review").type == "detectors" and wf.step("checks").retry_to == "work"


def test_workflow_can_be_forced(repo: Path, fake: FakePlatform) -> None:
    ticket(repo, "APP-31", "Task", "Crash when opening Profile", "Steps to reproduce: open Profile.")
    eng = Engine.start(load(repo), "APP-31", workflow_name="spike")
    eng.advance()
    assert eng.st.pipeline == "spike" and eng.st.node == "research" and eng.st.workflow_source == "override"


# ---------- your own workflows ----------


def test_extends_with_changes_and_a_custom_step(repo: Path, fake: FakePlatform) -> None:
    folder = workflow.folders(repo)[-1]
    folder.mkdir(parents=True)
    (folder / "hotfix.yaml").write_text(
        "extends: light\nmax_level: 1\nchanges:\n"
        "  - add: {name: analytics, kind: agent, type: custom, skill: analytics-check, retry_to: work,\n"
        "          task: Check the screen_view events.}\n    after: work\n"
        "  - set: work\n    model: haiku\n"
        "  - remove: review\n"
    )
    (folder / "analytics-check.md").write_text("# Analytics check\n")
    wf = workflow.get("hotfix", repo)
    names = wf.names
    assert names.index("analytics") == names.index("work") + 1 and "review" not in names
    assert wf.step("work").model == "haiku" and wf.max_level == 1
    assert wf.step("analytics").skill_file.endswith("analytics-check.md")
    assert wf.artifact("change") == "work"  # inherited

    from mobile_factory.wizard import write_local

    write_local(repo, {"tracker": {"pipelines": {"Bug": "hotfix"}}})
    assert route(load(repo).cfg.tracker, "Story") == "light"  # yours are merged onto the defaults
    eng = Engine.start(load(repo), "APP-1")
    assert eng.st.pipeline == "hotfix" and eng.st.ceiling == 1
    eng.advance()
    (repo / "app/src/main/java/Profile.kt").write_text("class Profile {\n    val height = 64\n}\n")
    flow = eng.flow("work")
    flow.parent.mkdir(parents=True, exist_ok=True)
    flow.write_text("appId: com.x\n---\n# criterion 1: Avatar shows\n- assertVisible: avatar\n")
    crit = [{"criterion": "Avatar shows", "met": True, "evidence": "criterion 1 passed"}]
    eng.submit("work", {"summary": "Wrap avatar height", "acceptance_criteria": crit, "flow": str(flow)})
    assert eng.st.node == "analytics" and "analytics-check.md" in eng.instructions()
    eng.submit("analytics", {"summary": "screen_view missing", "ok": False})
    assert eng.st.node == "work" and "screen_view missing" in eng.instructions()  # ok: false retried


def test_a_broken_workflow_stops_the_run_with_the_reason(repo: Path) -> None:
    folder = workflow.folders(repo)[-1]
    folder.mkdir(parents=True)
    (folder / "light.yaml").write_text(
        "steps:\n  - {name: preflight}\n  - {name: dance, kind: agent, type: tango, task: Dance.}\n  - {name: handoff}\n"
    )
    with pytest.raises(Exception, match="unknown step types: dance"):
        Engine.start(load(repo), "APP-1")


def test_wizard_runs_agent_steps_asks_at_gates_pauses_and_picks_up(
    repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as js

    import typer

    from mobile_factory import cli

    ticket(repo, "APP-40", "App", "Visitor check-in app")
    outputs: dict[str, object] = {"spec": SPEC}
    calls: list[str] = []

    def fake_agent(cmds: list[list[str]], cwd: Path, logs: list[Path], stage: object, **_: object) -> int:
        prompt = cmds[0][2]
        step = next((s for s in outputs if f"outputs/{s}.json" in prompt), None)
        if step is None:
            return 1  # an agent that wrote nothing
        calls.append(step)
        out = Path(prompt.split("write the output JSON to ")[1].split(" ")[0])
        out.write_text(js.dumps(outputs[step]))
        return 0

    monkeypatch.setattr(cli, "_agent_cli", lambda lc: "claude")
    monkeypatch.setattr(cli, "_run_watched", fake_agent)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_choose", lambda q, options, default: 2)  # "Pause here" at the plan gate
    eng = Engine.start(load(repo, ceiling=0), "APP-40")
    eng.advance()
    with pytest.raises(typer.Exit):
        cli._wizard(eng)
    assert calls == ["spec"] and eng.st.status == "waiting_gate" and eng.node().gate == "plan"

    again = Engine.load(load(repo), eng.st.id)  # later: `factory run APP-40` picks it up
    monkeypatch.setattr(cli, "_choose", lambda q, options, default: 0)  # approve
    outputs["architecture"] = ARCH
    with pytest.raises(typer.Exit):  # the next gate is approved, then scaffold has no scripted output: saved
        cli._wizard(again)
    assert again.st.gates["plan"].decision == "approved" and "architecture" in calls
    assert Engine.load(load(repo), eng.st.id).st.node == "scaffold"  # progress was saved


def test_wizard_asks_the_questions_inline(repo: Path, fake: FakePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    import json as js

    import typer

    from mobile_factory import cli

    ticket(repo, "APP-41", "App", "Visitor check-in app")
    plans = [{**SPEC, "verdict": "needs-info", "questions": ["How tall?", "Which screens?"]}, SPEC]

    def fake_agent(cmds: list[list[str]], cwd: Path, logs: list[Path], stage: object, **_: object) -> int:
        prompt = cmds[0][2]
        if "outputs/spec.json" not in prompt or not plans:
            return 1
        if len(plans) == 1:
            assert "ANSWERS" in prompt  # the second plan run sees the answers
        Path(prompt.split("write the output JSON to ")[1].split(" ")[0]).write_text(js.dumps(plans.pop(0)))
        return 0

    answers = iter(["64dp", ""])
    monkeypatch.setattr(cli, "_agent_cli", lambda lc: "claude")
    monkeypatch.setattr(cli, "_run_watched", fake_agent)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli, "_choose", lambda q, options, default: 2)  # pause at the plan gate
    eng = Engine.start(load(repo, ceiling=0), "APP-41")
    eng.advance()
    with pytest.raises(typer.Exit):
        cli._wizard(eng)
    text = eng.answers_file.read_text()
    assert "A: 64dp" in text and "Which screens?" in text and "not sure" in text
    assert eng.st.status == "waiting_gate" and eng.node().gate == "plan"
