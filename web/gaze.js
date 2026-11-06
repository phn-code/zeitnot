// Where the model is looking: square names and move notation in thought text. Pure, no DOM.

// An optional move-number prefix ("33. ", "12...", "…"), then castling, a piece move, a pawn capture or a square,
// then an optional check or mate sign. Hyphens are allowed next to a token so "b4-e1" yields two squares.
const TOKEN =
  /(?<!\w)(?:(?:\d{1,3}\s?(?:\.{1,3}|…)|\.\.\.|…)\s?)?(O-O-O|O-O|0-0-0|0-0|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h]x[a-h][1-8](?:=?[QRBN])?|[a-h][1-8](?:=?[QRBN])?)([+#])?(?!\w)/g;

const SQUARE = /^[a-h][1-8]$/;

/**
 * Tokens in reading order: {kind: "square" | "move", san, square, start, end}.
 * `san` has no check sign and uses "O-O" and "=Q". `square` is the square to glow, null for castling.
 * A bare square is a square. It is a move only with a piece letter, a capture, castling, a promotion,
 * a check sign or a move-number prefix ("33. b5").
 */
export function extractGaze(text) {
  const tokens = [];
  for (const m of text.matchAll(TOKEN)) {
    const [whole, body, sign] = m;
    const san = body.replaceAll("0", "O").replace(/([a-h][18])([QRBN])$/, "$1=$2");
    const prefixed = whole.length > body.length + (sign?.length ?? 0);
    const kind = SQUARE.test(san) && !prefixed && !sign ? "square" : "move";
    tokens.push({ kind, san, square: san.match(/[a-h][1-8]/g)?.at(-1) ?? null, start: m.index, end: m.index + whole.length });
  }
  return tokens;
}

const PIECE_MOVE = /^([KQRBN])([a-h])?([1-8])?x?([a-h][1-8])$/;
const PAWN_MOVE = /^(?:([a-h])x)?([a-h][1-8])(?:=([QRBN]))?$/;

function matcher(san) {
  if (san === "O-O") return (m) => m.flags.includes("k");
  if (san === "O-O-O") return (m) => m.flags.includes("q");
  const piece = san.match(PIECE_MOVE);
  if (piece) {
    const [, letter, file, rank, to] = piece;
    return (m) => m.piece === letter.toLowerCase() && m.to === to && (!file || m.from[0] === file) && (!rank || m.from[1] === rank);
  }
  const pawn = san.match(PAWN_MOVE);
  if (pawn) {
    const [, fromFile, to, promotion] = pawn;
    return (m) =>
      m.piece === "p" && m.to === to && m.from[0] === (fromFile ?? to[0]) && (!promotion || m.promotion === promotion.toLowerCase());
  }
  return () => false;
}

/**
 * The {from, to} of the one legal move a move token names, or null. `legalMoves` are chess.js verbose moves
 * ({from, to, piece, flags, promotion}). A missing "x" or an over-specified origin still matches; an ambiguous
 * token ("Nd2" when two knights can go there) does not. Promotions to different pieces share one {from, to}.
 */
export function resolveMove(token, legalMoves) {
  if (token.kind !== "move") return null;
  const matches = legalMoves.filter(matcher(token.san));
  const pairs = new Set(matches.map((m) => m.from + m.to));
  return pairs.size === 1 ? { from: matches[0].from, to: matches[0].to } : null;
}

const TOKEN_END = /[\s,.;:!?)\]]/;

/**
 * Gaze tokens of a growing text that are complete and not yet fired: those after `firedEnd` that are followed by a
 * space or punctuation, or every one once the text is `closed`. A token at the very end may still grow ("O-O" into
 * "O-O-O"), so it waits.
 */
export function settledTokens(text, firedEnd, closed) {
  return extractGaze(text).filter((t) => t.start >= firedEnd && (closed || (t.end < text.length && TOKEN_END.test(text[t.end]))));
}
