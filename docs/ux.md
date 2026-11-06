# UX design

Proposal dated 2026-11-06. This doc defines how the play app looks, moves and feels. The game logic, server contract and time manager are in [app.md](app.md) and do not change.

## Principles

- **The model's mind is the show.** Playing an LLM differs from playing an engine because you can watch it reason. Every visual flourish serves that. Nothing else animates for its own sake.
- **Minimal.** One accent colour per theme, generous space, no borders where spacing works, no icons without a purpose. Hide what the player does not need right now.
- **Motion with meaning.** Animations show cause and effect: a move landing, where the model is looking, how much of its time budget it has used. Durations stay short (150 to 300 ms for UI, about 220 ms for a piece move).
- **Respect the player.** `prefers-reduced-motion` turns off glows, pulses and the typewriter reveal but keeps every static highlight. `prefers-color-scheme` picks the first theme. Choices persist in `localStorage`.

## Screens

### Start

A centred column on an empty page: the wordmark `zeitnot`, a one-line tagline ("Play an LLM that decides when to think."), then three rows of chips: time control (1+0, 3+2, 5+3, 10+5 selected, 15+10, custom), side (White, Random, Black), and the model as a quiet select. Under the time control, one grey line gives the expected cost per game from the plan, for example "about $0.10 to $0.30 per game". One primary button: Play. The column fades and rises 8 px on load.

### Game

Desktop: the board on the left at `min(88vh, 720px)`, and a narrow column on the right called the mind panel. Narrow screens stack the board above the mind panel.

**Board.**
- cm-chessboard (MIT) with a theme-matched palette and a clean SVG piece set. Coordinates small and low-contrast, inside the board edge.
- Click-to-move and drag. Selecting a piece shows legal destinations as small dots, and captures as rings.
- The last move tints its two squares. Check gives the king's square a soft radial red glow.
- Promotion uses a small inline chooser, not auto-queen.
- Piece moves glide (about 220 ms, ease-out). A capture fades the captured piece.

**Mind panel, top to bottom.**
1. The opponent's header: an orb, the model name, and the model's clock.
2. The plan line, in small grey text, for example "medium thinking · 19 s budget".
3. The thought stream.
4. The move history, collapsed.
5. The player's clock at the bottom, level with the board's bottom edge.

**The orb** is the model's presence.
- **Idle.** A still, soft circle in the accent colour.
- **Thinking.** It breathes: a slow scale and glow loop of about 2.4 s.
- **Budget ring.** A thin ring around the orb fills over the plan's budget. It turns amber past the budget and red near the deadline. This shows the time manager at work.

**The thought stream.**
- Thought summaries appear as they stream. Each new chunk fades and slides in, revealing at reading speed (a fast typewriter of about 2 ms per character, capped so a chunk never takes longer than 600 ms).
- `**Headings**` render as small-caps labels in the accent colour, and the body text is quiet.
- The stream autoscrolls unless the player has scrolled up.
- When the move lands, the stream collapses into one history row: "14. Nf3 · medium · 8.4 s · $0.004". Clicking the row expands its thoughts.

**Gaze.** While thoughts stream, the board shows where the model is looking. This is the signature effect.
- The client scans each new thought chunk for square names (`e5`) and move notation (`Nf3`, `exd5`, `O-O`, `Qxd3+`, `33. b5`).
- A mentioned square gets a soft glow that fades over about 2.5 s.
- A mentioned move that is legal in the current position gets a faint arrow, resolved through chess.js. At most 3 arrows show at once, and the oldest fades first.
- Gaze uses the accent colour at low opacity, so it never competes with the real position.

**The harness, visible.**
- When the note "fallback ready: Nc6" arrives, a dashed grey ghost arrow shows the backup move with a tooltip, "backup move, ready if time runs short".
- When the model commits, the ghost and gaze clear. The chosen piece glides, and the destination square pulses once in the accent colour.
- If the fallback or emergency move is the one played, the history row says so with a small tag.

**Clocks.**
- Large tabular numerals. The active clock is full contrast and the idle one is muted.
- Under 10 s the clock shows tenths and pulses gently in red.

**Controls.** One quiet row: resign (with a confirm on second click), new game, sound on or off, and the theme switcher. A small cost readout shows this game and the total against the cap.

**Game end.** The board dims slightly and a card fades in over it with the result (for example "Checkmate. Gemini wins."). It shows the model's total and average think time, how many moves used each level, how many were fallbacks, and the game's cost. Buttons: Rematch (same settings, colours swapped), New game, Copy PGN.

**Sound.** Synthesised with WebAudio, so there are no asset files: a soft tock for a move, a lower tock for a capture, a two-note chime for check and for game end. On by default at low volume, toggleable, and persisted.

## Themes

Three themes behind the switcher. Each is a set of CSS custom properties, so a theme is data, not code. The player picks the default.

| Theme | Page | Board light / dark | Accent |
|---|---|---|---|
| Paper | warm off-white | cream / sage | ink green |
| Slate | cool grey | pale blue-grey / slate | white |
| Night | near-black | charcoal / graphite | violet glow |

## Fake mode

Fake mode is the free demo and the test bed, so its thoughts must exercise gaze. `FakeClient` writes thoughts that mention 2 or 3 of the moves it was offered and a square or two, with the same streaming rhythm as Gemini.

## Out of scope

Premoves, an analysis board, accounts, and online play.
