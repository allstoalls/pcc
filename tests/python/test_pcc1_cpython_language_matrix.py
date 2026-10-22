"""Finite current-pcc1 language matrix against a separate CPython oracle.

This is the compact release gate for ``CPY-P0-LANGUAGE-SEMANTIC-COMPATIBILITY``.
It does not replace the exhaustive host-pcc/pcc1 manifests in
``test_self_host_oracle_diff.py``.  Instead it keeps one deliberately small
cross-section cheap enough to run under every production GC backend while
still crossing the representation, object-model, exception and suspended-frame
boundaries most likely to diverge between host pcc and pcc1.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from pcc1_gate import find_current_pcc1, repo_root
from tests.py_corpus_support import PYTHON_CORPUS_CASES


REPO = repo_root()


@dataclass(frozen=True)
class LanguageCase:
    name: str
    source: str
    expected_stdout: bytes | None = None
    expected_status: int = 0


_CORPUS_NAMES = (
    "phase2__bignum_object_boundaries",
    "phase2__for_tuple_target",
    "phase3__property_with_validation",
    "phase3__raise_from",
    "phase3__finally_with_raise",
)
_CORPUS_BY_NAME = {case.name: case for case in PYTHON_CORPUS_CASES}


def _corpus_case(name: str) -> LanguageCase:
    case = _CORPUS_BY_NAME[name]
    return LanguageCase(
        name=name,
        source=case.source,
        expected_stdout=case.expected_stdout,
        expected_status=case.expected_status,
    )


CUSTOM_CASES = (
    LanguageCase(
        "generator_resume_state",
        """
        def values():
            yield 3
            yield 5
            return 8

        it = values()
        print(next(it))
        print(it.send(None))
        try:
            next(it)
        except StopIteration as exc:
            print(exc.value)
        """,
    ),
    LanguageCase(
        "coroutine_send_result",
        """
        async def answer():
            return 42

        task = answer()
        try:
            task.send(None)
        except StopIteration as exc:
            print(exc.value)
        """,
    ),
    LanguageCase(
        "weakref_and_finalizer",
        """
        import gc
        from weakref import ref

        events = []

        class Item:
            def __del__(self):
                events.append("del")

        item = Item()
        weak = ref(item)
        del item
        gc.collect()
        print(weak() is None)
        print(events)
        """,
    ),
    LanguageCase(
        "thread_local_exception_state",
        """
        from threading import Lock, Thread

        results = []
        lock = Lock()

        def worker(label: str) -> None:
            try:
                raise ValueError(label)
            except ValueError as exc:
                lock.acquire()
                results.append(str(exc))
                lock.release()

        first = Thread(target=worker, args=("first",))
        second = Thread(target=worker, args=("second",))
        first.start()
        second.start()
        first.join()
        second.join()
        results.sort()
        print(results)
        """,
    ),
    LanguageCase(
        "import_and_reflection",
        """
        import json

        class Box:
            def __init__(self, value: int):
                self.value = value

        box = Box(7)
        payload = json.loads('{"items": [2, 3, 5]}')
        print(getattr(box, "value"))
        print(sum(payload["items"]))
        print(type(box).__name__)
        """,
    ),
)


LANGUAGE_CASES = tuple(_corpus_case(name) for name in _CORPUS_NAMES) + CUSTOM_CASES


def _source(case: LanguageCase) -> str:
    return textwrap.dedent(case.source).lstrip()


def _run_cpython(case: LanguageCase, directory: Path) -> subprocess.CompletedProcess[bytes]:
    source = directory / (case.name + ".py")
    source.write_text(_source(case), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(source)],
        cwd=str(directory),
        capture_output=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("case", LANGUAGE_CASES, ids=lambda case: case.name)
def test_language_matrix_has_separate_cpython_oracle(
    tmp_path: Path,
    case: LanguageCase,
) -> None:
    result = _run_cpython(case, tmp_path)
    assert result.returncode == case.expected_status, result.stderr.decode(
        "utf-8", errors="replace"
    )
    if case.expected_stdout is not None:
        assert result.stdout == case.expected_stdout


@pytest.fixture(scope="session")
def current_pcc1() -> Path:
    binary = find_current_pcc1(REPO)
    if binary is None:
        pytest.fail(
            "no receipt-current pcc1 exists after the pcc1 gate provisioned "
            "stage1; inspect the stage1 build error"
        )
    return binary


@pytest.fixture(
    scope="module",
    params=LANGUAGE_CASES,
    ids=lambda case: case.name,
)
def compiled_language_case(
    request,
    tmp_path_factory,
    current_pcc1: Path,
    pcc_runtime_archive: Path,
) -> tuple[LanguageCase, Path, bytes]:
    case: LanguageCase = request.param
    directory = tmp_path_factory.mktemp("pcc1-language-" + case.name)
    source = directory / "program.py"
    executable = directory / "program.out"
    source.write_text(_source(case), encoding="utf-8")

    oracle = _run_cpython(case, directory)
    assert oracle.returncode == case.expected_status, oracle.stderr.decode(
        "utf-8", errors="replace"
    )
    if case.expected_stdout is not None:
        assert oracle.stdout == case.expected_stdout

    env = os.environ.copy()
    env["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    result = subprocess.run(
        [
            str(current_pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"current pcc1 failed to compile language case {case.name}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert executable.is_file()

    inspect_cmd = (
        ["otool", "-L", str(executable)]
        if sys.platform == "darwin"
        else ["readelf", "-d", str(executable)]
    )
    inspected = subprocess.run(
        inspect_cmd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    dependencies = (inspected.stdout + inspected.stderr).lower()
    assert "libpython" not in dependencies
    assert "python.framework" not in dependencies
    return case, executable, oracle.stdout


@pytest.mark.pcc_gate(probe="pcc1")
@pytest.mark.parametrize("gc_backend", range(5), ids=lambda value: f"gc{value}")
def test_current_pcc1_self_no_libpython_language_matrix_gc0_to_gc4(
    compiled_language_case: tuple[LanguageCase, Path, bytes],
    gc_backend: int,
) -> None:
    case, executable, expected_stdout = compiled_language_case
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = str(gc_backend)
    env["PCC_WITH_THREADS"] = "1"
    result = subprocess.run(
        [str(executable)],
        cwd=str(executable.parent),
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == case.expected_status, (
        f"{case.name} failed under GC{gc_backend}\n"
        f"stdout:\n{result.stdout.decode('utf-8', errors='replace')}\n"
        f"stderr:\n{result.stderr.decode('utf-8', errors='replace')}"
    )
    assert result.stdout == expected_stdout
