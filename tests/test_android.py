from __future__ import annotations

from pathlib import Path

import pytest

from mobile_factory.config import AndroidConfig
from mobile_factory.errors import FactoryError
from mobile_factory.platforms import android
from mobile_factory.platforms.android import Android
from mobile_factory.platforms.base import CheckRun

UI = (
    '<hierarchy><node text="Profile" bounds="[0,0][100,50]"/><node content-desc="Edit avatar" bounds="[10,60][50,100]"/>'
    '<node text="12:30" package="com.android.systemui" bounds="[0,0][1,1]"/></hierarchy>'
)


class StubAndroid(Android):
    def __init__(self, root: Path, cfg: AndroidConfig) -> None:
        super().__init__(root, cfg, cache=root / "cache")
        self.calls: list[tuple[str, ...]] = []
        self.ui, self.after = UI, UI  # the dump before / after an action
        self.answers: dict[tuple[str, ...], str] = {}

    def adb(self, *args: str, check: bool = False) -> str:
        self.calls.append(args)
        if args in self.answers:
            return self.answers[args]
        if args[:3] in (("shell", "input", "tap"), ("shell", "am", "start")):
            self.ui = self.after
        if args[:1] == ("exec-out",):  # the screen hash: follows what is on screen
            return f"{hash(self.ui)}  -"
        if args[:2] == ("shell", "uiautomator"):
            return "UI hierchary dumped to: /sdcard/factory-ui.xml"
        if args[:2] == ("shell", "cat"):
            return self.ui
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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Fake time: every sleep is recorded and advances the monotonic clock."""
    sleeps: list[float] = []
    monkeypatch.setattr(android.time, "monotonic", lambda: sum(sleeps))
    monkeypatch.setattr(android.time, "sleep", sleeps.append)
    return sleeps


def test_tap_uses_element_center(droid: StubAndroid, clock: list[float]) -> None:
    assert droid.tap("Edit avatar").startswith("tapped (30,80) 'Edit avatar'  screen UNCHANGED in 3000ms")
    assert ("shell", "input", "tap", "30", "80") in droid.calls
    with pytest.raises(FactoryError):
        droid.tap("Missing")


def test_tap_and_open_settle_on_the_screen_hash_not_the_ui_dump(droid: StubAndroid, clock: list[float]) -> None:
    droid.after = UI.replace("Profile", "Settings")
    assert droid.tap("Edit avatar").endswith("screen changed in 200ms") and droid.changed
    assert clock == [0.1, 0.1]  # changed, then held for one frame
    dumps = sum(c[:2] == ("shell", "uiautomator") for c in droid.calls)
    assert dumps == 1  # only to find the element: settling never dumps
    droid.after = droid.ui  # nothing moves: gives up at the 3s cap
    assert droid.open_link("acme://dl/x").endswith("screen UNCHANGED in 3000ms") and not droid.changed
    assert sum(c[:2] == ("shell", "uiautomator") for c in droid.calls) == dumps


def test_checks_run_only_touched_modules(droid: StubAndroid, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(droid, "gradle", lambda tasks, log: seen.append(tasks) or CheckRun(True, "ok"))
    assert droid.modules_for(["feature/profile/src/A.kt", "app/B.kt", "README.md"]) == ["app", "feature/profile"]
    droid.checks(["feature/profile/src/A.kt"], tmp_path)
    assert seen == [[":feature:profile:lintDebug", ":feature:profile:testDebugUnitTest"]]
    assert droid.checks(["docs/x.md"], tmp_path).summary.startswith("no configured gradle module")


def test_open_link_needs_scheme(droid: StubAndroid, clock: list[float]) -> None:
    with pytest.raises(FactoryError):
        droid.open_link("profile")
    assert droid.open_link("acme://dl/profile").startswith("opened acme://dl/profile  screen")


def test_build_context_from_config_else_discovered_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    droid = StubAndroid(tmp_path, AndroidConfig(app_module="cxapp"))
    asked: list[int] = []
    monkeypatch.setattr(droid, "_variants", lambda: asked.append(1) or {"StageAcmeDebug": "com.acme.stage"})
    droid.answers[("shell", "dumpsys", "package", "com.acme.stage")] = (
        "Activity Resolver Table:\n  Schemes:\n      https:\n        1 com.acme.stage/.Main\n      acme:\n"
        "        2 com.acme.stage/.Main\n"
    )
    ctx = droid.build_context()
    assert ctx["module"] == ":cxapp:" and ctx["variant"] == "StageAcmeDebug"
    assert ctx["install"] == ":cxapp:installStageAcmeDebug" and ctx["assemble"] == ":cxapp:assembleStageAcmeDebug"
    assert ctx["unit_tests"] == ":cxapp:testStageAcmeDebugUnitTest" and ctx["package"] == "com.acme.stage"
    assert ctx["deeplink"] == "acme://" and "discovered variant, package, scheme" in ctx["source"]
    assert droid.build_context() == ctx and asked == [1]  # cached per project + variant
    cfgd = StubAndroid(tmp_path, AndroidConfig(variant="ProdDebug", application_id="com.a", deeplink_scheme="a"))
    monkeypatch.setattr(cfgd, "_variants", lambda: pytest.fail("configured: nothing to discover"))
    assert cfgd.build_context()["source"] == "config" and cfgd.build_context()["deeplink"] == "a://"


def test_build_and_install_only_when_changed(
    droid: StubAndroid, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    apk = tmp_path / "app/build/outputs/apk/debug/app-debug.apk"
    src, built = ["s1"], []

    def gradle(tasks: list[str], log: Path) -> CheckRun:
        built.append(tasks)
        apk.parent.mkdir(parents=True, exist_ok=True)
        apk.write_bytes(b"apk-" + src[0].encode())
        return CheckRun(True, f"ok: {tasks[0]}")

    monkeypatch.setattr(droid, "gradle", gradle)
    monkeypatch.setattr(droid, "ensure_device", lambda: "emu-1")
    monkeypatch.setattr(android, "worktree_hash", lambda root: src[0])
    droid.answers[("shell", "pm", "path", "com.acme")] = "package:/data/app/base.apk"
    droid.answers[("install", "-r", str(apk))] = "Performing Streamed Install\nSuccess"
    installs = lambda: sum(1 for c in droid.calls if c[0] == "install")  # noqa: E731

    first = droid.build_install(tmp_path, launch=False)
    assert first.ok and built == [[":app:assembleDebug"]] and "installed app-debug.apk on emu-1" in first.summary
    again = droid.build_install(tmp_path, launch=False).summary
    assert "build skipped" in again and "install skipped" in again and len(built) == 1 and installs() == 1
    src[0] = "s2"  # sources changed: build; a different APK: install
    assert "installed" in droid.build_install(tmp_path, launch=False).summary and len(built) == 2 and installs() == 2
    droid.answers[("shell", "pm", "path", "com.acme")] = ""  # uninstalled by hand: install again
    assert "installed" in droid.build_install(tmp_path, launch=False).summary and installs() == 3


def test_snapshot_state_adds_ocr_below_the_status_bar(
    droid: StubAndroid, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(android, "ocr", lambda png: [(10, 40, "12:30"), (200, 900, "Level 3"), (220, 950, "Level 3")])
    s = droid.screen_state(tmp_path / "x.png")
    assert s.endswith("ocr       Level 3\n") and "12:30" not in s.split("ocr")[1]
    monkeypatch.setattr(android, "ocr", lambda png: [])  # no OCR on this machine: no line, nothing breaks
    assert "ocr" not in droid.screen_state(tmp_path / "x.png")


def test_ocr_degrades_when_it_cannot_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(android.platform, "system", lambda: "Linux")
    assert android.ocr_bin() is None or android.ocr_bin().is_file()
    monkeypatch.setattr(android, "ocr_bin", lambda: None)
    assert android.ocr(tmp_path / "x.png") == []


def test_screenshot_settles_fast(droid: StubAndroid, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    frames = iter([b"a", b"b", b"b"])
    sleeps: list[float] = []
    monkeypatch.setattr(android.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": next(frames)})())
    monkeypatch.setattr(android.time, "sleep", sleeps.append)
    droid.screenshot(tmp_path / "s.png")
    assert (tmp_path / "s.png").read_bytes() == b"b" and sleeps == [0.25, 0.25]


def test_route_graph_lookup_and_record(tmp_path: Path) -> None:
    graph = tmp_path / "flows" / android.ROUTES
    assert android.routes(graph, "space") == []
    assert android.add_route(graph, "p_spaces  > SpacesFragment @main < HomeFragment")
    assert not android.add_route(graph, "p_spaces > SpacesFragment @main < HomeFragment")  # once
    graph.write_text("# header\n" + graph.read_text())
    assert android.routes(graph, "spacesfragment") == ["p_spaces > SpacesFragment @main < HomeFragment"]


def test_doctor_reports_mitmproxy_without_installing(droid: StubAndroid, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(android, "has", lambda tool: tool != "mitmdump")
    mitm = next(c for c in droid.doctor() if c.name.startswith("mitmproxy"))
    assert not mitm.ok and mitm.optional and mitm.detail == "brew install mitmproxy"


def test_mock_rules_are_run_scoped(droid: StubAndroid, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = tmp_path / "types.json"
    body.write_text('{"types": []}')
    android.mock_add(tmp_path, "/v3/spaces/types", str(body))
    rules = android.mock_add(tmp_path, "/v3/spaces$", '.items |= map(.type = "Project Room")')
    assert rules == [
        {"pattern": "/v3/spaces/types", "file": str(body.resolve())},
        {"pattern": "/v3/spaces$", "jq": '.items |= map(.type = "Project Room")'},
    ]
    monkeypatch.setattr(android, "has", lambda tool: False)
    with pytest.raises(FactoryError, match="mitmproxy"):
        droid.mock_on(tmp_path)
    assert droid.mock_off(tmp_path).startswith("mocks off")
    assert ("shell", "settings", "put", "global", "http_proxy", ":0") in droid.calls
