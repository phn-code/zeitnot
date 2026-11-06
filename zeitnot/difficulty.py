"""How hard a position is. It sets how much of the clock a move gets, never which move: no rater's output reaches
the model.

Maia-2 (CSSLab, MIT licence) rates a position by the chance that an 1800 blitz player blunders in it: its human move
probabilities, weighted by whether each move drops its expected score by 10 points or more. On the 60 graded Opus
positions that chance separated Opus's blunders with AUC 0.73, against 0.47 for the old rules flag
(experiments/maia_check/FINDINGS.md). Maia-2 never trained on the first 10 plies of a game, so it does not rate
them; neither does it rate anything when the optional maia extra is missing or fails. The count of legal moves stands
in there. It scored about as well on the 60 positions but sees none of the threats.

The cut points are not trained. Over 300 positions past ply 10 from real blitz games, 46% fall below 1% and 22% at
10% or above.
"""

import importlib.util
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import chess

Tier = Literal["easy", "normal", "hard"]
EASY_BELOW = 0.01
HARD_FROM = 0.10
EASY_BELOW_MOVES = 28
HARD_FROM_MOVES = 40
MAIA_ELO = 1800
MAIA_FIRST_PLY = 10  # maia2/dataset.py trains on move_ply > 10
BLUNDER_DROP = 0.10
log = logging.getLogger("zeitnot.difficulty")


@dataclass(frozen=True)
class Difficulty:
    tier: Tier
    blunder_chance: float | None  # Maia's chance that an 1800 blitz player blunders here; None without Maia
    text: str  # "1800 players blunder here 34% of the time"


class Rater(Protocol):
    name: str

    def rate(self, board: chess.Board) -> Difficulty: ...


class LegalMovesRater:
    name = "legal moves"

    def rate(self, board: chess.Board) -> Difficulty:
        n = board.legal_moves.count()
        tier: Tier = "easy" if n < EASY_BELOW_MOVES else "hard" if n >= HARD_FROM_MOVES else "normal"
        return Difficulty(tier, None, f"{n} legal moves")


def maia_tier(chance: float) -> Tier:
    return "easy" if chance < EASY_BELOW else "hard" if chance >= HARD_FROM else "normal"


def maia_text(chance: float) -> str:
    share = "under 1%" if chance < 0.01 else f"{round(100 * chance)}%"
    return f"{MAIA_ELO} players blunder here {share} of the time"


class MaiaRater:
    """Maia-2 blitz on the CPU, about 0.25 s a position: one batch of the position and every position after a legal
    move. `fallback` rates what Maia does not: the first 10 plies, every position while Maia is still loading (the
    first load downloads 280 MB, and a game must not wait for it), and everything once Maia fails to load."""

    def __init__(self, model_dir: Path, fallback: Rater | None = None):
        self.model_dir = model_dir
        self.fallback = fallback or LegalMovesRater()
        self.failed = False
        self.loading = False
        self._lock = threading.Lock()
        self._net = None
        self._prepared = None

    @property
    def name(self) -> str:
        return self.fallback.name if self.failed else "maia2"

    def load(self) -> None:
        """Downloads the 280 MB checkpoint on first use, then checks its hash and loads it. Blocks; call it off the loop."""
        self.loading = True
        try:
            self._load()
        finally:
            self.loading = False

    def _load(self) -> None:
        with self._lock:
            if self._net is not None or self.failed:
                return
            try:
                from maia2 import inference, model

                net = model.from_pretrained(type="blitz", device="cpu", save_root=str(self.model_dir))
                net.eval()
                self._prepared = inference.prepare()
                self._net = net
            except Exception:
                log.exception("Maia-2 did not load; rating positions by %s", self.fallback.name)
                self.failed = True

    def rate(self, board: chess.Board) -> Difficulty:
        if board.ply() < MAIA_FIRST_PLY or (self._net is None and self.loading):
            return self.fallback.rate(board)
        self.load()
        if not self.failed:
            try:
                with self._lock:
                    chance = self._blunder_chance(board)
                return Difficulty(maia_tier(chance), chance, maia_text(chance))
            except Exception:
                log.exception("Maia-2 failed on %s; rating it by %s", board.fen(), self.fallback.name)
        return self.fallback.rate(board)

    def _blunder_chance(self, board: chess.Board) -> float:
        import torch
        from maia2 import inference

        all_moves, elo_dict, reverse = self._prepared
        moves = list(board.legal_moves)
        after = []
        for move in moves:
            b = board.copy(stack=False)
            b.push(move)
            after.append(b)
        scored = [b for b in after if not b.is_game_over()]
        inputs = [inference.preprocessing(b.fen(), MAIA_ELO, MAIA_ELO, elo_dict, all_moves) for b in [board, *scored]]
        with torch.no_grad():
            logits, _, value = self._net(torch.stack([i[0] for i in inputs]), torch.tensor([i[1] for i in inputs]),
                                         torch.tensor([i[2] for i in inputs]))
        legal = torch.stack([i[3] for i in inputs])
        policy = inference._masked_softmax(logits[:1], legal[:1])[0]
        to_move = (value / 2 + 0.5).clamp(0, 1).flatten().tolist()  # each row's score for its side to move
        score = {}
        values = iter(to_move[1:])
        for move, b in zip(moves, after, strict=True):
            if b.is_game_over():
                winner = b.outcome().winner
                score[move] = 0.5 if winner is None else float(winner == board.turn)
            else:
                score[move] = 1 - next(values)
        best = max(score.values())
        chance = 0.0
        for index in legal[0].nonzero().flatten().tolist():
            uci = reverse[index]
            move = chess.Move.from_uci(inference.mirror_move(uci) if board.turn == chess.BLACK else uci)
            if best - score[move] >= BLUNDER_DROP:
                chance += float(policy[index])
        return min(1.0, chance)


def maia_installed() -> bool:
    return importlib.util.find_spec("maia2") is not None and importlib.util.find_spec("torch") is not None


def make_rater(use_maia: bool, model_dir: Path) -> Rater:
    if use_maia and maia_installed():
        return MaiaRater(model_dir)
    return LegalMovesRater()
