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

/** The middle value in ms, or null for no values. */
export function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return Math.round(sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2);
}

export const sum = (values) => values.reduce((total, v) => total + v, 0);

/** "48.2 s" under a minute, "3:05" from a minute on. */
export function totalTime(ms) {
  return ms >= 60_000 ? formatClock(ms) : formatSeconds(ms);
}

export function moveLabel(ply, san) {
  const number = Math.floor(ply / 2) + 1;
  return ply % 2 === 0 ? `${number}. ${san}` : `${number}... ${san}`;
}

// Deepest first: the end card lists the levels a game used in this order.
export const LEVEL_LABEL = {
  max: "max",
  xhigh: "xhigh",
  high: "high",
  medium: "medium",
  low: "low",
  minimal: "minimal",
  none: "fallback only",
  forced: "forced",
};

export function describePlan({ level, budget_ms, difficulty }) {
  const seconds = budget_ms >= 10_000 ? Math.round(budget_ms / 1000) : (budget_ms / 1000).toFixed(1);
  const what = level === "none" ? "fallback only" : `${level} thinking`;
  return `${difficulty ? `${difficulty} · ` : ""}${what} · ${seconds} s budget`;
}

/** The line under the model's name: "hard · 34% blunder risk · high + xhigh", the second opinion's level last. */
export function describeSense({ difficulty, blunder_chance: chance, level, second_level: second }) {
  const risk = chance == null ? "" : ` · ${chance < 0.01 ? "<1%" : `${Math.round(chance * 100)}%`} blunder risk`;
  const deeper = second && second !== level ? ` + ${LEVEL_LABEL[second] ?? second}` : "";
  return `${difficulty}${risk} · ${LEVEL_LABEL[level] ?? level}${deeper}`;
}

/** "On 5+3 it thinks at low, high on hard moves with an xhigh second opinion." from one clock's {easy, normal, hard, second} levels. */
export function describeRegime(regime, minutes, increment) {
  if (!regime) return "";
  const { easy, normal, hard, second } = regime;
  const article = /^[aeiox]/.test(second ?? "") ? "an" : "a"; // "an xhigh", said "ex-high"
  const opinion = second && second !== hard ? `${article} ${second} second opinion` : "";
  const onHard = hard !== normal ? `${hard} on hard moves${opinion ? ` with ${opinion}` : ""}` : opinion ? `with ${opinion} on hard moves` : "";
  const onEasy = easy !== normal ? `${easy} on easy ones` : "";
  const extra = [onHard, onEasy].filter(Boolean);
  if (!extra.length) return `On ${minutes}+${increment} it thinks at ${normal} on every move.`;
  return `On ${minutes}+${increment} it thinks at ${normal}, ${extra.join(" and ")}.`;
}

/** The start screen's line under the time control for one opponent from /api/status: its speed and how deep it thinks
 * on that clock. */
export function engineHint(engine, minutes, increment) {
  if (!engine) return "";
  const regime = describeRegime(engine.regimes?.[`${minutes}+${increment}`], minutes, increment);
  return [engine.hint, regime].filter(Boolean).join(" ");
}

/** The time control the start screen picks for an opponent the player has not set one for. */
export function defaultPreset(engine) {
  const tc = engine?.time_control;
  return tc ? { minutes: tc.minutes, increment: tc.increment } : { ...DEFAULT_PRESET };
}

/** The counter beside the budget ring: "4.2 s of 12 s". */
export function elapsedText(elapsedMs, budgetMs) {
  const budget = budgetMs >= 10_000 ? Math.round(budgetMs / 1000) : (budgetMs / 1000).toFixed(1);
  return `${(Math.max(0, elapsedMs) / 1000).toFixed(1)} s of ${budget} s`;
}

/** The budget ring: how full it is, and its tone. Amber past the budget, red in the last quarter before the deadline. */
export function budgetRing(elapsedMs, budgetMs, deadlineMs) {
  const fraction = budgetMs > 0 ? Math.min(1, Math.max(0, elapsedMs / budgetMs)) : 1;
  if (elapsedMs < budgetMs) return { fraction, tone: "calm" };
  const redAt = deadlineMs - 0.25 * (deadlineMs - budgetMs);
  return { fraction, tone: elapsedMs < redAt ? "over" : "critical" };
}

/** "gemini-3.6-flash" to "Gemini 3.6 Flash". */
export function modelLabel(id) {
  return id
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** End-card numbers from the model's move records [{level, difficulty, source, elapsedMs, ponderHit}]. */
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
    prepared: records.filter((r) => r.ponderHit).length,
    hard: records.filter((r) => r.difficulty === "hard").length,
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
