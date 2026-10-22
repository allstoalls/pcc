from __future__ import annotations

import shlex
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_self_backend_emit as emit
from pcc.frontends.python.pipeline_self_backend_emit import emit_objects_many_in_process
from pcc.frontends.python.pipeline_self_backend_emit import run_emit_worker_pool


MANIFEST_VERSION = "pcc.self_backend.emit_batch.v1"


@pytest.fixture(autouse=True)
def _mock_workers_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("these coordinator tests must not compile or launch workers")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name in ("system", "fork", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    for name in ("emit_in_process", "run_emit_worker", "run_split_worker"):
        monkeypatch.setattr(emit, name, forbidden)


def _worker_items(tmp_path: Path, count: int):
    return [
        (
            str(tmp_path / f"result-{index}"),
            str(tmp_path / f"object-{index}"),
            str(tmp_path / f"input-{index}.ll"),
        )
        for index in range(count)
    ]


def _manifest_items(command: str):
    manifest = Path(shlex.split(command)[-1])
    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert lines[0] == MANIFEST_VERSION
    payload = lines[1:]
    assert len(payload) % 4 == 0
    return [payload[index : index + 4] for index in range(0, len(payload), 4)]


def test_oversized_emit_items_get_one_fresh_process_each(tmp_path: Path) -> None:
    command_batches = []

    def record_commands(commands, max_parallel=None):
        command_batches.append((list(commands), max_parallel))

    process_count = run_emit_worker_pool(
        ["/tmp/pcc1"],
        _worker_items(tmp_path, 5),
        "",
        str(tmp_path),
        "oversized",
        1,
        batch_max_items=4,
        manifest_version=MANIFEST_VERSION,
        item_bytes=[13_000_000, 11_000_000, 6_000_000, 5_000_000, 4_000_000],
        worker_arg="--pcc-self-backend-emit-batch-worker",
        small_int_decimal=str,
        shell_quote_arg=shlex.quote,
        run_worker_commands=record_commands,
    )

    assert process_count == 5
    commands, max_parallel = command_batches[0]
    assert max_parallel == 1
    assert len(commands) == 5
    assert all(len(_manifest_items(command)) == 1 for command in commands)


def test_safe_emit_batches_are_balanced_by_input_bytes(tmp_path: Path) -> None:
    command_batches = []

    def record_commands(commands, max_parallel=None):
        command_batches.append((list(commands), max_parallel))

    items = _worker_items(tmp_path, 5)
    sizes = [10, 9, 8, 1, 1]
    size_by_input = {item[2]: size for item, size in zip(items, sizes)}
    process_count = run_emit_worker_pool(
        ["/tmp/pcc1"],
        items,
        "",
        str(tmp_path),
        "safe",
        3,
        batch_max_items=2,
        manifest_version=MANIFEST_VERSION,
        item_bytes=sizes,
        worker_arg="--pcc-self-backend-emit-batch-worker",
        small_int_decimal=str,
        shell_quote_arg=shlex.quote,
        run_worker_commands=record_commands,
    )

    assert process_count == 3
    commands, max_parallel = command_batches[0]
    assert max_parallel == 3
    loads = []
    for command in commands:
        loads.append(
            sum(size_by_input[item[0]] for item in _manifest_items(command))
        )
    assert sorted(loads) == [9, 10, 10]


def test_native_emitter_materializes_text_for_the_worker_owner(
    tmp_path: Path,
) -> None:
    ir_text = 'target triple = "arm64-apple-darwin23.6.0"\n'
    observed_paths = []

    def run_pool(
        _prefix,
        worker_items,
        _cc,
        _tmp_dir,
        _label,
        _jobs,
        _fresh,
        **_kwargs,
    ):
        for result_path, object_path, ir_path in worker_items:
            observed_paths.append(ir_path)
            assert Path(ir_path).read_text(encoding="utf-8") == ir_text
            Path(object_path).write_text("asm\n", encoding="utf-8")
            Path(result_path).write_text(
                "self-aarch64-darwin-v0\n" + object_path + "\noff\n",
                encoding="utf-8",
            )
        return len(worker_items)

    results = emit_objects_many_in_process(
        [ir_text],
        str(tmp_path),
        "cc",
        split_large_modules=False,
        profile=None,
        internal_link=True,
        parse_target_triple=lambda _text: "arm64-apple-darwin23.6.0",
        host_target_triple=lambda: "arm64-apple-darwin23.6.0",
        target_supported=lambda _triple: True,
        native_worker_executable=lambda: "/tmp/pcc1",
        split_large_ir_modules=lambda texts: texts,
        source_workers_worthwhile=lambda _texts: False,
        worker_command_prefix_for_frontend=lambda: [],
        split_threshold_bytes=lambda: 2_000_000,
        split_shard_bytes=lambda: 1_000_000,
        jobs_for_ir_texts=lambda _texts, **_kwargs: 1,
        profile_counter=lambda *_args: None,
        profiled_gc_collect=lambda *_args, **_kwargs: None,
        profile_begin=lambda _profile: 0,
        profile_end=lambda *_args: None,
        run_worker_commands=lambda *_args, **_kwargs: None,
        small_int_decimal=str,
        shell_quote_arg=shlex.quote,
        split_worker_arg="--split-worker",
        plan_cache=lambda items, *_args: [("", "off") for _item in items],
        jobs=lambda _count: 1,
        jobs_for_input_sizes=lambda _sizes, **_kwargs: 1,
        jobs_env="PCC_SELF_BACKEND_JOBS",
        run_emit_worker_pool=run_pool,
        publish_cache=lambda *_args: True,
        maintain_cache=lambda *_args: None,
        emit_in_process=lambda _text: (_ for _ in ()).throw(
            AssertionError("native path must use the worker adapter")
        ),
        join_strings=lambda values, sep: sep.join(values),
    )

    expected_path = str(tmp_path / "self_backend_module_0.ll")
    assert observed_paths == [expected_path]
    assert Path(expected_path).read_text(encoding="utf-8") == ir_text
    # An internally linked native worker writes pcc's own object (``.pco``);
    # ``.o`` is the external-link artifact.
    assert results == [
        ("self-aarch64-darwin-v0", str(tmp_path / "self_backend_native_0.pco"))
    ]


def _emit_one_oversized(tmp_path: Path, oversized_admission):
    ir_text = 'target triple = "arm64-apple-darwin23.6.0"\n'
    tmp_path.mkdir()
    pools = []

    def run_pool(_prefix, worker_items, _cc, _tmp_dir, label, jobs, _fresh, **kwargs):
        pools.append((label, jobs, kwargs.get("admission_width"), kwargs.get("admission_byte_cap")))
        for result_path, object_path, _ir_path in worker_items:
            Path(object_path).write_text("asm\n", encoding="utf-8")
            Path(result_path).write_text(
                "self-aarch64-darwin-v0\n" + object_path + "\noff\n", encoding="utf-8",
            )
        return len(worker_items)

    emit_objects_many_in_process(
        [ir_text],
        str(tmp_path),
        "cc",
        split_large_modules=False,
        profile=None,
        internal_link=True,
        parse_target_triple=lambda _text: "arm64-apple-darwin23.6.0",
        host_target_triple=lambda: "arm64-apple-darwin23.6.0",
        target_supported=lambda _triple: True,
        native_worker_executable=lambda: "/tmp/pcc1",
        split_large_ir_modules=lambda texts: texts,
        source_workers_worthwhile=lambda _texts: False,
        worker_command_prefix_for_frontend=lambda: [],
        # The one input is at the threshold, so it takes the oversized lane.
        split_threshold_bytes=lambda: len(ir_text),
        split_shard_bytes=lambda: 1_000_000,
        jobs_for_ir_texts=lambda _texts, **_kwargs: 1,
        profile_counter=lambda *_args: None,
        profiled_gc_collect=lambda *_args, **_kwargs: None,
        profile_begin=lambda _profile: 0,
        profile_end=lambda *_args: None,
        run_worker_commands=lambda *_args, **_kwargs: None,
        small_int_decimal=str,
        shell_quote_arg=shlex.quote,
        split_worker_arg="--split-worker",
        plan_cache=lambda items, *_args: [("", "off") for _item in items],
        jobs=lambda _count: 1,
        jobs_for_input_sizes=lambda _sizes, **_kwargs: 1,
        jobs_env="PCC_SELF_BACKEND_JOBS",
        run_emit_worker_pool=run_pool,
        publish_cache=lambda *_args: True,
        maintain_cache=lambda *_args: None,
        emit_in_process=lambda _text: None,
        join_strings=lambda values, sep: sep.join(values),
        oversized_admission=oversized_admission,
    )
    return pools


def test_budgeted_oversized_admission_sets_the_wave_width_and_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_OVERSIZED_BYTE_CAP", raising=False)
    counts = []

    def admitted(count):
        counts.append(count)
        return 3, 123_456

    assert _emit_one_oversized(tmp_path / "budget", admitted) == [("oversized", 3, 3, 123_456)]
    assert counts == [1]
    # Without a declared budget the fixed 7 MB / width-2 waves stay.
    assert _emit_one_oversized(tmp_path / "fixed", lambda _count: None) == [("oversized", 1, 2, 7_000_000)]
    # An explicit byte cap keeps its legacy meaning over the budget model.
    monkeypatch.setenv("PCC_SELF_BACKEND_OVERSIZED_BYTE_CAP", "5")
    assert _emit_one_oversized(tmp_path / "explicit", admitted) == [("oversized", 1, 2, 5)]
    assert counts == [1]


class _PhaseSink:
    def __init__(self):
        self.lines = []
        self.events = []
        self.clock_calls = 0

    def monotonic(self):
        self.clock_calls += 1
        return self.clock_calls + 0.25

    def write(self, message):
        self.lines.append(message)
        self.events.append("write")

    def flush(self):
        self.events.append("flush")

    def records(self):
        return [dict(field.split("=", 1) for field in line.split()[4:]) for line in self.lines]


def _phase_sink(monkeypatch):
    sink = _PhaseSink()
    # Replace this module's bindings, not pytest's process-wide clock/stderr.
    monkeypatch.setattr(emit, "time", SimpleNamespace(monotonic=sink.monotonic), raising=False)
    monkeypatch.setattr(emit, "sys", SimpleNamespace(stderr=sink, implementation=sys.implementation))
    return sink


def _pool(tmp_path, dispatch, *, sizes=None, label="medium", count=5,
          batch_max_items=2, jobs=8, cap=0, diagnostic_total=-1, diagnostic_max=-1):
    return run_emit_worker_pool(
        ["mock-worker"], _worker_items(tmp_path, count), "", str(tmp_path), label, jobs,
        batch_max_items=batch_max_items, manifest_version=MANIFEST_VERSION,
        item_bytes=sizes, worker_arg="--mock-emit-worker", small_int_decimal=str,
        shell_quote_arg=shlex.quote, run_worker_commands=dispatch,
        admission_byte_cap=cap, admission_width=2,
        diagnostic_input_size_total=diagnostic_total,
        diagnostic_input_size_max=diagnostic_max,
    )


def test_phase_timing_flag_and_disabled_weight_access(tmp_path, monkeypatch):
    sink = _phase_sink(monkeypatch)

    class Sizes(list):
        reads = 0

        def __getitem__(self, index):
            self.reads += 1
            return super().__getitem__(index)

    counts = []
    for flag in (None, "", "0", "false", "unexpected", "1", " TRUE ", "yes", "on"):
        if flag is None:
            monkeypatch.delenv("PCC_PY_FRONTEND_WORKER_TIMING", raising=False)
        else:
            monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", flag)
        sizes = Sizes([10, 9, 8, 1, 1])
        before = sink.clock_calls
        _pool(tmp_path, lambda *_args, **_kwargs: None, sizes=sizes)
        enabled = flag in ("1", " TRUE ", "yes", "on")
        assert sink.clock_calls - before == (2 if enabled else 0)
        counts.append(sizes.reads)
    # Diagnostics reuse weights already read for packing; no extra size walk.
    assert len(set(counts)) == 1


def test_phase_timing_actual_batch_counts_and_flush_order(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    sink = _phase_sink(monkeypatch)
    observed = []

    def dispatch(commands, max_parallel):
        assert sink.events == ["write", "flush"]
        observed.append((len(commands), max_parallel))
        sink.events.append("dispatch")
        # One call snapshots the flag for both boundaries.
        monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "0")

    assert _pool(tmp_path, dispatch, sizes=[10, 9, 8, 1, 1]) == 3
    assert observed == [(3, 3)]
    assert sink.events == ["write", "flush", "dispatch", "write", "flush"]
    start, end = sink.records()
    assert start == {
        "event": "START", "phase": "emit", "lane": "medium", "wave": "1",
        "monotonic_s": "1.25", "input_count": "5", "command_count": "3",
        "scheduler_input_size_total": "29", "scheduler_input_size_max": "10",
        "requested_jobs": "3",
    }
    assert end == dict(start, event="END", monotonic_s="2.25")
    assert all(str(tmp_path) not in line and "input-" not in line for line in sink.lines)


@pytest.mark.parametrize("label,sizes,cap", [
    ("small", None, 0), ("medium", [10, 9, 8, 1, 1], 0),
    ("oversized", [13, 11, 6, 5, 4], 17),
])
def test_phase_timing_does_not_change_dispatch(tmp_path, monkeypatch, label, sizes, cap):
    sink = _phase_sink(monkeypatch)
    runs = []
    for flag in ("0", "1"):
        monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", flag)
        calls = []

        def dispatch(commands, max_parallel):
            calls.append((list(commands), max_parallel, [
                Path(shlex.split(command)[-1]).read_bytes() for command in commands
            ]))

        result = _pool(tmp_path, dispatch, label=label, sizes=sizes, cap=cap,
                       diagnostic_total=29, diagnostic_max=10)
        runs.append((result, calls))
    assert runs[0] == runs[1]
    records = sink.records()
    assert len(records) == 2 * len(runs[1][1])
    for index, (_commands, jobs, manifests) in enumerate(runs[1][1]):
        start, end = records[index * 2:index * 2 + 2]
        assert start["event"] == "START" and end["event"] == "END"
        assert start["wave"] == end["wave"] == str(index + 1)
        assert start["requested_jobs"] == end["requested_jobs"] == str(jobs)
        assert start["command_count"] == str(len(manifests))
    if label == "oversized":
        assert [(r["input_count"], r["scheduler_input_size_total"],
                 r["scheduler_input_size_max"]) for r in records[::2]] == [
            ("2", "17", "13"), ("2", "17", "11"), ("1", "5", "5"),
        ]


@pytest.mark.parametrize("label,fail_call", [("medium", 1), ("oversized", 2)])
def test_phase_timing_failed_dispatch_has_no_end(tmp_path, monkeypatch, label, fail_call):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    sink = _phase_sink(monkeypatch)
    failure = RuntimeError("mock dispatch failed")
    calls = []

    def dispatch(commands, max_parallel):
        calls.append((commands, max_parallel))
        if len(calls) == fail_call:
            raise failure

    with pytest.raises(RuntimeError) as caught:
        _pool(tmp_path, dispatch, label=label, sizes=[13, 11, 6, 5, 4], cap=17)
    assert caught.value is failure
    assert len(calls) == fail_call
    assert [r["event"] for r in sink.records()] == ["START", "END"] * (fail_call - 1) + ["START"]


@pytest.mark.parametrize("fault", ["monotonic", "write", "flush"])
@pytest.mark.parametrize("dispatch_fails", [False, True])
def test_phase_diagnostic_errors_do_not_replace_dispatch(tmp_path, monkeypatch, fault, dispatch_fails):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    sink = _phase_sink(monkeypatch)

    def broken(*_args):
        raise OSError("mock diagnostic failure")

    monkeypatch.setattr(emit.time if fault == "monotonic" else sink, fault, broken)
    failure = RuntimeError("original worker error")
    calls = []

    def dispatch(commands, max_parallel):
        calls.append((commands, max_parallel))
        if dispatch_fails:
            raise failure

    if dispatch_fails:
        with pytest.raises(RuntimeError) as caught:
            _pool(tmp_path, dispatch)
        assert caught.value is failure
    else:
        assert _pool(tmp_path, dispatch) == 3
    assert len(calls) == 1


def _fake_result(result_path, object_path, status="off"):
    Path(object_path).write_text("mock object\n", encoding="utf-8")
    Path(result_path).write_text(
        "self-aarch64-darwin-v0\n" + object_path + "\n" + status + "\n", encoding="utf-8",
    )


def _mock_emit(tmp_path, texts, *, native=True, split=False, threshold=2_000_000,
               hits=(), split_dispatch=None, calls=None, pool_options=None, invalid_result=False):
    calls = [] if calls is None else calls

    def dispatch(commands, max_parallel):
        calls.append((list(commands), max_parallel, [
            Path(shlex.split(command)[-1]).read_bytes() for command in commands
        ]))
        if not invalid_result:
            for command in commands:
                for _input_path, result_path, object_path, _cc in _manifest_items(command):
                    _fake_result(result_path, object_path)

    def pool(prefix, items, cc, directory, label, jobs, fresh, **kwargs):
        if pool_options is not None:
            pool_options.append((label, dict(kwargs)))
        return run_emit_worker_pool(
            prefix, items, cc, directory, label, jobs,
            batch_max_items=1 if fresh else 2, manifest_version=MANIFEST_VERSION,
            worker_arg="--mock-emit-worker", small_int_decimal=str,
            shell_quote_arg=shlex.quote, run_worker_commands=dispatch, **kwargs,
        )

    def cache_plan(items, *_args):
        plan = []
        for index, (result_path, object_path, _input_path) in enumerate(items):
            status = "hit" if index in hits else "off"
            if status == "hit":
                _fake_result(result_path, object_path, status)
            plan.append(("", status))
        return plan

    return emit_objects_many_in_process(
        texts, str(tmp_path), "", split_large_modules=split, profile=None, internal_link=True,
        parse_target_triple=lambda _text: "arm64-apple-darwin23.6.0",
        host_target_triple=lambda: "arm64-apple-darwin23.6.0", target_supported=lambda _triple: True,
        native_worker_executable=lambda: "mock-worker" if native else "",
        split_large_ir_modules=lambda values: values, source_workers_worthwhile=lambda _texts: True,
        worker_command_prefix_for_frontend=lambda: ["mock-source-worker"],
        split_threshold_bytes=lambda: threshold, split_shard_bytes=lambda: 10,
        jobs_for_ir_texts=lambda *_args, **_kwargs: 7,
        profile_counter=lambda *_args: None, profiled_gc_collect=lambda *_args, **_kwargs: None,
        profile_begin=lambda _profile: 0, profile_end=lambda *_args: None,
        run_worker_commands=split_dispatch, small_int_decimal=str, shell_quote_arg=shlex.quote,
        split_worker_arg="--mock-split-worker", plan_cache=cache_plan, jobs=lambda _count: 4,
        jobs_for_input_sizes=lambda *_args, **_kwargs: 4, jobs_env="PCC_SELF_BACKEND_JOBS",
        run_emit_worker_pool=pool, publish_cache=lambda *_args: True, maintain_cache=lambda *_args: None,
        emit_in_process=emit.emit_in_process, join_strings=lambda values, sep: sep.join(values),
    )


def test_small_phase_sizes_count_only_misses_without_reordering(tmp_path, monkeypatch):
    sink = _phase_sink(monkeypatch)
    texts = ["a", "b" * 10, "é" * 3, "d" * 8, "ee"]
    runs = []
    for flag in ("0", "1"):
        monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", flag)
        calls, options = [], []
        result = _mock_emit(tmp_path, texts, native=False, hits=(1,), calls=calls, pool_options=options)
        assert len(result) == 5
        assert options[0][0] == "small" and "item_bytes" not in options[0][1]
        assert options[0][1]["diagnostic_input_size_total"] == (14 if flag == "1" else -1)
        runs.append(calls)
    assert runs[0] == runs[1]
    records = sink.records()
    assert [(r["input_count"], r["command_count"], r["scheduler_input_size_total"],
             r["scheduler_input_size_max"], r["requested_jobs"]) for r in records] == [
        ("4", "2", "14", "8", "2"), ("4", "2", "14", "8", "2"),
    ]
    # The non-ASCII source uses the existing len(text) scheduler weight.
    assert sum(len(text.encode("utf-8")) for i, text in enumerate(texts) if i != 1) == 17
    commands = runs[1][0][0]
    assert [[Path(row[0]).name for row in _manifest_items(command)] for command in commands] == [
        ["self_backend_module_0.ll", "self_backend_module_2.ll"],
        ["self_backend_module_3.ll", "self_backend_module_4.ll"],
    ]


@pytest.mark.parametrize("native", [False, True])
def test_actual_emit_lane_order_and_sizes_are_unchanged(tmp_path, monkeypatch, native):
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_OVERSIZED_BYTE_CAP", raising=False)
    sink = _phase_sink(monkeypatch)
    # Synthetic payloads are only written and weighed; fake workers never parse them.
    texts = ["x" * 4_000_001, "y" * 2_000_001, "z" * 1_000_001, "s"]
    runs = []
    for flag in ("0", "1"):
        monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", flag)
        calls = []
        assert len(_mock_emit(tmp_path, texts, native=native, calls=calls)) == 4
        runs.append(calls)
    assert runs[0] == runs[1]
    expected = (
        [("oversized", "2", "2", "6000002", "4000001", "2"),
         ("medium", "1", "1", "1000001", "1000001", "1"),
         ("small", "1", "1", "1", "1", "1")]
        if native else
        [("huge", "1", "1", "4000001", "4000001", "1"),
         ("medium", "2", "1", "3000002", "2000001", "1"),
         ("small", "1", "1", "1", "1", "1")]
    )
    assert [tuple(r[key] for key in (
        "lane", "input_count", "command_count", "scheduler_input_size_total",
        "scheduler_input_size_max", "requested_jobs",
    )) for r in sink.records()[::2]] == expected
    assert [r["event"] for r in sink.records()] == ["START", "END"] * 3


@pytest.mark.parametrize("fails", [False, True])
def test_split_phase_records_actual_inputs_and_failure_boundary(tmp_path, monkeypatch, fails):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    sink = _phase_sink(monkeypatch)
    failure = RuntimeError("mock split failed")
    calls = []

    def split_dispatch(commands, max_parallel):
        assert sink.events == ["write", "flush"]
        assert len(commands) == max_parallel == 2
        if fails:
            raise failure
        for command in commands:
            args = shlex.split(command)
            shard = args[4] + "0.ll"
            Path(shard).write_text("shard", encoding="utf-8")
            Path(args[3]).write_text("pcc.self_backend.split.v1\n" + shard + "\n", encoding="utf-8")

    if fails:
        with pytest.raises(RuntimeError) as caught:
            _mock_emit(tmp_path, ["é" * 30, "s" * 10, "L" * 40], split=True,
                       threshold=20, split_dispatch=split_dispatch, calls=calls)
        assert caught.value is failure
        assert calls == []
        assert [r["event"] for r in sink.records()] == ["START"]
    else:
        assert len(_mock_emit(tmp_path, ["é" * 30, "s" * 10, "L" * 40], split=True,
                              threshold=20, split_dispatch=split_dispatch, calls=calls)) == 3
        assert [r["phase"] for r in sink.records()] == ["split", "split", "emit", "emit"]
        assert [r["event"] for r in sink.records()] == ["START", "END", "START", "END"]
    start = sink.records()[0]
    assert (start["input_count"], start["command_count"], start["scheduler_input_size_total"],
            start["scheduler_input_size_max"], start["requested_jobs"]) == ("2", "2", "70", "40", "2")


def test_no_phase_for_empty_or_cached_emit_work(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    sink = _phase_sink(monkeypatch)
    calls = []
    assert _mock_emit(tmp_path, [], calls=calls) == []
    assert len(_mock_emit(tmp_path, ["cached"], hits=(0,), calls=calls)) == 1
    assert _pool(tmp_path, lambda *_args, **_kwargs: pytest.fail("empty dispatch"), count=0) == 0
    assert calls == sink.lines == []
    assert sink.clock_calls == 0


def test_phase_end_marks_pool_return_before_result_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    sink = _phase_sink(monkeypatch)
    with pytest.raises(FileNotFoundError):
        _mock_emit(tmp_path, ["mock"], invalid_result=True)
    assert [r["event"] for r in sink.records()] == ["START", "END"]


def test_phase_records_are_bounded_and_unknown_sizes_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PY_FRONTEND_WORKER_TIMING", "1")
    sink = _phase_sink(monkeypatch)
    _pool(tmp_path, lambda *_args, **_kwargs: None, label="untrusted-label")
    assert all(r["lane"] == "other" and r["scheduler_input_size_total"] == "-1"
               and r["scheduler_input_size_max"] == "-1" for r in sink.records())
    emit._worker_phase_diagnostic(True, "START", "emit", "private\npath", 1,
                                  10 ** 1200, 1, 0, 0, 1)
    assert all(len(line) <= 1024 and line.count("\n") == 1 for line in sink.lines)
    assert "private" not in "".join(sink.lines)


def test_pipeline_wrapper_forwards_only_diagnostic_small_sizes(monkeypatch):
    from pcc.frontends.python import pipeline

    observed = []

    def pool(*args, **kwargs):
        observed.append((args, kwargs))
        return 7

    monkeypatch.setattr(pipeline._pipeline_self_backend_emit, "run_emit_worker_pool", pool)
    assert pipeline._run_self_backend_emit_worker_pool(
        ["mock-worker"], [("result", "object", "input")], "", "directory", "small", 4,
        False, diagnostic_input_size_total=13, diagnostic_input_size_max=8,
    ) == 7
    args, kwargs = observed[0]
    assert args == (["mock-worker"], [("result", "object", "input")], "", "directory", "small", 4)
    assert kwargs["item_bytes"] is None
    assert kwargs["diagnostic_input_size_total"] == 13
    assert kwargs["diagnostic_input_size_max"] == 8
