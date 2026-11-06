# Harness v2 proposal

Proposal dated 2026-11-06. This doc proposes the harness that should make Gemini Flash play its best chess in a real clocked game, at a price a hobby budget can carry. It combines four research passes over 2024 to 2026 work with this project's own measurements in [FINDINGS.md](../experiments/p0_probe/FINDINGS.md). The app it changes is described in [app.md](app.md).

## Why v1 needs replacing

v1 is a clock manager with a safety net. It picks a thinking level from the clock alone and sends the same stateless prompt every move. In the one real 10+5 game it never flagged, but it spent its effort in the wrong places.

- Think time and position criticality were negatively correlated (Spearman −0.38, using Stockfish's best-versus-second gap as criticality).
- 96% of the spend ($0.42 of $0.44) went to positions where the top moves were within 5 points.
- The game was lost to three mistakes in a row during Gemini's own attack. Each got 10 to 13 s while nearly 10 minutes stayed on the clock.
- The model never saw which of its pieces were attacked, what its last plan was, or how much time it had.

## What the evidence says

### Put rules facts in one prompt, not in tools or a conversation

| Finding | Source | Effect |
|---|---|---|
| A legal-move list in the prompt | [ChessArena](https://arxiv.org/html/2509.24239) | Gemini 2.5 Pro rated 1819 with the list against 1616 without it |
| Facts in the prompt beat a tool loop that fetches them | [LLM Chess](https://arxiv.org/html/2512.01992v1) | o4-mini rose from 73.3 to 96.7 win-minus-loss %, and Grok 3 Mini from 61.7 to 71.7 |
| The move history in the prompt | LLM Chess | Blunders per ply fell from 11.2% to 1.6% for o4-mini and from 9.1% to 3.5% for Grok 3 Mini |
| A piece list beside the board | [ChessQA](https://arxiv.org/html/2510.23948v1) | Gemini 2.5 Flash rose from 24.1% to 31.0% |
| Thinking on | ChessQA | +14.7 points on average |
| A growing multi-turn transcript | [Lost in conversation](https://arxiv.org/abs/2505.06120), ChessArena blindfold mode | Multi-turn performance fell 39% on average, and blindfold play without a board fell sharply |
| Long prompts and stale notes | [Context Rot](https://www.trychroma.com/research/context-rot), [Gemini 2.5 report](https://arxiv.org/html/2507.06261) | Focused ~300-token prompts beat long ones, and the Pokémon agent fixated on stale goals |
| Carried memory | [lmgame-Bench](https://arxiv.org/html/2505.15146v2) | Large gains for weak models (Llama-4-Maverick 44.6 to 98.1 on 2048), none for thinking models |

The conclusion is a fresh, compact, structured prompt every move. It carries the full state, rules facts computed in code, and one short note from the previous move. It never carries a growing transcript.

No one has published an ablation of attack maps, threat lists or hanging-piece tags for move choice. Our own data is the best evidence here. A one-move rules check predicted mistakes with AUC 0.76, and offering only safe moves halved the fast mode's loss per move.

### Do the lookahead in code, and verify with facts, not self-critique

- **Models write lookahead they then ignore.** In [Extracting Search Trees](https://arxiv.org/html/2605.06840v3), content deeper than one ply changed the chosen move only 3.7% of the time, against 1.7% for a control. [Atomic Chess](https://lacuna.tiptreesystems.com/work/atomic-chess-reveals-compositional-reasoning-failures-in-llms/wrk_b387e67f241306ba7545f62646a0327b) names the same failure "unpropagated refutation". The model's own reasoning says a move is bad, then it plays the move anyway.
- **Bare LLMs are poor judges of their own moves.** In [Hallucinations on the Board](https://arxiv.org/html/2608.04240), Gemini 3.1 Pro scored F1 22.2 at spotting mistakes without tools.
- **A claim gets corrected when it arrives as outside input.** [The Self-Correction Illusion](https://arxiv.org/html/2606.05976v1) found that presenting the same claim as user, tool or memory content raised the correction rate by 23 to 93 points.
- **Generic orchestration buys little.** In [When Does LLM Orchestration Pay Off?](https://arxiv.org/html/2608.00685), Best-of-N averaged 33.0% against 31.0% for tuned chain of thought, at 2 to 3 times the tokens, and the gain depended on the model. Majority voting of 20 samples solved 10% of depth-3 mates in [Guo et al.](https://arxiv.org/abs/2403.05632).
- **Search works only with a trained value function.** It added about 286 Elo in [MAV](https://arxiv.org/html/2412.12119) with a value function trained on Stockfish targets. No chess result shows it working with a prompted value function. [PokéChamp](https://arxiv.org/html/2503.04094v1)'s LLM minimax lost about a third of its games on time.

The conclusion is that the harness does the lookahead itself. It plays the chosen move on the board, lists the opponent's checks and captures in code, and shows the result to a fresh call as another player's suggestion. It never asks the model to "double-check your move".

### Use the opponent's clock, cheaply

- **Pondering is worth most of a doubling of think time.** Engine data puts it at 71% to 77% of the value of doubling think time ([TalkChess](https://talkchess.com/viewtopic.php?p=708328)). An LLM keeps no search tree between moves, so only analysis carried as text survives a missed prediction.
- **Expect to guess the human's move about half the time.** Human-move predictors match the actual move about 55% of the time at top 1 and about 91% at top 5 ([Otter](https://arxiv.org/abs/2608.05206)). A 2023 completion model matched 53.7% in [ALLIE](https://arxiv.org/html/2410.03893). Gemini's own rate is unmeasured, and measuring it on our 200 positions costs about $0.06.

### Cheap confidence signals are rules signals

- **No cheap probability for each move.** Gemini 3.x returns no logprobs and only one candidate per call, according to user reports on Google's forum that Google's docs do not confirm.
- **The Decisions API is poorly calibrated.** It scored ECE 0.32 on ProofWriter ([anth.us](https://anth.us/articles/openai-decisions-api-preview/)), and our sharpness test gave AUC 0.55.
- **Rules checks already beat every model signal we measured.** They scored AUC 0.74 to 0.76.

### Cost structure

Gemini 3.6 Flash's implicit cache needs at least 4,096 tokens, and our prompt is under 1,000, so caching does not apply. Input costs $0.75 per million tokens, so 800 input tokens cost $0.0006 per call. Thinking dominates the bill. A `medium` call costs about 15 times a `minimal` call. Price control therefore comes from choosing the thinking level per move, not from trimming the prompt.

## The proposed harness

### Per-move context

One structured prompt, rebuilt from the board each move, with the question last, as Google's and Anthropic's prompting guides advise.

| Section | Contents | Tokens |
|---|---|---|
| System | Role, the output contract, and one line: "The prior note is a hypothesis from one move ago. Re-check it against the position." | 80 |
| `<game>` | Your colour, time control, move number | 20 |
| `<clock>` | Your time, the opponent's time, the increment | 20 |
| `<moves>` | The game in SAN | 150 on average |
| `<position>` | FEN and a piece list ("White: Kg1 Qd1 Rf1 ...") | 120 |
| `<facts>` | Checks and captures available to you. Your pieces under attack and undefended or attacked by a cheaper piece. What the opponent's last move attacked. Mate-in-one if one exists. All computed by python-chess. | 50 to 150 |
| `<prior_note>` | On a correct prediction, the last plan and expected reply. On a miss, a single fact line such as "Expected ...Nf6, opponent played ...Bxh7+ (capture, check). Prior plan discarded." | 60 |
| `<moves_allowed>` | Legal moves in SAN, each unsafe one tagged "hangs piece" | 150 |
| Task | "Based on the position above, choose your move and write a note of 40 words or less with your plan and the reply you expect." | 30 |

The output is JSON. `move` is restricted to the legal moves, and `note` is a string of 40 words or less. The ASCII diagram is dropped in favour of the piece list, because LLM Chess found no consistent gain from ASCII and ChessQA measured a gain from the piece list. Total input is about 700 to 900 tokens.

### Per-move flow

1. **Rules stage (0 s).** Forced move plays at once. Compute the facts block, the safe set and a criticality flag. The flag is set when the opponent's last move was a capture or a check, when any of your pieces is attacked, or when the safe set is much smaller than the legal set.
2. **Ponder hit (about 0 to 1.4 s).** If pondering already analysed this exact position and its move passes the rules check, play it.
3. **Think.** A quiet position thinks at `low`, about 4 s. A critical position thinks at `medium`, about 12 s, with a longer deadline drawn from the time quiet moves saved. The backup call at `minimal` with the safe filter runs in parallel, as in v1.
4. **Verify when flagged.** Run this when the chosen move fails the rules check (it hangs the moved piece or raises the count of hanging pieces). The harness plays the move on the board and lists the opponent's checks and captures in the resulting position. Then it asks a fresh `low` call: "Another player suggests Nxf7. After it, the opponent can play these moves: ... Keep it or choose another move." In the v1 loss, this fires on Nxf7.
5. **Deadline.** The harness owns the clock. At the deadline it cancels the stream and plays the best answer it holds. It never sends a "hurry up" message, which [cost up to 30% accuracy](https://arxiv.org/html/2510.11713v1) in tests.
6. **Note handling.** The harness stores the note. When the opponent replies, python-chess compares the actual move with the expected reply and writes either the note or the miss line into the next prompt.

### Pondering

While you think, the server runs the per-move pipeline on the position after the reply the model's note expected. If you play that reply, the answer is ready, often before the increment is even added. If you don't, the work is thrown away and the normal pipeline runs.

The browser starts the ponder with `POST /api/ponder` after each model move. The server caches results by position. Pondering is a setting, because it adds cost.

### The price dial

Three presets. The costs are estimates for a 40-move game, assuming 65% quiet moves, 30% critical, 5% forced, verification on a quarter of moves, and the measured per-call costs.

| Preset | Quiet | Critical | Verify | Ponder | Estimated cost per game |
|---|---|---|---|---|---|
| Economy | `minimal` with safe filter | `low` | yes | no | about $0.09 |
| Balanced (default) | `low` | `medium` | yes | no | about $0.23 |
| Strong | `low` | `medium` | yes | yes | about $0.34 |

v1 cost $0.44 for its 53-move game, or about $0.33 per 40 moves. Balanced is about 30% cheaper per move than v1 and moves its effort to the positions that decide games. Strong costs about the same as v1 but uses your thinking time as well.

## Deliberately left out

| Idea | Why not |
|---|---|
| Multi-turn chat or replaying earlier thoughts | Multi-turn performance fell 39% on average. Context poisoning was observed. Cost grows quadratically. |
| Tool calls during the move (`get_current_board`, `get_legal_moves`) | LLM Chess introduced these tools, and its own ablation found that putting the board and legal moves in the prompt with only a move action raised o4-mini from 73.3 to 96.7 win-minus-loss %. Each round trip also costs seconds, and a Codex round trip costs 5 to 8 s. |
| Illegal-move retries ("up to 3 rethinks") | The answer is constrained to the legal moves (Gemini's enum, Codex's `outputSchema`), so an illegal move cannot occur. Game Arena needs rethinks because it does not constrain output. A failed call falls back to the backup move instead of spending clock on a retry. |
| Board images | Text beat images by 15 to 29 points in [IsoBench](https://arxiv.org/abs/2404.01266) |
| Majority voting, debate, self-refine | Small model-dependent gains at 2 to 4 times the tokens |
| LLM-valued tree search | No chess evidence with a prompted value function, and it does not fit 15 s per move |
| "Check for blunders" instructions | Models write a refutation and play the move anyway. The check runs in code. |
| Decisions API, logprob confidence | Poorly calibrated, or not available on Gemini 3.x |
| Lessons written during a game | No evidence for thinking models. Offline lessons are a later experiment. |
| Opening book | Gemini already plays openings in 2 to 4 s for under $0.002 a move |

## Proving it beats v1

All offline arms run on the same positions and are graded by Stockfish, as in P0. The total is about $2 of the roughly $9.40 left.

1. **Ponder value, about $0.06.** Run `minimal` on the 200 P0 positions to see how often Gemini predicts the human's actual move. Below about 40%, pondering stays off.
2. **Context and verify, about $1.50.** Pick about 60 positions: 40 where natural moves fail, taken from positions where earlier fast rungs blundered, plus 20 quiet ones. Run three arms:
   - v1's prompt at `low`;
   - the v2 context at `low`;
   - the v2 context plus verify-on-flag.

   Run `medium` on the critical subset only. Report loss per move, blunders, seconds and cost per arm.
3. **Confirmation, about $0.50.** Play two 10+5 games against Stockfish at 1500 with the Balanced preset, using `experiments/app_smoke/real_game.py`.

## Build order

Each step ends in a check that a reviewer can rerun.

1. Facts module, v2 prompt builder and JSON output with the note. Covered by unit tests and eval step 2.
2. Triage, longer deadlines for critical moves, and verify-on-flag in `player.py`. Covered by fake-client tests and eval step 2.
3. The UI shows verification: "Re-checking Nxf7: after it, the opponent can play Bxf7 or Kxf7".
4. Pondering, with `/api/ponder`, a position cache and "expecting Nf3" in the mind panel, if eval step 1 supports it.
5. The price dial on the start screen.

## Codex or Claude Code instead of the Gemini API

Both CLIs on this machine can play a move, measured on 2026-11-06. Neither should become the app's default.

| Backend | Wall time per move | Notes |
|---|---|---|
| Claude Code headless (`claude -p`), Haiku or Sonnet | 3.6 s, of which 1.1 to 1.4 s is the model | About 2.3 s of CLI start-up per call. Anthropic's Agent SDK page says third-party products may not offer claude.ai login without approval, so this stays a personal option, not an app feature. |
| Codex (`codex exec`), gpt-6.1-sol at low effort | 7.6 to 7.9 s once trimmed, 9.3 s as installed | As installed, each call carried 20,562 input tokens of Codex's own agent context and started your MCP servers. With `model_instructions_file` replacing the built-in instructions, and MCP servers, plugins, skills, memories and 21 tool features turned off per call with `-c` and `--disable`, input fell to 4,946 tokens. The rest is Codex's sandbox and environment blocks and its core tool schemas. Latency barely moved, so most of the time is process start-up and the agent loop, not the model. It runs on your ChatGPT plan, which OpenAI documents for automation. ChessBench rates gpt-6.1-sol at about 1,390 to 1,490, below Gemini 3.6 Flash's 1,916. |
| Gemini 3.6 Flash API | 1.4 s `minimal`, 4.2 s `low`, 12.1 s `medium` | Strongest chess per second and per dollar measured so far |

Both CLIs cost no money per call but draw on subscription limits.

### Running Codex models with minimal overhead

The lowest-overhead route that stays inside OpenAI's documented tools is one persistent `codex app-server` per game, with a fresh ephemeral thread per move. Measured on Codex 0.163.0:

- **Process start is paid once.** The server is ready in 0.9 s, and `thread/start` takes 0.06 to 0.23 s.
- **`thread/start` takes `baseInstructions`, which replaces Codex's built-in instructions.** `turn/start` takes `effort`, `outputSchema` (the legal-move enum), `summary` (streamed reasoning summaries) and `serviceTierForTurn`. `turn/interrupt` cancels at the deadline.
- **These settings strip Codex's injected context.** `include_permissions_instructions=false`, `include_environment_context=false`, `include_apps_instructions=false`, `include_collaboration_mode_instructions=false`, `tools.update_plan.enabled=false`, `tools.experimental_request_user_input.enabled=false`, MCP servers off, skills and memories off, `project_doc_max_bytes=0`, and `--disable` on 21 tool features. Together they cut input from 20,562 to about 3,950 tokens per call.
- **About 3,950 tokens remain.** The leftover is a multi-agent role block of about 2,700 characters, which no documented setting removes. It appears to come from the server-side model catalogue. The rest is the output schema and the prompt. Overriding the catalogue with the undocumented `model_catalog_json` key is possible but fragile, and not worth the risk for about 3,000 free tokens.

Measured per-move latency at effort `low` on three positions:

| Model, tier | Easy | Medium | Complex |
|---|---|---|---|
| gpt-6.1-sol, standard | 8.5 s | 10.4 s | 54.8 s |
| gpt-6.1-sol, Fast ("2x speed, increased usage") | 5.8 s | 8.4 s | 34.1 s |
| gpt-6-astra, standard | 6.9 s | 21.1 s | 56.4 s |
| gpt-6-astra, Fast | 8.4 s | 12.6 s | 55.4 s |

Sol and Astra reason at about 20 to 35 tokens per second, and every call waits 5 to 8 s before reasoning starts. That fits 15+10 and slower. In 10+5 the deadline will cut many moves short, so a Codex engine needs a backup that answers in about 1 s, which only the Gemini API or a rules-only move provides.

## Final design for the build

This section is the build contract. It supersedes the sections above where they differ.

### Engines

An engine turns one prompt, a list of allowed moves and a thinking level into streamed thoughts and one answer. The harness is engine-agnostic.

| Engine | Model | Levels used | Cost | Role |
|---|---|---|---|---|
| `codex` | `gpt-6.1-sol` through one persistent `codex app-server` per server process, a fresh ephemeral thread per call, with the stripped config above | `low` (quiet), `medium` (critical) | $0, uses the ChatGPT plan | The engine for every call: main, backup, verify and ponder |
| `gemini` | `gemini-3.6-flash` | `minimal`, `low`, `medium` | about $0.0003, $0.0026 and $0.009 a call | Off unless `ZEITNOT_GEMINI=1`. Then it can be chosen as the engine. |
| `fake` | scripted | any | $0 | Tests and demos |

No move ever comes from rules or code. The one exception is a position with exactly one legal move. If the model has no answer when its clock runs out, it loses on time.

When the main call thinks above `low`, a backup call runs in parallel on the same engine at `low`, choosing only among safe moves. When the main call is already `low`, there is no backup call.

Each engine keeps a live latency table: p50 and p90 seconds per level, seeded from the measurements above and updated after every call. The time manager plans from this table, not from constants.

### One move

1. **Rules stage.** A forced move plays at once. Build the facts block, the safe set and the criticality flag.
2. **Ponder cache.** If pondering analysed this exact position and its move passes the rules check, play it and report `ponder_hit`.
3. **Plan.** The budget comes from the clock as in v1. A critical position gets twice the budget, capped at 30% of the clock. A quiet move gets the cheapest level whose p50 fits. A critical move gets the deepest level whose p50 fits, and its deadline extends to the larger of 2 times budget and that level's p90, still capped.
4. **Main and backup calls.** Both run in parallel. The main call streams thoughts to the browser.
5. **Rules check on the answer.** It passes when the move does not leave the moved piece hanging and does not raise the number of hanging pieces.
6. **Verify, when the check fails and the remaining budget covers one more call at the main engine's `low` p50.** The harness plays the move, lists the opponent's checks and captures in the resulting position, and asks a fresh call to keep the move or choose another. The second call sees the first move as "another player's suggestion". Emit a `verify` event with the outcome.
7. **Veto, when the check fails and there is no time to verify.** Play the backup move only if one exists and it passes the rules check, and emit a `note` saying which move was vetoed and why. Otherwise play the main model's move as it is.
8. **Deadline.** If a backup answer exists, interrupt the main call (`turn/interrupt` for Codex, cancel for Gemini) and play the backup. If none exists, keep waiting for the main answer until the browser's clock flags. Never send a "hurry up" message.
9. **Note.** The main engine answers JSON `{move, note}`. The harness stores the note. On the next move it compares the opponent's actual reply with the note's expected reply and writes either the note or the miss line into the prompt.

### Pondering

`POST /api/ponder` takes the same body as `/api/think` plus `expected_reply`. It returns at once and runs the one-move pipeline in the background on the position after the expected reply. The result is cached by FEN. A ponder run in progress for a position that `/api/think` now asks for is awaited, not restarted. A new `/api/ponder` or `/api/think` for a different position cancels stale ponder runs. Pondering is on by default for `codex`, since it costs nothing, and off by default for `gemini`.

### Protocol additions

- `plan` gains `engine`, `critical` and `reasons`.
- A new `verify` event has `proposed`, `opponent_replies` (SAN list), `verdict` (`keep` or `change`) and `final`.
- `move` gains `note`, `ponder_hit` and `vetoed` (the vetoed SAN, or null).
- `GET /api/status` gains `engines` (available engines and models) and `pondering`.

### Browser additions

- The start screen defaults to Codex Sol and 15+10. It says: "Codex models take about 5 to 60 s per move; the model can lose on time at fast controls." Gemini appears as a choice only when `ZEITNOT_GEMINI=1`.
- The mind panel shows `verify` as "Re-checking Nxf7: after it the opponent can play Bxf7, Kxf7", and a veto as a struck-through move.
- While you think, the panel shows "Expecting Nf3" from the note, and a ponder hit shows as an instant move with a small "prepared" tag.

### Evaluation

`experiments/harness_eval/` runs v1 and v2 on the same positions and grades them with Stockfish 19 at depth 16, as in P0.

- **Positions.** 60 in total. 40 are critical: P0 positions where any earlier fast rung blundered, plus the real game's positions from ply 14 to 22. 20 are quiet.
- **Arms.** Each arm runs Sol through Codex. Gemini `low` arms run only with `--gemini`:
  - v1 prompt;
  - v2 context;
  - v2 context plus verify.
- **Report.** Loss per move, blunders, seconds and cost per arm, with 95% bootstrap intervals.

Then play one 15+10 game against Stockfish at 1500 with Codex Sol.

## Phase B: Claude engine, search experiment, opponent choice

Requested after the v2 contract. It builds on v2 once v2 lands.

### Claude through Claude Code

Measured on Claude Code 2.1.318 with Sonnet, from an empty working directory:

| Mode | Input tokens per move | Wall time | Model time |
|---|---|---|---|
| `claude -p` defaults | 31,285 | 6.8 s | 2.7 s |
| Stripped: `--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence`, `--tools ""`, our `--system-prompt`, `--json-schema` | 1,277 | 3.3 s | 1.5 s |

- **`--json-schema` returns `structured_output`.** It constrains the move to the legal enum the same way Codex's `outputSchema` does.
- **Start-up.** About 1.8 s of each call is process start-up. The engine keeps one warm `claude -p --input-format stream-json --output-format stream-json` process ready, and starts the next spare as soon as one is used. Each move still gets a fresh session, and the start-up is hidden.
- **Thinking text does not stream.** In headless mode, `thinking_delta` events arrive with empty text, with or without `showThinkingSummaries`. The animation uses those events as a heartbeat.
- **`--bare` is the cleanest mode but needs an API key.** It skips hooks, plugins, CLAUDE.md and more, but accepts only `ANTHROPIC_API_KEY` and never reads the subscription login.
- **The subscription route is for the user's own personal games.** Anthropic does not allow third-party products to offer claude.ai login without approval. The README says this, and documents `--bare` with an API key for anyone distributing the app.

### Visible rationale for every engine

Claude hides its thinking in headless mode, and Codex's reasoning summaries are short at `low`. So the structured answer becomes `{thoughts, move, note}`, in that order:

- `thoughts` is at most 60 words naming the candidates and threats considered.
- `thoughts` streams into the mind panel as it is generated, and it drives gaze.
- `note` keeps its v2 role.

### Opponent choice and thinking animation

- **Opponents are detected and fixed.**
  - The server detects which CLIs are installed and logged in, and re-checks when status is requested.
  - The start screen offers only the detected opponents: "Claude Opus" (`--model opus`) and "Codex Sol" (`gpt-6.1-sol`). There is no model choice; these two models are a requirement.
  - The default is the user's last choice, otherwise Claude Opus, otherwise Codex Sol. With neither detected, the screen explains how to install and log in, and offers fake mode.
  - Names are plain text, with no vendor logos and nothing that copies either product's terminal visuals, per Anthropic's Agent SDK branding guidance.
- **Thinking animation.** Each engine gets an original thinking animation in the orb:
  - a ring of points that pulses on every streamed thinking or rationale event;
  - a shimmer over the plan line;
  - an elapsed-time counter against the budget ring;
  - an accent colour per engine.

  When the rationale streams, its candidate moves appear as gaze arrows.

### Shallow search experiment

- **Scope.** Critical moves only, off by default.
- **Steps:**
  1. Three parallel calls propose candidates.
  2. For each of the top three, the harness plays it and a parallel call plays the opponent's strongest reply.
  3. The harness applies the reply and lists material and threats as facts.
  4. One call picks the candidate with the best worst-case line.
- **No code verdicts.** Code never scores a line, and the final choice is the model's.
- **Evaluation.** It is a fourth arm on the 40 critical positions. It becomes the default only if it lowers loss per move without adding time losses at 15+10.

## Results: Claude Opus on the 60-position evaluation

Run on 2026-11-06 with `experiments/harness_eval/run.py --engines claude`, in batches of 10. It made 120 Opus calls with no errors, refusals or limit hits. Positions are graded by Stockfish 19 at depth 16. The raw report is in `experiments/harness_eval/out/report.md`.

| Arm | Mean loss (95% CI) | Blunders | Change vs v1 (95% CI) | Mean s | p50 s | p90 s |
|---|---|---|---|---|---|---|
| v1 prompt | 8.1 (4.4 to 12.7) | 6 | | 10.5 | 10.8 | 14.0 |
| v2 context | 7.3 (3.9 to 11.4) | 6 | −0.8 (−4.3 to +2.6) | 7.7 | 7.2 | 11.6 |
| v2 context plus verify | 7.3 (3.9 to 11.4) | 6 | −0.8 (−4.3 to +2.6) | 8.2 | | |

- **Quality is unchanged and speed is better.** v2 is not measurably better in quality, but it answers about 27% faster at the median.
- **v2 removed the hanging-piece blunders.** v1's two blunders that left a piece hanging (Bxf4 and Bxh7+) did not recur in v2.
- **Opus's remaining blunders are not hanging pieces.** The rules check fired on 3 of 60 v2 answers. Verify changed 2 moves, both of which lost nothing either way. The check has AUC 0.47 for predicting an Opus blunder, which is useless. The mistakes are missed forcing replies, such as a missed Qc8+.
- **Two independent calls disagreeing is a usable signal.** Different moves from v1 and v2 predicted a v2 blunder with AUC 0.68. They disagreed on 35% of positions, which held 4 of the 6 blunders. This rests on only 6 blunders, so it is a lead, not a result.

Corrections to the Phase B section, as built:
- Claude runs Opus, not Sonnet.
- Each call starts a fresh process. A warm spare saved only about 0.4 s, and it would cost the legal-move enum.
- The streamed rationale field is `commentary`, because a field named "thoughts" drew a `reasoning_extraction` refusal.

## Phase C: the visible harness and second opinions

- **Every model call is visible as a branch.** Each call has an id, a parent and a role:
  - main;
  - second opinion;
  - backup;
  - re-check;
  - "your reply to X";
  - pick;
  - ponder "if you play X".

  The mind panel shows each branch's live commentary, pulses, status, timer and outcome. The board draws each branch's moves in that branch's colour. A one-line decision summary says why the final move won.
- **Second opinion on critical moves.** A second call runs in parallel with a different framing. If the two agree, the move plays.
- **Escalation on disagreement.** If they disagree and the deadline allows, the model plays the opponent's strongest reply to each candidate, code lists the resulting facts, and a pick call chooses. Otherwise the main answer plays.
- **The second opinion must earn its place.** It is an evaluation arm on the 40 critical positions and stays only if it lowers loss without hurting the clock.

## v4 design: one compute allocator

v1 to v3 grew by accretion. A critical flag picks a level, a re-check fires on a rules failure, a second opinion fires on critical positions, an escalation fires on disagreement, and pondering is separate. v4 replaces those separate rules with one decision, asked again after every step:

> Is the current answer likely enough to be a mistake, and is the clock rich enough, that the next unit of thinking is worth its time?

This is rational metareasoning: think more where the value of more computation beats its cost in clock time (Russell and Wefald, and Russek et al.'s finding that strong human players spend time this way). It also answers the "On the Clock" result: agents do not turn spare time into quality unless the harness makes them.

Note on the v1 to v3 evaluation: `run.py` fixed every call at `low` effort to isolate the prompt. Opus's 7.3 average loss and its 6 blunders all come from its cheapest setting. Higher efforts are untested.

### The ladder

Every way of spending more compute becomes a rung. Each rung reuses what the rungs below it produced, so the best answer so far is always ready.

| Rung | Action | Used for |
|---|---|---|
| R0 | Instant: the only legal move, or a validated ponder hit | Free moves |
| R1 | First answer: one call at the floor effort | Every move |
| R2 | Second opinion: a parallel call with the "most forcing" framing at the same effort | Raises or lowers confidence in R1 |
| R3 | Lookahead: for each disputed candidate, the model plays the opponent's strongest reply; code lists the consequences; a pick call chooses | Resolves disagreement with concrete lines |
| R4 | Deep pick: the R3 pick, or a fresh look at the position, at `xhigh` or `max` | The rare position that is high-risk with time to spare |

The latency of each rung and effort level is measured per engine and kept live. Its expected gain, meaning how often it turns a mistake into a good move, comes from evaluation data.

### Risk estimate

The binary critical flag is replaced by a blunder-risk score from 0 to 1, updated as evidence arrives. The flag fires on 52% of positions and has AUC 0.47 for Opus's blunders.

| When | Signal | Status |
|---|---|---|
| Before any call (free) | Tactical density: checks and captures available to both sides, pieces of ours that are hanging, check, captures in the last two plies, attackers around our king, pawns close to promotion, share of safe moves | Built from rules. Weights are fitted, not hand-set. |
| After R1 | Consequences of the answer: does it leave the opponent checks or captures that win material, and does it hang a piece? | Targets Opus's measured failure, missed forcing replies |
| After R2, or a ponder answer for this position | Agreement or disagreement between independent answers | AUC 0.68 for an Opus blunder in the evaluation |

The weights are fitted offline from logged games, each graded by Stockfish after the game, with logistic regression. This is the "learn from previous games" loop from the original brief. It only changes when to think, never which move to play, and it runs between games, not during them.

### Allocator and time bank

- **Per-move budget.** It comes from the clock as before.
- **Time bank.** Time not spent on instant moves, ponder hits and confident early moves goes into a bank.
- **The rule after each rung.** Continue only when both hold:
  - `risk × expected_gain(next rung) > price_of_time × p50(next rung)`;
  - the next rung's p90 fits before the deadline.

  `price_of_time` rises as the clock falls, the same idea as Stockfish's time manager.
- **Leaving early.** A low-risk answer plays at once, even when budget remains, and the time is banked.
- **Spending the bank.** A high-risk position may spend banked time beyond its nominal budget, up to a hard cap of 30% of the remaining clock.

### Clock regimes come from one ratio

The behaviour depends on ρ, the per-move budget (plus available bank) divided by the engine's R1 p50, not on the name of the time control. The same code path covers every clock. Time trouble late in a rapid game moves the harness down a regime automatically.

| ρ | Regime | Typical clocks for Opus | Behaviour |
|---|---|---|---|
| under 1.5 | Scarce | 1+0, deep time trouble | R1 at `low` only. Broad pondering to save clock. Accepts risk. |
| 1.5 to 5 | Blitz | 3+2, 5+3 | R1 at `low`. R2 when risk is moderate. R3 on disagreement if the bank allows. `high` for risky R1s. |
| 5 to 15 | Rapid | 10+5, 15+10 | The floor rises to `medium` or `high`. R2 by default on risky moves. R3 on disagreement. R4 at `xhigh` when risk stays high. |
| over 15 | Classical | 30+20 and slower | R2 on most moves. R3 on any disagreement. R4 at `max` for the highest-risk positions. |

`xhigh` and `max` are reached only through the allocator, never on every move. The start screen lets the player choose any time control, with a recommended default per engine (Opus 3+2, Sol 15+10). One line explains what the harness will do at that clock. During play the mind panel shows the current regime.

### Pondering

- **The opponent's clock is the best place for expensive thinking.** A ponder run climbs the ladder for as long as the user thinks, so a hit returns an answer deeper than the own-clock budget allows. It can reach `xhigh` while the user takes their time.
- **Two replies, conditionally.** The answer gives `expected_replies` with the top two, in order.
  - The top reply is always pondered.
  - The second is pondered only in the scarce and blitz regimes, where saved seconds matter most, and only when the position is not forcing. A forced recapture needs only the top reply.
  - Hit rates for the top one and the top two are logged for free. If the second reply adds under about 15 points of hit rate, it is dropped.
- **A per-game usage cap.** A soft cap on plan usage per game lowers ponder depth and rung ceilings as it is approached.

### What stays

- Pure LLM, and no code-chosen moves.
- Losing on time is allowed.
- No precomputation before a game.
- Claude Opus and Codex Sol, fixed.
- A fresh, stateless prompt with facts, every move.
- Every call visible in the process view, which gains the rung and the risk score.

### Calibration tests, run after v3 lands

1. **Effort ladder.**
   - Run Opus at `high`, `xhigh` and `max` on the 40 critical positions. `low` already exists.
   - Measure loss, blunders and latency per level.
   - This sets each rung's expected gain. About 120 calls.
2. **v3 second opinion and lookahead.**
   - Run on the 40 critical positions, giving R2 and R3 gains and the disagreement rate.
   - About 40 to 100 calls.
3. **Risk model.**
   - Fit and cross-validate on features from tests 1 and 2 plus P0. No calls.
4. **Real games.**
   - Several 3+2 and 5+3 games against Stockfish at `UCI_Elo` 1500, plus the user's own games.
   - Measure time use, losses on time, blunders and ponder hit rates.

Calls run in batches of 10, with usage watched between batches. The ladder's gain table and the regime thresholds are set from these results before v4 is built.

### Calibration results and the v4 revision

Measured on 2026-11-06 on the critical positions, Claude Opus, v2 prompt.

**More effort does not help.** On 20 critical positions:

| Level | Mean loss (95% CI) | Blunders | Mean s | p90 s | Output tokens |
|---|---|---|---|---|---|
| `low` | 10.0 (3.4 to 19.3) | 3 | 7.9 | 11.7 | 609 |
| `high` | 10.3 (3.4 to 20.0) | 3 | 18.1 | 40.2 | 1,474 |
| `xhigh` | 10.8 (2.7 to 21.6) | 3 | 39.4 | 117.3 | 3,515 |
| `max` | no answer within 300 s, one sample | | | | |

- `low` and `high` blundered on the same three positions (p049, p117, p179).
- `xhigh` blundered on p049, p080 and p117.
- The blind spots are stable across effort, so the remaining 20 positions and the `max` probe were skipped to save the user's plan usage.

**Structure helps.** On all 40 critical positions:

- **Result.** The second opinion with lookahead (v3) lowered mean loss from 10.2 to 8.4, paired 1.8 better (95% CI −1.0 to +5.7). Blunders fell from 6 to 4. It fixed p014 (47 to 0) and p056 (49 to 0), and on the 20-position subset p179. It created no new blunders.
- **Costs.** It disagreed on 45% of critical positions and changed 10 moves: 4 better, 4 slightly worse (3 to 13 points), and 2 the same. Mean time on critical moves rose from 7.7 s to 16.9 s.

**Revisions to the v4 design above:**

1. **Opus thinks at `low`.** Every Opus call uses `low` at every clock. Delete R4 and the effort-raising regime rules: `high` costs 2.3 times the time and `xhigh` 5 times, for no measured gain. `xhigh` and `max` stay available in code but unused, and get revisited only if a future model changes this.
2. **Spare time buys structure, not effort.** Richer clocks widen the structure: R2 on more positions, R3 on more disagreements, and later possibly more candidates or a second reply ply. They do not deepen the thinking.
3. **Escalate on stronger evidence.** Disagreement alone fires on 45% of critical positions. R3 should need disagreement plus a concrete danger sign in the risk score, such as the main move leaving the opponent a check or capture.
4. **The pick keeps the original by default.** Its prompt tells the model to keep the first move unless the reply line shows it loses material or walks into an attack. The model still judges. This targets the 4 needless swaps.
5. **Codex Sol is untested on the ladder.** Its effort may behave differently. Test it before applying these rules to Sol, quota permitting.

### Correction: effort is an open question, not a settled no

The two "Opus thinks at `low`" and "spare time buys structure, not effort" revisions above were too strong. Each level in the 20-position test has a 95% CI about ±8 points wide, so it cannot detect a 1 to 2 point gain per move, which compounds to real Elo over a game. Public data shows effort usually helps:

- **ChessBench** (repo `data/ratings.json`, Lichess Classical scale):
  - gpt-5.6-sol: low 1268, medium 1394, high 1530, xhigh 1465.
  - gemini-3-flash-preview: minimal 334, low 648, medium 1850, high 1744.
- **LLM Chess** (`data/elo_refined.csv`, Elo anchored to Komodo Dragon):
  - gpt-6.1-sol: high 1454, xhigh 1612.
  - gemini-3.6-flash: low 498, medium 921, high 1094.
  - gpt-6-astra: high 1614, xhigh 1579.
  - claude-opus-5-5: high only, 1575 ±113.
- **The pattern.**
  - Gains are large at the low end and flatten or dip at the top.
  - Part of the leaderboard gain is fewer illegal or malformed actions; LLM Chess reports o4-mini at 91 wrong actions per 1,000 moves at low and 34 at high. This harness already makes those impossible.
  - No public data covers Opus 5.5 below `high`.

The current code keeps `low`, `medium` and `high` for Opus, with latency seeds from the measured runs: `low` 7.9/11.7 s, `high` 18.1/40.2 s, `xhigh` 20.1/117.3 s, and `max` over 300 s. The deterministic planner then gives:

| Clock | Quiet | Critical |
|---|---|---|
| 3+2 | `low` | `medium` |
| 5+3 and slower | `low` | `high` |

The v4 allocator should treat effort as one of the actions it learns to choose. It should not hard-code effort either way.

## v4 as built: Maia-2 rates the position, the clock sets the effort

The user ruled out more effort experiments and any slow training project, and asked for a model that sees the whole board. Two observations shaped the design.

**Opus already chooses its own thinking time.** The effort level is a cap. Within it, Opus's adaptive thinking spends between 162 and 10,898 tokens on different positions at `xhigh`. But that spending follows how busy the board is, not where the danger is. On the 60 graded positions, Opus's thinking at `low` rises with the move number (Spearman +0.39). It falls slightly in positions where only one move works (−0.15), and it is no higher on the moves Opus blundered (232 against 225 tokens). It cannot spend time on a threat it has not seen.

**The v3 rules flag was noise for Opus.** It separated Opus's blunders with AUC 0.47.

**The allocator, as built:**

1. **Difficulty.** `zeitnot/difficulty.py` rates each position before the plan. Maia-2 computes the chance that an 1800 blitz player blunders: below 1% is easy, 10% or above is hard. Before ply 10, or without the optional `maia` extra, the count of legal moves rates the position instead. The rating never reaches the model. Evidence and caveats are in `experiments/maia_check/FINDINGS.md`: AUC 0.73 against 0.47 for the rules, the same as the legal-move count, and 0.83 combined with the second opinion.
2. **Budget.** `zeitnot/timeman.py` gives each move a share of the clock: 0.6 of a share when easy, 1 when normal and 2 when hard. That averages about one share per move.
3. **Effort.** The level is the deepest one whose p50 fits the budget and whose p90 fits the deadline (twice the budget). This follows the leaderboards and puts a cap on the slow case: Opus `xhigh` is typically 20 s but 117 s in the slow case, so it fits only hard moves at 15+10 and slower. Opus's levels are `low`, `medium`, `high` and `xhigh`; `max` (over 300 s) never fits.
4. **Second opinion and search.** These run on hard moves, where the critical flag used to trigger them. Disagreement still escalates to the reply calls and the pick.
5. **The start screen.** Under the time control it states what the clock buys, for example "On 15+10 it thinks at high, xhigh on hard moves."

| Clock | Opus, easy | Opus, normal | Opus, hard |
|---|---|---|---|
| 3+2 | `low` | `low` | `medium` |
| 5+3 | `low` | `low` | `high` |
| 10+5 | `medium` | `high` | `high` |
| 15+10 | `high` | `high` | `xhigh` |

**Not built from the v4 plan:**
- A trained risk model and a bandit over actions, because the user ruled out training.
- The stricter escalation trigger and the pick prompt that keeps the original move by default. Both remain open.

**Cost to watch.** Above `low`, every main call runs with a backup call at `low`. Slower clocks now think above `low` on most moves, so they use about twice the Opus calls per move.

## Final version: what the first real game changed

A 3+2 game against Opus, graded afterwards by `experiments/game_review/review.py`, drove the last round of changes. Both sides averaged about 5.5 points of win% lost per move.

- **What worked.**
  - Maia-2 separated Opus's errors with AUC 0.71 over 33 rated moves. Opus lost 9.9 points per move on hard positions, against 2.6 on easy ones.
  - Pondering played 21 of Opus's 38 moves at once.
- **What failed.**
  - **Discarded answers.** At move 27 the second opinion found the best move (Ng5). The harness discarded it because the full comparison did not fit before the deadline, so the main answer (Bd3) lost 38 points.
  - **Missed sacrifices.** Three of Opus's worst moves missed a knight sacrifice with check on e6. The prompt had tagged those sacrifices "hangs piece".
  - **A starved endgame.** Opus spent the clock evenly, then ran on the increment from move 30 with 1–8 s budgets on hard positions.

**Changes:**

1. **Quick comparison.** When the opinions split and only one round of calls fits, one pick call sees both moves with your checks and captures after each. Before, the main answer played without a comparison.
2. **Second opinions while pondering.** A ponder run on a hard position also runs the second opinion and the comparison, on your time. This is on by default; `ZEITNOT_PONDER_OPINIONS=0` turns it off.
3. **A pondering-aware clock.** `timeman.plan` shares the clock among the moves expected to cost time, using the game's ponder hit rate. The rate starts at 30%, is capped at 50%, and arrives with each request as `ponder_hits`. At 5+3 this lifts a normal move's budget from about 12 s to 17 s.
4. **Your pace sets the ponder window.** A ponder run may plan for 60% of your median think time (`opponent_ms`), capped at what Opus's own clock could cover if you moved at once.
5. **No rule overrides the model.** The rules check, the re-check and the veto are gone, and so are the "hangs piece" tags on allowed moves. Two measurements back this:
   - For Opus the rule predicted blunders at chance level (AUC 0.47–0.53).
   - In the game it would have flagged 5 of the 7 best moves Opus missed.

   The backup is still offered only safe moves, and it plays only when the main call misses the deadline.
6. **No search experiment.** It was never measured: there are no search calls in any log. It cost up to seven calls per hard move, and the second opinion already covers the comparison it offered.
7. **5+3 is Claude's default clock.** At 3+2, Opus's cheapest call (about 6 s) is the whole per-move pace. At 5+3, hard moves get `high` with room for the comparison.

**Not built:** an endgame reserve that caps hard-move spending. At 3+2 time is zero-sum, and the stretch of hard moves that drained the clock was where the game was decided.

**Next evidence:** grade each new game with `review.py`. After about 30 games, compare Maia with the legal-move count, and the second opinion's hit rate, on real labels.

**After the first 5+3 game.** Maia flagged Opus's errors with AUC 0.79 over 31 moves. Opus's worst move, 19...Na5 (loss 27), came on a hard position where the main call and the second opinion both chose it at `high`, with 23 s of a 43 s budget unused. `xhigh` never ran: a 43 s budget gives a 68 s deadline, and 4 of the 19 measured `xhigh` calls took longer. The fix keeps `xhigh` off the main call but lets the second opinion think one level deeper whenever that level's median fits the budget. The main answer covers a deeper call that runs long, because the second opinion is waited for only until the budget runs out. The backup stays: 53 moves ran one and 5 played it.

**Opus ran out of time in that 5+3 game.** The overspend was on normal moves, not hard ones. Moves 24 to 31 were rated normal, played at `high` and took 17 to 35 s against budgets of 14 to 25 s, taking the clock from 166 s to 46 s; later hard positions got 9 s at `low`, and Opus flagged at move 40. The planner had picked the deepest level whose median fits the budget, but think times have a long slow tail, so the mean sits above the median and half the moves overshoot. Levels are now budgeted by expected time: the mean of the last 20 calls, or the midpoint of the seed's p50 and p90 before three calls. At 5+3, normal moves now play at `low` and hard ones at `high` with an `xhigh` second opinion. At 10+5, hard moves move from an `xhigh` main call (one took 86 s in the next game) to `high` with an `xhigh` second opinion.
