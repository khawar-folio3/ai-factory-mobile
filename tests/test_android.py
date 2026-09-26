from __future__ import annotations

from pathlib import Path

import pytest

from mobile_factory.config import AndroidConfig
from mobile_factory.errors import FactoryError
from mobile_factory.platforms.android import Android
from mobile_factory.platforms.base import CheckRun

UI = (
    '<hierarchy><node text="Profile" bounds="[0,0][100,50]"/><node content-desc="Edit avatar" bounds="[10,60][50,100]"/>'
    '<node text="12:30" package="com.android.systemui" bounds="[0,0][1,1]"/></hierarchy>'
)


class StubAndroid(Android):
    def __init__(self, root: Path, cfg: AndroidConfig) -> None:
        super().__init__(root, cfg)
        self.calls: list[tuple[str, ...]] = []

    def adb(self, *args: str, check: bool = False) -> str:
        self.calls.append(args)
        if args[:2] == ("shell", "uiautomator"):
            return "UI hierchary dumped to: /sdcard/factory-ui.xml"
        if args[:2] == ("shell", "cat"):
            return UI
        if args[:4] == ("shell", "dumpsys", "activity", "activities"):
            return "  topResumedActivity=ActivityRecord{1 u0 com.acme/.MainActivity t1}"
        if args[:3] == ("shell", "dumpsys", "activity"):
            return "#0: ReportFragment{a}\n#1: ProfileFragment{b}"
        return ""


@pytest.fixture
def droid(tmp_path: Path) -> StubAndroid:
    return StubAndroid(tmp_path, AndroidConfig(modules=["app", "feature/profile"], application_id="com.acme"))


def test_screen_state_is_stable_text(droid: StubAndroid) -> None:
    s = droid.screen_state()
    assert "activity  com.acme/.MainActivity" in s
    assert "fragments ProfileFragment" in s and "ReportFragment" not in s
    assert "labels    Profile|Edit avatar" in s and "12:30" not in s


def test_tap_uses_element_center(droid: StubAndroid) -> None:
    assert droid.tap("Edit avatar") == "tapped (30,80) 'Edit avatar'"
    assert ("shell", "input", "tap", "30", "80") in droid.calls
    with pytest.raises(FactoryError):
        droid.tap("Missing")


def test_checks_run_only_touched_modules(droid: StubAndroid, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(droid, "gradle", lambda tasks, log: seen.append(tasks) or CheckRun(True, "ok"))
    assert droid.modules_for(["feature/profile/src/A.kt", "app/B.kt", "README.md"]) == ["app", "feature/profile"]
    droid.checks(["feature/profile/src/A.kt"], tmp_path)
    assert seen == [[":feature:profile:lintDebug", ":feature:profile:testDebugUnitTest"]]
    assert droid.checks(["docs/x.md"], tmp_path).summary.startswith("no configured gradle module")


def test_open_link_needs_scheme(droid: StubAndroid) -> None:
    with pytest.raises(FactoryError):
        droid.open_link("profile")
    assert droid.open_link("acme://dl/profile") == "opened acme://dl/profile"
