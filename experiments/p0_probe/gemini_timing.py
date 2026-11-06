"""P0 follow-up. Throwaway. Seconds per move and visible thinking for Gemini Flash models at medium thinking."""

import asyncio
import json
import os
import statistics
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

import probe

PATH = probe.OUT / "gemini_timing.jsonl"
MODELS = sys.argv[1].split(",") if len(sys.argv) > 1 else ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]
LEVEL = sys.argv[2] if len(sys.argv) > 2 else "medium"
N_POSITIONS = 10
SPACING_S = 2.0
USD_IN, USD_OUT = 0.75e-6, 3.75e-6


async def one(client: genai.Client, model: str, pos: dict) -> dict:
    legal = probe.legal_sans(pos)
    config = types.GenerateContentConfig(
        system_instruction=probe.INSTRUCTIONS,
        thinking_config=types.ThinkingConfig(thinking_level=LEVEL, include_thoughts=True),
        response_mime_type="text/x.enum",
        response_schema={"type": "STRING", "enum": legal},
    )
    prompt = probe.render(pos, "rich") + "\n\nLegal moves: " + ", ".join(legal)
    t0 = time.perf_counter()
    first_chunk = first_thought = first_answer = None
    thoughts, answer, usage = [], [], None
    async for chunk in await client.aio.models.generate_content_stream(model=model, contents=prompt, config=config):
        now = time.perf_counter() - t0
        first_chunk = first_chunk or now
        usage = chunk.usage_metadata or usage
        for cand in chunk.candidates or []:
            for part in (cand.content.parts if cand.content else None) or []:
                if not part.text:
                    continue
                if part.thought:
                    first_thought = first_thought or now
                    thoughts.append(part.text)
                else:
                    first_answer = first_answer or now
                    answer.append(part.text)
    total = time.perf_counter() - t0
    move = "".join(answer).strip()
    thought_tok = (usage.thoughts_token_count or 0) if usage else 0
    out_tok = (usage.candidates_token_count or 0) if usage else 0
    in_tok = (usage.prompt_token_count or 0) if usage else 0
    rec = {
        "pid": pos["id"], "model": model, "level": LEVEL, "move": move,
        "total_s": total, "first_chunk_s": first_chunk, "first_thought_s": first_thought, "first_answer_s": first_answer,
        "thought_tokens": thought_tok, "answer_tokens": out_tok, "input_tokens": in_tok,
        "usd_if_paid": in_tok * USD_IN + (thought_tok + out_tok) * USD_OUT,
        "thought_summary": "".join(thoughts),
    }
    if move not in legal:
        rec["error"] = f"illegal or empty move {move!r}"
    return rec


async def run_model(client: genai.Client, model: str, positions: list[dict], done: set) -> None:
    for pos in positions:
        if (pos["id"], model) in done:
            continue
        started = time.perf_counter()
        try:
            rec = await one(client, model, pos)
        except Exception as e:
            rec = {"pid": pos["id"], "model": model, "level": LEVEL, "error": f"{type(e).__name__}: {e}"[:300]}
        probe.append_jsonl(PATH, rec)
        print(f"{model:<18} {pos['id']} {rec.get('total_s', 0):5.1f}s thought_tok={rec.get('thought_tokens', '-')} "
              f"move={rec.get('move')} {rec.get('error', '')}", flush=True)
        if "RESOURCE_EXHAUSTED" in rec.get("error", ""):
            print(f"{model}: quota exhausted, stopping this model")
            return
        await asyncio.sleep(max(0.0, SPACING_S - (time.perf_counter() - started)))


async def main() -> None:
    load_dotenv(probe.ROOT / ".env")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    positions = probe.load_jsonl(probe.OUT / "positions.jsonl")[:N_POSITIONS]
    done = {(r["pid"], r["model"]) for r in probe.load_jsonl(PATH) if "error" not in r and r.get("level") == LEVEL}
    await asyncio.gather(*(run_model(client, m, positions, done) for m in MODELS))


asyncio.run(main())
