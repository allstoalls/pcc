from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.run_self_backend_bootstrap_gate import (
    IMPORT_RUNTIME_BENCHMARKS,
    BootstrapResult,
    USER_RUNTIME_C_BASELINES,
    USER_RUNTIME_BENCHMARKS,
    _best_runtime_seconds,
    _child_env,
    _check_pcc1_compile_threshold,
    _check_performance_thresholds,
    _check_stage_elapsed_threshold,
    _check_user_runtime_threshold,
    _host_slug,
    _parse_nm_text_symbol_sizes,
    _parse_stage_elapsed_seconds,
    _parse_size_text_bytes,
    _runtime_artifact_size_cases,
    _runtime_archive_symbol_sources,
    _runtime_case_ratios,
    _source_attribution_for_top_symbols,
    _runtime_text_symbol_cases,
    _run_bootstrap,
    _supported_host,
)


def _result(
    backend: str,
    *,
    elapsed: float,
    help_elapsed: float,
    smoke_compile: float,
    smoke_run: float,
    stage_elapsed: tuple[tuple[int, float], ...] = ((1, 1.0),),
    pcc0_compile: float = 2.0,
    user_runtime: float | None = 1.0,
    python_runtime: float | None = 4.0,
    c_runtime: float | None = 0.25,
    import_runtime: float | None = 0.5,
    python_import_runtime: float | None = 2.5,
) -> BootstrapResult:
    return BootstrapResult(
        backend=backend,
        stage=3,
        out_dir="/tmp/out",
        bin_path="/tmp/out/pcc3",
        returncode=0,
        elapsed_seconds=elapsed,
        size_bytes=1,
        help_returncode=0,
        help_elapsed_seconds=help_elapsed,
        smoke_compile_returncode=0,
        smoke_compile_seconds=smoke_compile,
        smoke_run_returncode=0,
        smoke_run_seconds=smoke_run,
        benchmark_compile_times=(("case", smoke_compile),),
        benchmark_run_times=(("case", smoke_run),),
        pcc0_compile_returncode=0,
        pcc0_compile_seconds=pcc0_compile,
        pcc0_benchmark_compile_times=(("case", pcc0_compile),),
        user_runtime_returncode=0,
        user_runtime_seconds=user_runtime,
        python_runtime_seconds=python_runtime,
        c_runtime_seconds=c_runtime,
        user_runtime_times=(("typed_int_loop", user_runtime),),
        python_runtime_times=(("typed_int_loop", python_runtime),),
        c_runtime_times=(("typed_int_loop", c_runtime),),
        user_runtime_artifact_size_bytes=1000.0,
        c_runtime_artifact_size_bytes=100.0,
        user_runtime_artifact_sizes=(("typed_int_loop", 1000),),
        c_runtime_artifact_sizes=(("typed_int_loop", 100),),
        user_runtime_text_size_bytes=600.0,
        c_runtime_text_size_bytes=50.0,
        user_runtime_text_sizes=(("typed_int_loop", 600),),
        c_runtime_text_sizes=(("typed_int_loop", 50),),
        user_runtime_text_top_symbols=(("typed_int_loop", "_py_main:120,_py_print:80"),),
        user_runtime_text_top_symbol_sources=(
            ("typed_int_loop", "_py_main=>main.o(src/main.c)"),
        ),
        import_runtime_returncode=0,
        import_runtime_seconds=import_runtime,
        python_import_runtime_seconds=python_import_runtime,
        import_runtime_times=(("import_math_sqrt", import_runtime),),
        python_import_runtime_times=(("import_math_sqrt", python_import_runtime),),
        stage_elapsed_seconds=stage_elapsed,
        links_libpython=True,
        failure_hint=None,
    )


def test_self_backend_bootstrap_gate_accepts_ratios_within_threshold():
    results = [
        _result(
            "llvm",
            elapsed=10.0,
            help_elapsed=1.0,
            smoke_compile=2.0,
            smoke_run=1.0,
        ),
        _result(
            "self",
            elapsed=19.0,
            help_elapsed=1.9,
            smoke_compile=3.9,
            smoke_run=1.9,
        ),
    ]

    assert _check_performance_thresholds(
        results,
        bootstrap_threshold=2.0,
        help_threshold=2.0,
        smoke_compile_threshold=2.0,
        smoke_run_threshold=2.0,
    )


def test_self_backend_bootstrap_gate_rejects_ratio_above_threshold():
    results = [
        _result(
            "llvm",
            elapsed=10.0,
            help_elapsed=1.0,
            smoke_compile=2.0,
            smoke_run=1.0,
        ),
        _result(
            "self",
            elapsed=21.0,
            help_elapsed=1.0,
            smoke_compile=2.0,
            smoke_run=1.0,
        ),
    ]

    assert not _check_performance_thresholds(
        results,
        bootstrap_threshold=2.0,
        help_threshold=2.0,
        smoke_compile_threshold=2.0,
        smoke_run_threshold=2.0,
    )


def test_bootstrap_gate_supports_linux_x86_64(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.platform.machine",
        lambda: "x86_64",
    )

    assert _supported_host()


def test_bootstrap_gate_rejects_unsupported_linux_arch(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.platform.machine",
        lambda: "riscv64",
    )

    assert not _supported_host()


def test_bootstrap_gate_timeout_preserves_bytes_output(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["bootstrap"],
            timeout=1,
            output=b"stage output\n",
            stderr=b"stage err\n",
        )

    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.subprocess.run",
        fake_run,
    )

    result = _run_bootstrap(
        backend="self",
        stage=1,
        timeout_seconds=1,
        dry_run=False,
    )

    assert result.returncode == 124
    assert result.failure_hint == "stage err"
    assert result.stage_elapsed_seconds == ()
    assert result.pcc0_compile_seconds is None
    assert result.user_runtime_seconds is None


def test_bootstrap_gate_out_dir_is_host_qualified(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.platform.system",
        lambda: "Linux",
    )
    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.platform.machine",
        lambda: "x86_64",
    )

    assert _host_slug() == "linux_x86_64"
    result = _run_bootstrap(
        backend="self",
        stage=1,
        timeout_seconds=1,
        dry_run=True,
    )

    assert result.out_dir.endswith("build/bootstrap-self-linux_x86_64")


def test_bootstrap_gate_child_env_pins_bounded_ir_pass_policy(monkeypatch):
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)

    env = _child_env()

    assert "LC_ALL" not in env
    assert env["PCC_PYTHON_IR_PASSES"] == "default"


def test_bootstrap_gate_child_env_preserves_explicit_ir_pass_policy(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "dce")

    env = _child_env()

    assert env["PCC_PYTHON_IR_PASSES"] == "dce"


def test_bootstrap_gate_child_env_accepts_ir_pass_experiment_overrides(
    monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")

    env = _child_env(
        python_ir_passes="default",
        python_ir_pass_transport="memory",
        python_ir_pass_timeout=45.0,
        python_ir_pass_telemetry_path="/tmp/pcc-ir-pass.jsonl",
    )

    assert env["PCC_PYTHON_IR_PASSES"] == "default"
    assert env["PCC_PYTHON_IR_PASS_TRANSPORT"] == "memory"
    assert env["PCC_PYTHON_IR_PASS_TIMEOUT"] == "45.0"
    assert env["PCC_PYTHON_IR_PASS_TELEMETRY"] == "1"
    assert env["PCC_PYTHON_IR_PASS_TELEMETRY_PATH"] == "/tmp/pcc-ir-pass.jsonl"


def test_bootstrap_gate_parses_per_stage_timing_markers():
    output = """
noise
PCC_BOOTSTRAP_STAGE_RESULT stage=1 elapsed_ms=12345 output=/tmp/pcc1
PCC_BOOTSTRAP_STAGE_RESULT stage=2 elapsed_ms=6789 output=/tmp/pcc2
"""

    assert _parse_stage_elapsed_seconds(output) == (
        (1, 12.345),
        (2, 6.789),
    )


def test_bootstrap_gate_enforces_absolute_stage_target():
    results = [
        _result(
            "self",
            elapsed=20.0,
            help_elapsed=1.0,
            smoke_compile=2.0,
            smoke_run=1.0,
            stage_elapsed=((1, 29.9), (2, 30.1)),
        )
    ]

    assert not _check_stage_elapsed_threshold(
        results,
        max_stage_elapsed=30.0,
    )
    assert _check_stage_elapsed_threshold(
        results,
        max_stage_elapsed=0.0,
    )


def test_bootstrap_gate_requires_stage_timing_measurements():
    result = _result(
        "self",
        elapsed=20.0,
        help_elapsed=1.0,
        smoke_compile=2.0,
        smoke_run=1.0,
        stage_elapsed=(),
    )

    assert not _check_stage_elapsed_threshold(
        [result],
        max_stage_elapsed=30.0,
    )


def test_bootstrap_gate_requires_pcc1_to_compile_faster_than_pcc0():
    fast = _result(
        "self",
        elapsed=1.0,
        help_elapsed=1.0,
        smoke_compile=0.9,
        smoke_run=1.0,
        pcc0_compile=1.0,
    )
    slow = _result(
        "self",
        elapsed=1.0,
        help_elapsed=1.0,
        smoke_compile=1.0,
        smoke_run=1.0,
        pcc0_compile=1.0,
    )

    assert _check_pcc1_compile_threshold(
        [fast],
        max_pcc1_compile_ratio=1.0,
    )
    assert not _check_pcc1_compile_threshold(
        [slow],
        max_pcc1_compile_ratio=1.0,
    )


def test_bootstrap_gate_requires_compiled_user_code_to_beat_cpython_by_3x():
    fast = _result(
        "self",
        elapsed=1.0,
        help_elapsed=1.0,
        smoke_compile=1.0,
        smoke_run=1.0,
        user_runtime=1.0,
        python_runtime=3.1,
    )
    slow = _result(
        "self",
        elapsed=1.0,
        help_elapsed=1.0,
        smoke_compile=1.0,
        smoke_run=1.0,
        user_runtime=1.0,
        python_runtime=2.0,
    )

    assert _check_user_runtime_threshold(
        [fast],
        max_user_runtime_ratio=0.333,
    )
    assert not _check_user_runtime_threshold(
        [slow],
        max_user_runtime_ratio=0.333,
    )


def test_bootstrap_gate_requires_user_runtime_measurements_for_ratio_gate():
    missing_compiled = _result(
        "self",
        elapsed=1.0,
        help_elapsed=1.0,
        smoke_compile=1.0,
        smoke_run=1.0,
        user_runtime=None,
        python_runtime=4.0,
    )
    missing_python = _result(
        "self",
        elapsed=1.0,
        help_elapsed=1.0,
        smoke_compile=1.0,
        smoke_run=1.0,
        user_runtime=1.0,
        python_runtime=None,
    )

    assert not _check_user_runtime_threshold(
        [missing_compiled],
        max_user_runtime_ratio=0.333,
    )
    assert not _check_user_runtime_threshold(
        [missing_python],
        max_user_runtime_ratio=0.333,
    )


def test_runtime_benchmark_ignores_warmup_and_uses_best_measured_run(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="42\n",
            stderr="",
        )

    times = iter(
        [
            0.00,
            0.50,
            1.00,
            1.12,
            2.00,
            2.04,
            3.00,
            3.08,
        ]
    )

    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.subprocess.run",
        fake_run,
    )
    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.time.monotonic",
        lambda: next(times),
    )

    code, elapsed = _best_runtime_seconds(
        ["/tmp/program.out"],
        "42\n",
        timeout_seconds=60,
        warmup_runs=1,
        measured_runs=3,
    )

    assert code == 0
    assert round(elapsed or 0.0, 3) == 0.04
    assert calls == [["/tmp/program.out"]] * 4


def _typed_int_loop_expected_stdout(n: int) -> str:
    mod_cycles, mod_remainder = divmod(n, 7)
    div_quotient, div_remainder = divmod(n, 13)
    modulo_sum = mod_cycles * 21 + mod_remainder * (mod_remainder - 1) // 2
    floor_div_sum = 13 * div_quotient * (div_quotient - 1) // 2
    floor_div_sum += div_quotient * div_remainder
    return f"{modulo_sum + floor_div_sum}\n"


def _typed_branch_loop_expected_stdout(n: int) -> str:
    half = n // 2
    positive = half * (half - 1) // 2
    total = n * (n - 1) // 2
    return f"{positive - (total - positive)}\n"


def _typed_function_call_loop_expected_stdout(n: int) -> str:
    cycles, remainder = divmod(n, 7)
    per_cycle = 21 + 7 * 2
    rem_sum = remainder * (remainder - 1) // 2 + remainder * 2
    return f"{cycles * per_cycle + rem_sum}\n"


def test_user_runtime_benchmarks_cover_typed_scalar_list_branch_and_call_hot_loops() -> None:
    by_name = {name: (source, expected) for name, source, expected in USER_RUNTIME_BENCHMARKS}

    assert set(by_name) == {
        "typed_int_loop",
        "typed_float_loop",
        "typed_list_int_loop",
        "typed_branch_loop",
        "typed_function_call_loop",
    }
    assert set(USER_RUNTIME_C_BASELINES) == set(by_name)

    int_source, int_expected = by_name["typed_int_loop"]
    assert "def bench(n: int) -> int:" in int_source
    assert "while i < n:" in int_source
    assert "i % 7" in int_source
    assert "i // 13" in int_source
    assert "print(bench(5000000))" in int_source
    assert int_expected == _typed_int_loop_expected_stdout(5_000_000)
    assert "bench(5000000)" in USER_RUNTIME_C_BASELINES["typed_int_loop"]
    assert "i % 7" in USER_RUNTIME_C_BASELINES["typed_int_loop"]
    assert "i / 13" in USER_RUNTIME_C_BASELINES["typed_int_loop"]

    float_source, float_expected = by_name["typed_float_loop"]
    assert "def bench(n: int) -> float:" in float_source
    assert "acc: float" in float_source
    assert "* 2.0 / 2.0" in float_source
    assert "print(bench(500000))" in float_source
    assert float_expected == "500000.0\n"
    assert "bench(500000)" in USER_RUNTIME_C_BASELINES["typed_float_loop"]
    assert "* 2.0 / 2.0" in USER_RUNTIME_C_BASELINES["typed_float_loop"]

    list_source, list_expected = by_name["typed_list_int_loop"]
    assert "def sum_ints(xs: list[int], rounds: int) -> int:" in list_source
    assert "for x in xs:" in list_source
    assert "total = total + x" in list_source
    assert "py_list_get_i64" not in list_source
    assert list_expected == "27200000\n"
    assert "sum_ints(xs, 16, 200000)" in USER_RUNTIME_C_BASELINES["typed_list_int_loop"]

    branch_source, branch_expected = by_name["typed_branch_loop"]
    assert "def bench(n: int) -> int:" in branch_source
    assert "half: int = n // 2" in branch_source
    assert "if i < half:" in branch_source
    assert "acc = acc - i" in branch_source
    assert "print(bench(3000000))" in branch_source
    assert branch_expected == _typed_branch_loop_expected_stdout(3_000_000)
    assert "bench(3000000)" in USER_RUNTIME_C_BASELINES["typed_branch_loop"]
    assert "if (i < half)" in USER_RUNTIME_C_BASELINES["typed_branch_loop"]

    call_source, call_expected = by_name["typed_function_call_loop"]
    assert "def bump(x: int) -> int:" in call_source
    assert "def step(i: int) -> int:" in call_source
    assert "return bump(i % 7)" in call_source
    assert "total = total + step(i)" in call_source
    assert "print(bench(2100000))" in call_source
    assert call_expected == _typed_function_call_loop_expected_stdout(2_100_000)
    assert "static int64_t bump" in USER_RUNTIME_C_BASELINES["typed_function_call_loop"]
    assert "total = total + step(i)" in USER_RUNTIME_C_BASELINES["typed_function_call_loop"]


def test_import_runtime_benchmarks_cover_native_import_latency_case() -> None:
    by_name = {name: (source, expected) for name, source, expected in IMPORT_RUNTIME_BENCHMARKS}

    assert set(by_name) == {
        "import_math_sqrt",
        "import_sys_platform",
        "from_os_import_path",
        "import_json_roundtrip",
    }
    source, expected = by_name["import_math_sqrt"]
    assert "import math" in source
    assert "math.sqrt(81.0)" in source
    assert expected == "9\n"

    source, expected = by_name["import_sys_platform"]
    assert "import sys" in source
    assert "sys.platform == sys.platform" in source
    assert expected == "True\n"

    source, expected = by_name["from_os_import_path"]
    assert "from os import path" in source
    assert "path.join" in source
    assert "path.basename" in source
    assert expected == "a/b\nfoo.txt\n"

    source, expected = by_name["import_json_roundtrip"]
    assert "import json" in source
    assert "json.loads" in source
    assert "json.dumps" in source
    assert expected == '1 2\n{"x": 10}\n'


def test_runtime_case_ratios_reports_each_user_runtime_case() -> None:
    text = _runtime_case_ratios(
        (
            ("typed_int_loop", 0.1),
            ("typed_float_loop", 0.2),
        ),
        (
            ("typed_int_loop", 1.0),
            ("typed_float_loop", 0.5),
            ("typed_list_int_loop", 0.3),
        ),
        (
            ("typed_int_loop", 0.01),
            ("typed_float_loop", 0.05),
            ("typed_list_int_loop", 0.02),
        ),
    )

    assert "typed_int_loop:pcc=0.100s,python=1.000s,ratio=0.100" in text
    assert "typed_int_loop:pcc=0.100s,python=1.000s,ratio=0.100,c=0.010s,pcc_vs_c=10.000" in text
    assert "typed_float_loop:pcc=0.200s,python=0.500s,ratio=0.400" in text
    assert "typed_list_int_loop:pcc=n/a,python=0.300s,ratio=n/a,c=0.020s,pcc_vs_c=n/a" in text


def test_runtime_artifact_size_cases_reports_pcc_and_c_sizes() -> None:
    text = _runtime_artifact_size_cases(
        (
            ("typed_int_loop", 1000),
            ("typed_float_loop", 2000),
        ),
        (
            ("typed_int_loop", 100),
            ("typed_float_loop", 500),
            ("typed_list_int_loop", 300),
        ),
    )

    assert "typed_int_loop:pcc=1000,c=100,ratio=10.000" in text
    assert "typed_float_loop:pcc=2000,c=500,ratio=4.000" in text
    assert "typed_list_int_loop:pcc=n/a,c=300,ratio=n/a" in text


def test_parse_size_text_bytes_supports_macho_and_gnu_size_output() -> None:
    macho = (
        "__TEXT\t__DATA\t__OBJC\tothers\tdec\thex\n"
        "32768\t16384\t0\t0\t49152\tc000\n"
    )
    gnu = (
        "   text    data     bss     dec     hex filename\n"
        "   1234     567      89    1890     762 program\n"
    )

    assert _parse_size_text_bytes(macho) == 32768
    assert _parse_size_text_bytes(gnu) == 1234
    assert _parse_size_text_bytes("no size table here\n") is None


def test_parse_nm_text_symbol_sizes_supports_macho_and_gnu_output() -> None:
    macho = (
        "0000000100000000 (__TEXT,__text) external __mh_execute_header\n"
        "0000000100001000 (__TEXT,__text) external _main\n"
        "0000000100001040 (__TEXT,__text) external _py_print\n"
        "00000001000010a0 (__TEXT,__text) external _py_exit\n"
        "00000001000010b0 (__TEXT,__const) external _not_text\n"
    )
    gnu = (
        "0000000000001000 0000000000000040 T main\n"
        "0000000000001040 0000000000000060 t py_print\n"
        "00000000000010a0 0000000000000010 D not_text\n"
    )

    assert _parse_nm_text_symbol_sizes(macho) == (
        ("_main", 64),
        ("_py_print", 96),
    )
    assert _parse_nm_text_symbol_sizes(gnu) == (
        ("main", 64),
        ("py_print", 96),
    )


def test_runtime_text_symbol_cases_reports_per_case_top_symbols() -> None:
    text = _runtime_text_symbol_cases(
        (
            ("typed_int_loop", "_py_print:120,_main:64"),
            ("typed_float_loop", "_py_float:88"),
        )
    )

    assert "typed_int_loop:_py_print:120,_main:64" in text
    assert "typed_float_loop:_py_float:88" in text


def test_source_attribution_for_top_symbols_uses_runtime_archive_sources() -> None:
    sources = {
        "_pcc_gc_telemetry": "py_gc_backend.o(pcc/py_runtime/py/py_gc_backend.py)",
        "_py_str_mod": "py_format.o(pcc/py_runtime/src/py_format.c)",
    }

    text = _source_attribution_for_top_symbols(
        "_pcc_gc_telemetry:3184,_py_str_mod:2900,_missing:1",
        sources,
    )

    assert "_pcc_gc_telemetry=>py_gc_backend.o(pcc/py_runtime/py/py_gc_backend.py)" in text
    assert "_py_str_mod=>py_format.o(pcc/py_runtime/src/py_format.c)" in text
    assert "_missing=>unknown" in text


def test_runtime_archive_symbol_sources_parses_nm_archive_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "pcc/py_runtime"
    runtime.mkdir(parents=True)
    archive = runtime / "libpy_runtime_pcc_py.a"
    archive.write_text("", encoding="utf-8")
    nm_output = (
        f"{archive}:py_gc_backend.o: 00000000000012e8 T _pcc_gc_telemetry\n"
        f"{archive}:py_format.o: 00000000000007ec T _py_str_mod\n"
        f"{archive}:py_format.o:                  U _missing\n"
    )

    def fake_run(cmd, capture_output=False, text=False, timeout=None):
        assert cmd == ["nm", "-A", str(archive)]
        return subprocess.CompletedProcess(cmd, 0, stdout=nm_output, stderr="")

    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate.subprocess.run",
        fake_run,
    )
    monkeypatch.setattr(
        "scripts.run_self_backend_bootstrap_gate._repo_root",
        lambda: str(tmp_path),
    )
    (runtime / "py").mkdir(parents=True)
    (runtime / "src").mkdir(parents=True)
    (runtime / "py/py_gc_backend.py").write_text("", encoding="utf-8")
    (runtime / "src/py_format.c").write_text("", encoding="utf-8")

    sources = _runtime_archive_symbol_sources(str(archive))

    assert sources["_pcc_gc_telemetry"] == "py_gc_backend.o(pcc/py_runtime/py/py_gc_backend.py)"
    assert sources["_py_str_mod"] == "py_format.o(pcc/py_runtime/src/py_format.c)"
    assert "_missing" not in sources
