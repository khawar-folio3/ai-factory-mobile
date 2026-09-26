from __future__ import annotations

from mobile_factory.autonomy import RiskSignals, assess, gate_is_automatic
from mobile_factory.config import AutonomyConfig, GateConfig

CFG = AutonomyConfig(ceiling=4)


def test_tiny_copy_fix_is_fully_autonomous() -> None:
    r = assess(RiskSignals(category="copy", estimated_files=1), CFG)
    assert r.score == 0 and r.level == 4


def test_ceiling_caps_the_level() -> None:
    r = assess(RiskSignals(category="copy", estimated_files=1), CFG, ceiling=1)
    assert r.allowed_level == 4 and r.level == 1


def test_auth_area_drops_to_supervised_or_lower() -> None:
    r = assess(RiskSignals(estimated_files=2, risk_classes=["auth"]), CFG)
    assert r.score == 46 and r.level == 2
    assert "high-risk area: auth" in r.explain()


def test_broad_change_is_manual() -> None:
    s = RiskSignals(actual_files=12, modules_touched=4, lines_changed=400, public_api_change=True, reproduced=False)
    r = assess(s, CFG)
    assert r.score == 100 and r.level == 0


def test_actual_diff_overrides_estimate() -> None:
    low = assess(RiskSignals(estimated_files=1), CFG)
    high = assess(RiskSignals(estimated_files=1, actual_files=9, lines_changed=120), CFG)
    assert high.score > low.score


def test_gate_threshold() -> None:
    assert gate_is_automatic(GateConfig(auto_at=3), 3)
    assert not gate_is_automatic(GateConfig(auto_at=3), 2)
    assert not gate_is_automatic(GateConfig(auto_at=5), 4)
