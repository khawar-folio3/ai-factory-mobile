from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
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
from .guardrail import context as review_ctx
from .guardrail import diff as difflib
from .guardrail import limits
from .guardrail import rules as rl
from .integrations import github, tracker
from .outputs import (
    EXAMPLES,
    MODELS,
    AcceptOut,
    BaselineOut,
    FixOut,
    PlanOut,
    ReproOut,
    ReviewOut,
    SpecOut,
    SplitOut,
    TriageOut,
    VerifyOut,
)
from .platforms import make as make_platform
from .platforms.base import Platform, snapshot_diff
from .proc import run
from .state import GateRecord, RunState, RunStore, new_run_id

ATTRIBUTION = re.compile(r"claude|chatgpt|copilot|cursor|generated (with|by)|co-authored-by|🤖", re.I)


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

    # ---------- lifecycle ----------

    @classmethod
    def start(
        cls,
        lc: LoadedConfig,
        ticket: str,
        ceiling: int | None = None,
        base: str | None = None,
        workflow_name: str | None = None,
    ) -> Engine:
        store = RunStore(lc.runs_dir)
        for other in store.all():
            if other.ticket == ticket and not other.finished:
                raise FactoryError(f"{ticket} already has an open run {other.id}: `factory resume` or `factory abort`")
        wf = workflow.get(workflow_name or _route_ticket(lc, ticket))
        nodes = wf.steps
        st = RunState(
            id=new_run_id(ticket),
            ticket=ticket,
            created_at=events.now(),
            updated_at=events.now(),
            pipeline=wf.name,
            workflow_source="override" if workflow_name else "",
            workflow_reason="chosen with --workflow" if workflow_name else "",
            node=nodes[0].name,
            ceiling=min(wf.max_level, lc.cfg.autonomy.ceiling if ceiling is None else ceiling),
            base=base or ("" if lc.cfg.project.base_branch == "ask" else lc.cfg.project.base_branch),
        )
        eng = cls(lc, st)
        eng.save()
        eng.bus.emit(events.RUN_STARTED, st.id, ticket=ticket, ceiling=st.ceiling)
        return eng

    @classmethod
    def load(cls, lc: LoadedConfig, run_id: str | None = None) -> Engine:
        return cls(lc, RunStore(lc.runs_dir).load(run_id))

    def save(self) -> None:
        self.store.save(self.st)

    @property
    def wf(self) -> workflow.Workflow:
        return workflow.get(self.st.pipeline)

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
                if self.st.status == "waiting_gate":
                    rec = self.st.gates[self.node().gate or ""]
                    if rec.decision == "pending":
                        break
                    if rec.decision == "rejected":
                        raise Stop("rejected", f"gate {rec.gate} rejected: {rec.reason or 'no reason given'}")
                    self._complete_node()
                    continue
                node = self.node()
                if node.kind == "agent":
                    if self.st.status != "waiting_agent":
                        self.st.status = "waiting_agent"
                        self.bus.emit(events.NODE_STARTED, self.st.id, node=node.name, title=node.title)
                        self.bus.emit(events.AGENT_WAITING, self.st.id, node=node.name)
                    break
                self.bus.emit(events.NODE_STARTED, self.st.id, node=node.name, title=node.title)
                result = AUTO[node.type](self)
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
            raise FactoryError(f"{node_name} output invalid:\n{e}\nSchema: `factory schema {node_name}`") from e
        try:
            jump = POST[node.type](self, parsed)
            self.st.outputs[node_name] = parsed.model_dump()
            if jump:
                self.bus.emit(events.NODE_COMPLETED, self.st.id, node=node_name, next=jump)
                self.goto(jump)
            else:
                self._after_node(node)
        except Retry as r:
            self._retry(r.reason)
        except Stop as s:
            self.finish("stopped", s.outcome, s.reason)
            self.save()
            return self.st
        self.save()
        return self.advance()

    def _after_node(self, node: Node) -> None:
        if node.gate and self._gate_needed(node.gate):
            self.st.status = "waiting_gate"
            code = secrets.token_hex(2)
            self.st.gates[node.gate] = GateRecord(gate=node.gate, code=code, sha=self._gate_sha(node.gate))
            self.bus.emit(
                events.GATE_WAITING,
                self.st.id,
                gate=node.gate,
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
            self.bus.emit(events.GATE_DECIDED, self.st.id, gate=node.gate, decision="auto", level=self.st.level)
        self._complete_node()

    def _complete_node(self) -> None:
        self.bus.emit(events.NODE_COMPLETED, self.st.id, node=self.st.node)
        if self.st.node == self.nodes[-1].name:
            self.finish("done", self.st.outcome or self.wf.outcome, "")
        else:
            self._next_node()

    def _gate_needed(self, gate: str) -> bool:
        if gate == "review" and any(
            f.get("outcome") == "dismissed" and f.get("severity") in ("blocker", "major")
            for f in self.out("review").get("findings", [])
        ):
            return True  # a dismissed blocker/major always gets human eyes
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
        self.bus.emit(events.GATE_DECIDED, self.st.id, gate=gate, decision=rec.decision, by=by)
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
        self.bus.emit(events.NODE_FAILED, self.st.id, node=self.st.node, reason=reason, attempt=self.st.fix_attempts)
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
        if self.st.status == "waiting_gate":
            gate = node.gate or ""
            return (
                f"{head}\nWAITING ON A HUMAN at gate '{gate}'. Do not approve it yourself.\n"
                f"Ask the user to review, then run in their own terminal:  factory approve {gate}   (or: factory reject {gate})\n\n"
                + self.gate_summary(gate)
            )
        if node.kind != "agent":
            return f"{head}\nrunner is at an automatic step; run `factory resume`."
        skill = resources.files("mobile_factory.skills").joinpath(f"{node.skill}.md")
        lines = [
            head,
            f"TASK  {node.task}",
            f"SKILL {skill}",
            f"RUN   {self.dir}  (ticket.json, outputs, context/)",
            f"HOME  {self.lc.state_dir}  (taste.md, knowledge/, flows/, tickets/)",
        ]
        if fail := self.out("_last_failure"):
            lines.append(f"LAST FAILURE ({fail['node']}): {fail['reason'][-1500:]}")
        if node.type == "review":
            lines.append(f"CONTEXT {self.dir / 'context'}  ->  {self.out('_review_ctx').get('summary', '')}")
        lines.extend(self._delegation(node))
        lines.append(
            f"SUBMIT write JSON then: factory submit {node.name} <file.json>   (schema: factory schema {node.name})"
        )
        lines.append(f"EXAMPLE {json.dumps(EXAMPLES[node.type])}")
        return "\n".join(lines)

    def _delegation(self, node: Node) -> list[str]:
        step = node.name
        models = {**({step: node.model} if node.model else {}), **self.cfg.agents.models}
        parts = list(node.parallel)
        lines = []
        done = self._parts_done(parts)
        if parts and done:
            lines.append(
                "PARTS    already done alongside an earlier step, for this exact diff: " + ", ".join(map(str, done))
            )
            lines.append(f"THEN     factory-{step} merges those files, dedupes, applies fixes and writes the output")
        elif parts:
            out = self.dir / "context"
            how = "start ALL of these in one message (parallel)" if self.cfg.agents.parallel else "run these in turn"
            lines.append(f"PARALLEL {how}; each writes findings JSON (schema: factory schema review), read-only:")
            lines += [
                f"  factory-{p} ({models.get(p, 'inherit')})  ->  {out / f'{p}.json'}"
                f"  (description: {p.removeprefix('review-')})"
                for p in parts
            ]
            lines.append(f"THEN     factory-{step} merges those files, dedupes, applies fixes and writes the output")
        lines.append(
            f"AGENT    delegate to the `factory-{step}` subagent ({models.get(step, 'inherit')}); "
            "if your tool has no subagents, do it yourself"
        )
        for helper in node.alongside if self.cfg.agents.parallel else ():
            lines.append(
                f"ALONGSIDE start `factory-{helper}` ({models.get(helper, 'inherit')}) in the SAME message, read-only  "
                f"->  {self.dir / 'context' / f'{helper}.json'}  (description: {helper})"
            )
        if node.scout:
            scout = self.dir / "context" / f"{step}-scout.md"
            if scout.is_file():
                lines.append(f"HINT     {scout}  (scout findings: start there, search again only for what it lacks)")
            else:
                lines.insert(
                    0,
                    f"SCOUT    first start `factory-scout` ({models.get('scout', 'haiku')}) with what `{step}` needs to "
                    f"know (greps, git history, gradle/adb output)  ->  {scout}  (description: scout {step}); "
                    "then delegate the step",
                )
        if step == self.wf.artifact("change"):
            for name, what in (("locate", "code map"), ("history", "the code's past: regressions, earlier fixes")):
                if (hint := self.dir / "context" / f"{name}.json").is_file():
                    lines.append(f"HINT     {hint}  ({what}: start there, confirm before editing)")
        return lines

    def _parts_done(self, parts: list[str]) -> list[Path]:
        """Review part files written for the current diff (newer than diff.patch); else [] and they run again."""
        ctx = self.dir / "context"
        files = [ctx / f"{p}.json" for p in parts]
        diff = ctx / "diff.patch"
        if not parts or not diff.is_file() or not all(f.is_file() for f in files):
            return []
        return files if all(f.stat().st_mtime >= diff.stat().st_mtime for f in files) else []

    def progress(self) -> str:
        names = [n.name for n in self.nodes]
        i = names.index(self.st.node)
        cur = {"waiting_gate": "◆", "stopped": "✕", "done": "●"}.get(self.st.status, "◉")
        dots = "".join("●" if j < i else (cur if j == i else "○") for j in range(len(names)))
        risk = f"risk {self.st.risk.score} · L{self.st.level}" if self.st.risk else f"ceiling L{self.st.ceiling}"
        return (
            f"{self.st.ticket}  {dots}  {self.st.node} ({i + 1}/{len(names)}) · {self.st.pipeline} · {risk}"
            f" · run {self.st.id}"
        )

    def gate_summary(self, gate: str) -> str:
        if gate == "plan":
            t = self.art("plan")
            plan = "\n".join(f"  - {p}" for p in t.get("plan", []))
            why = (
                "Acceptance criteria:\n" + "\n".join(f"  - {c}" for c in t.get("acceptance_criteria", []))
                if t.get("acceptance_criteria")
                else f"Root cause guess: {t.get('root_cause_hypothesis', '')}"
            )
            return f"Plan for {self.st.ticket}: {t.get('summary', '')}\n{why}\n{plan}\n\n{self.st.risk.explain() if self.st.risk else ''}"
        if gate == "repro":
            r = self.art("before")
            return f"Reproduced: {r.get('reproduced')} (confidence {r.get('confidence')})\nSteps: {'; '.join(r.get('steps', []))}\nSnapshots: {self.dir / 'snapshots' / 'before'}"
        if gate == "diff":
            stat = self.git("diff", "--stat", self.st.checkpoint, check=False) if self.st.checkpoint else ""
            return f"Change: {self.art('change').get('summary', '')}\n{stat}\n\n{self.st.risk.explain() if self.st.risk else ''}"
        if gate == "review":
            return rl.table([rl.Finding.model_validate(f) for f in self.out("review").get("findings", [])])
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


def _preflight(e: Engine) -> dict[str, Any]:
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


def route(tc: TrackerConfig, ticket_type: str, parent_type: str = "") -> str:
    """The workflow for a ticket type (tracker.pipelines); 'parent' means the parent's workflow (sub-tasks);
    file tickets without a type are bug fixes."""
    if not ticket_type:
        return "bugfix"
    known = workflow.all_workflows()
    pipe = tc.pipelines.get(ticket_type)
    if pipe == "parent":
        if not parent_type or tc.pipelines.get(parent_type, "parent") == "parent":
            raise Stop("ineligible", f"{ticket_type} has no parent with a workflow: run the parent, or map the type")
        return route(tc, parent_type)
    if pipe:
        if pipe not in known:
            raise Stop("ineligible", f"{ticket_type} maps to workflow '{pipe}', which does not exist")
        return pipe
    raise Stop(
        "ineligible",
        f"no workflow for ticket type {ticket_type}: add it to tracker.pipelines (known: {', '.join(known)})",
    )


def detect_workflow(tc: TrackerConfig, t: tracker.Ticket) -> classify.Detection:
    """The Jira type's workflow unless the ticket's text clearly says otherwise (tracker.detect)."""
    try:
        by_type = route(tc, t.type, t.parent_type) if t.type else ""
    except Stop:
        by_type = ""  # unmapped type: let the text decide
    if not tc.detect:
        if not by_type and t.type:
            route(tc, t.type, t.parent_type)  # raises the clear "no workflow" reason
        return classify.Detection(by_type or "bugfix", "jira", "ticket type", {})
    found = classify.detect(by_type, t.summary, t.description)
    if t.type and not by_type and not any(found.scores.values()):
        route(tc, t.type, t.parent_type)  # an unknown type and no signal in the text: stop with the clear reason
    return found


def _route_ticket(lc: LoadedConfig, key: str) -> str:
    """Pick the workflow before the run starts, so its first steps are the right ones; intake re-checks it."""
    try:
        return detect_workflow(lc.cfg.tracker, tracker.make(lc.cfg.tracker, lc.root).get(key)).workflow
    except (FactoryError, Stop):
        return "bugfix"  # intake reports the real problem


def _intake(e: Engine) -> dict[str, Any]:
    t = e.tracker.get(e.st.ticket)
    tc = e.cfg.tracker
    if tc.projects and t.key.split("-")[0] not in tc.projects:
        raise Stop("ineligible", f"{t.key} is outside tracker.projects {tc.projects}")
    found = detect_workflow(tc, t)
    if e.st.workflow_source != "override" and found.workflow != e.st.pipeline:
        raise Stop("ineligible", f"{t.key} needs {found.workflow} ({found.reason}) but the run is {e.st.pipeline}")
    if e.st.workflow_source != "override":
        e.st.workflow_source, e.st.workflow_reason = found.source, found.reason
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
    p = e.cfg.project
    pattern = p.branch_pattern_by_type.get(e.out("intake").get("type", ""), e.wf.branch)
    name = pattern.format(key=e.st.ticket, slug=_slug(e.art("plan").get("summary", "")))
    e.git.create_branch(name, f"{e.cfg.vcs.remote}/{e.st.base}")
    e.st.branch = name
    e.st.checkpoint = e.git.head()
    msg = ""
    if to := e.cfg.tracker.transitions.get("start"):
        msg = e.tracker.transition(e.st.ticket, to)
    return {"branch": name, "checkpoint": e.st.checkpoint, "tracker": msg}


def _tested(e: Engine, rep: dict[str, Any], ver: dict[str, Any], snaps: str) -> str:
    """Plain words a reviewer would write: where it was reproduced, what now works, what else was checked."""
    steps = "; ".join(rep.get("steps", []))
    if ver.get("criteria"):
        lines = [f"- Checked on {e.cfg.android.variant} (emulator): {steps}" if steps else ""]
        lines += [f"- {c['criterion']}: {c['evidence']}" for c in ver.get("criteria", []) if c.get("met")]
        lines += [f"  - {ln[2:]}" for ln in snaps.splitlines() if ln.startswith("- ")]
        return "\n".join(ln for ln in lines if ln)
    lines = [f"- Reproduced on {e.cfg.android.variant} (emulator): {steps}" if steps else ""]
    if ver.get("defect_fixed"):
        lines.append("- After the fix the issue no longer occurs")
    if ver.get("adjacent_unchanged"):
        lines.append("- Related screens behave as before")
    lines += [f"  - {ln[2:]}" for ln in snaps.splitlines() if ln.startswith("- ")]
    return "\n".join(ln for ln in lines if ln)


def _changed(e: Engine) -> tuple[list[str], list[difflib.FileDiff]]:
    files = e.git.changed_files(e.st.checkpoint)
    return files, difflib.parse(e.git.diff(e.st.checkpoint, files))


def _checks(e: Engine) -> dict[str, Any]:
    files, _ = _changed(e)
    logs = e.dir / "logs"
    res = e.platform.checks(files, logs)
    if not res.ok:
        raise Retry(f"checks failed ({res.log}):\n{res.summary}")
    for cmd in e.cfg.guardrail.commands:
        r = run(["sh", "-c", cmd], e.lc.root, log=logs / f"cmd-{_slug(cmd)}.log")
        if not r.ok:
            raise Retry(f"`{cmd}` failed:\n{(r.out + r.err)[-1500:]}")
    build = e.platform.build_install(logs)
    if not build.ok:
        raise Retry(f"build failed ({build.log}):\n{build.summary}")
    _prepare_review(e)  # reviewers can start alongside verify
    return {"checks": res.summary, "build": build.summary.splitlines()[0]}


def _prepare_review(e: Engine) -> None:
    """The guardrail context for the current diff: built after checks so reviewers can start alongside verify.
    An unchanged diff is left alone, so the part files written for it (newer than diff.patch) stay valid."""
    patch = e.git.diff(e.st.checkpoint)
    sha = hashlib.sha256(patch.encode()).hexdigest()
    if e.out("_review_ctx").get("patch_sha") == sha and (e.dir / "context" / "diff.patch").is_file():
        return
    ctx = review_ctx.build(e.lc, patch, e.dir / "context")
    e.st.outputs["_review_ctx"] = {
        "summary": ctx.summary(),
        "detector": [f.model_dump() for f in ctx.detector_findings],
        "patch_sha": sha,
    }


def _commit(e: Engine) -> dict[str, Any]:
    files = e.git.changed_files(e.st.checkpoint)
    amend = e.git.commits_since(e.st.checkpoint) > 0
    subject = f"{e.st.ticket}: {e.art('change')['summary']}"
    sha = e.git.commit(files, subject, amend=amend)
    e.st.signals.actual_files, e.st.signals.lines_changed = len(files), e.git.numstat(e.st.checkpoint)
    e.reassess()  # guardrail rounds can grow the diff
    _prepare_review(e)  # usually unchanged since checks: the parts done alongside verify still count
    return {"sha": sha, "subject": subject, "amended": amend, "files": files}


def _pr_preview(e: Engine) -> dict[str, Any]:
    ticket_url = e.out("intake").get("url", "")
    fix, rep, ver = e.art("change"), e.art("before"), e.art("after")
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
            root_cause=fix.get("root_cause", ""),
            changes=fix.get("changes", ""),
            criteria="\n".join(
                f"- [{'x' if c.get('met') else ' '}] {c.get('criterion', '')}" for c in ver.get("criteria", [])
            ),
            tested=_tested(e, rep, ver, snaps),
            notes=fix.get("notes", ""),
        )
    )
    title = f"{e.st.ticket}: {fix['summary']}"
    _guard_publish(e, title, body)
    f = e.dir / "pr-body.md"
    f.write_text(body)
    return {"title": title, "file": str(f), "sha": preview_sha(title, body)}


def _guard_publish(e: Engine, title: str, body: str) -> None:
    def refuse(why: str) -> None:
        raise Stop("refused", f"REFUSED: {why}. Nothing pushed.")

    if not title.startswith(f"{e.st.ticket}: "):
        refuse(f"title must start with '{e.st.ticket}: '")
    url = e.out("intake").get("url", "")
    if url and not body.lstrip().startswith(url):
        refuse("body must open with the ticket link")
    if e.cfg.vcs.forbid_attribution and ATTRIBUTION.search(body):
        refuse("body carries tool attribution")
    if e.git.branch() in (e.st.base, "HEAD") or e.st.ticket not in e.git.branch():
        refuse(f"on {e.git.branch()}, not the ticket branch")
    if e.git.dirty():
        refuse("uncommitted changes")
    if e.git.commits_since(e.st.checkpoint) != 1:
        refuse("expected exactly one commit on top of the checkpoint")
    if (
        rl.verdict([rl.Finding.model_validate(f) for f in e.out("review").get("findings", [])])
        != "READY FOR HUMAN REVIEW"
    ):
        refuse("guardrail verdict is not READY")
    if e.out("commit").get("sha") != e.git.head():
        refuse("HEAD moved after the guardrail review")


def _publish(e: Engine) -> dict[str, Any]:
    p = e.out("pr_preview")
    body = Path(p["file"]).read_text()
    _guard_publish(e, p["title"], body)
    approved = e.st.gates.get("pr")
    if preview_sha(p["title"], body) != p["sha"] or not approved or approved.sha != p["sha"]:
        raise Stop("refused", "REFUSED: PR title/body differ from the approved preview. Nothing pushed.")
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
    "commit": _commit,
    "pr_preview": _pr_preview,
    "publish": _publish,
    "handoff": _handoff,
    "report_preview": _report_preview,
    "post_report": _post_report_step,
    "create_tickets": _create_tickets,
}


# ---------- agent post-processing ----------


def _post_triage(e: Engine, o: Any) -> str | None:
    t: TriageOut = o
    if t.verdict != "eligible":
        if t.questions:
            (e.dir / "questions.md").write_text("\n".join(f"- {q}" for q in t.questions) + "\n")
        raise Stop(t.verdict, t.reason)
    s = e.st.signals
    s.category, s.estimated_files, s.public_api_change, s.risk_classes = (
        t.category,
        t.estimated_files,
        t.public_api_change,
        t.risk_classes,
    )
    e.reassess()
    return None


def _post_repro(e: Engine, o: Any) -> str | None:
    r: ReproOut = o
    missing = [lb for lb in r.snapshots if not (e.dir / "snapshots" / "before" / f"{lb}.txt").is_file()]
    if missing:
        raise FactoryError(f"snapshots not found for labels {missing}: capture with `factory snap before <label>`")
    e.st.signals.reproduced, e.st.signals.repro_confidence = r.reproduced, r.confidence
    if not r.reproduced and e.cfg.limits.require_repro:
        raise Stop("not-reproduced", "could not reproduce; see the reproduce output")
    e.reassess()
    return None


def _post_fix(e: Engine, o: Any) -> str | None:
    f: FixOut = o
    files, diffs = _changed(e)
    if not files:
        raise FactoryError("no changes found since the checkpoint")
    rep = limits.check(e.cfg.project, files, diffs, e.git.deleted_files(e.st.checkpoint))
    if not rep.ok:
        raise Stop("forbidden-path", "; ".join(rep.lines()))
    s = e.st.signals
    s.actual_files, s.lines_changed, s.tests_added = len(files), e.git.numstat(e.st.checkpoint), f.tests_added
    s.modules_touched = len(e.platform.modules_for(files)) or 1
    e.st.outputs.pop("_last_failure", None)
    e.reassess()
    return None


def _post_verify(e: Engine, o: Any) -> str | None:
    v: VerifyOut = o
    missing = [lb for lb in v.snapshots if not (e.dir / "snapshots" / "after" / f"{lb}.txt").is_file()]
    if missing:
        raise FactoryError(f"snapshots not found for labels {missing}: capture with `factory snap after <label>`")
    if not (v.defect_fixed and v.adjacent_unchanged):
        raise Retry(
            f"verification failed: defect_fixed={v.defect_fixed}, adjacent_unchanged={v.adjacent_unchanged}. {v.notes}"
        )
    return None


def _post_plan(e: Engine, o: Any) -> str | None:
    p: PlanOut = o
    if p.verdict == "too-big":
        (e.dir / "subtasks.md").write_text("\n".join(f"- {t}" for t in p.subtasks) + "\n")
        raise Stop("too-big", f"{p.reason} — proposed sub-tasks in {e.dir / 'subtasks.md'}")
    if p.verdict != "eligible":
        if p.questions:
            (e.dir / "questions.md").write_text("\n".join(f"- {q}" for q in p.questions) + "\n")
        raise Stop(p.verdict, p.reason)
    if not p.acceptance_criteria:
        raise FactoryError("an eligible plan needs acceptance criteria: from the ticket, or drafted for approval")
    s = e.st.signals
    s.category, s.estimated_files, s.public_api_change, s.risk_classes = (
        "feature",
        p.estimated_files,
        p.public_api_change,
        p.risk_classes,
    )
    e.reassess()
    return None


def _post_baseline(e: Engine, o: Any) -> str | None:
    b: BaselineOut = o
    missing = [lb for lb in b.snapshots if not (e.dir / "snapshots" / "before" / f"{lb}.txt").is_file()]
    if missing:
        raise FactoryError(f"snapshots not found for labels {missing}: capture with `factory snap before <label>`")
    return None


def _post_accept(e: Engine, o: Any) -> str | None:
    a: AcceptOut = o
    missing = [lb for lb in a.snapshots if not (e.dir / "snapshots" / "after" / f"{lb}.txt").is_file()]
    if missing:
        raise FactoryError(f"snapshots not found for labels {missing}: capture with `factory snap after <label>`")
    wanted = e.art("plan").get("acceptance_criteria", [])
    checked = {c.criterion.strip().lower() for c in a.criteria}
    unchecked = [c for c in wanted if c.strip().lower() not in checked]
    if unchecked:
        raise FactoryError("acceptance criteria not checked: " + "; ".join(unchecked))
    if failed := [c for c in a.criteria if not c.met]:
        raise Retry("acceptance failed: " + "; ".join(f"{c.criterion} ({c.evidence})" for c in failed))
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
        (e.dir / "questions.md").write_text("\n".join(f"- {q}" for q in sp.questions) + "\n")
        raise Stop("needs-info", sp.reason or "the spec needs answers first")
    if not sp.acceptance_criteria:
        raise FactoryError("the spec needs acceptance criteria for the first slice")
    e.st.signals.category = "new-app"
    e.reassess()
    return None


def _post_architecture(e: Engine, o: Any) -> str | None:
    return None


def _post_review(e: Engine, o: Any) -> str | None:
    r: ReviewOut = o
    detector = [rl.Finding.model_validate(f) for f in e.out("_review_ctx").get("detector", [])]
    unanswered = [
        d for d in detector if d.severity in ("blocker", "major") and not any(d.same_spot(f) for f in r.findings)
    ]
    if unanswered:
        raise FactoryError(
            "detector findings without an outcome: " + ", ".join(f"{d.rule}@{d.file}:{d.line}" for d in unanswered)
        )
    e.st.review_rounds += 1
    (e.dir / f"review-round-{e.st.review_rounds}.json").write_text(r.model_dump_json(indent=2))
    applied = [f for f in r.findings if f.outcome == "applied"]
    if rl.verdict(r.findings) == "NEEDS WORK":
        raise Stop("gate-needs-work", "open blocker/major findings:\n" + rl.table(r.findings))
    if applied:
        if e.st.review_rounds >= e.cfg.limits.max_review_rounds:
            raise Stop("gate-needs-work", f"review still changing code after {e.st.review_rounds} rounds")
        return "checks"  # re-run checks, verify and commit (amend), then review again
    return None


POST: dict[str, Callable[[Engine, Any], str | None]] = {
    "triage": _post_triage,
    "reproduce": _post_repro,
    "fix": _post_fix,
    "verify": _post_verify,
    "review": _post_review,
    "plan": _post_plan,
    "baseline": _post_baseline,
    "implement": _post_fix,
    "accept": _post_accept,
    "research": _post_research,
    "split": _post_split,
    "spec": _post_spec,
    "architecture": _post_architecture,
    "scaffold": _post_fix,
}
