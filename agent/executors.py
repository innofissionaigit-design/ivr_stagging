"""Separate thread pools, so slow network I/O cannot starve fast audio work.

THE FAILURE THIS PREVENTS
-------------------------
`asyncio.to_thread` does not create a thread. It submits to the event loop's
DEFAULT executor -- a single process-wide ThreadPoolExecutor sized
`min(32, os.cpu_count() + 4)`. Every to_thread call in this codebase shares
that one pool, and the things sharing it have wildly different durations:

    torchaudio.load / save          ~ms      per poll, per call
    TurnDetector.poll (Silero VAD)  ~10ms    per poll, per call
    FastPath.resolve                ~ms      per turn
    TurnASR.transcribe_utterance    ~1-2s    per turn   (GPU)
    SemanticCache.get / put         up to 45s per turn  (BLOCKING HTTP: embed)
    llm.extract_intent              up to 25s per turn  (BLOCKING HTTP: Ollama)

The last two are the problem. They are `urllib` calls -- fully synchronous,
holding their worker thread for the entire round trip, including the time
the request spends waiting in Ollama's internal queue where this process
cannot see it (OLLAMA_NUM_PARALLEL=2, so callers 3+ are queued by design).

Share one pool between those and the audio path and the pool becomes the
coupling. Enough concurrent callers with an LLM request in flight and every
worker is parked on a socket read. Then:

  * no thread is free to run TurnDetector.poll, so no call detects that its
    caller stopped talking;
  * no thread is free to run torchaudio.load, so the poll loop cannot even
    read its buffer;
  * no thread is free to run TurnASR, so turns that DID get detected sit
    undispatched.

Every call stalls, including the ones whose own LLM request already
returned, and including brand-new calls that have done nothing yet. One
slow dependency becomes a whole-system outage, and it presents as "the
agent stopped responding" rather than as anything pointing at Ollama.

That is the exact shape the peak-hour user story rules out: quality
becoming a function of how many other people are mid-turn.

THE FIX
-------
Blocking HTTP gets its own pool. The default pool is then reserved for the
audio and GPU path, whose work is short and predictable, so a saturated
Ollama can no longer prevent a caller's turn from being DETECTED -- it can
only make that turn's answer slower, which is a bounded, local, honest
degradation.

Note this pool intentionally does NOT need to be large. Its size is a
backpressure knob, not a throughput one: Ollama serves OLLAMA_NUM_PARALLEL
at a time and queues OLLAMA_MAX_QUEUE behind that, so threads beyond that
ceiling buy nothing but memory. Requests that cannot be served in time are
meant to hit llm.py's OLLAMA_TURN_BUDGET_S and fall back to the apology
line, which is a better outcome for a live caller than an unbounded wait.
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import os

# Tunable without a redeploy so it can be sized against real peak traffic.
HTTP_POOL_SIZE = int(os.environ.get("VOICE_AGENT_HTTP_POOL_SIZE", "24"))

_http_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=HTTP_POOL_SIZE,
    thread_name_prefix="blocking-http",
)


async def run_http(fn, *args):
    """Run a BLOCKING network call off the audio path.

    Use this instead of asyncio.to_thread for anything that talks to Ollama
    (llm.extract_intent, semantic_cache.get/put and the embed() inside
    them). Keep asyncio.to_thread for CPU/GPU work -- ASR, VAD, torchaudio,
    fast_path -- so those keep the default pool to themselves.

    Positional args only: loop.run_in_executor takes no kwargs, and every
    call site here passes positionally already.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_http_pool, fn, *args)


# ---------------------------------------------------------------------------
# ASR admission control
# ---------------------------------------------------------------------------
# TurnASR._transcribe_clip already takes a threading.Lock, so GPU inference is
# serialized no matter what. This semaphore is NOT a second copy of that -- it
# controls something different: WHERE callers wait for it.
#
# Without it, every concurrent turn calls asyncio.to_thread(...) and each one
# occupies a DEFAULT-POOL THREAD that does nothing but block on that lock. The
# default pool is ~32 threads and is shared with the audio path, so ~32
# simultaneous turns park every worker on the ASR lock and VAD polls and
# torchaudio stop running for every call -- the same starvation this module
# exists to prevent, just reached through the GPU instead of through Ollama.
#
# Acquiring here means the waiting happens on the EVENT LOOP, which costs a
# coroutine rather than a thread, and only the callers that can actually make
# progress ever enter the pool.
#
# 2, not 1: one turn holds the GPU while the next is already staged in a
# worker, so the device is not idle during the handoff between turns. Higher
# values buy nothing -- the lock serializes them anyway -- and just put threads
# back to sleeping in the pool, which is the thing being avoided.
ASR_CONCURRENCY = int(os.environ.get("VOICE_AGENT_ASR_CONCURRENCY", "2"))

# Safe at module scope on Python 3.10+: asyncio primitives no longer bind to a
# loop at construction, they attach to the running loop on first use.
asr_gate = asyncio.Semaphore(ASR_CONCURRENCY)


def shutdown(wait: bool = False):
    """Called from each app's shutdown hook. wait=False by default: a
    worker parked on a 20s socket read should not hold up process exit."""
    _http_pool.shutdown(wait=wait)


# Backstop for any exit path that does not run the app's shutdown hook.
atexit.register(lambda: _http_pool.shutdown(wait=False))
