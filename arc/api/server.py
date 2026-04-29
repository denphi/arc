"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from arc.api.routes import router
from arc.core.kernel import Kernel

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
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    return app


app = create_app()
