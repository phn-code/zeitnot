import sys

import chess
import pytest

from zeitnot.difficulty import LegalMovesRater, MaiaRater, maia_installed, maia_text, maia_tier, make_rater


def board(moves=""):
    b = chess.Board()
    for uci in moves.split():
        b.push_uci(uci)
    return b


# A middlegame position (ply 14) from the evaluation set.
MIDDLEGAME = chess.Board("r1bqk2r/pp1nnppp/2p1p3/3pP3/3P4/2PB1N2/P1P2PPP/R1BQK2R w KQkq - 1 8")


@pytest.mark.parametrize(("chance", "tier"), [(0.0, "easy"), (0.009, "easy"), (0.01, "normal"), (0.099, "normal"),
                                              (0.10, "hard"), (0.9, "hard")])
def test_maia_cut_points(chance, tier):
    assert maia_tier(chance) == tier


def test_maia_text_reads_as_a_share_of_players():
    assert maia_text(0.004) == "1800 players blunder here under 1% of the time"
    assert maia_text(0.337) == "1800 players blunder here 34% of the time"


def test_legal_moves_stand_in_with_percentile_cut_points():
    rater = LegalMovesRater()
    assert rater.rate(board()).tier == "easy" and rater.rate(board()).text == "20 legal moves"
    assert rater.rate(MIDDLEGAME).tier in ("normal", "hard") and rater.rate(MIDDLEGAME).blunder_chance is None


def test_without_maia_the_legal_move_count_rates(tmp_path):
    assert make_rater(False, tmp_path).name == "legal moves"


def test_the_opening_is_rated_by_the_fallback_without_loading_maia(tmp_path):
    rater = MaiaRater(tmp_path / "never")
    d = rater.rate(board("e2e4 e7e5 g1f3 b8c6"))
    assert d.text.endswith("legal moves") and rater._net is None and not rater.failed


def test_a_maia_that_cannot_load_falls_back_for_good(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "maia2", None)  # import maia2 raises ImportError
    rater = MaiaRater(tmp_path)
    d = rater.rate(MIDDLEGAME)
    assert rater.failed and rater.name == "legal moves" and d.blunder_chance is None
    assert rater.rate(MIDDLEGAME).text.endswith("legal moves")


def test_a_maia_error_on_one_position_falls_back_for_that_position_only(tmp_path):
    rater = MaiaRater(tmp_path)
    rater._net = object()  # loaded

    calls = []

    def flaky(b):
        calls.append(b.fen())
        if len(calls) == 1:
            raise RuntimeError("bad tensor")
        return 0.25

    rater._blunder_chance = flaky
    first, second = rater.rate(MIDDLEGAME), rater.rate(MIDDLEGAME)
    assert first.text.endswith("legal moves") and not rater.failed
    assert (second.tier, second.blunder_chance) == ("hard", 0.25)


@pytest.mark.skipif(not maia_installed(), reason="the maia extra is not installed")
def test_maia_rates_a_real_position():
    from zeitnot.config import Settings

    rater = MaiaRater(Settings().maia_dir)
    d = rater.rate(MIDDLEGAME)
    assert d.blunder_chance is not None and 0 <= d.blunder_chance <= 1 and d.tier == maia_tier(d.blunder_chance)


def test_a_maia_still_loading_rates_by_the_fallback_instead_of_waiting(tmp_path):
    rater = MaiaRater(tmp_path)
    rater.loading = True  # the server's warm-up thread is downloading the model
    d = rater.rate(MIDDLEGAME)
    assert d.text.endswith("legal moves") and rater._net is None and not rater.failed
