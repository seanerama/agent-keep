"""Real cron validation (#10, stage 22) — the stage's testing requirements:

table tests over `keep_spec.cron.parse_cron` and the ScheduleTrigger field
validator: valid five-field expressions accepted; garbage (`never gonna give
you up` — which the old token-count pattern PASSED — and out-of-range values,
wrong field counts, @names, malformed steps/ranges) rejected; every in-repo
spec/fixture cron string still validates (rule-4 additive tightening).
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from keep_spec import ScheduleTrigger, load_spec
from keep_spec.cron import CronSyntaxError, parse_cron

REPO_ROOT = Path(__file__).parents[3]
FIXTURES = Path(__file__).parent / "fixtures"

# ------------------------------------------------------------------ accepted expressions

VALID_EXPRESSIONS = [
    "0 8 * * 1",  # client-tracking's weekly digest — Monday 08:00
    "*/15 * * * *",  # step on *
    "0 0 1,15 * 1-5",  # list + range across day fields
    "30 6 * * *",  # scheduled-reporter fixture
    "0 7 * * 1-5",  # full-featured fixture
    "59 23 31 12 7",  # every bound's maximum; dow 7 = Sunday
    "0 0 * * 0",  # dow 0 = Sunday too
    "0-59/5 8-17 * * 1-5",  # step on a range
    "* * * * *",  # every minute
]


@pytest.mark.parametrize("expr", VALID_EXPRESSIONS)
def test_valid_expressions_accepted(expr: str) -> None:
    parse_cron(expr)  # must not raise
    trigger = ScheduleTrigger(kind="schedule", cron=expr, prompt="report")
    assert trigger.cron == expr


def test_parse_expands_lists_ranges_and_steps() -> None:
    fields = parse_cron("*/15 8-10 1,15 * 1-5")
    assert fields.minutes == frozenset({0, 15, 30, 45})
    assert fields.hours == frozenset({8, 9, 10})
    assert fields.days_of_month == frozenset({1, 15})
    assert fields.months == frozenset(range(1, 13))
    assert fields.days_of_week == frozenset({1, 2, 3, 4, 5})
    assert fields.dom_restricted and fields.dow_restricted
    assert not parse_cron("0 8 * * 1").dom_restricted


def test_dow_seven_normalizes_to_sunday_zero() -> None:
    assert parse_cron("0 9 * * 7").days_of_week == frozenset({0})
    assert parse_cron("0 9 * * 7").days_of_week == parse_cron("0 9 * * 0").days_of_week
    assert parse_cron("0 9 * * 5-7").days_of_week == frozenset({0, 5, 6})


# ------------------------------------------------------------------ rejected expressions

INVALID_EXPRESSIONS = [
    "never gonna give you up",  # five tokens — the OLD pattern passed this (#10)
    "61 25 32 13 8",  # every field out of range
    "0 7 * *",  # four fields
    "0 7 * * * *",  # six fields
    "@weekly",  # @names are out — five fields only
    "60 * * * *",  # minute above 59
    "* 24 * * *",  # hour above 23
    "* * 0 * *",  # day-of-month below 1
    "* * * 13 *",  # month above 12
    "* * * * 8",  # day-of-week above 7
    "*/0 * * * *",  # zero step
    "*/x * * * *",  # non-numeric step
    "5-1 * * * *",  # backwards range
    "1,,2 * * * *",  # empty list item
    "5/2 * * * *",  # step on a bare number (needs '*' or a range)
    "1-5-9 * * * *",  # malformed range
    "",  # empty
]


@pytest.mark.parametrize("expr", INVALID_EXPRESSIONS)
def test_invalid_expressions_rejected(expr: str) -> None:
    with pytest.raises(CronSyntaxError):
        parse_cron(expr)
    with pytest.raises(ValidationError) as excinfo:
        ScheduleTrigger(kind="schedule", cron=expr, prompt="report")
    assert "cron" in str(excinfo.value)


def test_rickroll_rejection_names_the_problem() -> None:
    """THE #10 headline: five tokens of prose no longer validate."""
    with pytest.raises(ValidationError):
        ScheduleTrigger(kind="schedule", cron="never gonna give you up", prompt="report")


# ------------------------------------------------- every in-repo cron string still valid

IN_REPO_SCHEDULE_SPECS = [
    REPO_ROOT / "examples" / "client-tracking.yaml",
    FIXTURES / "scheduled-reporter.yaml",
    FIXTURES / "full-featured.yaml",
]


@pytest.mark.parametrize("path", IN_REPO_SCHEDULE_SPECS, ids=lambda p: p.name)
def test_in_repo_spec_cron_strings_still_validate(path: Path) -> None:
    """Rule-4 additive tightening: the tightened field admits every spec that
    was already in the repo (their cron strings were all genuinely valid)."""
    spec = load_spec(path)
    assert spec.spec.triggers is not None
    schedules = [a for a in spec.spec.triggers.activations if isinstance(a, ScheduleTrigger)]
    assert schedules, f"{path.name} was expected to declare a schedule activation"
    for activation in schedules:
        parse_cron(activation.cron)  # must not raise
