import { applyCall, ariaLabel, arrowTone, assignTones, chip, emptyTree, isLive, splitLabel, treeRows } from "./calltree.js";
import { extractGaze, settledTokens } from "./gaze.js";
import { LEVEL_LABEL, budgetRing, describePlan, describeSense, elapsedText, formatSeconds, parseThought } from "./util.js";

const MS_PER_CHAR = 2;
const MAX_REVEAL_MS = 600;
const SPARKS = 24;
const SPARK_RADIUS = 17.2;
const SPARK_DECAY_MS = 1700;
const SPARK_REST = 0.16; // style.css gives resting sparks this opacity while thinking
const CARD_SPARKS = 8;
const SETTLE_MS = 900;
const COLLAPSE_AFTER_MS = 900;
const REPLAY_STAGGER_MS = 70;
const PINNED_PX = 48;
// The cards already show these: the backup card answers, and a failed card says so.
const SHOWN_BY_CARDS = /^fallback ready: |^\w[\w ]* call failed: /;
const SVG = "http://www.w3.org/2000/svg";
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/** A ring of `n` points of radius `r` in a `size` box. Each pulse lights the next point, so real activity turns it. */
function sparkRing(group, n, r, size, dotR) {
  return Array.from({ length: n }, (_, i) => {
    const angle = (2 * Math.PI * i) / n - Math.PI / 2;
    const dot = document.createElementNS(SVG, "circle");
    dot.setAttribute("class", "spark");
    dot.setAttribute("cx", String(size / 2 + r * Math.cos(angle)));
    dot.setAttribute("cy", String(size / 2 + r * Math.sin(angle)));
    dot.setAttribute("r", String(dotR));
    group.append(dot);
    return dot;
  });
}

function flare(dot, restOpacity, big, rest) {
  dot.animate(
    [
      { opacity: 1, r: big },
      { opacity: 0.55, offset: 0.25 },
      { opacity: restOpacity, r: rest },
    ],
    { duration: SPARK_DECAY_MS, easing: "cubic-bezier(0.2, 0.6, 0.3, 1)" },
  );
}

/** Text that a replay may deliver again from the start. Repeated pieces are skipped until the replay catches up. */
class Replayable {
  constructor() {
    this.pieces = [];
    this.cursor = null; // index into pieces while a replay is catching up
  }

  /** True when `text` is new and should be shown. */
  accept(text) {
    if (this.cursor !== null && this.cursor < this.pieces.length && this.pieces[this.cursor] === text) {
      this.cursor += 1;
      return false;
    }
    this.cursor = null;
    this.pieces.push(text);
    return true;
  }

  rewind() {
    this.cursor = 0;
  }
}

/** One model call: header (pulse ring, role and label, status chip, timer), engine and level, and its commentary. */
class Card {
  constructor(view, node) {
    this.view = view;
    this.id = node.id;
    this.startedAt = performance.now() - node.elapsedMs;
    this.expanded = true;
    this.pinnedOpen = false; // the player opened or closed it; automatic collapsing leaves it alone
    this.collapseTimer = null;
    this.commentaryText = new Replayable();
    this.chunkText = new Replayable();
    this.textNode = null;
    this.spoken = ""; // the commentary so far, for gaze
    this.fired = 0;
    this.reveal = null;
    this.head = 0;

    this.root = el("article", "call enter");
    this.root.tabIndex = -1;
    this.rails = el("span", "rails");
    this.rails.setAttribute("aria-hidden", "true");
    const card = el("div", "card");
    this.button = el("button", "call-head");
    this.button.type = "button";
    const ring = document.createElementNS(SVG, "svg");
    ring.setAttribute("class", "mini-orb");
    ring.setAttribute("viewBox", "0 0 16 16");
    ring.setAttribute("aria-hidden", "true");
    const group = document.createElementNS(SVG, "g");
    ring.append(group);
    this.sparks = sparkRing(group, CARD_SPARKS, 6.2, 16, 1.05);
    const core = document.createElementNS(SVG, "circle");
    core.setAttribute("class", "mini-core");
    core.setAttribute("cx", "8");
    core.setAttribute("cy", "8");
    core.setAttribute("r", "2.6");
    ring.append(core);
    this.title = el("span", "call-title");
    this.framing = el("span", "call-framing");
    const titles = el("span", "call-titles");
    titles.append(this.title, this.framing);
    this.chip = el("span", "status");
    this.time = el("span", "call-time");
    this.button.append(ring, titles, this.chip, this.time);
    this.meta = el("p", "call-meta");
    this.body = el("div", "call-body");
    this.inner = el("div", "call-inner");
    this.body.append(this.inner);
    card.append(this.button, this.meta, this.body);
    this.root.append(this.rails, card);
    this.button.addEventListener("click", () => this.toggle());
    this.root.addEventListener("pointerenter", () => view.focus(this.id));
    this.root.addEventListener("pointerleave", () => view.focus(null));
    this.button.addEventListener("focus", () => view.focus(this.id));
    this.button.addEventListener("blur", () => view.focus(null));
  }

  render(node, tone, row) {
    const r = this.root;
    r.dataset.role = node.role;
    r.dataset.status = node.status;
    r.dataset.tone = tone;
    r.dataset.last = String(row.last);
    r.style.setProperty("--depth", String(row.depth));
    this.rails.replaceChildren(
      ...row.rails.map((on, k) => {
        const rail = el("span", on ? "rail" : "rail off");
        rail.style.setProperty("--at", String(k + 1));
        return rail;
      }),
      ...(row.depth ? [el("span", "elbow"), ...(row.last ? [] : [el("span", "stem")])] : []),
    );
    const [title, framing] = splitLabel(node.label);
    this.title.textContent = title;
    this.framing.textContent = framing;
    const { word, move } = this.view.chipFor(node);
    this.chip.replaceChildren(document.createTextNode(word));
    if (move) this.chip.append(el("strong", "", ` ${move}`));
    this.meta.textContent = `${this.view.engineLabel} · ${LEVEL_LABEL[node.level] ?? node.level}`;
    if (!isLive(node)) this.time.textContent = formatSeconds(node.elapsedMs);
    this.button.setAttribute("aria-expanded", String(this.expanded));
    this.button.setAttribute("aria-label", ariaLabel(node, this.view.engineLabel));
    if (node.facts.length && !this.facts) {
      this.facts = el("ul", "facts");
      this.facts.append(...node.facts.map((f) => el("li", "", f)));
      this.inner.append(this.facts);
    }
    if (node.error && node.status === "failed") this.root.title = node.error;
  }

  tick(now) {
    this.time.textContent = formatSeconds(now - this.startedAt);
  }

  pulse() {
    if (reducedMotion.matches) return;
    flare(this.sparks[this.head], 0.22, "1.6px", "1.05px");
    this.head = (this.head + 1) % CARD_SPARKS;
  }

  toggle() {
    this.pinnedOpen = true;
    this.setExpanded(!this.expanded);
  }

  setExpanded(open) {
    clearTimeout(this.collapseTimer);
    this.expanded = open;
    this.root.classList.toggle("collapsed", !open);
    this.button.setAttribute("aria-expanded", String(open));
  }

  collapseSoon() {
    if (this.pinnedOpen) return;
    clearTimeout(this.collapseTimer);
    const run = () => {
      if (!this.root.matches(":hover, :focus-within")) this.setExpanded(false);
    };
    if (this.view.live && !reducedMotion.matches) this.collapseTimer = setTimeout(run, COLLAPSE_AFTER_MS);
    else run();
  }

  /** Commentary arrives a few characters at a time; each piece inks in, and gaze fires once a token is complete. */
  commentary(text) {
    if (!this.commentaryText.accept(text)) return;
    this.finishReveal();
    if (!this.textNode) {
      this.textNode = el("p", "commentary");
      this.inner.prepend(this.textNode);
    }
    this.textNode.classList.toggle("live", this.view.live);
    this.textNode.append(this.view.live ? el("span", "fresh", text) : document.createTextNode(text));
    this.spoken += text;
    this.fireSettled(false);
  }

  fireSettled(closed) {
    for (const token of settledTokens(this.spoken, this.fired, closed)) {
      this.view.gaze(this.id, token);
      this.fired = token.end;
    }
  }

  close() {
    this.finishReveal();
    this.fireSettled(true);
    this.textNode?.classList.remove("live");
  }

  /** A whole reasoning summary, typed out at reading speed while the move is live. */
  chunk(text) {
    if (!this.chunkText.accept(text)) return;
    this.finishReveal();
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
      offset += 1;
    }
    const visible = spans.reduce((acc, s) => acc.padEnd(s.start) + s.text, "");
    this.inner.append(chunk);
    const reveal = { spans, total: visible.length, tokens: extractGaze(visible), shown: 0, fired: 0 };
    const duration = !this.view.live || reducedMotion.matches ? 0 : Math.min(reveal.total * MS_PER_CHAR, MAX_REVEAL_MS);
    const started = performance.now();
    this.reveal = reveal;
    const step = (now) => {
      if (this.reveal !== reveal) return;
      this.show(reveal, duration ? Math.ceil((reveal.total * (now - started)) / duration) : reveal.total);
      if (reveal.shown < reveal.total) reveal.frame = requestAnimationFrame(step);
      else this.reveal = null;
    };
    step(started);
  }

  finishReveal() {
    const reveal = this.reveal;
    if (!reveal) return;
    cancelAnimationFrame(reveal.frame);
    this.reveal = null;
    this.show(reveal, reveal.total);
  }

  show(reveal, count) {
    const n = Math.min(count, reveal.total);
    for (const s of reveal.spans) {
      if (s.start + s.text.length < reveal.shown || s.start > n) continue;
      s.node.textContent = s.text.slice(0, Math.max(0, n - s.start));
    }
    reveal.shown = n;
    while (reveal.fired < reveal.tokens.length && reveal.tokens[reveal.fired].end <= n) {
      this.view.gaze(this.id, reveal.tokens[reveal.fired++]);
    }
    this.view.scrolled();
  }
}

/**
 * The process behind one move or one ponder run: the plan line, one card per model call nested by parent, harness
 * notes, and the decision. Live views stream; a history row rebuilds a still one from the move's recorded events.
 * `hooks` draw on the board: arrow(from, to, {kind, tone, call}), gaze(node, token, tone) and focus(call).
 */
export class ProcessView {
  constructor({ live, engineLabel, hooks = null, reply = null, tone = null, onScroll = () => {} }) {
    this.live = live;
    this.engineLabel = engineLabel;
    this.hooks = hooks;
    this.reply = reply; // {san, uci}: the opponent's reply a ponder run prepares for
    this.rootTone = tone; // a ponder view's colour, so side-by-side runs differ
    this.onScroll = onScroll;
    this.tree = emptyTree();
    this.tones = new Map();
    this.cards = new Map();
    this.events = [];
    this.lines = new Replayable(); // notes, which a ponder hit replays too
    this.decision = null;
    this.ended = false;
    this.outcome = null; // a ponder card's result once the opponent moved: "played" or "missed"
    this.el = el("section", reply ? "process pondering" : "process");
    this.planLine = el("p", "process-plan");
    this.cardsEl = el("div", "cards");
    this.cardsEl.setAttribute("role", "list");
    this.notesEl = el("div", "notes");
    this.verdict = el("div", "verdict");
    this.verdict.setAttribute("aria-live", "polite");
    this.el.append(this.planLine, this.cardsEl, this.notesEl, this.verdict);
    if (reply && hooks) hooks.arrow(reply.uci.slice(0, 2), reply.uci.slice(2, 4), { kind: "move", tone: tone ?? "b1", call: `ponder-${reply.uci}` });
  }

  static replay(events, engineLabel) {
    const view = new ProcessView({ live: false, engineLabel });
    for (const ev of events) view.apply(ev);
    view.el.classList.add("replay");
    if (view.cards.size > 1) for (const card of view.cards.values()) card.setExpanded(false);
    if (!reducedMotion.matches) {
      [...view.cardsEl.children].forEach((card, i) => card.style.setProperty("--enter-delay", `${i * REPLAY_STAGGER_MS}ms`));
    }
    return view;
  }

  apply(ev) {
    if (ev.type !== "pulse") this.events.push(ev);
    switch (ev.type) {
      case "plan":
        this.plan(ev);
        break;
      case "call":
        this.call(ev);
        break;
      case "thought": {
        const card = this.cards.get(ev.call);
        if (card && ev.stream) card.commentary(ev.text);
        else card?.chunk(ev.text);
        break;
      }
      case "pulse":
        this.cards.get(ev.call)?.pulse();
        break;
      case "note":
        if (!SHOWN_BY_CARDS.test(ev.text) && this.lines.accept(JSON.stringify(ev))) this.notesEl.append(el("p", "note", ev.text));
        break;
      case "decision":
        this.decide(ev);
        break;
      case "move":
        this.finish(ev);
        break;
    }
    this.scrolled();
  }

  plan(ev) {
    this.planEvent = ev;
    const extras = [ev.opinions && "second opinion"].filter(Boolean);
    this.planLine.replaceChildren(document.createTextNode(describePlan(ev) + extras.map((x) => ` · ${x}`).join("")));
    if (ev.difficulty_text) this.planLine.append(el("span", "reasons", ` · ${ev.difficulty_text}`));
    this.planLine.classList.toggle("thinking", this.live && !this.ended);
  }

  call(ev) {
    const node = applyCall(this.tree, ev);
    const fresh = !this.cards.has(node.id);
    if (fresh) this.cards.set(node.id, new Card(this, node));
    this.layout();
    if (fresh && this.live) {
      // A new round of calls starts: finished cards fold to their headers so the live ones stay in view.
      for (const [id, card] of this.cards) if (id !== node.id && !isLive(this.tree.nodes.get(id))) card.collapseSoon();
    }
    const card = this.cards.get(node.id);
    const tone = this.tones.get(node.id);
    if (["answered", "cancelled", "failed"].includes(ev.status)) {
      card.close();
      if (ev.status === "answered") this.drawAnswer(node, tone);
      if (this.live && [...this.tree.nodes.values()].some((n) => n.id !== node.id && isLive(n))) card.collapseSoon();
    }
  }

  drawAnswer(node, tone) {
    if (!this.hooks || !node.uci || this.reply) return;
    const kind = node.role === "backup" ? "ghost" : "move";
    this.hooks.arrow(node.uci.slice(0, 2), node.uci.slice(2, 4), { kind, tone: arrowTone(node, this.tones) ?? tone, call: node.id });
  }

  /** Insert new cards in tree order without moving existing ones, which would restart their animations. */
  layout() {
    const rows = treeRows(this.tree);
    const tones = assignTones(this.tree);
    for (const [id, tone] of tones) this.tones.set(id, this.rootTone && tone !== "muted" ? this.rootTone : tone);
    rows.forEach((row, i) => {
      const card = this.cards.get(row.id);
      card.render(this.tree.nodes.get(row.id), this.tones.get(row.id), row);
      if (card.root.parentNode === this.cardsEl) return;
      const next = rows.slice(i + 1).map((r) => this.cards.get(r.id).root).find((n) => n.parentNode === this.cardsEl);
      card.root.setAttribute("role", "listitem");
      this.cardsEl.insertBefore(card.root, next ?? null);
    });
  }

  /** A ponder root says how its preparation went; every other card shows its call's status. */
  chipFor(node) {
    if (node.role !== "ponder" || node.parent) return chip(node);
    if (this.outcome) return { word: this.outcome, move: null };
    if (this.decision) return { word: "prepared", move: this.decision.move };
    return { word: this.ended ? "stopped" : "preparing…", move: null };
  }

  decide(ev) {
    this.decision = ev;
    this.el.classList.add("decided");
    const sentence = el("p", "decision", ev.text);
    this.verdict.replaceChildren(sentence);
    const chosen = new Set([...this.tree.nodes.values()].filter((n) => n.status === "chosen").map((n) => n.id));
    if (ev.call) chosen.add(ev.call);
    for (const [id, card] of this.cards) {
      card.close();
      card.root.classList.toggle("is-chosen", chosen.has(id));
      // A prepared ponder run folds to its one-line header; a move keeps the winning card open.
      if ((this.cards.size > 1 || this.reply) && !card.pinnedOpen) card.setExpanded(!this.reply && chosen.has(id));
    }
    this.layout();
  }

  /** The move landed: its label under the decision, and the view stops being live. */
  finish(move) {
    this.end();
    this.cardsEl.querySelector(".waiting")?.remove();
    if (!move.san) return;
    if (move.source === "forced" && !this.decision) this.verdict.append(el("p", "decision", "The only legal move."));
    const line = el("p", "verdict-move");
    line.append(el("span", "san", move.san));
    if (move.ponder_hit) line.append(el("span", "tag prepared", "prepared"));
    if (move.source === "fallback") line.append(el("span", "tag fallback", "backup"));
    this.verdict.append(line);
  }

  end() {
    this.ended = true;
    this.planLine.classList.remove("thinking");
    for (const card of this.cards.values()) card.close();
    this.layout();
  }

  /** The opponent moved. A ponder view that prepared for it is adopted by the move's view; the others say so. */
  settle(outcome) {
    this.outcome = outcome;
    this.el.classList.add(outcome === "played" ? "played" : "missed");
    for (const card of this.cards.values()) {
      if (outcome !== "played") card.setExpanded(false);
    }
    this.layout();
  }

  /** The move's stream is about to replay this run from its start: skip what is already shown. */
  rewind() {
    this.live = true;
    this.ended = false;
    this.outcome = null;
    this.el.classList.remove("played", "missed");
    for (const card of this.cards.values()) {
      card.commentaryText.rewind();
      card.chunkText.rewind();
    }
    this.lines.rewind();
    this.events = [];
  }

  tick(now) {
    if (!this.live || this.ended) return;
    for (const [id, card] of this.cards) if (isLive(this.tree.nodes.get(id))) card.tick(now);
  }

  gaze(id, token) {
    const node = this.tree.nodes.get(id);
    if (!this.hooks || !this.live || this.reply || node.role === "backup") return;
    this.hooks.gaze(node, token, arrowTone(node, this.tones));
  }

  focus(id) {
    this.hooks?.focus(id && this.reply && this.tree.nodes.get(id)?.role === "ponder" ? `ponder-${this.reply.uci}` : id);
  }

  scrolled() {
    this.onScroll();
  }

  detach() {
    for (const card of this.cards.values()) card.finishReveal();
    this.hooks?.focus(null);
    this.hooks = null;
  }
}

/**
 * The mind panel: the orb and its budget ring, the current move's process view, the cards considering your replies
 * while you think, and the history rows. A view only: the game state lives in app.js.
 */
export class Mind {
  constructor(root, { hooks }) {
    this.root = root;
    this.hooks = hooks;
    this.orb = root.querySelector(".orb");
    this.ringFill = root.querySelector(".ring-fill");
    this.elapsed = root.querySelector(".elapsed");
    this.sense = root.querySelector(".sense");
    this.stream = root.querySelector(".stream");
    this.history = root.querySelector(".history");
    this.engineLabel = "";
    this.view = null; // the current move's process view
    this.ponders = []; // ponder views of this turn, in the order the replies were considered
    this.adopt = null; // the ponder view the next move's stream will replay
    this.sparks = sparkRing(root.querySelector(".sparks"), SPARKS, SPARK_RADIUS, 48, 1.05);
    this.sparks.forEach((dot, i) => dot.style.setProperty("--i", String(i)));
    this.head = 0;
    this.settleTimer = null;
    this.pinned = true;
    this.stream.addEventListener("scroll", () => {
      const s = this.stream;
      this.pinned = s.scrollHeight - s.scrollTop - s.clientHeight < PINNED_PX;
    }, { passive: true });
  }

  /** The engine's accent: "claude", "codex", "gemini" or "fake". */
  setEngine(name, label) {
    this.root.dataset.engine = name;
    this.engineLabel = label;
  }

  reset() {
    this.clearPonders();
    this.view?.detach();
    this.view = null;
    this.endSettle();
    this.history.replaceChildren();
    this.showSense(null);
    this.wait("");
  }

  /** The model starts thinking. A ponder run that prepared for the reply you played becomes this move's view. */
  think() {
    this.endSettle();
    this.view?.detach();
    const missed = this.ponders.filter((v) => v !== this.adopt);
    let view = this.adopt;
    if (view) view.rewind();
    else view = new ProcessView({ live: true, engineLabel: this.engineLabel, hooks: this.hooks, onScroll: () => this.follow() });
    this.adopt = null;
    this.ponders = [];
    view.planLine.classList.add("thinking");
    const parts = [];
    if (missed.length) {
      const strip = el("div", "considered");
      strip.append(el("p", "section-label", "Considered"), ...missed.map((v) => v.el));
      for (const v of missed) v.detach();
      parts.push(strip);
    }
    if (!view.cards.size) view.cardsEl.append(el("p", "waiting", "Reading the position"));
    parts.push(view.el);
    this.stream.replaceChildren(...parts);
    this.pinned = true;
    this.view = view;
    this.elapsed.textContent = "";
    this.showSense(null);
    this.setOrb("thinking", 0, "calm");
  }

  /** A move event's sibling events: plan, call, thought, pulse, note, decision. */
  event(ev) {
    if (!this.view) return;
    if (ev.type === "call") this.view.cardsEl.querySelector(".waiting")?.remove();
    if (ev.type === "pulse") this.pulse();
    if (ev.type === "plan") this.showSense(ev);
    this.view.apply(ev);
  }

  /** How hard the model finds this position and how deep it thinks, under its name while it moves. */
  showSense(plan) {
    this.sense.replaceChildren();
    if (!plan) return;
    this.sense.dataset.tier = plan.difficulty;
    const second = plan.second_level && plan.second_level !== plan.level ? `, second opinion at ${plan.second_level}` : "";
    this.sense.title = `${plan.difficulty_text} · ${plan.level} effort${second}`;
    this.sense.append(el("span", "dot"), el("span", "text", describeSense(plan)));
  }

  tick(elapsedMs, plan) {
    const { fraction, tone } = plan ? budgetRing(elapsedMs, plan.budget_ms, plan.deadline_ms) : { fraction: 0, tone: "calm" };
    this.setOrb("thinking", fraction, tone);
    this.elapsed.textContent = plan ? elapsedText(elapsedMs, plan.budget_ms) : `${(elapsedMs / 1000).toFixed(1)} s`;
    this.view?.tick(performance.now());
  }

  /** Any call is working: light the next spark. Several calls at once turn the ring faster. */
  pulse() {
    const state = this.orb.dataset.state;
    if ((state !== "thinking" && state !== "pondering") || reducedMotion.matches) return;
    flare(this.sparks[this.head], state === "thinking" ? SPARK_REST : 0.06, "1.9px", "1.05px");
    this.head = (this.head + 1) % SPARKS;
  }

  /** The move landed: the process view keeps its decision, and a history row can replay it. */
  commit({ label, level, difficulty, source, elapsedMs, ponderHit, move }) {
    const view = this.view;
    view?.apply(move);
    view?.verdict.querySelector(".verdict-move .san")?.replaceChildren(document.createTextNode(label));
    this.settle();
    const row = el("details", "row enter");
    const summary = el("summary");
    const meta = level === "forced" ? [LEVEL_LABEL.forced] : [LEVEL_LABEL[level] ?? level, formatSeconds(elapsedMs)];
    const calls = view ? view.tree.order.length : 0;
    if (calls > 1) meta.push(`${calls} calls`);
    summary.append(el("span", "san", label), el("span", "meta", meta.map((m) => ` · ${m}`).join("")));
    if (difficulty === "hard") summary.append(el("span", "tag hard", "hard"));
    if (ponderHit) summary.append(el("span", "tag prepared", "prepared"));
    if (source === "fallback") summary.append(el("span", "tag fallback", "backup"));
    if (view?.decision) summary.title = view.decision.text;
    row.append(summary);
    const events = view ? [...view.events] : [move];
    row.addEventListener("toggle", () => {
      if (!row.open || row.querySelector(".process")) return;
      const replay = ProcessView.replay(events, this.engineLabel);
      replay.verdict.querySelector(".verdict-move .san")?.replaceChildren(document.createTextNode(label));
      row.append(replay.el);
    });
    this.history.prepend(row);
    view?.detach();
  }

  /** Your move landed: a plain history row with how long you took, so both sides' times read in one list. */
  commitHuman({ label, elapsedMs }) {
    const row = el("details", "row you enter");
    const summary = el("summary");
    summary.append(el("span", "san", label), el("span", "meta", ` · you · ${formatSeconds(elapsedMs)}`));
    summary.addEventListener("click", (e) => e.preventDefault());
    row.append(summary);
    this.history.prepend(row);
  }

  /** Your turn: the last move's view stays, with the reply the model expects and the replies it is considering. */
  wait(text, expecting = null) {
    this.setOrb(this.ponders.length ? "pondering" : "idle", 0, "calm");
    const line = el("p", "your-move", text);
    if (expecting) {
      line.append(document.createTextNode(text ? " Expecting " : "Expecting "));
      line.append(el("strong", "", expecting));
    }
    this.ponderSection = el("section", "ponder-section");
    this.ponderSection.setAttribute("aria-label", "The model considering your replies");
    this.ponderSection.hidden = true;
    this.ponderSection.append(el("p", "section-label", "Considering your replies"));
    this.stream.replaceChildren(...[this.view?.el, text || expecting ? line : null, this.ponderSection].filter(Boolean));
    this.follow();
  }

  /** A ponder run for one reply you might play. Its events go to the returned view. */
  ponder(reply) {
    const tones = ["b1", "b2", "b3"];
    const view = new ProcessView({ live: true, engineLabel: this.engineLabel, hooks: this.hooks, reply,
                                   tone: tones[this.ponders.length % tones.length], onScroll: () => this.follow() });
    this.ponders.push(view);
    if (this.ponderSection) {
      this.ponderSection.hidden = false;
      this.ponderSection.append(view.el);
    }
    this.setOrb("pondering", 0, "calm");
    return view;
  }

  ponderEvent(view, ev) {
    if (ev.type === "pulse") this.pulse();
    view.apply(ev);
  }

  ponderEnded(view) {
    view.end();
    if (this.ponders.every((v) => v.ended) && this.orb.dataset.state === "pondering") this.setOrb("idle", 0, "calm");
  }

  /** You moved: the card that prepared for it flashes and will be replayed; the others collapse with what you played. */
  resolvePonder(uci, san) {
    this.adopt = this.ponders.find((v) => v.reply.uci === uci && v.cards.size) ?? null;
    for (const view of this.ponders) {
      if (view === this.adopt) view.settle("played");
      else view.settle(`you played ${san} instead`);
    }
  }

  clearPonders() {
    for (const view of this.ponders) view.detach();
    this.ponders = [];
    this.adopt = null;
  }

  /** The game ended while the model was thinking. Its partial view stays. */
  stop(text) {
    this.clearPonders();
    this.view?.end();
    this.view?.notesEl.append(el("p", "note", text));
    this.view?.detach();
    this.setOrb("idle", 0, "calm");
  }

  setOrb(state, fraction, tone) {
    if (state === "idle" && this.settleTimer) return;
    this.orb.dataset.state = state;
    this.orb.dataset.tone = tone;
    this.ringFill.style.strokeDashoffset = String(1 - fraction);
    if (state !== "thinking") this.elapsed.textContent = "";
  }

  endSettle() {
    clearTimeout(this.settleTimer);
    this.settleTimer = null;
  }

  /** The move landed: the sparks flare once around the ring and go out, then the orb rests. */
  settle() {
    this.endSettle();
    this.setOrb("settled", 1, "calm");
    if (!reducedMotion.matches) {
      for (const [i, spark] of this.sparks.entries()) {
        spark.animate([{ opacity: 0 }, { opacity: 0.95, offset: 0.35 }, { opacity: 0 }], {
          duration: 520,
          delay: ((i - this.head + SPARKS) % SPARKS) * 14,
          easing: "ease-out",
        });
      }
    }
    this.settleTimer = setTimeout(() => {
      this.settleTimer = null;
      if (this.orb.dataset.state === "settled") this.setOrb(this.ponders.some((v) => !v.ended) ? "pondering" : "idle", 0, "calm");
    }, reducedMotion.matches ? 0 : SETTLE_MS);
  }

  /** Keeps the newest card in view unless the player scrolled up to read. */
  follow() {
    if (this.pinned) this.stream.scrollTop = this.stream.scrollHeight;
  }
}
