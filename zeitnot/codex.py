"""The Codex engine: one `codex app-server` per server process, a fresh ephemeral thread per call.

One reader task owns the subprocess's stdout. It resolves responses by request id and routes notifications by
threadId to per-thread queues, so concurrent main, verify and ponder calls share the process. When the process
dies, every pending request and thread fails, and the next call starts a new process.
"""

import asyncio
import itertools
import json
import logging
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from .config import LATENCY_SEED, Level
from .engines import Answer, Call, LatencyTable, ModelEvent, Pulse, Rationale, RationaleParser, Thought, answer_schema, parse_answer

DISABLE = ["multi_agent", "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
           "browser_annotation_api", "computer_use", "goals", "image_generation", "plugins", "remote_plugin",
           "skill_search", "skill_mcp_dependency_install", "sleep_tool", "tool_suggest", "hooks", "shell_tool",
           "view_image", "in_app_browser", "unified_exec", "code_mode_host"]
CONFIG = ["include_permissions_instructions=false", "include_environment_context=false", "include_apps_instructions=false",
          "include_collaboration_mode_instructions=false", "tools.update_plan.enabled=false",
          "tools.experimental_request_user_input.enabled=false", "web_search=disabled", "skills.bundled.enabled=false",
          "skills.include_instructions=false", "memories.use_memories=false", "project_doc_max_bytes=0"]
MCP_LIST = ["codex", "mcp", "list", "--json", "--disable", "plugins"]
log = logging.getLogger("zeitnot.codex")
APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "applyPatchApproval",
             "execCommandApproval", "item/permissions/requestApproval"}


def mcp_server_names(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[str]:
    """Names of the user's configured MCP servers. Only the names are read: the output carries their secrets."""
    try:
        r = run(MCP_LIST, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"exit code {r.returncode}")
        return sorted({str(entry["name"]) for entry in json.loads(r.stdout)})
    except Exception as e:
        log.warning("could not list Codex MCP servers (%s); starting with plugins disabled only", type(e).__name__)
        return []


def app_server_argv(mcp_names: Sequence[str]) -> list[str]:
    """`-c mcp_servers={}` does not work: Codex merges it and every server stays on, so each one is disabled by name."""
    config = CONFIG + [f"mcp_servers.{name}.enabled=false" for name in mcp_names]
    return ["codex", "app-server"] + [x for f in DISABLE for x in ("--disable", f)] + [x for c in config for x in ("-c", c)]


def codex_logged_in() -> bool:
    """`codex login status` prints "Logged in using ChatGPT" and exits 0, or "Not logged in" and exits 1."""
    if shutil.which("codex") is None:
        return False
    try:
        r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (r.stdout + r.stderr).lower()
    return r.returncode == 0 and "logged in" in text and "not logged in" not in text


class AppServerError(RuntimeError):
    pass


class _Died:
    """Put on every thread queue when the process exits."""


class AppServer:
    def __init__(self, argv: Sequence[str] | None = None):
        self.argv = list(argv) if argv is not None else None
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.threads: dict[str, asyncio.Queue] = {}
        self.ids = itertools.count(1)
        self.lock = asyncio.Lock()
        self.starts = 0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.reader is not None and not self.reader.done()

    async def ready(self) -> "AppServer":
        async with self.lock:
            if not self.alive:
                await self._spawn()
        return self

    async def _spawn(self) -> None:
        if self.argv is None:
            self.argv = app_server_argv(await asyncio.to_thread(mcp_server_names))
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024,
        )
        self.starts += 1
        self.reader = asyncio.create_task(self._read(self.proc))
        await self.request("initialize", {"clientInfo": {"name": "zeitnot", "version": "0.2"}})
        self._write({"jsonrpc": "2.0", "method": "initialized"})

    def _write(self, msg: dict) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
            raise AppServerError("codex app-server is not running")
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())

    def send(self, method: str, params: dict) -> asyncio.Future:
        """Writes one request now and returns the future of its result."""
        rid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except AppServerError as e:
            self.pending.pop(rid, None)
            fut.set_exception(e)
        return fut

    async def request(self, method: str, params: dict) -> dict:
        return await self.send(method, params)

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.threads[thread_id] = q
        return q

    def unsubscribe(self, thread_id: str) -> None:
        self.threads.pop(thread_id, None)

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while line := await proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._route(msg)
        finally:
            error = AppServerError("codex app-server exited")
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(error)
            self.pending.clear()
            for q in self.threads.values():
                q.put_nowait(_Died())
            self.threads.clear()

    def _route(self, msg: dict) -> None:
        method, rid = msg.get("method"), msg.get("id")
        if method is None:
            fut = self.pending.pop(rid, None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                fut.set_exception(AppServerError(f"{msg['error'].get('message', msg['error'])}"))
            else:
                fut.set_result(msg.get("result", {}))
        elif rid is not None:
            if method in APPROVALS:
                self._write({"jsonrpc": "2.0", "id": rid, "result": {"decision": "decline"}})
            else:
                self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"{method} is not supported"}})
        else:
            q = self.threads.get((msg.get("params") or {}).get("threadId"))
            if q is not None:
                q.put_nowait(msg)

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if self.reader is not None:
            await asyncio.gather(self.reader, return_exceptions=True)


class CodexEngine:
    name = "codex"
    paid = False
    main_levels: tuple[Level, ...] = ("low", "medium")
    backup_level: Level = "low"

    def __init__(self, server: AppServer, model: str, service_tier: str | None = None, latency: LatencyTable | None = None):
        self.server = server
        self.model = model
        self.service_tier = service_tier
        self.label = "Codex " + model.rsplit("-", 1)[-1].title()
        self.latency = latency or LatencyTable(LATENCY_SEED["codex"])

    async def stream(self, call: Call) -> AsyncIterator[ModelEvent]:
        server = await self.server.ready()
        started = await server.request("thread/start", {
            "ephemeral": True, "model": self.model, "baseInstructions": call.system,
            "sandbox": "read-only", "approvalPolicy": "never",
        })
        thread = started["thread"]["id"]
        queue = server.subscribe(thread)
        params: dict[str, Any] = {
            "threadId": thread, "effort": call.level, "summary": "auto",
            "outputSchema": answer_schema(call.choices, call.with_note),
            "input": [{"type": "text", "text": call.prompt}],
        }
        if self.service_tier:
            params["serviceTierForTurn"] = self.service_tier
        turn_fut = server.send("turn/start", params)
        turn_id: str | None = None
        finished = False
        try:
            turn_id = (await asyncio.shield(turn_fut))["turn"]["id"]
            text, usage = None, {}
            parts: dict[tuple[str, int], str] = {}
            rationale = RationaleParser()
            while True:
                msg = await queue.get()
                if isinstance(msg, _Died):
                    raise AppServerError("codex app-server exited during the call")
                method, p = msg["method"], msg.get("params") or {}
                if method == "item/reasoning/summaryTextDelta":
                    key = (p["itemId"], p["summaryIndex"])
                    for done in [k for k in parts if k != key]:
                        yield Thought(parts.pop(done))
                    parts[key] = parts.get(key, "") + p["delta"]
                    yield Pulse()
                elif method in ("item/reasoning/textDelta", "item/reasoning/summaryPartAdded"):
                    yield Pulse()
                elif method == "item/agentMessage/delta":
                    if call.with_note and (new := rationale.feed(p.get("delta", ""))):
                        yield Rationale(new)
                elif method == "item/completed":
                    item = p.get("item") or {}
                    if item.get("type") == "reasoning":
                        for done in [k for k in parts if k[0] == item.get("id")]:
                            yield Thought(parts.pop(done))
                    elif item.get("type") == "agentMessage":
                        text = item.get("text")
                elif method == "thread/tokenUsage/updated":
                    usage = (p.get("tokenUsage") or {}).get("total") or usage
                    yield Pulse()
                elif method == "turn/completed":
                    finished = True
                    turn = p.get("turn") or {}
                    for leftover in parts.values():
                        yield Thought(leftover)
                    parts.clear()
                    if turn.get("status") != "completed":
                        err = (turn.get("error") or {}).get("message") or turn.get("status")
                        raise AppServerError(f"codex turn {turn.get('status')}: {err}")
                    break
            if text is None:
                raise AppServerError("codex finished the turn without an answer")
            san, note, expected = parse_answer(text, call.with_note)
            reasoning = usage.get("reasoningOutputTokens", 0)
            yield Answer(san, note, usage.get("inputTokens", 0), reasoning, max(0, usage.get("outputTokens", 0) - reasoning),
                         expected_reply=expected)
        finally:
            if not finished:
                self._interrupt(thread, turn_id, turn_fut)
            server.unsubscribe(thread)
            self._forget(thread)

    def _interrupt(self, thread: str, turn_id: str | None, turn_fut: asyncio.Future) -> None:
        """Sends turn/interrupt without awaiting, so it still goes out when this task is being cancelled."""
        def send(turn: str) -> None:
            try:
                self.server.send("turn/interrupt", {"threadId": thread, "turnId": turn}).add_done_callback(_ignore)
            except AppServerError:
                pass

        if turn_id is not None:
            send(turn_id)
        else:
            turn_fut.add_done_callback(lambda f: None if f.cancelled() or f.exception() else send(f.result()["turn"]["id"]))

    def _forget(self, thread: str) -> None:
        try:
            self.server.send("thread/unsubscribe", {"threadId": thread}).add_done_callback(_ignore)
        except AppServerError:
            pass


def _ignore(fut: asyncio.Future) -> None:
    if not fut.cancelled():
        fut.exception()
