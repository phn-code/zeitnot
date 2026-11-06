# Play app design

Updated 2026-11-06 for the final version. You play Claude Opus or Codex Sol at a time control you pick, in a local browser app, and watch its commentary stream beside the board. The harness rates how hard each position is and decides how long the model may think, but never chooses or overrides a move. The harness design and the evidence behind it are in [harness.md](harness.md). The broader research design is in [design.md](design.md).

## Two shapes considered

| | A. Server owns the game | B. Browser owns the game, server thinks on request |
|---|---|---|
| Game state | A server session holds board, clocks and a WebSocket | The browser holds the board (chess.js) and both clocks. The server keeps no game state. |
| Server interface | A WebSocket protocol with about 8 message types, session lifecycle, flag timers | One streaming endpoint, `POST /api/think` |
| Clock authority | Server | Browser. The model's clock includes network latency, which is the delay the player actually waits through. |
| Pondering later | Fits the session | `POST /api/ponder` fills a server cache keyed by position |
| Failure modes | Session cleanup, reconnects, races between a human move and the model task | The browser cancels a request with `AbortController` |

**Choice: B.** The app is local and single-player, so a server-side session adds states and races without adding anything the player notices. B keeps the server to one request in, one stream out.

## Contract

`POST /api/think` takes JSON:

```json
{
  "moves": ["e2e4", "e7e5"],
  "clock": {"ai_ms": 300000, "human_ms": 298000, "increment_ms": 3000},
  "engine": "claude",
  "time_control": "5+3",
  "opinions": true,
  "ponder_hits": 4,
  "opponent_ms": 6200
}
```

`moves` are UCI moves from the standard starting position. The server rebuilds the board from them and rejects an illegal history with HTTP 400. `engine` is `claude`, `codex`, `gemini` or `fake`, and must be on offer right now. Without it the server uses its default. `opinions` overrides `ZEITNOT_OPINIONS` for this request. `ponder_hits` counts the model's moves this game that played at once from pondering; the planner uses it to share the clock among the moves expected to cost time. `opponent_ms` is your median think time this game; a ponder run may plan for 60% of it. The response is NDJSON, one event per line:

| Event | Fields | When |
|---|---|---|
| `plan` | `budget_ms`, `deadline_ms`, `level`, `reason`, `engine`, `difficulty`, `difficulty_text`, `blunder_chance`, `opinions`, `second_level` | First, unless the move is forced |
| `call` | `call`, `status`, `role`, `label`, `parent`, `engine`, `level`, `framing`, `fen`, `elapsed_ms`, `move`, `uci`, `expected_reply`, `facts`, `error` | Each step in the life of one model call: `started`, then `answered`, `cancelled` or `failed`, and `chosen` for the answers that won |
| `note` | `text` | Harness decisions, for example "fallback ready: Nf3", "deadline reached" or "opinions split (Bc4 vs Nf3); time for a quick comparison" |
| `thought` | `text`, `stream`, `call` | A whole reasoning summary when `stream` is null. Otherwise new commentary text for the chunk with that id, which is the call id. |
| `pulse` | `call` | A heartbeat while that call works, at most four a second per call |
| `decision` | `text`, `move`, `call` | Just before `move`: one sentence on why this move won, and the id of the call whose answer it is |
| `move` | `uci`, `san`, `source`, `elapsed_ms`, `usd`, `tokens`, `note`, `ponder_hit`, `expected_reply`, `ponder_replies` | Exactly once, last, on success |
| `error` | `message` | Last, only when the model failed every attempt or the spend cap is reached |

Every model call has an id, unique per server process, so a ponder run's calls keep their ids when `/api/think` replays them. `parent` is the id of the call a call depends on, or null:

| Role | Label | Parent | When |
|---|---|---|---|
| `main` | "Main", "Main · attempt 2" | none | Every move that is not forced |
| `second_opinion` | "Second opinion · most forcing" | none | Hard moves with opinions on, at the planned level, including ponder runs |
| `backup` | "Backup · safe moves only" | none | When the main call thinks above the backup level |
| `reply` | "Your best reply to Bc4" | the call that proposed Bc4 | The opinions split and time allows. `fen` is the position after Bc4, and the answered event lists the `facts` after the reply. |
| `pick` | "Pick · Bc4 or Nf3" | none | After the replies |
| `ponder` | "If you play Nf3" | none | The main call of a ponder run. Every other top-level call of that run hangs under it. |

`decision` sentences, by path: "The model chose e4.", "Both opinions chose Nf3.", "Opinions split (Bc4 vs Nf3). After Bc4 you would play Qb3, so Nf3.", "Opinions split (Bc4 vs Nf3) with no time to compare, so the main answer Bc4.", "Opinions split (Bc4 vs Nf3); the pick chose Nf3." (the quick comparison), "Opinions split (Bc4 vs Nf3); the pick did not finish, so the main answer Bc4.", "The second opinion did not answer in time, so the main answer Bc4.", "The main call ran past the deadline, so the backup Nc3.", and on a ponder hit "You played Nf3 as expected, so the prepared Bc5 plays at once." A forced move has no `decision`; the browser shows "The only legal move."

`source` is `model`, `fallback` (the backup call's answer, also a model answer) or `forced`. `tokens` is `{"input": int, "thinking": int, "output": int}` summed over every call made for this move. `expected_reply` is the reply the winning answer expects, in SAN, checked against the position after the move, or null. `ponder_replies` are UCI replies to ponder, the expected one first: one by default, up to `ZEITNOT_PONDER_REPLIES`. A ponder run on a hard position asks the second opinion too, unless `ZEITNOT_PONDER_OPINIONS=0`.

`POST /api/ponder` takes the same body plus `expected_reply` (UCI) and returns `{status, fen}` at once. It runs the one-move pipeline in the background on the position after that reply and caches the result by FEN. Runs for replies to the same position ponder side by side; a request for another position cancels them.

`GET /api/ponder/stream?fen=...` streams one ponder run's events as NDJSON: everything so far except pulses, then live events until the run ends. It never sends the run's `move`; its last event is the run's `decision`. It returns 404 when nothing is pondering that position.

`GET /api/status` returns `spent_usd`, `cap_usd`, `fake`, `opinions`, `difficulty` (the rater: `maia2` or `legal moves`), `pondering` (per engine), `setup_help` (install and login lines when neither CLI is ready), and `engines`. Each engine entry has `id`, `name`, `model`, `label`, `default`, `free`, `suits`, `hint`, `time_control`, `latency` and `regimes` (the level per difficulty tier at move 10 for each clock, which the start screen states).

## Opponents and detection

| Engine | Model | Levels | Process |
|---|---|---|---|
| `claude` | Opus, fixed | `--effort` low, medium, high; xhigh and max are mapped but never planned | One stripped `claude -p --output-format stream-json` per call, run from an empty temporary directory |
| `codex` | `gpt-6.1-sol`, fixed | low, medium; xhigh and max are mapped but never planned | One `codex app-server` per server process, a fresh ephemeral thread per call |
| `gemini` | `gemini-3.6-flash` | minimal, low, medium | The Gemini API. Only with `ZEITNOT_GEMINI=1` and a key. |
| `fake` | scripted | any | No calls. In real mode it is offered, free, only when neither CLI is ready. |

A `Detector` runs `claude auth status --json` and `codex login status` in threads, and caches the result for 30 s. `/api/status` and `/api/think` read it, so a login while the app runs shows up without a restart. Detection never depends on Gemini.

Each engine streams `Thought`, `Rationale` and `Pulse` events and ends with one `Answer`. The answer is JSON `{commentary, move, expected_reply, note}`, with `move` constrained to the allowed moves: Claude's `--json-schema`, Codex's `outputSchema`, Gemini's response schema. `RationaleParser` decodes the commentary string out of the partial JSON as it streams, up to 60 words. Claude streams it as `input_json_delta` events of its structured-output tool, Codex as `item/agentMessage/delta`, and Gemini as answer text chunks. Claude's thinking deltas carry no text in headless mode, so they become pulses. When Claude's safeguards refuse an answer with a commentary, the engine asks once more without the commentary field.

Closing an engine stream interrupts the call: Claude's process is killed, Codex gets `turn/interrupt`, and Gemini's stream is closed.

## Module map

```
zeitnot/
  config.py    Settings from .env and the environment, prices, latency seeds, speed hints, setup help
  rules.py     board_from_moves, safe_moves, hanging, check_move
  facts.py     the facts block, line facts after a reply, the expected-reply check, the miss digest
  difficulty.py  the position's tier: Maia-2's blunder chance past ply 10, the legal-move count before it or without Maia
  context.py   the per-move prompt and the forcing framing
  timeman.py   plan(): pure, clock, difficulty tier and live latency in, Plan out
  engines.py   the engine protocol, the latency table, the answer schema and RationaleParser, Gemini and fake engines
  claude.py    ClaudeEngine and claude_available
  codex.py     AppServer, CodexEngine and codex_logged_in
  events.py    the NDJSON event types
  calls.py     the model calls of one move: ids, roles and parents, call events, one queue, a pulse gate per call
  difficulty.py  Maia-2's blunder chance per position, the legal-move count where Maia does not rate
  player.py    one_move(): forced moves, difficulty, plan, main, second opinion and backup, comparison,
               decision; think() adds pondering and the log
  compare.py   escalate(): the full comparison (reply calls, then the pick) or the quick one (the pick alone)
  ponder.py    background runs cached by FEN, several per turn
  ledger.py    SpendLedger persisted to data/spend.json
  server.py    FastAPI app, Detector, /api/think, /api/ponder, /api/status, static files from web/
  __main__.py  python -m zeitnot: uvicorn on 127.0.0.1:8000, opens the browser
web/
  index.html, app.js, board.js, mind.js, calltree.js, gaze.js, util.js, sound.js, style.css, themes.css
```

## One move, step by step

1. Rebuild the board. If there is exactly one legal move, yield `move` with source `forced` and stop.
2. If pondering has a result for this position, replay its events, yield a `decision`, and yield its move with `ponder_hit`. Otherwise note the moves the other ponder runs prepared, for the digest.
3. Compute the facts, rate the position's difficulty off the event loop, compute the plan, and yield `plan`.
4. Start the main call. On a hard move with opinions on, start the second opinion with the most-forcing framing, one level deeper than the main call when that level's median latency fits the budget, else at the same level. When the level is above the backup level, start the backup call at low with only safe moves.
5. When the main answer lands, wait for the second opinion until the budget runs out (until the deadline when the main answer itself came after the budget), and never past the point where not even the quick comparison fits. On a split, run `compare.escalate`: the full comparison when two low p50s fit before the deadline, the quick one when one fits. The pick's answer is the move. Otherwise go on with the main answer, which plays as it is: no rule overrides it. If the main call fails three times, a landed second opinion plays before the move ends in an error.
6. At the deadline, play the backup move if it landed. Otherwise keep waiting for the main answer. The main call gets up to three attempts when it fails.
7. Cancel unfinished calls, mark the winning calls `chosen`, and yield `decision`. Charge every finished call to the ledger, store the note, the expected reply and the unplayed candidates for the next digest, append a record with the decision and the call tree to `logs/moves.jsonl`, and yield `move`.

If the browser disconnects, the server cancels every call of the move.

## Browser behaviour

- **Setup.** One plain-text chip per detected opponent, remembered between visits. Time-control presets 1+0, 3+2, 5+3, 10+5 and 15+10, plus custom, remembered per opponent and defaulting to the opponent's own (5+3 for Claude, 15+10 for Codex). Under it, one line says what the clock buys: "On 5+3 it thinks at medium, high on hard moves and low on easy ones." Colour: white, black or random. When no opponent is ready, install and login steps and the fake demo.
- **Board.** cm-chessboard with chess.js for rules, a promotion picker, gaze glows, and arrows per call: each call's answer and the moves its commentary names in that call's colour, the backup move as a dashed ghost, your replies in the opponent colour. Hovering or focusing a card dims every other call's arrows. At most one arrow per square pair is drawn.
- **Clocks.** Both clocks tick in the browser using `performance.now()`. The increment is added after each move. The model's clock runs while its request is in flight. A clock at zero loses the game, for the model as for you.
- **Mind panel.** The orb with its budget ring, sparks that advance on each `pulse` from any call, and an elapsed counter. Below it, the process view (`mind.js`): the plan line, which shimmers while the model thinks, one card per call nested by parent, harness notes, and the decision. `calltree.js` holds the pure parts: the reducer that applies `call` events (a status only moves forward, so a replay is harmless), the tree rows with their connector rails, and the colour per call. While you think, a "Considering your replies" section shows one process view per ponder run. When you move, the matching run's view becomes the move's view, and `/api/think` replays its events into it without repeating the text. Each finished move collapses into a history row that rebuilds its process view from the recorded events.
- **Game end.** Checkmate, stalemate, the draw rules chess.js reports, a flag, or resignation. The end card offers a rematch, a new game and the PGN.

## Verification

- `pytest` covers the rules, facts, the time manager, the pipeline with scripted engines, the second opinion and its escalation (call lifecycle and parents, agreement, split with and without time, deadlines, no code-chosen move on any path, decision sentences), the expected-reply check and the miss digest, pondering and the ponder stream, the Maia-2 rater and its fallback, the quick comparison, the Claude engine against a fake `claude` program (streaming, refusal, interrupt), the Codex engine against a fake `codex app-server`, detection against fake CLIs (installed, logged out, missing), the picker's status data, and the eval runner's resume. It also runs the node tests for `web/gaze.js`, `web/util.js` and `web/calltree.js`.
- One real move each from Claude Opus and Codex Sol through `/api/think` confirms pulses, streamed commentary and a legal move.
