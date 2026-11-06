import { Chessboard, FEN, INPUT_EVENT_TYPE } from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.15.4/src/Chessboard.js";
import { Extension, EXTENSION_POINT } from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.15.4/src/model/Extension.js";
import { Markers } from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.15.4/src/extensions/markers/Markers.js";
import { Arrows } from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.15.4/src/extensions/arrows/Arrows.js";
import {
  PromotionDialog,
  PROMOTION_DIALOG_RESULT_TYPE,
} from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.15.4/src/extensions/promotion-dialog/PromotionDialog.js";

const ASSETS = "https://cdn.jsdelivr.net/npm/cm-chessboard@8.15.4/assets/";
const MOVE_MS = 220;
const GLOW_MS = 2500;
const PULSE_MS = 700;
const ARROW_FADE_MS = 300;
const MAX_GAZE_ARROWS = 3;
const GHOST_TITLE = "backup move, ready if time runs short";

// cm-chessboard matches marker and arrow types by reference, so each type exists once, here.
// Slices are ids in its markers sprite; the fills and animations are in style.css.
const MARK = {
  last: { class: "zn-last", slice: "markerSquare" },
  check: { class: "zn-check", slice: "markerSquare" },
  selected: { class: "zn-selected", slice: "markerSquare" },
  dot: { class: "zn-dot", slice: "markerDot" },
  ring: { class: "zn-ring", slice: "markerCircle" },
  gaze: { class: "zn-gaze", slice: "markerSquare" },
  pulse: { class: "zn-pulse", slice: "markerSquare" },
};
const ARROW = {
  gaze: { class: "zn-gaze" },
  fading: { class: "zn-gaze zn-fading" },
  ghost: { class: "zn-ghost" },
};

class AfterRedraw extends Extension {
  constructor(chessboard, props) {
    super(chessboard);
    this.registerExtensionPoint(EXTENSION_POINT.afterRedrawBoard, props.callback);
  }
}

/**
 * The board: position, animated moves, move input with legal-move dots and promotion, and the overlays
 * (last move, check, gaze glows and arrows, the fallback ghost arrow, the landing pulse).
 */
export class Board {
  /** One board per game: the orientation is fixed at construction, so there is no turn animation to race a resize. */
  constructor(el, { orientation, onMove }) {
    this.el = el;
    this.onMove = onMove;
    this.legalFrom = () => [];
    this.positionBefore = FEN.start;
    // Markers and Arrows redraw every element on any change, which restarts CSS animations. Each animated
    // element is tracked here (key -> {selector, at, timer}) so sync() can resume it with a negative delay.
    this.live = new Map();
    this.gazeIds = []; // "e2e4" ids of the gaze arrows, oldest first
    this.ghostId = null;
    for (const [name, ms] of Object.entries({ move: MOVE_MS, glow: GLOW_MS, pulse: PULSE_MS, fade: ARROW_FADE_MS })) {
      el.style.setProperty(`--zn-${name}-ms`, `${ms}ms`);
    }
    this.cb = new Chessboard(el, {
      position: FEN.start,
      orientation,
      assetsUrl: ASSETS,
      style: { cssClass: "zeitnot", borderType: "none", animationDuration: MOVE_MS, pieces: { file: "pieces/standard.svg" } },
      extensions: [
        { class: Markers, props: { autoMarkers: null } },
        { class: Arrows },
        { class: PromotionDialog },
        { class: AfterRedraw, props: { callback: () => this.sync() } },
      ],
    });
  }

  setPosition(fen, animated) {
    return this.cb.setPosition(fen, animated);
  }

  enableInput(color, legalFrom) {
    this.legalFrom = legalFrom;
    if (!this.cb.isMoveInputEnabled()) this.cb.enableMoveInput((event) => this.onInput(event), color);
  }

  disableInput() {
    if (this.cb.isMoveInputEnabled()) this.cb.disableMoveInput();
    this.clearTargets();
  }

  onInput(event) {
    switch (event.type) {
      case INPUT_EVENT_TYPE.moveInputStarted: {
        const moves = this.legalFrom(event.squareFrom);
        if (moves.length) {
          this.positionBefore = this.cb.getPosition();
          this.showTargets(event.squareFrom, moves);
        }
        return moves.length > 0;
      }
      case INPUT_EVENT_TYPE.validateMoveInput: {
        this.clearTargets();
        const { squareFrom: from, squareTo: to } = event;
        const moves = this.legalFrom(from).filter((m) => m.to === to);
        if (!moves.length) return false;
        // The board moves the piece itself right after this returns, so the game hears about it afterwards.
        const settled = this.cb.state.moveInputProcess;
        if (moves.some((m) => m.promotion)) settled.then(() => this.askPromotion(from, to, moves[0].color));
        else settled.then(() => this.onMove({ from, to }));
        return true;
      }
      case INPUT_EVENT_TYPE.moveInputCanceled:
        this.clearTargets();
        return undefined;
      default:
        return undefined;
    }
  }

  askPromotion(from, to, color) {
    this.cb.showPromotionDialog(to, color, (result) => {
      if (result.type === PROMOTION_DIALOG_RESULT_TYPE.pieceSelected) this.onMove({ from, to, promotion: result.piece.charAt(1) });
      else this.cb.setPosition(this.positionBefore, true);
    });
  }

  showTargets(from, moves) {
    this.cb.addMarker(MARK.selected, from);
    for (const to of new Set(moves.map((m) => m.to))) {
      const capture = moves.some((m) => m.to === to && m.flags.includes("c"));
      this.cb.addMarker(capture ? MARK.ring : MARK.dot, to);
    }
    this.sync();
  }

  clearTargets() {
    for (const type of [MARK.selected, MARK.dot, MARK.ring]) this.cb.removeMarkers(type);
    this.sync();
  }

  markLastMove(from, to) {
    this.cb.removeMarkers(MARK.last);
    if (from) {
      this.cb.addMarker(MARK.last, from);
      this.cb.addMarker(MARK.last, to);
    }
    this.sync();
  }

  markCheck(square) {
    this.cb.removeMarkers(MARK.check);
    if (square) this.cb.addMarker(MARK.check, square);
    this.sync();
  }

  glow(square) {
    this.addMark(MARK.gaze, square, GLOW_MS);
  }

  pulse(square) {
    this.addMark(MARK.pulse, square, PULSE_MS);
  }

  gazeArrow(from, to) {
    const id = from + to;
    if (id === this.ghostId) return;
    const seen = this.gazeIds.indexOf(id);
    if (seen >= 0) {
      this.gazeIds.push(...this.gazeIds.splice(seen, 1));
      return;
    }
    this.removeArrow(ARROW.fading, from, to);
    this.gazeIds.push(id);
    this.addArrow(ARROW.gaze, from, to);
    if (this.gazeIds.length > MAX_GAZE_ARROWS) {
      const oldest = this.gazeIds.shift();
      const [f, t] = [oldest.slice(0, 2), oldest.slice(2)];
      this.removeArrow(ARROW.gaze, f, t);
      this.addArrow(ARROW.fading, f, t, ARROW_FADE_MS, () => this.removeArrow(ARROW.fading, f, t));
    }
  }

  ghost(from, to) {
    this.ghostId = from + to;
    if (this.gazeIds.includes(this.ghostId)) {
      this.gazeIds = this.gazeIds.filter((id) => id !== this.ghostId);
      this.removeArrow(ARROW.gaze, from, to);
    }
    this.addArrow(ARROW.ghost, from, to);
  }

  clearGaze() {
    this.removeMark(MARK.gaze);
    for (const type of [ARROW.gaze, ARROW.fading, ARROW.ghost]) this.removeArrow(type);
    this.gazeIds = [];
    this.ghostId = null;
  }

  destroy() {
    this.untrack("");
    this.cb.destroy();
  }

  addMark(type, square, lifetimeMs) {
    this.cb.removeMarkers(type, square);
    this.track(`m:${type.class}@${square}`, `g[data-square="${square}"] > use.${type.class}`, lifetimeMs, () =>
      this.removeMark(type, square),
    );
    this.cb.addMarker(type, square);
    this.sync();
  }

  removeMark(type, square) {
    this.untrack(`m:${type.class}@${square ?? ""}`);
    this.cb.removeMarkers(type, square);
    this.sync();
  }

  addArrow(type, from, to, lifetimeMs, onExpire) {
    const id = from + to;
    this.track(`a:${type.class}@${id}`, `g.arrow.${type.class.replace(" ", ".")}[data-arrow="${id}"]`, lifetimeMs, onExpire);
    this.cb.addArrow(type, from, to);
    this.sync();
  }

  removeArrow(type, from, to) {
    this.untrack(`a:${type.class}@${from ? from + to : ""}`);
    this.cb.removeArrows(type, from, to);
    this.sync();
  }

  track(key, selector, lifetimeMs, onExpire) {
    this.untrack(key);
    const timer = lifetimeMs ? setTimeout(onExpire, lifetimeMs) : null;
    this.live.set(key, { selector, at: performance.now(), timer });
  }

  untrack(prefix) {
    for (const [key, entry] of this.live) {
      if (!key.startsWith(prefix)) continue;
      clearTimeout(entry.timer);
      this.live.delete(key);
    }
  }

  /** Runs after every redraw: resume animations where they were, and give the ghost arrow its tooltip. */
  sync() {
    const now = performance.now();
    for (const { selector, at } of this.live.values()) {
      const el = this.el.querySelector(selector);
      if (el) el.style.animationDelay = `${at - now}ms`;
    }
    for (const arrow of this.el.querySelectorAll("g.arrow.zn-ghost")) {
      if (arrow.querySelector("title")) continue;
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = GHOST_TITLE;
      arrow.append(title);
    }
  }
}
