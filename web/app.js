import { Chess } from "https://cdn.jsdelivr.net/npm/chess.js@1.4.0/+esm";
import { Board } from "./board.js";
import { resolveMove } from "./gaze.js";
import { Mind } from "./mind.js";
import { Sound } from "./sound.js";
import {
  DEFAULT_PRESET,
  PRESETS,
  describeCost,
  formatClock,
  formatSeconds,
  formatUsd,
  modelLabel,
  moveLabel,
  ndjsonLines,
  soundCues,
  summarize,
} from "./util.js";

const $ = (id) => document.getElementById(id);
const THEMES = ["paper", "slate", "night"];
const SIDES = [
  ["white", "White"],
  ["random", "Random"],
  ["black", "Black"],
];
const END_CARD_DELAY_MS = 450;

const prefs = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`zeitnot.${key}`);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`zeitnot.${key}`, JSON.stringify(value));
    } catch {
      // Private windows and blocked storage: the choice lasts for this page only.
    }
  },
};

const sound = new Sound({ enabled: prefs.get("sound", true), volume: prefs.get("volume", 0.7) });
const mind = new Mind($("mind"), { onGaze: gazeAt });
let board = null;
let serverStatus = null;
let game = null;

function newGame(settings) {
  const ms = Math.round(settings.minutes * 60_000);
  return {
    settings,
    chess: new Chess(),
    humanColor: settings.humanColor,
    aiColor: settings.humanColor === "w" ? "b" : "w",
    model: settings.model,
    incrementMs: settings.increment * 1000,
    clocks: { w: ms, b: ms },
    turnStart: 0,
    uciMoves: [],
    abort: null,
    spendUsd: 0,
    over: false,
    result: null, // {text, winner: "human" | "ai" | "draw" | null}, null winner when the game was aborted
    thinking: false,
    plan: null, // the plan event of the move being thought about
    legal: [], // chess.js verbose moves for the model's position, for gaze and the ghost arrow
    records: [], // one {level, source, elapsedMs, usd} per model move, for the end card
    lowTickSecond: null,
  };
}

const aiName = (g) => modelLabel(g.model).split(" ")[0];

function remaining(g, color) {
  const running = !g.over && g.chess.turn() === color;
  return g.clocks[color] - (running ? performance.now() - g.turnStart : 0);
}

function renderClocks() {
  for (const [color, id] of [
    [game.aiColor, "ai-clock"],
    [game.humanColor, "human-clock"],
  ]) {
    const ms = remaining(game, color);
    const clock = $(id);
    const text = formatClock(ms);
    if (clock.textContent !== text) clock.textContent = text;
    clock.classList.toggle("active", !game.over && game.chess.turn() === color);
    clock.classList.toggle("low", ms < 10_000);
  }
}

function renderCost() {
  if (!serverStatus) return;
  const spent = game ? `This game ${formatUsd(game.spendUsd)} · ` : "";
  $("cost").textContent = `${spent}${formatUsd(serverStatus.spent_usd)} of ${formatUsd(serverStatus.cap_usd)}`;
}

async function refreshStatus() {
  try {
    serverStatus = await (await fetch("/api/status")).json();
  } catch {
    return;
  }
  $("mode-badge").hidden = !serverStatus.fake;
  renderCost();
  renderCostHint();
}

function tick() {
  if (!game || game.over) return;
  renderClocks();
  if (game.thinking) mind.tick(performance.now() - game.turnStart, game.plan);
  const human = remaining(game, game.humanColor);
  if (game.chess.turn() === game.humanColor && human < 10_000) {
    const second = Math.floor(human / 1000);
    if (second !== game.lowTickSecond && human > 0) sound.play("lowtime");
    game.lowTickSecond = second;
  }
  for (const color of ["w", "b"]) {
    if (game.chess.turn() === color && remaining(game, color) <= 0) {
      lostOnTime(color);
      return;
    }
  }
}

function lostOnTime(color) {
  if (color === game.humanColor) endGame("You lost on time.", "ai");
  else endGame(`${aiName(game)} lost on time. You win.`, "human");
}

function endGame(text, winner) {
  if (game.over) return;
  const g = game;
  g.abort?.abort();
  g.clocks[g.chess.turn()] = Math.max(0, remaining(g, g.chess.turn()));
  g.over = true;
  g.result = { text, winner };
  board.disableInput();
  board.clearGaze();
  if (g.thinking) mind.stop(winner === null ? text : "stopped");
  else mind.wait("");
  g.thinking = false;
  renderClocks();
  resetResign();
  $("resign").disabled = true;
  sound.play("end");
  setTimeout(() => game === g && showEndCard(g), END_CARD_DELAY_MS);
}

function gameOverResult(chess) {
  if (chess.isCheckmate()) {
    return chess.turn() === game.humanColor ? [`Checkmate. ${aiName(game)} wins.`, "ai"] : ["Checkmate. You win.", "human"];
  }
  if (chess.isStalemate()) return ["Draw by stalemate.", "draw"];
  if (chess.isInsufficientMaterial()) return ["Draw by insufficient material.", "draw"];
  if (chess.isThreefoldRepetition()) return ["Draw by threefold repetition.", "draw"];
  if (chess.isDrawByFiftyMoves()) return ["Draw by the fifty-move rule.", "draw"];
  return ["Draw.", "draw"];
}

function commitMove(move) {
  const mover = move.color;
  const now = performance.now();
  const left = game.clocks[mover] - (now - game.turnStart);
  game.clocks[mover] = left + game.incrementMs;
  game.turnStart = now;
  if (left <= 0) {
    game.clocks[mover] = 0;
    lostOnTime(mover);
    return;
  }
  game.uciMoves.push(move.from + move.to + (move.promotion ?? ""));
  if (game.chess.isGameOver()) {
    endGame(...gameOverResult(game.chess));
    return;
  }
  beginTurn();
}

function beginTurn() {
  game.turnStart = performance.now();
  game.lowTickSecond = null;
  renderClocks();
  if (game.chess.turn() === game.aiColor) {
    requestModelMove(game);
  } else {
    mind.wait(game.chess.inCheck() ? "Check. Your move." : "Your move.");
    board.enableInput(game.humanColor, (square) => game.chess.moves({ square, verbose: true }));
  }
}

/** Board highlights and sound for a move already played in game.chess. Resolves when the piece has landed. */
function showMove(move) {
  const chess = game.chess;
  board.markLastMove(move.from, move.to);
  board.markCheck(chess.inCheck() ? chess.findPiece({ type: "k", color: chess.turn() })[0] : null);
  return board.setPosition(chess.fen(), true).then(() => {
    for (const [event, delayMs] of soundCues(move)) sound.play(event, delayMs);
  });
}

function humanMove({ from, to, promotion }) {
  if (!game || game.over || game.chess.turn() !== game.humanColor) return;
  let move;
  try {
    move = game.chess.move({ from, to, promotion });
  } catch {
    board.setPosition(game.chess.fen(), true);
    return;
  }
  board.disableInput();
  showMove(move);
  commitMove(move);
}

function gazeAt(token) {
  if (!game?.thinking) return;
  if (token.square) board.glow(token.square);
  const move = resolveMove(token, game.legal);
  if (move) board.gazeArrow(move.from, move.to);
}

function handleEvent(g, ev) {
  switch (ev.type) {
    case "plan":
      g.plan = ev;
      mind.plan(ev);
      break;
    case "note": {
      mind.note(ev.text);
      const san = ev.text.match(/^fallback ready: (\S+)$/)?.[1];
      const backup = san && g.legal.find((m) => m.san === san);
      if (backup) board.ghost(backup.from, backup.to);
      break;
    }
    case "thought":
      mind.thought(ev.text);
      break;
    case "move":
      applyModelMove(g, ev);
      break;
    case "error":
      endGame(`The model could not move: ${ev.message}`, null);
      break;
  }
}

function applyModelMove(g, ev) {
  let move;
  try {
    move = g.chess.move({ from: ev.uci.slice(0, 2), to: ev.uci.slice(2, 4), promotion: ev.uci[4] });
  } catch {
    endGame(`The server sent an illegal move (${ev.uci}).`, null);
    return;
  }
  const level = ev.source === "forced" ? "forced" : (g.plan?.level ?? "none");
  g.records.push({ level, source: ev.source, elapsedMs: ev.elapsed_ms, usd: ev.usd });
  g.spendUsd += ev.usd;
  g.thinking = false;
  mind.commit({ label: moveLabel(g.uciMoves.length, ev.san), level, source: ev.source, elapsedMs: ev.elapsed_ms, usd: ev.usd });
  board.clearGaze();
  showMove(move).then(() => game === g && board.pulse(move.to));
  commitMove(move);
  refreshStatus();
}

async function requestModelMove(g) {
  const controller = new AbortController();
  g.abort = controller;
  g.plan = null;
  g.thinking = true;
  g.legal = g.chess.moves({ verbose: true });
  mind.think();
  const body = {
    moves: g.uciMoves,
    clock: {
      ai_ms: Math.max(0, Math.round(remaining(g, g.aiColor))),
      human_ms: Math.max(0, Math.round(remaining(g, g.humanColor))),
      increment_ms: g.incrementMs,
    },
    model: g.model,
  };
  let moved = false;
  try {
    const res = await fetch("/api/think", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(typeof detail.detail === "string" ? detail.detail : res.statusText);
    }
    for await (const ev of ndjsonLines(res)) {
      if (game !== g || g.over) return;
      moved ||= ev.type === "move";
      handleEvent(g, ev);
    }
    if (!moved && game === g && !g.over) endGame("The model stream ended without a move.", null);
  } catch (err) {
    if (controller.signal.aborted || game !== g || g.over) return;
    endGame(`Request failed: ${err.message}`, null);
  }
}

function pgnText(g) {
  const winnerColor = { human: g.humanColor, ai: g.aiColor }[g.result.winner];
  const result = g.result.winner === "draw" ? "1/2-1/2" : winnerColor === "w" ? "1-0" : winnerColor === "b" ? "0-1" : "*";
  const ai = modelLabel(g.model);
  const date = new Date().toISOString().slice(0, 10).replaceAll("-", ".");
  const headers = {
    Event: "zeitnot",
    Site: "local",
    Date: date,
    White: g.humanColor === "w" ? "You" : ai,
    Black: g.humanColor === "b" ? "You" : ai,
    Result: result,
    TimeControl: `${Math.round(g.settings.minutes * 60)}+${g.settings.increment}`,
  };
  for (const [key, value] of Object.entries(headers)) g.chess.setHeader(key, value);
  return g.chess.pgn();
}

function showEndCard(g) {
  const s = summarize(g.records);
  $("end-title").textContent = g.result.text;
  const rows = [
    ["Think time", s.moves ? `${s.thinkMs >= 60_000 ? formatClock(s.thinkMs) : formatSeconds(s.thinkMs)} total · ${formatSeconds(s.avgMs)} a move` : "no moves"],
    ["Levels", s.levels.map(([label, n]) => `${label} ${n}`).join(" · ") || "none"],
    ["Fallbacks", `${s.fallbacks}${s.emergencies ? ` · ${s.emergencies} emergency` : ""}`],
    ["Cost", formatUsd(g.spendUsd)],
  ];
  $("end-stats").replaceChildren(
    ...rows.flatMap(([label, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value;
      return [dt, dd];
    }),
  );
  $("copy-pgn").textContent = "Copy PGN";
  $("board-wrap").classList.add("ended");
  $("end-card").hidden = false;
  $("rematch").focus({ preventScroll: true });
}

function hideEndCard() {
  $("board-wrap").classList.remove("ended");
  $("end-card").hidden = true;
}

function startGame(settings) {
  game?.abort?.abort();
  game = newGame(settings);
  $("setup").hidden = true;
  $("game").hidden = false;
  $("game-controls").hidden = false;
  board?.destroy();
  board = new Board($("board"), { orientation: settings.humanColor, onMove: humanMove });
  hideEndCard();
  mind.reset();
  $("ai-name").textContent = modelLabel(settings.model);
  $("resign").disabled = false;
  resetResign();
  renderCost();
  sound.play("start");
  beginTurn();
}

function showSetup() {
  game?.abort?.abort();
  game = null;
  hideEndCard();
  $("game").hidden = true;
  $("game-controls").hidden = true;
  $("setup").hidden = false;
  refreshStatus();
}

let resignTimer = null;

function resetResign() {
  clearTimeout(resignTimer);
  resignTimer = null;
  $("resign").textContent = "Resign";
  $("resign").classList.remove("armed");
}

function onResign() {
  if (!game || game.over) return;
  if (resignTimer) {
    resetResign();
    endGame(`You resigned. ${aiName(game)} wins.`, "ai");
    return;
  }
  $("resign").textContent = "Confirm resign";
  $("resign").classList.add("armed");
  resignTimer = setTimeout(resetResign, 3000);
}

const setup = {
  minutes: prefs.get("minutes", DEFAULT_PRESET.minutes),
  increment: prefs.get("increment", DEFAULT_PRESET.increment),
  custom: prefs.get("custom", false),
  side: prefs.get("side", "random"),
};

function chip(text, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "chip";
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function renderCostHint() {
  $("cost-hint").textContent = serverStatus?.fake
    ? "Fake model: no API calls, no cost."
    : `${describeCost(setup.minutes, setup.increment)} on Gemini Flash`;
}

function renderSetup() {
  const presetMatch = (p) => !setup.custom && p.minutes === setup.minutes && p.increment === setup.increment;
  for (const button of $("presets").children) {
    const p = PRESETS[Number(button.dataset.index)];
    button.setAttribute("aria-pressed", String(p ? presetMatch(p) : setup.custom));
  }
  for (const button of $("sides").children) button.setAttribute("aria-pressed", String(button.dataset.side === setup.side));
  $("custom").hidden = !setup.custom;
  $("minutes").value = setup.minutes;
  $("increment").value = setup.increment;
  renderCostHint();
  for (const key of ["minutes", "increment", "custom", "side"]) prefs.set(key, setup[key]);
}

function buildSetup() {
  if (!setup.custom && !PRESETS.some((p) => p.minutes === setup.minutes && p.increment === setup.increment)) setup.custom = true;
  $("presets").append(
    ...PRESETS.map((p, i) => {
      const button = chip(`${p.minutes}+${p.increment}`, () => {
        Object.assign(setup, { minutes: p.minutes, increment: p.increment, custom: false });
        renderSetup();
      });
      button.dataset.index = i;
      return button;
    }),
    chip("custom", () => {
      setup.custom = true;
      renderSetup();
      $("minutes").focus();
    }),
  );
  $("sides").append(
    ...SIDES.map(([side, label]) => {
      const button = chip(label, () => {
        setup.side = side;
        renderSetup();
      });
      button.dataset.side = side;
      return button;
    }),
  );
  for (const key of ["minutes", "increment"]) {
    $(key).addEventListener("input", () => {
      const value = Number($(key).value);
      if (Number.isFinite(value) && value >= Number($(key).min)) {
        setup[key] = value;
        renderCostHint();
        prefs.set(key, value);
      }
    });
  }
  $("play").addEventListener("click", () => {
    const humanColor = setup.side === "random" ? (Math.random() < 0.5 ? "w" : "b") : setup.side === "white" ? "w" : "b";
    prefs.set("model", $("model").value);
    startGame({ minutes: setup.minutes, increment: setup.increment, humanColor, model: $("model").value });
  });
  renderSetup();
}

function fillModels() {
  const select = $("model");
  const preferred = prefs.get("model", null);
  const models = serverStatus?.models ?? [];
  select.replaceChildren(
    ...models.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = modelLabel(name);
      option.selected = name === (models.includes(preferred) ? preferred : serverStatus.default_model);
      return option;
    }),
  );
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  for (const button of document.querySelectorAll("[data-theme-choice]")) {
    button.setAttribute("aria-pressed", String(button.dataset.themeChoice === theme));
  }
}

function buildPrefs() {
  applyTheme(THEMES.includes(document.documentElement.dataset.theme) ? document.documentElement.dataset.theme : THEMES[0]);
  for (const button of document.querySelectorAll("[data-theme-choice]")) {
    button.addEventListener("click", () => {
      applyTheme(button.dataset.themeChoice);
      prefs.set("theme", button.dataset.themeChoice);
    });
  }
  const toggle = $("sound-toggle");
  const volume = $("volume");
  const render = () => {
    toggle.setAttribute("aria-pressed", String(sound.enabled));
    volume.disabled = !sound.enabled;
  };
  volume.value = sound.volume;
  toggle.addEventListener("click", () => {
    sound.setEnabled(!sound.enabled);
    prefs.set("sound", sound.enabled);
    render();
  });
  volume.addEventListener("input", () => {
    sound.setVolume(Number(volume.value));
    prefs.set("volume", sound.volume);
  });
  render();
  const unlock = () => sound.unlock();
  document.addEventListener("pointerdown", unlock, { capture: true });
  document.addEventListener("keydown", unlock, { capture: true });
}

async function init() {
  buildPrefs();
  buildSetup();
  await refreshStatus();
  fillModels();
  $("resign").addEventListener("click", onResign);
  $("new-game").addEventListener("click", showSetup);
  $("again").addEventListener("click", showSetup);
  $("rematch").addEventListener("click", () => {
    const s = game.settings;
    startGame({ ...s, humanColor: game.humanColor === "w" ? "b" : "w" });
  });
  $("copy-pgn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(pgnText(game));
      $("copy-pgn").textContent = "Copied";
    } catch {
      $("copy-pgn").textContent = "Copy failed";
    }
  });
  setInterval(tick, 100);
}

init();
