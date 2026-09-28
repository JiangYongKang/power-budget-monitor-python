"""字段/单位/量纲/数值范围校验：拒绝原因必须可区分。"""
from __future__ import annotations

import math
from datetime import UTC

import pytest

from power_budget.models import Metric, RejectReason, Unit
from power_budget.validation import (
    ValidationError,
    parse_reading,
    parse_timestamp,
    require_str,
)


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("not-a-dict", RejectReason.BAD_TYPE),
        ({"unit": "W", "value": 1}, RejectReason.MISSING_FIELD),
        ({"metric": "power", "value": 1}, RejectReason.MISSING_FIELD),
        ({"metric": "power", "unit": "W"}, RejectReason.MISSING_FIELD),
        ({"metric": 42, "unit": "W", "value": 1}, RejectReason.UNKNOWN_METRIC),
        ({"metric": "power", "unit": 42, "value": 1}, RejectReason.UNKNOWN_UNIT),
        ({"metric": "power", "unit": "Pa", "value": 1}, RejectReason.UNKNOWN_UNIT),
        ({"metric": "power", "unit": "V", "value": 1}, RejectReason.UNIT_METRIC_MISMATCH),
        ({"metric": "current", "unit": "kW", "value": 1}, RejectReason.UNIT_METRIC_MISMATCH),
        ({"metric": "power", "unit": "W", "value": "x"}, RejectReason.BAD_TYPE),
        ({"metric": "power", "unit": "W", "value": True}, RejectReason.BAD_TYPE),
        ({"metric": "power", "unit": "W", "value": math.nan}, RejectReason.NON_FINITE),
        ({"metric": "power", "unit": "W", "value": math.inf}, RejectReason.NON_FINITE),
        ({"metric": "power", "unit": "W", "value": -0.001}, RejectReason.VALUE_OUT_OF_RANGE),
        ({"metric": "power", "unit": "W", "value": 1e9}, RejectReason.VALUE_OUT_OF_RANGE),
        ({"metric": "voltage", "unit": "mV", "value": -1}, RejectReason.VALUE_OUT_OF_RANGE),
    ],
)
def test_reading_reject_reasons_are_distinct(raw, reason):
    with pytest.raises(ValidationError) as exc:
        parse_reading(raw)
    assert exc.value.reason is reason, (raw, exc.value.reason)
    assert exc.value.field is not None or isinstance(raw, str)


@pytest.mark.parametrize(
    "raw,metric,si",
    [
        ({"metric": "power", "unit": "W", "value": 123}, Metric.POWER, 123.0),
        ({"metric": "power", "unit": "kW", "value": 1.5}, Metric.POWER, 1500.0),
        ({"metric": "voltage", "unit": "V", "value": 230}, Metric.VOLTAGE, 230.0),
        ({"metric": "voltage", "unit": "mV", "value": 3300}, Metric.VOLTAGE, 3.3),
        ({"metric": "current", "unit": "A", "value": 2.5}, Metric.CURRENT, 2.5),
        ({"metric": "current", "unit": "mA", "value": 250}, Metric.CURRENT, 0.25),
    ],
)
def test_unit_conversion_to_si(raw, metric, si):
    reading = parse_reading(raw)
    assert reading.metric is metric
    assert reading.unit is Unit(raw["unit"])
    assert reading.si_value == pytest.approx(si)


def test_boundary_values_accepted():
    assert parse_reading({"metric": "power", "unit": "W", "value": 0.0}).si_value == 0.0
    assert parse_reading({"metric": "power", "unit": "W", "value": 10_000_000}).si_value == 10_000_000


def test_timestamp_parsing():
    from datetime import datetime, timezone

    ref = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert parse_timestamp("2026-01-01T00:00:00Z") == ref
    assert parse_timestamp("2026-01-01T00:00:00+00:00") == ref
    assert parse_timestamp("2026-01-01T00:00:00") == ref  # naive -> UTC
    assert parse_timestamp(1767225600) == ref
    assert parse_timestamp(1767225600.0) == ref
    for bad in ["", "nope", True, False, None]:
        with pytest.raises(ValidationError) as exc:
            parse_timestamp(bad)
        assert exc.value.reason is RejectReason.BAD_TIMESTAMP


def test_require_str():
    with pytest.raises(ValidationError) as e:
        require_str({}, "k")
    assert e.value.reason is RejectReason.MISSING_FIELD
    with pytest.raises(ValidationError) as e:
        require_str({"k": "   "}, "k")
    assert e.value.reason is RejectReason.MISSING_FIELD
    with pytest.raises(ValidationError) as e:
        require_str({"k": 1}, "k")
    assert e.value.reason is RejectReason.BAD_TYPE
    assert require_str({"k": "v"}, "k") == "v"
