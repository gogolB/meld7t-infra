"""FastAPI app entrypoint (spec §5)."""
from fastapi import FastAPI

from .routes import router

app = FastAPI(title="MELD 7T Platform API", version="0.1.0")
app.include_router(router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
