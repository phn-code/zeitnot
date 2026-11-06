"""FastAPI app: POST /api/think streams NDJSON, POST /api/ponder, GET /api/status, static files from web/."""

import asyncio
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import replace
from pathlib import Path

import chess
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .claude import ClaudeEngine, claude_available
from .codex import AppServer, CodexEngine, codex_logged_in
from .config import (CODEX_MODEL, DEFAULT_TIME_CONTROL, DETECT_TTL_S, FAKE_DELAY_S, LATENCY_SEED, ROOT, SETUP_HELP, SPEED_HINT,
                     Settings)
from .difficulty import Rater, Tier, make_rater
from .engines import Engine, EngineSet, FakeEngine, GeminiEngine, LatencyTable
from .events import to_ndjson
from .ledger import SpendLedger
from .player import Harness, ThinkRequest, one_move, think
from .ponder import Ponderer
from .rules import IllegalHistory, board_from_moves
from .timeman import INSTANT_PRIOR, Clock, plan, second_level

WEB_DIR = ROOT / "web"
SUITS = {"claude": "5+3 or slower", "codex": "15+10 or slower", "gemini": "3+2 or slower", "fake": "any time control"}
# Clocks the start screen can describe: each one's level per difficulty tier at move 10.
REGIME_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90)
REGIME_INCREMENTS = (0, 1, 2, 3, 5, 10, 15, 20, 30)
TIERS: tuple[Tier, ...] = ("easy", "normal", "hard")


class ClockBody(BaseModel):
    ai_ms: int = Field(ge=0)
    human_ms: int = Field(ge=0)
    increment_ms: int = Field(ge=0)


class ThinkBody(BaseModel):
    moves: list[str]
    clock: ClockBody
    model: str | None = None
    engine: str | None = None
    time_control: str | None = Field(default=None, max_length=16)
    opinions: bool | None = None
    ponder_hits: int = Field(default=0, ge=0)
    opponent_ms: int | None = Field(default=None, ge=0)


class PonderBody(ThinkBody):
    expected_reply: str


SUBSCRIPTIONS = ("claude", "codex")


class Detector:
    """Which subscription CLIs are installed and logged in, checked at most once per `ttl_s`.

    Re-checking on /api/status means logging in while the app runs makes the opponent appear.
    """

    def __init__(self, probes: Mapping[str, Callable[[], bool]], ttl_s: float = DETECT_TTL_S,
                 clock: Callable[[], float] = time.monotonic):
        self.probes = dict(probes)
        self.ttl_s = ttl_s
        self.clock = clock
        self.found: set[str] = set()
        self.checked_at: float | None = None
        self.lock = asyncio.Lock()

    async def check(self) -> set[str]:
        async with self.lock:
            if self.checked_at is None or self.clock() - self.checked_at >= self.ttl_s:
                results = await asyncio.gather(*(asyncio.to_thread(probe) for probe in self.probes.values()))
                self.found = {name for name, ok in zip(self.probes, results) if ok}
                self.checked_at = self.clock()
        return self.found


def default_detector(settings: Settings) -> Detector:
    if settings.fake:
        return Detector({})
    return Detector({"claude": lambda: claude_available(settings.claude_bare), "codex": codex_logged_in})


def build_engines(settings: Settings, claude_dir: Path | None = None) -> EngineSet:
    """Every engine this server could offer. Which subscription engines are offered is the Detector's call."""
    if settings.fake:
        return EngineSet({"fake": FakeEngine(settings.fake_delay_s)})
    engines: dict[str, Engine] = {
        "claude": ClaudeEngine(claude_dir or Path(tempfile.mkdtemp(prefix="zeitnot-claude-")), settings.claude_bare),
        "codex": CodexEngine(AppServer(), CODEX_MODEL, settings.codex_service_tier),
    }
    if settings.gemini and settings.api_key:
        from google import genai

        engines["gemini"] = GeminiEngine(genai.Client(api_key=settings.api_key), settings.gemini_models[0],
                                         LatencyTable(LATENCY_SEED["gemini"]))
    engines["fake"] = FakeEngine(FAKE_DELAY_S, priced=False)
    return EngineSet(engines)


def offered(engines: EngineSet, found: set[str]) -> list[str]:
    """Detected subscription engines first, Claude before Codex, then Gemini, and the fake demo when neither CLI is ready."""
    keys = [k for k in SUBSCRIPTIONS if k in found and k in engines]
    keys += [k for k in engines.engines if k not in SUBSCRIPTIONS and k != "fake"]
    if not any(k in SUBSCRIPTIONS for k in keys) and "fake" in engines:
        keys.append("fake")
    return keys


def engine_status(key: str, engine: Engine, default: bool, pondering: bool = False) -> dict:
    """One opponent for the start screen: who it is, how fast it is, and the time control it starts at."""
    minutes, increment = DEFAULT_TIME_CONTROL[engine.name]
    return {
        "id": key, "name": engine.name, "model": engine.model, "label": engine.label, "default": default,
        "free": not engine.paid, "suits": SUITS.get(engine.name), "hint": SPEED_HINT.get(engine.name, ""),
        "time_control": {"minutes": minutes, "increment": increment}, "latency": engine.latency.snapshot(),
        "regimes": regimes(engine, INSTANT_PRIOR if pondering else 0.0),
    }


class FreshStaticFiles(StaticFiles):
    """Static files the browser revalidates on every load, so a reload always runs the current web/ code."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def regimes(engine: Engine, instant: float = 0.0) -> dict[str, dict[str, str]]:
    """How deep the engine thinks at move 10 of each clock, per difficulty tier, plus the hard move's second opinion:
    {"10+5": {"easy": "medium", "normal": "high", "hard": "high", "second": "xhigh"}}. `instant` is the ponder hit
    rate a game starts from."""
    out = {}
    for minutes in REGIME_MINUTES:
        for increment in REGIME_INCREMENTS:
            clock = Clock(minutes * 60_000, minutes * 60_000, increment * 1000)
            plans = {t: plan(clock, 20, t, engine.latency, engine.main_levels, instant) for t in TIERS}
            hard = plans["hard"]
            out[f"{minutes}+{increment}"] = {**{t: p.level for t, p in plans.items()},
                                            "second": second_level(hard.level, hard.budget_ms, engine.latency, engine.main_levels)}
    return out


def create_app(settings: Settings, web_dir: Path = WEB_DIR, engines: EngineSet | None = None,
               detector: Detector | None = None, rater: Rater | None = None) -> FastAPI:
    engines = engines or build_engines(settings)
    detector = detector or default_detector(settings)
    rater = rater or make_rater(settings.maia, settings.maia_dir)
    harness = Harness(engines, SpendLedger(settings.ledger_path), settings, ponder=Ponderer(), rater=rater)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await detector.check()
        # Maia-2 takes a few seconds to load, minutes on first use to download it. Moves rated before it is ready use
        # the legal-move count, and a daemon thread never holds up shutdown.
        threading.Thread(target=getattr(rater, "load", lambda: None), name="maia-load", daemon=True).start()
        yield
        await harness.ponder.close()
        for engine in engines.engines.values():
            server = getattr(engine, "server", None)
            if isinstance(server, AppServer):
                await server.close()
            if isinstance(engine, ClaudeEngine) and engine.workdir.name.startswith("zeitnot-claude-"):
                shutil.rmtree(engine.workdir, ignore_errors=True)

    app = FastAPI(title="zeitnot", lifespan=lifespan)

    async def opponents() -> tuple[list[str], str]:
        keys = offered(engines, await detector.check())
        return keys, next((k for k in SUBSCRIPTIONS if k in keys), keys[0])

    async def engine_for(body: ThinkBody) -> str:
        """The engine by name, or the one serving a model, or the default. Only engines on offer right now."""
        keys, default = await opponents()
        if body.engine is not None:
            if body.engine not in keys:
                raise HTTPException(400, f"engine {body.engine!r} is not available, expected one of {keys}")
            return body.engine
        if body.model is None:
            return default
        for key in keys:
            if body.model == engines[key].model or (key in ("gemini", "fake") and body.model in settings.gemini_models):
                return key
        raise HTTPException(400, f"unknown model {body.model!r}")

    async def parse(body: ThinkBody) -> tuple[ThinkRequest, chess.Board]:
        name = await engine_for(body)
        try:
            board = board_from_moves(body.moves)
        except IllegalHistory as e:
            raise HTTPException(400, str(e)) from e
        if not any(board.legal_moves):
            raise HTTPException(400, "the game is over, there is no move to make")
        opinions = settings.opinions if body.opinions is None else body.opinions
        req = ThinkRequest(tuple(body.moves), Clock(**body.clock.model_dump()), name, engines[name].model, body.time_control,
                           opinions, ponder_hits=body.ponder_hits, opponent_ms=body.opponent_ms)
        return req, board

    @app.post("/api/think")
    async def api_think(body: ThinkBody) -> StreamingResponse:
        req, _ = await parse(body)

        async def lines() -> AsyncIterator[str]:
            async with aclosing(think(req, harness)) as events:
                async for event in events:
                    yield to_ndjson(event)

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.post("/api/ponder", status_code=202)
    async def api_ponder(body: PonderBody) -> dict:
        req, board = await parse(body)
        try:
            reply = chess.Move.from_uci(body.expected_reply)
        except ValueError as e:
            raise HTTPException(400, f"expected_reply {body.expected_reply!r} is not UCI") from e
        if reply not in board.legal_moves:
            raise HTTPException(400, f"expected_reply {body.expected_reply!r} is not legal here")
        base = board.fen()
        reply_san = board.san(reply)
        board.push(reply)
        fen = board.fen()
        if board.legal_moves.count() <= 1:
            harness.ponder.keep_base(req.engine, base)
            return {"status": "skipped", "fen": fen}
        target = replace(req, moves=req.moves + (reply.uci(),))
        status = harness.ponder.start(req.engine, fen, base,
                                      lambda: one_move(target, harness, time.monotonic(), ponder=reply_san))
        return {"status": status, "fen": fen}

    @app.get("/api/ponder/stream")
    async def api_ponder_stream(fen: str, engine: str) -> StreamingResponse:
        """The ponder run's events so far, then live ones until it ends. Its move is never sent here."""
        run = harness.ponder.get(engine, fen)
        if run is None:
            raise HTTPException(404, "nothing is pondering that position")

        async def lines() -> AsyncIterator[str]:
            async with aclosing(run.follow()) as events:
                async for event in events:
                    yield to_ndjson(event)

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.get("/api/status")
    async def api_status() -> dict:
        keys, default = await opponents()
        ready = any(k in SUBSCRIPTIONS for k in keys) or settings.fake
        return {
            "spent_usd": harness.ledger.spent_usd,
            "cap_usd": settings.cap_usd,
            "models": [engines[k].model for k in keys],
            "default_model": engines[default].model,
            "fake": settings.fake,
            "engines": [engine_status(k, engines[k], k == default, settings.ponder.get(engines[k].name, False)) for k in keys],
            "pondering": {k: settings.ponder.get(engines[k].name, False) for k in keys},
            "opinions": settings.opinions,
            "difficulty": harness.rater.name,
            "setup_help": [] if ready else [SETUP_HELP[k] for k in SUBSCRIPTIONS],
        }

    app.mount("/", FreshStaticFiles(directory=web_dir, html=True), name="web")
    return app
