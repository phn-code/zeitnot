"""P0 probe. Throwaway instrument that measures gpt-6-luna rungs on real blitz positions.

Every API result is appended to out/*.jsonl and skipped on rerun, so a rerun never pays twice.
"""

import argparse
import asyncio
import json
import math
import random
import statistics
import time
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import zstandard
from dotenv import load_dotenv
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"
SF = ROOT / "tools/stockfish/stockfish/stockfish-linux-x86-64-universal"
DB_ZST = ROOT / "data/raw/lichess/lichess_db_standard_rated_2013-01.pgn.zst"
DB_PGN = DB_ZST.with_suffix("")

MODEL = "gpt-6-luna"
USD_IN, USD_CACHED, USD_OUT = 0.10e-6, 0.01e-6, 0.50e-6
MAX_OUT_TOKENS = 32000
INSTRUCTIONS = (
    "You are a strong chess player. Check every capture, check and threat for both sides, "
    "then choose the best move for the side to move."
)
SHARPNESS_LEVELS = [
    {"label": "Many moves are fine"},
    {"label": "A few moves are clearly better than the rest"},
    {"label": "Only one or two moves keep the position"},
    {"label": "One exact move is required, or a tactic decides the game now"},
]


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, cap_usd: float, already_spent: float):
        self.cap, self.spent, self.reserved = cap_usd, already_spent, 0.0

    def reserve(self, usd: float) -> None:
        if self.spent + self.reserved + usd > self.cap:
            raise BudgetExceeded(f"spent ${self.spent:.4f}, reserved ${self.reserved:.4f}, cap ${self.cap:.2f}")
        self.reserved += usd

    def settle(self, reserved: float, actual: float) -> None:
        self.reserved -= reserved
        self.spent += actual


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path: Path, rec: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def sample_positions(n: int, seed: int) -> list[dict]:
    path = OUT / "positions.jsonl"
    cached = load_jsonl(path)
    if len(cached) >= n:
        return cached
    path.unlink(missing_ok=True)
    if not DB_PGN.exists():
        with DB_ZST.open("rb") as src, DB_PGN.open("wb") as dst:
            zstandard.ZstdDecompressor().copy_stream(src, dst)

    offsets = []
    with DB_PGN.open(encoding="utf-8", errors="replace") as f:
        while True:
            offset = f.tell()
            h = chess.pgn.read_headers(f)
            if h is None:
                break
            if "Blitz" not in h.get("Event", "") or h.get("Termination") != "Normal":
                continue
            try:
                elos = int(h["WhiteElo"]), int(h["BlackElo"])
            except (KeyError, ValueError):
                continue
            if all(1200 <= e <= 2200 for e in elos):
                offsets.append(offset)

    rng = random.Random(seed)
    rng.shuffle(offsets)
    positions = []
    with DB_PGN.open(encoding="utf-8", errors="replace") as f:
        for offset in offsets:
            if len(positions) >= n:
                break
            f.seek(offset)
            game = chess.pgn.read_game(f)
            moves = list(game.mainline_moves())
            if len(moves) < 30:
                continue
            ply = rng.randint(12, min(len(moves) - 2, 100))
            board = chess.Board()
            for m in moves[:ply]:
                board.push(m)
            if board.is_game_over() or board.legal_moves.count() < 2:
                continue
            positions.append({
                "id": f"p{len(positions):03d}",
                "fen": board.fen(),
                "history": [m.uci() for m in moves[:ply]],
                "human": board.san(moves[ply]),
                "elo": [int(game.headers["WhiteElo"]), int(game.headers["BlackElo"])],
                "tc": game.headers.get("TimeControl"),
                "ply": ply,
            })
    for p in positions:
        append_jsonl(path, p)
    print(f"sampled {len(positions)} positions from {len(offsets)} qualifying games")
    return positions


def side(board: chess.Board) -> str:
    return "White" if board.turn == chess.WHITE else "Black"


def diagram(board: chess.Board) -> str:
    rows = []
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            cells.append(piece.symbol() if piece else ".")
        rows.append(f"{rank + 1} " + " ".join(cells))
    rows.append("  a b c d e f g h")
    return "\n".join(rows)


def render(pos: dict, variant: str) -> str:
    board = chess.Board(pos["fen"])
    if variant == "fen":
        return f"Chess position (FEN): {pos['fen']}\n{side(board)} to move."
    movetext = chess.Board().variation_san([chess.Move.from_uci(u) for u in pos["history"]])
    return (
        f"Chess position (FEN): {pos['fen']}\n\n"
        f"Board (uppercase is White, lowercase is Black):\n{diagram(board)}\n\n"
        f"Moves so far: {movetext}\n\n{side(board)} to move."
    )


def legal_sans(pos: dict) -> list[str]:
    board = chess.Board(pos["fen"])
    return sorted(board.san(m) for m in board.legal_moves)


def input_cost(usage) -> float:
    cached = usage.input_tokens_details.cached_tokens
    return (usage.input_tokens - cached) * USD_IN + cached * USD_CACHED


async def call_decisions(client: AsyncOpenAI, pos: dict, variant: str) -> tuple[dict, float]:
    board = chess.Board(pos["fen"])
    legal = legal_sans(pos)
    t0 = time.perf_counter()
    d = await client.decisions.create(
        model=MODEL,
        input=render(pos, variant),
        questions=[
            {"name": "move", "type": "choice", "instructions": f"Choose the best move for {side(board)}.",
             "choices": [{"value": s} for s in legal]},
            {"name": "sharpness", "type": "score",
             "instructions": f"How much does the choice of move matter for {side(board)} in this position?",
             "levels": SHARPNESS_LEVELS},
        ],
    )
    latency = time.perf_counter() - t0
    usd = input_cost(d.usage)
    rec = {"latency_s": latency, "usd": usd, "input_tokens": d.usage.input_tokens,
           "reasoning_tokens": d.usage.output_tokens_details.reasoning_tokens}
    move, sharp = d.answers
    if move.type == "choice":
        probs = {p.value: p.probability for p in move.probabilities}
        rec.update(move=move.choice, confidence=move.confidence, p_top=probs.get(move.choice), probs=probs)
        if move.choice not in legal:
            rec["error"] = f"illegal choice {move.choice}"
    else:
        rec["error"] = "refusal"
    if sharp.type == "score":
        rec.update(sharpness=sharp.score, sharpness_conf=sharp.confidence)
    return rec, usd


async def call_responses(client: AsyncOpenAI, pos: dict, effort: str) -> tuple[dict, float]:
    legal = legal_sans(pos)
    t0 = time.perf_counter()
    r = await client.responses.create(
        model=MODEL,
        instructions=INSTRUCTIONS,
        reasoning={"effort": effort},
        max_output_tokens=MAX_OUT_TOKENS,
        input=render(pos, "rich") + "\n\nLegal moves: " + ", ".join(legal),
        text={"format": {"type": "json_schema", "name": "move", "strict": True, "schema": {
            "type": "object", "properties": {"move": {"type": "string", "enum": legal}},
            "required": ["move"], "additionalProperties": False}}},
    )
    latency = time.perf_counter() - t0
    u = r.usage
    usd = input_cost(u) + u.output_tokens * USD_OUT
    rec = {"latency_s": latency, "usd": usd, "status": r.status, "input_tokens": u.input_tokens,
           "output_tokens": u.output_tokens, "reasoning_tokens": u.output_tokens_details.reasoning_tokens}
    if r.status == "completed":
        rec["move"] = json.loads(r.output_text)["move"]
        if rec["move"] not in legal:
            rec["error"] = f"illegal move {rec['move']}"
    else:
        rec["error"] = f"status {r.status}"
    return rec, usd


async def run_api(positions: list[dict], args) -> None:
    load_dotenv(ROOT / ".env")
    client = AsyncOpenAI(max_retries=0, timeout=600)
    path = OUT / "api.jsonl"
    existing = load_jsonl(path)
    done = {(r["pid"], r["rung"]) for r in existing if "error" not in r}
    budget = Budget(args.cap, sum(r.get("usd", 0.0) for r in existing))

    jobs = []
    for pos in positions[: args.n_decisions]:
        for variant in ("fen", "rich"):
            jobs.append((pos, f"decisions-{variant}"))
    rng = random.Random(args.seed)
    for i, pos in enumerate(positions[: args.n_responses]):
        efforts = list(args.efforts) + (list(args.extra_efforts) if i < args.n_extra else [])
        rng.shuffle(efforts)
        jobs.extend((pos, f"responses-{e}") for e in efforts)
    jobs = [(pos, rung) for pos, rung in jobs if (pos["id"], rung) not in done]
    print(f"{len(jobs)} API calls to run, ${budget.spent:.4f} already spent, cap ${args.cap:.2f}")

    print("warming connections (untimed, unlogged)")
    await call_decisions(client, positions[0], "fen")
    await call_responses(client, positions[0], "none")

    sems = {"decisions": asyncio.Semaphore(6), "responses": asyncio.Semaphore(6)}
    max_reserve = {"decisions": 0.001, "responses": 2000 * USD_IN + MAX_OUT_TOKENS * USD_OUT}

    async def run(pos: dict, rung: str) -> None:
        kind, arg = rung.split("-", 1)
        async with sems[kind]:
            try:
                budget.reserve(max_reserve[kind])
            except BudgetExceeded as e:
                print(f"skip {pos['id']} {rung}: {e}")
                return
            usd = 0.0
            try:
                call = call_decisions if kind == "decisions" else call_responses
                rec, usd = await call(client, pos, arg)
            except Exception as e:
                rec = {"error": f"{type(e).__name__}: {e}"[:300]}
            finally:
                budget.settle(max_reserve[kind], usd)
            rec.update(pid=pos["id"], rung=rung, at=time.time())
            append_jsonl(path, rec)
            status = rec.get("error") or rec.get("move")
            print(f"{pos['id']} {rung:<18} {rec.get('latency_s', 0):6.2f}s ${usd:.5f} {status}  total ${budget.spent:.4f}")

    await asyncio.gather(*(run(pos, rung) for pos, rung in jobs))


def win_pct(score: chess.engine.PovScore, turn: chess.Color) -> float:
    cp = max(-1000, min(1000, score.pov(turn).score(mate_score=1000)))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


async def grade(positions: list[dict], records: list[dict], args, path: Path | None = None) -> dict:
    """Stockfish win percentages for every recorded move, cached in `path` (default OUT / "grades.json")."""
    path = path or OUT / "grades.json"
    grades = json.loads(path.read_text()) if path.exists() else {}
    rng = random.Random(args.seed + 1)
    need: dict[str, set[str]] = {}
    for pos in positions:
        g = grades.setdefault(pos["id"], {"win": {}})
        if "random" not in g:
            legal = legal_sans(pos)
            g["random"] = rng.sample(legal, min(3, len(legal)))
        need[pos["id"]] = {pos["human"], *g["random"]}
    for r in records:
        if "move" in r and "error" not in r:
            need[r["pid"]].add(r["move"])

    queue: asyncio.Queue = asyncio.Queue()
    for pos in positions:
        queue.put_nowait(pos)

    async def worker() -> None:
        _, engine = await chess.engine.popen_uci(str(SF))
        await engine.configure({"Threads": 1, "Hash": 64})
        limit = chess.engine.Limit(depth=args.depth)
        while not queue.empty():
            pos = queue.get_nowait()
            board = chess.Board(pos["fen"])
            g = grades[pos["id"]]
            if "best" not in g:
                infos = await engine.analyse(board, limit, multipv=5)
                wins = [(board.san(i["pv"][0]), win_pct(i["score"], board.turn)) for i in infos]
                g["win"].update(dict(wins))
                g["best_move"], g["best"] = wins[0]
                g["gap"] = wins[0][1] - wins[1][1] if len(wins) > 1 else 100.0
            for san in need[pos["id"]] - g["win"].keys():
                info = await engine.analyse(board, limit, root_moves=[board.parse_san(san)])
                g["win"][san] = win_pct(info["score"], board.turn)
        await engine.quit()

    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(args.engines)))
    path.write_text(json.dumps(grades, indent=1))
    print(f"graded {len(positions)} positions at depth {args.depth} in {time.perf_counter() - t0:.0f}s")
    return grades


def loss(grades: dict, pid: str, san: str) -> float:
    g = grades[pid]
    return max(0.0, g["best"] - g["win"][san])


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for rank, i in enumerate(order):
            r[i] = rank
        return r
    return statistics.correlation(ranks(xs), ranks(ys))


def rung_rows(positions: list[dict], records: list[dict], grades: dict, rungs: list[str]) -> list[str]:
    pids = {p["id"] for p in positions}
    rows = ["| Rung | n | Errors | Mean loss | Median loss | Best move | Good (<5) | Blunder (>=30) | p50 s | p90 s | Reasoning tok | $/call |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for rung in rungs:
        if rung == "human":
            moves = [(p["id"], p["human"]) for p in positions]
            recs = []
        elif rung == "random":
            moves = [(p["id"], san) for p in positions for san in grades[p["id"]]["random"]]
            recs = []
        else:
            recs = [r for r in records if r["rung"] == rung and r["pid"] in pids]
            moves = [(r["pid"], r["move"]) for r in recs if "move" in r and "error" not in r]
        if not moves:
            continue
        losses = [loss(grades, pid, san) for pid, san in moves]
        best = sum(grades[pid]["best_move"] == san for pid, san in moves) / len(moves)
        ok = [r for r in recs if "error" not in r]
        lat = [r["latency_s"] for r in ok]
        rows.append(
            f"| {rung} | {len(moves)} | {len(recs) - len(ok)} | {statistics.mean(losses):.1f} | {statistics.median(losses):.1f} "
            f"| {best:.0%} | {sum(l < 5 for l in losses) / len(losses):.0%} | {sum(l >= 30 for l in losses) / len(losses):.0%} "
            + (f"| {pct(lat, 0.5):.2f} | {pct(lat, 0.9):.2f} | {statistics.mean(r['reasoning_tokens'] for r in ok):.0f} "
               f"| {statistics.mean(r['usd'] for r in ok):.5f} |" if ok else "| | | | |")
        )
    return rows


def summarize(positions: list[dict], records: list[dict], grades: dict, args) -> str:
    subset = positions[: args.n_responses]
    efforts = list(args.efforts) + list(args.extra_efforts)
    lines = [
        "# P0 probe results", "",
        f"Model `{MODEL}`. Positions are 1200 to 2200 rated Lichess blitz games from January 2013. "
        f"Loss is the drop in win probability (percentage points) against Stockfish 19's best move at depth {args.depth}. "
        "Latency is wall clock per call from this machine, after warm-up, with up to 6 Responses calls and 6 Decisions calls in flight.", "",
        f"## All {len(positions[: args.n_decisions])} positions", "",
        *rung_rows(positions[: args.n_decisions], records, grades, ["decisions-fen", "decisions-rich", "human", "random"]), "",
        f"## First {len(subset)} positions, every rung", "",
        *rung_rows(subset, records, grades, ["decisions-rich", *[f"responses-{e}" for e in efforts], "human", "random"]), "",
    ]
    for rung in ("decisions-fen", "decisions-rich"):
        recs = [r for r in records if r["rung"] == rung and "error" not in r and r.get("p_top") is not None]
        if not recs:
            continue
        lines += [f"## Calibration of `{rung}` top-move probability", "",
                  "| p_top bin | n | Mean p_top | Is best move | Good (<5) |", "|---|---|---|---|---|"]
        for lo, hi in [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]:
            b = [r for r in recs if lo <= r["p_top"] < hi]
            if b:
                is_best = sum(grades[r["pid"]]["best_move"] == r["move"] for r in b) / len(b)
                good = sum(loss(grades, r["pid"], r["move"]) < 5 for r in b) / len(b)
                lines.append(f"| {lo:.1f} to {min(hi, 1):.1f} | {len(b)} | {statistics.mean(r['p_top'] for r in b):.2f} | {is_best:.0%} | {good:.0%} |")
        losses = [loss(grades, r["pid"], r["move"]) for r in recs]
        sharp = [r for r in recs if "sharpness" in r]
        lines += ["",
                  f"Spearman correlation of p_top with loss: {spearman([r['p_top'] for r in recs], losses):+.2f} (negative means confident moves lose less).",
                  f"Spearman correlation of the sharpness score with Stockfish's best-vs-second gap: "
                  f"{spearman([r['sharpness'] for r in sharp], [grades[r['pid']]['gap'] for r in sharp]):+.2f}.", ""]
    spent = sum(r.get("usd", 0.0) for r in records)
    errors = sum("error" in r for r in records)
    lines += [f"Total API spend recorded: ${spent:.4f} over {len(records)} logged calls, {errors} with errors."]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-decisions", type=int, default=200)
    ap.add_argument("--n-responses", type=int, default=30)
    ap.add_argument("--efforts", type=lambda s: s.split(","), default=["none", "low", "medium", "high"])
    ap.add_argument("--extra-efforts", type=lambda s: s.split(",") if s else [], default=["xhigh"])
    ap.add_argument("--n-extra", type=int, default=10)
    ap.add_argument("--cap", type=float, default=1.00)
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--engines", type=int, default=6)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--skip-api", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    positions = sample_positions(max(args.n_decisions, args.n_responses), args.seed)
    if not args.skip_api:
        asyncio.run(run_api(positions, args))
    latest: dict[tuple[str, str], dict] = {}
    for r in load_jsonl(OUT / "api.jsonl"):
        key = (r["pid"], r["rung"])
        if "error" not in r or "error" in latest.get(key, {"error": True}):
            latest[key] = r
    records = list(latest.values())
    grades = asyncio.run(grade(positions, records, args))
    report = summarize(positions, records, grades, args)
    raw_spend = sum(r.get("usd", 0.0) for r in load_jsonl(OUT / "api.jsonl"))
    report += f"\nRaw logged spend, including superseded retries: ${raw_spend:.4f}.\n"
    (OUT / "summary.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
