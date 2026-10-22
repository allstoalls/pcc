"""threading.Thread passes its kwargs to the target, as Thread.run does.

The native threading.Thread lowering refused a non-empty ``kwargs=`` with
"native threading.Thread target kwargs are not supported yet" at compile
time, which stopped every Lib/test module importing test.support's
threading_helper (run_concurrently passes kwargs) from compiling, and pcc's
threading provider dropped them in start(). Both now bind the keywords to
the target (functools.partial), so the thread calls target(*args, **kwargs).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
import threading

results = []


def work(a, b=0, *, label):
    results.append((label, a + b))


def main():
    first = threading.Thread(target=work, args=(1,), kwargs={"b": 2, "label": "kw"})
    first.start()
    first.join()
    keywords = {"label": "only"}
    second = threading.Thread(target=work, args=(5,), kwargs=keywords)
    second.start()
    second.join()
    third = threading.Thread(target=work, args=(7,), kwargs=dict(label="dict", b=1))
    third.start()
    third.join()
    print(sorted(results))


main()
'''


@pytest.mark.integration
def test_thread_target_receives_keyword_arguments_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "thread_keywords.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "thread_keywords"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
