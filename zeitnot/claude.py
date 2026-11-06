"""The Claude engine: one stripped `claude -p` process per call, run from an empty directory the app owns.

A fresh process per call keeps the legal-move enum in `--json-schema`, so an illegal answer cannot occur. A warm
spare process saved about 0.4 s a call but needs a general schema and a legality retry
(experiments/claude_startup). Closing the stream kills the process, which is how the harness interrupts a call.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from pathlib import Path

from .config import CLAUDE_MODEL, LATENCY_SEED, Level
from .engines import (Answer, Call, LatencyTable, ModelEvent, Pulse, Rationale, RationaleParser, answer_schema,
                      expected_of, truncate_note)

STRIPPED = ["--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
            "--tools", ""]
STREAM = ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
EFFORT: dict[Level, str] = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
# Without --bare the CLI would prefer a key in the environment over the subscription login, and bill it.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
NO_COMMENTARY = " Leave out the commentary."
log = logging.getLogger("zeitnot.claude")


class ClaudeError(RuntimeError):
    pass


class ClaudeRefused(ClaudeError):
    """The model's safeguards stopped the answer. Seen with the reasoning_extraction category on a commentary field."""


def claude_env(bare: bool) -> dict[str, str]:
    env = dict(os.environ)
    if not bare:
        for var in API_KEY_VARS:
            env.pop(var, None)
    return env


def claude_argv(command: Sequence[str], model: str, level: Level, system: str, schema: dict, bare: bool) -> list[str]:
    return [*command, "-p", *(["--bare"] if bare else []), "--model", model, "--effort", EFFORT[level], *STRIPPED,
            "--system-prompt", system, "--json-schema", json.dumps(schema), *STREAM]


def claude_available(bare: bool, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """Installed and logged in. `claude auth status` exits 1 with loggedIn false when signed out, and makes no model call."""
    if shutil.which("claude") is None:
        return False
    if bare:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    try:
        r = run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=15,
                env=claude_env(False), cwd=tempfile.gettempdir())
        return r.returncode == 0 and json.loads(r.stdout).get("loggedIn") is True
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


class ClaudeEngine:
    """Always Opus. Opus cannot turn thinking off, so minimal maps to --effort low and every other level to itself."""

    name = "claude"
    model = CLAUDE_MODEL
    label = "Claude Opus"
    paid = False
    main_levels: tuple[Level, ...] = ("low", "medium", "high", "xhigh")
    backup_level: Level = "low"

    def __init__(self, workdir: Path, bare: bool = False, command: Sequence[str] = ("claude",),
                 latency: LatencyTable | None = None):
        self.workdir = workdir
        self.bare = bare
        self.command = list(command)
        self.latency = latency or LatencyTable(LATENCY_SEED["claude"])

    async def stream(self, call: Call) -> AsyncIterator[ModelEvent]:
        try:
            async with aclosing(self._once(call, commentary=call.with_note)) as events:
                async for event in events:
                    yield event
        except ClaudeRefused as e:
            if not call.with_note:
                raise
            log.warning("Claude refused the answer with commentary (%s); asking once without it", e)
            async with aclosing(self._once(call, commentary=False)) as events:
                async for event in events:
                    yield event

    async def _once(self, call: Call, commentary: bool) -> AsyncIterator[ModelEvent]:
        schema = answer_schema(call.choices, call.with_note, commentary=commentary)
        system = call.system if commentary or not call.with_note else call.system + NO_COMMENTARY
        argv = claude_argv(self.command, self.model, call.level, system, schema, self.bare)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=self.workdir, env=claude_env(self.bare), limit=16 * 1024 * 1024,
        )
        stderr = asyncio.create_task(proc.stderr.read())
        try:
            proc.stdin.write(call.prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            rationale = RationaleParser()
            result = None
            while line := await proc.stdout.readline():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    result = ev
                    break
                inner = ev.get("event") or {} if ev.get("type") == "stream_event" else {}
                delta = inner.get("delta") or {}
                kind = delta.get("type")
                if kind == "thinking_delta":
                    yield Pulse()
                elif kind in ("input_json_delta", "text_delta"):
                    new = rationale.feed(delta.get("partial_json") or delta.get("text") or "") if commentary else ""
                    yield Rationale(new) if new else Pulse()
            if result is None:
                await proc.wait()
                err = (await stderr).decode(errors="replace").strip()
                raise ClaudeError(f"claude exited with code {proc.returncode} without an answer: {err[-300:]}")
            if result.get("stop_reason") == "refusal":
                raise ClaudeRefused(str(result.get("result", ""))[:200])
            data = result.get("structured_output")
            if result.get("is_error") or not isinstance(data, dict) or "move" not in data:
                raise ClaudeError(f"claude answered without a move: {str(result.get('result', result.get('subtype')))[:300]}")
            if commentary and not rationale.raw and (new := rationale.feed(json.dumps(data))):
                yield Rationale(new)
            usage = result.get("usage") or {}
            thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0)
            input_tokens = sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            note = truncate_note(data.get("note")) if call.with_note else None
            expected = expected_of(data) if call.with_note else None
            yield Answer(str(data["move"]).strip(), note, input_tokens, thinking, max(0, usage.get("output_tokens", 0) - thinking),
                         expected_reply=expected)
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            stderr.cancel()
