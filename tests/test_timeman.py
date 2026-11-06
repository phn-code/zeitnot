import pytest

from zeitnot.config import LATENCY_SEED
from zeitnot.engines import LatencyTable
from zeitnot.timeman import Clock, instant_share, plan, second_level

CODEX = ("low", "medium")


def clock(ai, inc=0):
    return Clock(ai_ms=ai, human_ms=ai, increment_ms=inc)


def codex():
    return LatencyTable(LATENCY_SEED["codex"])


@pytest.mark.parametrize(
    ("clk", "tier", "expected"),
    [
        pytest.param(clock(900_000, 10_000), "easy", ("low", 18_300, 55_000), id="15+10 easy: 0.6 share, nothing fits, low with its p90"),
        pytest.param(clock(900_000, 10_000), "normal", ("low", 30_500, 61_000), id="15+10 normal: medium's p90 misses the deadline"),
        pytest.param(clock(900_000, 10_000), "hard", ("medium", 61_000, 122_000), id="15+10 hard: 2x share, the deepest level that fits"),
        pytest.param(clock(600_000, 5_000), "normal", ("low", 19_000, 55_000), id="10+5 normal"),
        pytest.param(clock(600_000, 5_000), "hard", ("low", 38_000, 76_000), id="10+5 hard: medium's slow case does not fit"),
        pytest.param(clock(180_000, 2_000), "hard", ("low", 12_200, 54_000), id="3+2 hard: low's p90, capped at 30% of clock"),
        pytest.param(clock(5_000), "normal", ("low", 125, 1_500), id="5 s left: still a model call"),
        pytest.param(clock(1_000), "hard", ("low", 50, 0), id="1 s left: still a model call, no deadline"),
    ],
)
def test_plan_table(clk, tier, expected):
    p = plan(clk, 0, tier, codex(), CODEX)
    assert (p.level, p.budget_ms, p.deadline_ms) == expected and p.reason.startswith(tier)


def test_easy_moves_bank_time_and_hard_moves_spend_it():
    budgets = [plan(clock(600_000, 5_000), 20, t, codex(), CODEX).budget_ms for t in ("easy", "normal", "hard")]
    assert budgets == [14_400, 24_000, 48_000]


def test_the_deepest_level_whose_typical_and_slow_cases_both_fit():
    table = LatencyTable({"low": (5_000, 9_000), "medium": (20_000, 30_000), "high": (30_000, 200_000)})
    p = plan(clock(600_000, 5_000), 0, "hard", table, ("low", "medium", "high"))
    assert p.level == "medium", "high's p50 fits the 38 s budget but its 200 s slow case misses the 76 s deadline"


def test_budget_is_capped_at_30_percent_of_the_clock():
    p = plan(clock(20_000, 30_000), 0, "hard", LatencyTable({"low": (10, 10), "medium": (20, 20)}), CODEX)
    assert p.budget_ms == 6_000 and p.level == "medium"


def test_plan_follows_the_live_table():
    table = LatencyTable({"low": (5_000, 9_000), "medium": (20_000, 30_000)})
    assert plan(clock(600_000, 5_000), 0, "hard", table, CODEX).level == "medium"
    for _ in range(3):
        table.record("medium", 60_000)
    assert plan(clock(600_000, 5_000), 0, "hard", table, CODEX).level == "low"


def test_latency_table_uses_the_seed_until_three_calls_then_a_window():
    table = LatencyTable({"low": (10_000, 55_000)})
    table.record("low", 4_000)
    table.record("low", 6_000)
    assert (table.p50("low"), table.p90("low")) == (10_000, 55_000)
    table.record("low", 8_000)
    assert (table.p50("low"), table.p90("low")) == (6_000, 8_000)
    for _ in range(LatencyTable.WINDOW):
        table.record("low", 1_000)
    assert table.p90("low") == 1_000


def test_the_ponder_hit_rate_starts_at_a_prior_and_is_capped():
    assert instant_share(0, 0) == pytest.approx(0.3)
    assert instant_share(4, 8) == pytest.approx((4 + 1.2) / 12)
    assert instant_share(20, 20) == 0.5, "a lucky run of hits cannot claim more than half the moves"


def test_moves_expected_from_pondering_free_up_time_for_the_rest():
    plain = plan(clock(300_000, 3_000), 20, "normal", codex(), CODEX)
    pondered = plan(clock(300_000, 3_000), 20, "normal", codex(), CODEX, instant=0.5)
    assert (plain.budget_ms, pondered.budget_ms) == (12_400, 22_400)


def test_a_ponder_run_may_plan_for_the_opponents_think_time_within_the_cap():
    table = LatencyTable({"low": (5_000, 9_000), "medium": (20_000, 30_000)})
    quick = plan(clock(300_000, 3_000), 20, "normal", table, CODEX, opponent_ms=10_000)
    slow = plan(clock(300_000, 3_000), 20, "normal", table, CODEX, opponent_ms=60_000)
    assert (quick.level, quick.budget_ms) == ("low", 12_400), "a quick opponent leaves the move's own budget"
    assert (slow.level, slow.budget_ms) == ("medium", 36_000), "0.6 of a 60 s think"
    short = plan(clock(20_000, 0), 60, "normal", table, CODEX, opponent_ms=60_000)
    assert short.budget_ms <= 3_000, "never longer than the model's own clock could cover"


def test_the_second_opinion_thinks_one_level_deeper_when_its_typical_time_fits():
    opus = LatencyTable(LATENCY_SEED["claude"])
    levels = ("low", "medium", "high", "xhigh")
    assert second_level("high", 43_000, opus, levels) == "xhigh", "xhigh's 20 s median fits; its 117 s slow case need not"
    assert second_level("high", 15_000, opus, levels) == "high", "no deeper level fits a 15 s budget"
    assert second_level("xhigh", 90_000, opus, levels) == "xhigh", "max is not one of the levels"


def test_a_level_is_budgeted_by_its_expected_time_not_its_median():
    table = LatencyTable({"low": (5_000, 9_000), "high": (14_000, 40_000)})
    p = plan(clock(300_000, 3_000), 20, "normal", table, ("low", "high"))
    assert p.budget_ms == 12_400 and p.level == "low", "high's 14 s median would fit, its 27 s expected time does not"
    for ms in (8_000, 9_000, 10_000):
        table.record("high", ms)
    assert table.expected("high") == 9_000 and plan(clock(300_000, 3_000), 20, "normal", table, ("low", "high")).level == "high"
