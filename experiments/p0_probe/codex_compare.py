"""P0 follow-up. Throwaway. Move quality and latency of Codex models (ChatGPT plan) on the 30-position subset."""

import json
import statistics
import subprocess
import sys
import threading
import time

import probe

PATH = probe.OUT / "codex_compare.jsonl"
DISABLE = ["multi_agent", "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
           "browser_annotation_api", "computer_use", "goals", "image_generation", "plugins", "remote_plugin",
           "skill_search", "skill_mcp_dependency_install", "sleep_tool", "tool_suggest", "hooks", "shell_tool",
           "view_image", "in_app_browser", "unified_exec", "code_mode_host"]
CONFIG = ["include_permissions_instructions=false", "include_environment_context=false", "include_apps_instructions=false",
          "include_collaboration_mode_instructions=false", "tools.update_plan.enabled=false",
          "tools.experimental_request_user_input.enabled=false", "web_search=disabled",
          "mcp_servers.langsmith.enabled=false", "mcp_servers.playwright.enabled=false", "skills.bundled.enabled=false",
          "skills.include_instructions=false", "memories.use_memories=false", "project_doc_max_bytes=0"]
LOCK = threading.Lock()


class AppServer:
    def __init__(self):
        args = ["codex", "app-server"] + [x for f in DISABLE for x in ("--disable", f)] + [x for c in CONFIG for x in ("-c", c)]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.next_id = 0
        self.call("initialize", {"clientInfo": {"name": "zeitnot-compare", "version": "0.1"}})
        self.send("initialized", None, notify=True)

    def send(self, method, params, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return msg.get("id")

    def read(self):
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("app-server closed")
        return json.loads(line)

    def call(self, method, params):
        rid = self.send(method, params)
        while True:
            msg = self.read()
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]

    def move(self, model, effort, pos):
        legal = probe.legal_sans(pos)
        t0 = time.perf_counter()
        thread = self.call("thread/start", {"ephemeral": True, "model": model, "baseInstructions": probe.INSTRUCTIONS,
                                            "sandbox": "read-only", "approvalPolicy": "never"})["thread"]["id"]
        schema = {"type": "object", "properties": {"move": {"type": "string", "enum": legal}},
                  "required": ["move"], "additionalProperties": False}
        prompt = probe.render(pos, "rich") + "\n\nLegal moves: " + ", ".join(legal)
        self.call("turn/start", {"threadId": thread, "effort": effort, "summary": "auto", "outputSchema": schema,
                                 "input": [{"type": "text", "text": prompt}]})
        answer, usage = None, {}
        while True:
            msg = self.read()
            m, p = msg.get("method", ""), msg.get("params", {})
            if m == "item/completed" and p.get("item", {}).get("type") == "agentMessage":
                answer = json.loads(p["item"]["text"])["move"]
            elif m == "thread/tokenUsage/updated":
                usage = p.get("tokenUsage", {}).get("last") or {}
            elif m == "turn/completed":
                break
        return {"pid": pos["id"], "model": model, "effort": effort, "move": answer,
                "total_s": round(time.perf_counter() - t0, 2), "reasoning_tokens": usage.get("reasoningOutputTokens"),
                "input_tokens": usage.get("inputTokens")}


def worker(model, effort, queue, done):
    server = AppServer()
    while True:
        with LOCK:
            if not queue:
                break
            pos = queue.pop(0)
        if (pos["id"], model, effort) in done:
            continue
        try:
            rec = server.move(model, effort, pos)
        except Exception as e:
            rec = {"pid": pos["id"], "model": model, "effort": effort, "error": str(e)[:200]}
        with LOCK:
            probe.append_jsonl(PATH, rec)
            print(f"{model} {effort} {pos['id']} {rec.get('total_s')}s {rec.get('move', rec.get('error'))}", flush=True)
    server.proc.terminate()


def main():
    specs = [s.split(":") for s in (sys.argv[1:] or ["gpt-6.1-sol:low", "gpt-6-astra:low"])]
    positions = probe.load_jsonl(probe.OUT / "positions.jsonl")[:30]
    done = {(r["pid"], r["model"], r["effort"]) for r in probe.load_jsonl(PATH) if "error" not in r}
    threads = []
    for model, effort in specs:
        queue = list(positions)
        for _ in range(3):
            t = threading.Thread(target=worker, args=(model, effort, queue, done))
            t.start()
            threads.append(t)
    for t in threads:
        t.join()


main()
