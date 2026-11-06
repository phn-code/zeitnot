"""A fake `codex app-server` for the Codex client tests. Speaks JSON-RPC over stdio like the real one.

The turn's input text steers it: `delay=0.4` waits before answering while streaming summary deltas, `move=Nf3`
picks the answer (else the first enum choice), `fail` ends the turn as failed, `die` exits the process, and
`approve` sends a server request first. Every message received is appended to $FAKE_CODEX_LOG as one JSON line.
"""

import asyncio
import json
import os
import re
import sys

LOG = os.environ.get("FAKE_CODEX_LOG")
out_lock = asyncio.Lock()
interrupts: dict[str, asyncio.Event] = {}
waiting: dict[int, asyncio.Future] = {}


def log(msg: dict) -> None:
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(msg) + "\n")


async def send(msg: dict) -> None:
    async with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


async def notify(method: str, params: dict) -> None:
    await send({"jsonrpc": "2.0", "method": method, "params": params})


async def run_turn(thread: str, turn: str, params: dict) -> None:
    text = params["input"][0]["text"]
    choices = params["outputSchema"]["properties"]["move"]["enum"]
    delay = float(m.group(1)) if (m := re.search(r"delay=([\d.]+)", text)) else 0.05
    move = m.group(1) if (m := re.search(r"move=(\S+)", text)) else choices[0]
    if "die" in text:
        os._exit(3)
    if "approve" in text:
        fut = asyncio.get_running_loop().create_future()
        waiting[9001] = fut
        await send({"jsonrpc": "2.0", "id": 9001, "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": thread, "turnId": turn, "itemId": "x"}})
        await fut
    stop = interrupts.setdefault(turn, asyncio.Event())
    for i, piece in enumerate(["**Looking", " around**\n\n", f"Thinking about {move}."]):
        await notify("item/reasoning/summaryTextDelta",
                     {"threadId": thread, "turnId": turn, "itemId": "r1", "summaryIndex": 0 if i < 2 else 1, "delta": piece})
        try:
            await asyncio.wait_for(stop.wait(), delay / 3)
            await notify("turn/completed", {"threadId": thread, "turn": {"id": turn, "items": [], "status": "interrupted"}})
            return
        except TimeoutError:
            pass
    await notify("item/completed", {"threadId": thread, "turnId": turn, "completedAtMs": 0,
                                    "item": {"type": "reasoning", "id": "r1", "summary": [], "content": []}})
    if "fail" in text:
        await notify("turn/completed", {"threadId": thread, "turn": {"id": turn, "items": [], "status": "failed",
                                                                     "error": {"message": "usage limit"}}})
        return
    if "note" in params["outputSchema"]["properties"]:
        answer = {"commentary": f"Weighing {move} against \"d4\".\nBoth hold e5.", "move": move, "expected_reply": "e5",
                  "note": "Develop and castle."}
    else:
        answer = {"move": move}
    text = json.dumps(answer)
    for i in range(0, len(text), 7):
        await notify("item/agentMessage/delta", {"threadId": thread, "turnId": turn, "itemId": "m1", "delta": text[i:i + 7]})
    await notify("item/completed", {"threadId": thread, "turnId": turn, "completedAtMs": 0,
                                    "item": {"type": "agentMessage", "id": "m1", "text": text}})
    await notify("thread/tokenUsage/updated", {"threadId": thread, "turnId": turn, "tokenUsage": {
        "total": {"inputTokens": 900, "cachedInputTokens": 0, "outputTokens": 150, "reasoningOutputTokens": 120, "totalTokens": 1050},
        "last": {"inputTokens": 900, "cachedInputTokens": 0, "outputTokens": 150, "reasoningOutputTokens": 120, "totalTokens": 1050}}})
    await notify("turn/completed", {"threadId": thread, "turn": {"id": turn, "items": [], "status": "completed"}})


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    threads = turns = 0
    tasks = set()
    while line := await reader.readline():
        msg = json.loads(line)
        log(msg)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:
            fut = waiting.pop(rid, None)
            if fut:
                fut.set_result(msg)
            continue
        if method == "initialize":
            await send({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            threads += 1
            await send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": f"t{threads}"}}})
        elif method == "turn/start":
            turns += 1
            turn = f"u{turns}"
            await send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": turn, "items": [], "status": "inProgress"}}})
            task = asyncio.create_task(run_turn(params["threadId"], turn, params))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        elif method == "turn/interrupt":
            interrupts.setdefault(params["turnId"], asyncio.Event()).set()
            await send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            await send({"jsonrpc": "2.0", "id": rid, "result": {}})


asyncio.run(main())
