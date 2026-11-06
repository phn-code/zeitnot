"""Claude Opus at higher effort on the critical harness-eval positions: what xhigh and max buy over low.

Each call is the v2 arm's call from run.py (run.call_for), with only the level changed. Records go to
out/effort.jsonl, one per position and level; a rerun skips the good ones and retries the errored ones. Moves are
graded into out/effort_grades.json, seeded once from out/grades.json, so run.py's files are only read. The `low`
baseline is the claude v2 records in out/calls.jsonl, read-only. The report goes to out/effort_report.md.

    .venv/bin/python experiments/harness_eval/effort.py --levels xhigh --limit 1 --workers 1   # one call
    .venv/bin/python experiments/harness_eval/effort.py                                         # high, xhigh, max on 40
    .venv/bin/python experiments/harness_eval/effort.py --report-only
"""

import argparse
import asyncio
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run  # noqa: E402  (also puts the repo root and experiments/p0_probe on sys.path)
from run import probe  # noqa: E402

from zeitnot.claude import ClaudeEngine  # noqa: E402
from zeitnot.config import Level  # noqa: E402
from zeitnot.engines import Answer  # noqa: E402

ENGINE = "claude"
BASELINE: Level = "low"
LEVELS: tuple[str, ...] = get_args(Level)


def critical_positions(out: Path, limit: int) -> list[dict]:
    positions = [p for p in probe.load_jsonl(out / "positions.jsonl") if p["group"] == "critical"]
    return positions[:limit] if limit else positions


def good_records(path: Path) -> dict[tuple[str, str], dict]:
    """(pid, level) -> the latest record without an error."""
    return {(r["pid"], r["level"]): r for r in probe.load_jsonl(path) if "error" not in r}


def baseline_records(calls_path: Path, pids: set[str]) -> dict[str, dict]:
    """pid -> run.py's claude v2 record at low."""
    return {r["pid"]: r for r in probe.load_jsonl(calls_path)
            if (r["engine"], r["kind"]) == (ENGINE, "v2") and "error" not in r and r["pid"] in pids}


async def run_levels(positions: list[dict], engine, levels: list[Level], path: Path, workers: int, timeout: float) -> None:
    done = good_records(path)
    sem = asyncio.Semaphore(workers)

    async def one(pos: dict, level: Level) -> None:
        if (pos["id"], level) in done:
            return
        async with sem:
            call = replace(run.call_for(run.Job(pos, ENGINE, "v2"), {}), level=level)
            rec: dict = {"pid": pos["id"], "engine": ENGINE, "level": level, "prompt": run.PROMPT_C}
            answers: list[Answer] = []
            started = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    async with aclosing(engine.stream(call)) as events:
                        async for event in events:
                            if isinstance(event, Answer):
                                answers = [event]
                answer = answers[0] if answers else None
                if answer is None or answer.san not in call.choices:
                    raise ValueError(f"no legal answer: {answer.san if answer else None!r}")
                rec.update(move=answer.san, note=answer.note)
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            tokens = [sum(a.input_tokens for a in answers), sum(a.thinking_tokens for a in answers),
                      sum(a.output_tokens for a in answers)] if answers else None
            rec.update(seconds=round(time.monotonic() - started, 2), tokens=tokens, at=time.time())
            probe.append_jsonl(path, rec)
            print(f"{level:<6} {pos['id']} {rec['seconds']:6.1f}s {rec.get('move', rec.get('error'))}", flush=True)

    await asyncio.gather(*(one(p, level) for level in levels for p in positions))


async def grade(positions: list[dict], moves: list[dict], out: Path, args) -> dict:
    path = out / "effort_grades.json"
    if not path.exists() and (out / "grades.json").exists():
        shutil.copyfile(out / "grades.json", path)
    sf = argparse.Namespace(seed=args.seed, depth=args.depth, engines=args.sf_engines)
    return await probe.grade(positions, moves, sf, path=path)


def report(positions: list[dict], records: dict[tuple[str, str], dict], baseline: dict[str, dict], grades: dict,
           levels: list[Level], depth: int) -> str:
    rows: dict[str, dict[str, dict]] = {f"{BASELINE} (calls.jsonl v2)": baseline}
    rows.update({level: {pid: r for (pid, lv), r in records.items() if lv == level} for level in levels})
    base_loss = {pid: probe.loss(grades, pid, r["move"]) for pid, r in baseline.items()}
    lines = ["# Effort ladder, Claude Opus", "",
             f"{len(positions)} critical positions from out/positions.jsonl. Each call is run.py's v2 call with only "
             f"the level changed. Loss is the drop in win probability against Stockfish 19's best move at depth {depth}. "
             f"Blunder means a loss of {run.BLUNDER:.0f} or more. Intervals are 95% bootstrap intervals of the mean. "
             f"The paired column is level minus {BASELINE} on the positions both answered (negative is better). Seconds "
             "are wall clock per call with the configured number of calls in flight.", "",
             f"The {BASELINE} row is run.py's claude v2 arm, recorded with the `{run.PROMPT}` answer schema. The other "
             f"rows use today's `{run.PROMPT_C}` schema, so the paired difference also carries that schema change.", "",
             "| Level | n | Mean loss (95% CI) | Blunders | vs low, paired (95% CI) | Mean s | p50 s | p90 s | Mean out tokens |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, recs in rows.items():
        if not recs:
            lines.append(f"| {name} | 0 | | | | | | | |")
            continue
        loss = {pid: probe.loss(grades, pid, r["move"]) for pid, r in recs.items()}
        xs = list(loss.values())
        lo, hi = run.bootstrap(xs)
        paired = [loss[pid] - base_loss[pid] for pid in loss if pid in base_loss]
        plo, phi = run.bootstrap(paired)
        diff = "" if recs is baseline or not paired else f"{statistics.mean(paired):+.1f} ({plo:+.1f} to {phi:+.1f}), n={len(paired)}"
        secs = [r["seconds"] for r in recs.values()]
        toks = [r["tokens"][1] + r["tokens"][2] for r in recs.values() if r.get("tokens")]
        lines.append(f"| {name} | {len(xs)} | {statistics.mean(xs):.1f} ({lo:.1f} to {hi:.1f}) | {sum(x >= run.BLUNDER for x in xs)} "
                     f"| {diff} | {statistics.mean(secs):.1f} | {probe.pct(secs, 0.5):.1f} | {probe.pct(secs, 0.9):.1f} "
                     f"| {statistics.mean(toks) if toks else float('nan'):.0f} |")
    return "\n".join(lines) + "\n"


def parse_levels(text: str) -> list[Level]:
    levels = [x for x in text.split(",") if x]
    if bad := [x for x in levels if x not in LEVELS]:
        raise SystemExit(f"unknown levels {bad}, expected some of {LEVELS}")
    return levels


async def main(argv: list[str] | None = None, out: Path = run.OUT, engine=None) -> str:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="high,xhigh,max", help=f"comma-separated, from {','.join(LEVELS)}")
    ap.add_argument("--limit", type=int, default=0, help="only the first N critical positions")
    ap.add_argument("--workers", type=int, default=2, help="calls in flight; run.py's claude arms used 2")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--engines-sf", dest="sf_engines", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    levels = parse_levels(args.levels)

    positions = critical_positions(out, args.limit)
    path = out / "effort.jsonl"
    if not args.report_only:
        owned = engine is None
        engine = engine or ClaudeEngine(Path(tempfile.mkdtemp(prefix="zeitnot-claude-")))
        try:
            await run_levels(positions, engine, levels, path, args.workers, args.timeout)
        finally:
            if owned:
                shutil.rmtree(engine.workdir, ignore_errors=True)

    pids = {p["id"] for p in positions}
    records = {k: r for k, r in good_records(path).items() if k[0] in pids and k[1] in levels}
    baseline = baseline_records(out / "calls.jsonl", pids)
    moves = [{"pid": pid, "move": r["move"]} for pid, r in baseline.items()]
    moves += [{"pid": pid, "move": r["move"]} for (pid, _), r in records.items()]
    grades = await grade(positions, moves, out, args)
    text = report(positions, records, baseline, grades, levels, args.depth)
    (out / "effort_report.md").write_text(text)
    print(text)
    return text


if __name__ == "__main__":
    asyncio.run(main())
