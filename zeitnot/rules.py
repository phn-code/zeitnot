"""Chess rules the harness needs without an engine: replay, one-ply safety, hanging pieces and the rules check."""

from collections.abc import Sequence

import chess

VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}


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


def hanging(board: chess.Board, color: chess.Color) -> list[chess.Square]:
    """Squares of `color`'s pieces, king excluded, that are attacked and undefended or attacked by a cheaper piece."""
    out = []
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        cheapest = min(VALUE[board.piece_type_at(a)] for a in attackers)
        if not board.attackers(color, sq) or cheapest < VALUE[piece.piece_type]:
            out.append(sq)
    return sorted(out)


def describe(board: chess.Board, square: chess.Square) -> str:
    return f"{chess.piece_name(board.piece_type_at(square))} on {chess.square_name(square)}"


def check_move(board: chess.Board, move: chess.Move) -> str | None:
    """The rules check on an answer. None when it passes, otherwise why it fails.

    It fails when the moved piece is left hanging (is_safe) or another of my pieces becomes hanging. The moved
    piece is left out of the count so an even trade, which is_safe allows, is not flagged twice.
    """
    me = board.turn
    after = board.copy()
    after.push(move)
    if not is_safe(board, move):
        return f"it leaves the {describe(after, move.to_square)} hanging"
    before = set(hanging(board, me)) - {move.from_square}
    now = set(hanging(after, me)) - {move.to_square}
    if len(now) > len(before):
        newly = sorted(now - before) or sorted(now)
        return f"it leaves the {describe(after, newly[0])} hanging"
    return None
