"""Score each evaluation position with three Maia-2 difficulty measures, fixed before any result was seen:
expected loss under Maia's human move distribution (the primary), the chance of a 10-point blunder, and the
distribution's entropy. Blitz model, both players 1800.

usage: .venv/bin/python experiments/maia_check/score.py experiments/harness_eval/out/positions.jsonl \
           experiments/maia_check/out/scores.json ~/.cache/zeitnot/maia2
"""

import json
import math
import sys
import time

import chess
import torch
from maia2 import inference, model as maia_model

ELO = 1800
BLUNDER = 0.10


def forward(net, prepared, boards):
    """Move probabilities and the side-to-move expected score for each board, in one batch."""
    all_moves_dict, elo_dict, reverse = prepared
    tensors, legal = [], []
    for b in boards:
        x, es, eo, lm = inference.preprocessing(b.fen(), ELO, ELO, elo_dict, all_moves_dict)
        tensors.append((x, es, eo))
        legal.append(lm)
    x = torch.stack([t[0] for t in tensors])
    es = torch.tensor([t[1] for t in tensors])
    eo = torch.tensor([t[2] for t in tensors])
    lm = torch.stack(legal)
    with torch.no_grad():
        logits, _, value = net(x, es, eo)
    probs = inference._masked_softmax(logits, lm)
    value = (value / 2 + 0.5).clamp(0, 1)
    return probs, value, lm, reverse


def difficulty(net, prepared, board):
    children = list(board.legal_moves)
    after = []
    for m in children:
        b = board.copy(stack=False)
        b.push(m)
        after.append(b)
    playable = [i for i, b in enumerate(after) if not b.is_game_over()]
    probs, value, lm, reverse = forward(net, prepared, [board] + [after[i] for i in playable])
    mover_score = {}
    for k, i in enumerate(playable, start=1):
        mover_score[children[i]] = 1 - float(value[k])
    for i, b in enumerate(after):
        if b.is_game_over():
            outcome = b.outcome()
            mover_score[children[i]] = 1.0 if outcome.winner == board.turn else 0.5 if outcome.winner is None else 0.0
    root_probs = probs[0]
    policy = {}
    for idx in lm[0].nonzero().flatten().tolist():
        uci = reverse[idx]
        if board.turn == chess.BLACK:
            uci = inference.mirror_move(uci)
        policy[chess.Move.from_uci(uci)] = float(root_probs[idx])
    best = max(mover_score.values())
    exp_loss = sum(p * (best - mover_score[m]) for m, p in policy.items())
    p_blunder = sum(p for m, p in policy.items() if best - mover_score[m] >= BLUNDER)
    entropy = -sum(p * math.log(p) for p in policy.values() if p > 0)
    return {"exp_loss": exp_loss, "p_blunder": p_blunder, "entropy": entropy, "root_value": float(value[0]),
            "n_legal": len(children)}


def main(positions_path, out_path, model_dir):
    net = maia_model.from_pretrained(type="blitz", device="cpu", save_root=model_dir)
    net.eval()
    prepared = inference.prepare()
    out, times = {}, []
    for line in open(positions_path):
        p = json.loads(line)
        board = chess.Board(p["fen"])
        t = time.perf_counter()
        out[p["id"]] = difficulty(net, prepared, board)
        times.append(time.perf_counter() - t)
    json.dump({"elo": ELO, "model": "blitz", "scores": out, "seconds": times}, open(out_path, "w"), indent=1)
    times.sort()
    print(f"{len(out)} positions; seconds per position median {times[len(times)//2]:.3f}, max {times[-1]:.3f}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
