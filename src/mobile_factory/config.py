from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError

CONFIG_NAME = "factory.yaml"
LOCAL_NAME = ".factory/local.yaml"  # per developer, git-ignored: which accounts and auth this dev uses here
FACTORY_DIR = ".factory"
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
    branch_pattern_by_type: dict[str, str] = Field(default_factory=lambda: {"Task": "feature/{key}-{slug}"})
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
    flows_dir: str = ".factory/flows"


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
    allowed_types: list[str] = Field(default_factory=lambda: ["Bug", "Task"])
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
    taste: str = ".factory/taste.md"
    knowledge: str = ".factory/knowledge"
    slop: bool = True
    slop_overrides: str = ".factory/slop.yaml"
    commands: list[str] = Field(default_factory=list)


class NotificationsConfig(_Model):
    slack_webhook: str = ""


class VizConfig(_Model):
    pixel_agents: bool = False


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
    def __init__(self, root: Path, cfg: FactoryConfig, raw: dict[str, Any], missing_env: set[str]) -> None:
        self.root = root
        self.cfg = cfg
        self.raw = raw
        self.missing_env = missing_env

    @property
    def factory_dir(self) -> Path:
        return self.root / FACTORY_DIR

    @property
    def runs_dir(self) -> Path:
        return self.factory_dir / "runs"


def find_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for p in (cur, *cur.parents):
        if (p / CONFIG_NAME).is_file():
            return p
    raise ConfigError(f"no {CONFIG_NAME} found from {cur} upwards: run `factory init` in the repo root")


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
    file = root / CONFIG_NAME
    raw = deep_merge(read_yaml(file), read_yaml(root / LOCAL_NAME))
    missing: set[str] = set()
    resolved = interpolate(raw, environment(raw.get("secrets_file", DEFAULT_SECRETS_FILE)), missing)
    try:
        cfg = FactoryConfig.model_validate(resolved)
    except ValidationError as e:
        raise ConfigError(f"{file}: {e}") from e
    return LoadedConfig(root, cfg, raw, missing)


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
