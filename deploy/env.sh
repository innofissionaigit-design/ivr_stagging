# Environment for every service in the stack. Source before launching.
#
# Everything referenced here MUST live under /workspace. RunPod wipes the
# container's overlay filesystem ("/" and "/root") on every restart and
# keeps only the network volume, so anything installed to /usr/local/bin
# or stored in /var/lib is gone the next time the pod boots. That is not a
# hypothetical: the ollama binary and the entire Postgres installation
# have each been lost to it three times.

export HF_HOME=/workspace/.cache/huggingface
export TORCH_HOME=/workspace/.cache/torch
export HF_HUB_ENABLE_HF_TRANSFER=0

export OLLAMA_MODELS=/workspace/.ollama/models
# -1 keeps models resident indefinitely. Without it Ollama unloads after
# five idle minutes and the next caller pays a 47s cold start mid-call.
export OLLAMA_KEEP_ALIVE=-1

export PYTHONPATH=/workspace/AI4Bharat_NeMo:/workspace/kolkata-care-voice-agent:

# DATABASE_URL is deliberately NOT set: clinic-api/db.py then defaults to
# sqlite:////workspace/clinic.db, which persists across restarts. Postgres
# physically cannot run on this volume -- see that file's docstring. Set
# this only when pointing at a real external Postgres.
export CLINIC_API_BASE=http://localhost:8080
export TTS_URL=http://localhost:8002/synthesize
export SILERO_VAD_REPO=/workspace/silero-vad

# ADDED BY SOURAV -- "otp will not be hardcoded". OTP_MESSAGING_WEBHOOK_URL
# is deliberately NOT set here: clinic-api/otp_messaging_config.py then
# defaults it to "" and runs with no real provider connected (every OTP
# is still generated for real -- see models.generate_otp_code() -- it
# just isn't pushed anywhere; read it from the database instead). Set
# this to your own SMS/WhatsApp/e-mail provider's endpoint to actually
# deliver OTPs -- see that file's own module docstring for exactly what
# gets POSTed to it. This is the ONLY environment variable a deploying
# company needs to add to connect their own provider; nothing else in
# this stack needs to change.
# export OTP_MESSAGING_WEBHOOK_URL=https://your-provider.example.com/send-otp

# ADDED BY SOURAV -- Phase 1: Database Schema & Policy Tables. Both
# deliberately NOT set here, same "safe default, opt in" pattern as
# OTP_MESSAGING_WEBHOOK_URL just above -- unlike that one, though, setting
# either of these alone does NOT connect anything yet: Phase 1's walk-in/
# prescription/insurance/billing stories read from clinic-api's own local
# database only (see clinic-api/company_config.py's own module docstring).
# These exist so a real insurer or billing system's URL has one obvious
# place to go WHEN clinic-api/main.py is updated to actually call it.
# export INSURANCE_PROVIDER_API_URL=https://your-insurer.example.com/eligibility
# export BILLING_SYSTEM_API_URL=https://your-billing-system.example.com/api

# ADDED BY SOURAV -- "Caller asks to be called back" story. Deliberately
# NOT set here: agent/callback_config.py then defaults CALLBACKS_ENABLED
# to true, i.e. the feature is ON unless a deploying clinic explicitly
# turns it off. Uncomment to disable callbacks entirely for this
# deployment (agent/callback_flow.py's check_callback_availability() then
# always reports "disabled", regardless of the clinic's operating hours).
# export CALLBACKS_ENABLED=false

# /workspace/bin first: that is where the persistent ollama binary lives.
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
# THE SECRET IS NOT IN THIS FILE, AND MUST NOT BE ADDED TO IT.
# HOSPITAL_GATEWAY_API_KEY and HOSPITAL_GATEWAY_DLR_TOKEN are the only two
# credentials this repository has ever needed, and this file is committed,
# read by every service, and routinely pasted into bug reports. Export them
# from the shell, a systemd drop-in, or an uncommitted deploy/env.secret.sh
# sourced after this one.
#
# LEAVING HOSPITAL_GATEWAY_URL UNSET IS A SUPPORTED STATE, not a broken one.
# clinic-api then records every message as `skipped` with the reason
# attached, and reply_templates.py stops promising callers an SMS -- see
# notify_service.queue_message() and _written_confirmation_clause(). A bench
# pod behaves correctly and messages nobody.
export HOSPITAL_GATEWAY_URL=${HOSPITAL_GATEWAY_URL:-}
export HOSPITAL_GATEWAY_AUTH_HEADER=${HOSPITAL_GATEWAY_AUTH_HEADER:-Authorization}

# DLT registration, from the hospital's account on the operator portal.
# SENDER_ID is the registered 6-character header; ENTITY_ID is the Principal
# Entity ID. Both are submitted with every message and both are matched by
# the operator -- a mismatch is a silent non-delivery, not an error we see.
export HOSPITAL_GATEWAY_SENDER_ID=${HOSPITAL_GATEWAY_SENDER_ID:-}
export HOSPITAL_GATEWAY_ENTITY_ID=${HOSPITAL_GATEWAY_ENTITY_ID:-}

# Registered content template IDs, one per event. clinic-api refuses to send
# a template whose ID is empty rather than paying for an operator rejection
# -- see notifications.preflight(). The bodies these IDs correspond to are
# in clinic-api/message_templates.py and must match the portal exactly.
export HOSPITAL_GATEWAY_TEMPLATE_BOOKED=${HOSPITAL_GATEWAY_TEMPLATE_BOOKED:-}
export HOSPITAL_GATEWAY_TEMPLATE_RESCHEDULED=${HOSPITAL_GATEWAY_TEMPLATE_RESCHEDULED:-}
export HOSPITAL_GATEWAY_TEMPLATE_CANCELLED=${HOSPITAL_GATEWAY_TEMPLATE_CANCELLED:-}

# agent/slot_parse.parse_phone() stores the last 10 digits; the gateway wants
# E.164. This is the prefix put back on.
export HOSPITAL_GATEWAY_COUNTRY_CODE=${HOSPITAL_GATEWAY_COUNTRY_CODE:-91}

# 8s, comfortably longer than the 4s agent/tools_client.py allows this
# service -- and safe precisely BECAUSE the send is a background task that
# runs after the response has already gone back to the caller.
export HOSPITAL_GATEWAY_TIMEOUT_S=${HOSPITAL_GATEWAY_TIMEOUT_S:-8}

# How long a `sent` message may go without a delivery receipt before the
# staff queue calls it a failure. See notifications.DEFAULT_STALE_MINUTES.
export HOSPITAL_GATEWAY_STALE_MINUTES=${HOSPITAL_GATEWAY_STALE_MINUTES:-15}
