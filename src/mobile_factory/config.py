from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError

CONFIG_NAME = "factory.yaml"
FACTORY_DIR = ".factory"  # legacy in-repo layout; migrated out by state_dir()
LOCAL_FILE = "local.yaml"  # per developer: which accounts and auth this dev uses for the repo
# Nothing lives in the repo: every file the factory keeps for a repo moves here from <repo>/.factory on first use.
LEGACY_STATE = (
    "local.yaml",
    "runs",
    "data",
    "events.jsonl",
    "taste.md",
    "knowledge",
    "flows",
    "tickets",
    "slop.yaml",
    "evals",
)
DEFAULT_SECRETS_FILE = "~/.config/mobile-factory/secrets.env"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SECRET_KEY = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|webhook)", re.I)
_SECRET_VALUE = re.compile(
    r"^(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[\w-]{10,}|ATATT[\w-]{20,}|figd_[\w-]{20,}"
    r"|sk-[\w-]{20,}|AKIA[0-9A-Z]{16}|https://hooks\.slack\.com/services/\S+)$"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectConfig(_Model):
    name: str
    platform: Literal["android"] = "android"
    base_branch: str = "ask"
    branch_pattern: str = "bugfix/{key}-{slug}"
    branch_pattern_by_type: dict[str, str] = Field(
        default_factory=lambda: {t: "feature/{key}-{slug}" for t in ("Task", "Story", "Improvement", "New Feature")}
    )
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [
            "**/build.gradle*",
            "gradle/**",
            "gradle.properties",
            "**/AndroidManifest.xml",
            "**/proguard-rules.pro",
            "**/*.jks",
            "**/*.keystore",
            ".github/**",
        ]
    )
    local_only_paths: list[str] = Field(default_factory=list)
    test_dirs: list[str] = Field(default_factory=lambda: ["/test/", "/androidTest/", "Tests/"])


class AndroidConfig(_Model):
    app_module: str = "app"
    modules: list[str] = Field(default_factory=lambda: ["app"])
    variant: str = "Debug"
    application_id: str = ""
    launch_activity: str = ""
    avd: str = ""
    deeplink_scheme: str = ""
    lint_task: str = "lint{variant}"
    test_task: str = "test{variant}UnitTest"
    flows_dir: str = "flows"


class AutonomyConfig(_Model):
    ceiling: int = Field(2, ge=0, le=4)
    # risk score at or below the value allows that level
    thresholds: dict[int, int] = Field(default_factory=lambda: {4: 15, 3: 35, 2: 55, 1: 75})
    low_risk_categories: list[str] = Field(
        default_factory=lambda: ["typo", "copy", "ui_spacing", "color_token", "string_resource", "unit_test"]
    )
    high_risk_classes: list[str] = Field(
        default_factory=lambda: ["payment", "auth", "security", "data_loss", "migration", "mdm", "crypto"]
    )


class GateConfig(_Model):
    auto_at: int = Field(..., ge=0, le=5)


def _default_gates() -> dict[str, GateConfig]:
    return {
        "plan": GateConfig(auto_at=1),
        "repro": GateConfig(auto_at=2),
        "diff": GateConfig(auto_at=3),
        "review": GateConfig(auto_at=3),
        "pr": GateConfig(auto_at=4),
        "report": GateConfig(auto_at=5),  # posting to a ticket: always a human
        "tickets": GateConfig(auto_at=5),  # creating tickets: always a human
        "architecture": GateConfig(auto_at=5),  # a new app's stack: always a human
    }


class LimitsConfig(_Model):
    max_fix_attempts: int = Field(2, ge=0, le=5)
    max_review_rounds: int = Field(2, ge=1, le=5)
    require_repro: bool = True
    rollback_on_fail: bool = True


class TrackerConfig(_Model):
    kind: Literal["jira", "file"] = "file"
    provider: Literal["rest", "twg"] = "rest"
    site: str = ""
    email: str = ""
    token: str = ""
    projects: list[str] = Field(default_factory=list)
    allowed_types: list[str] = Field(default_factory=lambda: ["Bug", "Task"])  # legacy; routing uses `pipelines`
    pipelines: dict[str, str] = Field(  # ticket type -> workflow; unmapped types stop with a clear reason
        default_factory=lambda: {
            "Bug": "bugfix",
            "Task": "bugfix",
            "Sub-task": "bugfix",
            "Story": "feature",
            "Improvement": "feature",
            "New Feature": "feature",
            "Spike": "spike",
            "Epic": "epic",
            "App": "new-app",
        }
    )
    block_labels: list[str] = Field(default_factory=lambda: ["no-bot", "security", "needs-design"])
    transitions: dict[str, str] = Field(default_factory=dict)


class VcsConfig(_Model):
    host: Literal["github"] = "github"
    remote: str = "origin"
    draft: bool = True
    pr_label: str = ""
    forbid_attribution: bool = True
    github_account: str = ""  # gh login used for this repo (per dev, in .factory/local.yaml); "" = gh's active account


class GuardrailConfig(_Model):
    taste: str = "taste.md"
    knowledge: str = "knowledge"
    slop: bool = True
    slop_overrides: str = "slop.yaml"
    commands: list[str] = Field(default_factory=list)


# haiku | sonnet | opus | inherit, or a full model id. Steps not listed run on the session's model.
DEFAULT_MODELS = {
    "triage": "sonnet",
    "reproduce": "sonnet",
    "fix": "opus",
    "verify": "opus",
    "review": "opus",
    "review-correctness": "opus",
    "review-taste": "sonnet",
    "review-detectors": "haiku",
    "guardrail-learn": "sonnet",
    "learn-tally": "sonnet",
    "locate": "sonnet",
    "plan": "opus",
    "baseline": "sonnet",
    "implement": "opus",
    "accept": "sonnet",
    "research": "opus",
    "split": "opus",
    "spec": "opus",
    "architecture": "opus",
    "scaffold": "opus",
    "history": "haiku",  # git log/blame and past PRs: facts, no judgement
    "scout": "haiku",  # greps, git history, CLI output: mechanical work stays on the cheapest tier
}


class AgentsConfig(_Model):
    use: list[Literal["claude", "cursor"]] = Field(
        default_factory=lambda: list[Literal["claude", "cursor"]](["claude"])
    )  # per developer (local.yaml)
    models: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_MODELS))
    parallel: bool = True  # fan independent sub-tasks out to subagents at once (Claude Code, Cursor 2.4+)
    max_parallel: int = 16  # upper bound on subagents started at once; work is split as finely as this allows
    # Cursor wants its own model ids (Settings → Models); a tier left as "inherit" runs on the chat's model
    cursor_models: dict[str, str] = Field(
        default_factory=lambda: {"haiku": "inherit", "sonnet": "inherit", "opus": "inherit"}
    )

    @field_validator("cursor_models")
    @classmethod
    def _merge_default_tiers(cls, v: dict[str, str]) -> dict[str, str]:
        return {"haiku": "inherit", "sonnet": "inherit", "opus": "inherit", **v}

    def model_for(self, step: str, target: str) -> str:
        m = self.models.get(step, "inherit")
        return self.cursor_models.get(m, m) if target == "cursor" else m

    @field_validator("models")
    @classmethod
    def _merge_default_models(cls, v: dict[str, str]) -> dict[str, str]:
        return {**DEFAULT_MODELS, **v}


class NotificationsConfig(_Model):
    slack_webhook: str = ""


class VizConfig(_Model):
    pixel_agents: bool = False  # watch factory work in a visualiser (historical name: it switches any `tool` on)
    tool: str = "claude-office"  # which visualiser (mobile_factory.viz.BACKENDS)
    pixel_hooks: bool = True  # let the office see Claude Code sessions live (Pixel Agents' own hook)
    pixel_labels: bool = True  # always show agent labels in the office


class ToolOverride(_Model):
    install: str = ""
    skip: bool = False


class SetupConfig(_Model):
    tools: dict[str, ToolOverride] = Field(default_factory=dict)


class McpServer(_Model):
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _one_transport(cls, v: str, info: Any) -> str:
        if v and info.data.get("url"):
            raise ValueError("an MCP server has either url or command, not both")
        return v


class FactoryConfig(_Model):
    version: Literal[1] = 1
    project: ProjectConfig
    android: AndroidConfig = Field(default_factory=AndroidConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    gates: dict[str, GateConfig] = Field(default_factory=_default_gates)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    vcs: VcsConfig = Field(default_factory=VcsConfig)
    guardrail: GuardrailConfig = Field(default_factory=GuardrailConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    viz: VizConfig = Field(default_factory=VizConfig)
    setup: SetupConfig = Field(default_factory=SetupConfig)
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    secrets_file: str = DEFAULT_SECRETS_FILE

    @field_validator("gates")
    @classmethod
    def _merge_default_gates(cls, v: dict[str, GateConfig]) -> dict[str, GateConfig]:
        return {**_default_gates(), **v}


class LoadedConfig:
    def __init__(
        self, root: Path, cfg: FactoryConfig, raw: dict[str, Any], missing_env: set[str], state: Path | None = None
    ) -> None:
        self.root = root
        self.cfg = cfg
        self.raw = raw
        self.missing_env = missing_env
        self.state_dir = state or state_dir(root)

    @property
    def factory_dir(self) -> Path:
        return self.state_dir

    def path(self, configured: str) -> Path:
        """A configured file (taste, knowledge, flows, …): relative paths live in the per-developer home."""
        p = Path(os.path.expanduser(configured))
        return p if p.is_absolute() else self.state_dir / str(p).removeprefix(f"{FACTORY_DIR}/")

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"


def state_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("FACTORY_HOME", "~/.config/mobile-factory")))


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def project_key(root: Path) -> str:
    """owner__repo from the origin remote, so clones and worktrees share one home; else the main checkout's name+hash."""
    if m := re.search(r"[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", _git(root, "remote", "get-url", "origin")):
        return f"{m.group(1)}__{m.group(2)}"
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    main = Path(common).parent if common else root.resolve()
    return f"{main.name}__{hashlib.sha256(str(main).encode()).hexdigest()[:8]}"


def state_dir(root: Path) -> Path:
    """Everything the factory keeps for a repo, outside it: never committed, survives `git clean`, shared by worktrees."""
    d = state_home() / "projects" / project_key(root)
    moves = [(root / CONFIG_NAME, d / CONFIG_NAME)] + [(root / FACTORY_DIR / n, d / n) for n in LEGACY_STATE]
    for old, new in moves:  # one-time move from the old in-repo layout
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
    legacy = root / FACTORY_DIR
    if legacy.is_dir() and not any(legacy.iterdir()):
        legacy.rmdir()
    return d


def config_path(root: Path) -> Path:
    return state_dir(root) / CONFIG_NAME


def local_path(root: Path) -> Path:
    return state_dir(root) / LOCAL_FILE


def repo_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    top = _git(cur, "rev-parse", "--show-toplevel")
    return Path(top) if top else cur


def find_root(start: Path | None = None) -> Path:
    root = repo_root(start)
    if config_path(root).is_file():
        return root
    raise ConfigError(f"this repo is not set up yet: run `factory init` in {root}")


def secrets_path(value: str = DEFAULT_SECRETS_FILE) -> Path:
    return Path(os.path.expanduser(os.environ.get("FACTORY_SECRETS_FILE", value)))


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def environment(secrets_file: str = DEFAULT_SECRETS_FILE) -> dict[str, str]:
    """Secrets file first, process env wins."""
    return {**read_env_file(secrets_path(secrets_file)), **os.environ}


def interpolate(value: Any, env: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            missing.add(name)
            return ""

        return _ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v, env, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, env, missing) for v in value]
    return value


def plaintext_secrets(raw: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            p = f"{path}.{k}" if path else str(k)
            if (
                isinstance(v, str)
                and v
                and not _ENV_REF.search(v)
                and (_SECRET_KEY.search(str(k)) or _SECRET_VALUE.match(v))
            ):
                hits.append(p)
            else:
                hits += plaintext_secrets(v, p)
    elif isinstance(raw, list):
        for i, v in enumerate(raw):
            if isinstance(v, str) and _SECRET_VALUE.match(v):
                hits.append(f"{path}[{i}]")
            else:
                hits += plaintext_secrets(v, f"{path}[{i}]")
    return hits


def read_yaml(file: Path) -> dict[str, Any]:
    if not file.is_file():
        return {}
    try:
        data = yaml.safe_load(file.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{file}: invalid YAML: {e}") from e
    if leaks := plaintext_secrets(data):
        raise ConfigError(
            f"{file}: plaintext secret at {', '.join(leaks)}. Use ${{VAR}} and `factory secrets set VAR` instead."
        )
    return data if isinstance(data, dict) else {}


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(root: Path | None = None) -> LoadedConfig:
    root = root or find_root()
    state = state_dir(root)
    file = state / CONFIG_NAME
    raw = deep_merge(read_yaml(file), read_yaml(state / LOCAL_FILE))
    missing: set[str] = set()
    resolved = interpolate(raw, environment(raw.get("secrets_file", DEFAULT_SECRETS_FILE)), missing)
    try:
        cfg = FactoryConfig.model_validate(resolved)
    except ValidationError as e:
        raise ConfigError(f"{file}: {e}") from e
    return LoadedConfig(root, cfg, raw, missing, state)


def figma_mode(cfg: FactoryConfig) -> Literal["desktop", "remote", "none"]:
    urls = [s.url for s in cfg.mcp_servers.values()]
    if any(":3845" in u for u in urls):
        return "desktop"
    return "remote" if any("figma.com" in u for u in urls) else "none"


def env_refs(raw: Any) -> set[str]:
    if isinstance(raw, str):
        return {m.group(1) for m in _ENV_REF.finditer(raw)}
    if isinstance(raw, dict):
        return set().union(*(env_refs(v) for v in raw.values())) if raw else set()
    if isinstance(raw, list):
        return set().union(*(env_refs(v) for v in raw)) if raw else set()
    return set()
