from __future__ import annotations

import base64
import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..config import TrackerConfig, state_dir
from ..errors import ConfigError, FactoryError
from ..proc import run

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
IOS_RE = re.compile(r"\biOS\b|iPhone|iPad", re.I)


class Link(BaseModel):
    type: str = ""
    direction: str = ""
    key: str = ""
    status: str = ""


class Comment(BaseModel):
    author: str = ""
    created: str = ""
    body: str = ""


class Ticket(BaseModel):
    key: str
    url: str = ""
    type: str = ""
    status: str = ""
    status_category: str = ""
    priority: str = ""
    summary: str = ""
    description: str = ""
    environment: str = ""
    components: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    reporter: str = ""
    assignee: str = ""
    fix_versions: list[str] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)

    def mentions_ios(self) -> bool:
        return bool(IOS_RE.search(" ".join([self.summary, self.description, *(c.body for c in self.comments)])))

    def blocked_labels(self, block: list[str]) -> list[str]:
        return [lb for lb in self.labels if lb in block]


def adf_text(node: Any) -> str:
    """Flatten Atlassian Document Format (or plain strings) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(t for t in (adf_text(n) for n in node) if t)
    if isinstance(node, dict):
        if "text" in node and isinstance(node["text"], str):
            return node["text"]
        if node.get("type") in ("hardBreak", "paragraph") and not node.get("content"):
            return "\n"
        parts = adf_text(node.get("content"))
        url = (node.get("attrs") or {}).get("url")
        return f"{parts} {url}".strip() if url else parts
    return ""


def _name(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("displayName") or v.get("value") or "")
    return "" if v is None else str(v)


def from_jira_fields(key: str, fields: dict[str, Any], url: str = "") -> Ticket:
    comments = fields.get("comment", {}).get("comments", []) if isinstance(fields.get("comment"), dict) else []
    links = []
    for ln in fields.get("issuelinks") or []:
        other = ln.get("inwardIssue") or ln.get("outwardIssue") or {}
        links.append(
            Link(
                type=_name(ln.get("type")),
                direction="inward" if ln.get("inwardIssue") else "outward",
                key=other.get("key", ""),
                status=_name((other.get("fields") or {}).get("status")),
            )
        )
    status = fields.get("status") or {}
    return Ticket(
        key=key,
        url=url,
        type=_name(fields.get("issuetype")),
        status=_name(status),
        status_category=str((status.get("statusCategory") or {}).get("key", "")) if isinstance(status, dict) else "",
        priority=_name(fields.get("priority")),
        summary=fields.get("summary") or "",
        description=adf_text(fields.get("description"))[:4000],
        environment=adf_text(fields.get("environment"))[:600],
        components=[_name(c) for c in fields.get("components") or []],
        labels=list(fields.get("labels") or []),
        reporter=_name(fields.get("reporter")),
        assignee=_name(fields.get("assignee")),
        fix_versions=[_name(v) for v in fields.get("fixVersions") or []],
        attachments=[a.get("filename", "") for a in fields.get("attachment") or []],
        links=links,
        comments=[
            Comment(author=_name(c.get("author")), created=c.get("created", ""), body=adf_text(c.get("body"))[:600])
            for c in comments[-10:]
        ],
    )


class Tracker:
    def get(self, key: str) -> Ticket:
        raise NotImplementedError

    def ping(self) -> str:
        return "ok"

    def transition(self, key: str, to_status: str) -> str:
        raise NotImplementedError

    def comment(self, key: str, markdown: str) -> str:
        raise NotImplementedError

    def create(self, project: str, type_: str, summary: str, description: str, parent: str = "") -> str:
        """Create a work item; returns its key."""
        raise NotImplementedError


def adf(markdown: str) -> dict[str, Any]:
    """Plain Atlassian document: one paragraph per block of text (formatting stays as typed)."""
    blocks = [b.strip() for b in markdown.split("\n\n") if b.strip()]
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": b}]} for b in blocks],
    }


class FileTracker(Tracker):
    """Tickets as YAML/Markdown files in the factory home: `tickets/<KEY>.yaml` or `<KEY>.md` (front matter optional)."""

    def __init__(self, root: Path) -> None:
        self.dir = state_dir(root) / "tickets"

    def get(self, key: str) -> Ticket:
        for ext in (".yaml", ".yml", ".md"):
            f = self.dir / f"{key}{ext}"
            if f.is_file():
                text = f.read_text()
                if ext == ".md":
                    meta: dict[str, Any] = {}
                    if text.startswith("---"):
                        _, fm, text = text.split("---", 2)
                        meta = yaml.safe_load(fm) or {}
                    title = next((ln[2:] for ln in text.splitlines() if ln.startswith("# ")), key)
                    return Ticket.model_validate({"key": key, "summary": title, "description": text.strip(), **meta})
                return Ticket.model_validate({"key": key, **(yaml.safe_load(text) or {})})
        raise FactoryError(f"no ticket file for {key} in {self.dir} (.yaml or .md)")

    def transition(self, key: str, to_status: str) -> str:
        return f"file tracker: {key} not moved (no status)"

    def comment(self, key: str, markdown: str) -> str:
        f = self.dir / f"{key}.comments.md"
        with f.open("a") as fh:
            fh.write(markdown.rstrip() + "\n\n---\n\n")
        return f"comment added to {f.name}"

    def create(self, project: str, type_: str, summary: str, description: str, parent: str = "") -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        used = [
            int(m.group(1)) for p in self.dir.glob(f"{project}-*.*") if (m := re.match(rf"{project}-(\d+)\.", p.name))
        ]
        key = f"{project}-{max(used, default=0) + 1}"
        meta = {"type": type_, **({"parent": parent} if parent else {})}
        (self.dir / f"{key}.md").write_text(f"---\n{yaml.safe_dump(meta)}---\n# {summary}\n\n{description}\n")
        return key


class JiraRest(Tracker):
    def __init__(self, cfg: TrackerConfig) -> None:
        if not (cfg.site and cfg.email and cfg.token):
            raise ConfigError("tracker jira/rest needs site, email and token (${JIRA_EMAIL}, ${JIRA_API_TOKEN})")
        self.base = f"https://{cfg.site.removeprefix('https://').rstrip('/')}"
        auth = base64.b64encode(f"{cfg.email}:{cfg.token}".encode()).decode()
        self.headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(  # noqa: S310 - fixed https base from config
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={**self.headers, **({"Content-Type": "application/json"} if body is not None else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise FactoryError(f"Jira {method} {path}: HTTP {e.code} {e.read()[:300]!r}") from e
        except urllib.error.URLError as e:
            raise FactoryError(f"Jira unreachable: {e.reason}") from e

    def ping(self) -> str:
        me = self._call("GET", "/rest/api/3/myself")
        return f"{self.base} as {me.get('displayName', '?')}"

    def get(self, key: str) -> Ticket:
        d = self._call("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}")
        return from_jira_fields(d["key"], d["fields"], f"{self.base}/browse/{d['key']}")

    def transition(self, key: str, to_status: str) -> str:
        ts = self._call("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
        match = next((t for t in ts if t["to"]["name"] == to_status or t["name"] == to_status), None)
        if not match:
            return f"no transition to {to_status} on {key}; move it by hand"
        self._call("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}})
        return f"{key} -> {to_status}"

    def comment(self, key: str, markdown: str) -> str:
        self._call("POST", f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment", {"body": adf(markdown)})
        return f"comment added to {key}"

    def create(self, project: str, type_: str, summary: str, description: str, parent: str = "") -> str:
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": type_},
            "summary": summary,
            "description": adf(description),
        }
        if parent:
            fields["parent"] = {"key": parent}
        return str(self._call("POST", "/rest/api/3/issue", {"fields": fields})["key"])


def site_prefix(site: str) -> str:
    return re.sub(r"^https?://", "", site).split(".")[0].split("/")[0]


def normalize_site(value: str) -> str:
    host = re.sub(r"^https?://", "", value.strip()).split("/")[0].lower()
    return host if "." in host or not host else f"{host}.atlassian.net"


class JiraTwg(Tracker):
    """Atlassian twg CLI with the dev's own login; `--site` lets one machine work against several Jira sites."""

    def __init__(self, cfg: TrackerConfig) -> None:
        self.site = cfg.site

    def _json(self, *args: str) -> Any:
        site = ["--site", site_prefix(self.site)] if self.site else []
        with tempfile.NamedTemporaryFile(suffix=".json") as f:
            r = run(["twg", *site, *args, "--output", "json", "--output-file", f.name])
            data = json.loads(Path(f.name).read_text() or "{}")
        if not r.ok or data.get("ok") is False:
            raise FactoryError(f"twg {' '.join(args[:3])} failed: {data.get('error') or r.err[:300]}")
        return data

    def ping(self) -> str:
        me = self._json("whoami")
        d = me.get("data", me)
        self._json("jira", "workitem", "query", "--jql", "created >= -1d", "--limit", "1")
        return f"twg as {d.get('name', '?')} <{d.get('email', '?')}>"

    def get(self, key: str) -> Ticket:
        d = self._json("jira", "workitem", "get", key, "--full")
        d = d.get("data", d)
        d = d[0] if isinstance(d, list) else d
        url = d.get("url") or (f"https://{self.site}/browse/{key}" if self.site else "")
        return from_jira_fields(d.get("key", key), d.get("fields", d), url)

    def transition(self, key: str, to_status: str) -> str:
        ts = self._json("jira", "workitem", "transition", "--id", key)
        match = next((t for t in ts.get("data", {}).get("transitions", []) if t.get("toName") == to_status), None)
        if not match:
            return f"no transition to {to_status} on {key}; move it by hand"
        self._json("jira", "workitem", "transition", "--id", key, "--transition-id", str(match["id"]))
        return f"{key} -> {to_status}"

    def comment(self, key: str, markdown: str) -> str:
        self._json(
            "jira", "workitem", "comment", "create", "--issue-id", key, "--body", markdown, "--body-format", "markdown"
        )
        return f"comment added to {key}"

    def create(self, project: str, type_: str, summary: str, description: str, parent: str = "") -> str:
        args = ["jira", "workitem", "create", "--space", project, "--type", type_, "--summary", summary]
        args += ["--description", description, "--description-format", "markdown", "--yes"]
        d = self._json(*args, *(["--parent", parent] if parent else []))
        d = d.get("data", d)
        return str(d.get("key") or d.get("issueKey") or d.get("id", "?"))


def make(cfg: TrackerConfig, root: Path) -> Tracker:
    if cfg.kind == "file":
        return FileTracker(root)
    return JiraTwg(cfg) if cfg.provider == "twg" else JiraRest(cfg)


def ticket_url(cfg: TrackerConfig, t: Ticket) -> str:
    return t.url or (f"https://{cfg.site}/browse/{t.key}" if cfg.site else "")
