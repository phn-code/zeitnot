import chess
import pytest

from zeitnot.rules import IllegalHistory, board_from_moves, emergency_move, render, safe_moves

START = chess.Board()


def sans(board):
    return {board.san(m) for m in safe_moves(board)}


def test_board_from_moves_replays_history():
    board = board_from_moves(["e2e4", "e7e5"])
    assert board.fen().startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w")


@pytest.mark.parametrize("history", [["e2e5"], ["e2e4", "e2e4"], ["zz"], ["e2e4", "e7e5", "d1h5", "g8f6", "h5f7", "e8f7", "f7f7"]])
def test_board_from_moves_rejects_illegal_history(history):
    with pytest.raises(IllegalHistory):
        board_from_moves(history)


def test_safe_moves_excludes_queen_landing_en_prise():
    board = chess.Board("4k3/8/8/3p4/8/8/8/3QK3 w - - 0 1")
    assert {"Qe4", "Qc4"}.isdisjoint(sans(board))
    assert "Qd4" in sans(board)


def test_safe_moves_allows_queen_capturing_an_undefended_pawn():
    board = chess.Board("4k3/8/8/3p4/8/8/8/3QK3 w - - 0 1")
    assert "Qxd5" in sans(board)


def test_safe_moves_excludes_piece_attacked_by_cheaper_piece_even_when_defended():
    board = chess.Board("4k3/8/2p5/8/8/8/6B1/3QK3 w - - 0 1")
    assert "Qd5" not in sans(board)


def test_safe_moves_allows_defended_piece_attacked_by_costlier_piece():
    attacked_alone = chess.Board("4k3/8/8/8/8/2r5/3P4/4K3 w - - 0 1")
    assert "d3" not in sans(attacked_alone)
    defended = chess.Board("4k3/8/8/8/8/2r5/3PK3/8 w - - 0 1")
    assert "d3" in sans(defended)


def test_safe_moves_allows_capture_of_equal_value_even_if_recaptured():
    board = chess.Board("4k3/8/2p5/3n4/8/2N5/8/4K3 w - - 0 1")
    assert "Nxd5" in sans(board)


def test_safe_moves_falls_back_to_all_legal_moves_when_none_pass():
    board = chess.Board("6k1/5p2/P6K/r6b/3P4/8/8/8 w - - 0 1")
    legal = sorted(m.uci() for m in board.legal_moves)
    assert len(legal) == 2
    assert [m.uci() for m in safe_moves(board)] == legal


def test_safe_moves_is_a_fixed_order():
    uci = [m.uci() for m in safe_moves(START)]
    assert uci == sorted(uci) and len(uci) == 20


def test_emergency_move_is_first_safe_move():
    board = chess.Board("4k3/8/8/3p4/8/8/8/3QK3 w - - 0 1")
    assert emergency_move(board) == safe_moves(board)[0]
    assert emergency_move(START).uci() == "a2a3"


def test_render_lists_choices_and_history():
    board = board_from_moves(["e2e4", "e7e5"])
    text = render(board, ["Nf3", "Nc3"])
    assert "1. e4 e5" in text and "White to move." in text and "Legal moves: Nf3, Nc3" in text
    assert "(none)" in render(START, ["e4"])
