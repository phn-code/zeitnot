import test from "node:test";
import assert from "node:assert/strict";
import {
  budgetRing,
  describeCost,
  describePlan,
  formatClock,
  formatUsd,
  modelLabel,
  moveLabel,
  ndjsonLines,
  openingLevel,
  parseThought,
  soundCues,
  summarize,
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

test("formatUsd drops trailing zeros under ten cents", () => {
  assert.equal(formatUsd(0.004), "$0.004");
  assert.equal(formatUsd(0.0091), "$0.0091");
  assert.equal(formatUsd(0.4409), "$0.44");
});

test("moveLabel", () => {
  assert.equal(moveLabel(0, "e4"), "1. e4");
  assert.equal(moveLabel(1, "e5"), "1... e5");
  assert.equal(moveLabel(23, "Nf6"), "12... Nf6");
});

test("describePlan reads like the spec's plan line", () => {
  assert.equal(describePlan({ level: "medium", budget_ms: 19_000 }), "medium thinking · 19 s budget");
  assert.equal(describePlan({ level: "none", budget_ms: 1_500 }), "fallback only · 1.5 s budget");
});

test("budgetRing fills over the budget, then turns amber, then red near the deadline", () => {
  assert.deepEqual(budgetRing(5_000, 10_000, 20_000), { fraction: 0.5, tone: "calm" });
  assert.deepEqual(budgetRing(12_000, 10_000, 20_000), { fraction: 1, tone: "over" });
  assert.deepEqual(budgetRing(18_000, 10_000, 20_000), { fraction: 1, tone: "critical" });
});

test("openingLevel matches the time manager's first plan for each preset", () => {
  assert.deepEqual(
    [[1, 0], [3, 2], [5, 3], [10, 5], [15, 10]].map(([m, i]) => openingLevel(m, i)),
    ["none", "low", "low", "medium", "medium"],
  );
});

test("describeCost gives a per-game range", () => {
  assert.equal(describeCost(10, 5), "about $0.28 to $0.47 per game");
  assert.equal(describeCost(3, 2), "about $0.09 to $0.14 per game");
  assert.equal(describeCost(1, 0), "about $0.01 per game");
});

test("modelLabel", () => {
  assert.equal(modelLabel("gemini-3.6-flash"), "Gemini 3.6 Flash");
});

test("summarize counts levels in a fixed order, fallbacks and cost", () => {
  const s = summarize([
    { level: "low", source: "model", elapsedMs: 4_000, usd: 0.003 },
    { level: "medium", source: "model", elapsedMs: 12_000, usd: 0.009 },
    { level: "medium", source: "fallback", elapsedMs: 20_000, usd: 0.01 },
    { level: "forced", source: "forced", elapsedMs: 0, usd: 0 },
  ]);
  assert.deepEqual(s.levels, [["medium", 2], ["low", 1], ["forced", 1]]);
  assert.equal(s.thinkMs, 36_000);
  assert.equal(s.avgMs, 9_000);
  assert.equal(s.fallbacks, 1);
  assert.equal(s.emergencies, 0);
  assert.ok(Math.abs(s.usd - 0.022) < 1e-12);
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
