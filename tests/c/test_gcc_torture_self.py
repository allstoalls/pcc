from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit
from tests.owned_c_corpus import run_owned_c_corpus
from tests.corpus_execution_phases import ExecutionPhases
from tests.corpus_manifest import runtime_cases_except
from tests.gcc_torture_cases import (
    DEFAULT_TIMEOUT,
    PccCompileResult,
    _read_case_source,
    gcc_torture_case_options,
    run_native,
    run_pcc,
)
from tests.self_backend_c_testsuite_common import (
    REPO_ROOT,
    assert_result_triplet_matches,
)

GCC_TORTURE_DIR = REPO_ROOT / "projects" / "gcc-torture-execute"
GCC_TORTURE_MANIFEST_PATH = REPO_ROOT / "tests" / "gcc_torture_manifest.json"
GCC_TORTURE_MANIFEST = json.loads(GCC_TORTURE_MANIFEST_PATH.read_text(encoding="utf-8"))

# Keep this as the full runtime-exact-match bucket so self-backend promotion
# evidence stays broad. Use short range scouts before running this formal gate;
# the full parametrized run is intentionally expensive on the supported host.
GCC_TORTURE_SELF_BACKEND_EXACT_MATCH_CASES = tuple(
    GCC_TORTURE_MANIFEST.get("runtime_exact_match", [])
)
GCC_TORTURE_SELF_BACKEND_RETURNCODE_CASES = tuple(
    GCC_TORTURE_MANIFEST.get("runtime_returncode_match_only", [])
)
GCC_TORTURE_SELF_BACKEND_COMPARISON_CASES = (
    GCC_TORTURE_SELF_BACKEND_EXACT_MATCH_CASES
    + tuple(runtime_cases_except(
        GCC_TORTURE_MANIFEST, {"runtime_exact_match", "runtime_returncode_match_only"}
    ))
)

pytestmark = pytest.mark.integration


def _case_params(cases):
    return [
        pytest.param(
            relative_path,
            marks=pytest.mark.xdist_group(name=f"gcc_torture:{relative_path}"),
        )
        for relative_path in cases
    ]


def _case_path(relative_path: str) -> Path:
    return GCC_TORTURE_DIR / relative_path


@lru_cache(maxsize=None)
def _run_backend(
    case_path: Path,
    *,
    backend: str,
    timeout: int = DEFAULT_TIMEOUT,
    allow_unimplemented_backend: bool = False,
):
    unit = TranslationUnit(case_path.name, str(case_path), _read_case_source(case_path))
    phases = ExecutionPhases()
    try:
        options = gcc_torture_case_options(case_path)
        # The oracle's host -lm is supplied by PCC's own runtime in this lane.
        # Do not silently discard any future caller-selected link dependency.
        if options.link_args not in ((), ("-lm",)):
            raise RuntimeError("unsupported owned corpus link dependency: " + repr(options.link_args))
        evaluator = CEvaluator(
            backend=backend,
            allow_unimplemented_backend=allow_unimplemented_backend,
        )
        result = run_owned_c_corpus(evaluator,
            [unit],
            base_dir=str(case_path.parent),
            include_dirs=[str(case_path.parent)],
            timeout=timeout,
            cpp_args=[options.standard],
            on_stage=phases.begin,
        )
        return phases.complete(result)
    except Exception as exc:
        return phases.fail(exc)


def _run_self_backend(case_path: Path, timeout: int = DEFAULT_TIMEOUT):
    return _run_backend(
        case_path,
        backend="self",
        timeout=timeout,
        allow_unimplemented_backend=True,
    )


def _run_llvm_backend(case_path: Path, timeout: int = DEFAULT_TIMEOUT):
    """Historical label: run_pcc now uses the default owned self backend.

    Keep the existing coverage/node identities; this lane is not evidence of
    independent LLVM execution or native pcc1 ownership. An unsupported
    PCC_BACKEND selection fails through the ordinary worker result.
    """
    return run_pcc(case_path, REPO_ROOT, timeout)


@pytest.mark.parametrize(
    "relative_path", _case_params(GCC_TORTURE_SELF_BACKEND_COMPARISON_CASES)
)
def test_gcc_torture_self_backend_matches_native_and_llvm_exactly(relative_path):
    case_path = _case_path(relative_path)
    assert case_path.is_file(), f"missing gcc torture case: {case_path}"

    native_result = run_native(case_path, REPO_ROOT)
    llvm_result = _run_llvm_backend(case_path)
    self_result = _run_self_backend(case_path)

    native_result.require_execution("host reference")
    llvm_result.require_execution("default owned pcc")
    self_result.require_execution("explicit owned self")

    assert_result_triplet_matches(
        relative_path, "self", self_result, "native", native_result
    )
    assert_result_triplet_matches(
        relative_path, "self", self_result, "llvm", llvm_result
    )


@pytest.mark.parametrize(
    "relative_path", _case_params(GCC_TORTURE_SELF_BACKEND_RETURNCODE_CASES)
)
def test_gcc_torture_self_backend_returncode_matches_native_and_llvm(relative_path):
    case_path = _case_path(relative_path)
    assert case_path.is_file(), f"missing gcc torture case: {case_path}"

    native_result = run_native(case_path, REPO_ROOT)
    llvm_result = _run_llvm_backend(case_path)
    self_result = _run_self_backend(case_path)

    native_result.require_execution("host reference")
    llvm_result.require_execution("default owned pcc")
    self_result.require_execution("explicit owned self")

    assert self_result.returncode == native_result.returncode, (
        f"{relative_path} return code mismatch:\n"
        f"native={native_result.returncode}\n"
        f"self={self_result.returncode}\n"
        f"self stderr:\n{self_result.stderr}"
    )
    assert self_result.returncode == llvm_result.returncode, (
        f"{relative_path} return code mismatch:\n"
        f"llvm={llvm_result.returncode}\n"
        f"self={self_result.returncode}\n"
        f"self stderr:\n{self_result.stderr}"
    )
