"""FastAPI app: POST /api/think streams NDJSON, GET /api/status, static files from web/."""

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from pydantic import BaseModel, Field

from .config import ROOT, Settings
from .ledger import SpendLedger
from .models import FakeClient, GeminiClient, ModelClient
from .player import ThinkRequest, think, to_ndjson
from .rules import IllegalHistory, board_from_moves
from .timeman import Clock

WEB_DIR = ROOT / "web"


class ClockBody(BaseModel):
    ai_ms: int = Field(ge=0)
    human_ms: int = Field(ge=0)
    increment_ms: int = Field(ge=0)


class ThinkBody(BaseModel):
    moves: list[str]
    clock: ClockBody
    model: str | None = None


def make_client_factory(settings: Settings) -> Callable[[str], ModelClient]:
    if settings.fake:
        fake = FakeClient(settings.fake_delay_s)
        return lambda model: fake
    if not settings.api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Put it in .env, or run with ZEITNOT_FAKE_MODEL=1.")
    shared = genai.Client(api_key=settings.api_key)
    return lambda model: GeminiClient(shared, model)


def create_app(settings: Settings, web_dir: Path = WEB_DIR) -> FastAPI:
    app = FastAPI(title="zeitnot")
    ledger = SpendLedger(settings.ledger_path)
    client_for = make_client_factory(settings)

    @app.post("/api/think")
    async def api_think(body: ThinkBody) -> StreamingResponse:
        model = body.model or settings.default_model
        if model not in settings.models:
            raise HTTPException(400, f"unknown model {model!r}, expected one of {list(settings.models)}")
        try:
            board = board_from_moves(body.moves)
        except IllegalHistory as e:
            raise HTTPException(400, str(e)) from e
        if not any(board.legal_moves):
            raise HTTPException(400, "the game is over, there is no move to make")
        req = ThinkRequest(tuple(body.moves), Clock(**body.clock.model_dump()), model)

        async def lines() -> AsyncIterator[str]:
            async with aclosing(think(req, client_for(model), ledger, settings)) as events:
                async for event in events:
                    yield to_ndjson(event)

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.get("/api/status")
    async def api_status() -> dict:
        return {
            "spent_usd": ledger.spent_usd,
            "cap_usd": settings.cap_usd,
            "models": list(settings.models),
            "default_model": settings.default_model,
            "fake": settings.fake,
        }

    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
