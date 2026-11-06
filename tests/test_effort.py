import importlib.util
import json
from pathlib import Path

import chess
import pytest

from zeitnot.engines import FakeEngine

ROOT = Path(__file__).resolve().parents[1]
EFFORT = ROOT / "experiments" / "harness_eval" / "effort.py"
SF = ROOT / "tools/stockfish/stockfish/stockfish-linux-x86-64-universal"
pytestmark = pytest.mark.skipif(not SF.exists(), reason="Stockfish grades the moves")


def position(pid, history, human, group="critical"):
    board = chess.Board()
    for uci in history:
        board.push_uci(uci)
    return {"id": pid, "fen": board.fen(), "history": history, "human": human, "group": group}


POSITIONS = [position("c1", ["e2e4", "d7d5"], "exd5"), position("c2", ["e2e4", "e7e5", "g1f3", "b8c6"], "Bb5"),
             position("q1", ["d2d4"], "d5", "quiet")]


@pytest.fixture(scope="module")
def effort():
    spec = importlib.util.spec_from_file_location("harness_eval_effort", EFFORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def out(tmp_path):
    (tmp_path / "positions.jsonl").write_text("".join(json.dumps(p) + "\n" for p in POSITIONS))
    (tmp_path / "grades.json").write_text("{}")
    baseline = [{"pid": "c1", "engine": "claude", "kind": "v2", "prompt": "commentary", "move": "exd5", "seconds": 5.0},
                {"pid": "c1", "engine": "claude", "kind": "v1", "move": "Nc3", "seconds": 5.0},
                {"pid": "c2", "engine": "codex", "kind": "v2", "move": "Bb5", "seconds": 5.0}]
    (tmp_path / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in baseline))
    return tmp_path


def snapshot(path):
    return {p.name: p.read_bytes() for p in path.iterdir()}


def records(out):
    return [json.loads(line) for line in (out / "effort.jsonl").read_text().splitlines()]


ARGV = ["--levels", "high,xhigh", "--depth", "4", "--engines-sf", "1"]


async def test_the_ladder_runs_critical_positions_per_level_reports_and_writes_only_its_own_files(effort, out):
    before = snapshot(out)
    engine = FakeEngine({"high": 0, "xhigh": 0})
    text = await effort.main(ARGV, out=out, engine=engine)

    after = snapshot(out)
    assert set(after) == set(before) | {"effort.jsonl", "effort_grades.json", "effort_report.md"}
    assert {k: after[k] for k in before} == before, "positions, grades and calls are only read"
    rows = records(out)
    assert sorted((r["pid"], r["level"]) for r in rows) == [("c1", "high"), ("c1", "xhigh"), ("c2", "high"), ("c2", "xhigh")]
    assert all("error" not in r and r["engine"] == "claude" and r["prompt"] == effort.run.PROMPT_C for r in rows)
    assert (out / "effort_report.md").read_text() == text
    lines = {line.split(" | ")[0]: line for line in text.splitlines() if line.startswith("| ")}
    assert lines["| low (calls.jsonl v2)"].split(" | ")[1] == "1", "the baseline is the claude v2 record only"
    assert lines["| high"].split(" | ")[1] == "2" and ", n=1" in lines["| high"], "paired against low on c1 only"
    grades = json.loads((out / "effort_grades.json").read_text())
    assert {r["move"] for r in rows if r["pid"] == "c1"} | {"exd5"} <= set(grades["c1"]["win"])

    await effort.main(ARGV, out=out, engine=engine)
    assert records(out) == rows, "a rerun makes no new calls"


async def test_a_rerun_retries_errored_records_and_keeps_good_ones(effort, out):
    good = {"pid": "c1", "engine": "claude", "level": "high", "move": "Nc3", "seconds": 9.0, "tokens": None}
    bad = {"pid": "c2", "engine": "claude", "level": "high", "error": "TimeoutError: ", "seconds": 300.0, "tokens": None}
    (out / "effort.jsonl").write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
    await effort.main(["--levels", "high", "--depth", "4", "--engines-sf", "1"], out=out, engine=FakeEngine({"high": 0}))
    rows = records(out)
    assert rows[:2] == [good, bad] and [(r["pid"], "error" in r) for r in rows[2:]] == [("c2", False)]


def test_unknown_levels_are_refused(effort):
    with pytest.raises(SystemExit):
        effort.parse_levels("high,ultra")
