from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from functools import cached_property
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from . import autonomy, classify, events, usage, viz, workflow
from .config import LoadedConfig, TrackerConfig
from .errors import FactoryError, Refused, Stop
from .gitops import Git
from .globs import matches
from .guardrail import context as review_ctx
from .guardrail import diff as difflib
from .guardrail import limits
from .guardrail import rules as rl
from .integrations import github, tracker
from .outputs import EXAMPLES, MODELS, CustomOut, SpecOut, SplitOut, WorkOut, fields
from .platforms import maestro
from .platforms import make as make_platform
from .platforms.android import ROUTES
from .platforms.base import Platform, files_hash, snapshot_diff
from .proc import run
from .state import GateRecord, RunState, RunStore, new_run_id

ATTRIBUTION = re.compile(r"claude|chatgpt|copilot|cursor|generated (with|by)|co-authored-by|🤖", re.I)


MAX_QUESTION_ROUNDS = 3
REOPENABLE = ("gate-needs-work", "failed")  # stopped runs `factory resume` restarts at the step they stopped on
NEVER_STAGE = ["gradle.properties", "variants/*.properties"]  # a developer's local client switch
TEMP_MARKER = re.compile(r"(//|#|/\*|<!--)\s*TEMP\b")
TESTS = "unit_tests"  # factory.yaml `steps: {unit_tests: true}` or `factory run --tests`: the work step adds unit tests


class Ask(Exception):
    """The step needs the developer: questions to answer, or one choice to make."""

    def __init__(self, reason: str, questions: list[str]) -> None:
        super().__init__(reason)
        self.reason, self.questions = reason, questions


class Retry(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


Node = workflow.Step  # a workflow step; kept under its old name for callers


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:40]


def preview_sha(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]


class Engine:
    def __init__(self, lc: LoadedConfig, st: RunState) -> None:
        self.lc = lc
        self.cfg = lc.cfg
        self.st = st
        self.store = RunStore(lc.runs_dir)
        self.dir = self.store.path(st.id)
        self.git = Git(lc.root, self.cfg.project.local_only_paths)
        sinks: list[events.Sink] = []
        if self.cfg.notifications.slack_webhook:
            sinks.append(events.slack_sink(self.cfg.notifications.slack_webhook))
        if not isinstance(v := viz.make(self.cfg.viz), viz.NullVisualizer):
            sinks.append(viz.event_sink(v, lc.root))
        self.bus = events.EventBus(lc.state_dir / "events.jsonl", sinks)
        self.notify: Callable[[str], None] = lambda _msg: None  # progress lines for slow automatic steps (CLI sets it)

    # ---------- lifecycle ----------

    @classmethod
    def start(
        cls,
        lc: LoadedConfig,
        ticket: str,
        ceiling: int | None = None,
        base: str | None = None,
        workflow_name: str | None = None,
        *,
        enable: list[str] | None = None,
        direction: str = "",
    ) -> Engine:
        store = RunStore(lc.runs_dir)
        earlier = [r for r in store.all() if r.ticket == ticket]
        if open_run := next((r for r in earlier if not r.finished), None):
            raise FactoryError(f"{ticket} already has an open run {open_run.id}: `factory resume` or `factory abort`")
        found = _route_ticket(lc, ticket)
        wf = workflow.get(workflow_name or found.workflow, lc.root)
        check_workflow(wf)
        nodes = wf.steps
        st = RunState(
            id=new_run_id(ticket),
            ticket=ticket,
            created_at=events.now(),
            updated_at=events.now(),
            pipeline=wf.name,
            kind=found.kind,
            workflow_source="override" if workflow_name else "",
            workflow_reason="chosen with --workflow" if workflow_name else "",
            node=nodes[0].name,
            ceiling=min(wf.max_level, lc.cfg.autonomy.ceiling if ceiling is None else ceiling),
            base=base or ("" if lc.cfg.project.base_branch == "ask" else lc.cfg.project.base_branch),
            enabled=list(enable or []),
        )
        eng = cls(lc, st)
        eng.save()
        if earlier:
            eng._carry_over(earlier[-1])
        if direction.strip():
            eng.direct(direction)
        eng.bus.emit(events.RUN_STARTED, st.id, ticket=ticket, ceiling=st.ceiling)
        return eng

    @classmethod
    def load(cls, lc: LoadedConfig, run_id: str | None = None) -> Engine:
        return cls(lc, RunStore(lc.runs_dir).load(run_id))

    def _carry_over(self, prev: RunState) -> None:
        """What an earlier run of the ticket learned (why it stopped, questions and answers, its plan) feeds the plan."""
        old = self.store.path(prev.id)
        try:
            plan_step = workflow.get(prev.pipeline, self.lc.root).artifact("plan")
        except FactoryError:
            plan_step = ""
        plan = prev.outputs.get(plan_step, {})
        parts = [f"# Earlier run {prev.id}: {prev.status} {prev.outcome}".rstrip()]
        if prev.stop_reason:
            parts += ["", f"Stop reason: {prev.stop_reason}"]
        for name, title in (
            ("stop-reason.md", "Stop reason (file)"),
            ("questions.md", "Questions"),
            ("answers.md", "Answers"),
        ):
            if (f := old / name).is_file():
                parts += ["", f"## {title}", f.read_text().strip()]
        if plan:
            parts += ["", f"## Its plan ({plan_step})", json.dumps(plan, indent=2)]
        ctx = self.dir / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "previous.md").write_text("\n".join(parts) + "\n")
        if (old / "answers.md").is_file():
            self.answers_file.write_text((old / "answers.md").read_text())

    @property
    def direction_file(self) -> Path:
        return self.dir / "direction.md"

    def direct(self, text: str) -> None:
        """The developer's direction, appended with a timestamp; every later agent step gets it as a hint."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.direction_file.open("a") as fh:
            fh.write(f"## {events.now()}\n{text.strip()}\n\n")

    def direction(self) -> str:
        """The latest direction's first line, for the run view."""
        if not self.direction_file.is_file():
            return ""
        entries = [ln for ln in self.direction_file.read_text().splitlines() if ln.strip() and not ln.startswith("## ")]
        return entries[-1] if entries else ""

    def reopen(self) -> None:
        """A run stopped by review or a failed step starts again at the step it stopped on."""
        if self.st.status != "stopped" or self.st.outcome not in REOPENABLE:
            raise FactoryError(f"run {self.st.id} is {self.st.status} ({self.st.outcome}); only {REOPENABLE} reopen")
        self.st.status, self.st.outcome, self.st.stop_reason = "running", "", ""
        self._start(self.st.node, "reopened")

    def enabled(self, node: Node) -> bool:
        """Optional steps (`optional: true`) run only when factory.yaml `steps.<name>` or the run turns them on."""
        return not node.params.get("optional") or self.cfg.steps.get(node.name, False) or node.name in self.st.enabled

    def _start(self, name: str, title: str) -> None:
        self.st.started[name] = time.time()
        self.bus.emit(events.NODE_STARTED, self.st.id, node=name, title=title)

    def _took(self, name: str) -> int:
        """Milliseconds since the step's current attempt started (0 when it never started: skipped)."""
        t0 = self.st.started.pop(name, None)
        return int((time.time() - t0) * 1000) if t0 else 0

    def flow(self, step: str) -> Path:
        """The Maestro flow recorded while an agent drove the device in `step`."""
        return self.dir / "flows" / f"{step}.yaml"

    def skipped(self, name: str) -> bool:
        return bool(self.out(name).get("skipped"))

    def save(self) -> None:
        self.store.save(self.st)

    @property
    def wf(self) -> workflow.Workflow:
        return workflow.get(self.st.pipeline, self.lc.root)

    @property
    def nodes(self) -> list[Node]:
        return list(self.wf.steps)

    def art(self, role: str) -> dict[str, Any]:
        """The output of the step playing `role` (plan, before, change, after) in this run's workflow."""
        step = self.wf.artifact(role)
        return self.out(step) if step else {}

    def node(self, name: str | None = None) -> Node:
        n = name or self.st.node
        return next(x for x in self.nodes if x.name == n)

    def goto(self, name: str) -> None:
        self.st.node = name
        self.st.status = "running"
        self.st.history.append(name)

    def _next_node(self) -> None:
        names = [n.name for n in self.nodes]
        i = names.index(self.st.node)
        self.goto(names[i + 1])

    @cached_property
    def platform(self) -> Platform:
        return make_platform(self.lc)

    @cached_property
    def tracker(self) -> tracker.Tracker:
        return tracker.make(self.cfg.tracker, self.lc.root)

    def out(self, name: str) -> dict[str, Any]:
        return self.st.outputs.get(name, {})

    # ---------- driving ----------

    def advance(self) -> RunState:
        try:
            while not self.st.finished:
                if self.st.status == "waiting_answers":
                    q = self.dir / "questions.md"
                    if (
                        self.answers_file.is_file()
                        and q.is_file()
                        and self.answers_file.stat().st_mtime > q.stat().st_mtime
                    ):
                        self.st.outputs.pop("_ask", None)  # answered in the agent's chat, written to answers.md
                        self.st.status = "waiting_agent"
                    break
                if self.st.status == "waiting_gate":
                    rec = self.st.gates[self.node().gate or ""]
                    if rec.decision == "pending":
                        break
                    if rec.decision == "rejected":
                        raise Stop("rejected", f"gate {rec.gate} rejected: {rec.reason or 'no reason given'}")
                    self._complete_node()
                    continue
                node = self.node()
                if not self.enabled(node):
                    self.st.outputs[node.name] = {"skipped": True}
                    self._complete_node(skipped=True)
                    continue
                if node.kind == "agent":
                    if self.st.status != "waiting_agent":
                        self.st.status = "waiting_agent"
                        self._start(node.name, node.title)
                        self.bus.emit(events.AGENT_WAITING, self.st.id, node=node.name)
                    break
                self._start(node.name, node.title)
                self.notify(f"▸ {node.title}…")
                t0 = time.monotonic()
                result = AUTO[node.type](self)
                self.notify(f"✓ {node.title} ({time.monotonic() - t0:.0f}s)")
                if result is not None:
                    self.st.outputs[node.name] = result
                self._after_node(node)
        except Retry as r:
            self._retry(r.reason)
            self.save()
            return self.advance()
        except Stop as s:
            self.finish("stopped", s.outcome, s.reason)
        self.save()
        return self.st

    def submit(self, node_name: str, data: dict[str, Any]) -> RunState:
        node = self.node()
        if self.st.finished:
            raise FactoryError(f"run {self.st.id} is {self.st.status}")
        if node.name != node_name or node.kind != "agent" or self.st.status != "waiting_agent":
            raise FactoryError(f"run is at {self.st.node} ({self.st.status}); cannot submit {node_name}")
        try:
            parsed = MODELS[node.type].model_validate(data)
        except ValidationError as e:
            raise FactoryError(f"{node_name} output invalid:\n{e}\nFields: {fields(MODELS[node.type])}") from e
        self._keep_version(node_name, data)
        try:
            jump = POST[node.type](self, parsed)
            self.st.outputs[node_name] = parsed.model_dump()
            if jump:
                self.bus.emit(
                    events.NODE_COMPLETED, self.st.id, node=node_name, next=jump, duration_ms=self._took(node_name)
                )
                self.goto(jump)
            else:
                self._after_node(node)
        except Retry as r:
            self._retry(r.reason)
        except Ask as a:
            self._ask(node_name, a)
            self.save()
            return self.st
        except Stop as s:
            self.finish("stopped", s.outcome, s.reason)
            self.save()
            return self.st
        self.save()
        return self.advance()

    def _keep_version(self, step: str, data: dict[str, Any]) -> None:
        """Every submitted output stays: <step>.json, <step>.2.json, … under submitted/."""
        d = self.dir / "submitted"
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.glob(f"{step}.json"))) + len(list(d.glob(f"{step}.[0-9]*.json")))
        (d / (f"{step}.json" if not n else f"{step}.{n + 1}.json")).write_text(json.dumps(data, indent=2))

    # ---------- questions for the developer ----------

    @property
    def answers_file(self) -> Path:
        return self.dir / "answers.md"

    def _ask(self, step: str, a: Ask) -> None:
        if self.st.question_rounds >= MAX_QUESTION_ROUNDS:
            (self.dir / "questions.md").write_text("\n".join(f"- {q}" for q in a.questions) + "\n")
            self.finish("stopped", "needs-info", f"still unclear after {self.st.question_rounds} rounds: {a.reason}")
            return
        self.st.question_rounds += 1
        self.st.status = "waiting_answers"
        self.st.outputs["_ask"] = {"step": step, "reason": a.reason, "questions": a.questions}
        (self.dir / "questions.md").write_text("\n".join(f"- {q}" for q in a.questions) + "\n")
        self.bus.emit(
            events.GATE_WAITING, self.st.id, gate="questions", node=step, ticket=self.st.ticket, summary=a.reason[:200]
        )

    def answer(self, answers: list[str]) -> RunState:
        """The developer's answers (same order as the questions; '' = not sure). The step runs again with them."""
        ask = self.out("_ask")
        if self.st.status != "waiting_answers" or not ask:
            raise FactoryError("no questions are waiting")
        with self.answers_file.open("a") as fh:
            fh.write(f"## Round {self.st.question_rounds} ({ask['step']})\n")
            for q, ans in zip(ask["questions"], answers, strict=False):
                fh.write(f"- Q: {q}\n  A: {ans or 'not sure: make the most reasonable assumption and say which'}\n")
            fh.write("\n")
        self.st.outputs.pop("_ask", None)
        self.st.status = "waiting_agent"
        self.bus.emit(events.GATE_DECIDED, self.st.id, gate="questions", node=ask["step"], decision="answered")
        self.save()
        return self.st

    def _after_node(self, node: Node) -> None:
        if node.gate and self._gate_needed(node.gate):
            self.st.status = "waiting_gate"
            code = secrets.token_hex(2)
            self.st.gates[node.gate] = GateRecord(gate=node.gate, code=code, sha=self._gate_sha(node.gate))
            self.bus.emit(
                events.GATE_WAITING,
                self.st.id,
                gate=node.gate,
                node=node.name,
                ticket=self.st.ticket,
                risk=self.st.risk.score if self.st.risk else None,
                level=self.st.level,
                summary=self.gate_summary(node.gate).splitlines()[0],
            )
            return
        if node.gate:
            self.st.gates[node.gate] = GateRecord(
                gate=node.gate, decision="auto", at=events.now(), sha=self._gate_sha(node.gate)
            )
            self.bus.emit(
                events.GATE_DECIDED, self.st.id, gate=node.gate, node=node.name, decision="auto", level=self.st.level
            )
        self._complete_node()

    def _complete_node(self, **data: Any) -> None:
        self.bus.emit(
            events.NODE_COMPLETED, self.st.id, node=self.st.node, duration_ms=self._took(self.st.node), **data
        )
        if self.st.node == self.nodes[-1].name:
            self.finish("done", self.st.outcome or self.wf.outcome, "")
        else:
            self._next_node()

    def _gate_needed(self, gate: str) -> bool:
        if gate == "pr" and any(
            f.get("outcome") in ("dismissed", "open", "not applied") and f.get("severity") in ("blocker", "major")
            for f in self.out("review").get("findings", [])
        ):
            return True  # a dismissed or open blocker/major always gets human eyes
        return not autonomy.gate_is_automatic(self.cfg.gates[gate], self.st.level)

    def _gate_sha(self, gate: str) -> str:
        return self.out("pr_preview").get("sha", "") if gate == "pr" else ""

    def decide(self, gate: str, approve: bool, by: str, code: str = "", reason: str = "") -> RunState:
        rec = self.st.gates.get(gate)
        if self.st.status != "waiting_gate" or not rec or rec.decision != "pending":
            raise FactoryError(f"gate {gate} is not waiting (run at {self.st.node}, {self.st.status})")
        if approve and code != rec.code:
            raise Refused(f"confirmation code mismatch for gate {gate}")
        rec.decision, rec.by, rec.at, rec.reason = ("approved" if approve else "rejected"), by, events.now(), reason
        self.bus.emit(events.GATE_DECIDED, self.st.id, gate=gate, node=self.st.node, decision=rec.decision, by=by)
        self.save()
        return self.advance()

    def _retry(self, reason: str) -> None:
        target = self.node().retry_to or self.wf.artifact("change")
        if not target:
            self.finish(
                "stopped", "failed", f"{self.st.node} failed; the workflow has no step to retry: {reason[:300]}"
            )
            return
        self.st.fix_attempts += 1
        self.bus.emit(
            events.NODE_FAILED,
            self.st.id,
            node=self.st.node,
            reason=reason,
            attempt=self.st.fix_attempts,
            duration_ms=self._took(self.st.node),
        )
        self.st.outputs["_last_failure"] = {"node": self.st.node, "reason": reason[-3000:]}
        if self.st.fix_attempts > self.cfg.limits.max_fix_attempts:
            if self.cfg.limits.rollback_on_fail and self.st.checkpoint:
                self.git.save_patch(self.st.checkpoint, self.dir / f"failed-attempt-{self.st.fix_attempts}.patch")
                files = self.git.rollback(self.st.checkpoint)
                self.bus.emit(events.ROLLBACK, self.st.id, checkpoint=self.st.checkpoint, files=files)
            self.finish(
                "stopped",
                "verify-failed",
                f"still failing after {self.st.fix_attempts - 1} extra attempt(s): {reason[:300]}",
            )
            return
        self.goto(target)

    def finish(self, status: Literal["done", "stopped"], outcome: str, reason: str) -> None:
        self.st.status = status
        self.st.outcome = outcome
        self.st.stop_reason = reason
        with contextlib.suppress(Exception):  # token count is bookkeeping: it never fails a run
            self.st.outputs["_usage"] = usage.for_run(self.st, self.lc.root).as_dict()
        self.bus.emit(
            events.RUN_FINISHED,
            self.st.id,
            ticket=self.st.ticket,
            outcome=outcome,
            reason=reason,
            pr_url=self.st.pr_url,
        )

    def reassess(self) -> autonomy.RiskAssessment:
        self.st.risk = autonomy.assess(self.st.signals, self.cfg.autonomy, self.st.ceiling)
        self.bus.emit(
            events.RISK_ASSESSED, self.st.id, score=self.st.risk.score, level=self.st.risk.level, node=self.st.node
        )
        return self.st.risk

    # ---------- agent instructions ----------

    def instructions(self) -> str:
        node = self.node()
        head = self.progress()
        if self.st.finished:
            return f"{head}\n{self.st.status}: {self.st.outcome} {self.st.stop_reason or self.st.pr_url}".rstrip()
        if self.st.status == "waiting_answers":
            ask = self.out("_ask")
            qs = "\n".join(f"  {i}. {q}" for i, q in enumerate(ask.get("questions", []), 1))
            return (
                f"{head}\nQUESTIONS FOR THE USER from `{ask.get('step')}` ({ask.get('reason', '')[:300]}).\n{qs}\n"
                "Ask the user these in the chat, word for word. Append their answers to {self.answers_file} as"
                " '- Q: …  A: …' lines (empty answer = 'not sure'), then run `factory resume`."
            )
        if self.st.status == "waiting_gate":
            gate = node.gate or ""
            return (
                f"{head}\nWAITING ON A HUMAN at gate '{gate}'. Do not approve it yourself.\n"
                f"Ask the user to review, then run in their own terminal:  factory approve {gate}   (or: factory reject {gate})\n\n"
                + self.gate_summary(gate)
            )
        if node.kind != "agent":
            return f"{head}\nrunner is at an automatic step; run `factory resume`."
        skill = node.skill_file or resources.files("mobile_factory.skills").joinpath(f"{node.skill}.md")
        lines = [
            head,
            f"TASK  {node.task}",
            *(
                [
                    f"DIRECTION {self.direction_file}  (HIGH PRIORITY, from the developer: follow it and say how; it"
                    " never overrides gates, forbidden paths or the no-secrets rule)"
                ]
                if self.direction_file.is_file()
                else []
            ),
            f"SKILL {skill}",
            f"RUN   {self.dir}  (ticket.json, outputs, context/)",
            f"HOME  {self.lc.state_dir}  (taste.md, knowledge/, flows/, tickets/)",
        ]
        if self.answers_file.is_file():
            lines.append(f"ANSWERS {self.answers_file}  (the developer's answers to earlier questions: follow them)")
        if fail := self.out("_last_failure"):
            lines.append(f"LAST FAILURE ({fail['node']}): {fail['reason'][-1500:]}")
        if (build := self.dir / "context" / "build.md").is_file():
            lines.append(f"BUILD {build}  (module, variant, install/assemble/test tasks, package, deeplink: use these)")
        if self.st.kind:
            lines.append(
                f"KIND  {self.st.kind}"
                + (": the flow shows the defect before the fix" if self.st.kind == "bugfix" else "")
            )
        if self.st.adopted and node.name == self.wf.artifact("change"):
            lines.append(
                f"ADOPTED {self.st.pr_url}: the change is already on this branch. Verify it against the criteria;"
                " change code only where a criterion fails. `summary` = what this run verified or fixed"
            )
        if (unit := node.params.get("with_tests")) and (self.cfg.steps.get(TESTS) or TESTS in self.st.enabled):
            lines.append(f"UNIT  {' '.join(unit.split())}")
        if (tests := self.out("_tests")).get("ok"):
            lines.append(f"TESTS already ran by `checks` for this diff ({tests['summary'][:200]}): do not rerun them")
        lines.extend(self._delegation(node))
        lines.extend(self._device_lines(node))
        lines.append(f"SUBMIT write JSON like EXAMPLE, then: factory submit {node.name} <file.json>")
        lines.append(f"EXAMPLE {json.dumps(EXAMPLES[node.type])}")
        return "\n".join(lines)

    def _delegation(self, node: Node) -> list[str]:
        prev = self.dir / "context" / "previous.md"
        lines = [f"HINT     {prev}  (an earlier run of this ticket: why it stopped, Q&A, its plan: build on it)"]
        lines = lines if prev.is_file() and node.name == self.wf.artifact("plan") else []
        if node.params.get("solo"):
            return [*lines, "AGENT    do this step yourself in this session: no subagents"]
        model = node.model or self.cfg.agents.models.get(node.name, "inherit")
        return [
            *lines,
            f"AGENT    delegate to the `factory-{node.skill}` subagent ({model}); "
            "if your tool has no subagents, do it yourself",
        ]

    def library(self) -> Path:
        return self.lc.path(self.cfg.android.flows_dir) / maestro.LIBRARY

    def _device_lines(self, node: Node) -> list[str]:
        """The proving step writes one Maestro flow with a `# criterion N:` section per criterion and runs it."""
        if node.name != self.wf.artifact("after"):
            return []
        flow = self.flow(node.name)
        shots = self.dir / "snapshots" / "after"
        return [
            f"FLOW     write {flow}: one `# criterion N: <text>` section per criterion, assertVisible/assertNotVisible",
            *(f"START    from {f}" for f in maestro.matching(self.library(), self.art("plan").get("screens", []))),
            f"ROUTES   grep {self.library().parent / ROUTES}",
            f"RUN      (cd {shots} && {maestro.binary() or 'maestro'} --device <serial> test {flow})   (fix the flow and rerun, at most 3 rounds)",
        ]

    def progress(self) -> str:
        names = [n.name for n in self.nodes]
        i = names.index(self.st.node)
        cur = {"waiting_gate": "◆", "stopped": "✕", "done": "●"}.get(self.st.status, "◉")
        dots = "".join("●" if j < i else (cur if j == i else "○") for j in range(len(names)))
        return f"{self.st.ticket}  {dots}  {self.nodes[i].title or self.st.node} · {i + 1}/{len(names)}"

    def gate_summary(self, gate: str) -> str:
        if gate == "plan":
            t = self.art("plan")
            crit = "\n".join(f"  - {c}" for c in t.get("acceptance_criteria", []))
            return f"Plan for {self.st.ticket}: {t.get('summary', '')}\nAcceptance criteria:\n{crit}\n\n{self.st.risk.explain() if self.st.risk else ''}"
        if gate == "pr":
            p = self.out("pr_preview")
            extra = (
                f"\nOn publish the ticket moves to: {self.cfg.tracker.transitions['review']}"
                if "review" in self.cfg.tracker.transitions
                else ""
            )
            return (
                f"Draft PR preview (sha {p.get('sha')}): {p.get('title')}\n{p.get('file')}{extra}\n\n"
                + Path(p.get("file", "/dev/null")).read_text()
            )
        return gate


# ---------- automatic nodes ----------


def _build_context(e: Engine) -> None:
    """Resolved once per run (config, else discovered and cached per project + variant): agents never guess it."""
    try:
        ctx = e.platform.build_context()
    except FactoryError as err:
        ctx = {"error": str(err)[:300]}
    e.st.outputs["_build"] = ctx
    if ctx:
        f = e.dir / "context" / "build.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(f"{k:<10} {v or '(not set)'}" for k, v in ctx.items())
        other = f"`:<module>:{ctx['unit_tests'].rsplit(':', 1)[-1]}`" if ctx.get("unit_tests") else ""
        f.write_text(
            f"# Build context\n\n{rows}\n\nUse these names as they are: never guess modules or run `gradlew tasks`."
            + (f"\nUnit tests of another module: {other}.\n" if other else "\n")
        )


def _preflight(e: Engine) -> dict[str, Any]:
    _build_context(e)
    if e.node().params.get("git") is False:  # no code changes in this workflow: no branch, tree or push needed
        return {"git": False}
    if dirty := e.git.dirty():
        raise Stop("preflight-failed", f"working tree not clean: {', '.join(dirty[:5])}")
    if not e.st.base:
        raise Stop("preflight-failed", "base branch not chosen: re-run with `factory run <KEY> --base <branch>`")
    e.git.fetch(e.cfg.vcs.remote, e.st.base)
    if not github.authenticated(e.lc.root):
        raise Stop("preflight-failed", "gh is not authenticated: `gh auth login`")
    return {"base": e.st.base, "base_sha": e.git("rev-parse", f"{e.cfg.vcs.remote}/{e.st.base}")}


def route(tc: TrackerConfig, ticket_type: str, parent_type: str = "", root: Path | None = None) -> str:
    """The workflow for a ticket type (tracker.pipelines); 'parent' means the parent's workflow (sub-tasks);
    file tickets without a type are code tickets."""
    if not ticket_type:
        return classify.CODE
    known = workflow.all_workflows(root)
    pipe = tc.pipelines.get(ticket_type)
    if pipe == "parent":
        if not parent_type or tc.pipelines.get(parent_type, "parent") == "parent":
            raise Stop("ineligible", f"{ticket_type} has no parent with a workflow: run the parent, or map the type")
        return route(tc, parent_type, root=root)
    if pipe:
        if pipe not in known:
            raise Stop("ineligible", f"{ticket_type} maps to workflow '{pipe}', which does not exist")
        return pipe
    raise Stop(
        "ineligible",
        f"no workflow for ticket type {ticket_type}: add it to tracker.pipelines (known: {', '.join(known)})",
    )


def detect_workflow(tc: TrackerConfig, t: tracker.Ticket, root: Path | None = None) -> classify.Detection:
    """The Jira type's workflow unless the ticket's text clearly says otherwise (tracker.detect); code tickets also
    get their kind (bugfix, task, feature) for the branch and the work step."""
    try:
        by_type = route(tc, t.type, t.parent_type, root) if t.type else ""
    except Stop:
        by_type = ""  # unmapped type: let the text decide
    kind = classify.KIND_BY_TYPE.get(t.parent_type if tc.pipelines.get(t.type) == "parent" else t.type, "")
    if not tc.detect:
        if not by_type and t.type:
            route(tc, t.type, t.parent_type, root)  # raises the clear "no workflow" reason
        return classify.Detection(by_type or classify.CODE, "jira", "ticket type", {}, kind or "bugfix")
    family = workflow.get(by_type, root).family if by_type else ""
    found = classify.detect(family, t.summary, t.description, kind)
    if by_type and found.workflow == family:  # the text agrees with the type: keep the mapped (maybe custom) one
        found.workflow = by_type
    elif by_type and not family:  # a workflow of your own with no built-in family: the type decides
        found = classify.Detection(by_type, "jira", "ticket type", found.scores, found.kind)
    if t.type and not by_type and not any(found.scores.values()):
        route(tc, t.type, t.parent_type, root)  # an unknown type and no signal in the text: stop with the reason
    return found


def _route_ticket(lc: LoadedConfig, key: str) -> classify.Detection:
    """Pick the workflow before the run starts, so its first steps are the right ones; intake re-checks it."""
    try:
        return detect_workflow(lc.cfg.tracker, tracker.make(lc.cfg.tracker, lc.root).get(key), lc.root)
    except (FactoryError, Stop):
        return classify.Detection(classify.CODE, "jira", "", {}, "bugfix")  # intake reports the real problem


def _intake(e: Engine) -> dict[str, Any]:
    t = e.tracker.get(e.st.ticket)
    tc = e.cfg.tracker
    if tc.projects and t.key.split("-")[0] not in tc.projects:
        raise Stop("ineligible", f"{t.key} is outside tracker.projects {tc.projects}")
    found = detect_workflow(tc, t, e.lc.root)
    if e.st.workflow_source != "override" and found.workflow != e.st.pipeline:
        raise Stop("ineligible", f"{t.key} needs {found.workflow} ({found.reason}) but the run is {e.st.pipeline}")
    if e.st.workflow_source != "override":
        e.st.workflow_source, e.st.workflow_reason = found.source, found.reason
    e.st.kind = found.kind
    if blocked := t.blocked_labels(tc.block_labels):
        raise Stop("ineligible", f"label {', '.join(blocked)}")
    (e.dir / "ticket.json").write_text(t.model_dump_json(indent=2))
    return {
        "type": t.type,
        "workflow": e.st.pipeline,
        "why": e.st.workflow_reason,
        "summary": t.summary,
        "url": tracker.ticket_url(tc, t),
        "mentions_ios": t.mentions_ios(),
    }


def _branch(e: Engine) -> dict[str, Any]:
    if pr := github.open_pr(e.lc.root, e.st.ticket):
        e.git.switch_remote(pr["headRefName"], e.cfg.vcs.remote)
        e.git.fetch(e.cfg.vcs.remote, pr["baseRefName"])
        e.st.branch, e.st.base, e.st.pr_url, e.st.adopted = pr["headRefName"], pr["baseRefName"], pr["url"], True
        e.st.checkpoint = e.git.head()
        e.notify(f"adopted {pr['url']}: verifying it on {pr['headRefName']}, no new branch or PR")
        return {"branch": e.st.branch, "checkpoint": e.st.checkpoint, "adopted": pr["url"]}
    p = e.cfg.project
    pattern = p.branch_pattern_by_type.get(e.out("intake").get("type", ""), e.wf.branch)
    title = e.art("plan").get("summary") or e.out("intake").get("summary", "")  # light: no plan yet, the ticket title
    name = pattern.format(key=e.st.ticket, slug=_slug(title), kind=e.st.kind or "work")
    e.git.create_branch(name, f"{e.cfg.vcs.remote}/{e.st.base}")
    e.st.branch = name
    e.st.checkpoint = e.git.head()
    msg = ""
    if to := e.cfg.tracker.transitions.get("start"):
        msg = e.tracker.transition(e.st.ticket, to)
    return {"branch": name, "checkpoint": e.st.checkpoint, "tracker": msg}


def _criteria(ver: dict[str, Any]) -> list[dict[str, Any]]:
    return list(ver.get("acceptance_criteria") or [])


def _tested(e: Engine, ver: dict[str, Any], snaps: str) -> str:
    """Plain words a reviewer would write: what now works, with its evidence, and what the screenshots show."""
    lines = [f"- {c['criterion']}: {c['evidence']}" for c in _criteria(ver) if c.get("met")]
    lines += [f"  - {ln[2:]}" for ln in snaps.splitlines() if ln.startswith("- ")]
    return "\n".join(lines)


def _changed(e: Engine) -> tuple[list[str], list[difflib.FileDiff]]:
    """The run's change; an adopted PR with nothing new is checked as the whole PR against its base."""
    since = e.st.checkpoint
    if e.st.adopted and not e.git.changed_files(since):
        since = e.git.merge_base(f"{e.cfg.vcs.remote}/{e.st.base}")
    files = e.git.changed_files(since)
    return files, difflib.parse(e.git.diff(since, files))


def _lint_tests(e: Engine) -> tuple[str, str]:
    """Lint + unit tests of the touched modules and the extra guardrail commands: (summary, failure or '')."""
    files, _ = _changed(e)
    sources = files_hash(e.lc.root, files, "\n".join(e.cfg.guardrail.commands))
    if (prev := e.out("_tests")).get("hash") == sources and prev.get("ok"):  # a pass on the same sources stays a pass
        return f"{prev['summary']} (reused: sources unchanged since it ran)", ""
    summary, failed = _run_lint_tests(e, files)
    e.st.outputs["_tests"] = {"hash": sources, "ok": not failed, "summary": summary}
    return summary, failed


def _run_lint_tests(e: Engine, files: list[str]) -> tuple[str, str]:
    logs = e.dir / "logs"
    res = e.platform.checks(files, logs)
    if not res.ok:
        return res.summary, f"checks failed ({res.log}):\n{res.summary}"
    for cmd in e.cfg.guardrail.commands:
        r = run(["sh", "-c", cmd], e.lc.root, log=logs / f"cmd-{_slug(cmd)}.log")
        if not r.ok:
            return res.summary, f"`{cmd}` failed:\n{(r.out + r.err)[-1500:]}"
    return res.summary, ""


def _checks(e: Engine) -> dict[str, Any]:
    summary, failed = _lint_tests(e)
    if failed:
        raise Retry(failed)
    build = e.platform.build_install(e.dir / "logs")  # gradle install = `adb install -r`: app data and sign-in stay
    if not build.ok:
        raise Retry(f"build failed ({build.log}):\n{build.summary}")
    return {"checks": summary, "build": " · ".join(build.summary.splitlines()[:2])}


def _detectors(e: Engine) -> dict[str, Any]:
    """Review: the deterministic detectors only, no agents; every finding (blockers too) is a PR review note."""
    ctx = review_ctx.build(e.lc, e.git.diff(e.st.checkpoint), e.dir / "context")
    e.st.outputs["_review_ctx"] = {"summary": ctx.summary(), "files": e.git.changed_files(e.st.checkpoint)}
    return {"findings": [{**f.model_dump(), "outcome": "open"} for f in ctx.detector_findings]}


def _size(e: Engine, files: list[str]) -> None:
    """Risk counts the product code only: test files add no file or line points; new untracked files count."""
    tests = e.cfg.project.test_dirs
    code = [f for f in files if not any(d in f"/{f}" for d in tests)]
    e.st.signals.actual_files = len(code)
    e.st.signals.lines_changed = e.git.numstat(e.st.checkpoint, code) if code else 0


def _commit(e: Engine) -> dict[str, Any]:
    changed = e.git.changed_files(e.st.checkpoint)
    if e.st.adopted and not changed:
        e.notify(f"nothing to commit: {e.st.pr_url} already meets the criteria")
        return {"sha": e.git.head(), "subject": "", "amended": False, "files": [], "left_out": []}
    reviewed = set(e.out("_review_ctx").get("files", changed))
    untracked = set(e.git.untracked())
    files = [f for f in changed if not matches(f, NEVER_STAGE) and (f not in untracked or f in reviewed)]
    left = sorted(set(changed) - set(files))
    added = [
        ln for ln in e.git.diff(e.st.checkpoint, files).splitlines() if ln.startswith("+") and not ln.startswith("+++")
    ]
    if marked := [ln[1:].strip() for ln in added if TEMP_MARKER.search(ln)]:
        raise FactoryError(
            f"refusing to commit: TEMP marker in the change ({marked[0][:80]}): remove it, then `factory resume`"
        )
    amend = e.git.commits_since(e.st.checkpoint) > 0
    subject = f"{e.st.ticket}: {e.art('change')['summary']}"
    sha = e.git.commit(files, subject, amend=amend)
    e.notify(f"{'amended' if amend else 'committed'} {sha[:10]} {subject}: {', '.join(files)}")
    if left:
        e.notify(f"left out of the commit (client switch or not in the reviewed diff): {', '.join(left)}")
    _size(e, files)
    e.reassess()
    return {"sha": sha, "subject": subject, "amended": amend, "files": files, "left_out": left}


def _pr_preview(e: Engine) -> dict[str, Any]:
    if e.st.adopted:
        return _adopted_preview(e)
    ticket_url = e.out("intake").get("url", "")
    fix, ver = e.art("change"), e.art("after")
    snaps = "\n".join(
        f"- {label}: {change.strip().splitlines()[0] if change.strip() else ''}"
        for label, change in snapshot_diff(e.dir / "snapshots")
    )
    template = e.wf.pr_template
    body = (
        resources.files("mobile_factory.templates")
        .joinpath(template)
        .read_text()
        .format(
            ticket_url=ticket_url or e.st.ticket,
            changes=fix.get("changes") or fix.get("summary", ""),
            criteria="\n".join(
                f"- [{'x' if c.get('met') else ' '}] {c.get('criterion', '')}"
                for c in _criteria(ver)
                if not c.get("blocked")
            ),
            tested=_tested(e, ver, snaps),
            notes=fix.get("notes", ""),
        )
    ) + _notes(e)
    title = f"{e.st.ticket}: {fix['summary']}"
    _guard_publish(e, title, body)
    f = e.dir / "pr-body.md"
    f.write_text(body)
    return {"title": title, "file": str(f), "sha": preview_sha(title, body)}


def _adopted_preview(e: Engine) -> dict[str, Any]:
    """An adopted PR keeps its title and body: the gate approves only what gets pushed to it."""
    sha = e.out("commit").get("sha", "")
    push = e.out("commit").get("subject") or "nothing (no code change)"
    crit = "\n".join(
        f"- [{'x' if c.get('met') else ' '}] {c.get('criterion', '')}: {c.get('evidence') or c.get('reason', '')}"
        for c in _criteria(e.art("after"))
    )
    body = f"Existing PR {e.st.pr_url} ({e.st.branch} -> {e.st.base})\nPush: {push}\n\nVerified:\n{crit}\n"
    title = f"{e.st.ticket}: update {e.st.pr_url}"
    f = e.dir / "pr-body.md"
    f.write_text(body + _notes(e))
    return {"title": title, "file": str(f), "sha": preview_sha(title, f.read_text()), "head": sha}


def _notes(e: Engine) -> str:
    """What the draft PR must say out loud: blocked criteria and the detector findings."""
    blocked = [c for c in _criteria(e.art("after")) if c.get("blocked")]
    out = (
        "\n## Blocked\n" + "\n".join(f"- {c['criterion']}: {c.get('reason', '')}" for c in blocked) + "\n"
        if blocked
        else ""
    )
    findings = [rl.Finding.model_validate(f) for f in e.out("review").get("findings", [])]
    notes = [
        f"- {f.severity}: {f.suggestion} ({f.file}{f':{f.line}' if f.line else ''}){f' - {f.reason}' if f.reason else ''}"
        for f in findings
        if f.outcome in ("open", "not applied")
    ]
    return out + ("\n## Open review notes\n" + "\n".join(notes) + "\n" if notes else "")


def _guard_publish(e: Engine, title: str, body: str) -> None:
    def refuse(why: str) -> None:
        raise Stop("refused", f"REFUSED: {why}. Nothing pushed.")

    if not title.startswith(f"{e.st.ticket}: "):
        refuse(f"title must start with '{e.st.ticket}: '")
    url = e.out("intake").get("url", "")
    if url and not e.st.adopted and not body.lstrip().startswith(url):
        refuse("body must open with the ticket link")
    if e.cfg.vcs.forbid_attribution and ATTRIBUTION.search(body):
        refuse("body carries tool attribution")
    if e.git.branch() in (e.st.base, "HEAD") or e.st.ticket not in e.git.branch():
        refuse(f"on {e.git.branch()}, not the ticket branch")
    if e.git.dirty():
        refuse("uncommitted changes")
    if e.git.commits_since(e.st.checkpoint) != (0 if e.st.adopted and not e.out("commit").get("files") else 1):
        refuse("expected exactly one commit on top of the checkpoint")
    if e.out("commit").get("sha") != e.git.head():
        refuse("HEAD moved after the commit step")


def _publish(e: Engine) -> dict[str, Any]:
    p = e.out("pr_preview")
    body = Path(p["file"]).read_text()
    _guard_publish(e, p["title"], body)
    approved = e.st.gates.get("pr")
    if preview_sha(p["title"], body) != p["sha"] or not approved or approved.sha != p["sha"]:
        raise Stop("refused", "REFUSED: PR title/body differ from the approved preview. Nothing pushed.")
    if e.st.adopted:
        if e.out("commit").get("files"):
            github.push(e.lc.root, e.cfg.vcs.remote, e.st.branch)
        return {"pr_url": e.st.pr_url, "pushed": bool(e.out("commit").get("files")), "tracker": ""}
    github.push(e.lc.root, e.cfg.vcs.remote, e.st.branch)
    url = github.create_pr(
        e.lc.root, e.st.base, e.st.branch, p["title"], Path(p["file"]), draft=e.cfg.vcs.draft, label=e.cfg.vcs.pr_label
    )
    e.st.pr_url = url
    msg = (
        e.tracker.transition(e.st.ticket, e.cfg.tracker.transitions["review"])
        if "review" in e.cfg.tracker.transitions
        else ""
    )
    return {"pr_url": url, "tracker": msg}


def _handoff(e: Engine) -> dict[str, Any]:
    if (e.dir / "mock.pid").is_file():  # never leave the device on a mocking proxy
        with contextlib.suppress(Exception):
            e.platform.mock_off(e.dir)
    if e.wf.outcome != "pr":
        return {"outcome": e.st.outcome or e.wf.outcome, "run": str(e.dir)}
    e.st.outcome = "draft-pr" if e.cfg.vcs.draft else "pr"
    return {"pr_url": e.st.pr_url, "branch": e.st.branch, "snapshots": str(e.dir / "snapshots")}


def _report_preview(e: Engine) -> dict[str, Any]:
    r = e.art("plan")
    lines = [f"**Question:** {r.get('question', '')}", "", f"**Answer:** {r.get('answer', '')}", "", "**Findings**"]
    lines += [f"- {f}" for f in r.get("findings", [])]
    for o in r.get("options", []):
        lines += ["", f"**Option: {o['name']}**"] + [f"- + {p}" for p in o.get("pros", [])]
        lines += [f"- - {c}" for c in o.get("cons", [])]
    if r.get("recommendation"):
        lines += ["", f"**Recommendation:** {r['recommendation']}"]
    if r.get("open_questions"):
        lines += ["", "**Open questions**", *[f"- {q}" for q in r["open_questions"]]]
    f = e.dir / "report.md"
    f.write_text("\n".join(lines) + "\n")
    return {"file": str(f), "sha": preview_sha(e.st.ticket, f.read_text())}


def _post_report_step(e: Engine) -> dict[str, Any]:
    p = e.out("report_preview")
    body = Path(p["file"]).read_text()
    approved = e.st.gates.get("report")
    if preview_sha(e.st.ticket, body) != p["sha"] or not approved or approved.decision == "rejected":
        raise Stop("refused", "REFUSED: the report differs from the approved preview. Nothing posted.")
    msg = e.tracker.comment(e.st.ticket, body)
    e.st.outcome = "report-posted"
    return {"tracker": msg}


def _stories_from(e: Engine) -> list[dict[str, Any]]:
    src = e.node().params.get("source") or e.wf.artifact("plan")
    return list(e.out(src).get("stories", []))


def _create_tickets(e: Engine) -> dict[str, Any]:
    stories = _stories_from(e)
    project = e.st.ticket.split("-")[0]
    parent = e.st.ticket if e.out("intake").get("type") == "Epic" else ""
    keys = []
    for st in stories:
        ac = "\n".join(f"- {c}" for c in st.get("acceptance_criteria", []))
        body = f"{st.get('description', '')}\n\nAcceptance criteria:\n{ac}\n\nFrom {e.st.ticket}."
        keys.append(e.tracker.create(project, st.get("type", "Story"), st["summary"], body.strip(), parent))
    (e.dir / "created.md").write_text(
        "\n".join(f"- {k}: {s['summary']}" for k, s in zip(keys, stories, strict=True)) + "\n"
    )
    if e.wf.outcome == "tickets":
        e.st.outcome = "tickets-created"
    return {"created": keys}


AUTO: dict[str, Callable[[Engine], dict[str, Any] | None]] = {
    "preflight": _preflight,
    "intake": _intake,
    "branch": _branch,
    "checks": _checks,
    "detectors": _detectors,
    "commit": _commit,
    "pr_preview": _pr_preview,
    "publish": _publish,
    "handoff": _handoff,
    "report_preview": _report_preview,
    "post_report": _post_report_step,
    "create_tickets": _create_tickets,
}


# ---------- agent post-processing ----------


def _check_change(e: Engine) -> None:
    """The change stays inside the allowed paths; its size and modules feed the risk score."""
    files, diffs = _changed(e)
    if not files:
        raise FactoryError("no changes found since the checkpoint")
    rep = limits.check(e.cfg.project, files, diffs, e.git.deleted_files(e.st.checkpoint))
    if not rep.ok:
        raise Stop("forbidden-path", "; ".join(rep.lines()))
    s = e.st.signals
    _size(e, files)
    s.tests_added = any(d in f"/{f}" for f in files for d in e.cfg.project.test_dirs)
    s.modules_touched = len(e.platform.modules_for(files)) or 1
    e.st.outputs.pop("_last_failure", None)
    e.reassess()


def _post_work(e: Engine, o: Any) -> str | None:
    w: WorkOut = o
    if w.questions:
        raise Ask(w.notes or "the ticket is unclear", w.questions)
    if w.stop:
        raise Stop("ineligible", w.stop)
    if len(w.summary) < 5 or not w.acceptance_criteria:
        raise FactoryError(f"{e.st.node} needs a `summary` (commit subject) and its `acceptance_criteria`")
    if not w.flow or not Path(w.flow).is_file():
        raise FactoryError(f"flow not found: {w.flow or '(empty)'}: write it at the FLOW line and run it")
    _check_change(e)
    if failed := [c for c in w.acceptance_criteria if not c.met and not c.blocked]:
        raise Retry("criteria not met: " + "; ".join(f"{c.criterion} ({c.evidence})" for c in failed))
    return None


def _post_research(e: Engine, o: Any) -> str | None:
    return None


def _post_split(e: Engine, o: Any) -> str | None:
    s: SplitOut = o
    if too_vague := [x.summary for x in s.stories if not x.acceptance_criteria]:
        raise FactoryError("every story needs acceptance criteria: " + "; ".join(too_vague))
    return None


def _post_spec(e: Engine, o: Any) -> str | None:
    sp: SpecOut = o
    if sp.verdict == "needs-info":
        raise Ask(sp.reason or "the spec needs answers first", sp.questions or ["What is missing from the ticket?"])
    if not sp.acceptance_criteria:
        raise FactoryError("the spec needs acceptance criteria for the first slice")
    e.st.signals.category = "new-app"
    e.reassess()
    return None


def _post_architecture(e: Engine, o: Any) -> str | None:
    return None


def _post_custom(e: Engine, o: Any) -> str | None:
    c: CustomOut = o
    if not c.ok:
        if e.node().retry_to:
            raise Retry(f"{e.st.node}: {c.summary} {c.details}".strip())
        raise Stop("failed", f"{e.st.node}: {c.summary}")
    return None


def check_workflow(wf: workflow.Workflow) -> None:
    """Every step's type must be one the engine knows: agent types have a schema and a check, auto types an action."""
    bad = [
        f"{s.name} ({s.kind} type {s.type})"
        for s in wf.steps
        if (s.kind == "agent" and (s.type not in POST or s.type not in MODELS))
        or (s.kind == "auto" and s.type not in AUTO)
    ]
    if bad:
        raise FactoryError(
            f"workflow {wf.name} ({wf.source}): unknown step types: {', '.join(bad)}. Agent types: "
            f"{', '.join(sorted(POST))}; auto types: {', '.join(sorted(AUTO))}"
        )


POST: dict[str, Callable[[Engine, Any], str | None]] = {
    "research": _post_research,
    "split": _post_split,
    "spec": _post_spec,
    "architecture": _post_architecture,
    "custom": _post_custom,
    "work": _post_work,
}
