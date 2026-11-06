"""Harness v1 against v2 on the same positions, graded by Stockfish 19 at depth 16 as in P0 (docs/harness.md, Evaluation).

Arms per engine, all at `low`: the v1 prompt, the v2 context, and the v2 context plus verify when the rules check
fails. The verify arm reuses the v2 arm's answer and adds the verify call, so its effect is measured on the same
first move. With --opinions, a fourth arm adds the Phase C second opinion on the critical positions: it reuses the
v2 record as the main answer, makes one "second" call with the most-forcing framing, and when the two disagree, one
"escalate" record holds the two reply calls and the pick (zeitnot.compare.escalate). The shallow-search arm was
removed with the search experiment. Every call is appended to
out/calls.jsonl and skipped on rerun, so a finished call is never repeated and existing records are never touched.

    .venv/bin/python experiments/harness_eval/run.py --dry-run           # show the positions, make no calls
    .venv/bin/python experiments/harness_eval/run.py                     # Claude Opus if logged in, else Codex
    .venv/bin/python experiments/harness_eval/run.py --opinions --limit 10   # the opinions arm, ten positions at a time
    .venv/bin/python experiments/harness_eval/run.py --engines codex     # Codex Sol only
    .venv/bin/python experiments/harness_eval/run.py --engines codex,gemini --max-gemini-usd 1.00
    .venv/bin/python experiments/harness_eval/run.py --report-only       # grade and report what is recorded
"""

import argparse
import asyncio
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.pgn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments" / "p0_probe"))

import probe  # noqa: E402

from zeitnot import compare  # noqa: E402
from zeitnot.calls import Calls, Landed  # noqa: E402
from zeitnot.claude import ClaudeEngine, claude_available  # noqa: E402
from zeitnot.codex import AppServer, CodexEngine  # noqa: E402
from zeitnot.config import CODEX_MODEL, GEMINI_MODEL, LATENCY_SEED  # noqa: E402
from zeitnot.context import ANSWER, FORCING_TASK, SYSTEM, Situation, build  # noqa: E402
from zeitnot.engines import Answer, Call, LatencyTable  # noqa: E402
from zeitnot.facts import compute, opponent_replies  # noqa: E402
from zeitnot.rules import check_move  # noqa: E402
from zeitnot.timeman import Clock  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
P0 = ROOT / "experiments" / "p0_probe" / "out"
GAME_PGN = ROOT / "experiments" / "app_smoke" / "out" / "game-20261106-120826.pgn"
GAME_PLIES = range(14, 23)
FAST_RUNGS = ("decisions-fen", "decisions-rich", "responses-none")
N_CRITICAL, N_QUIET = 40, 20
BLUNDER, MISTAKE = 30.0, 20.0
LEVEL = "low"
# The most one Gemini `low` call is allowed to cost: about 10k thinking tokens. Reserved before the call, so the cap holds.
GEMINI_RESERVE_USD = 0.05
# A nominal 10+5 middle-game clock for the v2 context, since offline positions have no clock.
NOMINAL = Clock(ai_ms=480_000, human_ms=480_000, increment_ms=5_000)
V1_INSTRUCTIONS = (
    "You are a strong chess player. Check every capture, check and threat for both sides, "
    "then choose the best move for the side to move."
)
ARMS = ("v1", "v2", "v2+verify", "opinions")
# Records before Phase B have no "prompt" field: their v2 answers were {move, note}, with no commentary.
# Phase C answers also carry expected_reply. The opinions arm's own calls use it; its main answer is the v2 record.
# Records from before the final version saw allowed moves tagged "hangs piece"; the "-untagged" prompts do not.
PROMPT = "commentary-untagged"
PROMPT_C = "commentary+expected_reply-untagged"
OPINION_KINDS = ("second", "escalate")


def v1_prompt(board: chess.Board, choices: list[str]) -> str:
    """zeitnot v1's rules.render, frozen here as the baseline."""
    rows = []
    for rank in range(7, -1, -1):
        cells = [(p.symbol() if (p := board.piece_at(chess.square(f, rank))) else ".") for f in range(8)]
        rows.append(f"{rank + 1} " + " ".join(cells))
    rows.append("  a b c d e f g h")
    side = "White" if board.turn == chess.WHITE else "Black"
    movetext = chess.Board().variation_san(board.move_stack) or "(none)"
    return (f"Chess position (FEN): {board.fen()}\n\nBoard (uppercase is White, lowercase is Black):\n" + "\n".join(rows)
            + f"\n\nMoves so far: {movetext}\n\n{side} to move.\n\nLegal moves: {', '.join(choices)}")


def board_of(pos: dict) -> chess.Board:
    board = chess.Board()
    for uci in pos["history"]:
        board.push_uci(uci)
    return board


def select_positions() -> list[dict]:
    path = OUT / "positions.jsonl"
    if path.exists():
        return probe.load_jsonl(path)
    p0 = probe.load_jsonl(P0 / "positions.jsonl")
    grades = json.loads((P0 / "grades.json").read_text())
    moves: dict[str, list[str]] = {}
    for r in probe.load_jsonl(P0 / "api.jsonl"):
        if r.get("rung") in FAST_RUNGS and "error" not in r and "move" in r:
            moves.setdefault(r["pid"], []).append(r["move"])
    for r in probe.load_jsonl(P0 / "filtered.jsonl"):
        moves.setdefault(r["pid"], []).append(r["move"])
    for r in probe.load_jsonl(P0 / "fast_tier.jsonl"):
        if r.get("effort") == "none" and "move" in r:
            moves.setdefault(r["pid"], []).append(r["move"])

    def losses(pid: str) -> list[float]:
        win = grades[pid]["win"]
        return [grades[pid]["best"] - win[m] for m in moves.get(pid, []) if m in win]

    game_positions = []
    game = chess.pgn.read_game(GAME_PGN.open())
    board = game.board()
    for ply, move in enumerate(game.mainline_moves()):
        if ply in GAME_PLIES and board.turn == chess.WHITE:
            game_positions.append({"id": f"g{ply:03d}", "fen": board.fen(), "history": [m.uci() for m in board.move_stack],
                                   "human": board.san(move), "group": "critical", "source": "v1 game, the model's move"})
        board.push(move)

    blundered = sorted((p for p in p0 if any(x >= BLUNDER for x in losses(p["id"]))),
                       key=lambda p: (-sum(x >= BLUNDER for x in losses(p["id"])), p["id"]))
    critical = [{**p, "group": "critical", "source": "P0, a fast rung blundered"} for p in blundered[: N_CRITICAL - len(game_positions)]]
    quiet = [{**p, "group": "quiet", "source": "P0, no fast-rung mistake"}
             for p in p0 if losses(p["id"]) and max(losses(p["id"])) < MISTAKE][:N_QUIET]
    selected = game_positions + critical + quiet
    OUT.mkdir(parents=True, exist_ok=True)
    for p in selected:
        probe.append_jsonl(path, p)
    return selected


@dataclass(frozen=True)
class Job:
    pos: dict
    engine: str
    kind: str  # "v1", "v2", "verify", "second" or "escalate"


def call_for(job: Job, records: dict) -> Call:
    board = board_of(job.pos)
    legal = list(board.legal_moves)
    sans = [board.san(m) for m in legal]
    if job.kind == "v1":
        return Call(V1_INSTRUCTIONS, v1_prompt(board, sans), tuple(sans), LEVEL, board.fen(), with_note=False)
    situation = Situation(NOMINAL, "10+5", compute(board), None)
    if job.kind == "v2":
        return Call(SYSTEM, build(board, situation, legal), tuple(sans), LEVEL, board.fen())
    if job.kind == "second":
        return Call(SYSTEM, build(board, situation, legal, task=FORCING_TASK), tuple(sans), LEVEL, board.fen())
    proposed = board.parse_san(records[(job.pos["id"], job.engine, "v2")]["move"])
    suggestion, task = verify_parts(board, proposed, opponent_replies(board, proposed))
    return Call(SYSTEM, build(board, situation, legal, suggestion, task), tuple(sans), LEVEL, board.fen())


def _landed(board: chess.Board, key: str, san: str) -> Landed:
    return Landed(key, board.parse_san(san), Answer(san, None, 0, 0, 0), None)


async def escalate_move(pos: dict, engine, main_san: str, second_san: str, timeout: float) -> tuple[str, list[Answer], dict]:
    """The opinions arm on a split: the same zeitnot.compare.escalate the app runs, on the recorded v2 and second
    answers. Without a pick it plays the main answer, as the app does."""
    board = board_of(pos)
    situation = Situation(NOMINAL, "10+5", compute(board), None)
    calls = Calls(board)
    compared: list[compare.Compared] = []
    candidates = [_landed(board, "main", main_san), _landed(board, "second", second_san)]
    try:
        async for _ in compare.escalate(calls, engine, board, situation, candidates, time.monotonic() + timeout, compared):
            pass
    finally:
        await asyncio.gather(*calls.close(), return_exceptions=True)
    found = compared[0]
    move = board.san(found.pick.move) if found.pick else main_san
    detail = {"pick": board.san(found.pick.move) if found.pick else None, "cut": found.cut,
              "replies": {board.san(m): san_after(board, m, r) for m, r in found.replies.items()},
              "decision": found.text(board, candidates[0].move)}
    return move, calls.answers, detail


def san_after(board: chess.Board, move: chess.Move, reply: chess.Move) -> str:
    after = board.copy()
    after.push(move)
    return after.san(reply)


def verify_parts(board: chess.Board, proposed: chess.Move, replies: list[str]) -> tuple[str, str]:
    """The suggestion section and task for a verify call, the v2 stage the app dropped: the first move arrives as
    another player's suggestion."""
    san = board.san(proposed)
    listed = ", ".join(replies) if replies else "no checks or captures"
    suggestion = f"Another player suggests {san}. After it, the opponent can play these checks and captures: {listed}."
    return suggestion, f"Keep {san} or choose another move. " + ANSWER


def needs_verify(pos: dict, v2: dict | None) -> bool:
    if not v2 or "error" in v2:
        return False
    board = board_of(pos)
    return check_move(board, board.parse_san(v2["move"])) is not None


class GeminiBudget:
    def __init__(self, cap: float, spent: float):
        self.cap, self.spent, self.reserved = cap, spent, 0.0

    def reserve(self) -> bool:
        if self.spent + self.reserved + GEMINI_RESERVE_USD > self.cap:
            return False
        self.reserved += GEMINI_RESERVE_USD
        return True

    def settle(self, usd: float) -> None:
        self.reserved -= GEMINI_RESERVE_USD
        self.spent += usd


async def run_calls(positions: list[dict], engines: dict, args, path: Path) -> None:
    records = {(r["pid"], r["engine"], r["kind"]): r for r in probe.load_jsonl(path) if "error" not in r}
    gemini_spent = sum(r.get("usd", 0.0) for r in probe.load_jsonl(path) if r["engine"] == "gemini")
    budget = GeminiBudget(args.max_gemini_usd, gemini_spent)
    print(f"gemini spent so far ${gemini_spent:.4f} of ${args.max_gemini_usd:.2f}")
    sems = {"codex": asyncio.Semaphore(args.codex_workers), "gemini": asyncio.Semaphore(args.gemini_workers),
            "claude": asyncio.Semaphore(args.claude_workers)}

    async def one(job: Job) -> None:
        key = (job.pos["id"], job.engine, job.kind)
        if key in records:
            return
        async with sems[job.engine]:
            if job.engine == "gemini" and not budget.reserve():
                print(f"skip {key}: the Gemini cap ${args.max_gemini_usd:.2f} would be exceeded")
                return
            started = time.monotonic()
            answers: list[Answer] = []
            rec: dict = {"pid": job.pos["id"], "engine": job.engine, "kind": job.kind}
            if job.kind != "v1":
                rec["prompt"] = PROMPT_C if job.kind in OPINION_KINDS else PROMPT
            try:
                if job.kind == "escalate":
                    main_san = records[(job.pos["id"], job.engine, "v2")]["move"]
                    second_san = records[(job.pos["id"], job.engine, "second")]["move"]
                    san, answers, detail = await asyncio.wait_for(
                        escalate_move(job.pos, engines[job.engine], main_san, second_san, args.timeout), args.timeout)
                    rec.update(move=san, main=main_san, second=second_san, calls=len(answers), **detail)
                else:
                    c = call_for(job, records)
                    async with asyncio.timeout(args.timeout):
                        async with aclosing(engines[job.engine].stream(c)) as events:
                            async for event in events:
                                if isinstance(event, Answer):
                                    answers = [event]
                    answer = answers[0] if answers else None
                    if answer is None or answer.san not in c.choices:
                        raise ValueError(f"no legal answer: {answer.san if answer else None!r}")
                    rec.update(move=answer.san, note=answer.note)
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            usd = sum(a.usd for a in answers)
            if job.engine == "gemini":
                budget.settle(usd)
            tokens = [sum(a.input_tokens for a in answers), sum(a.thinking_tokens for a in answers),
                      sum(a.output_tokens for a in answers)] if answers else None
            rec.update(seconds=round(time.monotonic() - started, 2), usd=usd, tokens=tokens, at=time.time())
            probe.append_jsonl(path, rec)
            if "error" not in rec:
                records[key] = rec
            print(f"{job.engine:<6} {job.kind:<6} {job.pos['id']} {rec['seconds']:6.1f}s {rec.get('move', rec.get('error'))}", flush=True)

    first = [Job(p, e, k) for e in engines for p in positions for k in ("v1", "v2")]
    await asyncio.gather(*(one(j) for j in first))
    verify = [Job(p, e, "verify") for e in engines for p in positions if needs_verify(p, records.get((p["id"], e, "v2")))]
    print(f"{len(verify)} verify calls needed")
    await asyncio.gather(*(one(j) for j in verify))
    if not getattr(args, "opinions", False):
        return
    critical = [p for p in positions if p["group"] == "critical"]
    second = [Job(p, e, "second") for e in engines for p in critical if (p["id"], e, "v2") in records]
    await asyncio.gather(*(one(j) for j in second))
    split = [Job(p, e, "escalate") for e in engines for p in critical
             if (s := records.get((p["id"], e, "second"))) and s["move"] != records[(p["id"], e, "v2")]["move"]]
    print(f"{len(split)} of {sum((p['id'], e, 'second') in records for e in engines for p in critical)} second opinions disagree")
    await asyncio.gather(*(one(j) for j in split))


def arm_moves(positions: list[dict], records: dict, engine: str) -> dict[str, dict[str, tuple[str, float, float]]]:
    """arm -> pid -> (move, seconds, usd). The verify arm keeps the v2 move when no verify was needed."""
    out: dict[str, dict] = {arm: {} for arm in ARMS}
    for p in positions:
        pid = p["id"]
        v1, v2, ver, second, esc = (records.get((pid, engine, k)) for k in ("v1", "v2", "verify", "second", "escalate"))
        if v2 and second:
            # The main and second calls run in parallel in the app, so the wall time is the slower of the two.
            both = max(v2["seconds"], second["seconds"])
            if second["move"] == v2["move"]:
                out["opinions"][pid] = (v2["move"], both, v2["usd"] + second["usd"])
            elif esc:
                out["opinions"][pid] = (esc["move"], both + esc["seconds"], v2["usd"] + second["usd"] + esc["usd"])
        if v1:
            out["v1"][pid] = (v1["move"], v1["seconds"], v1["usd"])
        if v2:
            out["v2"][pid] = (v2["move"], v2["seconds"], v2["usd"])
            if not needs_verify(p, v2):
                out["v2+verify"][pid] = out["v2"][pid]
            elif ver:
                out["v2+verify"][pid] = (ver["move"], v2["seconds"] + ver["seconds"], v2["usd"] + ver["usd"])
    return out


def bootstrap(xs: list[float], n: int = 2000, seed: int = 11) -> tuple[float, float]:
    if not xs:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def report(positions: list[dict], records: dict, grades: dict, engines: list[str]) -> str:
    lines = ["# Harness eval", "",
             f"{len(positions)} positions ({sum(p['group'] == 'critical' for p in positions)} critical, "
             f"{sum(p['group'] == 'quiet' for p in positions)} quiet). Loss is the drop in win probability against "
             "Stockfish 19's best move at depth 16. Blunder means a loss of 30 or more. Intervals are 95% bootstrap "
             "intervals of the mean. Seconds are wall clock per call, with the configured number of calls in flight.", ""]
    for group in ("all", "critical", "quiet"):
        subset = [p for p in positions if group == "all" or p["group"] == group]
        lines += [f"## {group.title()} positions", "",
                  "| Engine | Arm | n | Mean loss (95% CI) | Blunders | vs v1, paired (95% CI) | Mean s | Total $ | $/move |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for engine in engines:
            arms = arm_moves(subset, records, engine)
            for arm in ARMS:
                rows = arms[arm]
                if not rows:
                    continue
                loss = {pid: probe.loss(grades, pid, mv) for pid, (mv, _, _) in rows.items()}
                xs = list(loss.values())
                lo, hi = bootstrap(xs)
                paired = [loss[pid] - probe.loss(grades, pid, arms["v1"][pid][0]) for pid in loss if pid in arms["v1"]]
                plo, phi = bootstrap(paired)
                diff = "" if arm == "v1" or not paired else f"{statistics.mean(paired):+.1f} ({plo:+.1f} to {phi:+.1f})"
                usd = sum(u for _, _, u in rows.values())
                lines.append(f"| {engine} | {arm} | {len(xs)} | {statistics.mean(xs):.1f} ({lo:.1f} to {hi:.1f}) "
                             f"| {sum(x >= BLUNDER for x in xs)} | {diff} | {statistics.mean(s for _, s, _ in rows.values()):.1f} "
                             f"| {usd:.4f} | {usd / len(xs):.5f} |")
        lines.append("")
    for engine in engines:
        lines += opinions_summary(positions, records, grades, engine)
    for engine in engines:
        fired = sum(1 for p in positions if needs_verify(p, records.get((p["id"], engine, "v2"))))
        changed = sum(1 for p in positions if (v := records.get((p["id"], engine, "verify")))
                      and v["move"] != records[(p["id"], engine, "v2")]["move"])
        lines.append(f"{engine}: the rules check failed on {fired} v2 answers; verify changed the move {changed} times.")
        prompts = {records[k].get("prompt", "note-only") for k in records if k[1] == engine and k[2] not in ("v1", *OPINION_KINDS)}
        if len(prompts) > 1:
            lines.append(f"{engine}: warning, the v2 and verify records mix prompts ({', '.join(sorted(prompts))}); "
                         "compare arms only within one prompt.")
    return "\n".join(lines) + "\n"


def opinions_summary(positions: list[dict], records: dict, grades: dict, engine: str) -> list[str]:
    """Disagreement, escalations, changed moves, loss against v2 paired, and time, for the opinions arm."""
    critical = [p for p in positions if p["group"] == "critical"]
    pairs = [(p["id"], records[(p["id"], engine, "v2")], records[(p["id"], engine, "second")]) for p in critical
             if (p["id"], engine, "v2") in records and (p["id"], engine, "second") in records]
    if not pairs:
        return []
    split = [pid for pid, v2, second in pairs if second["move"] != v2["move"]]
    escalated = [pid for pid in split if (pid, engine, "escalate") in records]
    picked = [pid for pid in escalated if records[(pid, engine, "escalate")].get("pick")]
    arms = arm_moves(critical, records, engine)
    rows = arms["opinions"]
    changed = [pid for pid, (mv, _, _) in rows.items() if mv != arms["v2"][pid][0]]
    paired = [probe.loss(grades, pid, rows[pid][0]) - probe.loss(grades, pid, arms["v2"][pid][0]) for pid in rows]
    lo, hi = bootstrap(paired)
    v2_s = statistics.mean(arms["v2"][pid][1] for pid in rows) if rows else float("nan")
    op_s = statistics.mean(s for _, s, _ in rows.values()) if rows else float("nan")
    loss = f"{statistics.mean(paired):+.1f} ({lo:+.1f} to {hi:+.1f})" if paired else "n/a"
    return [f"## Opinions arm, {engine}", "",
            f"- Second opinions recorded: {len(pairs)} of {len(critical)} critical positions.",
            f"- Disagreement: {len(split)} of {len(pairs)} ({len(split) / len(pairs):.0%}).",
            f"- Escalations recorded: {len(escalated)} of {len(split)} splits; the pick landed on {len(picked)}.",
            f"- Final move differs from v2 on {len(changed)} of {len(rows)} positions.",
            f"- Loss against v2, paired, opinions minus v2: {loss} over {len(paired)} positions (negative is better).",
            f"- Mean seconds: v2 {v2_s:.1f}, opinions {op_s:.1f} (main and second in parallel, then the escalation).",
            "- The second, reply and pick calls use the Phase C prompt with expected_reply; the main answer is the v2 record.",
            ""]


def build_engines(names: list[str]) -> dict:
    engines = {}
    if "codex" in names:
        engines["codex"] = CodexEngine(AppServer(), CODEX_MODEL)
    if "claude" in names:
        engines["claude"] = ClaudeEngine(Path(tempfile.mkdtemp(prefix="zeitnot-claude-")))
    if "gemini" in names:
        from google import genai

        from zeitnot.engines import GeminiEngine

        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise SystemExit("GEMINI_API_KEY is not set")
        engines["gemini"] = GeminiEngine(genai.Client(api_key=key), GEMINI_MODEL, LatencyTable(LATENCY_SEED["gemini"]))
    return engines


async def grade(positions: list[dict], records: dict, args) -> dict:
    """probe.grade, pointed at this experiment's out/ so P0's files stay untouched. P0's grades seed the cache."""
    path = OUT / "grades.json"
    if not path.exists():
        p0 = json.loads((P0 / "grades.json").read_text())
        path.write_text(json.dumps({p["id"]: p0[p["id"]] for p in positions if p["id"] in p0}, indent=1))
    probe.OUT = OUT
    graded = [{"pid": pid, "move": r["move"]} for (pid, _, _), r in records.items()]
    return await probe.grade(positions, graded, args)


async def main() -> None:
    load_dotenv(ROOT / ".env")
    # Claude Opus when it is logged in, else Codex. Codex and Gemini run only when --engines names them.
    default_engines = "claude" if claude_available(False) else "codex"
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default=default_engines, help="comma-separated: codex, claude, gemini")
    ap.add_argument("--max-gemini-usd", type=float, default=1.00, help="hard cap on recorded Gemini spend across all runs")
    ap.add_argument("--codex-workers", type=int, default=2)
    ap.add_argument("--gemini-workers", type=int, default=4)
    ap.add_argument("--claude-workers", type=int, default=2)
    ap.add_argument("--opinions", action="store_true", help="add the second-opinion arm on the critical positions")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--limit", type=int, default=0, help="only the first N positions, for a trial run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--engines-sf", dest="sf_engines", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    names = [n for n in args.engines.split(",") if n]

    positions = select_positions()
    if args.limit:
        positions = positions[: args.limit]
    print(f"{len(positions)} positions: {sum(p['group'] == 'critical' for p in positions)} critical, "
          f"{sum(p['group'] == 'quiet' for p in positions)} quiet")
    if args.dry_run:
        for p in positions:
            print(f"  {p['id']} {p['group']:<8} {p['source']:<28} {p['fen']}")
        return

    path = OUT / "calls.jsonl"
    if not args.report_only:
        engines = build_engines(names)
        try:
            await run_calls(positions, engines, args, path)
        finally:
            for engine in engines.values():
                if isinstance(getattr(engine, "server", None), AppServer):
                    await engine.server.close()
                if isinstance(engine, ClaudeEngine):
                    shutil.rmtree(engine.workdir, ignore_errors=True)
    records = {(r["pid"], r["engine"], r["kind"]): r for r in probe.load_jsonl(path) if "error" not in r}
    ids = {p["id"] for p in positions}
    records = {k: v for k, v in records.items() if k[0] in ids}

    class GradeArgs:
        seed, depth, engines = args.seed, args.depth, args.sf_engines

    grades = await grade(positions, records, GradeArgs)
    text = report(positions, records, grades, [n for n in names if any(k[1] == n for k in records)])
    (OUT / "report.md").write_text(text)
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
