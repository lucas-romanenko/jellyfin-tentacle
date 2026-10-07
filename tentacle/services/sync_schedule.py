"""The nightly sync's 5-field cron string as an APScheduler trigger (#458).

APScheduler 3 reads day_of_week as 0 = Monday ... 6 = Sunday and refuses 7;
cron reads 0 and 7 as Sunday and 1 as Monday. Passed through as it was,
"0 4 * * 1-5" ran Tuesday to Saturday, "0 3 * * 0" ran on Mondays and
"0 3 * * 7" scheduled nothing. The day-of-week field is turned into day names
here, which both read the same. APScheduler also needs both day fields to
match, where cron runs on either one when both are set: that becomes an
OrTrigger.

A value that is no usable schedule raises ValueError, so the caller decides:
startup runs at the default time with a warning, the settings form refuses it.
"""
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _day(value: str, range_end: bool = False) -> int:
    """A cron day of week (0-7 or a name) as 0 = Sunday ... 7 = Sunday.
    A name range that ends in "sun" ("sat-sun", "mon-sun") runs through
    Sunday, as APScheduler (where sun is the last day) already read it."""
    v = value.lower()
    if v in _NAMES:
        n = _NAMES.index(v)
        return 7 if range_end and n == 0 else n
    if v.isdigit() and int(v) <= 7:
        return int(v)
    raise ValueError(f"'{value}' is no day of the week")


def _days_of_week(field: str) -> str:
    """Cron's day-of-week field as APScheduler day names ("1-5" -> "mon,...,fri")."""
    days = set()
    for item in field.split(","):
        rng, _, step = item.partition("/")
        if step and not step.isdigit() or step == "0":
            raise ValueError(f"'{item}' has no valid step")
        if rng == "*":
            first, last = 0, 7
        elif "-" in rng:
            a, _, b = rng.partition("-")
            first, last = _day(a), _day(b, range_end=True)
            if first > last:
                raise ValueError(f"'{item}' runs backwards")
        else:
            first = _day(rng)
            last = 7 if step else first
        days.update(d % 7 for d in range(first, last + 1, int(step or 1)))
    # Monday first, as APScheduler counts
    return ",".join(_NAMES[d] for d in sorted(days, key=lambda d: (d - 1) % 7))


def sync_trigger(cron: str):
    """The trigger for a 5-field cron string; ValueError when it is no schedule."""
    parts = (cron or "").strip().split()
    if len(parts) != 5:
        raise ValueError("expected 5 cron fields")
    minute, hour, day, month, dow = parts
    try:
        if dow == "*":
            # The default and every "M H * * *" the time picker writes
            return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week="*")
        names = _days_of_week(dow)
        if day.startswith("*") or dow.startswith("*"):
            return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=names)
        # Both day fields set: cron runs on either one
        return OrTrigger([
            CronTrigger(minute=minute, hour=hour, day=day, month=month),
            CronTrigger(minute=minute, hour=hour, month=month, day_of_week=names),
        ])
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(str(e)) from e
