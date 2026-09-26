from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from ..config import AndroidConfig
from ..errors import FactoryError
from ..proc import has, run
from .base import Check, CheckRun, Platform

_LABEL = re.compile(r'(?:text|content-desc)="([^"]+)"')
_BOUNDS = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
_FRAGMENT = re.compile(r"#\d+: ([A-Za-z0-9_]+Fragment)\{")
_ERR = re.compile(r"^e: |error:|FAILED|What went wrong|Lint found|tests completed|Exception")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%H%M%S")


def _tail(text: str, pattern: re.Pattern[str] = _ERR, n: int = 25) -> str:
    lines = text.splitlines()
    hits = [ln for ln in lines if pattern.search(ln)][:n]
    return "\n".join([*hits, "...", *lines[-12:]])


class Android(Platform):
    name = "android"

    def __init__(self, root: Path, cfg: AndroidConfig) -> None:
        self.root = root
        self.cfg = cfg

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
                has("maestro"),
                "" if has("maestro") else "optional: curl -fsSL https://get.maestro.mobile.dev | bash",
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

    def build_install(self, log_dir: Path, launch: bool = True) -> CheckRun:
        self.ensure_device()
        res = self.gradle([f":{self.cfg.app_module}:install{self.cfg.variant}"], log_dir / f"install-{_stamp()}.log")
        if res.ok and launch:
            res = CheckRun(True, f"{res.summary}\n{self.launch()}", res.log)
        return res

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

    def screen_state(self) -> str:
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
        return f"activity  {top}\nfragments {' '.join(frags[-6:])}\nlabels    {'|'.join(labels[:60])}\n"

    def screenshot(self, dest: Path) -> None:
        prev = ""
        for _ in range(8):  # settle: two identical frames a second apart
            data = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, check=False).stdout
            if not data:
                raise FactoryError("screencap failed")
            dest.write_bytes(data)
            cur = hashlib.sha256(data).hexdigest()
            if cur == prev:
                return
            prev = cur
            time.sleep(1)

    def tap(self, label: str, nth: int = 1) -> str:
        nodes = [ln for ln in self._dump().splitlines() if _LABEL.search(ln)]
        exact = [ln for ln in nodes if label in _LABEL.findall(ln)]
        loose = [ln for ln in nodes if label.lower() in ln.lower()]
        hits = exact or loose
        if len(hits) < nth:
            raise FactoryError(f"no element matching '{label}' on screen")
        m = _BOUNDS.search(hits[nth - 1])
        if not m:
            raise FactoryError(f"element '{label}' has no bounds")
        x1, y1, x2, y2 = map(int, m.groups())
        x, y = (x1 + x2) // 2, (y1 + y2) // 2
        self.adb("shell", "input", "tap", str(x), str(y))
        return f"tapped ({x},{y}) '{_LABEL.findall(hits[nth - 1])[0]}'"

    def wait_for(self, text: str, timeout: int = 30) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if text.lower() in self._dump().lower():
                return True
            time.sleep(2)
        return False

    def open_link(self, link: str) -> str:
        if "://" not in link:
            if not self.cfg.deeplink_scheme:
                raise FactoryError("set android.deeplink_scheme or pass a full URI")
            link = f"{self.cfg.deeplink_scheme}://{link.lstrip('/')}"
        pkg = [self.cfg.application_id] if self.cfg.application_id else []
        self.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"'{link}'", *pkg)
        return f"opened {link}"

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

    def run_flow(self, flow: Path, log_dir: Path) -> CheckRun:
        if not has("maestro"):
            raise FactoryError("maestro not installed: curl -fsSL https://get.maestro.mobile.dev | bash")
        log = log_dir / f"flow-{flow.stem}-{_stamp()}.log"
        r = run(["maestro", "test", str(flow)], self.root, log=log, timeout=1800)
        return CheckRun(
            r.ok,
            f"{'ok' if r.ok else 'FAILED'}: maestro {flow.name}" + ("" if r.ok else f"\n{(r.out + r.err)[-1500:]}"),
            log,
        )
