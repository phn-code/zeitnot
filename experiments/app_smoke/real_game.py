"""Throwaway smoke test. Plays one real game through POST /api/think with browser-style clocks.

Stockfish at a limited Elo stands in for the human so the harness runs under a real clock without a person.
"""

import json
import statistics
import sys
import time
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"
SF = ROOT / "tools/stockfish/stockfish/stockfish-linux-x86-64-universal"
URL = "http://127.0.0.1:8000/api/think"

MINUTES, INC_S = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else (10, 5)
OPPONENT_ELO, OPPONENT_MOVETIME_S, MAX_MODEL_MOVES = 1500, 1.0, 60


def main() -> None:
    OUT.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    events_path = OUT / f"game-{stamp}.jsonl"
    board = chess.Board()
    clocks = {chess.WHITE: MINUTES * 60_000, chess.BLACK: MINUTES * 60_000}
    inc_ms = INC_S * 1000
    model_color = chess.WHITE
    engine = chess.engine.SimpleEngine.popen_uci(str(SF))
    engine.configure({"UCI_LimitStrength": True, "UCI_Elo": OPPONENT_ELO, "Threads": 1})
    moves_meta, result = [], None
    try:
        while not board.is_game_over() and sum(m["side"] == "model" for m in moves_meta) < MAX_MODEL_MOVES:
            mover = board.turn
            t0 = time.monotonic()
            if mover == model_color:
                body = {"moves": [m.uci() for m in board.move_stack],
                        "clock": {"ai_ms": int(clocks[mover]), "human_ms": int(clocks[not mover]), "increment_ms": int(inc_ms)}}
                move_ev, plan, notes, thought_chars = None, None, [], 0
                with httpx.stream("POST", URL, json=body, timeout=None) as res:
                    res.raise_for_status()
                    for line in res.iter_lines():
                        if not line:
                            continue
                        ev = json.loads(line)
                        with events_path.open("a") as f:
                            f.write(json.dumps({"ply": len(board.move_stack), **ev}) + "\n")
                        if ev["type"] == "plan":
                            plan = ev
                        elif ev["type"] == "note":
                            notes.append(ev["text"])
                        elif ev["type"] == "thought":
                            thought_chars += len(ev["text"])
                        elif ev["type"] == "move":
                            move_ev = ev
                        elif ev["type"] == "error":
                            result = f"model error: {ev['message']}"
                if move_ev is None:
                    result = result or "stream ended without a move"
                    break
                move = chess.Move.from_uci(move_ev["uci"])
                meta = {"side": "model", "san": board.san(move), "level": plan["level"] if plan else "forced",
                        "source": move_ev["source"], "usd": move_ev["usd"], "thought_chars": thought_chars, "notes": notes}
            else:
                move = engine.play(board, chess.engine.Limit(time=OPPONENT_MOVETIME_S)).move
                meta = {"side": "opponent", "san": board.san(move)}
            spent = (time.monotonic() - t0) * 1000
            clocks[mover] -= spent
            meta.update(ply=len(board.move_stack), spent_ms=int(spent), clock_after_ms=int(max(0, clocks[mover])))
            if clocks[mover] <= 0:
                result = f"{'model' if mover == model_color else 'opponent'} lost on time"
                moves_meta.append(meta)
                break
            clocks[mover] += inc_ms
            board.push(move)
            moves_meta.append(meta)
            if meta["side"] == "model":
                print(f"{len(board.move_stack):3d} {meta['san']:<7} {meta['level']:<7} {meta['source']:<9} "
                      f"{spent / 1000:5.1f}s clock {clocks[mover] / 1000:6.1f}s ${meta['usd']:.4f} thoughts {thought_chars}ch", flush=True)
    finally:
        engine.quit()

    result = result or (board.result() if board.is_game_over() else f"stopped after {MAX_MODEL_MOVES} model moves")
    game = chess.pgn.Game.from_board(board)
    game.headers.update({"Event": f"zeitnot smoke {MINUTES:g}+{INC_S:g}", "White": "gemini-3.6-flash + harness",
                         "Black": f"Stockfish UCI_Elo {OPPONENT_ELO}", "Result": board.result() if board.is_game_over() else "*"})
    (OUT / f"game-{stamp}.pgn").write_text(str(game) + "\n")

    model = [m for m in moves_meta if m["side"] == "model"]
    secs = sorted(m["spent_ms"] / 1000 for m in model)
    print(f"\nresult: {result}")
    print(f"model moves {len(model)}, seconds per move median {statistics.median(secs):.1f}, p90 {secs[int(0.9 * (len(secs) - 1))]:.1f}, max {secs[-1]:.1f}")
    for key in ("level", "source"):
        counts = {}
        for m in model:
            counts[m[key]] = counts.get(m[key], 0) + 1
        print(f"{key}: {counts}")
    print(f"model clock left {clocks[model_color] / 1000:.1f}s, cost ${sum(m['usd'] for m in model):.4f}")
    print(f"pgn {OUT / f'game-{stamp}.pgn'}\nevents {events_path}")


if __name__ == "__main__":
    main()
