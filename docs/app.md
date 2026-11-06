# Play app design

Proposal dated 2026-11-06. You play Gemini Flash at a time control you pick, in a local browser app, and watch its reasoning stream beside the board. The harness decides how long the model may think on each move. The measurements behind the numbers here are in [FINDINGS.md](../experiments/p0_probe/FINDINGS.md). The broader research design is in [design.md](design.md).

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
  "clock": {"ai_ms": 600000, "human_ms": 598000, "increment_ms": 5000},
  "model": "gemini-3.6-flash"
}
```

`moves` are UCI moves from the standard starting position. The server rebuilds the board from them and rejects an illegal history with HTTP 400. The response is NDJSON, one event per line:

| Event | Fields | When |
|---|---|---|
| `plan` | `budget_ms`, `deadline_ms`, `level`, `reason` | First, unless the move is forced |
| `note` | `text` | Harness decisions worth showing, for example "fallback ready: Nf3" or "deadline reached" |
| `thought` | `text` | Each streamed piece of the model's thinking summary |
| `move` | `uci`, `san`, `source`, `elapsed_ms`, `usd`, `tokens` | Exactly once, last, on success |
| `error` | `message` | Last, only when no move can be produced, for example when the spend cap is reached |

`source` is one of `model`, `fallback`, `forced` or `emergency`. `tokens` is `{"input": int, "thinking": int, "output": int}` summed over every call made for this move.

`GET /api/status` returns `{"spent_usd", "cap_usd", "models", "default_model", "fake"}`.

## Module map

```
zeitnot/
  config.py    Settings from .env and the environment, the price table, the latency table
  rules.py     board_from_moves, render (prompt text), safe_moves, emergency_move
  timeman.py   plan(): pure, clock and position in, Plan out
  models.py    ModelEvent types, the ModelClient protocol, GeminiClient, FakeClient
  ledger.py    SpendLedger persisted to data/spend.json
  player.py    think(): orchestrates one move and yields events
  server.py    FastAPI app: /api/think, /api/status, static files from web/
  __main__.py  python -m zeitnot: uvicorn on 127.0.0.1:8000, opens the browser
web/
  index.html, app.js, style.css
tests/
```

## Core types

```python
@dataclass(frozen=True)
class Clock:
    ai_ms: int; human_ms: int; increment_ms: int

Level = Literal["none", "minimal", "low", "medium", "high"]   # "none" means no main call, fallback only

@dataclass(frozen=True)
class Plan:
    level: Level; budget_ms: int; deadline_ms: int; reason: str

# What a model client yields while streaming
@dataclass(frozen=True)
class Thought:
    text: str
@dataclass(frozen=True)
class Answer:
    san: str; input_tokens: int; thinking_tokens: int; output_tokens: int
ModelEvent = Thought | Answer

class ModelClient(Protocol):
    def stream(self, prompt: str, choices: list[str], level: Level) -> AsyncIterator[ModelEvent]: ...

# What think() yields; serialised one per NDJSON line
Event = PlanEvent | NoteEvent | ThoughtEvent | MoveEvent | ErrorEvent
async def think(req: ThinkRequest, client: ModelClient, ledger: SpendLedger, settings: Settings) -> AsyncIterator[Event]
```

`GeminiClient.stream` calls `client.aio.models.generate_content_stream` with `ThinkingConfig(thinking_level=level, include_thoughts=True)`, `response_mime_type="text/x.enum"` and `response_schema={"type": "STRING", "enum": choices}`. Parts with `part.thought` become `Thought`. The final text and `usage_metadata` become one `Answer`. `experiments/p0_probe/gemini_timing.py` is a working reference for this call.

`FakeClient` needs no API key. It yields a few scripted thoughts with short delays, then answers with a deterministic choice. Its delays come from settings, so tests can make it slower than a deadline. `ZEITNOT_FAKE_MODEL=1` selects it.

## Time manager v1

`plan(clock, ply, legal_count, latency)` is a pure function.

1. `moves_to_go = max(10, 40 - ply // 2)`.
2. `budget = min(clock.ai_ms / moves_to_go + 0.8 * clock.increment_ms, 0.25 * clock.ai_ms)`.
3. `level` is the highest of `medium` and `low` whose p50 latency fits inside `budget`. Otherwise `level` is `none`.
4. `deadline_ms = min(2 * budget, 0.30 * clock.ai_ms, clock.ai_ms - 1500)`, floored at 0. The main call is cancelled at the deadline. The 1,500 ms safety margin applies only here, so it never zeroes the budget.
5. Below 10,000 ms on the model's clock, `level` is `none`. Below 2,000 ms, the player makes no API call and plays `emergency_move`.

At the start of a game this gives `none` with a 3 s deadline at 1+0 (fallback moves only), `low` at 3+2 and 5+3, and `medium` at 10+5 and 15+10. The first version subtracted the safety margin from the budget, which made 1+0 play rules-only moves from move 1.

The latency table starts at p50 11,000 ms for `medium` (measured in P0), 5,000 ms for `low` and 2,500 ms for `minimal`. The last two are guesses until they are measured on a billed key. The table lives in `config.py`.

## One move, step by step

1. Rebuild the board. If there is exactly one legal move, yield `move` with source `forced` and stop.
2. If the ledger is over its cap, yield `error` and stop.
3. Compute and yield the `plan`.
4. Start the fallback task. It calls the model at `minimal` with only the moves that pass `safe_moves` and collects the answer without streaming thoughts. When it lands, yield a `note`.
5. If `plan.level` is not `none`, start the main task at `plan.level` with every legal move. Yield each `Thought` as a `thought` event.
6. Commit the main answer if it arrives before the deadline. Otherwise, or if the main call fails, yield a `note` and commit the fallback answer, waiting for it until the deadline if needed. If neither exists, commit `emergency_move` (the first safe move in a fixed order, rules only).
7. Cancel unfinished tasks, add the cost of every finished call to the ledger, append a record to `logs/moves.jsonl`, and yield `move`.

If the browser disconnects, the server cancels both tasks.

## Browser behaviour

- **Setup.** Time-control presets 1+0, 3+2, 5+3, 10+5 (default) and 15+10, plus custom minutes and increment. Colour: white, black or random. A model list comes from `/api/status`.
- **Board.** An MIT- or BSD-licensed board library with pinned versions, and chess.js for rules. Promotion may auto-queen in v1.
- **Clocks.** Both clocks tick in the browser using `performance.now()`. The increment is added after each move. The model's clock runs while its request is in flight. A clock at zero loses the game.
- **Thinking panel.** It shows the plan, the notes and the streamed thoughts as they arrive, with `**bold**` headings rendered. Each finished move shows its source, seconds and cost. Earlier moves' thoughts stay available, collapsed.
- **Game end.** Checkmate, stalemate, the draw rules chess.js reports, a flag, or resignation. A new-game button resets.
- **Cost.** The panel shows this game's spend and the ledger total from `/api/status`.

## Verification

- `pytest` covers `plan()` cases, `safe_moves` cases, and `think()` with `FakeClient`: forced move, main answer in time, deadline then fallback, main error then fallback, panic and emergency, and the spend cap. It also streams `/api/think` through FastAPI's test client in fake mode.
- A browser run in fake mode plays a full game through the UI.
- With a billed Gemini key, one real game confirms the timings and the thinking stream.
