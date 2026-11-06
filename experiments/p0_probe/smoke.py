import json, time, chess
from dotenv import load_dotenv
from openai import OpenAI
load_dotenv()
c = OpenAI()
b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3")
legal = sorted(b.san(m) for m in b.legal_moves)
t = time.perf_counter()
d = c.decisions.create(model="gpt-6-luna", input=f"Chess position (FEN): {b.fen()}\nWhite to move.", questions=[
    {"name": "move", "type": "choice", "instructions": "Choose the best move for White.", "choices": [{"value": s} for s in legal]},
    {"name": "sharpness", "type": "score", "instructions": "How much does the choice of move matter here?",
     "levels": [{"label": "Many moves are fine"}, {"label": "A few moves are clearly better"}, {"label": "Only one or two moves keep the position"}, {"label": "One exact move is required"}]},
])
dt = time.perf_counter() - t
a = d.answers[0]
top = sorted(a.probabilities, key=lambda p: -p.probability)[:4]
print(f"decisions {dt:.2f}s choice={a.choice} conf={a.confidence:.3f} top={[(p.value, round(p.probability,3)) for p in top]} sum={sum(p.probability for p in a.probabilities):.3f}")
print("sharpness", d.answers[1].score, d.answers[1].confidence, "usage", d.usage.model_dump())
t = time.perf_counter()
r = c.responses.create(model="gpt-6-luna", reasoning={"effort": "none"}, input=f"Chess position (FEN): {b.fen()}\nWhite to move. Pick the best move.",
    text={"format": {"type": "json_schema", "name": "move", "strict": True, "schema": {"type": "object", "properties": {"move": {"type": "string", "enum": legal}}, "required": ["move"], "additionalProperties": False}}})
print(f"responses(none) {time.perf_counter()-t:.2f}s status={r.status} out={r.output_text} usage={r.usage.model_dump()}")
