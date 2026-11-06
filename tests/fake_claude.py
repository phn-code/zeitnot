"""A fake `claude -p --output-format stream-json` for the Claude engine tests.

The prompt on stdin steers it: `move=Nf3` picks the answer (else the first enum choice), `delay=0.4` spreads the
deltas over that many seconds, `think` sends empty thinking deltas first, `refuse` stops an answer that has a
commentary field with a refusal, `crash` exits 2 with a message on stderr. Each run appends
{argv, cwd, pid, api_key} to $FAKE_CLAUDE_LOG.
"""

import json
import os
import re
import sys
import time

argv = sys.argv[1:]
prompt = sys.stdin.read()
schema = json.loads(argv[argv.index("--json-schema") + 1])
if log := os.environ.get("FAKE_CLAUDE_LOG"):
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "pid": os.getpid(),
                            "api_key": "ANTHROPIC_API_KEY" in os.environ}) + "\n")


def emit(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def delta(d: dict) -> None:
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": d}})


if "crash" in prompt:
    sys.stderr.write("Invalid API key · Please run /login\n")
    sys.exit(2)
props = schema["properties"]
enum = props["move"].get("enum") or ["e4"]
move = m.group(1) if (m := re.search(r"move=(\S+)", prompt)) else enum[0]
delay = float(m.group(1)) if (m := re.search(r"delay=([\d.]+)", prompt)) else 0.02
emit({"type": "system", "subtype": "init", "tools": ["StructuredOutput"], "mcp_servers": []})
if "think" in prompt:
    for _ in range(3):
        delta({"type": "thinking_delta", "thinking": ""})
if "refuse" in prompt and "commentary" in props:
    emit({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "refusal"}}})
    emit({"type": "result", "subtype": "success", "is_error": True, "stop_reason": "refusal",
          "result": "API Error: safeguards flagged this message [reasoning_extraction]"})
    sys.exit(0)
answer = {}
if "commentary" in props:
    answer["commentary"] = f"Comparing {move} with \"d4\": both fight for é5.\nThe knight on g5 eyes f7."
answer["move"] = move
if "expected_reply" in props:
    answer["expected_reply"] = "...e5"
if "note" in props:
    answer["note"] = "Develop and castle."
text = json.dumps(answer)
pieces = [text[i:i + 6] for i in range(0, len(text), 6)]
for piece in pieces:
    time.sleep(delay / len(pieces))
    delta({"type": "input_json_delta", "partial_json": piece})
emit({"type": "result", "subtype": "success", "is_error": False, "stop_reason": "tool_use", "structured_output": answer,
      "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1200, "cache_read_input_tokens": 1000,
                "output_tokens": 220, "output_tokens_details": {"thinking_tokens": 20}}})
