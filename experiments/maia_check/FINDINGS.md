# Maia-2 as the difficulty rater: findings

**Question.** Can a pretrained model of human play tell which positions Opus will blunder in, well enough to decide how long Opus thinks? Nobody publishes a model of LLM chess errors, so the candidate is a model of human difficulty that needs no training.

**Setup.** This check made no new model calls. It rated the 60 positions that Opus already played and Stockfish already graded (`experiments/harness_eval/out`).
- **Model.** Maia-2 0.11.0, blitz model, on the CPU with both players set to 1800.
- **Measures.** `score.py` computes three, fixed before any result was seen:
  - **Expected loss (the primary measure).** Maia's human move probabilities, weighted by each move's drop in Maia's own expected score from the best move.
  - **Blunder probability.** The probability mass on moves that drop that score by 10 points or more.
  - **Entropy.** How spread out Maia's move probabilities are.
- **Label.** A blunder is a move that loses 10 or more points of Lichess win%.
- **Speed.** One position, together with the position after each of its legal moves, takes 0.23 s on the CPU.

```bash
.venv/bin/python experiments/maia_check/score.py experiments/harness_eval/out/positions.jsonl experiments/maia_check/out/scores.json ~/.cache/zeitnot/maia2
.venv/bin/python experiments/maia_check/analyze.py . experiments/maia_check/out/scores.json      # out/analysis.txt
.venv/bin/python -I experiments/maia_check/calibrate.py experiments/harness_eval/out/positions.jsonl ~/.cache/zeitnot/maia2 300  # out/calibration.txt
```

## Results

Opus at `low` blundered in 10 of the 60 positions. The brackets show 95% bootstrap intervals.

| Signal | AUC |
|---|---|
| Maia expected loss (primary) | 0.69 [0.52, 0.85] |
| Maia blunder probability | 0.73 [0.56, 0.89] |
| Maia entropy | 0.59 [0.41, 0.77] |
| The v3 rules flag ("critical") | 0.47 [0.29, 0.64] |
| The rules check on Opus's move | 0.53 [0.46, 0.64] |
| Count of legal moves | 0.73 [0.55, 0.89] |
| Second opinion disagrees (40 positions) | 0.73 [0.57, 0.88] |
| Second opinion plus Maia blunder probability (40 positions) | 0.83 [0.68, 0.95] |

Pooling every graded Opus answer per position (14 positions with a blunder) gives similar numbers: 0.72 for expected loss and 0.76 for blunder probability.

## What it means

- **Maia beats the rules it replaces.** The v3 criticality flag predicted Opus's blunders no better than chance.
- **Much of Maia's signal is how busy the board is.** Expected loss correlates +0.65 with the count of legal moves. With that count removed, expected loss falls to 0.54 and blunder probability to 0.62. The plain count of legal moves scored as well as Maia, so it is the fallback.
- **Maia adds to the second opinion.** Disagreement alone scores 0.73; with Maia's rank added, 0.83.
- **The power is low.** With 10 blunders, every interval spans about 0.3. None of these differences is significant.
- **The shipped measure was chosen after seeing the results.** The primary measure was expected loss. The app uses blunder probability, which scored a little higher and reads as a plain sentence ("1800 players blunder here 34% of the time").

## Calibration and the opening

- **Maia-2 never trained on the first 10 plies.** `maia2/dataset.py` keeps only `move_ply > 10`. Before ply 10 its output is wrong: at the start position it gives 1. Nf3 55% and 1. e4 almost nothing, and it rates the position hard.
- **So the app does not use it there.** In the first 10 plies, the count of legal moves rates the position instead.
- **The cut points come from real games, not training.** Over 300 positions past ply 10 from the blitz games behind the evaluation set, 46% fall below 1% (easy) and 22% at 10% or above (hard). By move number, the median is 0.2% in moves 6 to 8, 1.9% in moves 9 to 15, 3.8% in moves 16 to 25, and 0.6% from move 26.
- **The fallback's cut points come from the same positions.** The count of legal moves has its 25th percentile at 28 and its 75th at 39.

## What decides it

The app logs each move's difficulty, blunder chance and level in `logs/moves.jsonl`. Grading those moves with Stockfish after the game gives labels from real play at no extra model cost. Once about 30 games are in, compare Maia with the legal-move count on those labels, and keep Maia only if it still beats the count.
