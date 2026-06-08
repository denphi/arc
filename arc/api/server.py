"""FastAPI application factory."""
from __future__ import annotations


from contextlib import asynccontextmanager

from fastapi import FastAPI

from arc.api.research_loop_routes import router as research_loop_router
from arc.api.routes import router
from arc.core.kernel import Kernel
from arc.version import __version__

_kernel: Kernel | None = None


def get_kernel() -> Kernel:
    if _kernel is None:
        raise RuntimeError("Kernel not initialized")
    return _kernel


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _kernel
    _kernel = Kernel()
    await _kernel.startup()
    app.state.kernel = _kernel
    yield
    await _kernel.shutdown()


def create_app(config_path: str = "arc.toml") -> FastAPI:
    app = FastAPI(
        title="ARC-Sim2L",
        description="Agentic research framework using Sim2L artifacts",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(research_loop_router)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
