import json
import time

import chess
import pytest
from fastapi.testclient import TestClient

from conftest import ScriptedRater
from zeitnot.config import Settings
from zeitnot.ledger import SpendLedger
from zeitnot.engines import EngineSet, FakeEngine
from zeitnot.server import WEB_DIR, build_engines, create_app, default_detector

CLOCK = {"ai_ms": 600_000, "human_ms": 598_000, "increment_ms": 5_000}
PLAN_KEYS = {"type", "budget_ms", "deadline_ms", "level", "reason", "engine", "difficulty", "difficulty_text", "blunder_chance",
             "opinions", "second_level"}
STATUS_KEYS = {"id", "name", "model", "label", "default", "free", "suits", "hint", "time_control", "latency", "regimes"}
MOVE_KEYS = {"type", "uci", "san", "source", "elapsed_ms", "usd", "tokens", "note", "ponder_hit", "expected_reply",
             "ponder_replies"}


@pytest.fixture
def fake_settings(tmp_path):
    return Settings(fake=True, fake_delay_s={"minimal": 0.01, "low": 0.04, "medium": 0.15, "high": 0.15},
                    ledger_path=tmp_path / "spend.json")


@pytest.fixture
def client(fake_settings):
    with TestClient(create_app(fake_settings, rater=ScriptedRater())) as c:
        yield c


def stream_events(client, body, path="/api/think"):
    with client.stream("POST", path, json=body) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in r.iter_lines() if line]


def test_think_streams_ndjson_in_fake_mode(client):
    events = stream_events(client, {"moves": ["e2e4", "e7e5"], "clock": CLOCK, "time_control": "10+5"})
    types = [e["type"] for e in events]
    assert types[0] == "plan" and types[-1] == "move" and types.count("move") == 1 and "thought" in types
    plan, move = events[0], events[-1]
    assert set(plan) == PLAN_KEYS and plan["engine"] == "fake" and plan["difficulty"] == "easy"
    assert plan["level"] == "medium", "10+5 affords the fake engine's deepest level even on an easy move"
    assert set(move) == MOVE_KEYS and move["source"] == "model" and move["ponder_hit"] is False
    assert move["note"] and move["expected_reply"] and set(move["tokens"]) == {"input", "thinking", "output"}


def test_think_move_is_legal_and_san_matches(client):
    move = stream_events(client, {"moves": ["e2e4", "e7e5"], "clock": CLOCK})[-1]
    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    assert board.san(chess.Move.from_uci(move["uci"])) == move["san"]


def test_hard_position_streams_a_backup_and_a_hard_plan(client):
    events = stream_events(client, {"moves": ["e2e4", "d7d5"], "clock": CLOCK})
    plan = events[0]
    assert (plan["difficulty"], plan["difficulty_text"], plan["level"], plan["opinions"]) == ("hard", "scripted hard", "medium", True)
    assert any(e["type"] == "note" and e["text"].startswith("fallback ready: ") for e in events)
    assert events[-1]["type"] == "move"


def test_think_forced_move_streams_a_single_move_line(client):
    history = ["a2a4", "g8h6", "b1a3", "h6f5", "c2c3", "c7c5", "a3c4", "f5h6", "c4d6"]
    events = stream_events(client, {"moves": history, "clock": CLOCK})
    assert [e["type"] for e in events] == ["move"] and events[0]["source"] == "forced"


def test_ponder_then_think_on_the_expected_reply_is_a_ponder_hit(client):
    first = stream_events(client, {"moves": [], "clock": CLOCK})[-1]
    board = chess.Board()
    board.push_uci(first["uci"])
    reply = sorted(board.legal_moves, key=lambda m: m.uci())[0].uci()
    r = client.post("/api/ponder", json={"moves": [first["uci"]], "clock": CLOCK, "expected_reply": reply})
    assert r.status_code == 202 and r.json()["status"] == "started"
    time.sleep(0.3)
    assert client.post("/api/ponder", json={"moves": [first["uci"]], "clock": CLOCK, "expected_reply": reply}).json()["status"] == "ready"
    events = stream_events(client, {"moves": [first["uci"], reply], "clock": CLOCK})
    move = events[-1]
    assert move["ponder_hit"] is True and move["elapsed_ms"] < 200
    assert "thought" in [e["type"] for e in events]


def test_ponder_streams_through_think_while_it_is_still_running(client):
    r = client.post("/api/ponder", json={"moves": ["e2e4"], "clock": CLOCK, "expected_reply": "e7e5"})
    assert r.json()["status"] == "started"
    events = stream_events(client, {"moves": ["e2e4", "e7e5"], "clock": CLOCK})
    assert events[-1]["ponder_hit"] is True and events[0]["type"] == "plan"


def test_think_on_an_unexpected_reply_is_not_a_ponder_hit(client):
    client.post("/api/ponder", json={"moves": ["e2e4"], "clock": CLOCK, "expected_reply": "e7e5"})
    events = stream_events(client, {"moves": ["e2e4", "c7c5"], "clock": CLOCK})
    assert events[-1]["ponder_hit"] is False


@pytest.mark.parametrize(("reply", "code"), [("e2e4", 400), ("zz", 400)], ids=["illegal reply", "not uci"])
def test_ponder_rejects_bad_expected_replies(client, reply, code):
    assert client.post("/api/ponder", json={"moves": ["e2e4"], "clock": CLOCK, "expected_reply": reply}).status_code == code


def test_ponder_skips_a_forced_position(client):
    history = ["a2a4", "g8h6", "b1a3", "h6f5", "c2c3", "c7c5", "a3c4", "f5h6"]
    r = client.post("/api/ponder", json={"moves": history, "clock": CLOCK, "expected_reply": "c4d6"})
    assert r.json()["status"] == "skipped"


def test_think_charges_the_ledger_and_status_reports_it(client, fake_settings):
    stream_events(client, {"moves": [], "clock": CLOCK})
    status = client.get("/api/status").json()
    assert status["spent_usd"] > 0 and status["fake"] is True
    assert SpendLedger(fake_settings.ledger_path).spent_usd == status["spent_usd"]


def test_status_shape(client):
    status = client.get("/api/status").json()
    assert set(status) == {"spent_usd", "cap_usd", "models", "default_model", "fake", "engines", "pondering", "opinions",
                           "difficulty", "setup_help"}
    assert status["difficulty"] == "scripted"
    assert [e["name"] for e in status["engines"]] == ["fake"] and status["engines"][0]["default"] is True
    assert set(status["engines"][0]) == STATUS_KEYS
    assert status["pondering"] == {"fake": True} and status["setup_help"] == []


def test_think_over_cap_streams_an_error_line(tmp_path):
    settings = Settings(fake=True, cap_usd=0.0, ledger_path=tmp_path / "spend.json")
    with TestClient(create_app(settings)) as c:
        events = stream_events(c, {"moves": [], "clock": CLOCK})
    assert [e["type"] for e in events] == ["error"] and "cap" in events[0]["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"moves": ["e2e5"], "clock": CLOCK},
        {"moves": ["f2f3", "e7e5", "g2g4", "d8h4"], "clock": CLOCK},
        {"moves": [], "clock": CLOCK, "model": "gpt-9"},
        {"moves": [], "clock": CLOCK, "engine": "gemini"},
    ],
    ids=["illegal history", "game already over", "unknown model", "engine not available"],
)
def test_think_rejects_bad_requests_with_400(client, body):
    r = client.post("/api/think", json=body)
    assert r.status_code == 400 and r.json()["detail"]


def test_think_rejects_malformed_body_with_422(client):
    assert client.post("/api/think", json={"moves": [], "clock": {"ai_ms": -1, "human_ms": 0, "increment_ms": 0}}).status_code == 422


def test_real_mode_holds_claude_opus_codex_sol_and_the_free_demo(tmp_path):
    engines = build_engines(Settings(gemini=False, api_key="key"), claude_dir=tmp_path)
    assert [(k, e.label, e.model) for k, e in engines.engines.items()] == [
        ("claude", "Claude Opus", "opus"), ("codex", "Codex Sol", "gpt-6.1-sol"), ("fake", "Fake Demo", "fake")]
    assert engines["claude"].main_levels == ("low", "medium", "high", "xhigh") and engines["fake"].paid is False
    with_flag = build_engines(Settings(gemini=True, api_key="key"), claude_dir=tmp_path)
    assert list(with_flag.engines) == ["claude", "codex", "gemini", "fake"]


def test_index_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>" in r.text


def test_web_files_are_revalidated_on_every_load(client):
    assert client.get("/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_the_start_screen_has_no_model_choice():
    page = (WEB_DIR / "index.html").read_text()
    assert "<select" not in page and 'id="opponents"' in page


def posing_as(name, model, label):
    engine = FakeEngine({"minimal": 0.01, "low": 0.03, "medium": 0.05, "high": 0.05}, priced=False)
    engine.name, engine.model, engine.label = name, model, label
    return engine


def picker_engines():
    return EngineSet({"claude": posing_as("claude", "opus", "Claude Opus"),
                      "codex": posing_as("codex", "gpt-6.1-sol", "Codex Sol"),
                      "fake": FakeEngine({"minimal": 0.01, "low": 0.03, "medium": 0.05, "high": 0.05}, priced=False)})


class Clockwork:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Fake `claude` and `codex` executables on an otherwise empty PATH. cli("claude", "in" | "out" | "missing")."""
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    scripts = {
        "claude": f"""#!/bin/sh
if [ "$(cat {state}/claude)" = in ]; then echo '{{"loggedIn": true, "authMethod": "claude.ai"}}'; exit 0; fi
echo '{{"loggedIn": false, "authMethod": "none"}}'; exit 1
""",
        "codex": f"""#!/bin/sh
if [ "$(cat {state}/codex)" = in ]; then echo "Logged in using ChatGPT"; exit 0; fi
echo "Not logged in"; exit 1
""",
    }
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    def set_state(name, value):
        path = bin_dir / name
        if value == "missing":
            path.unlink(missing_ok=True)
            return
        (state / name).write_text(value)
        path.write_text(scripts[name])
        path.chmod(0o755)

    return set_state


def detected_client(tmp_path, clock=None):
    detector = default_detector(Settings())
    if clock is not None:
        detector.clock = clock
    return TestClient(create_app(Settings(ledger_path=tmp_path / "spend.json"), engines=picker_engines(), detector=detector))


@pytest.mark.parametrize(("claude", "codex", "ids", "default"), [
    ("in", "in", ["claude", "codex"], "claude"),
    ("out", "in", ["codex"], "codex"),
    ("in", "missing", ["claude"], "claude"),
    ("missing", "out", ["fake"], "fake"),
    ("out", "missing", ["fake"], "fake"),
], ids=["both logged in", "claude logged out", "codex missing", "claude missing, codex logged out", "neither"])
def test_opponents_are_the_cli_tools_installed_and_logged_in(cli, tmp_path, claude, codex, ids, default):
    cli("claude", claude)
    cli("codex", codex)
    with detected_client(tmp_path) as c:
        status = c.get("/api/status").json()
    assert [e["id"] for e in status["engines"]] == ids
    assert [e["id"] for e in status["engines"] if e["default"]] == [default]
    help_lines = status["setup_help"]
    if default == "fake":
        assert len(help_lines) == 2 and "claude auth login" in help_lines[0] and "codex login" in help_lines[1]
    else:
        assert help_lines == []


def test_logging_in_while_the_app_runs_shows_the_opponent_within_the_cache_time(cli, tmp_path):
    cli("claude", "out")
    cli("codex", "missing")
    clock = Clockwork()
    with detected_client(tmp_path, clock) as c:
        assert [e["id"] for e in c.get("/api/status").json()["engines"]] == ["fake"]
        cli("claude", "in")
        clock.now = 29.0
        assert [e["id"] for e in c.get("/api/status").json()["engines"]] == ["fake"], "cached for 30 s"
        clock.now = 31.0
        assert [e["id"] for e in c.get("/api/status").json()["engines"]] == ["claude"]


def test_picker_data_names_each_opponent_once_with_its_speed_and_time_control(cli, tmp_path):
    cli("claude", "in")
    cli("codex", "in")
    with detected_client(tmp_path) as c:
        status = c.get("/api/status").json()
    assert all(set(e) == STATUS_KEYS for e in status["engines"])
    claude, codex = status["engines"]
    assert (claude["label"], claude["model"], codex["label"], codex["model"]) == ("Claude Opus", "opus", "Codex Sol", "gpt-6.1-sol")
    assert claude["hint"].startswith("Claude Opus takes about 8 s on an easy move")
    assert set(claude["regimes"]["3+2"]) == {"easy", "normal", "hard", "second"} and "10+5" in claude["regimes"]
    assert codex["hint"].startswith("Codex models take about 5 to 60 s")
    assert claude["time_control"] == {"minutes": 5, "increment": 3} and codex["time_control"] == {"minutes": 15, "increment": 10}
    assert status["pondering"] == {"claude": True, "codex": True}


@pytest.mark.parametrize(("body", "used"), [
    ({"engine": "codex"}, "codex"),
    ({"model": "opus"}, "claude"),
    ({}, "claude"),
], ids=["by name", "by model", "default"])
def test_think_routes_to_the_chosen_opponent(cli, tmp_path, body, used):
    cli("claude", "in")
    cli("codex", "in")
    with detected_client(tmp_path) as c:
        assert stream_events(c, {"moves": [], "clock": CLOCK, **body})[-1]["type"] == "move"
        calls = {e["id"]: e["latency"]["low"]["n"] for e in c.get("/api/status").json()["engines"]}
    assert calls == {key: int(key == used) for key in calls}


def test_think_refuses_an_opponent_that_is_not_logged_in(cli, tmp_path):
    cli("claude", "in")
    cli("codex", "out")
    with detected_client(tmp_path) as c:
        r = c.post("/api/think", json={"moves": [], "clock": CLOCK, "engine": "codex"})
    assert r.status_code == 400 and "not available" in r.json()["detail"]


def test_think_takes_the_ponder_hits_and_your_think_time(client):
    body = {"moves": ["e2e4", "e7e5"], "clock": CLOCK, "ponder_hits": 3, "opponent_ms": 9_000}
    assert stream_events(client, body)[-1]["type"] == "move"
    for bad in ({"ponder_hits": -1}, {"opponent_ms": -5}):
        assert client.post("/api/think", json={"moves": [], "clock": CLOCK, **bad}).status_code == 422
