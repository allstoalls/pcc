"""Observe the general datetime API needed by TOML against CPython 3.15."""

import ast
import datetime as host_datetime
from pathlib import Path
import re
import time as clock
import tomllib as host_tomllib

import pytest

from pcc.stdlib import datetime as owned_datetime
from pcc.stdlib import tomllib as owned_tomllib
from pcc.stdlib.tomllib import _re as owned_toml_re


def _date_record(value):
    return (
        value.year, value.month, value.day, value.toordinal(),
        value.weekday(), value.isoweekday(), tuple(value.isocalendar()),
        value.isoformat(), str(value), repr(value),
    )


def _delta_record(value):
    return (
        value.days, value.seconds, value.microseconds,
        value.total_seconds(), str(value), repr(value),
    )


def _time_record(value):
    offset = value.utcoffset()
    return (
        value.hour, value.minute, value.second, value.microsecond, value.fold,
        None if offset is None else _delta_record(offset), value.tzname(),
        value.isoformat(), str(value), repr(value),
    )


def _datetime_record(value):
    offset = value.utcoffset()
    return (
        value.year, value.month, value.day,
        value.hour, value.minute, value.second, value.microsecond, value.fold,
        None if offset is None else _delta_record(offset), value.tzname(),
        value.isoformat(), str(value), repr(value),
    )


def test_provider_imports_no_host_datetime_or_c_accelerator():
    source = Path(owned_datetime.__file__).read_text(encoding="utf-8")
    imports = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    assert imports.isdisjoint({"datetime", "_datetime", "_pydatetime", "_strptime"})
    assert owned_datetime.__all__ == host_datetime.__all__
    assert owned_datetime.MINYEAR == host_datetime.MINYEAR
    assert owned_datetime.MAXYEAR == host_datetime.MAXYEAR


@pytest.mark.parametrize("args", [
    (1, 1, 1), (9999, 12, 31), (2000, 2, 29), (1900, 3, 1),
    (2024, 2, 29), (2020, 12, 31), (2021, 1, 1),
])
def test_date_constructor_calendar_and_iso_match_cpython(args):
    actual = owned_datetime.date(*args)
    expected = host_datetime.date(*args)
    assert _date_record(actual) == _date_record(expected)
    assert _date_record(owned_datetime.date.fromordinal(actual.toordinal())) == _date_record(expected)
    assert _date_record(owned_datetime.date.fromisoformat(expected.isoformat())) == _date_record(expected)


@pytest.mark.parametrize("kind, args", [
    ("date", (0, 1, 1)), ("date", (10000, 1, 1)),
    ("date", (2023, 2, 29)), ("date", (1900, 2, 29)),
    ("date", (2024, 13, 1)), ("date", (2024, 4, 31)),
    ("date", (2024.0, 1, 1)),
    ("time", (24, 0)), ("time", (1, 60)),
    ("time", (1, 2, 60)), ("time", (1, 2, 3, 1000000)),
    ("datetime", (2024, 2, 29, 24)),
])
def test_invalid_constructor_values_raise_same_exception_class(kind, args):
    records = []
    for module in (owned_datetime, host_datetime):
        try:
            getattr(module, kind)(*args)
        except Exception as exc:
            records.append(type(exc))
        else:
            records.append(None)
    assert records[0] is records[1]
    assert records[0] in (TypeError, ValueError)


class _IndexValue:
    def __init__(self, value):
        self.value = value

    def __index__(self):
        return self.value


def test_constructor_uses_integer_index_protocol():
    args = (_IndexValue(2024), _IndexValue(2), _IndexValue(29))
    assert _date_record(owned_datetime.date(*args)) == _date_record(host_datetime.date(*args))


@pytest.mark.parametrize("arguments", [
    {}, {"seconds": -1}, {"microseconds": -1},
    {"weeks": 2, "days": -3, "hours": 4, "minutes": 5},
    {"days": 0.125, "seconds": -0.25},
    {"microseconds": 0.5}, {"microseconds": 1.5}, {"microseconds": -1.5},
    {"days": 999999999},
])
def test_timedelta_normalization_and_rounding_match_cpython(arguments):
    assert _delta_record(owned_datetime.timedelta(**arguments)) == _delta_record(
        host_datetime.timedelta(**arguments)
    )


@pytest.mark.parametrize("args", [(0,), (12, 34), (23, 59, 59, 999999)])
@pytest.mark.parametrize("offset", [None, 0, 19800, -25200])
def test_time_constructor_offsets_and_iso_match_cpython(args, offset):
    records = []
    for module in (owned_datetime, host_datetime):
        tz = None if offset is None else module.timezone(module.timedelta(seconds=offset))
        records.append(_time_record(module.time(*args, tzinfo=tz, fold=1)))
    assert records[0] == records[1]


@pytest.mark.parametrize("offset", [None, 0, 19800, -25200, 86399.5])
def test_datetime_constructor_offsets_and_iso_match_cpython(offset):
    records = []
    for module in (owned_datetime, host_datetime):
        tz = None if offset is None else module.timezone(module.timedelta(seconds=offset))
        value = module.datetime(2024, 2, 29, 12, 34, 56, 123456, tzinfo=tz, fold=1)
        assert isinstance(value, module.date)
        records.append(_datetime_record(value))
    assert records[0] == records[1]


def test_timezone_utc_identity_offsets_names_and_validation():
    records = []
    for module in (owned_datetime, host_datetime):
        assert module.UTC is module.timezone.utc
        assert module.timezone(module.timedelta(0)) is module.UTC
        zone = module.timezone(module.timedelta(hours=5, minutes=30), "named zone")
        records.append((str(zone), repr(zone), zone.tzname(None), _delta_record(zone.utcoffset(None))))
        for seconds in (-86400, 86400):
            with pytest.raises(ValueError):
                module.timezone(module.timedelta(seconds=seconds))
        with pytest.raises(TypeError):
            module.timezone(3600)
        with pytest.raises(TypeError):
            module.time(1, tzinfo="UTC")
    assert records[0] == records[1]


def test_aware_datetime_equality_arithmetic_and_offset_conversion():
    records = []
    for module in (owned_datetime, host_datetime):
        utc = module.datetime(2024, 2, 29, 12, tzinfo=module.UTC)
        east = module.datetime(2024, 2, 29, 15, tzinfo=module.timezone(module.timedelta(hours=3)))
        records.append((
            utc == east, hash(utc) == hash(east), _delta_record(east - utc),
            _datetime_record(east.astimezone(module.UTC)),
            _datetime_record(utc + module.timedelta(days=1, microseconds=1)),
        ))
    assert records[0] == records[1]


def test_public_calendar_properties_are_read_only():
    for module in (owned_datetime, host_datetime):
        value = module.datetime(2024, 2, 29, 12, 34, 56)
        for name in ("year", "month", "day", "hour", "minute", "second", "microsecond", "tzinfo"):
            with pytest.raises(AttributeError):
                setattr(value, name, 3)


def test_now_reads_real_clock_and_uses_utc():
    before = clock.time()
    value = owned_datetime.datetime.now(owned_datetime.UTC)
    after = clock.time()
    assert before <= value.timestamp() <= after
    assert value.tzinfo is owned_datetime.UTC


@pytest.mark.parametrize("kind, text, fmt", [
    ("date", "2024-02-29", "%Y-%m-%d"),
    ("time", "12:34", "%H:%M"),
    ("datetime", "2024-02-29 12:34", "%Y-%m-%d %H:%M"),
])
def test_unowned_string_parser_has_explicit_capability_error(kind, text, fmt):
    with pytest.raises(NotImplementedError, match="owned _strptime parser"):
        getattr(owned_datetime, kind).strptime(text, fmt)


@pytest.mark.parametrize("value, kind, record", [
    ("2024-02-29", "date", _date_record),
    ("12:34", "time", _time_record),
    ("12:34:56.123456789", "time", _time_record),
    ("1979-05-27T07:32:00.123456", "datetime", _datetime_record),
    ("1979-05-27T07:32:00Z", "datetime", _datetime_record),
    ("1979-05-27 07:32:00-07:00", "datetime", _datetime_record),
])
def test_general_toml_uses_owned_calendar_objects(monkeypatch, value, kind, record):
    # Host imports resolve absolute "datetime" to CPython. Select the owned
    # implementation explicitly to exercise the parser's actual constructors.
    for name in ("date", "datetime", "time", "timedelta", "timezone", "tzinfo"):
        monkeypatch.setattr(owned_toml_re, name, getattr(owned_datetime, name))
    owned_toml_re.cached_tz.cache_clear()
    try:
        document = "value = " + value + "\n"
        actual = owned_tomllib.loads(document)["value"]
        expected = host_tomllib.loads(document)["value"]
        assert type(actual) is getattr(owned_datetime, kind)
        assert record(actual) == record(expected)
    finally:
        owned_toml_re.cached_tz.cache_clear()


@pytest.mark.parametrize("kind", ["time", "datetime"])
@pytest.mark.parametrize("microsecond", [0, 123456, 999999])
@pytest.mark.parametrize("timespec", [
    "auto", "hours", "minutes", "seconds", "milliseconds", "microseconds",
    "invalid", "", None, 1, [], b"seconds",
])
def test_isoformat_timespec_behavior_matches_cpython(kind, microsecond, timespec):
    records = []
    for module in (owned_datetime, host_datetime):
        arguments = (12, 34, 56, microsecond)
        if kind == "datetime":
            arguments = (2024, 2, 29) + arguments
        value = getattr(module, kind)(*arguments)
        try:
            records.append(("value", value.isoformat(timespec=timespec)))
        except Exception as exc:
            records.append(("exception", type(exc)))
    assert records[0] == records[1]


def test_iso_time_formatter_has_real_no_libpython_body(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    output = tmp_path / "datetime.ll"
    compile_python(str(Path(owned_datetime.__file__)), str(output),
                   emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self")
    text = output.read_text()
    body = re.search(r"^define[^\n]*@user_pcc_stdlib_datetime__format_time\(.*?^}", text, re.M | re.S)
    assert body is not None
    assert "strict.nolib.stub" not in body.group()
    assert "@py_cpy_" not in body.group()


def test_timedelta_constructor_emits_type_correct_owned_object(tmp_path):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python

    output = tmp_path / "datetime.ll"
    compile_python(str(Path(owned_datetime.__file__)), str(output),
                   emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self",
                   recursive_stdlib=True, target_triple="arm64-apple-darwin")
    # Parsing alone missed a boxed float passed as a double to fabs after
    # timedelta's modf branch join. Actual owned emission verifies operands.
    assert emit_owned_object(output.read_text(), "arm64-apple-darwin")[:4] == b"\xcf\xfa\xed\xfe"
