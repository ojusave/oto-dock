"""oto-audio — provider-agnostic STT / TTS / VAD / turn-classifier package.

Imported by BOTH the OtoDock proxy (chat audio surface: ``/v1/audio/*``) and the
phone server (telephony call pipeline). The package owns no telephony and no
HTTP surface — it is pure provider code.

Provider classes are PURE: they receive credentials and audio-format parameters
as constructor kwargs and never read the DB, environment, or files. The caller
(proxy or phone) resolves credentials and instantiates. See
``CONTRIBUTING_PROVIDERS.md`` to add a new provider.
"""

__version__ = "0.1.0a0"
