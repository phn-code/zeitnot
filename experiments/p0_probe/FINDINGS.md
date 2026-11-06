# P0 findings

Probe run on 2026-11-06 against `gpt-6-luna`. Total API spend about $0.37. These scripts are throwaway instruments. The decisions they support are in [design.md](../../docs/design.md).

## Method

- **Positions.** 200 positions from rated Lichess blitz games in the January 2013 open database, both players rated 1200 to 2200. One position per game, at a random ply from 12 to 100.
- **Grading.** Stockfish 19 at depth 16 grades every move as the drop in win probability, in percentage points, against its own best move. It uses the Lichess win-probability formula. "Good" means a loss under 5 and "blunder" means a loss of 30 or more.
- **Latency.** Wall clock per call from this machine, in one session, after warm-up calls. Up to 6 calls of each kind ran in flight.
- **Prompt.** Responses calls receive the FEN, a text diagram, the move list and the legal moves. Structured output restricts the answer to the legal moves. Decisions calls get the same text plus one `choice` question over the legal moves.

`probe.py` runs the main measurement. `fast_tier.py` compares service tiers, `signals.py` scores escalation signals, and `filter_test.py` tests the safe-move filter. Raw records are in `out/`.

## Results

### Quality rises with effort, and so does latency

On the 30 positions that ran every effort level:

| Rung | Mean loss | Good | Blunder | Median latency | Reasoning tokens | Cost per call |
|---|---|---|---|---|---|---|
| Decisions | 17.8 | 50% | 30% | 0.19 s | 0 | $0.00007 |
| Responses `none` | 20.5 | 57% | 33% | 1.43 s | 0 | $0.00007 |
| Responses `low` | 12.5 | 70% | 10% | 10.8 s | 759 | $0.00045 |
| Responses `medium` | 7.6 | 77% | 7% | 28.3 s | 3,081 | $0.0016 |
| Responses `high` | 4.5 | 83% | 7% | 95.1 s | 9,171 | $0.0047 |
| Responses `xhigh` (9 positions) | 1.2 | 100% | 0% | 163 s | 12,540 | $0.0063 |
| Human move from the game | 2.4 | 83% | 0% | | | |

The step from `none` to `low` is clear: 8.1 points, with a 95% bootstrap interval of [1.7, 14.9]. The steps from `low` to `medium` and from `medium` to `high` point the same way, but their intervals include zero at 30 positions.

### Luna's token rate limits latency

Luna generates about 70 to 90 reasoning tokens per second on the default tier. Effort controls reasoning length only loosely. One `medium` call used 1.6k tokens and another used 10k. On the fast tier (`service_tier="fast"`, double price), the same 10 positions gave these results:

| Effort and tier | Median latency | Range | Tokens per second |
|---|---|---|---|
| `low`, default | 11.6 s | 7.5 to 14.0 s | 81 |
| `low`, fast | 6.3 s | 3.5 to 9.8 s | 116 |
| `medium`, fast | 13.6 s | 2.4 to 70.3 s | 137 |

A 3+2 game allows about 6.5 s per move and 5+3 about 10.5 s. On the default tier only Decisions and `none` fit a 3+2 move. On the fast tier, `low` fits about half the time.

### The Decisions API is fast but plays no better than effort `none`

Over 200 positions, Decisions with the rich prompt lost 19.5 points per move against 22.1 for a random legal move. With FEN alone it lost 25.4, worse than random. Its probabilities are spread thin. 72% of its top choices had a probability under 0.2.

- The top-move probability carries a weak signal. The Spearman correlation with loss is −0.26. In the 22 positions where the top probability was 0.4 or more, 80 to 100% of the picks were good moves.
- The `score` question about how sharp the position is shows no relation to Stockfish's gap between the best and second-best move (Spearman +0.14). Its AUC for predicting a mistake is 0.55.
- The top 10 Decisions moves contain a good move 88% of the time, and the top 5 contain one 74% of the time.

### A one-move rules check predicts fast-rung mistakes better than the model's own signals

Effort `none` made a mistake (loss of 20 or more) in 40% of the 200 positions. For each signal, the AUC for predicting such a mistake is below. 0.5 means useless and 1.0 means perfect.

| Signal | AUC |
|---|---|
| The moved piece is now attacked and undefended, or attacked by a cheaper piece | 0.76 |
| The number of my pieces in that state went up | 0.74 |
| Number of legal moves | 0.71 |
| Number of captures available | 0.68 |
| Decisions top-move probability is low | 0.67 |
| Decisions disagrees with `none` | 0.58 |
| Decisions sharpness score | 0.55 |
| In check | 0.46 |

### Offering only safe moves halves the fast rungs' loss

The filter removes every move that leaves the moved piece attacked and undefended, or attacked by a cheaper piece, unless the move captured something at least as valuable. It keeps a median of 24 of 34 legal moves. Luna then chooses among the remaining moves.

| Rung, 200 positions | Mean loss | Median loss | Good | Blunder | Latency |
|---|---|---|---|---|---|
| `none` | 21.1 | 6.1 | 47% | 30% | 1.4 s |
| `none` with filter | 10.0 | 1.9 | 66% | 12% | 1.3 s |
| Decisions | 19.5 | 6.4 | 46% | 26% | 0.2 s |
| Decisions with filter | 11.4 | 2.4 | 60% | 13% | 0.2 s |
| Human move from the game | 4.8 | 0.4 | 75% | 3% | |

The filter cuts the loss of `none` by 11.1 points, with a 95% interval of [7.7, 14.6]. On the 30-position subset, filtered `none` (7.8) and `medium` (7.6) show no measurable difference, with an interval of [−9.8, +10.1]. Filtered `none` answers about 20 times faster. The Decisions filter needs no extra call, because the harness picks the most probable safe move from the distribution it already has.

### Gemini 3.6 Flash at medium thinking takes about 11 s per move

`gemini_timing.py` streamed Gemini Flash at `thinking_level="medium"` on positions p000 to p009 on the free tier. Only Gemini 3.6 Flash completed any calls. It finished 6 of 10, and the other 4 returned "503 high demand". Gemini 3.7 Flash and 3.8 Flash returned only 503 errors, then hit the free quota.

| Measure, 6 moves | Value |
|---|---|
| Seconds per move | median 10.9, range 9.5 to 26.9 |
| First thinking summary appears after | 2.0 to 4.5 s |
| Thinking tokens per move | median 1,424, range 1,274 to 4,352 |
| Effective tokens per second, including time to first token | 122 to 162 |
| Mean loss on these 6 positions | 0.2 |
| Cost per move at paid introductory prices | about $0.008 |

On a billed key the same 10 positions ran every level with no errors:

| Level | p50 s | p90 s | Thinking tokens, median | Mean loss | Cost per move |
|---|---|---|---|---|---|
| `minimal` | 1.4 | 1.8 | 0 | 2.1 | $0.0003 |
| `low` | 4.2 | 7.3 | 470 | 1.0 | $0.0026 |
| `medium` | 12.1 | 26.9 | 1,804 | 2.3 | $0.0091 |

These 10 positions are mostly easy, since Luna's filtered `none` also averaged a loss of 1.2 on them, so the quality column does not separate the levels. `zeitnot/config.py` uses the p50 column as its latency table.

A pre-measurement estimate from ChessBench's cost data said 5 to 7 s. It assumed about 1,000 thinking tokens at about 200 tokens per second, and both assumptions were wrong. At 11 s per move, medium thinking on every move fits 10+5 and slower, is borderline at 5+3, and does not fit 3+2. The free tier failed 4 of 10 calls on 3.6 Flash and every call on 3.7 and 3.8 Flash, so it cannot support play.

## Caveats

- Each effort comparison rests on 30 positions. Treat the order of `low`, `medium` and `high` as likely, not proven.
- Latency comes from one session on one network. Provider load changes it.
- The positions come from 2013 amateur games, not from the agent's own games.
- The safe-move filter is hand-written chess knowledge at the level of the rules. It runs no search and no evaluation, so it fits the purity contract. Results always report it separately.
