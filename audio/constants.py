"""Audio-format constants shared by every provider.

Asterisk AudioSocket telephony is 8 kHz / 16-bit signed little-endian / mono;
`FRAME_SIZE` is one AudioSocket audio frame (20 ms). These are protocol
constants that never change.

Providers take audio-format parameters from here (or via constructor args).
They must NEVER `import config` from the phone server — that module does
`load_dotenv` and reads `PROXY_*` env vars at import time, so an installable,
process-agnostic `audio/` package cannot depend on it.
"""

SAMPLE_RATE = 8000   # Hz — AudioSocket telephony sample rate
SAMPLE_WIDTH = 2     # bytes per sample — 16-bit signed little-endian
CHANNELS = 1         # mono
FRAME_SIZE = 320     # bytes per AudioSocket frame (20 ms @ 8 kHz / 16-bit)
