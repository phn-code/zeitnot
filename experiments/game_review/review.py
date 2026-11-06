"""Grade a game from logs/moves.jsonl with Stockfish and line each model move up with its plan.

Every ply is graded by the drop in the mover's Lichess win% against Stockfish's best move at depth 16, the yardstick
of experiments/harness_eval. For the model's moves it adds what the harness decided: Maia's difficulty, the level,
the budget, the time used, and whether a pondered answer played. No model calls; Stockfish runs locally.

usage: .venv/bin/python experiments/game_review/review.py [--game -1] [--depth 16]
"""

import argparse
import json
import math
import statistics as st
import sys
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).resolve().parents[2]
SF = ROOT / "tools/stockfish/stockfish/stockfish-linux-x86-64-universal"
LOG = ROOT / "logs/moves.jsonl"
OUT = Path(__file__).resolve().parent / "out"
ERROR = 10.0  # win% points; the label of experiments/maia_check
BIG = 20.0


def win_pct(score: chess.engine.PovScore, color: chess.Color) -> float:
    cp = max(-1000, min(1000, score.pov(color).score(mate_score=1000)))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def games(rows: list[dict]) -> list[list[dict]]:
    """Split the log into games: a record starts a new game when its moves do not extend the last one."""
    out, prev = [], None
    for r in rows:
        if prev is None or not (len(r["moves"]) > len(prev) and r["moves"][: len(prev)] == prev):
            out.append([])
        out[-1].append(r)
        prev = r["moves"] + [r["uci"]]
    return out


def auc(scores: list[float], labels: list[bool]) -> float | None:
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def review(records: list[dict], depth: int) -> dict:
    moves = records[-1]["moves"] + [records[-1]["uci"]]
    by_ply = {len(r["moves"]): r for r in records}
    model_color = chess.WHITE if len(records[0]["moves"]) % 2 == 0 else chess.BLACK
    board = chess.Board()
    plies = []
    with chess.engine.SimpleEngine.popen_uci(str(SF)) as sf:
        sf.configure({"Threads": 4, "Hash": 256})
        info = sf.analyse(board, chess.engine.Limit(depth=depth))
        for ply, uci in enumerate(moves):
            mover = board.turn
            best_win = win_pct(info["score"], mover)
            best_san = board.san(info["pv"][0]) if info.get("pv") else None
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            board.push(move)
            if board.is_game_over():
                outcome = board.outcome()
                after_win = 100.0 if outcome.winner == mover else 50.0 if outcome.winner is None else 0.0
                info = None
            else:
                info = sf.analyse(board, chess.engine.Limit(depth=depth))
                after_win = win_pct(info["score"], mover)
            entry = {"ply": ply, "move_no": ply // 2 + 1, "side": "model" if mover == model_color else "you", "san": san,
                     "best": best_san, "loss": round(max(0.0, best_win - after_win), 1),
                     "white_win": round(after_win if mover == chess.WHITE else 100 - after_win, 1)}
            r = by_ply.get(ply)
            if r is not None:
                plan = r.get("plan") or {}
                entry.update(difficulty=plan.get("difficulty"), blunder_chance=plan.get("blunder_chance"),
                             rated_by=plan.get("difficulty_text"), level=plan.get("level"), budget_ms=plan.get("budget_ms"),
                             elapsed_ms=r["elapsed_ms"], source=r["source"], ponder_hit=r.get("ponder_hit"),
                             roles=[c["role"] for c in r.get("calls", [])], decision=r.get("decision"))
            plies.append(entry)
            if info is None:
                break
    return {"model_color": "white" if model_color == chess.WHITE else "black", "moves": len(plies),
            "final_fen": board.fen(), "plies": plies}


def report(g: dict) -> str:
    plies = g["plies"]
    model = [p for p in plies if p["side"] == "model"]
    you = [p for p in plies if p["side"] == "you"]
    lines = [f"Model played {g['model_color']}; {len(plies)} plies graded. Final position: {g['final_fen']}", ""]
    for name, side in (("Model", model), ("You", you)):
        losses = [p["loss"] for p in side]
        lines.append(f"{name:5}: {len(side)} moves, mean loss {st.mean(losses):.1f}, errors (>= {ERROR:.0f}) "
                     f"{sum(l >= ERROR for l in losses)}, of them big (>= {BIG:.0f}) {sum(l >= BIG for l in losses)}")
    lines.append("")
    lines.append("Big swings (loss >= 10), with the harness's plan for the model's moves:")
    for p in plies:
        if p["loss"] >= ERROR:
            extra = ""
            if p["side"] == "model":
                chance = p.get("blunder_chance")
                rated = f"Maia {100 * chance:.0f}%" if chance is not None else p.get("rated_by")
                extra = (f" | {p.get('difficulty')} ({rated}), {p.get('level')}, {p.get('elapsed_ms', 0) / 1000:.1f} s of "
                         f"{p.get('budget_ms', 0) / 1000:.1f} s budget{', prepared' if p.get('ponder_hit') else ''}")
            lines.append(f"  {p['move_no']:>3}{'.' if p['ply'] % 2 == 0 else '...'} {p['san']:<7} {p['side']:5} loss {p['loss']:5.1f}"
                         f"  best {p['best']:<7} White win% after {p['white_win']:5.1f}{extra}")
    rated = [p for p in model if p.get("blunder_chance") is not None]
    if rated:
        a = auc([p["blunder_chance"] for p in rated], [p["loss"] >= ERROR for p in rated])
        lines += ["", f"Maia on the model's {len(rated)} rated moves: AUC {a:.2f} for its errors" if a is not None
                  else f"Maia rated {len(rated)} model moves; no split of errors to score"]
    tiers = {}
    for p in model:
        tiers.setdefault(p.get("difficulty") or "forced", []).append(p)
    lines.append("")
    lines.append("Model moves by difficulty: tier, moves, mean loss, errors, levels used, mean seconds")
    for tier in ("easy", "normal", "hard", "forced"):
        if tier in tiers:
            ps = tiers[tier]
            levels = sorted({p.get("level") or "-" for p in ps})
            lines.append(f"  {tier:6} {len(ps):3}  {st.mean(p['loss'] for p in ps):5.1f}  {sum(p['loss'] >= ERROR for p in ps):3}"
                         f"  {','.join(levels):14} {st.mean(p.get('elapsed_ms', 0) for p in ps) / 1000:5.1f}")
    hits = sum(bool(p.get("ponder_hit")) for p in model)
    total = sum(p.get("elapsed_ms", 0) for p in model) / 1000
    lines.append(f"\nModel think time {total:.0f} s over {len(model)} moves; {hits} moves came from pondering.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", type=int, default=-1, help="index into the games in logs/moves.jsonl; -1 is the last")
    ap.add_argument("--depth", type=int, default=16)
    args = ap.parse_args()
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    records = games(rows)[args.game]
    g = review(records, args.depth)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"game-{int(records[0]['ts'])}.json"
    path.write_text(json.dumps(g, indent=1))
    print(report(g))
    print(f"\nPer-ply grades: {path.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
