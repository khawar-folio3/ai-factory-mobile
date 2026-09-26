from __future__ import annotations

from pathlib import Path

from conftest import FakePlatform, git, load

from mobile_factory import config, metrics
from mobile_factory.evals import Evals, report
from mobile_factory.integrations.tracker import FileTracker, adf_text, from_jira_fields
from mobile_factory.pipeline import Engine
from mobile_factory.platforms.base import snapshot_diff


def test_adf_and_jira_fields() -> None:
    adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Crash on iPhone"}]}]}
    t = from_jira_fields(
        "APP-9",
        {
            "summary": "Crash",
            "issuetype": {"name": "Bug"},
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "description": adf,
            "labels": ["no-bot"],
            "issuelinks": [
                {"type": {"name": "Blocks"}, "inwardIssue": {"key": "APP-1", "fields": {"status": {"name": "Open"}}}}
            ],
            "comment": {"comments": [{"author": {"displayName": "A"}, "body": adf}]},
        },
    )
    assert adf_text(adf) == "Crash on iPhone"
    assert t.type == "Bug" and t.status_category == "new"
    assert t.mentions_ios() and t.blocked_labels(["no-bot"]) == ["no-bot"]
    assert t.links[0].key == "APP-1" and t.links[0].direction == "inward"


def test_file_tracker_markdown(repo: Path) -> None:
    t = FileTracker(repo).get("APP-1")
    assert t.summary == "Avatar is clipped on the profile screen" and t.type == "Bug"


def test_snapshot_diff(tmp_path: Path) -> None:
    fp = FakePlatform()
    fp.snapshot(tmp_path, "before", "home")
    fp.snapshot(tmp_path, "before", "profile")
    fp.snapshot(tmp_path, "after", "home")
    fp.state = "activity  Main\nlabels    fixed\n"
    fp.snapshot(tmp_path, "after", "profile")
    d = dict(snapshot_diff(tmp_path))
    assert d["home"] == "unchanged"
    assert "+labels    fixed" in d["profile"]


def test_eval_case_roundtrip_and_metrics(repo: Path, fake: FakePlatform) -> None:
    (repo / "app/src/main/java/Profile.kt").write_text("class Profile {\n    val height = 64\n}\n")
    git(repo, "commit", "-q", "-am", "APP-1: human fix")
    ev = Evals(repo)
    case = ev.add("APP-1", "HEAD")
    assert case.human_files == ["app/src/main/java/Profile.kt"]
    wt = ev.prepare("APP-1")
    assert config.config_path(wt) == config.config_path(repo)  # the worktree shares this repo's factory home
    assert "height = 48" in (wt / "app/src/main/java/Profile.kt").read_text()
    assert git(wt, "status", "--porcelain") == "" and not (wt / "factory.yaml").exists()  # nothing copied in

    eng = Engine.start(load(repo), "APP-2")
    eng.st.outcome, eng.st.status = "draft-pr", "done"
    s = ev.score("APP-1", eng.st, ["app/src/main/java/Profile.kt", "app/Other.kt"])
    assert s.file_recall == 1.0 and s.file_precision == 0.5
    assert "cases 1" in report(ev.results())

    m = metrics.summarize([eng.st])
    assert m["runs"] == 1 and m["pr_rate"] == 1.0
    assert "PR rate 100%" in metrics.render(m)
