#!/bin/bash
# One-line-per-service readiness check. Safe to run repeatedly while the
# models are still loading.
set -u

probe() {  # probe <name> <url>
    local name=$1 url=$2
    local body
    body=$(curl -sf -m 4 "$url" 2>/dev/null)
    if [ -n "$body" ]; then
        printf '  %-13s UP    %s\n' "$name" "$(echo "$body" | head -c 120)"
    else
        printf '  %-13s ..... not ready\n' "$name"
    fi
}

echo "== services =="
probe ollama      "http://localhost:11434/api/tags"
probe tts         "http://localhost:${TTS_PORT:-8002}/health"
probe clinic-api  "http://localhost:${CLINIC_API_PORT:-8080}/api/health"
probe voice-agent "http://localhost:${VOICE_AGENT_PORT:-8100}/api/health"
probe voice-pcm   "http://localhost:${VOICE_AGENT_PCM_PORT:-8101}/api/health"

echo
echo "== resident models =="
/workspace/bin/ollama ps 2>/dev/null | tail -n +1 || echo "  ollama not reachable"

echo
echo "== gpu =="
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
