import { extractGaze } from "./gaze.js";
import { LEVEL_LABEL, budgetRing, describePlan, formatSeconds, formatUsd, parseThought } from "./util.js";

const MS_PER_CHAR = 2;
const MAX_REVEAL_MS = 600;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/**
 * The mind panel: the orb and its budget ring, the plan line, the live thought stream and the history rows.
 * A view only: the game state lives in app.js, which calls think, plan, note, thought, tick and commit or stop.
 */
export class Mind {
  constructor(root, { onGaze }) {
    this.onGaze = onGaze;
    this.orb = root.querySelector(".orb");
    this.ringFill = root.querySelector(".ring-fill");
    this.planLine = root.querySelector(".plan");
    this.stream = root.querySelector(".stream");
    this.history = root.querySelector(".history");
    this.turn = null; // the element collecting the current move's thoughts and notes
    this.reveal = null; // the chunk being typed out
  }

  reset() {
    this.finishReveal({ gaze: false });
    this.turn = null;
    this.history.replaceChildren();
    this.wait("");
  }

  /** An empty stream with one quiet line, for the player's turn and the end of a game. */
  wait(text) {
    this.setOrb("idle", 0, "calm");
    this.planLine.textContent = "";
    this.stream.replaceChildren(el("p", "quiet", text));
  }

  think() {
    this.finishReveal({ gaze: false });
    this.turn = el("div", "turn");
    this.turn.append(el("p", "quiet waiting", "Reading the position"));
    this.stream.replaceChildren(this.turn);
    this.planLine.textContent = "";
    this.setOrb("thinking", 0, "calm");
  }

  plan(plan) {
    this.planLine.textContent = describePlan(plan);
  }

  tick(elapsedMs, plan) {
    const { fraction, tone } = plan ? budgetRing(elapsedMs, plan.budget_ms, plan.deadline_ms) : { fraction: 0, tone: "calm" };
    this.setOrb("thinking", fraction, tone);
  }

  note(text) {
    this.append(el("p", "note", text));
  }

  thought(text) {
    this.finishReveal({ gaze: true });
    const chunk = el("div", "chunk");
    const spans = [];
    let offset = 0;
    for (const block of parseThought(text)) {
      const p = el("p", block.heading ? "h" : "");
      for (const span of block.spans) {
        const node = el(span.strong ? "strong" : "span");
        p.append(node);
        spans.push({ node, text: span.text, start: offset });
        offset += span.text.length;
      }
      chunk.append(p);
      offset += 1; // the "\n" between blocks in the visible text
    }
    const visible = spans.reduce((acc, s) => acc.padEnd(s.start) + s.text, "");
    this.append(chunk);
    this.startReveal({ spans, total: visible.length, tokens: extractGaze(visible), shown: 0, fired: 0 });
  }

  commit({ label, level, source, elapsedMs, usd }) {
    this.finishReveal({ gaze: false });
    const turn = this.turn ?? el("div", "turn");
    this.turn = null;
    turn.querySelector(".waiting")?.remove();
    const row = el("details", "row enter");
    const summary = el("summary");
    const meta = level === "forced" ? [LEVEL_LABEL.forced] : [LEVEL_LABEL[level], formatSeconds(elapsedMs), formatUsd(usd)];
    summary.append(el("span", "san", label), el("span", "meta", meta.map((m) => ` · ${m}`).join("")));
    if (source === "fallback" || source === "emergency") summary.append(el("span", `tag ${source}`, source));
    row.append(summary, turn);
    this.history.prepend(row);
    this.wait("");
  }

  /** The game ended while the model was thinking. Its partial thoughts stay in the stream. */
  stop(text) {
    this.finishReveal({ gaze: false });
    this.turn?.querySelector(".waiting")?.remove();
    this.note(text);
    this.turn = null;
    this.setOrb("idle", 0, "calm");
  }

  setOrb(state, fraction, tone) {
    this.orb.dataset.state = state;
    this.orb.dataset.tone = tone;
    this.ringFill.style.strokeDashoffset = String(1 - fraction);
  }

  append(node) {
    const pinned = this.isPinned();
    this.turn?.querySelector(".waiting")?.remove();
    (this.turn ?? this.stream).append(node);
    if (pinned) this.stream.scrollTop = this.stream.scrollHeight;
  }

  isPinned() {
    const s = this.stream;
    return s.scrollHeight - s.scrollTop - s.clientHeight < 48;
  }

  // Gaze fires as each token is typed out, so the board looks where the text is.

  startReveal(reveal) {
    const duration = reducedMotion.matches ? 0 : Math.min(reveal.total * MS_PER_CHAR, MAX_REVEAL_MS);
    const started = performance.now();
    this.reveal = reveal;
    const step = (now) => {
      if (this.reveal !== reveal) return;
      this.show(reveal, duration ? Math.ceil((reveal.total * (now - started)) / duration) : reveal.total, true);
      if (reveal.shown < reveal.total) reveal.frame = requestAnimationFrame(step);
      else this.reveal = null;
    };
    step(started);
  }

  finishReveal({ gaze }) {
    const reveal = this.reveal;
    if (!reveal) return;
    cancelAnimationFrame(reveal.frame);
    this.reveal = null;
    this.show(reveal, reveal.total, gaze);
  }

  show(reveal, count, gaze) {
    const n = Math.min(count, reveal.total);
    const pinned = this.isPinned();
    for (const s of reveal.spans) {
      if (s.start + s.text.length < reveal.shown || s.start > n) continue;
      s.node.textContent = s.text.slice(0, Math.max(0, n - s.start));
    }
    reveal.shown = n;
    while (reveal.fired < reveal.tokens.length && reveal.tokens[reveal.fired].end <= n) {
      const token = reveal.tokens[reveal.fired++];
      if (gaze) this.onGaze(token);
    }
    if (pinned) this.stream.scrollTop = this.stream.scrollHeight;
  }
}
