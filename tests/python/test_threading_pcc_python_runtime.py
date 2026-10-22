from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime


def test_threaded_pcc_python_runtime_selects_owned_pthread_kernel_source() -> None:
    runtime = Path(__file__).absolute().parents[2] / "pcc" / "runtime"
    makefile = (runtime / "Makefile").read_text(encoding="utf-8")

    assert "ifeq ($(PCC_WITH_THREADS),1)" in makefile
    assert "PY_MODULES += freestanding_thread_kernel_pthread" in makefile
    assert "FREESTANDING_PY_MODULES += freestanding_thread_kernel" in makefile


def test_default_pcc_python_thread_objects_remain_synchronous(
    tmp_path,
    monkeypatch,
) -> None:
    from pcc.frontends.python.pipeline import compile_python

    runtime = cached_pcc_python_runtime()
    archive = runtime / "libpy_runtime_pcc_py.a"
    source = tmp_path / "pcc_python_thread_sync.py"
    executable = tmp_path / "pcc_python_thread_sync"
    source.write_text(
        textwrap.dedent(
            """
            from threading import Thread

            values = []

            def worker() -> None:
                values.append(7)

            def main() -> None:
                thread = Thread(target=worker)
                print(len(values))
                thread.start()
                print(len(values))
                thread.join()

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PCC_WITH_THREADS", "0")
    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(archive),
    )

    completed = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines() == ["0", "1"]
