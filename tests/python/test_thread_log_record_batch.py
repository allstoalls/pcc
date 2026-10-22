"""Actual-body checks for complete, bounded thread-log delivery batches."""

from __future__ import annotations

import pytest

from test_runtime_log_thread_wiring import RawLogModel, _forbid_pair_registration_and_init


def pair(model):
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 17, 3)


@pytest.mark.parametrize("format_name", ["json", "text"])
@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("dropped", [0, 7])
def test_complete_thread_records_and_pending_loss_share_one_raw_write(format_name, paired, dropped):
    model = RawLogModel(format_name)
    model.state["pcc_log_thread_trace_dropped"] = dropped
    if paired:
        _forbid_pair_registration_and_init(model)
        pair(model)
    else:
        model.emit()
    assert len(model.write_calls) == 1
    assert model.opens == [(b"trace.log\0", 1, 2)] and model.closes == [17]
    assert model.output.endswith(b"\n")
    assert len(model.output.splitlines()) == 1 + int(paired) + int(dropped > 0)
    if format_name == "json":
        records = model.records()
        assert records[0]["event"] == ("safepoint_suspend_deferred" if paired else "safepoint_stop_observed")
        if paired:
            assert [(record["thread"], record["value0"], record["value1"])
                    for record in records[:2]] == [(41, 17, 3)] * 2
            assert records[1]["event"] == "safepoint_resume_deferred"
        if dropped:
            assert records[-1]["event"] == "trace_dropped" and records[-1]["value0"] == dropped
    else:
        assert b"[pcc.thread] ts=123 thread=" in model.output
        assert b"value0=17 value1=3 ptr=0x0\n" in model.output if paired else b"value0=11 value1=2 ptr=0x0\n" in model.output
    assert model.state["pcc_log_write_lock"] == model.state["pcc_log_emitting"] == 0
    assert model.state["pcc_log_thread_trace_dropped"] == 0


@pytest.mark.parametrize("format_name", ["json", "text"])
def test_every_successful_short_write_boundary_preserves_the_exact_batch(format_name):
    full = RawLogModel(format_name)
    full.state["pcc_log_thread_trace_dropped"] = 7
    pair(full)
    expected = bytes(full.output)
    # Every possible split across the pair and drop notice retries exactly
    # the remaining suffix. EINTR before either segment does not lose bytes.
    for boundary in range(1, len(expected)):
        model = RawLogModel(format_name)
        model.state["pcc_log_thread_trace_dropped"] = 7
        model.write_results = [-4, boundary, -4]
        pair(model)
        assert model.output == expected, boundary
        assert model.write_calls[0][1] == expected
        assert model.write_calls[1][1] == expected
        assert model.write_calls[2][1] == expected[boundary:]
        assert model.write_calls[3][1] == expected[boundary:]
        assert model.closes == [17]
        assert model.state["pcc_log_write_lock"] == model.state["pcc_log_emitting"] == 0


class SimulatedProcessExit(BaseException):
    pass


@pytest.mark.parametrize("format_name", ["json", "text"])
def test_exit_at_every_formatting_step_cannot_leave_a_record_fragment(format_name):
    complete = RawLogModel(format_name)
    complete.state["pcc_log_thread_trace_dropped"] = 7
    original = complete.namespace["_append_trace_text"]
    operations = []

    def count(*args):
        operations.append(1)
        return original(*args)

    complete.namespace["_append_trace_text"] = count
    pair(complete)
    for stop_at in range(len(operations)):
        model = RawLogModel(format_name)
        model.state["pcc_log_thread_trace_dropped"] = 7
        append = model.namespace["_append_trace_text"]
        seen = []

        def interrupted(*args):
            if len(seen) == stop_at:
                raise SimulatedProcessExit
            seen.append(1)
            return append(*args)

        model.namespace["_append_trace_text"] = interrupted
        with pytest.raises(SimulatedProcessExit):
            pair(model)
        assert model.output == b"" and model.write_calls == []
        assert model.opens == []  # I/O starts only after complete formatting.


@pytest.mark.parametrize("after_write", [False, True])
def test_exit_around_normal_full_write_leaves_zero_or_complete_records(after_write):
    model = RawLogModel()
    model.state["pcc_log_thread_trace_dropped"] = 7
    original = model.namespace["write"]

    def exit_at_write(*args):
        if after_write:
            original(*args)
        raise SimulatedProcessExit

    model.namespace["write"] = exit_at_write
    with pytest.raises(SimulatedProcessExit):
        pair(model)
    if after_write:
        assert [record["event"] for record in model.records()] == [
            "safepoint_suspend_deferred", "safepoint_resume_deferred", "trace_dropped"]
    else:
        assert model.output == b""


@pytest.mark.parametrize("error", [0, -5])
@pytest.mark.parametrize("prefix", [0, 9])
def test_batch_io_error_is_bounded_closes_sink_and_retains_loss(error, prefix):
    model = RawLogModel()
    model.state["pcc_log_thread_trace_dropped"] = 7
    model.write_results = ([prefix] if prefix else []) + [error]
    pair(model)
    assert len(model.output) == prefix
    assert len(model.write_calls) == 1 + int(prefix > 0)
    assert model.closes == [17]
    assert model.state["pcc_log_write_lock"] == model.state["pcc_log_emitting"] == 0
    assert model.state["pcc_log_thread_trace_dropped"] == 9
    # A fatal error after a successful partial OS write retains that prefix;
    # this sink does not promise transactional file rollback or crash recovery.


@pytest.mark.parametrize("format_name", ["json", "text"])
def test_bounded_trace_formatter_matches_existing_extreme_wire_values(format_name):
    values = [-(1 << 63), (1 << 63) - 1]
    for value in values:
        model = RawLogModel(format_name)
        model.namespace["pcc_diagnostics_runtime_log_event_code"](9, 23, value, value, (1 << 64) - 1)
        reference = RawLogModel(format_name)
        reference.namespace["pcc_diagnostics_runtime_log_event"](
            b"thread\0", b"resume_world_failed\0", value, value, (1 << 64) - 1)
        assert model.output == reference.output
        assert len(model.write_calls) == 1


def test_unexpected_future_event_overflow_drops_the_whole_unwritten_batch():
    model = RawLogModel()
    model.state["pcc_log_thread_trace_dropped"] = 7
    model.namespace["_event_from_code"] = lambda *args: b"x" * 2000 + b"\0"
    pair(model)
    assert model.output == b"" and model.opens == [] and model.write_calls == []
    assert model.state["pcc_log_thread_trace_dropped"] == 9
    assert model.state["pcc_log_write_lock"] == model.state["pcc_log_emitting"] == 0
