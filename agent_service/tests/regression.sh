#!/usr/bin/env bash
# Comprehensive regression for Agent Shield Audio.
# Assumes server on :8765 and test audio on :9000.

set +e
SERVER="${SERVER:-http://localhost:8765}"
AUDIO="${AUDIO:-http://127.0.0.1:9000}"
echo "Testing against SERVER=$SERVER  AUDIO=$AUDIO"
PAY='X-PAYMENT: demo-payment-proof-0123456789abcdef'
SHORT_PAY='X-PAYMENT: short'

pass=0; fail=0
ok()    { echo "  PASS: $1"; pass=$((pass+1)); }
bad()   { echo "  FAIL: $1"; fail=$((fail+1)); }
hr()    { echo; echo "─── $1 ───"; }

j_get() { python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$1'))"; }

# ========== A. HEALTH / DISCOVERY ==========
hr "A. Health / Discovery"
[ "$(curl -s $SERVER/health | j_get status)" = "ok" ] && ok "/health returns ok" || bad "/health"

manifest=$(curl -s $SERVER/.well-known/x402)
[ "$(echo "$manifest" | j_get name)" = "Agent Shield Audio" ] && ok "/.well-known/x402 names service" || bad "manifest name"

quote=$(curl -s $SERVER/v1/quote)
[ "$(echo "$quote" | j_get price_usdc)" = "0.01" ] && ok "/v1/quote shows price=0.01" || bad "quote price"
[ "$(echo "$quote" | j_get network)" = "base-sepolia" ] && ok "/v1/quote shows network" || bad "quote network"

[ "$(curl -s $SERVER/ -o /dev/null -w '%{http_code}')" = "200" ] && ok "/ serves demo page" || bad "demo page"

# ========== B. x402 PAYMENT FLOW ==========
hr "B. x402 Payment Flow"
r=$(curl -s -o /tmp/r.json -w '%{http_code}' -X POST $SERVER/v1/transcribe \
    -H 'Content-Type: application/json' \
    -d "{\"url\":\"$AUDIO/clean.mp3\"}")
[ "$r" = "402" ] && ok "no X-PAYMENT → 402" || bad "expected 402 got $r"
v=$(cat /tmp/r.json | j_get x402Version)
[ "$v" = "1" ] && ok "402 body has x402Version=1" || bad "x402Version got '$v'"
amt=$(python3 -c "import json; d=json.load(open('/tmp/r.json')); print(d['accepts'][0]['maxAmountRequired'])")
[ "$amt" = "10000" ] && ok "maxAmountRequired = 10000 (USDC 6-decimal)" || bad "amount got '$amt'"
net=$(python3 -c "import json; d=json.load(open('/tmp/r.json')); print(d['accepts'][0]['network'])")
[ "$net" = "base-sepolia" ] && ok "network = base-sepolia" || bad "network got '$net'"
scheme=$(python3 -c "import json; d=json.load(open('/tmp/r.json')); print(d['accepts'][0]['scheme'])")
[ "$scheme" = "exact" ] && ok "scheme = exact" || bad "scheme got '$scheme'"

# Short header should also fail (< 16 chars in our stub validator)
r=$(curl -s -o /dev/null -w '%{http_code}' -X POST $SERVER/v1/transcribe \
    -H 'Content-Type: application/json' -H "$SHORT_PAY" \
    -d "{\"url\":\"$AUDIO/clean.mp3\"}")
[ "$r" = "402" ] && ok "short X-PAYMENT (<16 chars) → 402" || bad "expected 402 got $r"

# ========== C. CORE PIPELINE ==========
hr "C. Core Pipeline — known-good test audio"

# C1: Clean English, no translate, detect=on → must be clean
r=$(curl -s -X POST $SERVER/v1/transcribe -H 'Content-Type: application/json' -H "$PAY" \
    -d "{\"url\":\"$AUDIO/clean.mp3\",\"detect_injection\":true}")
det=$(echo "$r" | j_get injection_detected)
lang=$(echo "$r" | j_get language_detected)
[ "$det" = "False" ] && ok "C1 clean en: injection_detected=False" || bad "C1 got injection_detected=$det"
[ "$lang" = "en" ] && ok "C1 detects English" || bad "C1 lang=$lang"
[ -n "$(echo "$r" | j_get text)" ] && ok "C1 produces transcript" || bad "C1 empty text"

# C2: Injection English, detect=on → must catch
r=$(curl -s -X POST $SERVER/v1/transcribe -H 'Content-Type: application/json' -H "$PAY" \
    -d "{\"url\":\"$AUDIO/injection.mp3\",\"detect_injection\":true}")
det=$(echo "$r" | j_get injection_detected)
reason=$(echo "$r" | j_get injection_reason)
[ "$det" = "True" ] && ok "C2 injection en: injection_detected=True" || bad "C2 got injection_detected=$det"
echo "$reason" | grep -qi "keyword\|prompt-guard" && ok "C2 reason: $reason" || bad "C2 reason: $reason"

# C3: Spanish + translate → must produce English translation, no false positive
r=$(curl -s -X POST $SERVER/v1/transcribe -H 'Content-Type: application/json' -H "$PAY" \
    -d "{\"url\":\"$AUDIO/spanish.mp3\",\"translate\":true,\"detect_injection\":true}")
det=$(echo "$r" | j_get injection_detected)
lang=$(echo "$r" | j_get language_detected)
trans=$(echo "$r" | j_get translation_en)
[ "$det" = "False" ] && ok "C3 spanish clean: injection_detected=False" || bad "C3 got injection_detected=$det"
[ "$lang" = "es" ] && ok "C3 detects Spanish" || bad "C3 lang=$lang"
echo "$trans" | grep -qi "podcast\|technology\|talk" && ok "C3 produces English translation" || bad "C3 translation: $trans"

# C4: detect_injection=false on injection audio → should still transcribe, NOT flag
r=$(curl -s -X POST $SERVER/v1/transcribe -H 'Content-Type: application/json' -H "$PAY" \
    -d "{\"url\":\"$AUDIO/injection.mp3\",\"detect_injection\":false}")
det=$(echo "$r" | j_get injection_detected)
reason=$(echo "$r" | j_get injection_reason)
[ "$det" = "False" ] && ok "C4 detect_injection=false: not flagged even on injection audio" || bad "C4 got $det"
[ "$reason" = "detection disabled" ] && ok "C4 reason='detection disabled'" || bad "C4 reason=$reason"

# C5: translate=true on already-English audio → translation_en populated, same content
r=$(curl -s -X POST $SERVER/v1/transcribe -H 'Content-Type: application/json' -H "$PAY" \
    -d "{\"url\":\"$AUDIO/clean.mp3\",\"translate\":true,\"detect_injection\":true}")
trans=$(echo "$r" | j_get translation_en)
det=$(echo "$r" | j_get injection_detected)
[ -n "$trans" ] && [ "$trans" != "None" ] && ok "C5 en+translate: translation_en populated" || bad "C5 translation_en=$trans"
[ "$det" = "False" ] && ok "C5 en+translate clean: not flagged" || bad "C5 got $det"

# ========== D. RECEIPT ==========
hr "D. Receipt integrity"
r=$(curl -s -X POST $SERVER/v1/transcribe -H 'Content-Type: application/json' -H "$PAY" \
    -d "{\"url\":\"$AUDIO/clean.mp3\"}")
text=$(echo "$r" | python3 -c "import json,sys;print(json.load(sys.stdin)['text'])")
hash_ret=$(echo "$r" | python3 -c "import json,sys;print(json.load(sys.stdin)['receipt']['content_hash'])")
hash_calc=$(python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode('utf-8')).hexdigest())" "$text")
[ "$hash_ret" = "$hash_calc" ] && ok "receipt.content_hash matches sha256(text)" || bad "hash mismatch: $hash_ret vs $hash_calc"

issued=$(echo "$r" | python3 -c "import json,sys;print(json.load(sys.stdin)['receipt']['issued_at'])")
now=$(date +%s)
diff=$((now - issued))
[ "$diff" -ge 0 ] && [ "$diff" -lt 60 ] && ok "receipt.issued_at within 60s of now (diff=${diff}s)" || bad "issued_at diff=$diff"

# ========== E. ERROR HANDLING ==========
hr "E. Error handling"

# E1: Missing url field → 422
r=$(curl -s -o /dev/null -w '%{http_code}' -X POST $SERVER/v1/transcribe \
    -H 'Content-Type: application/json' -H "$PAY" -d '{}')
[ "$r" = "422" ] && ok "missing url → 422" || bad "expected 422 got $r"

# E2: Invalid url string → 422
r=$(curl -s -o /dev/null -w '%{http_code}' -X POST $SERVER/v1/transcribe \
    -H 'Content-Type: application/json' -H "$PAY" -d '{"url":"not-a-url"}')
[ "$r" = "422" ] && ok "invalid url → 422" || bad "expected 422 got $r"

# E3: Empty body → 422
r=$(curl -s -o /dev/null -w '%{http_code}' -X POST $SERVER/v1/transcribe \
    -H 'Content-Type: application/json' -H "$PAY" -d '')
[ "$r" = "422" ] && ok "empty body → 422" || bad "expected 422 got $r"

# E4: Wrong method (GET) → 405
r=$(curl -s -o /dev/null -w '%{http_code}' -X GET $SERVER/v1/transcribe -H "$PAY")
[ "$r" = "405" ] && ok "GET /v1/transcribe → 405" || bad "expected 405 got $r"

# E5: Unreachable URL → 500 (downstream fetch error)
r=$(curl -s -o /tmp/r.json -w '%{http_code}' -X POST $SERVER/v1/transcribe \
    -H 'Content-Type: application/json' -H "$PAY" \
    -d '{"url":"http://127.0.0.1:1/nope.mp3"}')
[ "$r" = "500" ] && ok "unreachable URL → 500" || bad "expected 500 got $r"

# ========== F. RESPONSE SHAPE ==========
hr "F. Response shape"
r=$(curl -s -X POST $SERVER/v1/transcribe -H 'Content-Type: application/json' -H "$PAY" \
    -d "{\"url\":\"$AUDIO/clean.mp3\"}")
python3 - <<EOF
import json, sys
d = json.loads('''$r''')
required = ['language_detected','duration_seconds','text','translation_en','segments',
            'injection_detected','injection_score','injection_reason','pipeline_latency_seconds','receipt']
missing = [k for k in required if k not in d]
print('  PASS: all required fields present' if not missing else f'  FAIL: missing {missing}')
seg = d['segments'][0] if d['segments'] else {}
seg_required = ['start','end','text','injection_score','injection_flagged']
seg_missing = [k for k in seg_required if k not in seg]
print('  PASS: segment shape ok' if not seg_missing else f'  FAIL: segment missing {seg_missing}')
EOF

# ========== SUMMARY ==========
hr "Summary"
echo "  pass: $pass"
echo "  fail: $fail"
[ "$fail" = "0" ] && echo "  RESULT: ALL GREEN" || echo "  RESULT: ${fail} FAILURE(S)"
