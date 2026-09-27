from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakePlatform, git, load
from typer.testing import CliRunner

from mobile_factory import selftest
from mobile_factory.cli import app
from mobile_factory.config import LoadedConfig
from mobile_factory.platforms.base import CheckRun

HOME = "activity  a/.Main\nfragments MyDayFragment\nlabels    My Day|Spaces\n"
SIGN_IN = "activity  a/.Login\nfragments \nlabels    Welcome|Sign In\n"
SAVED = """appId: com.example.demo
---
- openLink: "demo://dl/p_omx"
- tapOn: "Room"
- tapOn: ".*(AM|PM) - .*(AM|PM).*"
- tapOn: "Book"
- tapOn: "Seat"
"""


class Device(FakePlatform):
    def __init__(self, first: str = HOME) -> None:
        super().__init__()
        self.state, self.taps, self.links, self.builds = first, [], [], 0

    def screen_state(self, png: Path | None = None) -> str:
        return self.state + ("ocr       Room|Seat\n" if png else "")

    def _move(self, name: str) -> str:
        self.state, self.settle_ms, self.changed = f"activity  a/.Main\nlabels    {name}\n", 400, True
        return "screen changed in 400ms"

    def open_link(self, link: str) -> str:
        self.links.append(link)
        return f"opened {link}  {self._move('map')}"

    def tap(self, label: str, nth: int = 1) -> str:
        self.taps.append(label)
        self.last_hit = {"text": label, "id": "", "desc": "", "point": "1,1"}
        return f"tapped '{label}'  {self._move(label)}"

    def build_install(self, log_dir: Path, launch: bool = True) -> CheckRun:
        self.builds += 1
        if self.builds == 1:
            return CheckRun(True, "ok: :app:assembleDebug\ninstalled app.apk on fake-1")
        return CheckRun(True, "build skipped: sources unchanged\ninstall skipped: the same APK is already on fake-1")


@pytest.fixture
def lc(repo: Path, monkeypatch: pytest.MonkeyPatch) -> LoadedConfig:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(selftest.time, "sleep", lambda s: None)
    monkeypatch.setattr(selftest.maestro, "binary", lambda: "maestro")
    monkeypatch.setattr(selftest, "has", lambda tool: False)
    c = load(repo)
    flows = c.path(c.cfg.android.flows_dir)
    (flows / "maestro").mkdir(parents=True)
    (flows / "maestro" / f"{selftest.FLOW}.yaml").write_text(SAVED)
    (flows / "routes.txt").write_text("p_omx > OmxMapFragment @map < CampusActivity\n")
    return c


def run(lc: LoadedConfig, dev: Device, ask: bool = False) -> tuple[list[selftest.Step], list[str]]:
    said: list[str] = []
    steps = selftest.SelfTest(lc, dev, lc.state_dir / "selftest" / "t", said.append, lambda m: ask).run()
    return steps, said


def test_selftest_runs_every_step_and_writes_only_to_the_factory_home(lc: LoadedConfig, repo: Path) -> None:
    dev = Device()
    steps, said = run(lc, dev)
    assert [s.name for s in steps] == [
        "context",
        "signed in",
        "route + open",
        "tap polling",
        "snapshot",
        "record+replay",
        "saved flow",
        "install skip",
        "mitmproxy",
    ]
    assert all(s.ok for s in steps), [s.line() for s in steps]
    assert steps[-1].skipped and steps[-1].line().startswith("- mitmproxy")
    assert dev.links == ["demo://dl/p_omx"] and dev.taps == ["Room", "Seat"]
    assert "Room 400ms, Seat 400ms" in steps[3].detail and "ocr line" in steps[4].detail
    assert "(build+install skipped)" in steps[7].detail
    out = lc.state_dir / "selftest" / "t"
    flow = (out / "flows" / "selftest.yaml").read_text()
    assert 'openLink: "demo://dl/p_omx"' in flow and 'tapOn: {text: "Seat"}' in flow
    assert len(dev.flows) == 2 and dev.flows[1] == SAVED
    assert list((out / "snapshots" / "after").glob("selftest_*.txt"))
    assert "== selftest summary" in said[-1] and "8 passed, 0 failed, 1 skipped" in said[-1]
    assert git(repo, "status", "--porcelain") == ""


def test_signed_out_without_enter_stops_before_touching_the_app(lc: LoadedConfig) -> None:
    dev = Device(SIGN_IN)
    steps, _ = run(lc, dev)
    assert [s.name for s in steps] == ["context", "signed in"]
    assert not steps[1].ok and "sign in on the emulator" in steps[1].detail
    assert dev.taps == [] and dev.links == [] and dev.builds == 0


def test_signed_out_continues_after_the_user_signs_in(lc: LoadedConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = Device(SIGN_IN)
    monkeypatch.setattr(dev, "launch", lambda: setattr(dev, "state", HOME) or "launched")
    steps, _ = run(lc, dev, ask=True)
    assert steps[1].ok and len(steps) == 9


def test_unchanged_screen_fails_tap_polling(lc: LoadedConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = Device()
    monkeypatch.setattr(dev, "tap", lambda label, nth=1: setattr(dev, "changed", False) or "screen UNCHANGED")
    steps, _ = run(lc, dev)
    tap = next(s for s in steps if s.name == "tap polling")
    assert not tap.ok and "UNCHANGED" in tap.detail


def test_second_install_must_skip(lc: LoadedConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = Device()
    monkeypatch.setattr(dev, "build_install", lambda log_dir, launch=True: CheckRun(True, "ok: built\ninstalled"))
    steps, _ = run(lc, dev)
    assert not next(s for s in steps if s.name == "install skip").ok


def test_safe_taps_keep_plain_tabs_only(tmp_path: Path) -> None:
    f = tmp_path / "f.yaml"
    f.write_text(SAVED + '- tapOn: "Cancel Meeting"\n- tapOn: "10:30 AM"\n- tapOn: "Delete"\n')
    assert selftest.safe_taps(f) == ["Room", "Seat"]
    assert selftest.signed_out(SIGN_IN) and not selftest.signed_out(HOME)


def test_doctor_device_runs_the_selftest(lc: LoadedConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.setattr("mobile_factory.cli.make_platform", lambda c: Device())
    res = runner.invoke(app, ["doctor", "--device"])
    assert res.exit_code == 0, res.output
    assert "✓ tap polling" in res.output and "total" in res.output
