"""Does Maia-2 difficulty predict Opus's blunders better than the signals we have? Runs in the project venv.

Opus's labels come from the graded calls in experiments/harness_eval/out: a blunder loses 10 or more points of
Lichess win%. AUCs carry 95% bootstrap intervals; with 10 blunders in 60 positions they are wide.

usage: .venv/bin/python experiments/maia_check/analyze.py . experiments/maia_check/out/scores.json
"""

import json
import random
import statistics as st
import sys
from collections import defaultdict

root, scores_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
import chess  # noqa: E402
from zeitnot.rules import hanging, safe_moves  # noqa: E402


def old_critical(board):
    """The v3 rule zeitnot/facts.py used to set effort, kept here as the baseline: they just captured, you are in
    check, a piece of yours is hanging, or fewer than half the legal moves are safe."""
    if board.move_stack:
        last = board.pop()
        captured = board.is_capture(last)
        board.push(last)
        if captured:
            return True
    return (board.is_check() or bool(hanging(board, board.turn))
            or len(safe_moves(board)) < 0.5 * board.legal_moves.count())

OUT = f"{root}/experiments/harness_eval/out"
BLUNDER = 10.0
maia = json.load(open(scores_path))["scores"]
pos = {json.loads(l)["id"]: json.loads(l) for l in open(f"{OUT}/positions.jsonl")}
grades = json.load(open(f"{OUT}/grades.json"))
egrades = json.load(open(f"{OUT}/effort_grades.json"))


def loss(pid, san):
    win = {**grades[pid]["win"], **egrades[pid]["win"]}
    best = max(grades[pid]["best"], egrades[pid]["best"])
    return best - win[san] if san in win else None


calls = defaultdict(dict)
pooled = defaultdict(list)
for path in ("calls.jsonl", "effort.jsonl"):
    for line in open(f"{OUT}/{path}"):
        r = json.loads(line)
        if r.get("engine") != "claude" or not r.get("move"):
            continue
        kind = r.get("kind") or r.get("level")
        calls[kind][r["pid"]] = r["move"]
        if (l := loss(r["pid"], r["move"])) is not None:
            pooled[r["pid"]].append(l >= BLUNDER)


def auc(scores, labels):
    pos_ = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos_ or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos_ for n in neg)
    return wins / (len(pos_) * len(neg))


def auc_ci(scores, labels, n=2000, seed=7):
    rng = random.Random(seed)
    idx = list(range(len(scores)))
    vals = []
    for _ in range(n):
        s = [rng.choice(idx) for _ in idx]
        a = auc([scores[i] for i in s], [labels[i] for i in s])
        if a == a:
            vals.append(a)
    vals.sort()
    return auc(scores, labels), vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals))]


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    for k, i in enumerate(order):
        r[i] = k
    return r


def show(name, scores, labels):
    a, lo, hi = auc_ci(scores, labels)
    print(f"  {name:44} AUC {a:.2f}  [{lo:.2f}, {hi:.2f}]")


def board_of(pid):
    """The position with its history, so the old rule can see the last move."""
    b = chess.Board()
    for uci in pos[pid]["history"]:
        b.push_uci(uci)
    assert b.fen() == pos[pid]["fen"], pid
    return b


def signals(ids, opus_move):
    out = {}
    out["Maia expected loss (primary)"] = [maia[i]["exp_loss"] for i in ids]
    out["Maia blunder probability"] = [maia[i]["p_blunder"] for i in ids]
    out["Maia policy entropy"] = [maia[i]["entropy"] for i in ids]
    out["rules: position critical"] = [float(old_critical(board_of(i))) for i in ids]
    from zeitnot.rules import check_move
    flags = []
    for i in ids:
        b = chess.Board(pos[i]["fen"])
        flags.append(float(check_move(b, b.parse_san(opus_move[i])) is not None))
    out["rules: Opus's move fails the check"] = flags
    out["legal move count"] = [chess.Board(pos[i]["fen"]).legal_moves.count() for i in ids]
    return out


print("== Target A. Opus v2 at low effort blunders (loss >= 10 win%), all 60 positions")
v2 = calls["v2"]
ids = [i for i in pos if i in v2 and loss(i, v2[i]) is not None]
labels = [loss(i, v2[i]) >= BLUNDER for i in ids]
print(f"  n={len(ids)}, blunders={sum(labels)}")
sig = signals(ids, v2)
for name, s in sig.items():
    show(name, s, labels)
dis = [float(calls["v1"].get(i) not in (None, v2[i])) for i in ids]
show("Opus disagreement: v1 prompt vs v2 prompt", dis, labels)
both = [i for i in ids if i in calls["second"]]
if both:
    lab2 = [loss(i, v2[i]) >= BLUNDER for i in both]
    show(f"Opus disagreement: second opinion (n={len(both)})", [float(calls['second'][i] != v2[i]) for i in both], lab2)
    show(f"Maia expected loss on the same {len(both)}", [maia[i]["exp_loss"] for i in both], lab2)
combo = [a + b for a, b in zip(rank(sig["Maia expected loss (primary)"]), rank(dis))]
show("Maia rank + v1/v2 disagreement rank", combo, labels)

print("\n== Target B. Any Opus answer blundered, pooling every graded Opus call per position")
ids_b = [i for i in pos if pooled[i]]
lab_b = [any(pooled[i]) for i in ids_b]
rate = [sum(pooled[i]) / len(pooled[i]) for i in ids_b]
print(f"  n={len(ids_b)}, positions with a blunder={sum(lab_b)}, Opus answers per position median={st.median(len(pooled[i]) for i in ids_b)}")
for name, s in signals(ids_b, {i: calls['v2'][i] for i in ids_b}).items():
    if name.startswith("rules: Opus"):
        continue
    show(name, s, lab_b)
print(f"  Spearman(Maia expected loss, Opus blunder rate) = {st.correlation(rank([maia[i]['exp_loss'] for i in ids_b]), rank(rate)):+.2f}")

print("\n== Sanity. Does Maia see 'only one move works'? (Stockfish gap between best and second best)")
ids_c = list(pos)
gap = [grades[i]["gap"] for i in ids_c]
for k in ("exp_loss", "p_blunder", "entropy"):
    print(f"  Spearman(Maia {k}, gap) = {st.correlation(rank([maia[i][k] for i in ids_c]), rank(gap)):+.2f}")
mv = [chess.Board(pos[i]["fen"]).fullmove_number for i in ids_c]
print(f"  Spearman(Maia exp_loss, move number) = {st.correlation(rank([maia[i]['exp_loss'] for i in ids_c]), rank(mv)):+.2f}")

print("\n== Confounds. Is Maia only counting legal moves, and does it add to the second opinion?")
legal = [chess.Board(pos[i]["fen"]).legal_moves.count() for i in ids]
el = [maia[i]["exp_loss"] for i in ids]
pb = [maia[i]["p_blunder"] for i in ids]
print(f"  Spearman(Maia expected loss, legal count) = {st.correlation(rank(el), rank(legal)):+.2f}")
print(f"  Spearman(Maia blunder probability, legal count) = {st.correlation(rank(pb), rank(legal)):+.2f}")


def residual(a, b):
    ra, rb = rank(a), rank(b)
    slope, intercept = st.linear_regression(rb, ra)
    return [x - (slope * z + intercept) for x, z in zip(ra, rb)]


show("Maia expected loss, legal count removed", residual(el, legal), labels)
show("Maia blunder probability, legal count removed", residual(pb, legal), labels)
if both:
    dis2 = [float(calls["second"][i] != v2[i]) for i in both]
    for name, key in (("expected loss", "exp_loss"), ("blunder probability", "p_blunder")):
        m = [maia[i][key] for i in both]
        show(f"second opinion + Maia {name} (rank sum, n={len(both)})", [a + b for a, b in zip(rank(dis2), rank(m))], lab2)
