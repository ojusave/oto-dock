"""PostgreSQL-backed application store (facade).

The implementations are grouped by domain into the ``storage.db_*`` modules and
re-exported here so callers keep importing from ``storage.database`` unchanged:

- :mod:`storage.db_tasks`    — task runs and dynamic tasks
- :mod:`storage.db_settings` — platform settings
- :mod:`storage.db_users`    — users and user-agent membership
- :mod:`storage.db_chats`    — chats, messages, media tokens, plans
- :mod:`storage.db_apps`     — pinned mini-apps registry
- :mod:`storage.db_file_pins` — Dock file pins
- :mod:`storage.db_usage`    — usage records and usage limits
- :mod:`storage.db_meetings` — meetings and meeting turns

All functions are synchronous (called via ``asyncio.to_thread`` from async code).
"""

from storage.db_tasks import *  # noqa: F401,F403
from storage.db_settings import *  # noqa: F401,F403
from storage.db_users import *  # noqa: F401,F403
from storage.db_chats import *  # noqa: F401,F403
from storage.db_apps import *  # noqa: F401,F403
from storage.db_file_pins import *  # noqa: F401,F403
from storage.db_usage import *  # noqa: F401,F403
from storage.db_meetings import *  # noqa: F401,F403

# Private symbol accessed by name elsewhere (``import *`` skips underscores).
from storage.db_tasks import _EDITABLE_TASK_COLUMNS  # noqa: F401
