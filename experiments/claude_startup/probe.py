"""Claude Code start-up: (a) a fresh process per call with the legal-move enum, against (b) a warm spare process
fed by stream-json with a general schema. Each run is one subscription call; results go to out/runs.jsonl.

    .venv/bin/python experiments/claude_startup/probe.py fresh --model sonnet --effort low
    .venv/bin/python experiments/claude_startup/probe.py warm --model sonnet --effort low --warm-s 6
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import chess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zeitnot.context import SYSTEM, Situation, build  # noqa: E402
from zeitnot.facts import compute  # noqa: E402
from zeitnot.timeman import Clock  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
HISTORY = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "f3g5", "d7d5", "e4d5"]
STRIPPED = ["--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
            "--tools", ""]
STREAM = ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]


def position() -> tuple[str, list[str]]:
    board = chess.Board()
    for uci in HISTORY:
        board.push_uci(uci)
    legal = list(board.legal_moves)
    prompt = build(board, Situation(Clock(600_000, 600_000, 10_000), "15+10", compute(board), None), legal)
    return prompt, [board.san(m) for m in legal]


def schema(choices: list[str] | None) -> dict:
    move = {"type": "string", "enum": choices} if choices else {"type": "string"}
    return {"type": "object", "properties": {"commentary": {"type": "string"}, "move": move, "note": {"type": "string"}},
            "required": ["commentary", "move", "note"], "additionalProperties": False}


async def read_until_result(proc, t0: float, marks: dict, events: list) -> dict:
    while line := await proc.stdout.readline():
        ev = json.loads(line)
        t = round(time.monotonic() - t0, 3)
        kind = ev.get("type")
        if kind == "stream_event":
            inner = ev["event"]
            kind = inner.get("type")
            if kind == "content_block_delta":
                kind = inner["delta"]["type"]
        events.append((t, kind, ev))
        marks.setdefault(kind, t)
        if ev.get("type") == "result":
            return ev
    raise RuntimeError("no result")


async def run(mode: str, model: str, effort: str, warm_s: float) -> dict:
    prompt, choices = position()
    cwd = tempfile.mkdtemp(prefix="zeitnot-claude-probe-")
    base = ["claude", "-p", "--model", model, "--effort", effort, *STRIPPED, "--system-prompt", SYSTEM, *STREAM]
    marks: dict = {}
    events: list = []
    if mode == "fresh":
        argv = base + ["--json-schema", json.dumps(schema(choices))]
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, cwd=cwd, limit=16 << 20)
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
    else:
        argv = base + ["--input-format", "stream-json", "--json-schema", json.dumps(schema(None))]
        spawn = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, cwd=cwd, limit=16 << 20)
        early = []
        try:
            async with asyncio.timeout(warm_s):
                while line := await proc.stdout.readline():
                    early.append((round(time.monotonic() - spawn, 3), json.loads(line).get("type")))
        except TimeoutError:
            pass
        marks["before_input"] = early
        t0 = time.monotonic()
        msg = {"type": "user", "message": {"role": "user", "content": prompt}}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()
    result = await read_until_result(proc, t0, marks, events)
    wall = round(time.monotonic() - t0, 3)
    if proc.stdin and not proc.stdin.is_closing():
        proc.stdin.close()
    await proc.wait()
    shutil.rmtree(cwd, ignore_errors=True)
    usage = result.get("usage") or {}
    rec = {"mode": mode, "model": model, "effort": effort, "wall_s": wall, "api_ms": result.get("duration_api_ms"),
           "duration_ms": result.get("duration_ms"), "is_error": result.get("is_error"),
           "structured": result.get("structured_output"), "legal": (result.get("structured_output") or {}).get("move") in choices,
           "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
           "usage": usage, "first": {k: v for k, v in marks.items()}, "at": time.time()}
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "runs.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    with (OUT / f"events-{mode}-{model}-{effort}-{int(time.time())}.jsonl").open("w") as f:
        for t, kind, ev in events:
            f.write(json.dumps({"t": t, "kind": kind, "ev": ev}) + "\n")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["fresh", "warm"])
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--warm-s", type=float, default=6.0)
    args = ap.parse_args()
    rec = asyncio.run(run(args.mode, args.model, args.effort, args.warm_s))
    print(json.dumps({k: v for k, v in rec.items() if k != "usage"}, indent=1))


if __name__ == "__main__":
    main()
