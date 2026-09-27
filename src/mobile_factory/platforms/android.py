from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import AndroidConfig, state_home
from ..errors import FactoryError
from ..proc import has, run
from ..wizard import Checks
from . import maestro
from .base import Check, CheckRun, Platform, worktree_hash

_LABEL = re.compile(r'(?:text|content-desc)="([^"]+)"')
_ATTR = re.compile(r'(text|content-desc|resource-id)="([^"]*)"')
_BOUNDS = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
_FRAGMENT = re.compile(r"#\d+: ([A-Za-z0-9_]+Fragment)\{")
_ERR = re.compile(r"^e: |error:|FAILED|What went wrong|Lint found|tests completed|Exception")
_OCR_SRC = Path(__file__).with_name("ocr.swift")
STATUS_BAR = 150  # px: OCR text above this is clock/battery noise
ROUTES = "routes.txt"  # route graph in the flows folder: `path > Screen @set < reached from`, one per line
MOCKS, MOCK_PORT = "mocks.json", 8765  # run-scoped overrides, served by mitmproxy
_MOCK_ADDON = Path(__file__).with_name("mock_addon.py")
SETTLE, SETTLE_POLL = 3.0, 0.1  # after a tap / open: poll the screen hash until it changes and holds, capped
_NOT_DEEPLINK = {"http", "https", "file", "content", "package", "market", "intent"}


def ocr_bin() -> Path | None:
    """Local macOS Vision OCR, compiled once into the factory home; None where it cannot run."""
    exe = state_home() / "bin" / "ocr"
    if exe.is_file() and exe.stat().st_mtime >= _OCR_SRC.stat().st_mtime:
        return exe
    if platform.system() != "Darwin" or not has("swiftc"):
        return None
    exe.parent.mkdir(parents=True, exist_ok=True)
    return exe if run(["swiftc", "-O", "-o", str(exe), str(_OCR_SRC)], timeout=300).ok else None


def ocr(png: Path) -> list[tuple[int, int, str]]:
    """(x, y, text) for every text OCR reads on the image; [] when OCR is unavailable."""
    try:
        lines = run([str(exe), str(png)], timeout=30).out.splitlines() if (exe := ocr_bin()) else []
    except FactoryError:  # OCR is a bonus: a slow or broken build never fails a snapshot
        return []
    hits = [re.match(r"(\d+),(\d+)\t(.+)", ln) for ln in lines]
    return [(int(m[1]), int(m[2]), m[3]) for m in hits if m]


def mock_add(run_dir: Path, pattern: str, target: str) -> list[dict[str, str]]:
    """Add or replace the rule for `pattern`: a JSON file served as is, or a jq filter over the real response."""
    f = run_dir / MOCKS
    rules = [r for r in (json.loads(f.read_text()) if f.is_file() else []) if r["pattern"] != pattern]
    src = Path(target).expanduser()
    rules.append(
        {"pattern": pattern, "file": str(src.resolve())} if src.is_file() else {"pattern": pattern, "jq": target}
    )
    f.write_text(json.dumps(rules, indent=2))
    return rules


def _alive(pid_file: Path) -> int:
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return 0


def routes(graph: Path, text: str, limit: int = 15) -> list[str]:
    """Graph lines mentioning `text` (a path, screen or class): how to reach a screen and from where."""
    if not graph.is_file():
        return []
    return [ln for ln in graph.read_text().splitlines() if not ln.startswith("#") and text.lower() in ln.lower()][
        :limit
    ]


def add_route(graph: Path, line: str) -> bool:
    """Record a way to a screen once; False when the graph already has it."""
    line = " ".join(line.split())
    have = graph.read_text().splitlines() if graph.is_file() else []
    if line in have:
        return False
    graph.parent.mkdir(parents=True, exist_ok=True)
    graph.write_text("\n".join([*have, line]) + "\n")
    return True


def _node(line: str) -> dict[str, str]:
    """What the recorder can select a dumped node by: text, resource id, content-desc, else its centre."""
    a = dict(_ATTR.findall(line))
    hit = {"text": a.get("text", ""), "id": a.get("resource-id", ""), "desc": a.get("content-desc", "")}
    if m := _BOUNDS.search(line):
        x1, y1, x2, y2 = map(int, m.groups())
        hit["point"] = f"{(x1 + x2) // 2},{(y1 + y2) // 2}"
    return hit


def full_link(cfg: AndroidConfig, link: str) -> str:
    if "://" in link:
        return link
    if not cfg.deeplink_scheme:
        raise FactoryError("set android.deeplink_scheme or pass a full URI")
    return f"{cfg.deeplink_scheme}://{link.lstrip('/')}"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%H%M%S")


def _tail(text: str, pattern: re.Pattern[str] = _ERR, n: int = 25) -> str:
    lines = text.splitlines()
    hits = [ln for ln in lines if pattern.search(ln)][:n]
    return "\n".join([*hits, "...", *lines[-12:]])


class Android(Platform):
    name = "android"

    def __init__(self, root: Path, cfg: AndroidConfig, cache: Path | None = None) -> None:
        self.root = root
        self.cfg = cfg
        self.cache = cache or state_home() / "build"  # per project: last build, installed APKs, discovered context
        self.last_hit: dict[str, str] = {}

    @property
    def _cache_file(self) -> Path:
        return self.cache / f"{self.cfg.app_module.replace('/', '-')}-{self.cfg.variant}.json"

    def _cached(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self._cache_file.read_text()))
        except (OSError, ValueError):
            return {}

    def _cache_put(self, **kv: Any) -> None:
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_file.write_text(json.dumps({**self._cached(), **kv}, indent=2))

    def adb(self, *args: str, check: bool = False) -> str:
        return run(["adb", *args], self.root, check=check).out.replace("\r", "")

    def devices(self) -> list[str]:
        if not has("adb"):
            return []
        return [ln.split()[0] for ln in self.adb("devices").splitlines()[1:] if ln.strip().endswith("device")]

    def doctor(self) -> list[Check]:
        checks = [Check(f"{t} on PATH", has(t), "" if has(t) else f"install {t}") for t in ("adb", "java")]
        wrapper = (self.root / "gradlew").is_file()
        checks.append(Check("gradle wrapper", wrapper, "" if wrapper else "no ./gradlew in repo root"))
        checks.append(
            Check(
                "maestro",
                bool(mae := maestro.binary()),
                mae or "optional: curl -fsSL https://get.maestro.mobile.dev | bash",
                optional=True,
            )
        )
        devs = self.devices()
        serial = os.environ.get("ANDROID_SERIAL")
        if len(devs) == 1 or (serial and serial in devs):
            checks.append(Check("device", True, serial or devs[0]))
        elif devs:
            checks.append(Check("device", False, f"{len(devs)} devices attached: set ANDROID_SERIAL"))
        else:
            hint = f"will boot AVD {self.cfg.avd}" if self.cfg.avd else "start an emulator or set android.avd"
            checks.append(Check("device", bool(self.cfg.avd), hint))
        if self.cfg.application_id:
            checks.append(Check("application_id set", True, self.cfg.application_id))
        else:
            checks.append(Check("application_id set", False, "set android.application_id in factory.yaml"))
        mitm = has("mitmdump")
        checks.append(
            Check("mitmproxy (factory android mock)", mitm, "" if mitm else "brew install mitmproxy", optional=True)
        )
        return checks

    def ensure_device(self) -> str:
        devs = self.devices()
        if serial := os.environ.get("ANDROID_SERIAL"):
            if serial in devs:
                return serial
            raise FactoryError(f"ANDROID_SERIAL={serial} is not attached")
        if len(devs) == 1:
            return devs[0]
        if len(devs) > 1:
            raise FactoryError(f"{len(devs)} devices attached: set ANDROID_SERIAL")
        emulator = self._emulator()
        avd = self.cfg.avd or next(iter(run([emulator, "-list-avds"]).out.split()), "")
        if not avd:
            raise FactoryError("no device attached and no AVD available")
        subprocess.Popen(
            [emulator, "-avd", avd, "-no-snapshot-save"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(60):
            if self.adb("shell", "getprop", "sys.boot_completed").strip() == "1":
                return self.devices()[0]
            time.sleep(3)
        raise FactoryError(f"AVD {avd} did not boot in 180s")

    def _emulator(self) -> str:
        adb = run(["which", "adb"]).out.strip()
        candidate = Path(adb).resolve().parent.parent / "emulator" / "emulator" if adb else None
        if candidate and candidate.exists():
            return str(candidate)
        if has("emulator"):
            return "emulator"
        raise FactoryError("Android emulator binary not found")

    def gradle(self, tasks: list[str], log: Path) -> CheckRun:
        r = run(["./gradlew", *tasks, "--console=plain", "--continue"], self.root, log=log, timeout=3600)
        if r.ok:
            return CheckRun(True, f"ok: {' '.join(tasks)}", log)
        return CheckRun(False, f"FAILED: {' '.join(tasks)}\n{_tail(r.out + r.err)}", log)

    def _variants(self) -> dict[str, str]:
        return dict(Checks().variants(self.root, self.cfg.app_module))

    def build_context(self) -> dict[str, str]:
        """From config; what it lacks is discovered once (Gradle variants, dumpsys) and cached per variant."""
        c = self.cfg
        sig = [c.app_module, c.variant, c.application_id, c.deeplink_scheme]
        if (hit := self._cached()).get("context_for") == sig:
            ctx = dict(hit["context"])
            if not ctx["deeplink"] and ctx["package"] and (scheme := self._scheme(ctx["package"])):
                ctx["deeplink"] = f"{scheme}://"  # the app was not installed when first resolved
                self._cache_put(context=ctx)
            return ctx
        found: list[str] = []
        mod, variant = f":{c.app_module.replace('/', ':')}", c.variant
        app_id = c.application_id
        if variant in ("Debug", "Release") or not app_id:  # a bare build type: with flavors the real name is longer
            known = self._variants()
            if variant not in known and len(m := [v for v in known if v.endswith(variant)]) == 1:
                variant, found = m[0], [*found, "variant"]
            if not app_id and (app_id := known.get(variant, "")):
                found.append("package")
        if (scheme := c.deeplink_scheme or (self._scheme(app_id) if app_id else "")) and not c.deeplink_scheme:
            found.append("scheme")
        ctx = {
            "module": f"{mod}:",
            "variant": variant,
            "install": f"{mod}:install{variant}",
            "assemble": f"{mod}:assemble{variant}",
            "unit_tests": f"{mod}:{c.test_task.format(variant=variant)}",
            "lint": f"{mod}:{c.lint_task.format(variant=variant)}",
            "package": app_id,
            "deeplink": f"{scheme}://" if scheme else "",
            "source": f"config + discovered {', '.join(found)} (cached {self._cache_file})" if found else "config",
        }
        self._cache_put(context_for=sig, context=ctx)
        return ctx

    def _scheme(self, app_id: str) -> str:
        """The app's own deeplink scheme from the installed package's intent filters; '' when not installed."""
        with contextlib.suppress(FactoryError):
            dump = self.adb("shell", "dumpsys", "package", app_id).split("Schemes:", 1)
            schemes = re.findall(r"^\s+([a-z][\w+.-]*):$", dump[1], re.M) if len(dump) > 1 else []
            return next((s for s in schemes if s not in _NOT_DEEPLINK and "." not in s), "")  # a.b.c: OAuth redirect
        return ""

    def _apk(self, variant: str) -> Path | None:
        """The newest APK of `variant` (apk/<flavors>/<buildType>/…), else the newest of any."""
        out = self.root / self.cfg.app_module / "build" / "outputs" / "apk"
        apks = list(out.glob("**/*.apk"))
        mine = [p for p in apks if variant.lower() in str(p.relative_to(out)).lower().replace("/", "").replace("-", "")]
        return max(mine or apks, key=lambda p: p.stat().st_mtime, default=None)

    def build_install(self, log_dir: Path, launch: bool = True) -> CheckRun:
        """Build only when the sources changed since the last build; install (`adb install -r`: never uninstalls, the
        sign-in stays) only when that APK is not already on this device."""
        serial = self.ensure_device()
        ctx, cache, src = self.build_context(), self._cached(), worktree_hash(self.root)
        apk, log = Path(cache.get("apk", "")), None
        if cache.get("src") == src and apk.is_file():
            built = f"build skipped: sources unchanged since the last build ({apk.name})"
        else:
            res = self.gradle([ctx["assemble"]], log_dir / f"build-{_stamp()}.log")
            if not res.ok:
                return res
            if not (found := self._apk(ctx["variant"])):
                return CheckRun(False, f"FAILED: no APK under {self.cfg.app_module}/build/outputs/apk", res.log)
            apk, built, log = found, res.summary, res.log
            self._cache_put(src=src, apk=str(apk))
        pkg = ctx["package"]
        digest, key = hashlib.sha256(apk.read_bytes()).hexdigest(), f"{serial}|{pkg}"
        installed = self._cached().get("installed", {})
        on_device = not pkg or bool(self.adb("shell", "pm", "path", pkg).strip())
        if installed.get(key) == digest and on_device:
            summary = f"{built}\ninstall skipped: the same APK is already on {serial}"
        else:
            out = self.adb("install", "-r", str(apk))
            if "Success" not in out:
                return CheckRun(False, f"FAILED: adb install -r {apk.name}\n{out[-800:]}", log)
            self._cache_put(installed={**installed, key: digest})
            summary = f"{built}\ninstalled {apk.name} on {serial}"
        if launch:
            summary += f"\n{self.launch()}"
        return CheckRun(True, summary, log)

    def modules_for(self, files: list[str]) -> list[str]:
        mods = sorted(self.cfg.modules, key=len, reverse=True)  # longest prefix wins
        return sorted({m for f in files if (m := next((m for m in mods if f.startswith(f"{m}/")), None))})

    def checks(self, files: list[str], log_dir: Path) -> CheckRun:
        tasks = []
        for m in self.modules_for(files):
            g = ":" + m.replace("/", ":")
            tasks += [
                f"{g}:{self.cfg.lint_task.format(variant=self.cfg.variant)}",
                f"{g}:{self.cfg.test_task.format(variant=self.cfg.variant)}",
            ]
        if not tasks:
            return CheckRun(True, "no configured gradle module touched; nothing to run")
        return self.gradle(tasks, log_dir / f"checks-{_stamp()}.log")

    def _dump(self) -> str:
        for _ in range(3):
            if "dumped to" in self.adb("shell", "uiautomator", "dump", "/sdcard/factory-ui.xml"):
                break
            time.sleep(1)
        return self.adb("shell", "cat", "/sdcard/factory-ui.xml").replace("><", ">\n<")

    def _top(self) -> str:
        for ln in self.adb("shell", "dumpsys", "activity", "activities").splitlines():
            if "topResumedActivity" in ln or "mResumedActivity" in ln:
                m = re.search(r"([\w.]+/[\w.$]+)", ln)
                if m:
                    return m.group(1)
        return "?"

    def screen_state(self, png: Path | None = None) -> str:
        top = self._top()
        pkg = top.split("/")[0]
        frags = list(
            dict.fromkeys(
                f for f in _FRAGMENT.findall(self.adb("shell", "dumpsys", "activity", pkg)) if f != "ReportFragment"
            )
        )
        labels = list(
            dict.fromkeys(
                m for ln in self._dump().splitlines() if "com.android.systemui" not in ln for m in _LABEL.findall(ln)
            )
        )
        state = f"activity  {top}\nfragments {' '.join(frags[-6:])}\nlabels    {'|'.join(labels[:60])}\n"
        if png and (seen := ocr(png)):  # map, splash, dialog and web view text uiautomator cannot read
            words = dict.fromkeys(txt for _, y, txt in seen if y > STATUS_BAR)
            state += f"ocr       {'|'.join(list(words)[:60])}\n"
        return state

    def screenshot(self, dest: Path, settle: float = 3.0) -> None:
        prev, end = "", time.monotonic() + settle
        while True:  # settle: two identical frames in a row, polled fast, capped at `settle` seconds
            data = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, check=False).stdout
            if not data:
                raise FactoryError("screencap failed")
            dest.write_bytes(data)
            cur = hashlib.sha256(data).hexdigest()
            if cur == prev or time.monotonic() >= end:
                return
            prev = cur
            time.sleep(0.25)

    def tap(self, label: str, nth: int = 1) -> str:
        nodes = [ln for ln in self._dump().splitlines() if _LABEL.search(ln)]
        exact = [ln for ln in nodes if label in _LABEL.findall(ln)]
        loose = [ln for ln in nodes if label.lower() in ln.lower()]
        hits = exact or loose
        if len(hits) < nth:
            raise FactoryError(f"no element matching '{label}' on screen")
        self.last_hit = _node(hits[nth - 1])
        if "point" not in self.last_hit:
            raise FactoryError(f"element '{label}' has no bounds")
        x, y = self.last_hit["point"].split(",")
        before = self._frame()
        self.adb("shell", "input", "tap", x, y)
        return f"tapped ({x},{y}) '{_LABEL.findall(hits[nth - 1])[0]}'  {self._settled(before)}"

    def _frame(self) -> str:
        """The screen's hash, computed on the device: ~0.1s, where a UI dump takes seconds."""
        return self.adb("exec-out", "screencap | md5sum").split(" ", 1)[0]

    def _settled(self, before: str) -> str:
        """Poll the screen hash until it differs from `before` and holds for one frame (or SETTLE passes)."""
        t0, prev = time.monotonic(), before
        while time.monotonic() < t0 + SETTLE:
            time.sleep(SETTLE_POLL)
            cur = self._frame()
            if cur != before and cur == prev:
                break
            prev = cur
        self.changed, self.settle_ms = prev != before, int((time.monotonic() - t0) * 1000)
        return f"screen {'changed' if self.changed else 'UNCHANGED'} in {self.settle_ms}ms"

    def wait_for(self, text: str, timeout: int = 30) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if text.lower() in (dump := self._dump()).lower():
                self.last_hit = next((_node(ln) for ln in dump.splitlines() if text.lower() in ln.lower()), {})
                return True
            time.sleep(2)
        return False

    def type_text(self, text: str) -> str:
        """Into the focused field; `input text` needs spaces as %s and shell metacharacters escaped."""
        self.adb("shell", "input", "text", "'" + text.replace("'", "'\\''").replace(" ", "%s") + "'")
        return f"typed {len(text)} chars"

    def scroll(self, direction: str = "down") -> str:
        size = re.search(r"(\d+)x(\d+)", self.adb("shell", "wm", "size"))
        w, h = (int(size[1]), int(size[2])) if size else (1080, 2400)
        y1, y2 = (h * 3 // 4, h // 4) if direction == "down" else (h // 4, h * 3 // 4)
        self.adb("shell", "input", "swipe", str(w // 2), str(y1), str(w // 2), str(y2), "300")
        return f"scrolled {direction}"

    def open_link(self, link: str) -> str:
        link = full_link(self.cfg, link)
        pkg = [self.cfg.application_id] if self.cfg.application_id else []
        before = self._frame()
        self.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"'{link}'", *pkg)
        return f"opened {link}  {self._settled(before)}"

    def back(self) -> None:
        self.adb("shell", "input", "keyevent", "4")

    def launch(self) -> str:
        if not self.cfg.application_id:
            raise FactoryError("set android.application_id in factory.yaml")
        if self.cfg.launch_activity:
            act = self.cfg.launch_activity
            comp = f"{self.cfg.application_id}/{act}"
            self.adb("shell", "am", "start", "-n", comp)
            return f"launched {comp}"
        self.adb("shell", "monkey", "-p", self.cfg.application_id, "-c", "android.intent.category.LAUNCHER", "1")
        return f"launched {self.cfg.application_id}"

    def mock_on(self, run_dir: Path) -> str:
        """Route the device through a local proxy that answers the run's mock rules; the app code stays untouched."""
        if not has("mitmdump"):
            raise FactoryError(
                "network mocks need mitmproxy (`brew install mitmproxy`), its CA installed on the device (http://mitm.it)"
                " and a debug build that trusts user CAs (network_security_config debug-overrides)"
            )
        pid = run_dir / "mock.pid"
        if not _alive(pid):
            log = run_dir / "logs" / "mock-proxy.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                [
                    "mitmdump",
                    "--listen-port",
                    str(MOCK_PORT),
                    "-s",
                    str(_MOCK_ADDON),
                    "--set",
                    f"factory_mocks={run_dir / MOCKS}",
                ],
                stdout=log.open("w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            pid.write_text(str(proc.pid))
            for _ in range(50):  # listening, or it died (port taken, bad addon): a mock that cannot serve says so
                if proc.poll() is not None:
                    pid.unlink(missing_ok=True)
                    raise FactoryError(f"mock proxy exited: {log.read_text()[-300:].strip()}")
                with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", MOCK_PORT), 0.1):
                    break
                time.sleep(0.1)
        self.adb("reverse", f"tcp:{MOCK_PORT}", f"tcp:{MOCK_PORT}")
        self.adb("shell", "settings", "put", "global", "http_proxy", f"127.0.0.1:{MOCK_PORT}")
        n = len(json.loads((run_dir / MOCKS).read_text())) if (run_dir / MOCKS).is_file() else 0
        return f"mocking {n} rule(s) through 127.0.0.1:{MOCK_PORT}; undo with `factory android mock --off`"

    def mock_off(self, run_dir: Path) -> str:
        self.adb("shell", "settings", "put", "global", "http_proxy", ":0")
        self.adb("reverse", "--remove", f"tcp:{MOCK_PORT}")
        if pid := _alive(run_dir / "mock.pid"):
            os.kill(pid, signal.SIGTERM)
        (run_dir / "mock.pid").unlink(missing_ok=True)
        return "mocks off: device proxy cleared, real responses again"

    def run_flow(self, flow: Path, log_dir: Path) -> CheckRun:
        """Maestro runs in the log folder: its screenshots and debug output never land in the app repo."""
        if not (exe := maestro.binary()):
            raise FactoryError("maestro not installed: curl -fsSL https://get.maestro.mobile.dev | bash")
        stamp = f"{flow.stem}-{_stamp()}"
        log, dbg = log_dir / f"flow-{stamp}.log", log_dir / f"maestro-{stamp}"
        dbg.mkdir(parents=True, exist_ok=True)
        dev = ["--device", serial] if (serial := os.environ.get("ANDROID_SERIAL")) else []
        r = run([exe, *dev, "test", "--debug-output", str(dbg), str(flow.resolve())], log_dir, log=log, timeout=1800)
        return CheckRun(
            r.ok,
            f"{'ok' if r.ok else 'FAILED'}: maestro {flow.name}" + ("" if r.ok else f"\n{(r.out + r.err)[-1500:]}"),
            log,
            maestro.steps(dbg),
        )
