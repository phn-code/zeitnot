"""Rules facts for one position, line facts after a reply, and the prior-note digest. No evaluation."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

import chess

from .rules import VALUE, describe, hanging


@dataclass(frozen=True)
class Facts:
    checks: tuple[str, ...]
    captures: tuple[str, ...]  # "Nxe5 (pawn)"
    hanging: tuple[str, ...]  # "knight on c3, attacked by the bishop on b4"
    last_move: str | None  # "...Bb4 attacks your knight on c3"
    mate_in_one: tuple[str, ...]


def san_with_number(board: chess.Board, move: chess.Move) -> str:
    """'...Nf6' for a Black move and 'Nf3' for a White move, as a note would write it."""
    san = board.san(move)
    return san if board.turn == chess.WHITE else f"...{san}"


def _attackers_text(board: chess.Board, square: chess.Square, color: chess.Color) -> str:
    names = [describe(board, a) for a in sorted(board.attackers(color, square))]
    return ", ".join(f"the {n}" for n in names)


def compute(board: chess.Board) -> Facts:
    me = board.turn
    legal = list(board.legal_moves)
    checks, captures, mates = [], [], []
    for move in legal:
        san = board.san(move)
        if san.endswith("#"):
            mates.append(san)
        if board.gives_check(move):
            checks.append(san)
        if board.is_capture(move):
            victim = chess.PAWN if board.is_en_passant(move) else board.piece_type_at(move.to_square)
            captures.append(f"{san} ({chess.piece_name(victim)})")

    mine_hanging = hanging(board, me)
    hanging_text = []
    for sq in mine_hanging:
        defended = "defended" if board.attackers(me, sq) else "undefended"
        hanging_text.append(f"{describe(board, sq)}, attacked by {_attackers_text(board, sq, not me)}, {defended}")

    last_move = None
    if board.move_stack:
        last = board.pop()
        last_san = san_with_number(board, last)
        board.push(last)
        targets = [describe(board, sq) for sq in sorted(board.attacks(last.to_square))
                   if (p := board.piece_at(sq)) and p.color == me and p.piece_type != chess.KING]
        what = f"attacks your {', '.join(targets)}" if targets else "attacks none of your pieces"
        last_move = f"{last_san} {what}" + (" and gives check" if board.is_check() else "")
    return Facts(tuple(sorted(checks)), tuple(sorted(captures)), tuple(hanging_text), last_move, tuple(mates))


def opponent_replies(board: chess.Board, move: chess.Move) -> list[str]:
    """The opponent's checks and captures after `move`, in SAN, for a verify call."""
    after = board.copy()
    after.push(move)
    return sorted(after.san(m) for m in after.legal_moves if after.is_capture(m) or after.gives_check(m))


def material(board: chess.Board, color: chess.Color) -> int:
    return sum(VALUE[p.piece_type] for p in board.piece_map().values() if p.color == color and p.piece_type != chess.KING)


def end_facts(board: chess.Board) -> tuple[str, ...]:
    return ("it is checkmate",) if board.is_checkmate() else ("it is a draw",)


def line_facts(after_reply: chess.Board, me: chess.Color) -> tuple[str, ...]:
    """Rules facts for the side to move after a candidate and its reply. Facts only: no score, no verdict."""
    if after_reply.is_game_over():
        return ("you are checkmated",) if after_reply.is_checkmate() else ("it is a draw",)
    f = compute(after_reply)
    out = [f"material: you {material(after_reply, me)}, the opponent {material(after_reply, not me)}"]
    if f.last_move:
        out.append(f"the reply {f.last_move}")
    if f.hanging:
        out.append("your pieces under attack: " + "; ".join(f.hanging))
    if f.mate_in_one:
        out.append("you would have mate in one: " + ", ".join(f.mate_in_one))
    return tuple(out)


def parse_reply(after: chess.Board, text: str | None) -> chess.Move | None:
    """The model's expected reply as a legal move in `after`, or None. Accepts "...Nf6", "12...Nf6" and "Nf6"."""
    if not text:
        return None
    san = re.sub(r"^\d*\s*(?:\.+|…)?\s*", "", text.strip())
    try:
        return after.parse_san(san)
    except ValueError:
        return None


@dataclass(frozen=True)
class Prior:
    """What the model's last move left for its next one: the note, the reply it expected, and the other moves its
    calls answered in that position (its unplayed candidates)."""

    note: str | None
    expected: chess.Move | None
    candidates: tuple[chess.Move, ...] = ()


@dataclass(frozen=True)
class PriorNote:
    text: str
    hit: bool
    expected: str | None  # "...Nf6"
    actual: str  # "...Bxh7+"


MISS_LABEL = "Prepared for a different reply; re-check."
DIGEST_WORDS = 60
DIGEST_CANDIDATES = 4


def _words(parts: list[str]) -> int:
    return len(" ".join(parts).split())


def miss_digest(before_reply: chess.Board, expected: chess.Move | None, reply: chess.Move,
                candidates: Sequence[chess.Move]) -> str:
    """At most DIGEST_WORDS words: the label, what was expected and played, the prepared moves that are still legal,
    and the attacked pieces that are still attacked. Lists lose items from the end until the digest fits."""
    after = before_reply.copy()
    after.push(reply)
    me = after.turn
    actual = san_with_number(before_reply, reply)
    kinds = [k for k, yes in (("capture", before_reply.is_capture(reply)), ("check", before_reply.gives_check(reply))) if yes]
    played = f"{actual} ({', '.join(kinds)})" if kinds else actual
    lead = (f"Expected {san_with_number(before_reply, expected)}, opponent played {played}." if expected
            else f"You named no reply; opponent played {played}.")
    legal = [after.san(m) for m in dict.fromkeys(candidates) if m in after.legal_moves][:DIGEST_CANDIDATES]
    threats = [f"{'your' if color == me else 'their'} {describe(after, sq)}"
               for color in (me, not me) for sq in hanging(after, color)
               if sq in hanging(before_reply, color) and before_reply.piece_at(sq) == after.piece_at(sq)]
    while True:
        parts = [MISS_LABEL, lead]
        if legal:
            parts.append(f"Prepared moves still legal: {', '.join(legal)}.")
        if threats:
            parts.append(f"Still attacked: {'; '.join(threats)}.")
        if _words(parts) <= DIGEST_WORDS:
            return " ".join(parts)
        (threats if len(threats) >= len(legal) else legal).pop()


def prior_note(before_reply: chess.Board, prior: Prior, reply: chess.Move, prepared: Sequence[chess.Move] = ()) -> PriorNote:
    """The prior_note section. A hit keeps the note; a miss replaces it with the digest. `prepared` are the moves
    pondering answered for the positions the model expected."""
    actual = san_with_number(before_reply, reply)
    exp = san_with_number(before_reply, prior.expected) if prior.expected else None
    if prior.expected == reply:
        text = f"{prior.note}\nThe opponent played {actual}, as you expected." if prior.note else f"The opponent played {actual}, as you expected."
        return PriorNote(text, True, exp, actual)
    return PriorNote(miss_digest(before_reply, prior.expected, reply, [*prepared, *prior.candidates]), False, exp, actual)
