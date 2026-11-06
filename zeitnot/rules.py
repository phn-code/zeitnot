"""Chess rules the harness needs without an engine: replay, prompt text, one-ply safety, the emergency move."""

from collections.abc import Sequence

import chess

VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}

INSTRUCTIONS = (
    "You are a strong chess player. Check every capture, check and threat for both sides, "
    "then choose the best move for the side to move."
)


class IllegalHistory(ValueError):
    pass


def board_from_moves(moves: Sequence[str]) -> chess.Board:
    board = chess.Board()
    for i, uci in enumerate(moves):
        try:
            board.push_uci(uci)
        except ValueError as e:
            raise IllegalHistory(f"move {i + 1} ({uci!r}) is not legal: {e}") from e
    return board


def _diagram(board: chess.Board) -> str:
    rows = []
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            cells.append(piece.symbol() if piece else ".")
        rows.append(f"{rank + 1} " + " ".join(cells))
    rows.append("  a b c d e f g h")
    return "\n".join(rows)


def render(board: chess.Board, choices: Sequence[str], label: str = "Legal moves") -> str:
    side = "White" if board.turn == chess.WHITE else "Black"
    movetext = chess.Board().variation_san(board.move_stack) or "(none)"
    return (
        f"Chess position (FEN): {board.fen()}\n\n"
        f"Board (uppercase is White, lowercase is Black):\n{_diagram(board)}\n\n"
        f"Moves so far: {movetext}\n\n{side} to move.\n\n"
        f"{label}: {', '.join(choices)}"
    )


def is_safe(board: chess.Board, move: chess.Move) -> bool:
    """False when the moved piece lands en prise, unless it captured something at least as valuable."""
    me = board.turn
    moved_value = VALUE[board.piece_type_at(move.from_square)]
    captured = board.piece_type_at(move.to_square)
    if captured and VALUE[captured] >= moved_value:
        return True
    after = board.copy()
    after.push(move)
    attackers = after.attackers(not me, move.to_square)
    if not attackers:
        return True
    cheapest = min(VALUE[after.piece_type_at(s)] for s in attackers)
    return bool(after.attackers(me, move.to_square)) and cheapest >= VALUE[after.piece_type_at(move.to_square)]


def safe_moves(board: chess.Board) -> list[chess.Move]:
    """Legal moves that pass is_safe in a fixed order (UCI ascending). All legal moves when none pass."""
    legal = sorted(board.legal_moves, key=lambda m: m.uci())
    return [m for m in legal if is_safe(board, m)] or legal


def emergency_move(board: chess.Board) -> chess.Move:
    return safe_moves(board)[0]
