from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import LoadedConfig
from .errors import FactoryError
from .platforms import android as droid
from .platforms import maestro
from .platforms.base import Platform
from .proc import has
from .timeline import dur

ROUTE = "p_omx"
FLOW = "omx_params_meeting"  # saved project flow: its plain tapOn texts are the safe tabs to tap
TABS = ("Room", "Seat")
UNSAFE = re.compile(r"\b(book|search|confirm|delete|cancel|remove|decline|save|submit)\b|\d{1,2}:\d{2}", re.I)
SIGN_IN = re.compile(r"^(sign ?in|log ?in)\b", re.I)
SETTLE_MS = 3000
HOME_WAIT = 20.0


@dataclass
class Step:
    name: str
    ok: bool
    ms: int
    detail: str = ""
    skipped: bool = False

    @property
    def mark(self) -> str:
        return "-" if self.skipped else "✓" if self.ok else "✗"

    def line(self) -> str:
        return f"{self.mark} {self.name:<14} {dur(self.ms):>7}  {self.detail}"


def labels(state: str) -> list[str]:
    return next((ln.split(None, 1)[1].split("|") for ln in state.splitlines() if ln.startswith("labels ")), [])


def signed_out(state: str) -> bool:
    return any(SIGN_IN.match(lb.strip()) for lb in labels(state))


def safe_taps(flow: Path) -> list[str]:
    """Plain-text tapOn targets of a saved flow (no regex, so never a card) that are not action buttons."""
    if not flow.is_file():
        return []
    taps = re.findall(r'^- tapOn: "([^"]+)"$', flow.read_text(), re.M)
    return [t for t in dict.fromkeys(taps) if not re.search(r"[.*()|\[\]\\^$+?]", t) and not UNSAFE.search(t)]


def _flow_link(flow: Path, path: str) -> str:
    """The saved flow's own deeplink for `path` (keeps the app's link form, e.g. scheme://dl/<page>)."""
    links = re.findall(r'^- openLink: "([^"]+)"$', flow.read_text(), re.M) if flow.is_file() else []
    return next((ln for ln in links if ln.rstrip("/").endswith(f"/{path}") or ln.endswith(f"://{path}")), "")


class SelfTest:
    """`factory doctor --device`: every device primitive on the real emulator, timed; writes only under `out`."""

    def __init__(
        self, lc: LoadedConfig, p: Platform, out: Path, say: Callable[[str], None], ask: Callable[[str], bool]
    ) -> None:
        self.lc, self.p, self.out, self.say, self.ask = lc, p, out, say, ask
        self.steps: list[Step] = []
        self.flows = lc.path(lc.cfg.android.flows_dir)
        self.saved = self.flows / maestro.LIBRARY / f"{FLOW}.yaml"
        self.ctx: dict[str, str] = {}
        self.state = ""
        self.recorded: list[str] = []

    def _do(self, name: str, fn: Callable[[], tuple[bool, str]], skipped: bool = False) -> Step:
        t0 = time.monotonic()
        try:
            ok, detail = fn()
        except (FactoryError, OSError, ValueError) as e:
            ok, detail = False, " ".join(str(e).split())[:200]
        s = Step(name, ok or skipped, int((time.monotonic() - t0) * 1000), detail, skipped)
        self.steps.append(s)
        self.say(s.line())
        return s

    def run(self) -> list[Step]:
        t0 = time.monotonic()
        self.out.mkdir(parents=True, exist_ok=True)
        for name, fn, critical in (
            ("context", self.context, True),
            ("signed in", self.signed_in, True),
            ("route + open", self.route_open, False),
            ("tap polling", self.tap_polling, False),
            ("snapshot", self.snapshot, False),
            ("record+replay", self.replay, False),
            ("saved flow", self.saved_flow, False),
            ("install skip", self.install_skip, False),
        ):
            if not self._do(name, fn).ok and critical:
                break
        else:
            self._do("mitmproxy", self.mitmproxy, not has("mitmdump"))
        self.say(self.table(int((time.monotonic() - t0) * 1000)))
        return self.steps

    def table(self, total: int) -> str:
        rows = [s.line() for s in self.steps]
        passed = sum(s.ok and not s.skipped for s in self.steps)
        failed = sum(not s.ok for s in self.steps)
        foot = f"{passed} passed, {failed} failed, {sum(s.skipped for s in self.steps)} skipped   total {dur(total)}"
        return "\n".join(["", "== selftest summary", *rows, foot, f"out: {self.out}"])

    def context(self) -> tuple[bool, str]:
        c = self.ctx = self.p.build_context()
        miss = [k for k in ("module", "variant", "package") if not c.get(k)]
        scheme = c.get("deeplink", "") or "no scheme"
        return not miss, f"{c.get('module')} {c.get('variant', '')[:40]} {c.get('package')} {scheme}" + (
            f"  missing: {', '.join(miss)}" if miss else ""
        )

    def _home(self) -> str:
        """Launch and poll until two equal non-empty screen states in a row (splash done), capped."""
        self.p.launch()
        prev, end = "", time.monotonic() + HOME_WAIT
        while time.monotonic() < end:
            cur = self.p.screen_state()
            if labels(cur) and cur == prev:
                return cur
            prev = cur
            time.sleep(1)
        return prev

    def signed_in(self) -> tuple[bool, str]:
        self.state = self._home()
        if signed_out(self.state):
            if not self.ask("Sign in on the emulator, then press Enter"):
                return False, "signed out: sign in on the emulator, then rerun (credentials are never typed)"
            self.state = self._home()
            if signed_out(self.state):
                return False, "still on the sign-in screen"
        top = self.state.splitlines()[0].split()[-1] if self.state else "?"
        return bool(labels(self.state)), f"{top.rsplit('.', 1)[-1]}  {len(labels(self.state))} labels"

    def route_open(self) -> tuple[bool, str]:
        graph = self.flows / droid.ROUTES
        if not (hits := droid.routes(graph, ROUTE)):
            return False, f"no route for {ROUTE} in {graph}"
        path = hits[0].split(" > ")[0].strip()
        link = _flow_link(self.saved, path) or f"{self.ctx.get('deeplink', '')}{path}"
        if "://" not in link:
            return False, f"no deeplink scheme to open {path}"
        t0 = time.monotonic()
        self.p.open_link(link)
        ms, changed = int((time.monotonic() - t0) * 1000), self.p.changed
        self.recorded += maestro.open_link(link)
        self.state = self.p.screen_state()
        n = len(labels(self.state))
        return changed, f"{link} -> {n} labels, {'changed' if changed else 'UNCHANGED'} in {dur(ms)}"

    def tap_polling(self) -> tuple[bool, str]:
        tabs = [t for t in TABS if t in safe_taps(self.saved)] or [t for t in TABS if not UNSAFE.search(t)]
        out, ok = [], True
        for t in tabs:
            self.p.tap(t)
            ms, changed = self.p.settle_ms, self.p.changed
            self.recorded += maestro.tap(self.p.last_hit, t)
            ok &= changed and ms <= SETTLE_MS
            out.append(f"{t} {ms}ms{'' if changed else ' UNCHANGED'}")
        return ok and bool(tabs), "tap->settled " + ", ".join(out) if out else "no safe tab in the saved flow"

    def snapshot(self) -> tuple[bool, str]:
        label = f"selftest_{datetime.now(UTC).strftime('%H%M%S')}"
        png, state = self.p.snapshot(self.out / "snapshots", "after", label)
        ok = png.is_file() and png.with_suffix(".txt").is_file()
        ocr = "ocr line" if any(ln.startswith("ocr ") for ln in state.splitlines()) else "NO ocr line"
        return ok, f"{png.name} + .txt, {ocr}"

    def _replay(self, flow: Path) -> tuple[bool, str]:
        if not maestro.binary():
            return False, "maestro not installed"
        res = maestro.replay(self.p, flow, self.out / "logs")
        steps = res.steps
        bad = [s["step"] for s in steps if not s["ok"]]
        total = sum(int(s["duration_ms"]) for s in steps)
        per = " ".join(f"{'ok' if s['ok'] else 'FAIL'}:{s['step'].split()[0]}" for s in steps)
        head = f"{sum(s['ok'] for s in steps)}/{len(steps)} steps, maestro {dur(total)}"
        return res.ok and bool(steps) and not bad, f"{head}  {per[:160]}" + (f"  failed: {bad[0]}" if bad else "")

    def replay(self) -> tuple[bool, str]:
        if not self.recorded:
            return False, "nothing recorded (route + open / tap polling failed)"
        flow = self.out / "flows" / "selftest.yaml"
        flow.unlink(missing_ok=True)
        maestro.record(flow, self.ctx.get("package") or self.lc.cfg.android.application_id, self.recorded)
        return self._replay(flow)

    def saved_flow(self) -> tuple[bool, str]:
        return self._replay(self.saved) if self.saved.is_file() else (False, f"no saved flow {self.saved}")

    def mitmproxy(self) -> tuple[bool, str]:
        return (True, "installed") if has("mitmdump") else (False, "not installed: mock test skipped")

    def install_skip(self) -> tuple[bool, str]:
        logs, times, runs = self.out / "logs", [], []
        for _ in range(2):
            t0 = time.monotonic()
            r = self.p.build_install(logs, launch=False)
            times.append(int((time.monotonic() - t0) * 1000))
            if not r.ok:
                return False, r.summary.splitlines()[0]
            runs.append(r.summary)
        second = runs[1]
        ok = "build skipped" in second and "install skipped" in second
        first = "built" if "build skipped" not in runs[0] else "build skipped"
        return ok, f"1st {dur(times[0])} ({first}), 2nd {dur(times[1])}" + (
            "  (build+install skipped)" if ok else f"  2nd did not skip: {' / '.join(second.splitlines())[:120]}"
        )
