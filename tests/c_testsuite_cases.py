from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit
from tests.owned_c_corpus import run_owned_c_corpus
from tests.worker_process import run_worker_process
from tests.corpus_execution_phases import (
    CStageResult, ExecutionPhases, PccCompileResult, run_reference_stages,
)

DEFAULT_TIMEOUT = 10
XDIST_TIMEOUT = 20


@dataclass(frozen=True)
class CTestsuiteCaseConfig:
    native_cflags: tuple[str, ...] = ()
    cpp_args: tuple[str, ...] = ()


def subprocess_env():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def _default_timeout() -> int:
    # pytest-xdist runs many cases in parallel, and each pcc case also spawns
    # an extra worker process. A 10s budget becomes flaky under that load.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return XDIST_TIMEOUT
    return DEFAULT_TIMEOUT


def _host_cc():
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if cc is None:
        raise RuntimeError("host C compiler not found")
    return cc


def _read_case_source(case_path: Path) -> str:
    try:
        return case_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return case_path.read_text(encoding="latin-1")


def _read_case_tags(case_path: Path) -> list[str]:
    tags_path = case_path.parent / (case_path.name + ".tags")
    if not tags_path.is_file():
        return []
    return [tag for tag in tags_path.read_text(encoding="latin-1").split() if tag]


def case_config(case_path: Path) -> CTestsuiteCaseConfig:
    tags = set(_read_case_tags(case_path))
    native_cflags: list[str] = []
    cpp_args: list[str] = []

    if "c11" in tags:
        native_cflags.append("-std=c11")
        cpp_args.append("-std=c11")
    elif "c99" in tags:
        native_cflags.append("-std=c99")
        cpp_args.append("-std=c99")
    elif "c89" in tags:
        native_cflags.append("-std=c89")
        cpp_args.append("-std=c89")

    return CTestsuiteCaseConfig(
        native_cflags=tuple(native_cflags),
        cpp_args=tuple(cpp_args),
    )


def read_expected_output(case_path: Path) -> str:
    expected_path = case_path.parent / (case_path.name + ".expected")
    if expected_path.is_file():
        return expected_path.read_text(encoding="latin-1")
    return ""


@lru_cache(maxsize=None)
def run_native(
    case_path: Path,
    repo_root: Path,
    timeout: int | None = None,
):
    if timeout is None:
        timeout = _default_timeout()
    cc = _host_cc()
    config = case_config(case_path)
    with tempfile.TemporaryDirectory(prefix="c_testsuite_native_") as tmpdir:
        object_path = Path(tmpdir) / "a.o"
        binary = Path(tmpdir) / "a.out"
        commands = (
            ("compile", [cc, *config.native_cflags, "-c", str(case_path),
                         "-o", str(object_path)]),
            ("link", [cc, *config.native_cflags, str(object_path), "-o", str(binary)]),
            ("run", [str(binary)]),
        )
        return run_reference_stages(commands, cwd=repo_root,
                                    env=subprocess_env(), timeout=timeout)


@lru_cache(maxsize=None)
def run_pcc(
    case_path: Path,
    repo_root: Path,
    timeout: int | None = None,
) -> PccCompileResult:
    if timeout is None:
        timeout = _default_timeout()
    del repo_root
    return _run_pcc_worker("run", case_path, timeout)


def _run_pcc_worker(mode: str, case_path: Path, timeout: int) -> PccCompileResult:
    result = run_worker_process(
        _pcc_worker_entry,
        (mode, str(case_path), timeout),
        timeout,
    )
    if result.timed_out:
        return PccCompileResult.from_stages((
            CStageResult("worker", 124, stderr="timeout", completed=False),
        ))
    payload = result.payload
    if payload is None:
        return PccCompileResult.from_stages((CStageResult(
            "worker", 1,
            stderr=f"pcc worker exited without result (exitcode={result.exitcode})",
            completed=False,
        ),))
    return PccCompileResult.from_stages(
        CStageResult(**stage) for stage in payload["stages"]
    )


def _pcc_worker_entry(mode: str, case_path_str: str, timeout: int, conn) -> None:
    phases = ExecutionPhases()
    try:
        case_path = Path(case_path_str)
        unit = TranslationUnit(case_path.name, str(case_path), _read_case_source(case_path))
        config = case_config(case_path)
        cpp_args = config.cpp_args
        evaluator = CEvaluator()
        if mode == "compile":
            phases.begin("compile")
            evaluator.compile_translation_units(
                [unit], base_dir=str(case_path.parent),
                include_dirs=[str(case_path.parent)], cpp_args=cpp_args,
            )
            outcome = phases.complete()
        else:
            result = run_owned_c_corpus(
                evaluator, [unit], base_dir=str(case_path.parent),
                include_dirs=[str(case_path.parent)], cpp_args=cpp_args,
                timeout=timeout, on_stage=phases.begin,
            )
            outcome = phases.complete(result)
    except Exception as exc:
        outcome = phases.fail(exc)
    finally:
        try:
            conn.send({
                "returncode": outcome.returncode,
                "stdout": outcome.stdout,
                "stderr": outcome.stderr,
                "stages": [asdict(stage) for stage in outcome.stages],
            })
        finally:
            conn.close()
