# P0 probe results

Model `gpt-6-luna`. Positions are 1200 to 2200 rated Lichess blitz games from January 2013. Loss is the drop in win probability (percentage points) against Stockfish 19's best move at depth 16. Latency is wall clock per call from this machine, after warm-up, with up to 6 Responses calls and 6 Decisions calls in flight.

## All 200 positions

| Rung | n | Errors | Mean loss | Median loss | Best move | Good (<5) | Blunder (>=30) | p50 s | p90 s | Reasoning tok | $/call |
|---|---|---|---|---|---|---|---|---|---|---|---|
| decisions-fen | 200 | 0 | 25.4 | 15.6 | 8% | 36% | 38% | 0.19 | 0.39 | 0 | 0.00005 |
| decisions-rich | 200 | 0 | 19.5 | 6.4 | 16% | 46% | 26% | 0.20 | 0.44 | 0 | 0.00007 |
| human | 200 | 0 | 4.8 | 0.4 | 35% | 75% | 3% | | | | |
| random | 596 | 0 | 22.1 | 11.2 | 5% | 36% | 32% | | | | |

## First 30 positions, every rung

| Rung | n | Errors | Mean loss | Median loss | Best move | Good (<5) | Blunder (>=30) | p50 s | p90 s | Reasoning tok | $/call |
|---|---|---|---|---|---|---|---|---|---|---|---|
| decisions-rich | 30 | 0 | 17.8 | 5.2 | 10% | 50% | 30% | 0.19 | 0.34 | 0 | 0.00007 |
| responses-none | 30 | 0 | 20.5 | 3.5 | 20% | 57% | 33% | 1.43 | 1.70 | 0 | 0.00007 |
| responses-low | 30 | 0 | 12.5 | 1.3 | 30% | 70% | 10% | 10.76 | 14.59 | 759 | 0.00045 |
| responses-medium | 30 | 0 | 7.6 | 0.3 | 37% | 77% | 7% | 28.31 | 77.39 | 3081 | 0.00161 |
| responses-high | 30 | 0 | 4.5 | 0.3 | 33% | 83% | 7% | 95.07 | 185.53 | 9171 | 0.00465 |
| responses-xhigh | 9 | 1 | 1.2 | 0.0 | 22% | 100% | 0% | 162.76 | 252.87 | 12540 | 0.00634 |
| human | 30 | 0 | 2.4 | 0.0 | 47% | 83% | 0% | | | | |
| random | 88 | 0 | 21.1 | 9.0 | 6% | 41% | 32% | | | | |

## Calibration of `decisions-fen` top-move probability

| p_top bin | n | Mean p_top | Is best move | Good (<5) |
|---|---|---|---|---|
| 0.0 to 0.2 | 168 | 0.11 | 5% | 29% |
| 0.2 to 0.4 | 21 | 0.27 | 19% | 71% |
| 0.4 to 0.6 | 8 | 0.49 | 12% | 62% |
| 0.6 to 0.8 | 2 | 0.70 | 0% | 100% |
| 0.8 to 1.0 | 1 | 0.84 | 100% | 100% |

Spearman correlation of p_top with loss: -0.29 (negative means confident moves lose less).
Spearman correlation of the sharpness score with Stockfish's best-vs-second gap: +0.04.

## Calibration of `decisions-rich` top-move probability

| p_top bin | n | Mean p_top | Is best move | Good (<5) |
|---|---|---|---|---|
| 0.0 to 0.2 | 144 | 0.11 | 12% | 39% |
| 0.2 to 0.4 | 34 | 0.26 | 15% | 47% |
| 0.4 to 0.6 | 13 | 0.48 | 54% | 92% |
| 0.6 to 0.8 | 5 | 0.69 | 20% | 80% |
| 0.8 to 1.0 | 4 | 0.85 | 25% | 100% |

Spearman correlation of p_top with loss: -0.26 (negative means confident moves lose less).
Spearman correlation of the sharpness score with Stockfish's best-vs-second gap: +0.14.

Total API spend recorded: $0.3119 over 700 logged calls, 1 with errors.
Raw logged spend, including superseded retries: $0.3119.
