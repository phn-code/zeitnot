"""P0 follow-up. Throwaway. Which cheap signals predict that Luna's fast answer (effort none) is a mistake?"""

import json
import statistics

import chess

import probe

VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
MISTAKE = 20


def en_prise(board: chess.Board, color: chess.Color) -> int:
    """Pieces of `color` attacked and either undefended or attacked by a cheaper piece. Rules-level, no search."""
    count = 0
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        cheapest = min(VALUE[board.piece_type_at(a)] for a in attackers)
        if not board.attackers(color, sq) or cheapest < VALUE[piece.piece_type]:
            count += 1
    return count


def features(pos: dict, none_move: str, dec: dict) -> dict:
    board = chess.Board(pos["fen"])
    me = board.turn
    move = board.parse_san(none_move)
    after = board.copy()
    after.push(move)
    return {
        "in_check": float(board.is_check()),
        "captures_available": float(sum(board.is_capture(m) for m in board.legal_moves)),
        "my_en_prise": float(en_prise(board, me)),
        "their_en_prise": float(en_prise(board, not me)),
        "last_was_capture": float(_last_capture(pos)),
        "my_en_prise_after_move": float(en_prise(after, me)),
        "disagrees_with_decisions": float(dec["move"] != none_move),
        "decisions_p_top_low": 1 - dec["p_top"],
        "decisions_p_of_none_move_low": 1 - dec["probs"].get(none_move, 0.0),
        "decisions_sharpness": dec.get("sharpness", 0.0),
        "legal_moves": float(board.legal_moves.count()),
    }


def _last_capture(pos: dict) -> bool:
    board = chess.Board()
    for u in pos["history"][:-1]:
        board.push_uci(u)
    return board.is_capture(chess.Move.from_uci(pos["history"][-1]))


def auc(scores: list[float], labels: list[bool]) -> float:
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main() -> None:
    positions = probe.load_jsonl(probe.OUT / "positions.jsonl")
    grades = json.loads((probe.OUT / "grades.json").read_text())
    recs = {}
    for r in probe.load_jsonl(probe.OUT / "api.jsonl"):
        if "error" not in r:
            recs[(r["pid"], r["rung"])] = r

    rows = []
    for pos in positions:
        none = recs.get((pos["id"], "responses-none"))
        dec = recs.get((pos["id"], "decisions-rich"))
        if not none or not dec:
            continue
        f = features(pos, none["move"], dec)
        f["loss"] = probe.loss(grades, pos["id"], none["move"])
        rows.append(f)

    labels = [r["loss"] >= MISTAKE for r in rows]
    print(f"{len(rows)} positions, effort-none mistakes (loss >= {MISTAKE}): {sum(labels)} ({sum(labels) / len(rows):.0%})\n")
    print("AUC for predicting a mistake (0.5 = useless, 1.0 = perfect)")
    for name in [k for k in rows[0] if k != "loss"]:
        print(f"  {name:<32} {auc([r[name] for r in rows], labels):.2f}")

    print("\nEscalation rules: share of positions escalated, share of mistakes caught, mean loss if escalated moves became 0")
    rules = {
        "disagree": lambda r: r["disagrees_with_decisions"],
        "my piece en prise after move": lambda r: r["my_en_prise_after_move"] > 0,
        "in check or my piece en prise now": lambda r: r["in_check"] or r["my_en_prise"] > 0,
        "en prise after move OR disagree": lambda r: r["my_en_prise_after_move"] > 0 or r["disagrees_with_decisions"],
        "en prise after move OR en prise now OR check": lambda r: r["my_en_prise_after_move"] > 0 or r["my_en_prise"] > 0 or r["in_check"],
        "always": lambda r: True,
    }
    base = statistics.mean(r["loss"] for r in rows)
    for name, rule in rules.items():
        esc = [bool(rule(r)) for r in rows]
        caught = sum(e and y for e, y in zip(esc, labels)) / max(1, sum(labels))
        print(f"  {name:<46} escalate {sum(esc) / len(rows):4.0%}  catch {caught:4.0%}")
    print(f"\nmean loss of effort none: {base:.1f}")


if __name__ == "__main__":
    main()
