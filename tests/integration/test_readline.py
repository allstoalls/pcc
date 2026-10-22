import os
from contextlib import contextmanager
import fcntl
import hashlib
import subprocess
import tempfile

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import (
    collect_cpp_args,
    collect_translation_units,
    translation_unit_include_dirs,
)
from tests.parallel_jobs import translation_unit_jobs


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
PROJECTS_DIR = os.path.join(PROJECT_DIR, "projects")
READLINE_DIR = os.path.join(PROJECTS_DIR, "readline-8.2")
READLINE_TEST_MAIN = os.path.join(PROJECTS_DIR, "test_readline_main.c")
READLINE_HISTORY_TEST_MAIN = os.path.join(
    PROJECTS_DIR, "test_readline_history_main.c"
)
READLINE_MAKEFILE = os.path.join(READLINE_DIR, "Makefile")
READLINE_MAKE_GOAL = "libreadline.a"
READLINE_HISTORY_MAKE_GOAL = "libhistory.a"
READLINE_HISTORY_LINK_ARGS = ["-ltermcap"]
READLINE_CONFIG_LOCK = os.path.join(
    tempfile.gettempdir(),
    f"pcc-readline-build-{hashlib.sha256(READLINE_DIR.encode('utf-8')).hexdigest()[:16]}.lock",
)

pytestmark = pytest.mark.xdist_group(name="vendor_builds")


def _make_env():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


@contextmanager
def _readline_build_lock():
    os.makedirs(os.path.dirname(READLINE_CONFIG_LOCK), exist_ok=True)
    with open(READLINE_CONFIG_LOCK, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)


def _ensure_readline_configured():
    if os.path.isfile(READLINE_MAKEFILE):
        return

    with _readline_build_lock():
        if os.path.isfile(READLINE_MAKEFILE):
            return

        configure = subprocess.run(
            ["./configure"],
            cwd=READLINE_DIR,
            capture_output=True,
            text=True,
            timeout=600,
            env=_make_env(),
        )
        assert (
            configure.returncode == 0
        ), f"readline configure failed:\n{configure.stdout}\n{configure.stderr}"
        assert os.path.isfile(READLINE_MAKEFILE), "readline configure did not create Makefile"


def _readline_cpp_args():
    _ensure_readline_configured()
    return tuple(
        collect_cpp_args(
            READLINE_TEST_MAIN,
            dependencies=[f"{READLINE_DIR}={READLINE_MAKE_GOAL}"],
        )
    )


def _readline_units():
    _ensure_readline_configured()
    return collect_translation_units(
        READLINE_TEST_MAIN,
        dependencies=[f"{READLINE_DIR}={READLINE_MAKE_GOAL}"],
    )


def _readline_history_cpp_args():
    _ensure_readline_configured()
    return tuple(
        collect_cpp_args(
            READLINE_HISTORY_TEST_MAIN,
            dependencies=[f"{READLINE_DIR}={READLINE_HISTORY_MAKE_GOAL}"],
        )
    )


def _readline_history_units():
    _ensure_readline_configured()
    return collect_translation_units(
        READLINE_HISTORY_TEST_MAIN,
        dependencies=[f"{READLINE_DIR}={READLINE_HISTORY_MAKE_GOAL}"],
    )


@pytest.mark.integration
def test_readline_runtime_with_mcjit_depends_on():
    units, base_dir = _readline_history_units()

    result = CEvaluator().evaluate_translation_units(
        units,
        optimize=True,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_readline_history_cpp_args(),
        link_args=READLINE_HISTORY_LINK_ARGS,
    )

    assert result == 0


@pytest.mark.integration
def test_readline_runtime_with_system_link_depends_on():
    units, base_dir = _readline_units()

    result = CEvaluator().run_translation_units_with_system_cc(
        units,
        optimize=True,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_readline_cpp_args(),
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"readline system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "readline version 8.2" in result.stdout
    assert "history entry: readline" in result.stdout
    assert "OK" in result.stdout


def test_readline_runtime_with_self_backend_system_link_depends_on():
    units, base_dir = _readline_units()

    compiled = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).compile_translation_units(
        units,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_readline_cpp_args(),
    )

    result = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).run_compiled_translation_units_with_system_cc(
        compiled,
        optimize=True,
        base_dir=base_dir,
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"readline self backend system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "readline version 8.2" in result.stdout
    assert "history entry: readline" in result.stdout
    assert "OK" in result.stdout


def test_readline_make_goal_dependency_collects_library_sources():
    units, base_dir = _readline_units()

    names = [unit.name for unit in units]

    assert base_dir == os.path.abspath(PROJECTS_DIR)
    assert names[-1] == "test_readline_main.c"
    assert "readline.c" in names
    assert "history.c" in names
    assert "terminal.c" in names
    assert "examples/rlcat.c" not in names
    assert "examples/rlversion.c" not in names
