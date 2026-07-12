"""Transcript-logging policy for the audio package.

Verbatim caller/user transcripts are personal data. Logging them at INFO in
production is a GDPR landmine — and doubly so now that the same STT/turn
providers also run on the chat-audio path.

**Rule #1 for provider authors:** never write `logger.info(<transcript>)`.
Route every transcript through `log_transcript()`. The bare-log lint test
(`audio/providers/tests/test_no_bare_transcript_logs.py`) enforces this.

`OTO_AUDIO_LOG_TRANSCRIPTS=1` opts INTO INFO-level transcript logging for local
debugging. Default (unset / "0"): transcripts log at DEBUG only — never INFO.
"""

import logging
import os

LOG_TRANSCRIPTS: bool = os.environ.get("OTO_AUDIO_LOG_TRANSCRIPTS", "0") == "1"


def log_transcript(logger: logging.Logger, label: str, text: str) -> None:
    """Log a transcript at INFO only when explicitly opted in, else DEBUG.

    Empty / falsy text is dropped. Call this instead of a bare logger.info.

    Args:
        logger: the caller's module logger.
        label:  short context label (e.g. "Deepgram final", "Groq").
        text:   the transcript (may be None/empty — silently ignored).
    """
    if not text:
        return
    logger.log(logging.INFO if LOG_TRANSCRIPTS else logging.DEBUG, "%s: %s", label, text)
