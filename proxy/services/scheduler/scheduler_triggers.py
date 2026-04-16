"""Shared APScheduler trigger construction for scheduled tasks + notifications.

The cron and interval-anchor trigger logic was duplicated across
``services.scheduler.scheduler`` (``_register_task`` + ``_compute_next_run_times``) and
``services.notifications.notification_manager`` (``_register_notification``). It lives here
once so the embedded scheduler and the future standalone scheduler compute
identical fire times for the same row.

**Deliberately ``core.*``-free.** The standalone scheduler process
(``scheduler/standalone_scheduler.py``, T3-cloud-only) runs outside ``proxy/``
and avoids ``core`` imports; it imports ``build_cron_trigger`` from here (the
day-of-week remap must match everywhere) and keeps its own copy of the
interval-anchor logic pending its catch-up pass.

Callers own the divergent parts (run_at / ``delay_seconds`` / past-skip
clocks) and their own ``try``/``except`` wrappers — these builders may raise on
a malformed ``schedule`` / ``interval_seconds`` and the caller decides whether
to log-and-skip or swallow.
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Standard cron day-of-week numbering (0 or 7 = Sunday). APScheduler's numeric
# day_of_week is 0=Monday — even in ``CronTrigger.from_crontab`` — so numeric
# weekdays passed through verbatim fire one day late. The platform's contract
# is STANDARD cron everywhere (stored rows, API, dashboard humanizer); the
# builders below remap to APScheduler's convention at trigger construction.
# Names (mon..sun) mean the same day in both and need no remapping.
_STD_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _std_day(atom: str) -> int | None:
    """Raw standard-cron day for one dow atom: 0-7 numeric (7 = Sunday, kept
    as 7 so range endpoints stay ordered) or a three-letter name. None when
    it is neither."""
    a = atom.strip().lower()
    if a.isdigit():
        n = int(a)
        return n if 0 <= n <= 7 else None
    return _STD_DOW_NAMES.get(a)


def standard_dow_to_apscheduler(field: str) -> str:
    """Rewrite a standard-cron day-of-week field into APScheduler numbering.

    Handles the crontab forms ``*``, ``n``, ``a-b``, ``*/k``, ``a-b/k`` and
    comma lists thereof, names accepted anywhere a number is. Ranges that
    cross APScheduler's Monday week start expand into day lists (APScheduler
    rejects wrapped ranges like ``6-1``, and name ranges such as ``sun-tue``
    hit the same wall — hence names are normalized through numbers too).
    Unrecognized tokens pass through untouched so a malformed field still
    fails inside CronTrigger with its own error message.
    """
    out: list[str] = []
    for token in field.split(","):
        token = token.strip()
        expr, sep, step_s = token.partition("/")
        step = int(step_s) if step_s.isdigit() and int(step_s) > 0 else None
        if sep and step is None:
            out.append(token)
            continue
        if expr == "*":
            if step is None:
                out.append(token)
                continue
            raw: list[int] = list(range(0, 7, step))
        elif "-" in expr:
            lo_s, _, hi_s = expr.partition("-")
            lo, hi = _std_day(lo_s), _std_day(hi_s)
            if lo is None or hi is None or lo > hi:
                out.append(token)
                continue
            raw = list(range(lo, hi + 1, step or 1))
        elif sep:
            out.append(token)
            continue
        else:
            d = _std_day(expr)
            if d is None:
                out.append(token)
                continue
            raw = [d]
        days = sorted({(r % 7 + 6) % 7 for r in raw})
        if len(days) > 2 and (step or 1) == 1 and days == list(range(days[0], days[-1] + 1)):
            out.append(f"{days[0]}-{days[-1]}")
        else:
            out.append(",".join(str(d) for d in days))
    return ",".join(out)


def apscheduler_dow_to_standard(field: str) -> str:
    """Rewrite a day-of-week field from APScheduler numbering (0 = Monday)
    back to standard cron (0 or 7 = Sunday), preserving the fired days.

    The one-shot startup migration uses this on pre-existing rows: their
    numeric weekdays were INTERPRETED as APScheduler days, so rewriting them
    one day forward keeps every schedule firing exactly when it always did
    (the failure mode to avoid is silently shifting fire days). Numbers map
    0-6 → 1-7 so ranges never wrap; a lone Sunday emits the conventional 0;
    ``*/k`` becomes ``1-7/k`` (standard anchors steps at Sunday, APScheduler
    at Monday). Names and unrecognized tokens pass through untouched.
    """
    out: list[str] = []
    for token in field.split(","):
        token = token.strip()
        expr, sep, step_s = token.partition("/")
        suffix = f"{sep}{step_s}" if sep else ""
        if expr == "*":
            out.append(f"1-7{suffix}" if step_s.isdigit() and int(step_s) > 1 else token)
        elif "-" in expr:
            lo_s, _, hi_s = expr.partition("-")
            if lo_s.isdigit() and hi_s.isdigit() and 0 <= int(lo_s) <= int(hi_s) <= 6:
                out.append(f"{int(lo_s) + 1}-{int(hi_s) + 1}{suffix}")
            else:
                out.append(token)
        elif expr.isdigit() and 0 <= int(expr) <= 6:
            std = int(expr) + 1
            out.append(f"{0 if std == 7 else std}{suffix}")
        else:
            out.append(token)
    return ",".join(out)


def build_cron_trigger(schedule: str, tz):
    """CronTrigger from a 5-field STANDARD crontab string, interpreted in
    ``tz``. The day-of-week field is remapped to APScheduler numbering (see
    ``standard_dow_to_apscheduler``); everything that parses or validates a
    platform cron must come through here, never ``from_crontab`` directly."""
    fields = schedule.split()
    if len(fields) == 5:
        fields[4] = standard_dow_to_apscheduler(fields[4])
        schedule = " ".join(fields)
    return CronTrigger.from_crontab(schedule, timezone=tz)


def build_interval_trigger(interval_seconds: int, created_at: str | None, tz):
    """IntervalTrigger that fires every ``interval_seconds``, anchored so the
    first fire is exactly one interval after creation.

    ``start_date`` = ``created_at + interval_seconds`` (``created_at`` is an ISO
    string, or ``None`` → anchored at now). A naive ``created_at`` is read as
    UTC. Anchoring at creation (not registration) keeps the cadence
    deterministic across proxy restarts and edits.
    """
    if created_at:
        base = datetime.fromisoformat(created_at)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    start_date = base + timedelta(seconds=interval_seconds)
    return IntervalTrigger(
        seconds=interval_seconds,
        start_date=start_date,
        timezone=tz,
    )
