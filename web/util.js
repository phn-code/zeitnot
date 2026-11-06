export const PRESETS = [
  { minutes: 1, increment: 0 },
  { minutes: 3, increment: 2 },
  { minutes: 5, increment: 3 },
  { minutes: 10, increment: 5 },
  { minutes: 15, increment: 10 },
];
export const DEFAULT_PRESET = PRESETS[3];

/**
 * One thought chunk as blocks for the stream: [{heading, spans: [{text, strong}]}].
 * A paragraph that is all `**bold**` is a heading. Inline `**bold**` becomes a strong span, an unclosed `**` stays literal.
 */
export function parseThought(text) {
  return text
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => {
      const heading = p.match(/^\*\*([^*]+)\*\*$/);
      if (heading) return { heading: true, spans: [{ text: heading[1].trim(), strong: false }] };
      const spans = p
        .replace(/\s*\n\s*/g, " ")
        .split(/\*\*(.+?)\*\*/)
        .map((t, i) => ({ text: t, strong: i % 2 === 1 }))
        .filter((s) => s.text);
      return { heading: false, spans };
    });
}

export function formatClock(ms) {
  const clamped = Math.max(0, ms);
  if (clamped < 10_000) return (Math.ceil(clamped / 100) / 10).toFixed(1);
  const total = Math.ceil(clamped / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function formatSeconds(ms) {
  return `${(ms / 1000).toFixed(1)} s`;
}

export function formatUsd(usd) {
  return usd >= 0.1 ? `$${usd.toFixed(2)}` : `$${Number(usd.toFixed(4))}`;
}

export function moveLabel(ply, san) {
  const number = Math.floor(ply / 2) + 1;
  return ply % 2 === 0 ? `${number}. ${san}` : `${number}... ${san}`;
}

export const LEVEL_LABEL = { medium: "medium", low: "low", minimal: "minimal", none: "fallback only", forced: "forced" };

export function describePlan({ level, budget_ms }) {
  const seconds = budget_ms >= 10_000 ? Math.round(budget_ms / 1000) : (budget_ms / 1000).toFixed(1);
  const what = level === "none" ? "fallback only" : `${level} thinking`;
  return `${what} · ${seconds} s budget`;
}

/** The budget ring: how full it is, and its tone. Amber past the budget, red in the last quarter before the deadline. */
export function budgetRing(elapsedMs, budgetMs, deadlineMs) {
  const fraction = budgetMs > 0 ? Math.min(1, Math.max(0, elapsedMs / budgetMs)) : 1;
  if (elapsedMs < budgetMs) return { fraction, tone: "calm" };
  const redAt = deadlineMs - 0.25 * (deadlineMs - budgetMs);
  return { fraction, tone: elapsedMs < redAt ? "over" : "critical" };
}

// The first move's plan level per time control, mirroring zeitnot/timeman.py and the p50 latencies in zeitnot/config.py.
const MAIN_LEVEL_P50_MS = [
  ["medium", 12_100],
  ["low", 4_200],
];
// Measured cost per move (experiments/p0_probe/FINDINGS.md): the main call plus the minimal fallback call.
const MOVE_USD = { medium: 0.0091 + 0.0003, low: 0.0026 + 0.0003, none: 0.0003 };
const MODEL_MOVES_PER_GAME = [30, 50];

export function openingLevel(minutes, increment) {
  const ms = minutes * 60_000;
  if (ms < 10_000) return "none";
  const budget = Math.min(ms / 40 + 0.8 * increment * 1000, 0.25 * ms);
  return MAIN_LEVEL_P50_MS.find(([, p50]) => p50 <= budget)?.[0] ?? "none";
}

export function describeCost(minutes, increment) {
  const perMove = MOVE_USD[openingLevel(minutes, increment)];
  const [low, high] = MODEL_MOVES_PER_GAME.map((n) => `$${(n * perMove).toFixed(2)}`);
  return low === high ? `about ${low} per game` : `about ${low} to ${high} per game`;
}

/** "gemini-3.6-flash" to "Gemini 3.6 Flash". */
export function modelLabel(id) {
  return id
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** End-card numbers from the model's move records [{level, source, elapsedMs, usd}]. */
export function summarize(records) {
  const thinkMs = records.reduce((sum, r) => sum + r.elapsedMs, 0);
  const levels = new Map();
  for (const r of records) levels.set(r.level, (levels.get(r.level) ?? 0) + 1);
  return {
    moves: records.length,
    thinkMs,
    avgMs: records.length ? thinkMs / records.length : 0,
    levels: Object.keys(LEVEL_LABEL)
      .filter((l) => levels.has(l))
      .map((l) => [LEVEL_LABEL[l], levels.get(l)]),
    fallbacks: records.filter((r) => r.source === "fallback").length,
    emergencies: records.filter((r) => r.source === "emergency").length,
    usd: records.reduce((sum, r) => sum + r.usd, 0),
  };
}

/** Sound cues for a chess.js verbose move as [event, delayMs]. Mate gets no check cue: the game-end sound follows. */
export function soundCues({ flags, san }) {
  const cues = /[kq]/.test(flags)
    ? [["move", 0], ["move", 90]]
    : flags.includes("p")
      ? [["promote", 0]]
      : /[ce]/.test(flags)
        ? [["capture", 0]]
        : [["move", 0]];
  if (san.endsWith("+")) cues.push(["check", cues.at(-1)[1] + 110]);
  return cues;
}

export async function* ndjsonLines(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) yield JSON.parse(line);
    }
  }
  const tail = (buffer + decoder.decode()).trim();
  if (tail) yield JSON.parse(tail);
}
