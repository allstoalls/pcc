#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

SMOKE_BENCHMARKS: tuple[tuple[str, str, str], ...] = (
    ("print_int", "print(123)\n", "123\n"),
    (
        "function_call",
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n\n"
        "print(add(10, 32))\n",
        "42\n",
    ),
    (
        "two_functions",
        "def mul(a: int, b: int) -> int:\n"
        "    return a * b\n\n"
        "def main() -> None:\n"
        "    print(mul(6, 7))\n\n"
        "main()\n",
        "42\n",
    ),
)

IMPORT_RUNTIME_BENCHMARKS: tuple[tuple[str, str, str], ...] = (
    (
        "import_math_sqrt",
        "import math\n"
        "print(int(math.sqrt(81.0)))\n",
        "9\n",
    ),
    (
        "import_sys_platform",
        "import sys\n"
        "print(sys.platform == sys.platform)\n",
        "True\n",
    ),
    (
        "from_os_import_path",
        "from os import path\n"
        "print(path.join(\"a\", \"b\"))\n"
        "print(path.basename(\"/tmp/foo.txt\"))\n",
        "a/b\nfoo.txt\n",
    ),
    (
        "import_json_roundtrip",
        "import json\n"
        "d = json.loads('{\"a\": 1, \"b\": 2}')\n"
        "print(d[\"a\"], d[\"b\"])\n"
        "print(json.dumps({\"x\": 10}))\n",
        "1 2\n{\"x\": 10}\n",
    ),
)

USER_RUNTIME_BENCHMARKS: tuple[tuple[str, str, str], ...] = (
    (
        "typed_int_loop",
        "def bench(n: int) -> int:\n"
        "    acc: int = 0\n"
        "    i: int = 0\n"
        "    while i < n:\n"
        "        acc = acc + (i % 7) + (i // 13)\n"
        "        i = i + 1\n"
        "    return acc\n\n"
        "print(bench(5000000))\n",
        "961550961535\n",
    ),
    (
        "typed_float_loop",
        "def bench(n: int) -> float:\n"
        "    acc: float = 0.0\n"
        "    i: int = 0\n"
        "    while i < n:\n"
        "        acc = (acc + 1.0) * 2.0 / 2.0\n"
        "        i = i + 1\n"
        "    return acc\n\n"
        "print(bench(500000))\n",
        "500000.0\n",
    ),
    (
        "typed_list_int_loop",
        "def sum_ints(xs: list[int], rounds: int) -> int:\n"
        "    total: int = 0\n"
        "    r: int = 0\n"
        "    while r < rounds:\n"
        "        for x in xs:\n"
        "            total = total + x\n"
        "        r = r + 1\n"
        "    return total\n\n"
        "print(sum_ints([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], 200000))\n",
        "27200000\n",
    ),
    (
        "typed_branch_loop",
        "def bench(n: int) -> int:\n"
        "    acc: int = 0\n"
        "    i: int = 0\n"
        "    half: int = n // 2\n"
        "    while i < n:\n"
        "        if i < half:\n"
        "            acc = acc + i\n"
        "        else:\n"
        "            acc = acc - i\n"
        "        i = i + 1\n"
        "    return acc\n\n"
        "print(bench(3000000))\n",
        "-2250000000000\n",
    ),
    (
        "typed_function_call_loop",
        "def bump(x: int) -> int:\n"
        "    return x + 2\n\n"
        "def step(i: int) -> int:\n"
        "    return bump(i % 7)\n\n"
        "def bench(n: int) -> int:\n"
        "    total: int = 0\n"
        "    i: int = 0\n"
        "    while i < n:\n"
        "        total = total + step(i)\n"
        "        i = i + 1\n"
        "    return total\n\n"
        "print(bench(2100000))\n",
        "10500000\n",
    ),
)

USER_RUNTIME_C_BASELINES: dict[str, str] = {
    "typed_int_loop": (
        "#include <stdint.h>\n"
        "#include <stdio.h>\n"
        "static int64_t bench(int64_t n) {\n"
        "    int64_t acc = 0;\n"
        "    int64_t i = 0;\n"
        "    while (i < n) {\n"
        "        acc = acc + (i % 7) + (i / 13);\n"
        "        i = i + 1;\n"
        "    }\n"
        "    return acc;\n"
        "}\n"
        "int main(void) { printf(\"%lld\\n\", (long long)bench(5000000)); return 0; }\n"
    ),
    "typed_float_loop": (
        "#include <stdio.h>\n"
        "static double bench(long long n) {\n"
        "    double acc = 0.0;\n"
        "    long long i = 0;\n"
        "    while (i < n) {\n"
        "        acc = (acc + 1.0) * 2.0 / 2.0;\n"
        "        i = i + 1;\n"
        "    }\n"
        "    return acc;\n"
        "}\n"
        "int main(void) { printf(\"%.1f\\n\", bench(500000)); return 0; }\n"
    ),
    "typed_list_int_loop": (
        "#include <stdint.h>\n"
        "#include <stdio.h>\n"
        "static int64_t sum_ints(const int64_t *xs, int64_t n, int64_t rounds) {\n"
        "    int64_t total = 0;\n"
        "    int64_t r = 0;\n"
        "    while (r < rounds) {\n"
        "        for (int64_t i = 0; i < n; i++) {\n"
        "            total = total + xs[i];\n"
        "        }\n"
        "        r = r + 1;\n"
        "    }\n"
        "    return total;\n"
        "}\n"
        "int main(void) {\n"
        "    const int64_t xs[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};\n"
        "    printf(\"%lld\\n\", (long long)sum_ints(xs, 16, 200000));\n"
        "    return 0;\n"
        "}\n"
    ),
    "typed_branch_loop": (
        "#include <stdint.h>\n"
        "#include <stdio.h>\n"
        "static int64_t bench(int64_t n) {\n"
        "    int64_t acc = 0;\n"
        "    int64_t i = 0;\n"
        "    int64_t half = n / 2;\n"
        "    while (i < n) {\n"
        "        if (i < half) {\n"
        "            acc = acc + i;\n"
        "        } else {\n"
        "            acc = acc - i;\n"
        "        }\n"
        "        i = i + 1;\n"
        "    }\n"
        "    return acc;\n"
        "}\n"
        "int main(void) { printf(\"%lld\\n\", (long long)bench(3000000)); return 0; }\n"
    ),
    "typed_function_call_loop": (
        "#include <stdint.h>\n"
        "#include <stdio.h>\n"
        "static int64_t bump(int64_t x) { return x + 2; }\n"
        "static int64_t step(int64_t i) { return bump(i % 7); }\n"
        "static int64_t bench(int64_t n) {\n"
        "    int64_t total = 0;\n"
        "    int64_t i = 0;\n"
        "    while (i < n) {\n"
        "        total = total + step(i);\n"
        "        i = i + 1;\n"
        "    }\n"
        "    return total;\n"
        "}\n"
        "int main(void) { printf(\"%lld\\n\", (long long)bench(2100000)); return 0; }\n"
    ),
}

USER_RUNTIME_WARMUP_RUNS = 1
USER_RUNTIME_MEASURED_RUNS = 3


@dataclass(frozen=True)
class BootstrapResult:
    backend: str
    stage: int
    out_dir: str
    bin_path: str
    returncode: int
    elapsed_seconds: float
    size_bytes: int | None
    help_returncode: int | None
    help_elapsed_seconds: float | None
    smoke_compile_returncode: int | None
    smoke_compile_seconds: float | None
    smoke_run_returncode: int | None
    smoke_run_seconds: float | None
    benchmark_compile_times: tuple[tuple[str, float], ...]
    benchmark_run_times: tuple[tuple[str, float], ...]
    pcc0_compile_returncode: int | None
    pcc0_compile_seconds: float | None
    pcc0_benchmark_compile_times: tuple[tuple[str, float], ...]
    user_runtime_returncode: int | None
    user_runtime_seconds: float | None
    python_runtime_seconds: float | None
    c_runtime_seconds: float | None
    user_runtime_times: tuple[tuple[str, float], ...]
    python_runtime_times: tuple[tuple[str, float], ...]
    c_runtime_times: tuple[tuple[str, float], ...]
    user_runtime_artifact_size_bytes: float | None
    c_runtime_artifact_size_bytes: float | None
    user_runtime_artifact_sizes: tuple[tuple[str, int], ...]
    c_runtime_artifact_sizes: tuple[tuple[str, int], ...]
    user_runtime_text_size_bytes: float | None
    c_runtime_text_size_bytes: float | None
    user_runtime_text_sizes: tuple[tuple[str, int], ...]
    c_runtime_text_sizes: tuple[tuple[str, int], ...]
    user_runtime_text_top_symbols: tuple[tuple[str, str], ...]
    user_runtime_text_top_symbol_sources: tuple[tuple[str, str], ...]
    import_runtime_returncode: int | None
    import_runtime_seconds: float | None
    python_import_runtime_seconds: float | None
    import_runtime_times: tuple[tuple[str, float], ...]
    python_import_runtime_times: tuple[tuple[str, float], ...]
    stage_elapsed_seconds: tuple[tuple[int, float], ...]
    links_libpython: bool | None
    failure_hint: str | None


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _supported_host() -> bool:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        return True
    if sys.platform.startswith("linux") and machine in {
        "x86_64",
        "amd64",
    }:
        return True
    return False


def _host_slug() -> str:
    system = platform.system().lower() or sys.platform.lower()
    machine = platform.machine().lower() or "unknown"
    return re.sub(r"[^a-z0-9_]+", "_", f"{system}_{machine}")


def _child_env(
    *,
    python_ir_passes: str | None = None,
    python_ir_pass_transport: str | None = None,
    python_ir_pass_timeout: float | None = None,
    python_ir_pass_telemetry_path: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    if python_ir_passes is None:
        env.setdefault("PCC_PYTHON_IR_PASSES", "default")
    else:
        env["PCC_PYTHON_IR_PASSES"] = str(python_ir_passes)
    if python_ir_pass_transport is not None:
        env["PCC_PYTHON_IR_PASS_TRANSPORT"] = str(python_ir_pass_transport)
    if python_ir_pass_timeout is not None:
        env["PCC_PYTHON_IR_PASS_TIMEOUT"] = str(float(python_ir_pass_timeout))
    if python_ir_pass_telemetry_path is not None:
        env["PCC_PYTHON_IR_PASS_TELEMETRY"] = "1"
        env["PCC_PYTHON_IR_PASS_TELEMETRY_PATH"] = str(python_ir_pass_telemetry_path)
    return env


def _stage_bin(out_dir: str, stage: int) -> str:
    return os.path.join(out_dir, f"pcc{stage}")


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _parse_size_text_bytes(output: str) -> int | None:
    lines = [line.split() for line in output.splitlines() if line.split()]
    for index, parts in enumerate(lines[:-1]):
        header = [part.lower() for part in parts]
        values = lines[index + 1]
        if "__text" in header:
            text_index = header.index("__text")
        elif "text" in header:
            text_index = header.index("text")
        else:
            continue
        if text_index >= len(values):
            continue
        try:
            return int(values[text_index], 0)
        except ValueError:
            continue
    return None


def _artifact_text_size(path: str) -> int | None:
    if not os.path.exists(path):
        return None
    try:
        result = subprocess.run(
            ["size", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_size_text_bytes(result.stdout)


def _parse_nm_text_symbol_sizes(output: str) -> tuple[tuple[str, int], ...]:
    macho_entries: list[tuple[int, str]] = []
    sized_entries: list[tuple[str, int]] = []
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        if "(__TEXT,__text)" in line and len(parts) >= 4:
            try:
                addr = int(parts[0], 16)
            except ValueError:
                continue
            name = parts[-1]
            if name == "__mh_execute_header":
                continue
            macho_entries.append((addr, name))
            continue
        if len(parts) >= 4 and parts[2] in {"t", "T"}:
            try:
                size = int(parts[1], 16)
            except ValueError:
                continue
            if size > 0:
                sized_entries.append((parts[3], size))
    if sized_entries:
        return tuple(sized_entries)
    if not macho_entries:
        return ()
    by_addr: dict[int, str] = {}
    for addr, name in macho_entries:
        if addr not in by_addr:
            by_addr[addr] = name
    ordered = sorted(by_addr.items())
    inferred: list[tuple[str, int]] = []
    for index, (addr, name) in enumerate(ordered[:-1]):
        next_addr = ordered[index + 1][0]
        size = next_addr - addr
        if size > 0:
            inferred.append((name, size))
    return tuple(inferred)


def _format_text_top_symbols(
    symbols: tuple[tuple[str, int], ...],
    limit: int = 5,
) -> str:
    if not symbols:
        return "n/a"
    top = sorted(symbols, key=lambda item: item[1], reverse=True)[:limit]
    return ",".join(f"{name}:{size}" for name, size in top)


def _artifact_text_top_symbols(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    cmd = ["nm", "-m", path] if sys.platform == "darwin" else ["nm", "-S", path]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _format_text_top_symbols(_parse_nm_text_symbol_sizes(result.stdout))


def _runtime_member_source(archive: str, member: str) -> str:
    stem = member[:-2] if member.endswith(".o") else member
    root = os.path.dirname(str(archive))
    py_source = os.path.join(root, "py", stem + ".py")
    c_source = os.path.join(root, "src", stem + ".c")
    if "_pcc_py" in os.path.basename(str(archive)) and os.path.exists(py_source):
        return os.path.relpath(py_source, _repo_root())
    if os.path.exists(c_source):
        return os.path.relpath(c_source, _repo_root())
    if os.path.exists(py_source):
        return os.path.relpath(py_source, _repo_root())
    return member


def _runtime_archive_symbol_sources(archive: str) -> dict[str, str]:
    if not os.path.exists(archive):
        return {}
    try:
        result = subprocess.run(
            ["nm", "-A", archive],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    sources: dict[str, str] = {}
    archive_prefix = str(archive) + ":"
    for line in result.stdout.splitlines():
        if not line.startswith(archive_prefix):
            continue
        rest = line[len(archive_prefix) :]
        member, sep, body = rest.partition(":")
        if not sep:
            continue
        parts = body.split()
        if not parts or "(undefined)" in body:
            continue
        if parts[0] in {"U", "u"}:
            continue
        symbol = parts[-1]
        if symbol not in sources:
            source = _runtime_member_source(archive, member)
            sources[symbol] = member + "(" + source + ")"
    return sources


def _runtime_archive_for_symbol_sources() -> str:
    runtime_dir = os.path.join(_repo_root(), "pcc", "py_runtime")
    preferred = os.path.join(runtime_dir, "libpy_runtime_pcc_py.a")
    if os.path.exists(preferred):
        return preferred
    return os.path.join(runtime_dir, "libpy_runtime.a")


def _source_attribution_for_top_symbols(
    top_symbols: str,
    symbol_sources: dict[str, str],
) -> str:
    if top_symbols == "n/a":
        return "n/a"
    parts: list[str] = []
    for item in top_symbols.split(","):
        name = item.split(":", 1)[0]
        source = symbol_sources.get(name)
        if source is not None:
            parts.append(name + "=>" + source)
        else:
            parts.append(name + "=>unknown")
    return ",".join(parts) if parts else "n/a"


def _links_libpython(path: str) -> bool | None:
    if not os.path.exists(path):
        return None
    if sys.platform == "darwin":
        cmd = ["otool", "-L", path]
    else:
        cmd = ["ldd", path]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "") + (result.stderr or "")
    return "libpython" in text or "Python.framework" in text


def _help_smoke(path: str) -> tuple[int | None, float | None]:
    if not os.path.exists(path):
        return None, None
    try:
        start = time.monotonic()
        result = subprocess.run(
            [path, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        elapsed = time.monotonic() - start
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    return result.returncode, elapsed


def _geomean(values: list[float]) -> float | None:
    if not values:
        return None
    product = 1.0
    for value in values:
        if value <= 0:
            continue
        product *= value
    return product ** (1.0 / len(values))


def _pcc0_command() -> list[str]:
    if os.path.exists(os.path.join(_repo_root(), "pyproject.toml")):
        return ["uv", "run", "python", "-m", "pcc"]
    return [sys.executable, "-m", "pcc"]


def _benchmark_smoke_command(
    command_prefix: list[str],
    backend: str,
    *,
    temp_prefix: str,
) -> tuple[
    int | None,
    float | None,
    int | None,
    float | None,
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
]:
    with tempfile.TemporaryDirectory(
        prefix=temp_prefix,
    ) as tmp:
        compile_times: list[tuple[str, float]] = []
        run_times: list[tuple[str, float]] = []
        for name, source, expected_stdout in SMOKE_BENCHMARKS:
            src = os.path.join(tmp, name + ".py")
            out = os.path.join(tmp, name + ".out")
            with open(src, "w", encoding="utf-8") as f:
                f.write(source)
            compile_cmd = [*command_prefix, "--backend", backend, src, "-o", out]
            try:
                start = time.monotonic()
                build = subprocess.run(
                    compile_cmd,
                    cwd=_repo_root(),
                    env=_child_env(),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                compile_elapsed = time.monotonic() - start
            except (OSError, subprocess.TimeoutExpired):
                return None, None, None, None, tuple(compile_times), tuple(run_times)
            compile_times.append((name, compile_elapsed))
            if build.returncode != 0:
                return (
                    build.returncode,
                    _geomean([value for _name, value in compile_times]),
                    None,
                    None,
                    tuple(compile_times),
                    tuple(run_times),
                )
            try:
                start = time.monotonic()
                run = subprocess.run(
                    [out],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                run_elapsed = time.monotonic() - start
            except (OSError, subprocess.TimeoutExpired):
                return (
                    build.returncode,
                    _geomean([value for _name, value in compile_times]),
                    None,
                    None,
                    tuple(compile_times),
                    tuple(run_times),
                )
            run_times.append((name, run_elapsed))
            if run.returncode != 0 or run.stdout != expected_stdout:
                return (
                    build.returncode,
                    _geomean([value for _name, value in compile_times]),
                    1 if run.returncode == 0 else run.returncode,
                    _geomean([value for _name, value in run_times]),
                    tuple(compile_times),
                    tuple(run_times),
                )
        return (
            0,
            _geomean([value for _name, value in compile_times]),
            0,
            _geomean([value for _name, value in run_times]),
            tuple(compile_times),
            tuple(run_times),
        )


def _benchmark_smoke(
    path: str,
    backend: str,
) -> tuple[
    int | None,
    float | None,
    int | None,
    float | None,
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
]:
    if not os.path.exists(path):
        return None, None, None, None, (), ()
    return _benchmark_smoke_command(
        [path],
        backend,
        temp_prefix=f"pcc_bootstrap_{backend}_smoke_",
    )


def _benchmark_pcc0_compile(
    backend: str,
) -> tuple[int | None, float | None, tuple[tuple[str, float], ...]]:
    (
        compile_code,
        compile_elapsed,
        _run_code,
        _run_elapsed,
        compile_times,
        _run_times,
    ) = _benchmark_smoke_command(
        _pcc0_command(),
        backend,
        temp_prefix=f"pcc_bootstrap_pcc0_{backend}_smoke_",
    )
    return compile_code, compile_elapsed, compile_times


def _best_runtime_seconds(
    command: list[str],
    expected_stdout: str,
    *,
    timeout_seconds: int,
    warmup_runs: int = USER_RUNTIME_WARMUP_RUNS,
    measured_runs: int = USER_RUNTIME_MEASURED_RUNS,
) -> tuple[int | None, float | None]:
    best: float | None = None
    total_runs = warmup_runs + measured_runs
    for run_index in range(total_runs):
        try:
            start = time.monotonic()
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            elapsed = time.monotonic() - start
        except (OSError, subprocess.TimeoutExpired):
            return None, best
        if result.returncode != 0 or result.stdout != expected_stdout:
            code = 1 if result.returncode == 0 else result.returncode
            return code, best
        if run_index < warmup_runs:
            continue
        if best is None or elapsed < best:
            best = elapsed
    return 0, best


def _benchmark_user_runtime(
    path: str,
    backend: str,
) -> tuple[
    int | None,
    float | None,
    float | None,
    float | None,
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    float | None,
    float | None,
    tuple[tuple[str, int], ...],
    tuple[tuple[str, int], ...],
    float | None,
    float | None,
    tuple[tuple[str, int], ...],
    tuple[tuple[str, int], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
]:
    if not os.path.exists(path):
        return None, None, None, None, (), (), (), None, None, (), (), None, None, (), (), (), ()
    with tempfile.TemporaryDirectory(
        prefix=f"pcc_bootstrap_{backend}_runtime_",
    ) as tmp:
        pcc_times: list[tuple[str, float]] = []
        python_times: list[tuple[str, float]] = []
        c_times: list[tuple[str, float]] = []
        pcc_sizes: list[tuple[str, int]] = []
        c_sizes: list[tuple[str, int]] = []
        pcc_text_sizes: list[tuple[str, int]] = []
        c_text_sizes: list[tuple[str, int]] = []
        pcc_text_top_symbols: list[tuple[str, str]] = []
        pcc_text_top_symbol_sources: list[tuple[str, str]] = []
        symbol_sources = _runtime_archive_symbol_sources(
            _runtime_archive_for_symbol_sources()
        )
        for name, source, expected_stdout in USER_RUNTIME_BENCHMARKS:
            src = os.path.join(tmp, name + ".py")
            out = os.path.join(tmp, name + ".out")
            with open(src, "w", encoding="utf-8") as f:
                f.write(source)
            compile_cmd = [path, "--backend", backend, src, "-o", out]
            try:
                build = subprocess.run(
                    compile_cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None, None, None, None, tuple(pcc_times), tuple(python_times), tuple(c_times), _geomean([value for _name, value in pcc_sizes]), _geomean([value for _name, value in c_sizes]), tuple(pcc_sizes), tuple(c_sizes), _geomean([value for _name, value in pcc_text_sizes]), _geomean([value for _name, value in c_text_sizes]), tuple(pcc_text_sizes), tuple(c_text_sizes), tuple(pcc_text_top_symbols), tuple(pcc_text_top_symbol_sources)
            if build.returncode != 0:
                return (
                    build.returncode,
                    None,
                    None,
                    None,
                    tuple(pcc_times),
                    tuple(python_times),
                    tuple(c_times),
                    _geomean([value for _name, value in pcc_sizes]),
                    _geomean([value for _name, value in c_sizes]),
                    tuple(pcc_sizes),
                    tuple(c_sizes),
                    _geomean([value for _name, value in pcc_text_sizes]),
                    _geomean([value for _name, value in c_text_sizes]),
                    tuple(pcc_text_sizes),
                    tuple(c_text_sizes), tuple(pcc_text_top_symbols), tuple(pcc_text_top_symbol_sources),
                )
            pcc_size = _file_size(out)
            if pcc_size is not None:
                pcc_sizes.append((name, pcc_size))
            pcc_text_size = _artifact_text_size(out)
            if pcc_text_size is not None:
                pcc_text_sizes.append((name, pcc_text_size))
            pcc_top_symbols = _artifact_text_top_symbols(out)
            if pcc_top_symbols is not None:
                pcc_text_top_symbols.append((name, pcc_top_symbols))
                pcc_text_top_symbol_sources.append(
                    (
                        name,
                        _source_attribution_for_top_symbols(
                            pcc_top_symbols,
                            symbol_sources,
                        ),
                    )
                )
            pcc_code, pcc_elapsed = _best_runtime_seconds(
                [out],
                expected_stdout,
                timeout_seconds=60,
            )
            if pcc_elapsed is not None:
                pcc_times.append((name, pcc_elapsed))
            if pcc_code is None:
                return None, None, None, None, tuple(pcc_times), tuple(python_times), tuple(c_times), _geomean([value for _name, value in pcc_sizes]), _geomean([value for _name, value in c_sizes]), tuple(pcc_sizes), tuple(c_sizes), _geomean([value for _name, value in pcc_text_sizes]), _geomean([value for _name, value in c_text_sizes]), tuple(pcc_text_sizes), tuple(c_text_sizes), tuple(pcc_text_top_symbols), tuple(pcc_text_top_symbol_sources)
            if pcc_code != 0:
                return (
                    pcc_code,
                    _geomean([value for _name, value in pcc_times]),
                    None,
                    _geomean([value for _name, value in c_times]),
                    tuple(pcc_times),
                    tuple(python_times),
                    tuple(c_times),
                    _geomean([value for _name, value in pcc_sizes]),
                    _geomean([value for _name, value in c_sizes]),
                    tuple(pcc_sizes),
                    tuple(c_sizes),
                    _geomean([value for _name, value in pcc_text_sizes]),
                    _geomean([value for _name, value in c_text_sizes]),
                    tuple(pcc_text_sizes),
                    tuple(c_text_sizes), tuple(pcc_text_top_symbols), tuple(pcc_text_top_symbol_sources),
                )
            py_code, py_elapsed = _best_runtime_seconds(
                [sys.executable, src],
                expected_stdout,
                timeout_seconds=60,
            )
            if py_elapsed is not None:
                python_times.append((name, py_elapsed))
            if py_code is None:
                return (
                    None,
                    _geomean([value for _name, value in pcc_times]),
                    None,
                    _geomean([value for _name, value in c_times]),
                    tuple(pcc_times),
                    tuple(python_times),
                    tuple(c_times),
                    _geomean([value for _name, value in pcc_sizes]),
                    _geomean([value for _name, value in c_sizes]),
                    tuple(pcc_sizes),
                    tuple(c_sizes),
                    _geomean([value for _name, value in pcc_text_sizes]),
                    _geomean([value for _name, value in c_text_sizes]),
                    tuple(pcc_text_sizes),
                    tuple(c_text_sizes), tuple(pcc_text_top_symbols), tuple(pcc_text_top_symbol_sources),
                )
            if py_code != 0:
                return (
                    py_code,
                    _geomean([value for _name, value in pcc_times]),
                    _geomean([value for _name, value in python_times]),
                    _geomean([value for _name, value in c_times]),
                    tuple(pcc_times),
                    tuple(python_times),
                    tuple(c_times),
                    _geomean([value for _name, value in pcc_sizes]),
                    _geomean([value for _name, value in c_sizes]),
                    tuple(pcc_sizes),
                    tuple(c_sizes),
                    _geomean([value for _name, value in pcc_text_sizes]),
                    _geomean([value for _name, value in c_text_sizes]),
                    tuple(pcc_text_sizes),
                    tuple(c_text_sizes), tuple(pcc_text_top_symbols), tuple(pcc_text_top_symbol_sources),
                )
            c_source = USER_RUNTIME_C_BASELINES.get(name)
            if c_source is not None:
                c_src = os.path.join(tmp, name + ".c")
                c_out = os.path.join(tmp, name + ".c.out")
                with open(c_src, "w", encoding="utf-8") as f:
                    f.write(c_source)
                cc = os.environ.get("CC", "cc")
                try:
                    c_build = subprocess.run(
                        [cc, "-O2", "-std=c99", c_src, "-o", c_out],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    c_build = None
                if c_build is not None and c_build.returncode == 0:
                    c_size = _file_size(c_out)
                    if c_size is not None:
                        c_sizes.append((name, c_size))
                    c_text_size = _artifact_text_size(c_out)
                    if c_text_size is not None:
                        c_text_sizes.append((name, c_text_size))
                    c_code, c_elapsed = _best_runtime_seconds(
                        [c_out],
                        expected_stdout,
                        timeout_seconds=60,
                    )
                    if c_code == 0 and c_elapsed is not None:
                        c_times.append((name, c_elapsed))
        return (
            0,
            _geomean([value for _name, value in pcc_times]),
            _geomean([value for _name, value in python_times]),
            _geomean([value for _name, value in c_times]),
            tuple(pcc_times),
            tuple(python_times),
            tuple(c_times),
            _geomean([value for _name, value in pcc_sizes]),
            _geomean([value for _name, value in c_sizes]),
            tuple(pcc_sizes),
            tuple(c_sizes),
            _geomean([value for _name, value in pcc_text_sizes]),
            _geomean([value for _name, value in c_text_sizes]),
            tuple(pcc_text_sizes),
            tuple(c_text_sizes), tuple(pcc_text_top_symbols), tuple(pcc_text_top_symbol_sources),
        )


def _benchmark_import_runtime(
    path: str,
    backend: str,
) -> tuple[
    int | None,
    float | None,
    float | None,
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
]:
    if not os.path.exists(path):
        return None, None, None, (), ()
    with tempfile.TemporaryDirectory(
        prefix=f"pcc_bootstrap_{backend}_import_runtime_",
    ) as tmp:
        pcc_times: list[tuple[str, float]] = []
        python_times: list[tuple[str, float]] = []
        for name, source, expected_stdout in IMPORT_RUNTIME_BENCHMARKS:
            src = os.path.join(tmp, name + ".py")
            out = os.path.join(tmp, name + ".out")
            with open(src, "w", encoding="utf-8") as f:
                f.write(source)
            compile_cmd = [path, "--backend", backend, src, "-o", out]
            try:
                build = subprocess.run(
                    compile_cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None, None, None, tuple(pcc_times), tuple(python_times)
            if build.returncode != 0:
                return (
                    build.returncode,
                    None,
                    None,
                    tuple(pcc_times),
                    tuple(python_times),
                )
            pcc_code, pcc_elapsed = _best_runtime_seconds(
                [out],
                expected_stdout,
                timeout_seconds=30,
            )
            if pcc_elapsed is not None:
                pcc_times.append((name, pcc_elapsed))
            if pcc_code is None:
                return None, None, None, tuple(pcc_times), tuple(python_times)
            if pcc_code != 0:
                return (
                    pcc_code,
                    _geomean([value for _name, value in pcc_times]),
                    None,
                    tuple(pcc_times),
                    tuple(python_times),
                )
            py_code, py_elapsed = _best_runtime_seconds(
                [sys.executable, src],
                expected_stdout,
                timeout_seconds=30,
            )
            if py_elapsed is not None:
                python_times.append((name, py_elapsed))
            if py_code is None:
                return (
                    None,
                    _geomean([value for _name, value in pcc_times]),
                    None,
                    tuple(pcc_times),
                    tuple(python_times),
                )
            if py_code != 0:
                return (
                    py_code,
                    _geomean([value for _name, value in pcc_times]),
                    _geomean([value for _name, value in python_times]),
                    tuple(pcc_times),
                    tuple(python_times),
                )
        return (
            0,
            _geomean([value for _name, value in pcc_times]),
            _geomean([value for _name, value in python_times]),
            tuple(pcc_times),
            tuple(python_times),
        )


def _failure_hint(text: str) -> str | None:
    patterns = (
        r"undefined symbols?[^\n]*(?:\n[^\n]*)?",
        r"Undefined symbols?[^\n]*(?:\n[^\n]*)?",
        r"self backend[^\n]*",
        r"unsupported[^\n]*",
        r"PyPipelineError[^\n]*",
        r"Error: [^\n]*",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        return lines[-1][:240]
    return None


def _coerce_subprocess_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


_STAGE_RESULT_RE = re.compile(
    r"^PCC_BOOTSTRAP_STAGE_RESULT\s+stage=(\d+)\s+elapsed_ms=(\d+)\b",
    re.MULTILINE,
)


def _parse_stage_elapsed_seconds(output: str) -> tuple[tuple[int, float], ...]:
    out: list[tuple[int, float]] = []
    for match in _STAGE_RESULT_RE.finditer(output or ""):
        out.append((int(match.group(1)), int(match.group(2)) / 1000.0))
    return tuple(out)


def _run_bootstrap(
    *,
    backend: str,
    stage: int,
    timeout_seconds: int,
    dry_run: bool,
    python_ir_passes: str | None = None,
    python_ir_pass_transport: str | None = None,
    python_ir_pass_timeout: float | None = None,
    python_ir_pass_telemetry_path: str | None = None,
) -> BootstrapResult:
    repo = _repo_root()
    out_dir = os.path.join(
        repo,
        "build",
        f"bootstrap-{backend}-{_host_slug()}",
    )
    bin_path = _stage_bin(out_dir, stage)
    cmd = [
        "bash",
        os.path.join(repo, "scripts", "bootstrap.sh"),
        "--out-dir",
        out_dir,
        "--backend",
        backend,
        "--stage",
        str(stage),
    ]
    print("\n== bootstrap backend=" + backend + " stage=" + str(stage), flush=True)
    print("+ " + " ".join(cmd), flush=True)
    if dry_run:
        return BootstrapResult(
            backend=backend,
            stage=stage,
            out_dir=out_dir,
            bin_path=bin_path,
            returncode=0,
            elapsed_seconds=0.0,
            size_bytes=None,
            help_returncode=None,
            help_elapsed_seconds=None,
            smoke_compile_returncode=None,
            smoke_compile_seconds=None,
            smoke_run_returncode=None,
            smoke_run_seconds=None,
            benchmark_compile_times=(),
            benchmark_run_times=(),
            pcc0_compile_returncode=None,
            pcc0_compile_seconds=None,
            pcc0_benchmark_compile_times=(),
            user_runtime_returncode=None,
            user_runtime_seconds=None,
            python_runtime_seconds=None,
            c_runtime_seconds=None,
            user_runtime_times=(),
            python_runtime_times=(),
            c_runtime_times=(),
            user_runtime_artifact_size_bytes=None,
            c_runtime_artifact_size_bytes=None,
            user_runtime_artifact_sizes=(),
            c_runtime_artifact_sizes=(),
            user_runtime_text_size_bytes=None,
            c_runtime_text_size_bytes=None,
            user_runtime_text_sizes=(),
            c_runtime_text_sizes=(),
            user_runtime_text_top_symbols=(),
            user_runtime_text_top_symbol_sources=(),
            import_runtime_returncode=None,
            import_runtime_seconds=None,
            python_import_runtime_seconds=None,
            import_runtime_times=(),
            python_import_runtime_times=(),
            stage_elapsed_seconds=(),
            links_libpython=None,
            failure_hint=None,
        )

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=repo,
            env=_child_env(
                python_ir_passes=python_ir_passes,
                python_ir_pass_transport=python_ir_pass_transport,
                python_ir_pass_timeout=python_ir_pass_timeout,
                python_ir_pass_telemetry_path=python_ir_pass_telemetry_path,
            ),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        returncode = result.returncode
        output = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        output = _coerce_subprocess_text(exc.stdout) + _coerce_subprocess_text(
            exc.stderr
        )
    elapsed = time.monotonic() - start
    stage_elapsed_seconds = _parse_stage_elapsed_seconds(output)

    if returncode == 0:
        help_code, help_elapsed = _help_smoke(bin_path)
        (
            smoke_compile_code,
            smoke_compile_elapsed,
            smoke_run_code,
            smoke_run_elapsed,
            benchmark_compile_times,
            benchmark_run_times,
        ) = _benchmark_smoke(bin_path, backend)
        (
            pcc0_compile_code,
            pcc0_compile_elapsed,
            pcc0_benchmark_compile_times,
        ) = _benchmark_pcc0_compile(backend)
        (
            user_runtime_code,
            user_runtime_elapsed,
            python_runtime_elapsed,
            c_runtime_elapsed,
            user_runtime_times,
            python_runtime_times,
            c_runtime_times,
            user_runtime_artifact_size,
            c_runtime_artifact_size,
            user_runtime_artifact_sizes,
            c_runtime_artifact_sizes,
            user_runtime_text_size,
            c_runtime_text_size,
            user_runtime_text_sizes,
            c_runtime_text_sizes,
            user_runtime_text_top_symbols,
            user_runtime_text_top_symbol_sources,
        ) = _benchmark_user_runtime(bin_path, backend)
        (
            import_runtime_code,
            import_runtime_elapsed,
            python_import_runtime_elapsed,
            import_runtime_times,
            python_import_runtime_times,
        ) = _benchmark_import_runtime(bin_path, backend)
    else:
        help_code = None
        help_elapsed = None
        smoke_compile_code = None
        smoke_compile_elapsed = None
        smoke_run_code = None
        smoke_run_elapsed = None
        benchmark_compile_times = ()
        benchmark_run_times = ()
        pcc0_compile_code = None
        pcc0_compile_elapsed = None
        pcc0_benchmark_compile_times = ()
        user_runtime_code = None
        user_runtime_elapsed = None
        python_runtime_elapsed = None
        c_runtime_elapsed = None
        user_runtime_times = ()
        python_runtime_times = ()
        c_runtime_times = ()
        user_runtime_artifact_size = None
        c_runtime_artifact_size = None
        user_runtime_artifact_sizes = ()
        c_runtime_artifact_sizes = ()
        user_runtime_text_size = None
        c_runtime_text_size = None
        user_runtime_text_sizes = ()
        c_runtime_text_sizes = ()
        user_runtime_text_top_symbols = ()
        user_runtime_text_top_symbol_sources = ()
        import_runtime_code = None
        import_runtime_elapsed = None
        python_import_runtime_elapsed = None
        import_runtime_times = ()
        python_import_runtime_times = ()
    links_libpython = _links_libpython(bin_path) if os.path.exists(bin_path) else None
    hint = None
    if returncode != 0:
        hint = _failure_hint(output)
    elif help_code not in (None, 0):
        hint = f"{os.path.basename(bin_path)} --help exited {help_code}"
    elif smoke_compile_code not in (None, 0):
        hint = f"{os.path.basename(bin_path)} toy compile exited {smoke_compile_code}"
    elif smoke_run_code not in (None, 0):
        hint = f"{os.path.basename(bin_path)} toy run exited {smoke_run_code}"
    elif pcc0_compile_code not in (None, 0):
        hint = f"pcc0 toy compile exited {pcc0_compile_code}"
    elif user_runtime_code not in (None, 0):
        hint = f"{os.path.basename(bin_path)} user runtime benchmark exited {user_runtime_code}"
    elif import_runtime_code not in (None, 0):
        hint = f"{os.path.basename(bin_path)} import runtime benchmark exited {import_runtime_code}"

    return BootstrapResult(
        backend=backend,
        stage=stage,
        out_dir=out_dir,
        bin_path=bin_path,
        returncode=returncode,
        elapsed_seconds=elapsed,
        size_bytes=_file_size(bin_path),
        help_returncode=help_code,
        help_elapsed_seconds=help_elapsed,
        smoke_compile_returncode=smoke_compile_code,
        smoke_compile_seconds=smoke_compile_elapsed,
        smoke_run_returncode=smoke_run_code,
        smoke_run_seconds=smoke_run_elapsed,
        benchmark_compile_times=benchmark_compile_times,
        benchmark_run_times=benchmark_run_times,
        pcc0_compile_returncode=pcc0_compile_code,
        pcc0_compile_seconds=pcc0_compile_elapsed,
        pcc0_benchmark_compile_times=pcc0_benchmark_compile_times,
        user_runtime_returncode=user_runtime_code,
        user_runtime_seconds=user_runtime_elapsed,
        python_runtime_seconds=python_runtime_elapsed,
        c_runtime_seconds=c_runtime_elapsed,
        user_runtime_times=user_runtime_times,
        python_runtime_times=python_runtime_times,
        c_runtime_times=c_runtime_times,
        user_runtime_artifact_size_bytes=user_runtime_artifact_size,
        c_runtime_artifact_size_bytes=c_runtime_artifact_size,
        user_runtime_artifact_sizes=user_runtime_artifact_sizes,
        c_runtime_artifact_sizes=c_runtime_artifact_sizes,
        user_runtime_text_size_bytes=user_runtime_text_size,
        c_runtime_text_size_bytes=c_runtime_text_size,
        user_runtime_text_sizes=user_runtime_text_sizes,
        c_runtime_text_sizes=c_runtime_text_sizes,
        user_runtime_text_top_symbols=user_runtime_text_top_symbols,
        user_runtime_text_top_symbol_sources=user_runtime_text_top_symbol_sources,
        import_runtime_returncode=import_runtime_code,
        import_runtime_seconds=import_runtime_elapsed,
        python_import_runtime_seconds=python_import_runtime_elapsed,
        import_runtime_times=import_runtime_times,
        python_import_runtime_times=python_import_runtime_times,
        stage_elapsed_seconds=stage_elapsed_seconds,
        links_libpython=links_libpython,
        failure_hint=hint,
    )


def _worst_benchmark(times: tuple[tuple[str, float], ...]) -> str:
    if not times:
        return "n/a"
    name, value = max(times, key=lambda item: item[1])
    return f"{name}:{value:.3f}s"


def _runtime_case_ratios(
    pcc_times: tuple[tuple[str, float], ...],
    python_times: tuple[tuple[str, float], ...],
    c_times: tuple[tuple[str, float], ...] = (),
) -> str:
    if not pcc_times and not python_times and not c_times:
        return "n/a"
    python_by_name = dict(python_times)
    c_by_name = dict(c_times)
    seen: set[str] = set()
    parts: list[str] = []
    for name, pcc_elapsed in pcc_times:
        seen.add(name)
        python_elapsed = python_by_name.get(name)
        c_elapsed = c_by_name.get(name)
        parts.append(
            f"{name}:pcc={_fmt_seconds(pcc_elapsed)},"
            f"python={_fmt_seconds(python_elapsed)},"
            f"ratio={_fmt_ratio(_ratio(pcc_elapsed, python_elapsed))},"
            f"c={_fmt_seconds(c_elapsed)},"
            f"pcc_vs_c={_fmt_ratio(_ratio(pcc_elapsed, c_elapsed))}"
        )
    for name, python_elapsed in python_times:
        if name in seen:
            continue
        seen.add(name)
        c_elapsed = c_by_name.get(name)
        parts.append(
            f"{name}:pcc=n/a,"
            f"python={_fmt_seconds(python_elapsed)},"
            "ratio=n/a,"
            f"c={_fmt_seconds(c_elapsed)},"
            "pcc_vs_c=n/a"
        )
    for name, c_elapsed in c_times:
        if name in seen:
            continue
        parts.append(
            f"{name}:pcc=n/a,"
            "python=n/a,"
            "ratio=n/a,"
            f"c={_fmt_seconds(c_elapsed)},"
            "pcc_vs_c=n/a"
        )
    return ";".join(parts)


def _fmt_bytes(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return str(int(round(float(value))))


def _runtime_artifact_size_cases(
    pcc_sizes: tuple[tuple[str, int], ...],
    c_sizes: tuple[tuple[str, int], ...],
) -> str:
    if not pcc_sizes and not c_sizes:
        return "n/a"
    c_by_name = dict(c_sizes)
    seen: set[str] = set()
    parts: list[str] = []
    for name, pcc_size in pcc_sizes:
        seen.add(name)
        c_size = c_by_name.get(name)
        parts.append(
            f"{name}:pcc={_fmt_bytes(pcc_size)},"
            f"c={_fmt_bytes(c_size)},"
            f"ratio={_fmt_ratio(_ratio(float(pcc_size), float(c_size)) if c_size else None)}"
        )
    for name, c_size in c_sizes:
        if name in seen:
            continue
        parts.append(
            f"{name}:pcc=n/a,"
            f"c={_fmt_bytes(c_size)},"
            "ratio=n/a"
        )
    return ";".join(parts)


def _runtime_text_symbol_cases(
    top_symbols: tuple[tuple[str, str], ...],
) -> str:
    if not top_symbols:
        return "n/a"
    return ";".join(f"{name}:{symbols}" for name, symbols in top_symbols)


def _print_result(result: BootstrapResult) -> None:
    help_code = result.help_returncode if result.help_returncode is not None else "n/a"
    smoke_compile_code = (
        result.smoke_compile_returncode
        if result.smoke_compile_returncode is not None
        else "n/a"
    )
    smoke_run_code = (
        result.smoke_run_returncode
        if result.smoke_run_returncode is not None
        else "n/a"
    )
    pcc0_compile_code = (
        result.pcc0_compile_returncode
        if result.pcc0_compile_returncode is not None
        else "n/a"
    )
    user_runtime_code = (
        result.user_runtime_returncode
        if result.user_runtime_returncode is not None
        else "n/a"
    )
    import_runtime_code = (
        result.import_runtime_returncode
        if result.import_runtime_returncode is not None
        else "n/a"
    )
    libpython = result.links_libpython if result.links_libpython is not None else "n/a"
    stage_elapsed = (
        ",".join(
            f"{stage}:{elapsed:.3f}s" for stage, elapsed in result.stage_elapsed_seconds
        )
        if result.stage_elapsed_seconds
        else "n/a"
    )
    print(
        "result "
        f"backend={result.backend} "
        f"stage={result.stage} "
        f"exit={result.returncode} "
        f"elapsed={result.elapsed_seconds:.1f}s "
        f"size={result.size_bytes if result.size_bytes is not None else 'n/a'} "
        f"help={help_code} "
        f"help_elapsed={_fmt_seconds(result.help_elapsed_seconds)} "
        f"smoke_compile={smoke_compile_code} "
        f"smoke_compile_geomean={_fmt_seconds(result.smoke_compile_seconds)} "
        f"smoke_compile_worst={_worst_benchmark(result.benchmark_compile_times)} "
        f"smoke_run={smoke_run_code} "
        f"smoke_run_geomean={_fmt_seconds(result.smoke_run_seconds)} "
        f"smoke_run_worst={_worst_benchmark(result.benchmark_run_times)} "
        f"pcc0_compile={pcc0_compile_code} "
        f"pcc0_compile_geomean={_fmt_seconds(result.pcc0_compile_seconds)} "
        f"pcc1_vs_pcc0_compile_ratio={_fmt_ratio(_ratio(result.smoke_compile_seconds, result.pcc0_compile_seconds))} "
        f"user_runtime={user_runtime_code} "
        f"user_runtime_geomean={_fmt_seconds(result.user_runtime_seconds)} "
        f"python_runtime_geomean={_fmt_seconds(result.python_runtime_seconds)} "
        f"c_runtime_geomean={_fmt_seconds(result.c_runtime_seconds)} "
        f"user_runtime_vs_python_ratio={_fmt_ratio(_ratio(result.user_runtime_seconds, result.python_runtime_seconds))} "
        f"user_runtime_vs_c_ratio={_fmt_ratio(_ratio(result.user_runtime_seconds, result.c_runtime_seconds))} "
        f"user_runtime_cases={_runtime_case_ratios(result.user_runtime_times, result.python_runtime_times, result.c_runtime_times)} "
        f"user_runtime_artifact_size_geomean={_fmt_bytes(result.user_runtime_artifact_size_bytes)} "
        f"c_runtime_artifact_size_geomean={_fmt_bytes(result.c_runtime_artifact_size_bytes)} "
        f"user_runtime_artifact_size_ratio={_fmt_ratio(_ratio(result.user_runtime_artifact_size_bytes, result.c_runtime_artifact_size_bytes))} "
        f"user_runtime_artifact_sizes={_runtime_artifact_size_cases(result.user_runtime_artifact_sizes, result.c_runtime_artifact_sizes)} "
        f"user_runtime_text_size_geomean={_fmt_bytes(result.user_runtime_text_size_bytes)} "
        f"c_runtime_text_size_geomean={_fmt_bytes(result.c_runtime_text_size_bytes)} "
        f"user_runtime_text_size_ratio={_fmt_ratio(_ratio(result.user_runtime_text_size_bytes, result.c_runtime_text_size_bytes))} "
        f"user_runtime_text_sizes={_runtime_artifact_size_cases(result.user_runtime_text_sizes, result.c_runtime_text_sizes)} "
        f"user_runtime_text_top_symbols={_runtime_text_symbol_cases(result.user_runtime_text_top_symbols)} "
        f"user_runtime_text_top_symbol_sources={_runtime_text_symbol_cases(result.user_runtime_text_top_symbol_sources)} "
        f"import_runtime={import_runtime_code} "
        f"import_runtime_geomean={_fmt_seconds(result.import_runtime_seconds)} "
        f"python_import_runtime_geomean={_fmt_seconds(result.python_import_runtime_seconds)} "
        f"import_runtime_vs_python_ratio={_fmt_ratio(_ratio(result.import_runtime_seconds, result.python_import_runtime_seconds))} "
        f"import_runtime_cases={_runtime_case_ratios(result.import_runtime_times, result.python_import_runtime_times)} "
        f"stage_elapsed={stage_elapsed} "
        f"libpython={libpython}",
        flush=True,
    )
    if result.failure_hint:
        print("failure_hint=" + result.failure_hint, flush=True)


def _selected_backends(value: str) -> tuple[str, ...]:
    if value == "both":
        return ("llvm", "self")
    return (value,)


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}s"


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def _ratio(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None:
        return None
    if baseline <= 0:
        return None
    return value / baseline


def _check_ratio(
    *,
    label: str,
    value: float | None,
    baseline: float | None,
    threshold: float,
) -> bool:
    ratio = _ratio(value, baseline)
    if ratio is None:
        print(f"{label}_ratio=n/a", flush=True)
        return True
    print(f"{label}_ratio self/llvm={ratio:.3f}", flush=True)
    if ratio > threshold:
        print(
            f"FAIL {label} ratio {ratio:.3f} exceeds threshold {threshold:.3f}",
            file=sys.stderr,
        )
        return False
    return True


def _check_performance_thresholds(
    results: list[BootstrapResult],
    *,
    bootstrap_threshold: float,
    help_threshold: float,
    smoke_compile_threshold: float,
    smoke_run_threshold: float,
) -> bool:
    by_backend = {result.backend: result for result in results}
    llvm = by_backend.get("llvm")
    self_result = by_backend.get("self")
    if llvm is None or self_result is None:
        return True
    ok = True
    ok = (
        _check_ratio(
            label="bootstrap_elapsed",
            value=self_result.elapsed_seconds,
            baseline=llvm.elapsed_seconds,
            threshold=bootstrap_threshold,
        )
        and ok
    )
    ok = (
        _check_ratio(
            label="help_elapsed",
            value=self_result.help_elapsed_seconds,
            baseline=llvm.help_elapsed_seconds,
            threshold=help_threshold,
        )
        and ok
    )
    ok = (
        _check_ratio(
            label="smoke_compile_elapsed",
            value=self_result.smoke_compile_seconds,
            baseline=llvm.smoke_compile_seconds,
            threshold=smoke_compile_threshold,
        )
        and ok
    )
    ok = (
        _check_ratio(
            label="smoke_run_elapsed",
            value=self_result.smoke_run_seconds,
            baseline=llvm.smoke_run_seconds,
            threshold=smoke_run_threshold,
        )
        and ok
    )
    return ok


def _check_stage_elapsed_threshold(
    results: list[BootstrapResult],
    *,
    max_stage_elapsed: float,
) -> bool:
    if max_stage_elapsed <= 0:
        return True
    ok = True
    for result in results:
        if not result.stage_elapsed_seconds:
            print(
                f"FAIL stage elapsed backend={result.backend}: "
                "bootstrap stage timing measurements are required",
                file=sys.stderr,
            )
            ok = False
            continue
        for stage, elapsed in result.stage_elapsed_seconds:
            print(
                f"stage_elapsed backend={result.backend} "
                f"stage={stage} elapsed={elapsed:.3f}s "
                f"limit={max_stage_elapsed:.3f}s",
                flush=True,
            )
            if elapsed > max_stage_elapsed:
                print(
                    f"FAIL stage elapsed backend={result.backend} "
                    f"stage={stage} {elapsed:.3f}s exceeds "
                    f"{max_stage_elapsed:.3f}s",
                    file=sys.stderr,
                )
                ok = False
    return ok


def _check_pcc1_compile_threshold(
    results: list[BootstrapResult],
    *,
    max_pcc1_compile_ratio: float,
) -> bool:
    if max_pcc1_compile_ratio <= 0:
        return True
    ok = True
    for result in results:
        ratio = _ratio(result.smoke_compile_seconds, result.pcc0_compile_seconds)
        print(
            f"pcc1_vs_pcc0_compile_ratio backend={result.backend} "
            f"ratio={_fmt_ratio(ratio)} limit={max_pcc1_compile_ratio:.3f}",
            flush=True,
        )
        if ratio is not None and ratio >= max_pcc1_compile_ratio:
            print(
                f"FAIL pcc1 compiler speed backend={result.backend}: "
                f"pcc1/pcc0 compile ratio {ratio:.3f} must be below "
                f"{max_pcc1_compile_ratio:.3f}",
                file=sys.stderr,
            )
            ok = False
    return ok


def _check_user_runtime_threshold(
    results: list[BootstrapResult],
    *,
    max_user_runtime_ratio: float,
) -> bool:
    if max_user_runtime_ratio <= 0:
        return True
    ok = True
    for result in results:
        ratio = _ratio(result.user_runtime_seconds, result.python_runtime_seconds)
        print(
            f"user_runtime_vs_python_ratio backend={result.backend} "
            f"ratio={_fmt_ratio(ratio)} limit={max_user_runtime_ratio:.3f}",
            flush=True,
        )
        if ratio is None:
            print(
                f"FAIL user runtime speed backend={result.backend}: "
                "compiled and Python runtime measurements are required",
                file=sys.stderr,
            )
            ok = False
            continue
        if ratio is not None and ratio > max_user_runtime_ratio:
            print(
                f"FAIL user runtime speed backend={result.backend}: "
                f"compiled/Python runtime ratio {ratio:.3f} exceeds "
                f"{max_user_runtime_ratio:.3f}",
                file=sys.stderr,
            )
            ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the supported-host Python bootstrap gate with LLVM and/or "
            "self native emission."
        )
    )
    parser.add_argument(
        "--backend",
        choices=("llvm", "self", "both"),
        default="both",
        help="backend selection to run; default: both",
    )
    parser.add_argument(
        "--stage",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help="bootstrap stage limit; default: 1",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="per-backend bootstrap timeout in seconds; default: 900",
    )
    parser.add_argument(
        "--allow-non-supported-host",
        action="store_true",
        help="run even when the host is not the supported macOS arm64 target",
    )
    parser.add_argument(
        "--max-bootstrap-ratio",
        type=float,
        default=2.0,
        help="maximum allowed self/LLVM bootstrap wall-time ratio; default: 2.0",
    )
    parser.add_argument(
        "--max-help-ratio",
        type=float,
        default=2.0,
        help=("maximum allowed self/LLVM pcc --help latency ratio; " "default: 2.0"),
    )
    parser.add_argument(
        "--max-smoke-compile-ratio",
        type=float,
        default=2.0,
        help=("maximum allowed self/LLVM toy compile latency ratio; " "default: 2.0"),
    )
    parser.add_argument(
        "--max-smoke-run-ratio",
        type=float,
        default=2.0,
        help=(
            "maximum allowed self/LLVM toy executable runtime ratio; " "default: 2.0"
        ),
    )
    parser.add_argument(
        "--max-stage-elapsed",
        type=float,
        default=30.0,
        help=(
            "maximum allowed wall time for any single bootstrap stage "
            "(pcc0->pcc1, pcc1->pcc2, pcc2->pcc3); default: 30.0s. "
            "Use 0 to disable the absolute stage target check."
        ),
    )
    parser.add_argument(
        "--max-pcc1-compile-ratio",
        type=float,
        default=1.0,
        help=(
            "maximum allowed pcc1/pcc0 compile-time ratio on smoke programs; "
            "default: 1.0, meaning pcc1 must be faster than CPython-hosted pcc. "
            "Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--max-user-runtime-vs-python-ratio",
        type=float,
        default=0.333,
        help=(
            "maximum allowed compiled-program/CPython runtime ratio on the "
            "typed user benchmark; default: 0.333, i.e. at least 3x faster. "
            "Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print commands without executing them",
    )
    parser.add_argument(
        "--python-ir-passes",
        default=None,
        help=(
            "override PCC_PYTHON_IR_PASSES for the bootstrap subprocess. "
            "Default is the bounded gate policy: preserve caller env if set, "
            "otherwise force off."
        ),
    )
    parser.add_argument(
        "--python-ir-pass-transport",
        choices=("text", "memory"),
        default=None,
        help="override PCC_PYTHON_IR_PASS_TRANSPORT for IR-pass experiments",
    )
    parser.add_argument(
        "--python-ir-pass-timeout",
        type=float,
        default=None,
        help="override PCC_PYTHON_IR_PASS_TIMEOUT in seconds",
    )
    parser.add_argument(
        "--python-ir-pass-telemetry-path",
        default=None,
        help=(
            "set PCC_PYTHON_IR_PASS_TELEMETRY_PATH and enable telemetry for "
            "the bootstrap subprocess; prefer /tmp for throwaway experiments"
        ),
    )
    args = parser.parse_args(argv)

    if not args.allow_non_supported_host and not _supported_host():
        print(
            "self-backed bootstrap default gate is defined for the supported "
            "macOS arm64 host; pass --allow-non-supported-host to override",
            file=sys.stderr,
        )
        return 2

    print("self backend bootstrap gate", flush=True)
    print(f"host={platform.system()} {platform.machine()}", flush=True)
    if (
        args.python_ir_passes is not None
        or args.python_ir_pass_transport is not None
        or args.python_ir_pass_timeout is not None
        or args.python_ir_pass_telemetry_path is not None
    ):
        print(
            "python_ir_pass_experiment "
            f"passes={args.python_ir_passes if args.python_ir_passes is not None else 'env-or-off'} "
            f"transport={args.python_ir_pass_transport if args.python_ir_pass_transport is not None else 'env/default'} "
            f"timeout={args.python_ir_pass_timeout if args.python_ir_pass_timeout is not None else 'env/default'} "
            f"telemetry_path={args.python_ir_pass_telemetry_path if args.python_ir_pass_telemetry_path is not None else 'none'}",
            flush=True,
        )
    results = []
    for backend in _selected_backends(args.backend):
        result = _run_bootstrap(
            backend=backend,
            stage=args.stage,
            timeout_seconds=args.timeout,
            dry_run=args.dry_run,
            python_ir_passes=args.python_ir_passes,
            python_ir_pass_transport=args.python_ir_pass_transport,
            python_ir_pass_timeout=args.python_ir_pass_timeout,
            python_ir_pass_telemetry_path=args.python_ir_pass_telemetry_path,
        )
        results.append(result)
        _print_result(result)

    if len(results) == 2:
        first, second = results
        if first.size_bytes and second.size_bytes:
            ratio = second.size_bytes / first.size_bytes
            print(
                f"size_ratio {second.backend}/{first.backend}={ratio:.3f}",
                flush=True,
            )
        if not _check_performance_thresholds(
            results,
            bootstrap_threshold=args.max_bootstrap_ratio,
            help_threshold=args.max_help_ratio,
            smoke_compile_threshold=args.max_smoke_compile_ratio,
            smoke_run_threshold=args.max_smoke_run_ratio,
        ):
            return 1

    if not _check_stage_elapsed_threshold(
        results,
        max_stage_elapsed=args.max_stage_elapsed,
    ):
        return 1

    if not _check_pcc1_compile_threshold(
        results,
        max_pcc1_compile_ratio=args.max_pcc1_compile_ratio,
    ):
        return 1

    if not _check_user_runtime_threshold(
        results,
        max_user_runtime_ratio=args.max_user_runtime_vs_python_ratio,
    ):
        return 1

    for result in results:
        if result.returncode != 0:
            return result.returncode
        if result.help_returncode not in (None, 0):
            return 1
        if result.smoke_compile_returncode not in (None, 0):
            return 1
        if result.smoke_run_returncode not in (None, 0):
            return 1
        if result.pcc0_compile_returncode not in (None, 0):
            return 1
        if result.user_runtime_returncode not in (None, 0):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
