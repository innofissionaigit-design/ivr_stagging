# Environment for every service in the stack, on VAST.AI.
#
# WHY A SEPARATE FILE FROM env.sh
# -------------------------------
# env.sh encodes a RunPod-specific fact: RunPod wipes the container overlay
# ("/" and "/root") on every restart and keeps only the network volume, so
# everything MUST live under /workspace or it is lost on reboot.
#
# Vast.ai does not work that way. An instance's container filesystem IS the
# rented disk and it persists across stop/start, so the "/workspace or it
# vanishes" rule does not apply. /workspace is still used below purely as a
# convention, so paths match env.sh and nothing else in the repo has to know
# which provider it is running on.
#
# The real vast.ai differences are further down: PORTS and the DB backend.

export VOICE_AGENT_PROVIDER=vast

export HF_HOME=/workspace/.cache/huggingface
export TORCH_HOME=/workspace/.cache/torch
export HF_HUB_ENABLE_HF_TRANSFER=0

export OLLAMA_MODELS=/workspace/.ollama/models
# -1 keeps models resident indefinitely. Without it Ollama unloads after
# five idle minutes and the next caller pays a 47s cold start mid-call
# (measured -- see agent/llm.py's _call_ollama docstring).
export OLLAMA_KEEP_ALIVE=-1

# Bound Ollama's own internal queue. Unset, concurrent turns queue INSIDE
# ollama where this codebase cannot see them, and a caller waiting in that
# queue is indistinguishable from a cold start -- so llm.py's 90s timeout
# never fires and the socket dies first. See the concurrency audit, P1.
export OLLAMA_NUM_PARALLEL=2
export OLLAMA_MAX_QUEUE=32

export PYTHONPATH=/workspace/AI4Bharat_NeMo:/workspace/kolkata-care-voice-agent:

# SQLite on the instance disk. Same reasoning as env.sh: a 74-row
# read-mostly catalogue with one writer process does not need Postgres, and
# clinic-api/db.py already defaults here. Set DATABASE_URL to override.
export CLINIC_DB_PATH=/workspace/clinic.db

# CLINIC-API IS ON 8081, NOT 8080 -- deliberate, and a real collision, not taste.
# The vastai/base-image template this instance uses already binds 8080 for
# Jupyter, and advertises it in PORTAL_CONFIG as both "Jupyter" and
# "Jupyter Terminal" (localhost:8080:8080:/terminals/1). Leaving clinic-api on
# 8080 means whichever process starts second fails to bind -- and start_all.sh
# would report the port as already healthy, because its `start()` probe treats
# ANY 200 on /api/health OR /health as "already up on :8080". Jupyter answering
# that probe would make a dead clinic-api look successfully started.
export CLINIC_API_BASE=http://localhost:8081
export TTS_URL=http://localhost:8002/synthesize
export SILERO_VAD_REPO=/workspace/silero-vad

export PATH=/workspace/bin:/workspace/venv/bin:${PATH:-}

# ---------------------------------------------------------------------------
# PATIENT HISTORY -- disclosed only after verification
# Author: Chakravardhan
# ---------------------------------------------------------------------------
# The handset is SHARED. Everything here follows from that: the phone number
# says which record to LOOK AT and nothing about who is holding the phone.
#
# NOTE THERE IS NO OTP SETTING. An SMS code goes to the same shared handset
# the caller is already holding, so it proves possession of a phone that by
# the story's own premise proves nothing. Verification is knowledge-based --
# a PIN set at the counter, or a date of birth. See clinic-api/verification.py.

# Master switch. A clinic that decides no medical history should ever go
# down a phone line sets this to 0 and the flow answers "ask at the counter".
export VOICE_AGENT_HISTORY_DISCLOSURE=${VOICE_AGENT_HISTORY_DISCLOSURE:-1}

# Refuse to speak history when the audio path is a speakerphone, or cannot
# be classified at all. THIS IS THE "cannot HEAR" HALF OF THE STORY, and it
# is separate from verification: a correctly verified patient with the phone
# on loudspeaker is entitled to their history and must still not have it read
# to the room.
#
# SET TO 0 ONLY ON A BENCH POD. A browser microphone on laptop speakers
# classifies as speakerphone on every call, so a developer testing the
# verification flow would otherwise never get past this gate. It must be 1 in
# production; the audit row records which mode a disclosure happened under so
# the setting is visible after the fact.
export VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH=${VOICE_AGENT_HISTORY_REQUIRE_PRIVATE_PATH:-1}

# Wrong answers before the NUMBER is locked. Three, not five: an honest
# caller mistypes once, maybe twice. Counted per PATIENT, so hanging up and
# redialling does not refill the budget.
export VOICE_AGENT_VERIFY_MAX_ATTEMPTS=${VOICE_AGENT_VERIFY_MAX_ATTEMPTS:-3}

# Long enough that guessing a 4-digit PIN is pointless, short enough that a
# patient who fumbled can try again after tea instead of travelling in.
export VOICE_AGENT_VERIFY_LOCKOUT_MINUTES=${VOICE_AGENT_VERIFY_LOCKOUT_MINUTES:-30}

# How long a verification lasts WITHIN one call. Never across calls -- the
# handset is shared, and the last caller may have hung up in someone else's
# hand.
export VOICE_AGENT_VERIFY_TTL_MINUTES=${VOICE_AGENT_VERIFY_TTL_MINUTES:-10}

# PBKDF2 rounds for the PIN. A 4-digit PIN is not made strong by hashing --
# MAX_ATTEMPTS does that. What this buys is that a database dump is not
# instantly a list of every patient's PIN.
export VOICE_AGENT_VERIFY_PBKDF2_ROUNDS=${VOICE_AGENT_VERIFY_PBKDF2_ROUNDS:-120000}

# ---------------------------------------------------------------------------
# LANGUAGES -- Bengali, Hindi, English
# ---------------------------------------------------------------------------
# UNSET MEANS BENGALI ONLY, and that is the correct default today. The ASR
# checkpoint on this pod is indicconformer_stt_bn_* -- a BENGALI-ONLY model.
# It cannot transcribe Hindi or English, and no configuration changes that.
#
# agent/language.py's enabled() will NOT advertise a language whose ASR
# checkpoint variable is unset, precisely so this file cannot claim a
# capability the pod does not have. Listing "hi" below without setting
# VOICE_AGENT_NEMO_FILE_HI does nothing at all -- deliberately.
export VOICE_AGENT_LANGUAGES=${VOICE_AGENT_LANGUAGES:-bn}
export VOICE_AGENT_DEFAULT_LANG=${VOICE_AGENT_DEFAULT_LANG:-bn}

# fixed | script | parallel -- see agent/language.py.
#   fixed    every caller gets the default language. What the system has
#            always done, and the only honest setting while ASR is one
#            Bengali checkpoint.
#   script   read the transcript's script. Only meaningful once ASR can
#            EMIT more than one script.
#   parallel decode turn one with every enabled checkpoint and keep the
#            best. The only true audio-based detection, and the most
#            expensive: N decodes on turn one, on a shared GPU.
export VOICE_AGENT_LANG_STRATEGY=${VOICE_AGENT_LANG_STRATEGY:-fixed}

# Per-language ASR checkpoints. Setting one is what actually turns a
# language on. Loaded LAZILY, on the first caller who speaks it, because
# each IndicConformer checkpoint is VRAM on a card already holding
# Qwen2.5 and the TTS voices.
export VOICE_AGENT_NEMO_FILE_HI=${VOICE_AGENT_NEMO_FILE_HI:-}
export VOICE_AGENT_NEMO_FILE_EN=${VOICE_AGENT_NEMO_FILE_EN:-}

# Per-language TTS. tts_server.py looks for <TTS_CKPT_ROOT>/<lang> when the
# explicit variable is empty, and falls back to the Bengali voice when a
# language has no checkpoint -- reporting which language it ACTUALLY spoke
# in the X-TTS-Lang response header, so the agent is never lied to.
export TTS_CKPT_ROOT=${TTS_CKPT_ROOT:-/workspace/tts_checkpoints}
export TTS_CKPT_HI=${TTS_CKPT_HI:-}
export TTS_CKPT_EN=${TTS_CKPT_EN:-}
export TTS_SPEAKER_HI=${TTS_SPEAKER_HI:-}
export TTS_SPEAKER_EN=${TTS_SPEAKER_EN:-}

# ---------------------------------------------------------------------------
# HOSPITAL SMS GATEWAY -- the patient's written confirmation
# ---------------------------------------------------------------------------
# Identical to env.sh's block; see there for the full reasoning. Repeated
# rather than sourced because these two files are deliberately standalone --
# one is sourced, never both.
#
# THE SECRETS ARE NOT IN THIS FILE AND MUST NOT BE ADDED TO IT. Export
# HOSPITAL_GATEWAY_API_KEY and HOSPITAL_GATEWAY_DLR_TOKEN from an
# uncommitted deploy/env.secret.sh sourced after this one.
#
# ONE VAST.AI-SPECIFIC CONSEQUENCE. The delivery-receipt callback,
# POST /api/v1/notifications/receipt, lives on clinic-api -- and CLINIC_API_PORT
# above is INTERNAL ONLY, never published (see the PORTS section below). So
# the gateway cannot reach it on a stock vast.ai instance, and receipts will
# not arrive. Every message therefore sticks at `sent` and the staff queue
# reports it stale after HOSPITAL_GATEWAY_STALE_MINUTES -- which is correct
# behaviour reporting a real gap, not a bug to suppress.
#
# To actually collect receipts here, the instance needs clinic-api on a
# published port, and ports are fixed AT CREATION on vast.ai. Until then,
# treat `sent` as the terminal state on this provider.
export HOSPITAL_GATEWAY_URL=${HOSPITAL_GATEWAY_URL:-}
export HOSPITAL_GATEWAY_AUTH_HEADER=${HOSPITAL_GATEWAY_AUTH_HEADER:-Authorization}
export HOSPITAL_GATEWAY_SENDER_ID=${HOSPITAL_GATEWAY_SENDER_ID:-}
export HOSPITAL_GATEWAY_ENTITY_ID=${HOSPITAL_GATEWAY_ENTITY_ID:-}
export HOSPITAL_GATEWAY_TEMPLATE_BOOKED=${HOSPITAL_GATEWAY_TEMPLATE_BOOKED:-}
export HOSPITAL_GATEWAY_TEMPLATE_RESCHEDULED=${HOSPITAL_GATEWAY_TEMPLATE_RESCHEDULED:-}
export HOSPITAL_GATEWAY_TEMPLATE_CANCELLED=${HOSPITAL_GATEWAY_TEMPLATE_CANCELLED:-}
export HOSPITAL_GATEWAY_COUNTRY_CODE=${HOSPITAL_GATEWAY_COUNTRY_CODE:-91}
export HOSPITAL_GATEWAY_TIMEOUT_S=${HOSPITAL_GATEWAY_TIMEOUT_S:-8}

# How long a `sent` message may go without a delivery receipt before the
# staff queue calls it a failure. See notifications.DEFAULT_STALE_MINUTES.
export HOSPITAL_GATEWAY_STALE_MINUTES=${HOSPITAL_GATEWAY_STALE_MINUTES:-15}

# ---------------------------------------------------------------------------
# PORTS -- the one thing that genuinely differs from RunPod
# ---------------------------------------------------------------------------
# RunPod fronts services with an HTTPS proxy that upgrades WebSockets on the
# same origin as the page. Vast.ai does not: it maps each container port to
# an arbitrary EXTERNAL port on the host's public IP, reachable as
# http://<PUBLIC_IP>:<EXTERNAL_PORT>.
#
# This costs no code change, and that is worth stating explicitly rather than
# discovering later: static/index.html builds its socket URL as
#
#     `${wsProtocol}//${window.location.host}/ws/audio`
#
# -- relative to whatever host:port served the page. main.py serves its own
# static files (app.mount("/")), so the browser hits the same mapped port it
# loaded the page from and the URL resolves correctly on its own.
#
# TWO CONSEQUENCES that DO matter:
#
# 1. Ports must be requested AT RENT TIME in the instance's Docker options:
#        -p 8100:8100 -p 8101:8101 -p 8080:8080 -p 8002:8002
#    They cannot be added to a running instance. 8100 is the only one that
#    strictly must be public; 8080/8002 are internal and are exposed here
#    only for debugging.
#
# 2. Vast.ai maps plain HTTP, not HTTPS. Browsers refuse getUserMedia() on
#    an insecure origin -- EXCEPT for localhost. So a caller on a remote
#    machine cannot grant mic access over http://<IP>:8100 and the Start
#    Call button will fail with a permission error that looks like a mic
#    problem but is not one. Reach it through an SSH tunnel instead:
#
#        ssh -p <SSH_PORT> -L 8100:localhost:8100 root@<PUBLIC_IP>
#
#    then open http://localhost:8100 -- a secure origin, mic works.
# PORTS ARE CHOSEN TO MATCH WHAT THE STOCK TEMPLATE ALREADY MAPS.
# vastai/base-image:cuda-12.8.1-auto ships with exactly these published:
#     1111 (Instance Portal)  6006 (Tensorboard)  8080 (Jupyter + Jupyter
#     Terminal)  8384 (Syncthing)  10100  10200  72299
# A vast.ai instance's published ports are fixed AT CREATION and cannot be
# added later, and adding 8100/8101 meant saving a customised template -- which
# vast.ai creates as a PUBLIC template under the account. Reusing the spare
# already-mapped ports avoids publishing anything and needs no template at all.
#
#   voice agent PCM (main_pcm) -> 10100   (external; this is the caller's URL)
#   voice agent WebM (main.py) -> 10200   (external; legacy transport, kept
#                                          reachable for A/B and rollback)
#   clinic-api                 -> 8081    (internal only, never published)
#   TTS                        -> 8002    (internal only, never published)
#
# clinic-api and TTS are only ever reached over localhost by main.py, so they
# do not need to be published at all -- see CLINIC_API_BASE/TTS_URL above.
#
# WHY main_pcm IS ON THE CALLER-FACING PORT, NOT main.py
# ------------------------------------------------------
# These two values were swapped deliberately. main.py's transport is WebM via
# MediaRecorder, and MediaRecorder writes the container header ONLY into the
# first chunk of a session, so a still-growing buffer can only be read by
# re-decoding it from byte 0. main.py's _decode_to_wav() therefore spawns an
# ffmpeg process every POLL_INTERVAL_S (0.5s) and re-decodes the WHOLE call
# each time -- O(T) per poll, O(T^2) per call. agent/pcm_buffer.py's docstring
# does the arithmetic: a 90-second call decodes ~8,100 seconds of audio, a 90x
# amplification, plus ~2 process spawns per second PER CALL. That saturates CPU
# long before the GPU is busy and is the hard ceiling on concurrent callers.
#
# main_pcm.py sends raw pcm_s16le instead: no container, so no header to be
# missing, so nothing to re-decode. Appending is O(chunk) and reading the
# unprocessed tail is O(tail). No ffmpeg, no subprocess, no temp files.
#
# Both apps are self-contained -- main.py serves static/, main_pcm.py serves
# static/pcm/ (its own AudioWorklet client), and each client builds its socket
# URL from window.location.host -- so whichever app owns a port serves a
# matching client. Swapping the values below is all it takes to move callers
# between transports; nothing else in the stack needs to know.
#
# NOTE the variable names describe WHICH APP, not which role: start_all.sh
# binds main:app to VOICE_AGENT_PORT and main_pcm:app to VOICE_AGENT_PCM_PORT.
# So VOICE_AGENT_PCM_PORT=10100 is what puts the PCM app in front of callers.
# To roll back to the WebM transport, swap these two values back.
export VOICE_AGENT_PORT=10200
export VOICE_AGENT_PCM_PORT=10100
export CLINIC_API_PORT=8081
export TTS_PORT=8002

# Reaching it: vast.ai publishes plain HTTP on <PUBLIC_IP>:<mapped 10100>, and
# browsers refuse getUserMedia() on an insecure origin, so the mic will fail
# there. Use the Instance Portal / Jupyter Terminal for shell access, and for
# actually placing a call open the page over a tunnel so the origin is secure.
