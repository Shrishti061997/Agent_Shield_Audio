#!/usr/bin/env bash
# One-shot environment setup for Agent Shield Audio.
#
# Detects GPU vs CPU, picks the right venv + requirements file + Whisper model,
# installs deps idempotently, verifies ffmpeg + torch.
#
# Usage:
#   ./setup.sh           install + verify, print the run command
#   ./setup.sh run       install + verify + start the server on $PORT (default 8765)
#   ./setup.sh --force   wipe the venv and reinstall from scratch
#
# Env overrides:
#   PORT (default 8765)
#   PAY_TO_ADDRESS (default 0x...dEaD placeholder — set your real Base wallet)

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'
ok()   { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}!${NC} %s\n" "$1"; }
err()  { printf "${RED}✗${NC} %s\n" "$1" >&2; }
info() { printf "${DIM}→${NC} %s\n" "$1"; }

cd "$(dirname "$0")"

# ---------- parse args ----------
RUN_AFTER=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    run)     RUN_AFTER=1 ;;
    --force) FORCE=1 ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) err "unknown arg: $arg"; exit 2 ;;
  esac
done

# ---------- detect GPU ----------
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  MODE=gpu
  VENV=".venv"
  REQS="requirements.txt"
  MODEL="large-v3"
  HFCACHE=".hf_cache"
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
  VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "0")
  ok "GPU detected: ${GPU_NAME} (${VRAM_MB} MiB)"
else
  MODE=cpu
  VENV=".venv-cpu"
  REQS="requirements-cpu.txt"
  MODEL="small"
  HFCACHE=".hf_cache_cpu"
  warn "no GPU detected — using CPU build (Whisper '${MODEL}', ~5-6x slower)"
fi

# ---------- check Python ----------
if ! command -v python3 >/dev/null 2>&1; then
  err "python3 not found. Install Python 3.11 or 3.12."
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ok "python3 ${PY_VER}"

# ---------- check ffmpeg ----------
if ! command -v ffmpeg >/dev/null 2>&1; then
  err "ffmpeg not found. Install one of:"
  echo "    conda install -y -c conda-forge ffmpeg"
  echo "    sudo apt install ffmpeg     # Ubuntu/Debian"
  echo "    brew install ffmpeg          # macOS"
  exit 1
fi
ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# ---------- venv ----------
if [ "$FORCE" = 1 ] && [ -d "$VENV" ]; then
  info "wiping ${VENV} (--force)"
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  info "creating ${VENV}"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip --quiet
fi
ok "venv ready: ${VENV}"

# ---------- install deps ----------
# Idempotent — pip is a no-op when everything's satisfied.
info "installing ${REQS}"
"$VENV/bin/pip" install -r "$REQS" --quiet 2>&1 | tail -3 || true

# ---------- verify torch sees the expected device ----------
DEVICE_CHECK=$("$VENV/bin/python" - <<'PY'
import torch
print(f"{torch.__version__}|{torch.cuda.is_available()}")
PY
)
TORCH_VER="${DEVICE_CHECK%|*}"
CUDA_AVAIL="${DEVICE_CHECK#*|}"

if [ "$MODE" = "gpu" ] && [ "$CUDA_AVAIL" != "True" ]; then
  err "torch installed but cuda.is_available()=False. GPU mode broken."
  exit 1
fi
if [ "$MODE" = "cpu" ] && [ "$CUDA_AVAIL" = "True" ]; then
  warn "CPU mode requested but torch still sees CUDA. Did you reuse a GPU venv?"
fi
ok "torch ${TORCH_VER} (cuda=${CUDA_AVAIL})"

# ---------- print run command ----------
PORT="${PORT:-8765}"
PAY_TO_ADDRESS="${PAY_TO_ADDRESS:-0x000000000000000000000000000000000000dEaD}"

echo
printf "${GREEN}Setup complete (%s mode).${NC}\n\n" "$MODE"
cat <<EOF
To run the server:

  HF_HOME=\$(pwd)/${HFCACHE} \\
  WHISPER_MODEL=${MODEL} \\
  PAY_TO_ADDRESS=${PAY_TO_ADDRESS} \\
  PORT=${PORT} \\
  ${VENV}/bin/python server.py

Demo UI:        http://localhost:${PORT}/
x402 manifest:  http://localhost:${PORT}/.well-known/x402

EOF

# ---------- optional auto-run ----------
if [ "$RUN_AFTER" = 1 ]; then
  info "launching server..."
  exec env HF_HOME="$(pwd)/${HFCACHE}" \
           WHISPER_MODEL="${MODEL}" \
           PAY_TO_ADDRESS="${PAY_TO_ADDRESS}" \
           PORT="${PORT}" \
           "${VENV}/bin/python" server.py
fi
