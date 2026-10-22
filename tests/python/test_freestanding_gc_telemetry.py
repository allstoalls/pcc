from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

from pcc.frontends.python import pipeline
from pcc.frontends.python.codegen.runtime_abi import (
    FREESTANDING_GC_I64_GLOBALS,
    FREESTANDING_GC_RUNTIME_GLOBALS,
    RUNTIME_SIGNATURES,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
TELEMETRY_SOURCE = RUNTIME_DIR / "py" / "py_gc_telemetry.py"
# The retired C runtime's pcc_gc_telemetry(), frozen as the reference mapping.
C_ORACLE_SOURCE = REPO_ROOT / "tests" / "data" / "gc_telemetry_counter_oracle.c"
PUBLIC_SYMBOLS = {
    "pcc_gc_telemetry",
    "pcc_gc_backend2_worker_buffer_score",
    "pcc_gc_backend2_production_score",
    "pcc_gc_backend3_minor_productivity_score",
    "pcc_gc_backend3_remembered_update_score",
}
VERIFIED_INTRINSIC_FUNCTIONS = {"pcc_gc_backend"}


def _literal_runtime_imports() -> tuple[set[str], set[str]]:
    functions: set[str] = set()
    globals_: set[str] = set()
    tree = ast.parse(TELEMETRY_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "extern" and node.args:
            value = node.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                functions.add(value.value)
        if node.func.id == "global_addr" and node.args:
            value = node.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                globals_.add(value.value)
    return functions, globals_


def _extract_c_oracle() -> str:
    source = C_ORACLE_SOURCE.read_text(encoding="utf-8")
    marker = "int64_t pcc_gc_telemetry(int64_t metric) {"
    start = source.index(marker)
    depth = 0
    end = start
    for end in range(start, len(source)):
        char = source[end]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end += 1
                break
    assert depth == 0
    oracle = source[start:end].replace(
        "pcc_gc_telemetry(int64_t metric)",
        "pcc_gc_telemetry_oracle(int64_t metric)",
        1,
    )
    # The C implementation's private helper has the same linker spelling as
    # the pcc-Python state global for read-barrier telemetry. Keep both in the
    # differential by giving only the extracted oracle helper a local name.
    return oracle.replace("pcc_gc_metric_load(metric)", "pcc_gc_metric_load_oracle(metric)")


def _compile_telemetry_object(tmp_path: Path, emitter: str = "llvm") -> Path:
    llvm_ir = tmp_path / "py_gc_telemetry.ll"
    pipeline.compile_python(
        str(TELEMETRY_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "py_gc_telemetry.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("py_gc_telemetry_" + emitter + ".o")
    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _differential_source() -> str:
    oracle = _extract_c_oracle()
    py_functions, py_globals = _literal_runtime_imports()
    oracle_functions = set(
        re.findall(r"\b(pcc_gc_[A-Za-z0-9_]+)\s*\(", oracle)
    )
    oracle_functions -= {
        "pcc_gc_init_config",
        "pcc_gc_metric_load_oracle",
        "pcc_gc_telemetry_oracle",
    }
    oracle_globals = set(re.findall(r"&\s*(pcc_gc_[A-Za-z0-9_]+)", oracle))
    oracle_globals.update(
        re.findall(r"return\s+(pcc_gc_[A-Za-z0-9_]+)\s*;", oracle)
    )
    oracle_globals.discard("pcc_gc_pause_hist")

    functions = sorted(
        py_functions | oracle_functions | VERIFIED_INTRINSIC_FUNCTIONS
    )
    globals_ = sorted(py_globals | oracle_globals)
    function_values = {name: 10000 + index for index, name in enumerate(functions)}
    global_values = {name: 1000 + index for index, name in enumerate(globals_)}
    global_values["pcc_gc_debt_bytes"] = 8589934765
    for name in ("pcc_gc_metric_step", "pcc_gc_metric_max_pause_us",
                 "pcc_gc_metric_pause_count", "pcc_gc_metric_pause_sum_us"):
        global_values[name] = 8589934765

    aliases = {
        "pcc_gc_max_pause_us": "pcc_gc_metric_max_pause_us",
        "pcc_gc_pause_count": "pcc_gc_metric_pause_count",
        "pcc_gc_pause_sum_us": "pcc_gc_metric_pause_sum_us",
    }
    for c_name, py_name in aliases.items():
        global_values[c_name] = global_values[py_name]

    lines = [
        '#include "py_runtime.h"',
        "#include <stdint.h>",
        "#include <stdio.h>",
        "static void pcc_gc_init_config(void) {}",
    ]
    for name in globals_:
        # Declare each counter at its registered width: reading an i64
        # counter out of a 4-byte harness global returns neighbouring bytes.
        ctype = "int64_t" if aliases.get(name, name) in FREESTANDING_GC_I64_GLOBALS else "int32_t"
        lines.append(f"{ctype} {name} = {global_values[name]};")
    pause_values = [
        global_values[f"pcc_gc_metric_pause_hist{index}"] for index in range(4)
    ]
    lines.append(
        "int32_t pcc_gc_pause_hist[4] = {"
        + ", ".join(str(value) for value in pause_values)
        + "};"
    )
    for name in functions:
        lines.append(
            f"int64_t {name}(void) {{ return {function_values[name]}; }}"
        )
    lines.extend(
        [
            "static int64_t pcc_gc_metric_load_oracle(int64_t metric) {",
            "  if (metric == 0) return pcc_gc_metric_alloc;",
            "  if (metric == 1) return pcc_gc_metric_store;",
            "  if (metric == 2) return pcc_gc_metric_load;",
            "  if (metric == 3) return pcc_gc_metric_safepoint;",
            "  if (metric == 4) return pcc_gc_metric_pin;",
            "  if (metric == 5) return pcc_gc_metric_step;",
            "  return -1;",
            "}",
            oracle,
            "extern int64_t pcc_gc_telemetry(int64_t metric);",
            "int main(void) {",
            "  for (int64_t metric = -1; metric <= 117; metric++) {",
            "    int64_t expected = pcc_gc_telemetry_oracle(metric);",
            "    int64_t actual = pcc_gc_telemetry(metric);",
            "    if (actual != expected) {",
            '      fprintf(stderr, "metric %lld: expected %lld, got %lld\\n",',
            "              (long long)metric, (long long)expected, (long long)actual);",
            "      return (int)(metric + 2);",
            "    }",
            "  }",
            "  return 0;",
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def _run_counter_differential(tmp_path: Path, emitter: str) -> subprocess.CompletedProcess[str]:
    implementation = _compile_telemetry_object(tmp_path, emitter)
    harness = tmp_path / ("telemetry_differential_" + emitter + ".c")
    executable = tmp_path / ("telemetry_differential_" + emitter)
    harness.write_text(_differential_source(), encoding="utf-8")
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            "-O0",
            f"-I{RUNTIME_DIR / 'include'}",
            str(harness),
            str(implementation),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )


def test_freestanding_gc_telemetry_counter_abi_matches_c_oracle(tmp_path: Path):
    result = _run_counter_differential(tmp_path, "llvm")
    assert result.returncode == 0, result.stdout + result.stderr


def test_self_emitted_gc_telemetry_counter_abi_matches_c_oracle(tmp_path: Path):
    result = _run_counter_differential(tmp_path, "self")
    assert result.returncode == 0, result.stdout + result.stderr


def test_freestanding_gc_telemetry_object_has_typed_cross_object_closure(
    tmp_path: Path,
):
    obj = _compile_telemetry_object(tmp_path)
    expected_functions, expected_globals = _literal_runtime_imports()
    expected_functions |= VERIFIED_INTRINSIC_FUNCTIONS

    undefined_result = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined_result.returncode == 0, (
        undefined_result.stdout + undefined_result.stderr
    )
    undefined = {
        line.split()[-1].lstrip("_")
        for line in undefined_result.stdout.splitlines()
        if line.strip()
    }
    assert undefined == expected_functions | expected_globals

    for symbol in expected_functions:
        return_type, parameter_types, var_arg = RUNTIME_SIGNATURES[symbol]
        assert str(return_type) == "i64"
        assert not parameter_types
        assert var_arg is False
    assert expected_globals
    assert expected_globals <= FREESTANDING_GC_RUNTIME_GLOBALS

    symbols_result = subprocess.run(
        ["nm", "-g", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    defined = {
        line.split()[-1].lstrip("_")
        for line in symbols_result.stdout.splitlines()
        if line.strip() and " U " not in line
    }
    assert defined == PUBLIC_SYMBOLS


def test_production_archive_owns_and_runs_freestanding_gc_telemetry(
    tmp_path: Path, pcc_runtime_archive: Path
):
    members_result = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members_result.returncode == 0, members_result.stdout + members_result.stderr
    assert "py_gc_telemetry.o" in members_result.stdout.splitlines()

    symbols_result = subprocess.run(
        ["nm", "-A", "-g", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols_result.returncode == 0, symbols_result.stdout + symbols_result.stderr
    owners: dict[str, list[str]] = {symbol: [] for symbol in PUBLIC_SYMBOLS}
    for line in symbols_result.stdout.splitlines():
        symbol = line.split()[-1].lstrip("_") if line.strip() else ""
        if symbol in owners and " U " not in line:
            owners[symbol].append(line)
    assert all(len(lines) == 1 for lines in owners.values())
    assert all(":py_gc_telemetry.o:" in lines[0] for lines in owners.values())

    # Every declared counter id answers >= 0; the first id past the last
    # declared counter answers -1.
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(encoding="utf-8")
    last_metric = max(
        int(value) for value in re.findall(r"PCC_GC_COUNTER_\w+\s*=\s*(\d+)", header)
    )
    harness = tmp_path / "production_telemetry.c"
    executable = tmp_path / "production_telemetry"
    harness.write_text(
        '#include "py_runtime.h"\n'
        "#include <stdio.h>\n"
        "int main(void) {\n"
        f"  for (int64_t metric = 0; metric <= {last_metric}; metric++) {{\n"
        "    if (pcc_gc_telemetry(metric) < 0) {\n"
        '      fprintf(stderr, "metric %lld < 0\\n", (long long)metric);\n'
        "      return 1;\n"
        "    }\n"
        "  }\n"
        "  if (pcc_gc_backend2_worker_buffer_score() != pcc_gc_telemetry(29)) return 117;\n"
        "  if (pcc_gc_backend2_production_score() != pcc_gc_telemetry(28)) return 118;\n"
        "  if (pcc_gc_backend3_minor_productivity_score() != pcc_gc_telemetry(30)) return 119;\n"
        "  if (pcc_gc_backend3_remembered_update_score() != pcc_gc_telemetry(31)) return 120;\n"
        "  if (pcc_gc_telemetry(-1) != -1) return 121;\n"
        f"  if (pcc_gc_telemetry({last_metric + 1}) != -1) return 122;\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            str(harness),
            str(pcc_runtime_archive),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    for backend in range(5):
        env = dict(os.environ)
        env.pop("LC_ALL", None)
        env["PCC_GC_BACKEND"] = str(backend)
        result = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, (
            f"GC backend {backend}: " + result.stdout + result.stderr
        )
