from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_pcc_python_runtime()


def _compile_and_run(tmp_path: Path, source: str, env: dict[str, str] | None = None):
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "backend23_probe.c"
    exe = tmp_path / "backend23_probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
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
    assert build.returncode == 0, build.stdout + build.stderr
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, env=run_env)


def test_backend2_production_score_moves_under_alloc_and_step(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        '''
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) return 2;
            pcc_gc_telemetry_reset();
            for (int i = 0; i < 512; i++) {
                PyObject *s = py_str_new("cms-object", 10);
                if (s == 0) return 3;
                pcc_gc_release(s);
                if ((i % 16) == 0) pcc_gc_safepoint();
            }
            (void)pcc_gc_step(512);
            printf("%lld\\n", (long long)pcc_gc_backend2_worker_buffer_score());
            printf("%lld\\n", (long long)pcc_gc_backend2_production_score());
            printf("%lld\\n", (long long)pcc_gc_telemetry(PCC_GC_COUNTER_CMS_WORKBUFFER_SCORE));
            printf("%lld\\n", (long long)pcc_gc_telemetry(PCC_GC_COUNTER_CMS_PRODUCTION_SCORE));
            return 0;
        }
        ''',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    values = [int(x) for x in proc.stdout.strip().splitlines()]
    assert len(values) == 4
    assert values[0] > 0
    assert values[1] >= values[0]
    assert values[2] == values[0]
    assert values[3] == values[1]


def test_backend3_minor_productivity_and_remembered_update_score(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        '''
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);
            PyObject *lst = py_list_new(0);
            if (lst == 0) return 3;
            pcc_gc_store_root(&root, lst);
            pcc_gc_release(lst);

            for (int i = 0; i < 512; i++) {
                PyObject *s = py_str_new("gen-object", 10);
                if (s == 0) return 4;
                py_list_append(root, s);
                pcc_gc_release(s);
                if ((i % 8) == 0) (void)pcc_gc_step(32);
            }

            (void)pcc_gc_step(1024);
            printf("%lld\\n", (long long)pcc_gc_backend3_minor_productivity_score());
            printf("%lld\\n", (long long)pcc_gc_backend3_remembered_update_score());
            printf("%lld\\n", (long long)pcc_gc_telemetry(PCC_GC_COUNTER_GEN_MINOR_PRODUCTIVITY_SCORE));
            printf("%lld\\n", (long long)pcc_gc_telemetry(PCC_GC_COUNTER_GEN_REMEMBERED_UPDATE_SCORE));

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            return 0;
        }
        ''',
        env={
            "PCC_GC_MINOR_HEAP_SIZE": "2048",
            "PCC_GC_MINOR_ALLOC_MAX": "256",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    values = [int(x) for x in proc.stdout.strip().splitlines()]
    assert len(values) == 4
    assert values[0] > 0
    assert values[1] > 0
    assert values[2] == values[0]
    assert values[3] == values[1]


def test_backend23_public_symbols_are_wired():
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(encoding="utf-8")
    py_src = (RUNTIME_DIR / "py" / "py_gc_telemetry.py").read_text(encoding="utf-8")
    abi = (REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "runtime_abi.py").read_text(encoding="utf-8")

    assert "PCC_GC_COUNTER_CMS_PRODUCTION_SCORE" in header
    assert "PCC_GC_COUNTER_GEN_MINOR_PRODUCTIVITY_SCORE" in header
    assert '@c_abi_export("pcc_gc_backend2_production_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend3_minor_productivity_score")' in py_src
    assert '"pcc_gc_backend2_production_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend3_minor_productivity_score": (_I64, [], False)' in abi
