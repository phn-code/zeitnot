"""P0 follow-up. Throwaway. Does service_tier=fast raise Luna's reasoning speed enough for blitz?"""

import asyncio
import json
import random
import statistics
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

import probe

PATH = probe.OUT / "fast_tier.jsonl"


async def call(client, pos, effort, tier):
    legal = probe.legal_sans(pos)
    t0 = time.perf_counter()
    r = await client.responses.create(
        model=probe.MODEL, instructions=probe.INSTRUCTIONS, reasoning={"effort": effort},
        max_output_tokens=probe.MAX_OUT_TOKENS, service_tier=tier,
        input=probe.render(pos, "rich") + "\n\nLegal moves: " + ", ".join(legal),
        text={"format": {"type": "json_schema", "name": "move", "strict": True, "schema": {
            "type": "object", "properties": {"move": {"type": "string", "enum": legal}},
            "required": ["move"], "additionalProperties": False}}},
    )
    latency = time.perf_counter() - t0
    u = r.usage
    mult = 2.0 if r.service_tier in ("fast", "priority") else 1.0
    rec = {"pid": pos["id"], "effort": effort, "asked": tier, "served": r.service_tier, "latency_s": latency,
           "reasoning_tokens": u.output_tokens_details.reasoning_tokens, "output_tokens": u.output_tokens,
           "usd": mult * (probe.input_cost(u) + u.output_tokens * probe.USD_OUT), "status": r.status,
           "move": json.loads(r.output_text)["move"] if r.status == "completed" else None}
    probe.append_jsonl(PATH, rec)
    print(f"{pos['id']} {effort:<6} asked={tier:<7} served={r.service_tier:<8} {latency:6.1f}s {rec['reasoning_tokens']:5d} tok")
    return rec


async def main():
    load_dotenv(probe.ROOT / ".env")
    client = AsyncOpenAI(max_retries=0, timeout=600)
    positions = probe.load_jsonl(probe.OUT / "positions.jsonl")[30:40]
    rng = random.Random(3)
    sem = asyncio.Semaphore(5)

    async def pair(pos):
        async with sem:
            order = ["default", "fast"]
            rng.shuffle(order)
            for tier in order:
                await call(client, pos, "low", tier)
            await call(client, pos, "medium", "fast")

    await call(client, positions[0], "none", "fast")
    await asyncio.gather(*(pair(p) for p in positions))

    recs = [json.loads(line) for line in PATH.read_text().splitlines()]
    for effort, tier in [("low", "default"), ("low", "fast"), ("medium", "fast")]:
        rs = [r for r in recs if r["effort"] == effort and r["asked"] == tier]
        tps = [r["reasoning_tokens"] / r["latency_s"] for r in rs if r["reasoning_tokens"]]
        lat = sorted(r["latency_s"] for r in rs)
        print(f"{effort}/{tier}: n={len(rs)} served={sorted({r['served'] for r in rs})} "
              f"p50={statistics.median(lat):.1f}s range={lat[0]:.1f}-{lat[-1]:.1f}s "
              f"tok/s median={statistics.median(tps):.0f} reasoning tok median={statistics.median(r['reasoning_tokens'] for r in rs):.0f}")
    print(f"spent ${sum(r['usd'] for r in recs):.4f}")


asyncio.run(main())
