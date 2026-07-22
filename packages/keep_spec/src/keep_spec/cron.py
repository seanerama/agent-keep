"""Real five-field cron parsing for `ScheduleTrigger.cron` (#10, stage 22).

Exactly five whitespace-separated fields (minute hour day-of-month month
day-of-week) — no `@names`, no seconds field. Per field: `*`, single numbers,
ranges (`N-M`), comma lists of those, and `/step` on `*` or a range. Values
are range-checked (minute 0-59, hour 0-23, day-of-month 1-31, month 1-12,
day-of-week 0-7 with both 0 and 7 meaning Sunday — Vixie convention;
parsed day-of-week values are normalized to 0-6).

Timezone posture: expressions are evaluated in UTC by the runtime (the
schedule-trigger component computes fire boundaries on a UTC clock); the
schema's field description documents the same.

This module is the single source of truth for what a cron expression MEANS:
the schema validator (`models.ScheduleTrigger`) and the runtime's next-fire
computation (`agent_runtime.components.schedule_trigger`) both parse through
here, so "validates" and "runnable" cannot drift apart. `keep_spec` ships
whole into every composed image (see the composer), so the runtime import is
always satisfiable.
"""

from dataclasses import dataclass

__all__ = ["CronFields", "CronSyntaxError", "parse_cron"]


class CronSyntaxError(ValueError):
    """The expression is not a valid five-field cron expression."""


#: (name, low, high) per field, in field order. day-of-week admits 0-7 in
#: source text (0 and 7 both mean Sunday); parsed values are normalized 0-6.
_FIELD_BOUNDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


@dataclass(frozen=True)
class CronFields:
    """A parsed cron expression: the admitted values per field.

    `dom_restricted` / `dow_restricted` record whether the day-of-month /
    day-of-week SOURCE field was anything other than a bare ``*`` — the
    classic (Vixie) day-matching rule needs it: when BOTH day fields are
    restricted, a day matches if EITHER matches; otherwise the restricted
    one (if any) governs alone.
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # normalized 0-6, 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool


def _parse_number(name: str, token: str, low: int, high: int) -> int:
    if not token.isdigit():
        raise CronSyntaxError(f"{name} value {token!r} is not a number")
    value = int(token)
    if not low <= value <= high:
        raise CronSyntaxError(f"{name} value {value} is outside {low}-{high}")
    return value


def _parse_field(name: str, field: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for item in field.split(","):
        if not item:
            raise CronSyntaxError(f"{name} field {field!r} has an empty list item")
        span, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronSyntaxError(
                    f"{name} step {step_text!r} in {item!r} must be a positive number"
                )
            step = int(step_text)
        if span == "*":
            start, stop = low, high
        elif "-" in span:
            lo_text, _, hi_text = span.partition("-")
            start = _parse_number(name, lo_text, low, high)
            stop = _parse_number(name, hi_text, low, high)
            if stop < start:
                raise CronSyntaxError(f"{name} range {span!r} runs backwards")
        else:
            if slash:
                raise CronSyntaxError(
                    f"{name} step in {item!r} requires '*' or a range before the '/'"
                )
            start = stop = _parse_number(name, span, low, high)
        values.update(range(start, stop + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> CronFields:
    """Parse a five-field cron expression; raise `CronSyntaxError` on nonsense."""
    fields = expr.split()
    if len(fields) != len(_FIELD_BOUNDS):
        detail = " — @names are not supported" if expr.strip().startswith("@") else ""
        raise CronSyntaxError(
            f"expected 5 fields (minute hour day-of-month month day-of-week), "
            f"got {len(fields)}{detail}"
        )
    minutes, hours, dom, months, dow_raw = (
        _parse_field(name, field, low, high)
        for field, (name, low, high) in zip(fields, _FIELD_BOUNDS, strict=True)
    )
    return CronFields(
        minutes=minutes,
        hours=hours,
        days_of_month=dom,
        months=months,
        days_of_week=frozenset(value % 7 for value in dow_raw),  # 7 == Sunday == 0
        dom_restricted=fields[2] != "*",
        dow_restricted=fields[4] != "*",
    )
