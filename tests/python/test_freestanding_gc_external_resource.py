from __future__ import annotations

import platform
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
PORT_SOURCE = RUNTIME_DIR / "py" / "freestanding_gc_external_resource.py"

PUBLIC_SYMBOLS = {
    "pcc_gc_external_resource_register",
    "pcc_gc_external_resource_retain",
    "pcc_gc_external_resource_release_after_fence",
    "pcc_gc_external_resource_mark_fence_complete",
    "pcc_gc_external_resource_poll",
    "pcc_gc_external_resource_backend",
    "pcc_gc_external_resource_active_count",
    "pcc_gc_external_resource_pending_count",
    "pcc_gc_external_resource_release_count",
    "pcc_gc_external_resource_release_failure_count",
    "pcc_gc_external_resource_last_release_error",
    "pcc_gc_external_metal_buffer_register",
}


def _compile_ir(tmp_path: Path) -> Path:
    llvm_ir = tmp_path / "freestanding_gc_external_resource.ll"
    pipeline.compile_python(
        str(PORT_SOURCE),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return llvm_ir


def _build_object(tmp_path: Path, emitter: str) -> Path:
    llvm_ir = _compile_ir(tmp_path)
    source = llvm_ir
    if emitter == "self":
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "freestanding_gc_external_resource.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    obj = tmp_path / ("freestanding_gc_external_resource_" + emitter + ".o")
    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _build_driver(tmp_path: Path) -> Path:
    source = tmp_path / "fake_metal_driver.c"
    library = tmp_path / (
        "libfake_metal_driver.dylib"
        if sys.platform == "darwin"
        else "libfake_metal_driver.so"
    )
    source.write_text(
        "#include <stdint.h>\n"
        "int64_t pcc_metal_buffer_runtime_release(void *buffer) {\n"
        "  return (uintptr_t)buffer == 0xCAFEu ? -73 : -74;\n"
        "}\n",
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            *( ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"] ),
            str(source),
            "-o",
            str(library),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return library


def _harness_source() -> str:
    return r"""
#include "py_runtime.h"
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#ifndef PCC_HARNESS_NO_STDIO
#include <stdio.h>
#endif
#include <string.h>

typedef struct ReleaseStats {
    int64_t count;
    int64_t context_frees;
    uint64_t handle_sum;
} ReleaseStats;

static int64_t selected_backend = 0;
static uint64_t reentry_resource_id = 0;
static _Atomic int64_t safepoints = 0;
static _Atomic int64_t threaded_releases = 0;

#ifndef PCC_EXPECTED_METAL_RELEASE_ERROR
#define PCC_EXPECTED_METAL_RELEASE_ERROR (-73)
#endif

int64_t pcc_gc_backend(void) { return selected_backend; }
void pcc_thread_safepoint(void) {
    atomic_fetch_add_explicit(&safepoints, 1, memory_order_relaxed);
}

static int64_t release_device_handle(
    uint64_t native_handle,
    void *opaque_stats
) {
    ReleaseStats *stats = (ReleaseStats *)opaque_stats;
    if (stats != NULL) {
        stats->count++;
        stats->handle_sum += native_handle;
    }
    if (
        reentry_resource_id != 0
        && pcc_gc_external_resource_backend(reentry_resource_id)
           != selected_backend
    ) return -88;
    return native_handle == 0xDEADu ? -55 : 0;
}

static void note_context_free(void *opaque_stats) {
    ReleaseStats *stats = (ReleaseStats *)opaque_stats;
    if (stats != NULL) stats->context_frees++;
}

static int64_t release_threaded(uint64_t native_handle, void *context) {
    (void)native_handle;
    (void)context;
    atomic_fetch_add_explicit(&threaded_releases, 1, memory_order_relaxed);
    return 0;
}

static int check_backend(int64_t backend) {
    selected_backend = backend;
    ReleaseStats stats = {0, 0, 0};
    int64_t released_before = pcc_gc_external_resource_release_count();
    int64_t failures_before =
        pcc_gc_external_resource_release_failure_count();

    if (pcc_gc_external_resource_register(0, 1, release_device_handle, 0, 0)) {
        return 1;
    }
    if (pcc_gc_external_resource_register(1, 0, release_device_handle, 0, 0)) {
        return 2;
    }
    if (pcc_gc_external_resource_register(1, 1, 0, 0, 0)) return 3;

    uint64_t keepalive_id = pcc_gc_external_resource_register(
        PCC_GC_EXTERNAL_RESOURCE_GPU_FENCE,
        0x3000u + (uint64_t)backend,
        release_device_handle,
        &stats,
        NULL
    );
    reentry_resource_id = keepalive_id;
    uint64_t buffer_handle = 0x1000u + (uint64_t)backend;
    uint64_t buffer_id = pcc_gc_external_resource_register(
        PCC_GC_EXTERNAL_RESOURCE_GPU_BUFFER,
        buffer_handle,
        release_device_handle,
        &stats,
        note_context_free
    );
    if (keepalive_id == 0 || buffer_id == 0) return 4;
    if (pcc_gc_external_resource_backend(buffer_id) != backend) return 5;
    if (pcc_gc_external_resource_active_count() != 2) return 6;

    if (pcc_gc_external_resource_retain(buffer_id) != 2) return 7;
    if (pcc_gc_external_resource_release_after_fence(buffer_id) != 1) return 8;
    if (pcc_gc_external_resource_mark_fence_complete(buffer_id) != 0) return 9;
    if (pcc_gc_external_resource_mark_fence_complete(buffer_id) != 0) return 10;
    if (pcc_gc_external_resource_poll() != 0 || stats.count != 0) return 11;
    if (pcc_gc_external_resource_release_after_fence(buffer_id) != 0) return 12;
    if (pcc_gc_external_resource_pending_count() != 1) return 13;
    if (pcc_gc_external_resource_poll() != 1) return 14;
    if (stats.count != 1 || stats.handle_sum != buffer_handle) return 15;
    if (stats.context_frees != 1) return 16;

    reentry_resource_id = 0;
    if (pcc_gc_external_resource_release_after_fence(keepalive_id) != 0) return 17;
    if (pcc_gc_external_resource_poll() != 0) return 18;
    if (pcc_gc_external_resource_mark_fence_complete(keepalive_id) != 0) return 19;
    if (pcc_gc_external_resource_poll() != 1) return 20;

    uint64_t failed_id = pcc_gc_external_resource_register(
        PCC_GC_EXTERNAL_RESOURCE_GPU_BUFFER,
        0xDEADu,
        release_device_handle,
        &stats,
        NULL
    );
    if (failed_id == 0) return 21;
    if (pcc_gc_external_resource_release_after_fence(failed_id) != 0) return 22;
    if (pcc_gc_external_resource_mark_fence_complete(failed_id) != 0) return 23;
    if (pcc_gc_external_resource_poll() != 1) return 24;
    if (pcc_gc_external_resource_release_failure_count() != failures_before + 1) {
        return 25;
    }
    if (pcc_gc_external_resource_last_release_error() != -55) return 26;
    if (pcc_gc_external_resource_active_count() != 0) return 27;
    if (pcc_gc_external_resource_pending_count() != 0) return 28;
    if (pcc_gc_external_resource_release_count() != released_before + 3) return 29;
    if (pcc_gc_external_resource_retain(buffer_id) >= 0) return 30;
    if (pcc_gc_external_resource_release_after_fence(buffer_id) >= 0) return 31;
    if (pcc_gc_external_resource_mark_fence_complete(buffer_id) >= 0) return 32;
    return 0;
}

enum { THREADS = 4, PER_THREAD = 200 };

static void *thread_worker(void *opaque) {
    uintptr_t tid = (uintptr_t)opaque;
    for (uintptr_t i = 0; i < PER_THREAD; i++) {
        uint64_t id = pcc_gc_external_resource_register(
            PCC_GC_EXTERNAL_RESOURCE_GPU_BUFFER,
            0x100000u + tid * PER_THREAD + i,
            release_threaded,
            NULL,
            NULL
        );
        if (id == 0) return (void *)(uintptr_t)1;
        if ((i & 1u) != 0) {
            if (pcc_gc_external_resource_mark_fence_complete(id) != 0) {
                return (void *)(uintptr_t)2;
            }
            if (pcc_gc_external_resource_release_after_fence(id) != 0) {
                return (void *)(uintptr_t)3;
            }
        } else {
            if (pcc_gc_external_resource_release_after_fence(id) != 0) {
                return (void *)(uintptr_t)4;
            }
            if (pcc_gc_external_resource_mark_fence_complete(id) != 0) {
                return (void *)(uintptr_t)5;
            }
        }
        (void)pcc_gc_external_resource_poll();
    }
    return NULL;
}

static int check_threaded(void) {
    selected_backend = 4;
    pthread_t threads[THREADS];
    for (uintptr_t i = 0; i < THREADS; i++) {
        if (pthread_create(&threads[i], NULL, thread_worker, (void *)i) != 0) {
            return 1;
        }
    }
    for (int i = 0; i < THREADS; i++) {
        void *result = NULL;
        if (pthread_join(threads[i], &result) != 0 || result != NULL) return 2;
    }
    while (pcc_gc_external_resource_poll() != 0) {}
    if (atomic_load_explicit(&threaded_releases, memory_order_relaxed)
        != THREADS * PER_THREAD) return 3;
    if (pcc_gc_external_resource_active_count() != 0) return 4;
    if (pcc_gc_external_resource_pending_count() != 0) return 5;
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) return 2;
    const char *phase = argc == 3 ? argv[2] : "all";
#ifndef PCC_HARNESS_NO_STDIO
    setbuf(stdout, NULL);
#endif
    if (strcmp(phase, "all") == 0 || strcmp(phase, "single") == 0) {
        for (int64_t backend = 0; backend <= 4; backend++) {
            int result = check_backend(backend);
#ifndef PCC_HARNESS_NO_STDIO
            printf("%lld:%d\n", (long long)backend, result);
#endif
            if (result != 0) return 10 + result;
        }
    }
    if (strcmp(phase, "all") == 0 || strcmp(phase, "threaded") == 0) {
        int threaded = check_threaded();
#ifndef PCC_HARNESS_NO_STDIO
        printf("threaded:%d\n", threaded);
#endif
        if (threaded != 0) return 80 + threaded;
    }

    if (strcmp(phase, "all") == 0 || strcmp(phase, "metal") == 0) {
        selected_backend = 0;
        int64_t failures_before =
            pcc_gc_external_resource_release_failure_count();
        uint64_t metal_id =
            pcc_gc_external_metal_buffer_register(argv[1], 0xCAFEu);
        if (metal_id == 0) return 90;
        if (pcc_gc_external_resource_release_after_fence(metal_id) != 0) return 91;
        if (pcc_gc_external_resource_mark_fence_complete(metal_id) != 0) return 92;
        if (pcc_gc_external_resource_poll() != 1) return 93;
        if (
            pcc_gc_external_resource_release_failure_count()
            != failures_before + 1
        ) return 94;
        if (
            pcc_gc_external_resource_last_release_error()
            != PCC_EXPECTED_METAL_RELEASE_ERROR
        ) return 95;
#ifndef PCC_HARNESS_NO_STDIO
        puts("metal-adapter:1");
#endif
    }
    return 0;
}
"""


def _build_and_run(
    tmp_path: Path,
    name: str,
    implementation: list[str],
    driver: Path,
    phase: str | None = None,
    *,
    without_host_stdio: bool = False,
    expected_metal_release_error: int = -73,
) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(_harness_source(), encoding="utf-8")
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            "-O0",
            "-pthread",
            *(["-DPCC_HARNESS_NO_STDIO=1"] if without_host_stdio else []),
            "-DPCC_EXPECTED_METAL_RELEASE_ERROR="
            + str(expected_metal_release_error),
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(harness),
            *implementation,
            *( ["-ldl"] if sys.platform.startswith("linux") else [] ),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return subprocess.run(
        [str(executable), str(driver), *( [phase] if phase is not None else [] )],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _expected_output() -> str:
    return "0:0\n1:0\n2:0\n3:0\n4:0\nthreaded:0\nmetal-adapter:1\n"


def _port_metal_release_error() -> int:
    return -73 if sys.platform == "darwin" else -8


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("single", "0:0\n1:0\n2:0\n3:0\n4:0\n"),
        ("threaded", "threaded:0\n"),
        ("metal", "metal-adapter:1\n"),
    ],
)
def test_self_external_resource_phase_boundaries(
    tmp_path: Path, phase: str, expected: str
):
    obj = _build_object(tmp_path, "self")
    driver = _build_driver(tmp_path)
    result = _build_and_run(
        tmp_path,
        "external_resource_self_" + phase,
        [str(obj)],
        driver,
        phase,
        expected_metal_release_error=_port_metal_release_error(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_freestanding_external_resource_under_gc0_to_gc4(
    tmp_path: Path, emitter: str
):
    supported = (
        sys.platform == "darwin" and platform.machine() == "arm64"
    ) or (sys.platform.startswith("linux") and platform.machine() == "x86_64")
    assert supported
    driver = _build_driver(tmp_path)
    obj = _build_object(tmp_path, emitter)
    port = _build_and_run(
        tmp_path,
        "external_resource_port_" + emitter,
        [str(obj)],
        driver,
        expected_metal_release_error=_port_metal_release_error(),
    )
    assert port.returncode == 0, port.stdout + port.stderr
    assert port.stdout == _expected_output()


def test_linux_external_resource_ir_has_no_dynamic_loader_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_sys_platform_text",
        lambda self: "linux",
    )
    llvm_ir = _compile_ir(tmp_path).read_text(encoding="utf-8")

    assert "@dlopen" not in llvm_ir
    assert "@dlsym" not in llvm_ir
    assert "@dlclose" not in llvm_ir


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_linux_external_resource_object_has_no_dynamic_loader_undefined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, emitter: str
) -> None:
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_sys_platform_text",
        lambda self: "linux",
    )
    obj = _build_object(tmp_path, emitter)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    undefined_names = {
        line.split()[-1].lstrip("_")
        for line in undefined.stdout.splitlines()
        if line.strip()
    }
    expected = {
        "calloc",
        "free",
        "malloc",
        "memcpy",
        "pcc_gc_backend",
        "pcc_thread_safepoint",
        "strlen",
    }
    # A Darwin-only lookup is a known-null callee on Linux. The self backend
    # routes a call through null to the runtime-provided trap.
    llvm_ir = (tmp_path / "freestanding_gc_external_resource.ll").read_text(encoding="utf-8")
    if emitter == "self" and re.search(r"call [^\n]*\bnull\(", llvm_ir):
        expected.add("pcc_null_callee_trap")
    assert undefined_names == expected


@pytest.mark.parametrize("emitter", ["llvm", "self"])
def test_freestanding_external_resource_object_has_only_explicit_boundaries(
    tmp_path: Path, emitter: str
):
    obj = _build_object(tmp_path, emitter)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    undefined_names = {
        line.split()[-1].lstrip("_")
        for line in undefined.stdout.splitlines()
        if line.strip()
    }
    expected = {
        "calloc",
        "free",
        "malloc",
        "memcpy",
        "pcc_gc_backend",
        "pcc_thread_safepoint",
        "strlen",
    }
    if sys.platform == "darwin":
        expected.update({"dlclose", "dlopen", "dlsym"})
    assert undefined_names == expected
    symbols = subprocess.run(
        ["nm", "-g", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    defined_names = {
        line.split()[-1].lstrip("_")
        for line in symbols.stdout.splitlines()
        if " T " in line or " t " in line
    }
    assert PUBLIC_SYMBOLS <= defined_names


def test_production_archive_plan_uses_python_external_resource_owner():
    makefile = (RUNTIME_DIR / "Makefile").read_text(encoding="utf-8")
    freestanding_line = makefile.split("FREESTANDING_PY_MODULES =", 1)[1].splitlines()[0]
    assert "freestanding_gc_external_resource" in freestanding_line

    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=RUNTIME_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    archive_lines = [
        line
        for line in plan.stdout.splitlines()
        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line
    ]
    assert len(archive_lines) == 1
    assert "build_py/freestanding_gc_external_resource.o" in archive_lines[0]
    assert "build_py/pcc_gc_external_resource.o" not in archive_lines[0]


def test_built_production_archive_attributes_external_resource_to_python(
    tmp_path: Path, pcc_runtime_archive: Path
):
    members = subprocess.run(
        ["ar", "-t", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert members.returncode == 0, members.stdout + members.stderr
    member_names = members.stdout.splitlines()
    assert "freestanding_gc_external_resource.o" in member_names
    assert "pcc_gc_external_resource.o" not in member_names

    symbols = subprocess.run(
        ["nm", "-A", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert symbols.returncode == 0, symbols.stdout + symbols.stderr
    owners = [
        line
        for line in symbols.stdout.splitlines()
        if " T " in line
        and (
            line.rstrip().endswith(" pcc_gc_external_resource_register")
            or line.rstrip().endswith(" _pcc_gc_external_resource_register")
        )
    ]
    assert len(owners) == 1
    assert ":freestanding_gc_external_resource.o:" in owners[0]

    driver = _build_driver(tmp_path)
    result = _build_and_run(
        tmp_path,
        "external_resource_production_archive",
        [str(pcc_runtime_archive)],
        driver,
        without_host_stdio=True,
        expected_metal_release_error=_port_metal_release_error(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
