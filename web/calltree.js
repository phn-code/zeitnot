// The call tree of one move or one ponder run, built from `call` events. Pure, no DOM.

const RANK = { started: 0, answered: 1, cancelled: 1, failed: 1, chosen: 2 };
export const TONES = ["b0", "b1", "b2", "b3"];

/** An empty tree: nodes by call id, and ids in the order their calls started. */
export function emptyTree() {
  return { nodes: new Map(), order: [] };
}

/**
 * Applies one `call` event and returns its node. A status only moves forward, so a replayed or late event never
 * undoes a later one: a ponder hit replays every event the browser may already have seen.
 */
export function applyCall(tree, ev) {
  let node = tree.nodes.get(ev.call);
  if (!node) {
    node = {
      id: ev.call,
      parent: ev.parent,
      role: ev.role,
      label: ev.label,
      engine: ev.engine,
      level: ev.level,
      framing: ev.framing,
      fen: ev.fen,
      status: "started",
      move: null,
      uci: null,
      expectedReply: null,
      facts: [],
      error: null,
      elapsedMs: 0,
    };
    tree.nodes.set(ev.call, node);
    tree.order.push(ev.call);
  }
  if (RANK[ev.status] < RANK[node.status] || (ev.status === node.status && ev.status !== "started")) return node;
  node.status = ev.status;
  node.elapsedMs = ev.elapsed_ms;
  if (ev.move) Object.assign(node, { move: ev.move, uci: ev.uci, expectedReply: ev.expected_reply ?? node.expectedReply });
  if (ev.facts?.length) node.facts = ev.facts;
  if (ev.error) node.error = ev.error;
  return node;
}

export const isLive = (node) => node.status === "started";

/** A parent the tree does not hold (a ponder root seen only by another view) counts as no parent. */
function parentOf(tree, node) {
  return node.parent && tree.nodes.has(node.parent) ? node.parent : null;
}

/**
 * Rows to draw, depth first in start order: {id, depth, last, rails}. `last` is whether the node is its parent's last
 * child so far, which draws an elbow instead of a tee. `rails[k]` is whether the ancestor at depth k + 1 has a later
 * sibling, which draws a vertical line through this row at that depth.
 */
export function treeRows(tree) {
  const children = new Map([[null, []]]);
  for (const id of tree.order) {
    const parent = parentOf(tree, tree.nodes.get(id));
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(id);
  }
  const rows = [];
  const walk = (parent, depth, rails) => {
    const ids = children.get(parent) ?? [];
    ids.forEach((id, i) => {
      const last = i === ids.length - 1;
      rows.push({ id, depth, last, rails });
      walk(id, depth + 1, depth === 0 ? [] : [...rails, !last]);
    });
  };
  walk(null, 0, []);
  return rows;
}

/**
 * Each call's colour. A top-level call owns a tone: the main call b0 (the engine's accent), the others b1, b2, b3 in
 * start order, wrapping round. A child shares its parent's tone, so a reply reads as part of the candidate it tests.
 * The backup is muted: it is a safety net, not an opinion.
 */
export function assignTones(tree) {
  const tones = new Map();
  let next = 1;
  for (const id of tree.order) {
    const node = tree.nodes.get(id);
    const parent = parentOf(tree, node);
    if (node.role === "backup") tones.set(id, "muted");
    else if (parent) tones.set(id, tones.get(parent) === "muted" ? "b0" : tones.get(parent));
    else if (node.role === "main") tones.set(id, "b0");
    else {
      tones.set(id, TONES[next]);
      next = next % (TONES.length - 1) + 1;
    }
  }
  return tones;
}

/** The colour of a node's own move on the board: a reply is the opponent's move, so it takes the opponent's colour. */
export function arrowTone(node, tones) {
  return node.role === "reply" ? "opp" : tones.get(node.id);
}

/** "Second opinion · most forcing" as ["Second opinion", "most forcing"]. */
export function splitLabel(label) {
  const at = label.indexOf(" · ");
  return at < 0 ? [label, ""] : [label.slice(0, at), label.slice(at + 3)];
}

const WORDS = { started: "thinking", answered: "answered", chosen: "chosen", cancelled: "cancelled", failed: "failed" };

/** The status chip: {word, move}, for example {word: "answered", move: "Nf3"}. */
export function chip(node) {
  return { word: WORDS[node.status], move: ["answered", "chosen"].includes(node.status) ? node.move : null };
}

/** One sentence for screen readers: "Second opinion, most forcing, Claude Opus at high: answered Nf3 in 4.2 s". */
export function ariaLabel(node, engineLabel) {
  const [title, framing] = splitLabel(node.label);
  const { word, move } = chip(node);
  const time = isLive(node) ? "" : ` in ${(node.elapsedMs / 1000).toFixed(1)} s`;
  return `${title}${framing ? `, ${framing}` : ""}, ${engineLabel} at ${node.level}: ${word}${move ? ` ${move}` : ""}${time}`;
}
