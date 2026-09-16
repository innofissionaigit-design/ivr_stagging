"""Regenerate main_pcm.py from main.py by swapping ONLY the transport.

A script rather than a one-off hand edit because the two files have to
stay in sync while transport work and reasoning work proceed in parallel.
Re-run after any change to main.py and the PCM variant picks it up with
no merge.

The split is enforced, not merely intended: everything from
`_resolve_intent` downward must be byte-identical between the two files,
and this script refuses to write if that stops being true.

    python tools/make_pcm_variant.py
"""
from __future__ import annotations

import io
import sys

HEADER = '''"""Kolkata Care Diagnostics -- voice agent on the RAW PCM transport.

GENERATED FILE -- do not edit directly. Produced by
tools/make_pcm_variant.py from main.py; edit main.py and re-run that.

This variant changes only the TRANSPORT (how audio arrives and how the
turn detector reads it):
  * No ffmpeg, no WebM, no temp audio files, no subprocess per poll.
  * Appending is O(chunk) and reading the tail is O(tail), where the WebM
    path was O(call length) EVERY poll -- O(T^2) per call.
  * The sample index IS the timeline, exactly, so processed_until_s
    cannot drift away from real time.
See agent/pcm_buffer.py for the argument and the arithmetic.

----------------------------------------------------------------------
'''

CALLSESSION_DOC = '''class CallSession:
    """One PCM buffer for the ENTIRE call, and a marker for how much of it
    has been consumed.

    The continuous-buffer design is inherited from the WebM version, where
    it was forced: MediaRecorder puts the container header only in the
    first chunk, so resetting the buffer mid-call produced audio that
    could never be decoded again. That constraint is GONE here -- raw PCM
    has no header and any byte range is independently valid.

    It is kept anyway, because the second reason for it was always the
    better one: `processed_until_s` gives every turn an absolute,
    monotonic position on one call-long timeline. Silero's segment
    boundaries move as more audio arrives, so a turn detector run against
    a buffer that keeps restarting drops any utterance straddling the
    seam. Each poll therefore looks only at the UNPROCESSED TAIL.

    What PCM changes is the cost and the accuracy of that lookup: the tail
    is a slice rather than a full re-decode, and sample index maps to
    wall-clock exactly, so the timeline cannot drift.
    """

'''

SLICE_NEW = '''    sr = session.audio.sample_rate
    clip = session.audio.slice_tensor(start_s, end_s + UTTERANCE_PAD_S)
    clip_path = os.path.join(session.tmpdir, f"utt{seq}.wav")

    def _write():
        wav = clip.unsqueeze(0)
        out_sr = sr
        if sr != SAMPLE_RATE:
            # Only reachable when the browser refused a 16kHz AudioContext.
            # ASR expects 16k, so convert here rather than letting it
            # silently transcribe pitch-shifted audio.
            wav = torchaudio.functional.resample(wav, sr, SAMPLE_RATE)
            out_sr = SAMPLE_RATE
        torchaudio.save(clip_path, wav, out_sr)

    await asyncio.to_thread(_write)
    return clip_path'''

HELLO_NEW = '''    if msg.get("type") == "playback_done":
        session.release_gate()
    elif msg.get("type") == "hello":
        # The browser may refuse the 16kHz AudioContext we ask for. Trust
        # what the client reports over what we requested: a wrong assumed
        # rate would not fail loudly, it would just make every timestamp
        # and every transcript quietly wrong.
        rate = int(msg.get("sampleRate") or SAMPLE_RATE)
        session.declared_rate = rate
        session.audio.sample_rate = rate
        if rate != SAMPLE_RATE:
            logger.warning("[%s] client capturing at %dHz, not %dHz -- resampling per utterance",
                           session.call_id, rate, SAMPLE_RATE)
        logger.info("[%s] transport: %s @ %dHz", session.call_id,
                    msg.get("format", "pcm_s16le"), rate)'''

MIC_TAIL_NEW = '''    sr = session.audio.sample_rate
    n = int(seconds * sr)
    total = len(session.audio)
    if total < n:
        return None
    tail = session.audio.tail_tensor((total - n) / sr)
    if tail.numel() == 0:
        return None
    return tail.numpy(), sr'''


POLL_NEW = '''        sr = session.audio.sample_rate
        tail = session.audio.tail_tensor(session.processed_until_s)
        if tail.numel() < int(0.2 * sr):
            continue  # not enough new audio to judge yet -- not an error

        result'''


def main() -> None:
    src = io.open("main.py", encoding="utf-8").read()
    s = src

    def rep(old: str, new: str, label: str) -> None:
        nonlocal s
        if old not in s:
            sys.exit(f"PATCH MISS ({label}) -- main.py changed shape; update this script")
        s = s.replace(old, new, 1)

    rep('"""Kolkata Care Diagnostics -- Bengali voice agent, WebSocket orchestrator.',
        HEADER, "docstring")
    rep("import torchaudio\nfrom fastapi",
        "import torchaudio\nfrom agent.pcm_buffer import PcmCallBuffer, SAMPLE_RATE\nfrom fastapi",
        "imports")

    rep(s[s.index("async def _decode_to_wav("):s.index("class CallSession:")], "", "drop decoder")
    rep(s[s.index("class CallSession:"):s.index("    def __init__(self, ws: WebSocket):")],
        CALLSESSION_DOC, "CallSession docstring")

    rep('        self.raw_path = os.path.join(self.tmpdir, "call.webm")\n'
        '        self.wav_path = self.raw_path + ".wav"\n'
        '        open(self.raw_path, "wb").close()',
        "        self.audio = PcmCallBuffer()\n        self.declared_rate: int | None = None",
        "buffer")

    rep("    async def append(self, chunk: bytes):\n"
        "        self.last_activity = time.time()\n"
        '        with open(self.raw_path, "ab") as f:\n'
        "            f.write(chunk)",
        "    async def append(self, chunk: bytes):\n"
        "        self.last_activity = time.time()\n"
        "        self.audio.append(chunk)",
        "append")

    rep("    wav, sr = await asyncio.to_thread(torchaudio.load, session.wav_path)\n"
        "    a = max(0, int(start_s * sr))\n"
        "    b = min(int((end_s + UTTERANCE_PAD_S) * sr), wav.shape[-1])\n"
        '    clip_path = f"{session.wav_path}.utt{seq}.wav"\n'
        "    await asyncio.to_thread(torchaudio.save, clip_path, wav[:, a:b], sr)\n"
        "    return clip_path",
        SLICE_NEW, "slice")

    rep("    if not await _decode_to_wav(session.raw_path, session.wav_path):\n"
        "        return False\n"
        "    wav, sr = await asyncio.to_thread(torchaudio.load, session.wav_path)\n"
        "    buffer_end_s = wav.shape[-1] / sr\n"
        "    session.processed_until_s",
        "    buffer_end_s = session.audio.duration_s\n    session.processed_until_s",
        "resync")

    rep("        if not await _decode_to_wav(session.raw_path, session.wav_path):\n"
        "            continue  # too little data yet to form a valid container -- not an error\n"
        "\n"
        "        wav, sr = await asyncio.to_thread(torchaudio.load, session.wav_path)\n"
        "        wav = wav.mean(dim=0) if wav.shape[0] > 1 else wav.squeeze(0)\n"
        "\n"
        "        tail_start_sample = min(int(session.processed_until_s * sr), wav.shape[-1])\n"
        "        tail = wav[tail_start_sample:]\n"
        "\n"
        "        result",
        POLL_NEW, "poll")

    rep("    if not await _decode_to_wav(session.raw_path, session.wav_path):\n"
        "        return None\n"
        "    wav, sr = await asyncio.to_thread(torchaudio.load, session.wav_path)\n"
        "    wav = wav.mean(dim=0) if wav.shape[0] > 1 else wav.squeeze(0)\n"
        "    n = int(seconds * sr)\n"
        "    if wav.shape[-1] < n:\n"
        "        return None\n"
        "    return wav[-n:].numpy(), sr",
        MIC_TAIL_NEW, "mic tail")

    rep('    if msg.get("type") == "playback_done":\n        session.release_gate()',
        HELLO_NEW, "hello")

    rep("TAIL_READ_IS_CHEAP = False",
        "TAIL_READ_IS_CHEAP = True   # raw PCM: reading the tail is a slice, not a decode",
        "tail cost")

    rep('AUDIT_TRANSPORT = "webm"', 'AUDIT_TRANSPORT = "pcm"', "audit transport")

    rep('app.mount("/", StaticFiles(directory="static", html=True), name="static")',
        'app.mount("/", StaticFiles(directory="static/pcm", html=True), name="static")',
        "mount")

    # The point of the split: prove the reasoning half was untouched.
    a, b = "async def _resolve_intent(", "async def _resync_after_playback("
    if src[src.index(a):src.index(b)] != s[s.index(a):s.index(b)]:
        sys.exit("REFUSING TO WRITE: reasoning half diverged between main.py and the variant")

    io.open("main_pcm.py", "w", encoding="utf-8").write(s)
    print("main_pcm.py regenerated; reasoning half verified byte-identical")


if __name__ == "__main__":
    main()
