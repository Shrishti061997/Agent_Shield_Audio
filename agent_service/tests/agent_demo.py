#!/usr/bin/env python3
"""
agent_demo.py — simulates an autonomous agent paying for Agent Shield Audio.

This is the screen-recording. It shows the x402 wire flow end to end:

  1. Agent POSTs /v1/transcribe with NO payment      -> 402 + payment requirements
  2. Agent reads the x402 'accepts' block (price, asset, network, payTo)
  3. Agent attaches an X-PAYMENT proof and retries     -> 200 + transcript + verdict

v1 NOTE (matches README): the server's v1 verifier accepts any non-empty
X-PAYMENT proof to demonstrate the wire shape; a production facilitator
verify() call is the single swap-in. This client demonstrates the protocol
flow, not on-chain settlement — exactly the v1 scope the bundle documents.

Usage:
    # server on :8765, fixtures served on :9000 (same as regression.sh)
    python3 tests/agent_demo.py
    # or point elsewhere:
    SERVER=http://localhost:8000 AUDIO=http://127.0.0.1:9000 python3 tests/agent_demo.py
"""
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("SERVER", "http://localhost:8765")
AUDIO = os.environ.get("AUDIO", "http://127.0.0.1:9000")

# A stand-in payment proof. >=16 chars to satisfy the v1 stub verifier
# (see server.py _has_valid_payment). In production this is the
# facilitator-signed x402 payment payload.
PAYMENT_PROOF = "demo-payment-proof-0123456789abcdef"


def _post(path, body, headers=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        SERVER + path, data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def bar(t):
    print("\n" + "=" * 70)
    print(f"  {t}")
    print("=" * 70)


def run_case(title, audio_file, opts):
    bar(title)
    body = {"url": f"{AUDIO}/{audio_file}", **opts}
    print(f"Agent wants: POST /v1/transcribe  {json.dumps(body)}")

    # Step 1 — no payment.
    print("\n[1] Agent calls with NO payment ...")
    status, payload = _post("/v1/transcribe", body)
    print(f"    <- HTTP {status}")
    if status == 402:
        acc = payload["accepts"][0]
        usdc = int(acc["maxAmountRequired"]) / 1_000_000
        print(f"    Server requires payment: {usdc} USDC ({acc['network']})")
        print(f"    Pay to: {acc['payTo']}  asset: {acc['asset']}")
        print(f"    scheme: {acc['scheme']}  x402Version: {payload['x402Version']}")
    else:
        print(f"    Unexpected (expected 402): {payload}")
        return

    # Step 2 — agent "pays" and retries.
    print("\n[2] Agent attaches X-PAYMENT proof and retries ...")
    status, payload = _post("/v1/transcribe", body,
                            headers={"X-PAYMENT": PAYMENT_PROOF})
    print(f"    <- HTTP {status}")
    if status != 200:
        print(f"    Unexpected (expected 200): {payload}")
        return

    # Step 3 — show the result the agent acts on.
    print("\n[3] Agent receives transcript + safety verdict:")
    print(f"    language_detected : {payload['language_detected']}")
    print(f"    duration_seconds  : {payload['duration_seconds']}")
    text = payload["text"]
    print(f"    text              : {text[:90]}{'...' if len(text) > 90 else ''}")
    if payload.get("translation_en"):
        tr = payload["translation_en"]
        print(f"    translation_en    : {tr[:90]}{'...' if len(tr) > 90 else ''}")
    verdict = "INJECTION DETECTED" if payload["injection_detected"] else "clean"
    print(f"    injection_detected: {payload['injection_detected']}  ({verdict})")
    print(f"    injection_reason  : {payload['injection_reason']}")
    if payload["injection_detected"]:
        print("    -> Agent discards the injected instruction and does NOT act on it.")
    else:
        print("    -> Agent proceeds; content is safe to use.")
    print(f"    receipt.content_hash: {payload['receipt']['content_hash'][:24]}...")


def main():
    # Quick liveness so the recording fails fast if the server isn't up.
    try:
        with urllib.request.urlopen(SERVER + "/health", timeout=5) as r:
            if json.loads(r.read())["status"] != "ok":
                raise RuntimeError
    except Exception:
        print(f"ERROR: server not reachable at {SERVER} (start server.py first)",
              file=sys.stderr)
        sys.exit(1)

    print(f"Agent Shield Audio — autonomous agent demo")
    print(f"server={SERVER}  audio={AUDIO}")

    # Case 1: clean podcast -> agent gets a usable transcript, no flag.
    run_case("CASE 1 — clean English audio",
             "clean.mp3", {"detect_injection": True})

    # Case 2: poisoned audio -> agent is warned, refuses the injected order.
    run_case("CASE 2 — audio carrying a voice prompt-injection",
             "injection.mp3", {"detect_injection": True})

    # Case 3: Spanish + translate -> transcript, EN translation, no false flag.
    run_case("CASE 3 — Spanish audio, translate to English",
             "spanish.mp3", {"translate": True, "detect_injection": True})

    bar("DONE — agent paid per call over x402, got transcript + safety verdict")
    print()


if __name__ == "__main__":
    main()
