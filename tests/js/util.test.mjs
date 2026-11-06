import test from "node:test";
import assert from "node:assert/strict";
import {
  budgetRing,
  defaultPreset,
  describePlan,
  describeRegime,
  describeSense,
  elapsedText,
  engineHint,
  formatClock,
  modelLabel,
  median,
  moveLabel,
  ndjsonLines,
  parseThought,
  soundCues,
  sum,
  summarize,
  totalTime,
} from "../../web/util.js";

test("parseThought splits a Gemini summary into a heading and a body", () => {
  assert.deepEqual(parseThought("**Analyzing the Position**\n\nI see Bb4 pinning c3.\n\n\n"), [
    { heading: true, spans: [{ text: "Analyzing the Position", strong: false }] },
    { heading: false, spans: [{ text: "I see Bb4 pinning c3.", strong: false }] },
  ]);
});

test("parseThought keeps inline bold as a strong span and joins single newlines", () => {
  assert.deepEqual(parseThought("Play **Nf3** now\nor later"), [
    {
      heading: false,
      spans: [
        { text: "Play ", strong: false },
        { text: "Nf3", strong: true },
        { text: " now or later", strong: false },
      ],
    },
  ]);
});

test("parseThought leaves an unclosed ** literal and markup as plain text", () => {
  assert.deepEqual(parseThought("**Half"), [{ heading: false, spans: [{ text: "**Half", strong: false }] }]);
  assert.equal(parseThought("<img src=x onerror=alert(1)>")[0].spans[0].text, "<img src=x onerror=alert(1)>");
});

test("formatClock", () => {
  assert.equal(formatClock(600_000), "10:00");
  assert.equal(formatClock(59_001), "1:00");
  assert.equal(formatClock(4_321), "4.4");
  assert.equal(formatClock(-5), "0.0");
});

test("moveLabel", () => {
  assert.equal(moveLabel(0, "e4"), "1. e4");
  assert.equal(moveLabel(1, "e5"), "1... e5");
  assert.equal(moveLabel(23, "Nf6"), "12... Nf6");
});

test("describePlan reads like the spec's plan line", () => {
  assert.equal(describePlan({ level: "medium", budget_ms: 19_000 }), "medium thinking · 19 s budget");
  assert.equal(describePlan({ level: "none", budget_ms: 1_500 }), "fallback only · 1.5 s budget");
  assert.equal(describePlan({ level: "xhigh", budget_ms: 61_000, difficulty: "hard" }), "hard · xhigh thinking · 61 s budget");
});

test("describeRegime names the usual level first, then how hard and easy moves differ", () => {
  assert.equal(describeRegime({ easy: "low", normal: "high", hard: "xhigh" }, 10, 5),
    "On 10+5 it thinks at high, xhigh on hard moves and low on easy ones.");
  assert.equal(describeRegime({ easy: "low", normal: "low", hard: "medium" }, 3, 2), "On 3+2 it thinks at low, medium on hard moves.");
  assert.equal(describeRegime({ easy: "low", normal: "low", hard: "low" }, 1, 0), "On 1+0 it thinks at low on every move.");
  assert.equal(describeRegime(undefined, 7, 7), "");
  assert.equal(describeRegime({ easy: "low", normal: "low", hard: "high", second: "xhigh" }, 5, 3),
    "On 5+3 it thinks at low, high on hard moves with an xhigh second opinion.");
  assert.equal(describeRegime({ easy: "low", normal: "low", hard: "medium", second: "high" }, 3, 2),
    "On 3+2 it thinks at low, medium on hard moves with a high second opinion.");
  assert.equal(describeRegime({ easy: "medium", normal: "high", hard: "high", second: "xhigh" }, 10, 5),
    "On 10+5 it thinks at high, with an xhigh second opinion on hard moves and medium on easy ones.");
});

test("engineHint shows the speed line and the clock's levels, and no cost", () => {
  const hint = "Claude Opus takes about 8 s on an easy move and longer on hard ones; it can lose on time at 1+0.";
  const regimes = { "3+2": { easy: "low", normal: "low", hard: "medium" } };
  assert.equal(engineHint({ name: "claude", label: "Claude Opus", hint, regimes }, 3, 2), `${hint} On 3+2 it thinks at low, medium on hard moves.`);
  assert.equal(engineHint({ name: "claude", label: "Claude Opus", hint, regimes }, 7, 7), hint, "a clock with no regime shows the speed line only");
  assert.equal(engineHint({ name: "gemini", label: "Gemini 3.6 Flash", hint: "" }, 10, 5), "");
  assert.equal(engineHint(null, 10, 5), "");
});

test("defaultPreset takes the opponent's time control, else 10+5", () => {
  assert.deepEqual(defaultPreset({ time_control: { minutes: 15, increment: 10 } }), { minutes: 15, increment: 10 });
  assert.deepEqual(defaultPreset(null), { minutes: 10, increment: 5 });
});

test("elapsedText", () => {
  assert.equal(elapsedText(4_230, 12_400), "4.2 s of 12 s");
  assert.equal(elapsedText(800, 3_300), "0.8 s of 3.3 s");
});

test("budgetRing fills over the budget, then turns amber, then red near the deadline", () => {
  assert.deepEqual(budgetRing(5_000, 10_000, 20_000), { fraction: 0.5, tone: "calm" });
  assert.deepEqual(budgetRing(12_000, 10_000, 20_000), { fraction: 1, tone: "over" });
  assert.deepEqual(budgetRing(18_000, 10_000, 20_000), { fraction: 1, tone: "critical" });
});

test("modelLabel", () => {
  assert.equal(modelLabel("gemini-3.6-flash"), "Gemini 3.6 Flash");
});

test("summarize counts levels deepest first, fallbacks and prepared moves, and no cost", () => {
  const s = summarize([
    { level: "low", difficulty: "easy", source: "model", elapsedMs: 4_000 },
    { level: "xhigh", difficulty: "hard", source: "model", elapsedMs: 40_000 },
    { level: "high", source: "fallback", elapsedMs: 20_000 },
    { level: "forced", source: "forced", elapsedMs: 0 },
    { level: "low", source: "model", elapsedMs: 0, ponderHit: true },
  ]);
  assert.deepEqual(s.levels, [["xhigh", 1], ["high", 1], ["low", 2], ["forced", 1]]);
  assert.equal(s.thinkMs, 64_000);
  assert.equal(s.avgMs, 12_800);
  assert.equal(s.fallbacks, 1);
  assert.equal(s.prepared, 1);
  assert.equal(s.hard, 1);
  assert.equal("usd" in s, false);
});

test("describeSense reads the plan as the difficulty, Maia's blunder risk and the effort", () => {
  assert.equal(describeSense({ difficulty: "hard", blunder_chance: 0.337, level: "high" }), "hard · 34% blunder risk · high");
  assert.equal(describeSense({ difficulty: "easy", blunder_chance: 0.004, level: "low" }), "easy · <1% blunder risk · low");
  assert.equal(describeSense({ difficulty: "normal", blunder_chance: null, level: "medium" }), "normal · medium");
  assert.equal(describeSense({ difficulty: "hard", blunder_chance: 0.39, level: "high", second_level: "xhigh" }), "hard · 39% blunder risk · high + xhigh");
  assert.equal(describeSense({ difficulty: "hard", blunder_chance: 0.39, level: "xhigh", second_level: "xhigh" }), "hard · 39% blunder risk · xhigh");
});

test("median, sum and totalTime", () => {
  assert.equal(median([]), null);
  assert.equal(median([3_000, 1_000, 2_000]), 2_000);
  assert.equal(median([1_000, 4_000]), 2_500);
  assert.equal(sum([1, 2, 3]), 6);
  assert.equal(totalTime(48_200), "48.2 s");
  assert.equal(totalTime(185_000), "3:05");
});

test("soundCues: castling taps twice, a checking capture is capture then check, mate leaves the cue to game end", () => {
  assert.deepEqual(soundCues({ flags: "k", san: "O-O" }), [["move", 0], ["move", 90]]);
  assert.deepEqual(soundCues({ flags: "c", san: "Bxf7+" }), [["capture", 0], ["check", 110]]);
  assert.deepEqual(soundCues({ flags: "e", san: "exd6" }), [["capture", 0]]);
  assert.deepEqual(soundCues({ flags: "cp", san: "bxa8=Q" }), [["promote", 0]]);
  assert.deepEqual(soundCues({ flags: "n", san: "Qxf7#".replace("x", "") }), [["move", 0]]);
});

test("ndjsonLines reassembles lines split across chunks", async () => {
  const enc = new TextEncoder();
  const chunks = ['{"type":"pl', 'an","a":1}\n{"type":"note"', ',"text":"é"}\n', '{"type":"move"}'];
  const body = new ReadableStream({
    start(c) {
      for (const ch of chunks) c.enqueue(enc.encode(ch));
      c.close();
    },
  });
  const out = [];
  for await (const ev of ndjsonLines(new Response(body))) out.push(ev);
  assert.deepEqual(out, [{ type: "plan", a: 1 }, { type: "note", text: "é" }, { type: "move" }]);
});
