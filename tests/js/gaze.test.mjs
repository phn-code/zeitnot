import test from "node:test";
import assert from "node:assert/strict";
import { extractGaze, resolveMove } from "../../web/gaze.js";

const brief = (text) => extractGaze(text).map(({ kind, san, square }) => [kind, san, square]);

test("plain squares are squares", () => {
  assert.deepEqual(brief("The e5 square is weak, and h7 needs a guard."), [
    ["square", "e5", "e5"],
    ["square", "h7", "h7"],
  ]);
});

test("piece moves, captures, checks and mates are moves; signs are stripped", () => {
  assert.deepEqual(brief("Nf3, then Qxd3+ or exd5, and Rfxd1 leads to Qxf7#."), [
    ["move", "Nf3", "f3"],
    ["move", "Qxd3", "d3"],
    ["move", "exd5", "d5"],
    ["move", "Rfxd1", "d1"],
    ["move", "Qxf7", "f7"],
  ]);
});

test("castling in letters or zeros, long and short", () => {
  assert.deepEqual(brief("O-O is safer than O-O-O; 0-0 again, or 0-0-0."), [
    ["move", "O-O", null],
    ["move", "O-O-O", null],
    ["move", "O-O", null],
    ["move", "O-O-O", null],
  ]);
});

test("promotions with and without the equals sign", () => {
  assert.deepEqual(brief("e8=Q wins, exf8=N+ forks, b1Q too."), [
    ["move", "e8=Q", "e8"],
    ["move", "exf8=N", "f8"],
    ["move", "b1=Q", "b1"],
  ]);
});

test("a move-number prefix turns a pawn square into a move", () => {
  assert.deepEqual(brief("After 33. b5 axb5 34.Nxb5 and 34...c6, but c6 alone is a square. Then …e5."), [
    ["move", "b5", "b5"],
    ["move", "axb5", "b5"],
    ["move", "Nxb5", "b5"],
    ["move", "c6", "c6"],
    ["square", "c6", "c6"],
    ["move", "e5", "e5"],
  ]);
});

test("a check sign alone turns a pawn square into a move", () => {
  assert.deepEqual(brief("f7+ is strong"), [["move", "f7", "f7"]]);
});

test("hyphenated squares are separate squares; words, UCI and numbers are not tokens", () => {
  assert.deepEqual(brief("the b4-e1 diagonal"), [
    ["square", "b4", "b4"],
    ["square", "e1", "e1"],
  ]);
  assert.deepEqual(brief("Bead e2e4 a9 i5 Ne9 10.5 seconds A4 paper be4 xe4"), []);
});

test("offsets cover the whole token including its prefix and sign", () => {
  const text = "Consider 12... Qxd3+ now";
  const [token] = extractGaze(text);
  assert.equal(text.slice(token.start, token.end), "12... Qxd3+");
});

// White to move after 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. d3 Be7 5. Nbd2 d6, plus a pawn on b7 ready to promote
// and an en passant capture, as a chess.js verbose-move subset.
const LEGAL = [
  { from: "e1", to: "g1", piece: "k", flags: "k" },
  { from: "e1", to: "f1", piece: "k", flags: "n" },
  { from: "f3", to: "g5", piece: "n", flags: "n" },
  { from: "f3", to: "e5", piece: "n", flags: "c", captured: "p" },
  { from: "d2", to: "b3", piece: "n", flags: "n" },
  { from: "f3", to: "h4", piece: "n", flags: "n" },
  { from: "f3", to: "d4", piece: "n", flags: "n" },
  { from: "c4", to: "f7", piece: "b", flags: "c", captured: "p" },
  { from: "d1", to: "e2", piece: "q", flags: "n" },
  { from: "e4", to: "d5", piece: "p", flags: "c", captured: "p" },
  { from: "e5", to: "d6", piece: "p", flags: "e", captured: "p" },
  { from: "d3", to: "d4", piece: "p", flags: "n" },
  { from: "c2", to: "c4", piece: "p", flags: "b" },
  ...["q", "r", "b", "n"].map((promotion) => ({ from: "b7", to: "b8", piece: "p", flags: "np", promotion })),
  { from: "b7", to: "a8", piece: "p", flags: "cp", captured: "r", promotion: "q" },
  { from: "a1", to: "b1", piece: "r", flags: "n" },
  { from: "h1", to: "b1", piece: "r", flags: "n" },
];

const resolve = (text) => resolveMove(extractGaze(text)[0], LEGAL);

test("resolveMove finds piece moves, captures and castling", () => {
  assert.deepEqual(resolve("Ng5"), { from: "f3", to: "g5" });
  assert.deepEqual(resolve("Nxe5"), { from: "f3", to: "e5" });
  assert.deepEqual(resolve("Bxf7+"), { from: "c4", to: "f7" });
  assert.deepEqual(resolve("O-O"), { from: "e1", to: "g1" });
  assert.deepEqual(resolve("exd5"), { from: "e4", to: "d5" });
  assert.deepEqual(resolve("exd6"), { from: "e5", to: "d6" });
});

test("resolveMove forgives a missing x and an over-specified origin", () => {
  assert.deepEqual(resolve("Ne5"), { from: "f3", to: "e5" });
  assert.deepEqual(resolve("Nfg5"), { from: "f3", to: "g5" });
  assert.deepEqual(resolve("Nd2b3"), { from: "d2", to: "b3" });
});

test("resolveMove: pawn pushes need their own file; promotions collapse to one arrow", () => {
  assert.deepEqual(resolve("5. d4"), { from: "d3", to: "d4" });
  assert.deepEqual(resolve("1. c4"), { from: "c2", to: "c4" });
  assert.deepEqual(resolve("b8=Q"), { from: "b7", to: "b8" });
  assert.deepEqual(resolve("b8+"), { from: "b7", to: "b8" });
  assert.deepEqual(resolve("bxa8=Q"), { from: "b7", to: "a8" });
});

test("resolveMove rejects ambiguous, illegal and square tokens", () => {
  assert.equal(resolve("Rb1"), null, "two rooks reach b1");
  assert.deepEqual(resolve("Rab1"), { from: "a1", to: "b1" });
  assert.equal(resolve("Nd4+") && resolve("Nc3"), null, "no knight reaches c3");
  assert.equal(resolve("O-O-O"), null);
  assert.equal(resolve("Qh5"), null);
  assert.equal(resolve("d4"), null, "a bare square is never resolved");
});
