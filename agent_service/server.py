"""
Agent Shield Audio — FastAPI server.

POST /v1/transcribe   {url, translate?, detect_injection?}  → transcript + injection verdict
GET  /v1/quote                                              → x402 price quote
GET  /                                                      → demo HTML
GET  /health                                                → liveness
GET  /.well-known/x402                                      → x402 service manifest
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field, HttpUrl

from pipeline import get_pipeline, TranscriptionResult

# ---------- config ----------
PRICE_USDC = float(os.environ.get("PRICE_USDC", "0.01"))        # flat per call (v1)
PAY_TO_ADDRESS = os.environ.get("PAY_TO_ADDRESS", "0x0000000000000000000000000000000000000000")
NETWORK = os.environ.get("NETWORK", "base-sepolia")
FACILITATOR_URL = os.environ.get("FACILITATOR_URL", "https://x402.org/facilitator")
USDC_CONTRACT = os.environ.get(
    "USDC_CONTRACT",
    # USDC on Base Sepolia
    "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
)
SERVICE_NAME = "Agent Shield Audio"
SERVICE_VERSION = "0.1.0"

app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)


# ---------- request / response schemas ----------

class TranscribeRequest(BaseModel):
    url: HttpUrl = Field(..., description="Audio or video URL (YouTube, podcast, direct mp3/wav)")
    translate: bool = Field(False, description="Also produce English translation")
    detect_injection: bool = Field(True, description="Scan transcript for prompt-injection attacks")


# ---------- x402 helpers ----------

def _x402_payment_required(price_usdc: float, resource: str) -> JSONResponse:
    """
    Returns a 402 response following the x402 spec.
    Spec: https://github.com/coinbase/x402  (see "PaymentRequirements")
    """
    payload = {
        "x402Version": 1,
        "error": "X-PAYMENT header missing or invalid",
        "accepts": [{
            "scheme": "exact",
            "network": NETWORK,
            "maxAmountRequired": str(int(price_usdc * 1_000_000)),  # USDC has 6 decimals
            "resource": resource,
            "description": f"{SERVICE_NAME}: transcription + injection scan",
            "mimeType": "application/json",
            "payTo": PAY_TO_ADDRESS,
            "maxTimeoutSeconds": 60,
            "asset": USDC_CONTRACT,
            "extra": {"name": "USDC", "version": "2"},
        }],
    }
    return JSONResponse(status_code=402, content=payload)


def _has_valid_payment(request: Request) -> bool:
    """
    v1 payment verification — accept any non-empty X-PAYMENT header as a stand-in for
    facilitator verification. The structural flow (402 → pay → 200) is what we're
    demonstrating; swapping in a real facilitator verify call is a config change.
    """
    header = request.headers.get("X-PAYMENT") or request.headers.get("x-payment")
    if not header:
        return False
    # Bypass for local demo: any header with at least 16 chars counts.
    return len(header) >= 16


# ---------- endpoints ----------

@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}


@app.get("/.well-known/x402")
async def x402_manifest():
    """Service manifest for x402-aware discovery (agentic.market, crawlers)."""
    return {
        "name": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "description": (
            "Multilingual audio transcription with optional English translation and "
            "voice-prompt-injection detection. Built for autonomous agents that consume "
            "audio content on behalf of humans."
        ),
        "endpoints": [{
            "path": "/v1/transcribe",
            "method": "POST",
            "price_usdc": PRICE_USDC,
            "network": NETWORK,
            "pay_to": PAY_TO_ADDRESS,
            "asset": USDC_CONTRACT,
        }],
        "novel_features": [
            "Voice prompt-injection detection on transcribed audio",
            "Multilingual transcribe + EN translation in a single call",
        ],
    }


@app.get("/v1/quote")
async def quote():
    """Returns the price for a call. v1: flat regardless of audio duration."""
    return {
        "price_usdc": PRICE_USDC,
        "network": NETWORK,
        "pay_to": PAY_TO_ADDRESS,
        "asset": USDC_CONTRACT,
        "note": "v1 uses flat pricing; per-minute billing arrives in v2",
    }


@app.post("/v1/transcribe")
async def transcribe(req: TranscribeRequest, request: Request):
    resource = str(request.url)
    if not _has_valid_payment(request):
        return _x402_payment_required(PRICE_USDC, resource)

    t0 = time.time()
    try:
        pipe = get_pipeline()
        # pipeline is CPU/GPU-blocking; run off the event loop so concurrent
        # requests aren't serialized at the HTTP layer.
        result: TranscriptionResult = await asyncio.to_thread(
            pipe.run,
            str(req.url),
            req.translate,
            req.detect_injection,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"pipeline error: {type(e).__name__}: {e}")

    body = result.to_dict()
    body["receipt"] = {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "request_url": str(req.url),
        "content_hash": hashlib.sha256(body["text"].encode("utf-8")).hexdigest(),
        "issued_at": int(time.time()),
        "total_seconds": round(time.time() - t0, 2),
    }
    return body


@app.get("/", response_class=HTMLResponse)
async def root():
    demo_path = os.path.join(os.path.dirname(__file__), "demo.html")
    if os.path.exists(demo_path):
        with open(demo_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(f"<h1>{SERVICE_NAME}</h1><p>POST /v1/transcribe</p>")


if __name__ == "__main__":
    import uvicorn
    # Eagerly load models so first request isn't slow.
    print("[server] warming pipeline...")
    get_pipeline()
    print("[server] pipeline ready")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
