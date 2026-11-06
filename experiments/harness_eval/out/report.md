# Harness eval

40 positions (40 critical, 0 quiet). Loss is the drop in win probability against Stockfish 19's best move at depth 16. Blunder means a loss of 30 or more. Intervals are 95% bootstrap intervals of the mean. Seconds are wall clock per call, with the configured number of calls in flight. The search arm runs on critical positions only, so compare it with the other arms in the critical table; its seconds cover all of its calls, up to seven, three at a time.

## All positions

| Engine | Arm | n | Mean loss (95% CI) | Blunders | vs v1, paired (95% CI) | Mean s | Total $ | $/move |
|---|---|---|---|---|---|---|---|---|
| claude | v1 | 40 | 11.3 (6.1 to 17.7) | 6 |  | 10.8 | 0.0000 | 0.00000 |
| claude | v2 | 40 | 10.2 (5.1 to 16.2) | 6 | -1.1 (-6.1 to +3.9) | 7.7 | 0.0000 | 0.00000 |
| claude | v2+verify | 40 | 10.2 (5.1 to 16.2) | 6 | -1.1 (-6.1 to +3.9) | 7.9 | 0.0000 | 0.00000 |
| claude | opinions | 40 | 8.4 (4.1 to 13.7) | 4 | -2.9 (-6.3 to -0.2) | 16.9 | 0.0000 | 0.00000 |

## Critical positions

| Engine | Arm | n | Mean loss (95% CI) | Blunders | vs v1, paired (95% CI) | Mean s | Total $ | $/move |
|---|---|---|---|---|---|---|---|---|
| claude | v1 | 40 | 11.3 (6.1 to 17.7) | 6 |  | 10.8 | 0.0000 | 0.00000 |
| claude | v2 | 40 | 10.2 (5.1 to 16.2) | 6 | -1.1 (-6.1 to +3.9) | 7.7 | 0.0000 | 0.00000 |
| claude | v2+verify | 40 | 10.2 (5.1 to 16.2) | 6 | -1.1 (-6.1 to +3.9) | 7.9 | 0.0000 | 0.00000 |
| claude | opinions | 40 | 8.4 (4.1 to 13.7) | 4 | -2.9 (-6.3 to -0.2) | 16.9 | 0.0000 | 0.00000 |

## Quiet positions

| Engine | Arm | n | Mean loss (95% CI) | Blunders | vs v1, paired (95% CI) | Mean s | Total $ | $/move |
|---|---|---|---|---|---|---|---|---|

## Opinions arm, claude

- Second opinions recorded: 40 of 40 critical positions.
- Disagreement: 18 of 40 (45%).
- Escalations recorded: 18 of 18 splits; the pick landed on 18.
- Final move differs from v2 on 10 of 40 positions.
- Loss against v2, paired, opinions minus v2: -1.8 (-5.8 to +0.9) over 40 positions (negative is better).
- Mean seconds: v2 7.7, opinions 16.9 (main and second in parallel, then the escalation).
- The second, reply and pick calls use the Phase C prompt with expected_reply; the main answer is the v2 record.

claude: the rules check failed on 1 v2 answers; verify changed the move 0 times.
