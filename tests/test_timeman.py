import pytest

from zeitnot.config import LATENCY_P50_MS
from zeitnot.timeman import Clock, Plan, plan


def clock(ai, inc=0):
    return Clock(ai_ms=ai, human_ms=ai, increment_ms=inc)


@pytest.mark.parametrize(
    ("clk", "ply", "legal", "expected"),
    [
        pytest.param(clock(60_000), 0, 20, ("none", 1_500, 3_000), id="1+0 start: budget below low's p50, 3 s deadline"),
        pytest.param(clock(180_000, 2_000), 0, 20, ("low", 6_100, 12_200), id="3+2 start: low"),
        pytest.param(clock(300_000, 3_000), 0, 20, ("low", 9_900, 19_800), id="5+3 start: low"),
        pytest.param(clock(600_000, 5_000), 0, 20, ("medium", 19_000, 38_000), id="10+5 start: medium"),
        pytest.param(clock(900_000, 10_000), 0, 20, ("medium", 30_500, 61_000), id="15+10 start: medium"),
        pytest.param(clock(300_000), 0, 20, ("low", 7_500, 15_000), id="5+0 start: only low fits"),
        pytest.param(clock(60_000, 5_000), 80, 20, ("low", 10_000, 18_000), id="10+5 late game: low, deadline capped at 30% of clock"),
        pytest.param(clock(600_000), 60, 30, ("medium", 60_000, 120_000), id="late game: moves_to_go floors at 10"),
        pytest.param(clock(600_000), 200, 30, ("medium", 60_000, 120_000), id="very late ply stays at floor"),
        pytest.param(clock(20_000, 30_000), 0, 20, ("low", 5_000, 6_000), id="budget capped at 25% of clock"),
        pytest.param(clock(8_000, 5_000), 40, 20, ("none", 2_000, 2_400), id="panic: under 10 s, fallback only"),
        pytest.param(clock(10_000, 30_000), 0, 20, ("none", 2_500, 3_000), id="10 s exactly is not panic"),
        pytest.param(clock(1_999, 5_000), 0, 20, ("none", 0, 0), id="emergency: under 2 s, no call"),
        pytest.param(clock(600_000, 5_000), 10, 1, ("none", 0, 0), id="forced move: nothing to plan"),
    ],
)
def test_plan_table(clk, ply, legal, expected):
    p = plan(clk, ply, legal, LATENCY_P50_MS)
    assert (p.level, p.budget_ms, p.deadline_ms) == expected
    assert isinstance(p, Plan) and p.reason


def test_plan_picks_medium_over_low_when_both_fit():
    latency = {"medium": 1_000, "low": 500, "minimal": 100}
    assert plan(clock(600_000), 0, 20, latency).level == "medium"


def test_plan_level_boundary_is_inclusive():
    latency = {"medium": 19_000, "low": 5_000, "minimal": 2_500}
    assert plan(clock(600_000, 5_000), 0, 20, latency).level == "medium"
    latency["medium"] = 19_001
    assert plan(clock(600_000, 5_000), 0, 20, latency).level == "low"


def test_panic_threshold_is_10_seconds_on_the_model_clock():
    fast = {"medium": 100, "low": 50, "minimal": 20}
    assert plan(clock(10_000, 30_000), 0, 20, fast).level == "medium"
    assert plan(clock(9_999, 30_000), 0, 20, fast).level == "none"
