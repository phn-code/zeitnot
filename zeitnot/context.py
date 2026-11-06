"""Per-move context v2: one compact prompt rebuilt from the board each move, XML sections, the question last."""

from collections.abc import Sequence
from dataclasses import dataclass

import chess

from .facts import Facts, PriorNote
from .timeman import Clock

SYSTEM = (
    "You are a strong chess player. Each message describes one position: the game, the clocks, the moves so far, "
    "the position, facts computed from the rules, your note from the previous move, and the moves you may play. "
    'Answer with JSON whose keys come in this order. "commentary" is 60 words or less for the people watching: the '
    'candidate moves you compared and the threats on the board. "move" is one of the allowed moves. "expected_reply" is the '
    'opponent\'s reply you expect after your move, in SAN, for example "Nf6", or "" if you expect none. "note" is 40 words '
    "or less with your plan. "
    "The prior note is a hypothesis from one move ago. Re-check it against the position."
)
ANSWER = ("Write a commentary of 60 words or less, then the move, then the reply you expect, then a note of 40 words or "
          "less with your plan.")
TASK = "Based on the position above, choose your move. " + ANSWER
FORCING_TASK = "Based on the position above, choose the most forcing good move: a check, a capture or a direct threat. " + ANSWER
PIECE_ORDER = (chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN)


@dataclass(frozen=True)
class Situation:
    """What the prompt needs besides the board."""

    clock: Clock
    time_control: str | None
    facts: Facts
    prior: PriorNote | None


def side_name(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"


def mmss(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def piece_list(board: chess.Board, color: chess.Color) -> str:
    items = []
    for pt in PIECE_ORDER:
        for sq in sorted(board.pieces(pt, color)):
            items.append(chess.piece_symbol(pt).upper() + chess.square_name(sq))
    return f"{side_name(color)}: {' '.join(items)}"


def _section(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def build(board: chess.Board, s: Situation, moves: Sequence[chess.Move], suggestion: str | None = None,
          task: str = TASK, lines: str | None = None) -> str:
    me = board.turn
    f = s.facts
    game = f"You play {side_name(me)}." + (f" Time control {s.time_control}." if s.time_control else "")
    game += f" Move {board.fullmove_number}, {side_name(me)} to move."
    clock = f"Your time {mmss(s.clock.ai_ms)}. Opponent's time {mmss(s.clock.human_ms)}. Increment {s.clock.increment_ms // 1000} s."
    movetext = chess.Board().variation_san(board.move_stack) or "(none)"
    position = f"FEN: {board.fen()}\n{piece_list(board, chess.WHITE)}\n{piece_list(board, chess.BLACK)}"
    facts = [
        f"Checks you can give: {', '.join(f.checks) or 'none'}",
        f"Captures you can make: {', '.join(f.captures) or 'none'}",
        f"Your pieces under attack and undefended or attacked by a cheaper piece: {'; '.join(f.hanging) or 'none'}",
    ]
    if f.last_move:
        facts.append(f"The opponent's last move: {f.last_move}")
    if f.mate_in_one:
        facts.append(f"Mate in one: {', '.join(f.mate_in_one)}")
    sections = [
        _section("game", game),
        _section("clock", clock),
        _section("moves", movetext),
        _section("position", position),
        _section("facts", "\n".join(facts)),
    ]
    if s.prior is not None:
        sections.append(_section("prior_note", s.prior.text))
    # The allowed moves carry no "hangs piece" tags: that rule marked the winning sacrifices Opus missed as blunders.
    allowed = ", ".join(sorted(board.san(m) for m in moves))
    sections.append(_section("moves_allowed", allowed))
    if suggestion:
        sections.append(_section("suggestion", suggestion))
    if lines:
        sections.append(_section("lines", lines))
    return "\n".join(sections) + "\n\n" + task
