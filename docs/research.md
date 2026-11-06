# Prior art for a clock-aware LLM chess player

Survey date 2026-11-06. This file records what already exists, so the project builds on it instead of repeating it. The design that follows from it is in [design.md](design.md).

Each entry was opened or matched in a search result during the survey unless it is marked "unverified". Ratings from different benchmarks use different scales: an internal Bradley-Terry pool, Glicko against Maia, Elo anchored to Komodo Dragon, and Lichess Classical. Never compare ratings across rows of different benchmarks.

## What the survey found

1. We found no evaluation of LLMs playing chess under a real clock. Kaggle Game Arena, LLM Chess, ChessArena and ChessBench all give unlimited time or a per-move cap of minutes. ChessArena's "Blitz" mode is an output format, not a clock.
2. We found no system that combines model routing with clock-based time allocation in any game.
3. Parallel pondering on predicted opponent moves with LLMs is published (Speculative Actions, 2025). It reports wall-clock savings for a whole game between two slow agents. It does not measure savings on the player's own clock, Elo, or play against fast opponents.
4. Chess engines solved time management decades ago, and the rules are readable in source. Every engine signal has an LLM analogue that nobody has wired up.
5. Generic reflective memory (Reflexion, ExpeL) gave small or negative gains in competitive games. Self-improvement that targets a numeric policy looks more promising than self-improvement that targets prompts.
6. Two typed decision models launched in September and October 2026: TypeSafe's Jev and OpenAI's Decisions API. Neither has published calibration metrics.

## LLM chess benchmarks and harnesses

| Project | Harness design | Rating method and results | Clock |
|---|---|---|---|
| [Kaggle Game Arena](https://github.com/google-deepmind/game_arena), [report](https://arxiv.org/html/2609.31473) (DeepMind and Kaggle, 2025 to 2026) | FEN plus full PGN each turn, answer as SAN. No legal-move list. Three "rethink" retries on an illegal move. Optional majority voting. | Internal pool with DeepSeek V3.2 fixed at 0. Gemini 3 Pro Preview 1325, o3 1009, GPT-5.2 933, Claude Opus 4.5 236. | None |
| [LLM Chess](https://maxim-saplin.github.io/llm_chess), [paper](https://arxiv.org/html/2512.01992v1) (Saplin et al., 2025) | Tool loop with `get_current_board`, `get_legal_moves`, `make_move`. At most 10 turns and 3 mistakes per move. | Elo anchored to Komodo Dragon levels. o3 (low) about 758. o3 (low) used 1927 tokens per move and $8.17 per game. | 10-minute client timeout per move |
| [ChessArena](https://arxiv.org/html/2509.24239) (Liu et al., ACL 2026) | Output modes named Bullet, Blitz, Standard and Blindfold. 5 retries, then forfeit. | Glicko. Maia-1100 2220, o3 1948. No LLM beat Maia-1100. A legal-move list raised first-try legality from 71.6% to 97.5% for GPT-4.1. | None |
| [ChessBench](https://github.com/lightnesscaster/Chess-LLM-Benchmark), [leaderboard](https://chessbenchllm.onrender.com/) (Johnston) | FEN plus ASCII board, answer in UCI. One retry. | Glicko-2 anchored to Lichess Classical bots. gemini-3.1-pro high 2144 at $3.95 per game, medium 2028 at $1.17. gemini-4-argon medium 1971 at $0.097. | None |
| [llm-chess-puzzles](https://github.com/kagisearch/llm-chess-puzzles) (Kagi, 2024) | 1000 Lichess puzzles as FEN. | GPT-4-turbo solved 22.9%, with 16.3% illegal moves. | None |
| [chess-llm](https://github.com/carlini/chess-llm), [post](https://nicholas.carlini.com/writing/2023/chess-llm.html) (Carlini, 2023) | gpt-3.5-turbo-instruct completes a PGN. Wrapped as a UCI engine and run as a Lichess bot. | No invalid move in a few thousand moves. | Lichess clock, with no time management |

ChessBench is the best existing anchored baseline. Its rows show the price of reasoning effort. gemini-3.1-pro at high effort gains 116 points over medium effort for 3.4 times the cost per game.

## Representation and prompting

- [dynomight (2024)](https://dynomight.net/more-chess) found that chat formatting hurts. Making the model repeat the game so far, plus three short examples, raised gpt-4o to about 1540. Giving the legal-move list hurt in those tests.
- ChessArena found the opposite for legality. The legal-move list raised first-try legality from about 70% to over 97%. The two results conflict, so test both.
- Game Arena sends FEN and PGN together, because some models read movetext more reliably than FEN.
- A rules-only verifier in the loop ([arXiv 2605.17565](https://arxiv.org/abs/2605.17565)) raised move validity from 19.3% to 95.3% for a 3B model.
- Retry counts per game differ by more than 25 times between model families in Game Arena. The latency model has to include retries.
- [Karvonen (2024)](https://adamkarvonen.github.io/machine_learning/2024/01/03/chess-world-models.html) showed that a 50M PGN-trained model holds a linear board representation. That result is background only.

## Trained chess models

These choose moves with trained networks, so they break the project's no-engine rule as move choosers. They are useful as opponents, offline graders, or design templates.

- [Grandmaster-Level Chess Without Search](https://arxiv.org/abs/2402.04494) (DeepMind, 2024). A 270M transformer distilled from Stockfish reaches 2895 Lichess blitz.
- [External and Internal Planning with Language Models](https://arxiv.org/abs/2412.12119) (DeepMind, ICML 2025). A trained model that searches inside its own output.
- [ALLIE](https://arxiv.org/abs/2410.03893) (ICLR 2025). It sets its MCTS rollout count as `N = floor(c * t(m))`, where `t(m)` is the predicted human think time (Pearson r 0.697). This is the closest prior art to "think in proportion to difficulty", inside a trained model.
- [ChessMimic](https://arxiv.org/abs/2606.04473) (2026). Per-rating-band models predict a human's move, think time (mean error 4.1 s) and result (AUC 0.78). Weights are released.
- [Maia-2](https://arxiv.org/abs/2409.20553) (NeurIPS 2024). Human-move prediction across skill levels. Maia-1100 is the bar that no LLM cleared in ChessArena.
- [RL post-training on chess](https://arxiv.org/abs/2507.00726) (2025) plateaus far below expert level. The authors blame weak pretrained chess knowledge.

## Adaptive test-time compute and metareasoning

| Work | Idea | Relevance |
|---|---|---|
| [Snell et al.](https://arxiv.org/abs/2408.03314) (ICLR 2025) | The best test-time strategy depends on difficulty. Compute-optimal allocation is more than 4 times as efficient as best-of-N. | Estimate difficulty per position before picking a strategy. |
| [Learning How Hard to Think](https://arxiv.org/abs/2410.04707) (ICLR 2025) | A probe predicts the marginal value of more compute. Saves up to 50% compute at equal quality. | Template for the "think harder?" predictor. It needs hidden states, so we use proxy features. |
| [Latency and Token-Aware Test-Time Compute](https://arxiv.org/abs/2509.09864) (2025) | Picks strategy and budget per query, counting wall-clock latency. | The only LLM allocation paper we found that counts latency. |
| [Rational Metareasoning for LLMs](https://arxiv.org/abs/2410.05563) (2024) | Reward is utility minus a cost of reasoning. Cuts tokens 20 to 42% at equal accuracy. | Training method, so we borrow only the value-of-computation framing. |
| [Selecting Computations](https://arxiv.org/abs/1207.5879) (UAI 2012) | Metalevel control as Bayesian selection. Simple regret is the right target because only the final move pays. | Formal basis for the controller. |
| [Time spent thinking in online chess](https://pmc.ncbi.nlm.nih.gov/articles/PMC12553403) (Cognitive Science 2025) | Across more than 12M games, humans think longer where computation is worth more, and stronger players do this more. | A human benchmark for our agent's time use. |
| [On the Clock](https://neurips.cc/virtual/2026/170689) (NeurIPS 2026), [Timely Machine](https://arxiv.org/abs/2601.16486) (2026) | Agents ignore stated time budgets. Showing a clock fixes adherence but agents do not turn extra time into better results. | The open question this project answers for chess with harness code alone. |
| [AgileThinker](https://arxiv.org/abs/2511.04898) (ICLR 2026) | A reactive loop and a planning loop run at once in real-time games. It beats either loop alone as time pressure rises. | Closest architecture to our anytime executor. |
| [SwiftSage](https://arxiv.org/abs/2305.17390), [System-1.x](https://arxiv.org/abs/2407.14414), [Talker-Reasoner](https://arxiv.org/abs/2410.08328) | Fast and slow systems. SwiftSage escalates on hand rules. System-1.x learns which subgoals are hard. | Precedents for a fast policy with a gated slow path. |
| [Adaptive-Consistency](https://arxiv.org/abs/2305.11860) (EMNLP 2023), [DeepConf](https://arxiv.org/abs/2508.15260) (2025) | Stop sampling once agreement is high enough. Adaptive-Consistency cuts samples up to 7.9 times. DeepConf needs logprobs. | Vote entropy over candidate moves is a free uncertainty signal. |
| AutoMix, RouteLLM, RouterBench, Router-R1 | Learned routers between cheap and expensive models. | All optimise dollars. None routes against a deadline. |
| [FPX](https://arxiv.org/abs/2505.19481) (NeurIPS 2025) | Picks model size and quantization from real-time demands in Street Fighter. Up to 80% higher win rate. | The nearest latency-aware model routing in a game. |

## Engine time management

Stockfish computes an optimum and a maximum time per move from the clock in `timeman.cpp`. In `search.cpp`, after each iteration, it stops when elapsed time exceeds `min(optimum * fallingEval * reduction * bestMoveInstability * highBestMoveEffort, maximum)`. We checked these constants against the current source:

- `fallingEval = clamp((11.48 + 2.30 * (prevAvgScore - best) + ...) / 100, 0.576, 1.728)`. The engine thinks longer when the evaluation drops.
- `bestMoveInstability = 1.077 + 2.229 * totBestMoveChanges / threads`. It thinks longer when the best move keeps changing.
- `highBestMoveEffort` interpolates from 0.969 down to 0.714 as the share of nodes spent on the best move rises. It thinks less when one move dominates.
- `timeReduction` grows with how many iterations the best move has stayed the same.
- With one legal move the search caps at 500 ms.
- The optimum shrinks when the engine is behind on the clock relative to the opponent (the agent read this in `timeman.cpp`, which we did not check).

Lc0 stops when no other move can overtake the most-visited move with the visits that remain ([Lc0 time management](https://lczero.org/blog/2018/09/time-management)). [Baier and Winands](https://dke.maastrichtuniversity.nl/m.winands/documents/time_management_for_monte_carlo_tree_search.pdf) (IEEE TCIAIG 2016) compared stopping and extension rules for MCTS. The STOP rule raised win rate against GNU Go from 25.5% to 39.1% in 13x13 Go and was best in all five games tested.

| Engine signal | LLM analogue |
|---|---|
| Best move changes between iterations | Fast samples disagree, or a slower rung overturns a faster rung |
| Node share of the best move | Vote share or probability margin of the top move |
| Falling evaluation | The opponent's last move created a threat (checks, attacked undefended pieces), or the policy's confidence dropped |
| One legal move | Same rule. Move at once. |
| Behind on the clock | Same rule. Scale the budget down. |
| STOP: remaining work cannot change the answer | Stop sampling once the remaining samples cannot flip the plurality |

## Speculation and pondering

- [Speculative Actions](https://arxiv.org/html/2510.04371v2) (Columbia, ICLR 2026). Chess on TextArena. GPT-5 at high effort is the actor and GPT-5 at low effort predicts the opponent's next move. With 3 predictions, any-of-3 accuracy was 54.7% and average time saved was 19.5%. Their analysis caps the saving at 50%. The paper reports no clock, no Elo and no token overhead for chess.
- [Interactive Speculative Planning](https://arxiv.org/abs/2410.00079) (ICLR 2025). A fast agent drafts and a slow agent verifies, as in speculative decoding.
- [Dynamic Speculative Agent Planning](https://arxiv.org/abs/2509.01920) (2025). RL tunes how far ahead to speculate. Same latency at 30% lower cost.
- Engine ponder-hit rates are about 57% between different engines ([TalkChess data](https://talkchess.com/viewtopic.php?p=696910)).

## Memory, caching and self-improvement

- [Agentic Plan Caching](https://arxiv.org/abs/2506.14852) (NeurIPS 2025). 50% lower cost and 27% lower latency at about 97% of quality.
- [vCache](https://arxiv.org/abs/2502.03771) (ICLR 2026). A semantic cache with learned per-entry thresholds. In chess, a "similar" position can need the opposite move, so we use exact position keys only.
- [Don't Break the Cache](https://arxiv.org/abs/2601.06007) (2026). Provider prompt caching improved time to first token by 13 to 31% across OpenAI, Anthropic and Google.
- Reflexion, [ExpeL](https://arxiv.org/abs/2308.10144), Voyager, [Dynamic Cheatsheet](https://arxiv.org/abs/2504.07952), [ACE](https://arxiv.org/abs/2510.04618) and [GEPA](https://arxiv.org/abs/2507.19457) improve agents through text memory or prompt evolution. We found no chess result for any of them.
- Results from games are weaker. In [MemoPilot](https://arxiv.org/abs/2606.08656)'s baselines, Reflexion and ExpeL gave small or negative gains in rock-paper-scissors and Limit Hold'em. In Catan, [prompt and code evolution](https://arxiv.org/abs/2506.04651) helped some models and gave Mistral Large about 3%.
- We found no LLM player that builds its own opening book from its own analysis. Computer Othello programs have updated their books after each game for decades.

## Decision models and learned control

- Jev, by TypeSafe AI ([LangChain post](https://www.langchain.com/blog/building-a-harness-with-jev), [DataCamp overview](https://www.datacamp.com/blog/system-one-models-jev)). It takes a state plus typed questions (choice, score, yes or no) and returns probabilities. Vendor-reported latency is 70 to 500 ms. Access is early-access behind a waitlist. There are no published calibration metrics and no independent reproduction.
- OpenAI Decisions API ([reference](https://developers.openai.com/api/reference/resources/decisions/methods/create), [beta news](https://letsdatascience.com/news/openai-opens-decisions-api-public-beta-b69a1e40)). `POST /v1/decisions` with `gpt-6-luna` only. Question types are `predicate`, `choice` (2 to 255 options) and `score` (2 to 10 levels), with 1 to 400 questions per request. Public beta since 2026-10-06, input $0.10 per million tokens. Secondary sources quote about 150 ms against 1.6 s for a normal call. OpenAI tells users to pick thresholds from their own labelled data and publishes no calibration numbers.
- Azure AI Personalizer, the commercial descendant of Microsoft's [Decision Service](https://arxiv.org/abs/1606.03966), was [retired on 2026-10-01](https://azure.microsoft.com/en-ca/updates/ai-services-personalizer-will-be-retired-on-1-october-2026/). Vowpal Wabbit still ships releases.
- [Open Bandit Pipeline](https://zr-obp.readthedocs.io/) provides off-policy estimators (IPS, DR) for learning from logged decisions.

## Infrastructure facts

- Cerebras serves gpt-oss-120b at about 3,000 tokens per second, against about 500 on Groq ([Cerebras](https://www.cerebras.ai/blog/cerebras-launches-openai-s-gpt-oss-120b-at-a-blistering-3-000-tokens-sec), vendor figures).
- Claude Haiku 5.5 costs $0.10 / $0.50 per million input and output tokens and can run with thinking off. Claude Opus 5.5 costs $4 / $20 and has a fast mode with up to 2.5 times the output speed at double the price. Anthropic calls `effort` a behavioural signal, not a token budget. From API version 2026-10-21, Messages requests reject `temperature`.
- Stockfish `UCI_Elo` spans 1320 to 3190 with `UCI_LimitStrength`. Sources disagree on whether it was calibrated at 60+0.6 or 120+1 ([UCI options](https://github.com/official-stockfish/Stockfish/wiki/UCI-&-Commands)).
- [fastchess](https://github.com/Disservin/fastchess) runs UCI engine tournaments with SPRT and has a UCI compliance checker.
- The Lichess Bot API allows every time control except UltraBullet. Bot accounts play challenges, not the human pools ([Lichess blog](https://lichess.org/@/lichess/blog/welcome-lichess-boards/XlRW5REA)).

## Open gaps this project can claim

1. LLM chess results under a real clock with increment, with time forfeits counted.
2. A time manager for an LLM player that ports engine signals to LLM signals and allocates a sudden-death clock across a ladder of models and effort levels.
3. Pondering measured as own-clock savings and as Elo per extra token.
4. A self-prepared opening book built from the agent's own offline analysis.
5. A calibration study of the 2026 decision models in a domain with exact ground truth.
6. A public dataset of every model and effort level run on the same positions, with latency, cost and graded quality. Anyone can test allocation policies on it without spending API money.
