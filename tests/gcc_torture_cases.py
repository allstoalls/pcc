from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import warnings
from concurrent.futures import ThreadPoolExecutor
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

# GCC torture cases run under pytest-xdist and then spawn an extra worker
# process per case. A 10s budget is too tight under load and causes flaky
# false timeouts for cases that normally return a deterministic nonzero code.
DEFAULT_TIMEOUT = 20
_NATIVE_CC_FLAGS = (
    "-Wno-error=implicit-function-declaration",
    "-Wno-error=implicit-int",
)


# Pin the comparison dialect rather than inheriting the host compiler default.
# GNU17 supports C99 main fall-through and for declarations while retained
# legacy cases explicitly request GNU89. GCC 15 changed its default to GNU23:
# https://gcc.gnu.org/gcc-15/porting_to.html
# This is a language/link configuration adapter, not a DejaGnu replacement:
# https://gcc.gnu.org/onlinedocs/gccint/Directives.html
_DEFAULT_LANGUAGE_STANDARD = "-std=gnu17"
_COMMENT_OR_LITERAL = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
    re.DOTALL,
)
_DG_START = re.compile(r"\{\s*(dg-[a-z-]+)\b")
_OPTION_STRING = re.compile(r'\s*("(?:\\.|[^"\\])*")(.*)', re.DOTALL)


class GccTortureDirectiveWarning(UserWarning):
    """A retained corpus directive is outside this comparison adapter's scope."""


@dataclass(frozen=True)
class GccTortureComparisonOptions:
    standard: str
    link_args: tuple[str, ...]
    unmodeled_directives: tuple[str, ...] = ()


def _gcc_directives(source: str):
    """Read balanced directive comments without matching C string literals."""
    for token in _COMMENT_OR_LITERAL.finditer(source):
        comment = token.group()
        if not comment.startswith(("/*", "//")):
            continue
        for start in _DG_START.finditer(comment):
            depth = 1
            quoted = False
            escaped = False
            end = start.end()
            while end < len(comment) and depth:
                char = comment[end]
                if escaped:
                    escaped = False
                elif char == "\\" and quoted:
                    escaped = True
                elif char == '"':
                    quoted = not quoted
                elif not quoted:
                    if char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                end += 1
            if depth:
                raise ValueError("unterminated GCC test directive: " + start.group(1))
            yield start.group(1), comment[start.end() : end - 1].strip()


def gcc_torture_comparison_options(
    source: str, *, platform: str | None = None
) -> GccTortureComparisonOptions:
    """Share the explicit dialect and Linux libm across reference/PCC lanes.

    Only unconditional language-standard options are interpreted. Target
    selectors, prerequisites and other compiler options remain visible as
    unmodeled directives; they must not become unconditional flags or implied
    qualification. Conditional standards and malformed options fail explicitly.
    Unknown standards are passed unchanged to each compiler, without fallback.
    """
    standard = _DEFAULT_LANGUAGE_STANDARD
    base_standard = None
    additional_standards = []
    unmodeled = []
    seen_options = False
    for name, body in _gcc_directives(source):
        if name not in ("dg-options", "dg-additional-options"):
            if (
                name.startswith("dg-require-")
                or name in ("dg-skip-if", "dg-xfail-if", "dg-add-options")
                or (name == "dg-do" and "target" in body)
            ):
                unmodeled.append(name + " " + body)
            continue
        match = _OPTION_STRING.fullmatch(body)
        if match is None:
            if "-std" in body or "-ansi" in body:
                raise ValueError(
                    "ambiguous GCC language-standard directive: " + name + " " + body
                )
            unmodeled.append(name + " " + body)
            continue
        options = shlex.split(shlex.split(match.group(1))[0])
        standards = []
        other_options = []
        for option in options:
            if option == "-ansi":
                standards.append("-std=c89")
            elif option.startswith("-std="):
                if not option[5:] or any(char.isspace() for char in option[5:]):
                    raise ValueError("invalid GCC language-standard option: " + option)
                standards.append(option)
            elif option == "-std":
                raise ValueError("GCC language-standard option requires -std=VALUE")
            else:
                other_options.append(option)
        if match.group(2).strip():
            if standards:
                raise ValueError(
                    "conditional GCC language-standard directive requires "
                    "target evaluation: " + name + " " + body
                )
            unmodeled.append(name + " " + body)
            continue
        if other_options:
            unmodeled.append(name + " " + shlex.join(other_options))
        if name == "dg-options":
            if seen_options:
                raise ValueError("multiple unconditional dg-options directives are ambiguous")
            seen_options = True
            base_standard = standards[-1] if standards else None
        else:
            additional_standards.extend(standards)
    # dg-options replaces defaults; additional options are appended afterward.
    if base_standard is not None:
        standard = base_standard
    if additional_standards:
        standard = additional_standards[-1]
    target_platform = sys.platform if platform is None else platform
    link_args = ("-lm",) if target_platform.startswith("linux") else ()
    return GccTortureComparisonOptions(standard, link_args, tuple(unmodeled))


def gcc_torture_case_options(case_path: Path) -> GccTortureComparisonOptions:
    options = gcc_torture_comparison_options(_read_case_source(case_path))
    if options.unmodeled_directives:
        warnings.warn(
            f"{case_path.name}: comparison does not model GCC directives: "
            + "; ".join(options.unmodeled_directives),
            GccTortureDirectiveWarning,
            stacklevel=2,
        )
    return options


def subprocess_env():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


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


@lru_cache(maxsize=None)
def compile_native(case_path: Path, repo_root: Path, timeout: int = DEFAULT_TIMEOUT):
    cc = _host_cc()
    options = gcc_torture_case_options(case_path)
    with tempfile.TemporaryDirectory(prefix="gcc_torture_native_compile_") as tmpdir:
        binary = Path(tmpdir) / "a.out"
        return subprocess.run(
            [
                cc, options.standard, *_NATIVE_CC_FLAGS, str(case_path),
                "-o", str(binary), *options.link_args,
            ],
            cwd=repo_root,
            env=subprocess_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )


@lru_cache(maxsize=None)
def run_native(case_path: Path, repo_root: Path, timeout: int = DEFAULT_TIMEOUT):
    cc = _host_cc()
    options = gcc_torture_case_options(case_path)
    with tempfile.TemporaryDirectory(prefix="gcc_torture_native_") as tmpdir:
        object_path = Path(tmpdir) / "a.o"
        binary = Path(tmpdir) / "a.out"
        commands = (
            ("compile", [cc, options.standard, *_NATIVE_CC_FLAGS,
                         "-c", str(case_path), "-o", str(object_path)]),
            ("link", [cc, options.standard, str(object_path), "-o", str(binary),
                      *options.link_args]),
            ("run", [str(binary)]),
        )
        return run_reference_stages(commands, cwd=repo_root,
                                    env=subprocess_env(), timeout=timeout)


@lru_cache(maxsize=None)
def compile_pcc(case_path: Path, timeout: int = DEFAULT_TIMEOUT) -> PccCompileResult:
    return _run_pcc_worker("compile", case_path, timeout)


@lru_cache(maxsize=None)
def run_pcc(
    case_path: Path, repo_root: Path, timeout: int = DEFAULT_TIMEOUT
) -> PccCompileResult:
    del repo_root
    return _run_pcc_worker("run", case_path, timeout)


def run_native_and_pcc(
    case_path: Path,
    repo_root: Path,
    timeout: int = DEFAULT_TIMEOUT,
):
    """Run the native clang build and the pcc build concurrently.

    Both inner functions are subprocess-driven (GIL-free), so a 2-thread
    pool cuts per-test latency roughly in half. Returns (native, pcc) in
    the same order the serial callers expect.
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_native = ex.submit(run_native, case_path, repo_root, timeout)
        f_pcc = ex.submit(run_pcc, case_path, repo_root, timeout)
        return f_native.result(), f_pcc.result()


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
        options = gcc_torture_case_options(case_path)
        # The oracle's host -lm is supplied by PCC's own runtime in this lane.
        if options.link_args not in ((), ("-lm",)):
            raise RuntimeError("unsupported owned corpus link dependency: " + repr(options.link_args))
        cpp_args = [options.standard]
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
