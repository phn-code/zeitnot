import json

import chess
import pytest
from fastapi.testclient import TestClient

from zeitnot.config import Settings
from zeitnot.ledger import SpendLedger
from zeitnot.server import create_app

CLOCK = {"ai_ms": 600_000, "human_ms": 598_000, "increment_ms": 5_000}


@pytest.fixture
def fake_settings(tmp_path):
    return Settings(
        fake=True,
        fake_delay_s={"none": 0, "minimal": 0.01, "low": 0.04, "medium": 0.04, "high": 0.04},
        ledger_path=tmp_path / "spend.json",
    )


@pytest.fixture
def client(fake_settings):
    with TestClient(create_app(fake_settings)) as c:
        yield c


def stream_events(client, body):
    with client.stream("POST", "/api/think", json=body) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in r.iter_lines() if line]


def test_think_streams_ndjson_in_fake_mode(client):
    events = stream_events(client, {"moves": ["e2e4", "e7e5"], "clock": CLOCK})
    types = [e["type"] for e in events]
    assert types[0] == "plan" and types[-1] == "move" and types.count("move") == 1
    assert "thought" in types and "note" in types
    plan = events[0]
    assert set(plan) == {"type", "budget_ms", "deadline_ms", "level", "reason"} and plan["level"] == "medium"
    move = events[-1]
    assert set(move) == {"type", "uci", "san", "source", "elapsed_ms", "usd", "tokens"}
    assert move["source"] == "model" and set(move["tokens"]) == {"input", "thinking", "output"}


def test_think_move_is_legal_and_san_matches(client):
    move = stream_events(client, {"moves": ["e2e4", "e7e5"], "clock": CLOCK})[-1]
    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    assert board.san(chess.Move.from_uci(move["uci"])) == move["san"]


def test_think_forced_move_streams_a_single_move_line(client):
    history = ["a2a4", "g8h6", "b1a3", "h6f5", "c2c3", "c7c5", "a3c4", "f5h6", "c4d6"]
    events = stream_events(client, {"moves": history, "clock": CLOCK})
    assert [e["type"] for e in events] == ["move"] and events[0]["source"] == "forced"


def test_think_charges_the_ledger_and_status_reports_it(client, fake_settings):
    stream_events(client, {"moves": [], "clock": CLOCK})
    status = client.get("/api/status").json()
    assert status["spent_usd"] > 0 and status["fake"] is True
    assert SpendLedger(fake_settings.ledger_path).spent_usd == status["spent_usd"]


def test_status_shape(client):
    status = client.get("/api/status").json()
    assert set(status) == {"spent_usd", "cap_usd", "models", "default_model", "fake"}
    assert status["default_model"] in status["models"]


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
    ],
    ids=["illegal history", "game already over", "unknown model"],
)
def test_think_rejects_bad_requests_with_400(client, body):
    r = client.post("/api/think", json=body)
    assert r.status_code == 400 and r.json()["detail"]


def test_think_rejects_malformed_body_with_422(client):
    assert client.post("/api/think", json={"moves": [], "clock": {"ai_ms": -1, "human_ms": 0, "increment_ms": 0}}).status_code == 422


def test_real_mode_without_a_key_fails_at_startup():
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        create_app(Settings(fake=False, api_key=None))


def test_index_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>" in r.text
