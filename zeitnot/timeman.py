"""Time manager v1. Pure: clock and position in, Plan out."""

from collections.abc import Mapping
from dataclasses import dataclass

from .config import MAIN_LEVELS, Level

SAFETY_MARGIN_MS = 1_500
FALLBACK_ONLY_BELOW_MS = 10_000
NO_CALL_BELOW_MS = 2_000


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


def plan(clock: Clock, ply: int, legal_count: int, latency: Mapping[Level, int]) -> Plan:
    """deadline_ms == 0 means no API call is worth making: the caller plays the emergency move."""
    if legal_count <= 1:
        return Plan("none", 0, 0, "forced move")
    if clock.ai_ms < NO_CALL_BELOW_MS:
        return Plan("none", 0, 0, f"under {NO_CALL_BELOW_MS} ms left, rules-only move")

    moves_to_go = max(10, 40 - ply // 2)
    budget = int(min(clock.ai_ms / moves_to_go + 0.8 * clock.increment_ms, 0.25 * clock.ai_ms))
    deadline = int(max(0, min(2 * budget, 0.30 * clock.ai_ms, clock.ai_ms - SAFETY_MARGIN_MS)))

    if clock.ai_ms < FALLBACK_ONLY_BELOW_MS:
        return Plan("none", budget, deadline, f"under {FALLBACK_ONLY_BELOW_MS} ms left, fallback only")
    for level in MAIN_LEVELS:
        if latency[level] <= budget:
            return Plan(level, budget, deadline, f"{level} fits a {budget} ms budget (p50 {latency[level]} ms)")
    return Plan("none", budget, deadline, f"budget {budget} ms is below every level's p50, fallback only")
