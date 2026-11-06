"""Spread of Maia-2 blunder probability over ordinary positions past ply 10 (Maia-2 never trained on the first 10
plies), from the real blitz games behind the evaluation set. zeitnot/difficulty.py takes its cut points from this.

usage: .venv/bin/python experiments/maia_check/calibrate.py experiments/harness_eval/out/positions.jsonl ~/.cache/zeitnot/maia2 300
"""
import json, random, sys
import chess
sys.path.insert(0, sys.argv[0].rsplit("/", 1)[0])
from score import difficulty
from maia2 import inference, model as maia_model

seen, pool = set(), []
for line in open(sys.argv[1]):
    b = chess.Board()
    for u in json.loads(line)["history"]:
        if b.fen() not in seen and b.legal_moves.count() > 1 and b.ply() >= 10:
            seen.add(b.fen()); pool.append(b.fen())
        b.push_uci(u)
random.Random(11).shuffle(pool)
net = maia_model.from_pretrained(type="blitz", device="cpu", save_root=sys.argv[2]); net.eval()
prep = inference.prepare()
rows = []
for fen in pool[: int(sys.argv[3])]:
    b = chess.Board(fen)
    d = difficulty(net, prep, b)
    rows.append((d["p_blunder"], d["exp_loss"], b.legal_moves.count(), b.fullmove_number))
def pct(xs, q): xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))]
print(f"n={len(rows)} of {len(pool)} unique positions")
print(f"share below 1%: {sum(r[0] < 0.01 for r in rows) / len(rows):.2f}, at 10% or above: {sum(r[0] >= 0.10 for r in rows) / len(rows):.2f}")
for name, k in (("p_blunder", 0), ("exp_loss", 1), ("legal", 2)):
    xs = [r[k] for r in rows]
    print(name, " ".join(f"p{int(q*100)}={pct(xs, q):.3f}" for q in (0.1, 0.25, 0.3, 0.5, 0.7, 0.75, 0.9)))
for lo, hi in ((6, 8), (9, 15), (16, 25), (26, 99)):
    xs = [r[0] for r in rows if lo <= r[3] <= hi]
    if xs: print(f"moves {lo}-{hi}: n={len(xs)} median p_blunder={pct(xs, 0.5):.3f} p75={pct(xs, 0.75):.3f}")
