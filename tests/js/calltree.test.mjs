import test from "node:test";
import assert from "node:assert/strict";
import { applyCall, ariaLabel, arrowTone, assignTones, chip, emptyTree, splitLabel, treeRows } from "../../web/calltree.js";

const FEN = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2";
let clock = 0;
const ev = (call, status, extra = {}) => ({
  type: "call",
  call,
  status,
  role: "main",
  label: "Main",
  parent: null,
  engine: "claude",
  level: "high",
  framing: "plain",
  fen: FEN,
  elapsed_ms: (clock += 100),
  move: null,
  uci: null,
  expected_reply: null,
  facts: [],
  error: null,
  ...extra,
});

/** The split from the spec: main and second opinion disagree, each gets a reply, then a pick; a backup runs alongside. */
function splitTree() {
  const tree = emptyTree();
  for (const e of [
    ev("c1", "started"),
    ev("c2", "started", { role: "second_opinion", label: "Second opinion · most forcing", framing: "most forcing" }),
    ev("c3", "started", { role: "backup", label: "Backup · safe moves only", level: "low" }),
    ev("c1", "answered", { move: "Bc4", uci: "f1c4" }),
    ev("c2", "answered", { role: "second_opinion", label: "Second opinion · most forcing", move: "Nf3", uci: "g1f3" }),
    ev("c4", "started", { role: "reply", label: "Your best reply to Bc4", parent: "c1", level: "low" }),
    ev("c5", "started", { role: "reply", label: "Your best reply to Nf3", parent: "c2", level: "low" }),
    ev("c6", "started", { role: "pick", label: "Pick · Bc4 or Nf3", level: "low" }),
  ]) {
    applyCall(tree, e);
  }
  return tree;
}

test("a call's status only moves forward, so a replayed event never undoes a later one", () => {
  const tree = emptyTree();
  applyCall(tree, ev("c1", "started"));
  applyCall(tree, ev("c1", "answered", { move: "Nf3", uci: "g1f3", expected_reply: "d5" }));
  applyCall(tree, ev("c1", "chosen", { move: "Nf3", uci: "g1f3" }));
  const node = applyCall(tree, ev("c1", "started"));
  assert.equal(node.status, "chosen");
  assert.deepEqual([node.move, node.uci, node.expectedReply], ["Nf3", "g1f3", "d5"]);
  assert.deepEqual(tree.order, ["c1"]);
});

test("rows are depth first in start order, children after their parent", () => {
  const rows = treeRows(splitTree()).map(({ id, depth, last }) => [id, depth, last]);
  assert.deepEqual(rows, [
    ["c1", 0, false],
    ["c4", 1, true],
    ["c2", 0, false],
    ["c5", 1, true],
    ["c3", 0, false],
    ["c6", 0, true],
  ]);
});

test("rails continue a parent's line past its earlier children", () => {
  const tree = emptyTree();
  applyCall(tree, ev("p", "started", { role: "ponder", label: "If you play Nf3" }));
  applyCall(tree, ev("s", "started", { role: "second_opinion", label: "Second opinion · most forcing", parent: "p" }));
  applyCall(tree, ev("b", "started", { role: "backup", label: "Backup · safe moves only", parent: "p" }));
  applyCall(tree, ev("r", "started", { role: "reply", label: "Your best reply to Bc5", parent: "s" }));
  const rows = treeRows(tree);
  assert.deepEqual(
    rows.map(({ id, depth, last, rails }) => [id, depth, last, rails]),
    [
      ["p", 0, true, []],
      ["s", 1, false, []],
      ["r", 2, true, [true]],
      ["b", 1, true, []],
    ],
  );
});

test("a parent the view never saw counts as no parent", () => {
  const tree = emptyTree();
  applyCall(tree, ev("c9", "started", { role: "reply", label: "Your best reply to e5", parent: "elsewhere" }));
  assert.deepEqual(treeRows(tree), [{ id: "c9", depth: 0, last: true, rails: [] }]);
});

test("tones: main takes the accent, other top-level calls take the next tone, children share their parent's", () => {
  const tones = assignTones(splitTree());
  assert.deepEqual(Object.fromEntries(tones), { c1: "b0", c2: "b1", c3: "muted", c4: "b0", c5: "b1", c6: "b2" });
});

test("tones wrap round b1 to b3 and never reuse the accent for a second top-level call", () => {
  const tree = emptyTree();
  for (const id of ["p1", "p2", "p3", "p4"]) applyCall(tree, ev(id, "started", { role: "ponder", label: `If you play ${id}` }));
  assert.deepEqual([...assignTones(tree).values()], ["b1", "b2", "b3", "b1"]);
});

test("a reply's own move is drawn in the opponent's colour", () => {
  const tree = splitTree();
  const tones = assignTones(tree);
  assert.equal(arrowTone(tree.nodes.get("c4"), tones), "opp");
  assert.equal(arrowTone(tree.nodes.get("c2"), tones), "b1");
});

test("labels, chips and the screen-reader sentence", () => {
  assert.deepEqual(splitLabel("Second opinion · most forcing"), ["Second opinion", "most forcing"]);
  assert.deepEqual(splitLabel("Main"), ["Main", ""]);
  const tree = splitTree();
  assert.deepEqual(chip(tree.nodes.get("c2")), { word: "answered", move: "Nf3" });
  assert.deepEqual(chip(tree.nodes.get("c6")), { word: "thinking", move: null });
  const second = tree.nodes.get("c2");
  assert.equal(ariaLabel(second, "Claude Opus"), `Second opinion, most forcing, Claude Opus at high: answered Nf3 in ${(second.elapsedMs / 1000).toFixed(1)} s`);
});
