"""Which workflow a ticket really needs, from its text: Jira types are often wrong (bugs filed as Tasks, research
filed as Stories). Deterministic: phrases a person would use for each kind of work, weighed against the Jira type."""

from __future__ import annotations

import re
from dataclasses import dataclass

# workflow -> (weight, pattern). Patterns match lower-cased summary + description.
SIGNALS: dict[str, list[tuple[int, str]]] = {
    "bugfix": [
        (3, r"steps to reproduce|repro steps|\bexpected( result| behaviou?r)?:|\bactual( result| behaviou?r)?:"),
        (2, r"\b(crash(es|ed)?|anr|exception|stack ?trace|regression|broken|not working|doesn'?t work|fails? to)\b"),
        (1, r"\b(bug|error|wrong|incorrect|glitch|freez(e|es)|cut off|clipped|overlap(s|ping)?)\b"),
    ],
    "feature": [
        (3, r"\bas an? [\w -]+,? i (want|need|would like)\b|acceptance criteria|\buser story\b"),
        (2, r"\b(add|allow|enable|introduce|support|show|display|let users?)\b.*\b(new|option|screen|button|filter)\b"),
        (1, r"\b(new feature|feature request|design|figma|mockup)\b"),
    ],
    "task": [
        (3, r"\b(upgrade|bump|update) [\w.-]+ (to|from) v?\d|\bmigrat(e|ion) (to|from|off)\b|\btarget ?sdk\b"),
        (2, r"\b(refactor|clean ?up|remove (deprecated|unused|dead)|tech(nical)? debt|lint|rename|extract)\b"),
        (1, r"\b(dependency|dependencies|gradle|ci|pipeline|config(uration)?|library)\b"),
    ],
    "spike": [
        (3, r"\b(spike|investigate|investigation|research|evaluate|feasibility|proof of concept|poc)\b"),
        (2, r"\b(should we|can we|is it possible|compare|options for|find out)\b"),
    ],
    "epic": [(3, r"\b(epic|initiative|roadmap|phase \d|milestone)\b")],
    "new-app": [(3, r"\b(new app|from scratch|greenfield|build an? (\w+ )?app|create an? (\w+ )?app)\b")],
}
OVERRIDE = 4  # the text must beat the Jira type's workflow by this much to override it


@dataclass
class Detection:
    workflow: str
    source: str  # "jira" | "text" | "override"
    reason: str
    scores: dict[str, int]


def scores(text: str) -> dict[str, int]:
    t = text.lower()
    return {wf: sum(w for w, pat in sigs if re.search(pat, t)) for wf, sigs in SIGNALS.items()}


def detect(jira_workflow: str, summary: str, description: str) -> Detection:
    """`jira_workflow` is what the Jira type maps to ('' when unknown). The text wins when it is clearly different."""
    s = scores(f"{summary}\n{description}")
    best, top = max(s.items(), key=lambda kv: kv[1])
    if not jira_workflow:
        if top == 0:
            return Detection("bugfix", "text", "no type and no clear signal: treated as a bug fix", s)
        return Detection(
            best, "text", f"no ticket type; the text reads like {best} ({_why(best, summary, description)})", s
        )
    if best != jira_workflow and top - s.get(jira_workflow, 0) >= OVERRIDE:
        return Detection(
            best, "text", f"the text reads like {best}, not {jira_workflow} ({_why(best, summary, description)})", s
        )
    return Detection(jira_workflow, "jira", "ticket type", s)


def _why(wf: str, summary: str, description: str) -> str:
    t = f"{summary}\n{description}".lower()
    hits = [m.group(0) for _, pat in SIGNALS[wf] if (m := re.search(pat, t))]
    return ", ".join(f'"{h.strip()[:40]}"' for h in hits[:3])
