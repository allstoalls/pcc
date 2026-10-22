from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from pcc.diagnostics.bootstrap_profile_report import build_bootstrap_profile_report

RUNTIME_PROBE_SOURCE = r"""
#include "py_runtime.h"
#include "py_internal.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <sys/time.h>

static int64_t now_us(void) {
    struct timeval tv;
    if (gettimeofday(&tv, 0) != 0) return 0;
    return ((int64_t)tv.tv_sec * 1000000) + (int64_t)tv.tv_usec;
}

static int64_t rss_kb(void) {
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage) != 0) return 0;
#if defined(__APPLE__)
    return (int64_t)usage.ru_maxrss / 1024;
#else
    return (int64_t)usage.ru_maxrss;
#endif
}

static PyObject *noop_entry(PyObject *captures, PyObject *args) {
    (void)captures;
    (void)args;
    py_incref(py_None);
    return py_None;
}

static void resume_marker(void) {}

static void *thread_main(void *arg) {
    return arg;
}

static void print_row(
    const char *name,
    int64_t operations,
    int64_t wall_us,
    int64_t rss_before_kb,
    int64_t rss_after_kb,
    int64_t gc_pause_us,
    int64_t pin_events
) {
    printf(
        "row name=%s operations=%lld wall_us=%lld rss_before_kb=%lld "
        "rss_after_kb=%lld gc_pause_us=%lld pin_events=%lld\n",
        name,
        (long long)operations,
        (long long)wall_us,
        (long long)rss_before_kb,
        (long long)rss_after_kb,
        (long long)gc_pause_us,
        (long long)pin_events
    );
}

int main(int argc, char **argv) {
    int64_t iterations = 100;
    if (argc > 1) {
        iterations = atoll(argv[1]);
        if (iterations <= 0) iterations = 1;
    }
    if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 2;
    printf("workload_iterations=%lld\n", (long long)iterations);

    int64_t rss0 = rss_kb();
    int64_t start = now_us();
    for (int64_t i = 0; i < iterations; i++) {
        PyObject *coro = py_coroutine_new_native("noop", (void *)&noop_entry, 0, 0);
        if (coro == 0) return 3;
        PyObject *result = py_coroutine_run(coro);
        if (result == 0) return 4;
        pcc_gc_release(result);
        pcc_gc_release(coro);
    }
    int64_t end = now_us();
    int64_t gc0 = now_us();
    (void)pcc_gc_collect(0);
    int64_t gc1 = now_us();
    print_row("coroutine_thunk", iterations, end - start, rss0, rss_kb(), gc1 - gc0, 0);

    int32_t frame_map[1] = {0};
    PyObject **vthreads = (PyObject **)calloc((size_t)iterations, sizeof(PyObject *));
    if (vthreads == 0) return 5;
    int64_t pin_before = py_virtual_thread_pin_event_count();
    rss0 = rss_kb();
    start = now_us();
    for (int64_t i = 0; i < iterations; i++) {
        PyObject *cont = py_continuation_new(frame_map, 0, (void *)&resume_marker);
        if (cont == 0) return 5;
        PyObject *vt = py_virtual_thread_new(cont);
        if (vt == 0) return 6;
        pcc_gc_release(cont);
        if (py_virtual_thread_start(vt) != 0) return 7;
        vthreads[i] = vt;
    }
    if (py_virtual_thread_run_carrier_pool(2, iterations) != iterations) return 8;
    for (int64_t i = 0; i < iterations; i++) {
        if (py_virtual_thread_state(vthreads[i]) != 4) return 9;
        pcc_gc_release(vthreads[i]);
    }
    free(vthreads);
    end = now_us();
    gc0 = now_us();
    (void)pcc_gc_collect(0);
    gc1 = now_us();
    print_row(
        "pcc_virtual_thread",
        iterations,
        end - start,
        rss0,
        rss_kb(),
        gc1 - gc0,
        py_virtual_thread_pin_event_count() - pin_before
    );

    rss0 = rss_kb();
    start = now_us();
    for (int64_t i = 0; i < iterations; i++) {
        PccThreadHandle *thread = 0;
        if (pcc_thread_start(&thread, thread_main, 0) != 0) return 10;
        void *result = 0;
        if (pcc_thread_join(thread, &result) != 0) return 11;
    }
    end = now_us();
    gc0 = now_us();
    (void)pcc_gc_collect(0);
    gc1 = now_us();
    print_row("os_thread", iterations, end - start, rss0, rss_kb(), gc1 - gc0, 0);

    return 0;
}
"""


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except ValueError:
        return 0


def parse_probe_output(text: str) -> dict[str, Any]:
    iterations = 0
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("workload_iterations="):
            iterations = _as_int(line.split("=", 1)[1])
            continue
        if not line.startswith("row "):
            continue
        fields: dict[str, str] = {}
        for part in line.split()[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key] = value
        name = fields.get("name", "")
        operations = _as_int(fields.get("operations"))
        wall_us = _as_int(fields.get("wall_us"))
        pin_events = _as_int(fields.get("pin_events"))
        row = {
            "name": name,
            "operations": operations,
            "wall_us": wall_us,
            "wall_ms": wall_us / 1000.0,
            "latency_us_per_op": (wall_us / operations) if operations > 0 else 0.0,
            "throughput_ops_per_s": (
                (operations * 1000000.0) / wall_us if wall_us > 0 else 0.0
            ),
            "rss_before_kb": _as_int(fields.get("rss_before_kb")),
            "rss_after_kb": _as_int(fields.get("rss_after_kb")),
            "rss_delta_kb": _as_int(fields.get("rss_after_kb"))
            - _as_int(fields.get("rss_before_kb")),
            "gc_pause_us": _as_int(fields.get("gc_pause_us")),
            "pin_events": pin_events,
            "pinning_rate_per_1k_ops": (
                (pin_events * 1000.0) / operations if operations > 0 else 0.0
            ),
        }
        rows.append(row)
    return {"iterations": iterations, "rows": rows}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_runtime_probe(
    *,
    iterations: int,
    repo_root: str | Path | None = None,
    timeout: int = 1800,
    keep_tmp: bool = False,
) -> dict[str, Any]:
    root = Path(repo_root) if repo_root is not None else _repo_root()
    runtime_dir = root / "pcc" / "runtime"
    cc = os.environ.get("CC", "cc")
    tmp_obj: tempfile.TemporaryDirectory[str] | None = None
    if keep_tmp:
        tmp = Path(tempfile.mkdtemp(prefix="pcc-vthread-comparison-"))
    else:
        tmp_obj = tempfile.TemporaryDirectory(prefix="pcc-vthread-comparison-")
        tmp = Path(tmp_obj.name)
    try:
        work_runtime = tmp / "py_runtime"
        shutil.copytree(
            runtime_dir,
            work_runtime,
            ignore=shutil.ignore_patterns(
                "build", "build_pcc", "build_py", "build_libpython", "*.a",
                "*.a.*", "_native", "__pycache__",
            ),
        )
        # The threaded pcc-Python runtime: every module goes through pcc, so
        # this takes minutes where the retired C archive took seconds.
        pcc_bin = root / ".venv" / "bin" / "pcc"
        build_runtime = subprocess.run(
            [
                "make",
                "-B",
                "-C",
                str(work_runtime),
                f"PCC={pcc_bin if pcc_bin.is_file() else 'pcc'}",
                f"PYTHON={sys.executable}",
                f"PCC_REPO_ROOT={root}",
                "PCC_WITH_THREADS=1",
                "libpy_runtime_pcc_py.a",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if build_runtime.returncode != 0:
            raise RuntimeError(build_runtime.stdout + build_runtime.stderr)
        src = tmp / "virtual_thread_comparison_probe.c"
        exe = tmp / "virtual_thread_comparison_probe.out"
        src.write_text(textwrap.dedent(RUNTIME_PROBE_SOURCE).lstrip(), encoding="utf-8")
        build_probe = subprocess.run(
            [
                cc,
                "-DPCC_WITH_THREADS=1",
                "-std=c11",
                "-pthread",
                f"-I{work_runtime / 'include'}",
                f"-I{work_runtime / 'src'}",
                str(src),
                str(work_runtime / "libpy_runtime_pcc_py.a"),
                "-lm",
                "-o",
                str(exe),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if build_probe.returncode != 0:
            raise RuntimeError(build_probe.stdout + build_probe.stderr)
        run_probe = subprocess.run(
            [str(exe), str(iterations)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if run_probe.returncode != 0:
            raise RuntimeError(run_probe.stdout + run_probe.stderr)
        return parse_probe_output(run_probe.stdout)
    finally:
        if keep_tmp:
            print(f"kept temporary probe directory: {tmp}")  # noqa: T201
        elif tmp_obj is not None:
            tmp_obj.cleanup()


def sample_probe_data(iterations: int = 10) -> dict[str, Any]:
    operations = max(1, int(iterations))
    text = "\n".join(
        [
            f"workload_iterations={operations}",
            (
                f"row name=coroutine_thunk operations={operations} wall_us=1000 "
                "rss_before_kb=100 rss_after_kb=104 gc_pause_us=20 pin_events=0"
            ),
            (
                f"row name=pcc_virtual_thread operations={operations} wall_us=1500 "
                "rss_before_kb=104 rss_after_kb=108 gc_pause_us=25 pin_events=1"
            ),
            (
                f"row name=os_thread operations={operations} wall_us=9000 "
                "rss_before_kb=108 rss_after_kb=120 gc_pause_us=30 pin_events=0"
            ),
        ]
    )
    return parse_probe_output(text)


def build_virtual_thread_comparison_report(
    probe_data: dict[str, Any],
    *,
    bootstrap_profile_dir: str | Path | None = None,
    top: int = 6,
) -> dict[str, Any]:
    rows = list(probe_data.get("rows", []))
    names = {str(row.get("name", "")) for row in rows}
    required = {"coroutine_thunk", "pcc_virtual_thread", "os_thread"}
    bootstrap: dict[str, Any] | None = None
    if bootstrap_profile_dir is not None:
        profile = build_bootstrap_profile_report(bootstrap_profile_dir, top=top)
        bootstrap = {
            "profile_dir": profile.get("profile_dir"),
            "stage_count": profile.get("stage_count", 0),
            "total_wall_ms": profile.get("total_wall_ms", 0),
            "total_compile_wall_ms": profile.get("total_compile_wall_ms", 0),
            "total_publish_barrier_ms": profile.get("total_publish_barrier_ms", 0),
            "top_phases": profile.get("top_phases", []),
        }
    return {
        "schema": "pcc.diagnostics.virtual_thread_comparison.v1",
        "iterations": _as_int(probe_data.get("iterations")),
        "rows": rows,
        "bootstrap": bootstrap,
        "verdict": {
            "comparison_gate_complete": required.issubset(names),
            "production_virtual_threads": True,
            "production_scope": (
                "generated stackless virtual threads with typed continuation "
                "resume, saved generator frame slots, carrier-local work "
                "stealing, vthread-aware lock/event/condition/semaphore "
                "parking, and fd-poller suspension points"
            ),
            "limitations": [
                "not JVM/Loom native-stack copying; suspension points are explicit/generated state-machine points",
                "socket/file module-specific async wrappers are not present yet; current production path is the fd poller API plus file pin diagnostics",
            ],
        },
    }


def format_virtual_thread_comparison_report(report: dict[str, Any]) -> str:
    lines = [
        "pcc virtual-thread comparison report",
        f"iterations: {report.get('iterations', 0)}",
        "production_virtual_threads: "
        + str(
            bool(report.get("verdict", {}).get("production_virtual_threads"))
        ).lower(),
        "",
        (
            "workload              ops      wall_ms  latency_us  "
            "throughput_ops_s  rss_delta_kb  gc_pause_us  pin_rate_1k"
        ),
    ]
    for row in report.get("rows", []):
        lines.append(
            f"{str(row.get('name', '')):<20} "
            f"{_as_int(row.get('operations')):>6} "
            f"{float(row.get('wall_ms', 0.0)):>12.3f} "
            f"{float(row.get('latency_us_per_op', 0.0)):>11.3f} "
            f"{float(row.get('throughput_ops_per_s', 0.0)):>17.1f} "
            f"{_as_int(row.get('rss_delta_kb')):>13} "
            f"{_as_int(row.get('gc_pause_us')):>12} "
            f"{float(row.get('pinning_rate_per_1k_ops', 0.0)):>12.3f}"
        )
    bootstrap = report.get("bootstrap")
    if isinstance(bootstrap, dict):
        lines.extend(
            [
                "",
                "bootstrap impact",
                f"stage_count: {bootstrap.get('stage_count', 0)}",
                f"total_wall_ms: {bootstrap.get('total_wall_ms', 0)}",
                f"total_compile_wall_ms: {bootstrap.get('total_compile_wall_ms', 0)}",
                (
                    "total_publish_barrier_ms: "
                    + str(bootstrap.get("total_publish_barrier_ms", 0))
                ),
            ]
        )
        top = bootstrap.get("top_phases", [])
        if top:
            lines.append("top_phases:")
            for phase in top:
                lines.append(f"  {phase.get('name', '')}: {phase.get('ms', 0)} ms")
    remaining = report.get("verdict", {}).get("remaining", [])
    if remaining:
        lines.append("")
        lines.append("remaining:")
        for item in remaining:
            lines.append(f"- {item}")
    limitations = report.get("verdict", {}).get("limitations", [])
    if limitations:
        lines.append("")
        lines.append("limitations:")
        for item in limitations:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def dumps_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
