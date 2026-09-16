#!/bin/bash
# ============================================================================
# Kolkata Care Diagnostics voice agent -- ZERO-TO-RUNNING setup for VAST.AI.
#
# This is the script the repo never had. setup_addon.sh is NOT this: it is an
# add-on for a RunPod pod where voice-to-rx-repo has ALREADY provisioned NeMo,
# Ollama and the models, and it opens by refusing to run without them:
#
#     FATAL: /workspace/voice-to-rx-repo not found
#     FATAL: nemo not importable
#     FATAL: ollama not found
#     FATAL: qwen2.5:7b not pulled
#
# All four are true on a fresh vast.ai instance, so setup_addon.sh cannot be
# used here at all. This script builds that world from nothing instead.
#
#   bash deploy/setup_vast.sh 2>&1 | tee /workspace/setup_vast.log
#
# Idempotent: every phase checks before it acts, so re-running after a failure
# resumes rather than redoing. Safe to run as many times as you like.
#
# NOT everything can be automated. Two artifacts are licence-gated and need a
# credential this script cannot invent -- it stops with an exact instruction
# rather than half-installing and failing later inside a live call:
#   * IndicConformer  -- gated on HuggingFace, needs HF_TOKEN + accepted licence
#   * Indic-TTS       -- FastPitch/HiFi-GAN Bengali checkpoints
# ============================================================================
set -uo pipefail

REPO_URL="${REPO_URL:-https://github.com/innofissionaigit-design/ivr.git}"
REPO_DIR="${REPO_DIR:-/workspace/kolkata-care-voice-agent}"
# The vast.ai deploy files live on a branch until they are merged, so the
# clone below has to ask for it explicitly -- a default-branch clone would
# not contain this script's own siblings (env.vast.sh, the start_all.sh
# change), and the stack would come up with the RunPod environment instead.
REPO_BRANCH="${REPO_BRANCH:-dev-chakravardhan}"
VENV="${VENV:-/workspace/venv}"
NEMO_DIR="${NEMO_DIR:-/workspace/AI4Bharat_NeMo}"
SILERO_DIR="${SILERO_DIR:-/workspace/silero-vad}"
TTS_CKPT="${TTS_CKPT:-/workspace/tts_checkpoints}"

ok()    { printf '   OK   %s\n' "$1"; }
skip()  { printf '   --   %s\n' "$1"; }
warn()  { printf '   WARN %s\n' "$1"; }
die()   { printf '\n   FATAL %s\n\n' "$1"; exit 1; }
phase() { printf '\n==================================================================\n %s\n==================================================================\n' "$1"; }

# ---------------------------------------------------------------------------
phase "0. Preflight"
# ---------------------------------------------------------------------------
command -v nvidia-smi >/dev/null || die "no nvidia-smi -- this is not a GPU instance."
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | sed 's/^/   GPU: /'

VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
if [ "${VRAM_MB:-0}" -lt 14000 ] 2>/dev/null; then
    warn "only ${VRAM_MB}MB VRAM. Working set is ~10GB (ASR 1.2 + Qwen Q4 4.7 + bge-m3 2.2 + TTS 1). Expect OOM."
fi

# torch 2.1.0 (pinned in requirements.txt) ships kernels for sm_37..sm_90.
# Blackwell (RTX 50-series) is sm_120 and will fail at the first CUDA op with
# "no kernel image is available for execution on the device". Catch that here,
# not 40 minutes into a model download.
CAP=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.')
if [ -n "$CAP" ] && [ "$CAP" -gt 90 ] 2>/dev/null; then
    die "GPU compute capability sm_${CAP} is newer than torch 2.1.0 supports (max sm_90).
        This is a Blackwell / RTX 50-series card and torch 2.1.0 has no kernels for it.
        Use an Ada (RTX 40xx) or Ampere (RTX 30xx / A-series) instance, or bump the
        torch pin in requirements.txt and re-validate the NeMo fork against it."
fi
ok "GPU compute capability sm_${CAP} is within torch 2.1.0 range"

mkdir -p /workspace/bin /workspace/logs /workspace/.cache
FREE_GB=$(df -BG /workspace 2>/dev/null | awk 'NR==2{gsub("G","",$4); print $4}')
if [ "${FREE_GB:-0}" -lt 55 ] 2>/dev/null; then
    warn "only ${FREE_GB}GB free on /workspace. Need ~55GB (venv ~15, models ~12, TTS ~8, headroom)."
else
    ok "${FREE_GB}GB free on /workspace"
fi

# ---------------------------------------------------------------------------
phase "1. System packages"
# ---------------------------------------------------------------------------
# ffmpeg is NOT optional: main.py's _decode_to_wav() spawns it once per poll
# per call. Without it every turn silently fails to decode and the agent never
# hears anybody -- with no error, because _decode_to_wav just returns False and
# the poll loop treats that as "not enough data yet".
NEED_PKGS=""
for p in ffmpeg git curl build-essential libsndfile1; do
    dpkg -s "$p" >/dev/null 2>&1 || NEED_PKGS="$NEED_PKGS $p"
done
if [ -n "$NEED_PKGS" ]; then
    apt-get update -qq && apt-get install -y -qq $NEED_PKGS || die "apt install failed:$NEED_PKGS"
    ok "installed:$NEED_PKGS"
else
    skip "system packages already present"
fi
command -v ffmpeg >/dev/null || die "ffmpeg still missing -- main.py cannot decode any audio without it."
ok "ffmpeg present"

# ---------------------------------------------------------------------------
phase "2. Python venv + torch 2.1.0"
# ---------------------------------------------------------------------------
# torch 2.1.0 supports Python 3.8-3.11 only. On 3.12+ there is no 2.1.0 wheel
# and pip silently resolves to a much newer torch, which then breaks the fork.
PY=""
for c in python3.11 python3.10 python3.9; do
    command -v "$c" >/dev/null && { PY=$c; break; }
done
if [ -z "$PY" ]; then
    SYSPY=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "unknown")
    die "no python3.9/3.10/3.11 found (system python3 is ${SYSPY}).
        torch 2.1.0 has no wheel for 3.12+. Install one:
          apt-get install -y python3.11 python3.11-venv python3.11-dev"
fi
ok "using $PY"

if [ ! -x "$VENV/bin/python3" ]; then
    "$PY" -m venv "$VENV" || die "venv creation failed at $VENV"
    ok "created venv at $VENV"
else
    skip "venv exists at $VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip setuptools wheel

if ! "$VENV/bin/python3" -c "import torch" 2>/dev/null; then
    echo "   installing torch 2.1.0 + torchaudio 2.1.0 (cu121, ~2.5GB) ..."
    "$VENV/bin/pip" install -q torch==2.1.0 torchaudio==2.1.0 \
        --index-url https://download.pytorch.org/whl/cu121 || die "torch install failed"
    ok "torch installed"
else
    skip "torch already installed"
fi

"$VENV/bin/python3" -c "import torch, sys; print('   torch', torch.__version__, '| cuda:', torch.cuda.is_available(), '|', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NO GPU'); sys.exit(0 if torch.cuda.is_available() else 1)" \
    || die "torch cannot see the GPU"
ok "torch sees the GPU"

echo "   installing service dependencies ..."
"$VENV/bin/pip" install -q \
    "httpx==0.27.0" "fastapi==0.104.1" "uvicorn[standard]" "soundfile==0.12.1" \
    sqlalchemy pydantic huggingface_hub omegaconf || die "pip install of service deps failed"
ok "service dependencies installed"

# ---------------------------------------------------------------------------
phase "3. Ollama + LLM/embedding models"
# ---------------------------------------------------------------------------
# Installed to /workspace/bin, not /usr/local/bin: keeps the whole stack on the
# rented disk so it survives a stop/start as one unit.
if [ ! -x /workspace/bin/ollama ]; then
    echo "   downloading ollama ..."
    curl -fsSL https://ollama.com/download/ollama-linux-amd64.tgz -o /tmp/ollama.tgz \
        || die "ollama download failed"
    tar -xzf /tmp/ollama.tgz -C /workspace 2>/dev/null || tar -xzf /tmp/ollama.tgz -C /workspace/bin
    rm -f /tmp/ollama.tgz
    [ -x /workspace/bin/ollama ] || die "ollama binary not at /workspace/bin/ollama after extract"
    ok "ollama installed to /workspace/bin"
else
    skip "ollama already at /workspace/bin"
fi

export OLLAMA_MODELS=/workspace/.ollama/models
export OLLAMA_KEEP_ALIVE=-1
if ! pgrep -x ollama >/dev/null; then
    setsid /workspace/bin/ollama serve > /workspace/logs/ollama.log 2>&1 < /dev/null &
    for i in $(seq 1 30); do
        curl -sf -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 && break
        sleep 1
    done
    ok "ollama serving"
else
    skip "ollama already running"
fi
curl -sf -m 5 http://localhost:11434/api/tags >/dev/null 2>&1 \
    || die "ollama not responding on :11434 -- see /workspace/logs/ollama.log"

for M in "qwen2.5:7b" "bge-m3"; do
    if /workspace/bin/ollama list 2>/dev/null | grep -q "${M%%:*}"; then
        skip "$M already pulled"
    else
        echo "   pulling $M ..."
        /workspace/bin/ollama pull "$M" || die "ollama pull $M failed"
        ok "$M pulled"
    fi
done

# ---------------------------------------------------------------------------
phase "4. AI4Bharat NeMo fork"
# ---------------------------------------------------------------------------
# Mainline NeMo CANNOT load this checkpoint: IndicConformer uses a multilingual
# AGGREGATE tokenizer and mainline's _setup_monolingual_tokenizer raises
# KeyError: 'dir'. Only AI4Bharat's fork handles it -- see agent/asr.py.
if [ ! -d "$NEMO_DIR/.git" ]; then
    git clone --depth 1 -b nemo-v2 https://github.com/AI4Bharat/NeMo.git "$NEMO_DIR" \
        || die "NeMo fork clone failed (branch nemo-v2)"
    ok "cloned AI4Bharat/NeMo @ nemo-v2"
else
    skip "NeMo fork present at $NEMO_DIR"
fi

if ! PYTHONPATH="$NEMO_DIR" "$VENV/bin/python3" -c "import nemo.collections.asr" 2>/dev/null; then
    echo "   installing NeMo ASR dependencies (several minutes) ..."
    "$VENV/bin/pip" install -q -r "$NEMO_DIR/requirements/requirements_asr.txt" 2>/dev/null \
        || warn "some NeMo asr requirements failed -- checking the import anyway"
    if PYTHONPATH="$NEMO_DIR" "$VENV/bin/python3" -c "import nemo.collections.asr" 2>/dev/null; then
        ok "nemo.collections.asr imports"
    else
        warn "nemo.collections.asr STILL not importable -- ASR will not load. Inspect manually."
    fi
else
    skip "nemo.collections.asr already imports"
fi

# ---------------------------------------------------------------------------
phase "5. Silero VAD"
# ---------------------------------------------------------------------------
# Cloned locally rather than pulled through torch.hub at runtime: vad_stream.py
# prefers SILERO_VAD_REPO when the directory exists, so a local copy means a
# fresh process does not depend on github being reachable mid-call.
if [ ! -d "$SILERO_DIR/.git" ]; then
    git clone --depth 1 https://github.com/snakers4/silero-vad.git "$SILERO_DIR" \
        || die "silero-vad clone failed"
    ok "cloned silero-vad"
else
    skip "silero-vad present at $SILERO_DIR"
fi

# ---------------------------------------------------------------------------
phase "6. This repo"
# ---------------------------------------------------------------------------
if [ ! -d "$REPO_DIR/.git" ]; then
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR" \
        || git clone "$REPO_URL" "$REPO_DIR" \
        || die "repo clone failed"
    ok "cloned into $REPO_DIR (branch: $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD))"
else
    skip "repo present at $REPO_DIR (branch: $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD))"
fi

# Refuse to continue on a checkout that lacks the vast.ai environment file:
# start_all.sh would silently fall back to env.sh (the RunPod one), which has
# no OLLAMA_NUM_PARALLEL bound and none of the vast.ai port notes.
[ -f "$REPO_DIR/deploy/env.vast.sh" ] \
    || die "$REPO_DIR/deploy/env.vast.sh missing -- the checkout is on a branch
        without the vast.ai deploy files. Re-run with:
          REPO_BRANCH=dev-chakravardhan bash deploy/setup_vast.sh"

# clinic-api/main.py seeds itself at startup only when the table is EMPTY, so
# there is nothing to seed here -- just confirm the deps are importable.
if "$VENV/bin/python3" -c "import sqlalchemy, pydantic" 2>/dev/null; then
    ok "clinic-api dependencies present (it seeds itself on first start)"
else
    warn "sqlalchemy/pydantic missing -- clinic-api will not start"
fi

# ---------------------------------------------------------------------------
phase "7. Gated artifacts -- cannot be automated"
# ---------------------------------------------------------------------------
NEEDS_ASR=0
NEEDS_TTS=0

export HF_HOME=/workspace/.cache/huggingface
if find "$HF_HOME" -name "*.nemo" 2>/dev/null | grep -q .; then
    ok "IndicConformer checkpoint present"
elif [ -n "${HF_TOKEN:-}" ]; then
    echo "   HF_TOKEN set -- downloading IndicConformer (~1GB) ..."
    if "$VENV/bin/python3" -c "import os; from huggingface_hub import snapshot_download; snapshot_download('ai4bharat/indicconformer_stt_bn_hybrid_ctc_rnnt_large', token=os.environ['HF_TOKEN'])"; then
        ok "IndicConformer downloaded"
    else
        warn "IndicConformer download failed -- is the licence accepted on your HF account?"
        NEEDS_ASR=1
    fi
else
    NEEDS_ASR=1
fi

if [ -f "$TTS_CKPT/bn/fastpitch/best_model.pth" ]; then
    ok "Indic-TTS Bengali checkpoints present"
else
    NEEDS_TTS=1
fi

# ---------------------------------------------------------------------------
phase "SUMMARY"
# ---------------------------------------------------------------------------
if [ "$NEEDS_ASR" = "1" ]; then
    echo "   [ ] ASR -- IndicConformer NOT installed. It is a GATED HuggingFace model."
    echo "       1. Accept the licence while logged in to your HF account:"
    echo "          https://huggingface.co/ai4bharat/indicconformer_stt_bn_hybrid_ctc_rnnt_large"
    echo "       2. Re-run with your token:"
    echo "          export HF_TOKEN=hf_xxxxx && bash deploy/setup_vast.sh"
    echo "       Until then the agent will not start: agent/asr.py raises"
    echo "       FileNotFoundError when TurnASR() is constructed during app startup."
    echo
fi
if [ "$NEEDS_TTS" = "1" ]; then
    echo "   [ ] TTS -- Indic-TTS Bengali checkpoints NOT installed."
    echo "       Needs FastPitch + HiFi-GAN Bengali models from AI4Bharat/Bhashini,"
    echo "       laid out exactly as tts_server.py expects:"
    echo "         $TTS_CKPT/bn/fastpitch/{best_model.pth,config.json,speakers.pth}"
    echo "         $TTS_CKPT/bn/hifigan/{best_model.pth,config.json}"
    echo "       Plus its own venv at /workspace/tts_venv (the AI4Bharat coqui-tts"
    echo "       fork -- mainline coqui-tts is Python <3.11 only; see tts_server.py)."
    echo "       Without it the agent still runs, but every reply falls back to the"
    echo "       clips in static/fallback_audio/ -- which are gitignored and must be"
    echo "       recorded once themselves."
    echo
fi
if [ "$NEEDS_ASR" = "0" ] && [ "$NEEDS_TTS" = "0" ]; then
    echo "   Everything is installed. Start the stack:"
    echo "       source $REPO_DIR/deploy/env.vast.sh"
    echo "       bash   $REPO_DIR/deploy/start_all.sh"
    echo "       bash   $REPO_DIR/deploy/status.sh"
else
    echo "   Everything that COULD be automated is done: ollama + qwen2.5:7b + bge-m3,"
    echo "   torch 2.1.0 on GPU, the NeMo fork, silero-vad, the repo and the clinic"
    echo "   API. Only the gated artifacts above remain."
fi
echo
