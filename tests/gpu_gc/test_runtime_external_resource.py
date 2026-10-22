from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_pcc_python_runtime()


def test_external_device_resources_use_one_runtime_registry_across_gc0_to_gc4(
    tmp_path: Path,
):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "external_resource_probe.c"
    exe = tmp_path / "external_resource_probe.out"
    driver_src = tmp_path / "fake_metal_driver.c"
    driver_lib = tmp_path / (
        "libfake_metal_driver.dylib" if sys.platform == "darwin" else "libfake_metal_driver.so"
    )
    driver_src.write_text(
        textwrap.dedent(
            """
            #include <stdint.h>

            int64_t pcc_metal_buffer_runtime_release(void *buffer) {
                return (uintptr_t)buffer == 0xCAFEu ? -73 : -74;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    driver_build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            *(
                ["-dynamiclib"]
                if sys.platform == "darwin"
                else ["-shared", "-fPIC"]
            ),
            str(driver_src),
            "-o",
            str(driver_lib),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert driver_build.returncode == 0, driver_build.stdout + driver_build.stderr
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdint.h>
            #include <stdio.h>

            typedef struct ReleaseStats {
                int64_t count;
                uint64_t handle_sum;
            } ReleaseStats;

            static int64_t release_device_handle(
                uint64_t native_handle,
                void *opaque_stats
            ) {
                ReleaseStats *stats = (ReleaseStats *)opaque_stats;
                stats->count++;
                stats->handle_sum += native_handle;
                return 0;
            }

            static int check_backend(int64_t backend) {
                if (pcc_gc_set_backend(backend) != 0) return 0;
                ReleaseStats stats = {0, 0};
                int64_t released_before = pcc_gc_external_resource_release_count();

                uint64_t buffer_handle = 0x1000u + (uint64_t)backend;
                uint64_t fence_handle = 0x2000u + (uint64_t)backend;
                uint64_t buffer_id = pcc_gc_external_resource_register(
                    PCC_GC_EXTERNAL_RESOURCE_GPU_BUFFER,
                    buffer_handle,
                    release_device_handle,
                    &stats,
                    NULL
                );
                uint64_t fence_id = pcc_gc_external_resource_register(
                    PCC_GC_EXTERNAL_RESOURCE_GPU_FENCE,
                    fence_handle,
                    release_device_handle,
                    &stats,
                    NULL
                );
                if (buffer_id == 0 || fence_id == 0) return 0;
                if (pcc_gc_external_resource_backend(buffer_id) != backend) return 0;
                if (pcc_gc_external_resource_backend(fence_id) != backend) return 0;
                if (pcc_gc_external_resource_active_count() != 2) return 0;

                if (pcc_gc_external_resource_retain(buffer_id) != 2) return 0;
                if (pcc_gc_external_resource_release_after_fence(buffer_id) != 1) return 0;
                if (pcc_gc_external_resource_mark_fence_complete(buffer_id) != 0) return 0;
                pcc_gc_safepoint();
                if (stats.count != 0) return 0;
                if (pcc_gc_external_resource_release_after_fence(buffer_id) != 0) return 0;
                pcc_gc_safepoint();
                if (stats.count != 1 || stats.handle_sum != buffer_handle) return 0;

                if (pcc_gc_external_resource_release_after_fence(fence_id) != 0) return 0;
                (void)pcc_gc_collect(0);
                if (stats.count != 1) return 0;
                if (pcc_gc_external_resource_mark_fence_complete(fence_id) != 0) return 0;
                (void)pcc_gc_collect(0);
                if (stats.count != 2) return 0;
                if (stats.handle_sum != buffer_handle + fence_handle) return 0;

                if (pcc_gc_external_resource_active_count() != 0) return 0;
                if (pcc_gc_external_resource_pending_count() != 0) return 0;
                if (pcc_gc_external_resource_release_count() != released_before + 2) return 0;
                if (pcc_gc_external_resource_release_after_fence(buffer_id) >= 0) return 0;
                if (pcc_gc_external_resource_mark_fence_complete(fence_id) >= 0) return 0;
                return 1;
            }

            int main(int argc, char **argv) {
                if (argc != 2) return 2;
                for (int64_t backend = 0; backend <= 4; backend++) {
                    int ok = check_backend(backend);
                    printf("%lld:%d\\n", (long long)backend, ok);
                    if (!ok) return (int)(10 + backend);
                }

                if (pcc_gc_set_backend(0) != 0) return 30;
                int64_t failures_before =
                    pcc_gc_external_resource_release_failure_count();
                uint64_t metal_id = pcc_gc_external_metal_buffer_register(
                    argv[1], 0xCAFEu
                );
                if (metal_id == 0) return 31;
                if (pcc_gc_external_resource_release_after_fence(metal_id) != 0) {
                    return 32;
                }
                if (pcc_gc_external_resource_mark_fence_complete(metal_id) != 0) {
                    return 33;
                }
                pcc_gc_safepoint();
                if (
                    pcc_gc_external_resource_release_failure_count()
                    != failures_before + 1
                ) return 34;
                if (pcc_gc_external_resource_last_release_error() != -73) return 35;
                printf("metal-adapter:1\\n");
                return 0;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    link_args = ["-ldl"] if sys.platform.startswith("linux") else []
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-lm",
            *link_args,
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    proc = subprocess.run(
        [str(exe), str(driver_lib)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == [
        "0:1",
        "1:1",
        "2:1",
        "3:1",
        "4:1",
        "metal-adapter:1",
    ]


def test_external_resource_registry_is_opaque_and_shared_by_both_runtime_paths():
    python_kernel = (
        RUNTIME_DIR / "py" / "freestanding_gc_external_resource.py"
    ).read_text(encoding="utf-8")
    py_runtime = (RUNTIME_DIR / "py" / "py_obj.py").read_text(encoding="utf-8")
    makefile = (RUNTIME_DIR / "Makefile").read_text(encoding="utf-8")

    assert py_runtime.count("pcc_gc_external_resource_poll()") >= 2
    assert "freestanding_gc_external_resource" in makefile.split(
        "FREESTANDING_PY_MODULES =", 1
    )[1].splitlines()[0]
    assert "def pcc_gc_external_resource_register(" in python_kernel
    assert "def pcc_gc_external_resource_poll(" in python_kernel
