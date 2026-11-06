"""Time manager. Pure: clock, the position's difficulty tier, pondering and the engine's live latency in, Plan out.

Each move gets a share of the clock, scaled by how hard the position is: easy moves bank time and hard ones spend it.
Pondering makes many moves instant (21 of 38 in a measured 3+2 game), so the clock is shared among the moves expected
to cost time, using the game's own ponder hit rate. A ponder run thinks on the opponent's time, so it may plan for
most of the opponent's usual think time, though never past what the model's own clock could cover if the opponent
moved at once.
The level is the deepest one that fits, because the public leaderboards show more reasoning effort plays better; a
slower clock buys a deeper level. A level fits when its expected latency (the mean, which a long slow tail lifts
above the median) is within the budget and its slow case (p90) within the deadline of twice the budget. Choosing by
the median let half the moves overshoot: in a 5+3 game, normal moves at high took 25 to 35 s against 14 to 25 s
budgets, and the clock ran out. Within a level the model's adaptive thinking decides how long to think.

Every plan calls the model. No move may come from code: when even the cheapest level does not fit, it runs anyway and
the model may lose on time.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .config import Level
from .difficulty import Tier
from .engines import LatencyTable

SAFETY_MARGIN_MS = 1_500
# Easy positions are about 46% of a game past the opening and hard ones 22% (zeitnot/difficulty.py), so these
# spend about one share per move on average.
SHARE: dict[Tier, float] = {"easy": 0.6, "normal": 1.0, "hard": 2.0}
MAX_BUDGET = 0.30  # of the clock left
# The ponder hit rate starts at a prior worth four moves and is capped, so a lucky start cannot drain the clock.
INSTANT_PRIOR = 0.3
INSTANT_PRIOR_MOVES = 4
INSTANT_MAX = 0.5
PAID_MOVES_MIN = 5
WINDOW_SHARE = 0.6  # of the opponent's median think time a ponder run plans to use


def instant_share(ponder_hits: int, model_moves: int) -> float:
    """The share of the model's coming moves expected to play at once from pondering."""
    rate = (ponder_hits + INSTANT_PRIOR * INSTANT_PRIOR_MOVES) / (model_moves + INSTANT_PRIOR_MOVES)
    return min(INSTANT_MAX, rate)


@dataclass(frozen=True)
class Clock:
    ai_ms: int
    human_ms: int
    increment_ms: int


@dataclass(frozen=True)
class Plan:
    level: Level
    budget_ms: int
    deadline_ms: int
    reason: str


def plan(clock: Clock, ply: int, tier: Tier, latency: LatencyTable, levels: Sequence[Level], instant: float = 0.0,
         opponent_ms: int | None = None) -> Plan:
    """`levels` are the main engine's levels, cheapest first. `instant` is instant_share(); `opponent_ms`, for a
    ponder run only, is the opponent's median think time."""
    moves_to_go = max(10, 40 - ply // 2)
    paid = max(PAID_MOVES_MIN, moves_to_go * (1 - instant))
    share = clock.ai_ms / paid + 0.8 * clock.increment_ms
    budget = int(min(SHARE[tier] * share, MAX_BUDGET * clock.ai_ms))
    cap = int(max(0, min(MAX_BUDGET * clock.ai_ms, clock.ai_ms - SAFETY_MARGIN_MS)))
    # If the opponent moves before the run ends, the model waits for it on its own clock: never past the cap.
    window = min(int(WINDOW_SHARE * opponent_ms), cap // 2) if opponent_ms else 0
    budget = max(budget, window)
    fits = [level for level in levels if latency.expected(level) <= budget and latency.p90(level) <= 2 * budget]
    level = fits[-1] if fits else levels[0]
    deadline = min(max(2 * budget, latency.p90(level)), cap)
    if fits:
        reason = f"{tier}: {level} fits a {budget} ms budget (expected {latency.expected(level)} ms)"
    else:
        reason = f"{tier}: no level fits a {budget} ms budget, {level} anyway (expected {latency.expected(level)} ms)"
    return Plan(level, budget, deadline, reason)


def second_level(level: Level, budget_ms: int, latency: LatencyTable, levels: Sequence[Level]) -> Level:
    """The second opinion's level on a hard move: one deeper than the main call when its typical latency fits the
    budget. Its slow case need not fit, because the main answer covers it: the second opinion is waited for only until
    the budget runs out. So Opus's xhigh, too slow in the slow case to be the main call before 15+10, thinks beside a
    high main call from 5+3 on."""
    i = levels.index(level)
    if i + 1 < len(levels) and latency.p50(levels[i + 1]) <= budget_ms:
        return levels[i + 1]
    return level
