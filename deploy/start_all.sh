#!/bin/bash
# Bring the whole stack up after a pod restart. Idempotent: run it as many
# times as you like.
#
# This exists because RunPod wipes the container overlay on every restart,
# so "the pod is back" and "the service is back" are different events. The
# manual rebuild that used to sit between them was the single largest time
# sink in this project.
#
#   bash /workspace/kolkata-care-voice-agent/deploy/start_all.sh
#
# Every service is launched with setsid + </dev/null so it is fully
# detached from the ssh session. A plain background job dies with the ssh
# channel, which has silently left services down after a deploy more than
# once -- and one memorable time, a pkill pattern matched the launching
# ssh command's own command line and killed the thing doing the launching.
set -u

REPO=/workspace/kolkata-care-voice-agent
source "$REPO/deploy/${VOICE_AGENT_ENV_FILE:-env.sh}"
mkdir -p /workspace/logs /workspace/bin

start() {  # start <name> <port> <logfile> <command...>
    local name=$1 port=$2 log=$3; shift 3
    if curl -sf -m 3 "http://localhost:$port/api/health" >/dev/null 2>&1 \
    || curl -sf -m 3 "http://localhost:$port/health" >/dev/null 2>&1; then
        echo "  $name already up on :$port"
        return
    fi
    # Kill by PORT, never by command-line pattern: a pattern broad enough
    # to match the service is also broad enough to match this script.
    fuser -k "$port/tcp" >/dev/null 2>&1
    sleep 1
    setsid "$@" > "$log" 2>&1 < /dev/null &
    echo "  $name starting on :$port (log: $log)"
}

echo "== ollama =="
if ! pgrep -x ollama >/dev/null; then
    if [ -x /workspace/bin/ollama ]; then
        setsid /workspace/bin/ollama serve > /workspace/logs/ollama.log 2>&1 < /dev/null &
        echo "  ollama starting"
        sleep 5
    else
        echo "  !! /workspace/bin/ollama missing -- run deploy/install_ollama.sh"
    fi
else
    echo "  ollama already running"
fi

echo "== services =="
cat > /workspace/bin/_run_tts.sh <<'EOF'
#!/bin/bash
source /workspace/kolkata-care-voice-agent/deploy/${VOICE_AGENT_ENV_FILE:-env.sh}
cd /workspace/tts_venv
exec ./bin/python3 -m uvicorn tts_server:app --host 0.0.0.0 --port ${TTS_PORT:-8002} --app-dir /workspace/tts_venv
EOF
cat > /workspace/bin/_run_clinic.sh <<'EOF'
#!/bin/bash
source /workspace/kolkata-care-voice-agent/deploy/${VOICE_AGENT_ENV_FILE:-env.sh}
cd /workspace/kolkata-care-voice-agent/clinic-api
exec /workspace/venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port ${CLINIC_API_PORT:-8080}
EOF
cat > /workspace/bin/_run_main.sh <<'EOF'
#!/bin/bash
source /workspace/kolkata-care-voice-agent/deploy/${VOICE_AGENT_ENV_FILE:-env.sh}
cd /workspace/kolkata-care-voice-agent
exec /workspace/venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port ${VOICE_AGENT_PORT:-8100}
EOF
cat > /workspace/bin/_run_pcm.sh <<'EOF'
#!/bin/bash
source /workspace/kolkata-care-voice-agent/deploy/${VOICE_AGENT_ENV_FILE:-env.sh}
cd /workspace/kolkata-care-voice-agent
exec /workspace/venv/bin/python3 -m uvicorn main_pcm:app --host 0.0.0.0 --port ${VOICE_AGENT_PCM_PORT:-8101}
EOF
chmod +x /workspace/bin/_run_*.sh

start tts        "${TTS_PORT:-8002}" /workspace/logs/tts_server.log /workspace/bin/_run_tts.sh
start clinic-api "${CLINIC_API_PORT:-8080}" /workspace/logs/clinic_api.log /workspace/bin/_run_clinic.sh
start voice-agent "${VOICE_AGENT_PORT:-8100}" /workspace/logs/main_app.log  /workspace/bin/_run_main.sh
start voice-pcm  "${VOICE_AGENT_PCM_PORT:-8101}" /workspace/logs/pcm_app.log    /workspace/bin/_run_pcm.sh

echo
echo "Models load for 1-3 minutes. Watch readiness with:"
echo "  bash $REPO/deploy/status.sh"
