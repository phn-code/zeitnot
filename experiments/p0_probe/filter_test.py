"""P0 follow-up. Throwaway. Does offering Luna only one-ply-safe moves fix its fast-rung blunders?"""

import asyncio
import json
import statistics
import time

import chess
from dotenv import load_dotenv
from openai import AsyncOpenAI

import probe
from signals import VALUE

PATH = probe.OUT / "filtered.jsonl"


def safe(board: chess.Board, move: chess.Move) -> bool:
    """False when the moved piece lands en prise, unless it captured something at least as valuable."""
    me = board.turn
    moved_value = VALUE[board.piece_type_at(move.from_square)]
    captured = board.piece_type_at(move.to_square)
    if captured and VALUE[captured] >= moved_value:
        return True
    after = board.copy()
    after.push(move)
    attackers = after.attackers(not me, move.to_square)
    if not attackers:
        return True
    cheapest = min(VALUE[after.piece_type_at(s)] for s in attackers)
    return bool(after.attackers(me, move.to_square)) and cheapest >= VALUE[after.piece_type_at(move.to_square)]


def safe_sans(pos: dict) -> list[str]:
    board = chess.Board(pos["fen"])
    sans = sorted(board.san(m) for m in board.legal_moves if safe(board, m))
    return sans or probe.legal_sans(pos)


async def none_filtered(client: AsyncOpenAI, pos: dict) -> dict:
    cands = safe_sans(pos)
    t0 = time.perf_counter()
    r = await client.responses.create(
        model=probe.MODEL, instructions=probe.INSTRUCTIONS, reasoning={"effort": "none"},
        input=probe.render(pos, "rich") + "\n\nCandidate moves: " + ", ".join(cands),
        text={"format": {"type": "json_schema", "name": "move", "strict": True, "schema": {
            "type": "object", "properties": {"move": {"type": "string", "enum": cands}},
            "required": ["move"], "additionalProperties": False}}},
    )
    u = r.usage
    return {"pid": pos["id"], "rung": "none-filtered", "move": json.loads(r.output_text)["move"],
            "latency_s": time.perf_counter() - t0, "usd": probe.input_cost(u) + u.output_tokens * probe.USD_OUT}


async def main() -> None:
    load_dotenv(probe.ROOT / ".env")
    positions = probe.load_jsonl(probe.OUT / "positions.jsonl")
    recs = {(r["pid"], r["rung"]): r for r in probe.load_jsonl(probe.OUT / "api.jsonl") if "error" not in r}
    done = {(r["pid"], r["rung"]): r for r in probe.load_jsonl(PATH)}

    client = AsyncOpenAI(max_retries=1, timeout=120)
    sem = asyncio.Semaphore(8)

    async def run(pos):
        async with sem:
            rec = await none_filtered(client, pos)
            probe.append_jsonl(PATH, rec)
            done[(pos["id"], "none-filtered")] = rec

    await asyncio.gather(*(run(p) for p in positions if (p["id"], "none-filtered") not in done))

    for pos in positions:
        dec = recs[(pos["id"], "decisions-rich")]
        allowed = set(safe_sans(pos))
        best = max((m for m in dec["probs"] if m in allowed), key=lambda m: dec["probs"][m])
        done[(pos["id"], "decisions-filtered")] = {"pid": pos["id"], "rung": "decisions-filtered", "move": best}

    class Args:
        seed, depth, engines = 7, 16, 6
    extra = list(done.values()) + list(recs.values())
    grades = await probe.grade(positions, extra, Args)

    def row(name, moves):
        losses = [probe.loss(grades, pid, san) for pid, san in moves]
        print(f"  {name:<22} n={len(losses):3d} mean {statistics.mean(losses):5.1f}  median {statistics.median(losses):4.1f}  "
              f"good {sum(l < 5 for l in losses) / len(losses):4.0%}  blunder {sum(l >= 30 for l in losses) / len(losses):4.0%}")

    print("All 200 positions")
    for rung in ["responses-none", "none-filtered", "decisions-rich", "decisions-filtered"]:
        src = recs if rung.startswith(("responses", "decisions-rich")) else done
        row(rung, [(p["id"], src[(p["id"], rung)]["move"]) for p in positions])
    row("human", [(p["id"], p["human"]) for p in positions])
    print("First 30 positions")
    for rung in ["none-filtered", "decisions-filtered", "responses-low", "responses-medium", "responses-high"]:
        src = done if "filtered" in rung else recs
        row(rung, [(p["id"], src[(p["id"], rung)]["move"]) for p in positions[:30]])
    lat = [r["latency_s"] for r in done.values() if "latency_s" in r]
    print(f"none-filtered latency p50 {statistics.median(lat):.2f}s, spend ${sum(r.get('usd', 0) for r in done.values()):.4f}")
    print(f"safe moves per position, median {statistics.median(len(safe_sans(p)) for p in positions):.0f} "
          f"of {statistics.median(len(probe.legal_sans(p)) for p in positions):.0f} legal")


asyncio.run(main())
