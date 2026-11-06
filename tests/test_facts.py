import chess
import pytest

from zeitnot.context import SYSTEM, TASK, Situation, build, piece_list
from zeitnot.facts import DIGEST_WORDS, MISS_LABEL, Prior, compute, miss_digest, opponent_replies, parse_reply, prior_note
from zeitnot.rules import board_from_moves, check_move
from zeitnot.timeman import Clock


def board(moves):
    return board_from_moves(moves.split())


def test_facts_list_checks_captures_hanging_pieces_and_the_last_move():
    b = board("e2e4 d7d5")
    f = compute(b)
    assert f.captures == ("exd5 (pawn)",) and f.checks == ("Bb5+",)
    assert f.hanging == ("pawn on e4, attacked by the pawn on d5, undefended",)
    assert f.last_move == "...d5 attacks your pawn on e4"
    assert f.mate_in_one == ()


def test_mate_in_one_and_check_are_reported():
    b = board("e2e4 e7e5 f1c4 b8c6 d1h5 g8f6")
    assert compute(b).mate_in_one == ("Qxf7#",)
    after = board("e2e4 e7e5 f1c4 b8c6 d1h5 g8f6 h5f7")
    assert compute(after).last_move.endswith("and gives check")


def test_rules_check_flags_the_moved_piece_and_new_hanging_pieces_but_not_even_trades():
    b = board("e2e4 d7d5")
    assert check_move(b, b.parse_san("Qg4")) == "it leaves the queen on g4 hanging"
    assert check_move(b, b.parse_san("Nc3")) is None
    assert check_move(b, b.parse_san("exd5")) is None
    trade = chess.Board("4k3/8/2p5/3n4/8/2N5/8/4K3 w - - 0 1")
    assert check_move(trade, trade.parse_san("Nxd5")) is None
    # Moving the only defender of the c3 knight away leaves it hanging.
    pinned = chess.Board("4k3/8/8/b7/8/2N5/1B6/4K3 w - - 0 1")
    assert check_move(pinned, pinned.parse_san("Ba3")) == "it leaves the knight on c3 hanging"


def test_opponent_replies_are_their_checks_and_captures_after_the_move():
    b = board("e2e4 d7d5")
    assert opponent_replies(b, b.parse_san("Qg4")) == ["Bxg4", "dxe4"]


@pytest.mark.parametrize(("text", "uci"), [
    ("e5", "e7e5"), ("...e5", "e7e5"), ("1...e5", "e7e5"), (" Nf6 ", "g8f6"), ("…Nc6", "b8c6"),
    ("Ke7", None), ("Nf3", None), ("", None), (None, None), ("e5 and then d5", None),
], ids=["plain", "dots", "numbered", "spaces", "ellipsis", "illegal king", "white move", "empty", "none", "prose"])
def test_expected_reply_is_validated_against_the_position_after_the_move(text, uci):
    got = parse_reply(board("e2e4"), text)
    assert (got.uci() if got else None) == uci


def test_prior_note_hit_keeps_the_note():
    before = board("e2e4")
    hit = prior_note(before, Prior("Centre.", before.parse_san("e5")), before.parse_san("e5"))
    assert hit.hit and hit.text == "Centre.\nThe opponent played ...e5, as you expected."
    assert prior_note(before, Prior(None, before.parse_san("e5")), before.parse_san("e5")).text == "The opponent played ...e5, as you expected."


def test_a_miss_is_a_labelled_digest_of_legal_prepared_moves_and_standing_threats():
    # After 1. e4 d5 the model (Black) expected 2. exd5 and prepared ...Qxd5 for it. White played 2. Nf3 instead.
    before = board("e2e4 d7d5")
    prepared = [chess.Move.from_uci("d8d5"), chess.Move.from_uci("d5e4"), chess.Move.from_uci("g8f6")]
    miss = prior_note(before, Prior("Take back with the queen.", before.parse_san("exd5")), before.parse_san("Nf3"), prepared)
    assert not miss.hit and miss.text.startswith(MISS_LABEL)
    assert "Take back" not in miss.text
    assert "Expected exd5, opponent played Nf3." in miss.text
    assert "Prepared moves still legal: dxe4, Nf6." in miss.text, "Qxd5 is no longer legal"
    assert miss.text.endswith("Still attacked: their pawn on e4.")
    assert len(miss.text.split()) <= DIGEST_WORDS
    defended = prior_note(before, Prior(None, before.parse_san("exd5")), before.parse_san("Nc3"), prepared)
    assert "Still attacked" not in defended.text, "Nc3 defends e4, so that threat is gone"


def test_the_digest_names_no_reply_when_none_was_expected_and_never_exceeds_sixty_words():
    before = board("e2e4")
    silent = prior_note(before, Prior("Keep calm.", None), before.parse_san("e5"))
    assert silent.text == f"{MISS_LABEL} You named no reply; opponent played ...e5."
    busy = chess.Board("r1bqkbnr/pppppppp/2n5/8/8/2N5/PPPPPPPP/R1BQKBNR w KQkq - 2 2")
    every = list(busy.legal_moves)
    reply = busy.parse_san("Nb5")
    text = miss_digest(busy, None, reply, every * 3)
    assert len(text.split()) <= DIGEST_WORDS and text.startswith(MISS_LABEL)
    assert text.count(",") <= 3, "at most four prepared moves"


def test_context_sections_in_order_with_the_question_last():
    b = board("e2e4 d7d5")
    s = Situation(Clock(581_000, 590_000, 5_000), "10+5", compute(b), None)
    prompt = build(b, s, list(b.legal_moves))
    tags = [t for t in ("<game>", "<clock>", "<moves>", "<position>", "<facts>", "<moves_allowed>") if t in prompt]
    assert tags == ["<game>", "<clock>", "<moves>", "<position>", "<facts>", "<moves_allowed>"]
    assert [prompt.index(t) for t in tags] == sorted(prompt.index(t) for t in tags)
    assert "<prior_note>" not in prompt and prompt.endswith(TASK)
    assert "You play White. Time control 10+5. Move 2, White to move." in prompt
    assert "Your time 9:41. Opponent's time 9:50. Increment 5 s." in prompt
    assert "1. e4 d5" in prompt and f"FEN: {b.fen()}" in prompt
    assert "Nc3, " in prompt and "Qg4, " in prompt and "hangs" not in prompt, "allowed moves carry no rule tags"
    assert "Diagram" not in prompt and "a b c d e f g h" not in prompt
    assert "The prior note is a hypothesis from one move ago. Re-check it against the position." in SYSTEM


def test_context_includes_the_prior_note_and_a_suggestion():
    b = board("e2e4 d7d5")
    prior = prior_note(board("e2e4"), Prior("Centre.", board("e2e4").parse_san("e5")), board("e2e4").parse_san("d5"))
    s = Situation(Clock(60_000, 60_000, 0), None, compute(b), prior)
    prompt = build(b, s, list(b.legal_moves), "Other players suggest Nc3 and exd5.", "Choose.")
    assert f"<prior_note>\n{MISS_LABEL} Expected ...e5, opponent played ...d5.\n</prior_note>" in prompt
    assert "<suggestion>\nOther players suggest Nc3 and exd5.\n</suggestion>" in prompt
    assert prompt.endswith("Choose.") and "Time control" not in prompt


def test_piece_list():
    assert piece_list(chess.Board(), chess.WHITE) == (
        "White: Ke1 Qd1 Ra1 Rh1 Bc1 Bf1 Nb1 Ng1 Pa2 Pb2 Pc2 Pd2 Pe2 Pf2 Pg2 Ph2")
