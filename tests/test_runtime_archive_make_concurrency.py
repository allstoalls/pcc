from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MAKEFILE = REPO_ROOT / "pcc" / "runtime" / "Makefile"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _fixture_python_dispatch(tmp_path: Path) -> str:
    # Inventory queries use the real target/configuration contract. These
    # publication tests still select their single transaction-marker object
    # explicitly; they do not build or certify a native runtime archive.
    shutil.copyfile(RUNTIME_MAKEFILE, tmp_path / "Makefile")
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        'case "$1:$2" in\n'
        '  -m:pcc.tools.runtime_module_inventory)\n'
        f'    exec {shlex.quote(sys.executable)} "$@" ;;\n'
        '  -m:pcc.ir.optimization.driver)\n'
        '    test "$#" -eq 5\n'
        # The fixture's IR is a transaction marker, not executable IR.
        '    cp "$4" "$5"\n'
        '    exit 0 ;;\n'
        '  -m:pcc.tools.runtime_archive_provenance)\n'
        '    case "$3" in\n'
        '      assemble) ;;\n'
        '      stamp-target)\n'
        '        test "$#" -eq 5 && test "$4" = --archive\n'
        # Check the published marker bundle without inventing target metadata.
        '        IFS= read -r build_id < "$5"\n'
        '        IFS= read -r capi_symbol < "$5.capi_syms"\n'
        '        IFS= read -r receipt < "$5.provenance.json"\n'
        '        test "$capi_symbol" = "Py$build_id"\n'
        '        test "$receipt" = "$build_id:$capi_symbol"\n'
        '        exit 0 ;;\n'
        '      *) echo "unexpected provenance command: $3" >&2; exit 1 ;;\n'
        '    esac ;;\n'
        '  *) echo "unexpected Python command: $*" >&2; exit 1 ;;\n'
        'esac\n'
    )


def _wait_for_file(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"make exited before reaching archive assembly:\n{stdout}{stderr}"
            )
        if time.monotonic() >= deadline:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise AssertionError("timed out waiting for archive assembly")
        time.sleep(0.01)


def _finish(process: subprocess.Popen[str]) -> tuple[int, str, str]:
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise AssertionError(f"concurrent make did not finish:\n{stdout}{stderr}")
    return process.returncode, stdout, stderr


def test_direct_make_serializes_runtime_archive_publication(
    tmp_path: Path,
) -> None:
    """Two direct Make writers publish one coherent manifest-last bundle."""
    state_dir = tmp_path / "state"
    tool_dir = tmp_path / "tools"
    state_dir.mkdir()
    tool_dir.mkdir()
    (tmp_path / "member.o").write_bytes(b"object\n")
    (tmp_path / "member.o.provenance.json").write_text(
        "receipt\n", encoding="utf-8"
    )

    fake_ar = tool_dir / "ar"
    _write_executable(
        fake_ar,
        "#!/bin/sh\n"
        "set -eu\n"
        "archive=$2\n"
        "printf '%s\\n' \"$BUILD_ID\" > \"$archive\"\n"
        '":" > "$STATE_DIR/ar-$BUILD_ID"\n'
        'if [ "$BUILD_ID" = "A" ]; then sleep 0.4; fi\n',
    )

    fake_ranlib = tool_dir / "ranlib"
    _write_executable(
        fake_ranlib,
        "#!/bin/sh\n"
        "set -eu\n"
        'test -f "$1"\n',
    )

    fake_nm = tool_dir / "nm"
    _write_executable(
        fake_nm,
        "#!/bin/sh\n"
        "set -eu\n"
        'archive=$2\n'
        'IFS= read -r build_id < "$archive"\n'
        'printf "0000000000000000 T Py%s\\n" "$build_id"\n',
    )

    fake_python = tool_dir / "python"
    _write_executable(
        fake_python,
        _fixture_python_dispatch(tmp_path)
        + "archive=\n"
        "output=\n"
        "capi_inventory=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --archive) shift; archive=$1 ;;\n'
        '    --output) shift; output=$1 ;;\n'
        '    --capi-inventory) shift; capi_inventory=$1 ;;\n'
        "  esac\n"
        "  shift\n"
        "done\n"
        'test -f "$archive"\n'
        'test -f "$capi_inventory"\n'
        'IFS= read -r build_id < "$archive"\n'
        'IFS= read -r capi_symbol < "$capi_inventory"\n'
        'test "$capi_symbol" = "Py$build_id"\n'
        'printf "%s:%s\\n" "$build_id" "$capi_symbol" > "$output"\n',
    )

    real_mv = shutil.which("mv")
    assert real_mv is not None
    publish_log = state_dir / "publish.log"
    fake_mv = tool_dir / "mv"
    _write_executable(
        fake_mv,
        "#!/bin/sh\n"
        "set -eu\n"
        "destination=\n"
        'for argument in "$@"; do destination=$argument; done\n'
        'printf "%s:%s\\n" "$BUILD_ID" "$destination" '
        '>> "$PUBLISH_LOG"\n'
        f"exec {shlex.quote(real_mv)} \"$@\"\n",
    )

    archive = tmp_path / "runtime.a"
    command = [
        "make",
        "-rR",
        "-B",
        "-f",
        str(RUNTIME_MAKEFILE),
        "LIB_PCC_PY=runtime.a",
        "PCC_PY_OBJECTS=member.o",
        "PCC_PY_RECEIPTS=member.o.provenance.json",
        f"AR={fake_ar}",
        f"RANLIB={fake_ranlib}",
        f"PYTHON={fake_python}",
        f"PCC_REPO_ROOT={REPO_ROOT}",
        "runtime.a",
    ]
    base_env = os.environ.copy()
    base_env.update(
        {
            "PATH": str(tool_dir) + os.pathsep + base_env["PATH"],
            "PUBLISH_LOG": str(publish_log),
            "STATE_DIR": str(state_dir),
        }
    )

    env_a = base_env | {"BUILD_ID": "A"}
    first = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=env_a,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    _wait_for_file(state_dir / "ar-A", first)

    env_b = base_env | {"BUILD_ID": "B"}
    second = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=env_b,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    first_result = _finish(first)
    second_result = _finish(second)

    assert (first_result[0], second_result[0]) == (0, 0), (
        "first make:\n"
        + first_result[1]
        + first_result[2]
        + "\nsecond make:\n"
        + second_result[1]
        + second_result[2]
    )
    assert archive.read_text(encoding="utf-8") == "B\n"
    assert Path(str(archive) + ".capi_syms").read_text(encoding="utf-8") == "PyB\n"
    assert Path(str(archive) + ".provenance.json").read_text(
        encoding="utf-8"
    ) == "B:PyB\n"
    assert publish_log.read_text(encoding="utf-8").splitlines() == [
        "A:runtime.a",
        "A:runtime.a.capi_syms",
        "A:runtime.a.provenance.json",
        "B:runtime.a",
        "B:runtime.a.capi_syms",
        "B:runtime.a.provenance.json",
    ]
    assert not Path(str(archive) + ".build.lock").exists()
    assert not Path(str(archive) + ".tmp").exists()
    assert not Path(str(archive) + ".capi_syms.nm.tmp").exists()
    assert not Path(str(archive) + ".capi_syms.tmp").exists()
    assert not Path(str(archive) + ".provenance.json.tmp").exists()


def _real_direct_make_fixture(
    tmp_path: Path,
) -> tuple[list[str], dict[str, str], Path, Path, Path]:
    """Drive the production .py -> .ll -> .o -> receipt dependency rules."""
    state_dir = tmp_path / "state-real"
    tool_dir = tmp_path / "tools-real"
    source_dir = tmp_path / "py"
    state_dir.mkdir()
    tool_dir.mkdir()
    source_dir.mkdir()
    (source_dir / "member.py").write_text(
        "def runtime_member() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    event_log = state_dir / "events.log"

    fake_pcc = tool_dir / "pcc"
    _write_executable(
        fake_pcc,
        "#!/bin/sh\n"
        "set -eu\n"
        "output=\n"
        'for argument in "$@"; do\n'
        '  case "$argument" in --emit-llvm=*) output=${argument#*=} ;; esac\n'
        "done\n"
        'test -n "$output"\n'
        'printf "%s:pcc:start\\n" "$BUILD_ID" >> "$EVENT_LOG"\n'
        '":" > "$STATE_DIR/pcc-$BUILD_ID-start"\n'
        'if [ "$BUILD_ID" = "A" ]; then sleep 0.4; fi\n'
        'printf "%s\\n" "$BUILD_ID" > "$output"\n'
        'printf "%s:pcc:end\\n" "$BUILD_ID" >> "$EVENT_LOG"\n',
    )

    fake_ir_to_obj = tool_dir / "ir-to-obj"
    _write_executable(
        fake_ir_to_obj,
        "#!/bin/sh\n"
        "set -eu\n"
        "ir=$1\n"
        "object=$2\n"
        "shift 2\n"
        "receipt=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in --provenance) shift; receipt=$1 ;; esac\n'
        "  shift\n"
        "done\n"
        'test -n "$receipt"\n'
        'IFS= read -r ir_id < "$ir"\n'
        'test "$ir_id" = "$BUILD_ID"\n'
        'printf "%s:ir-to-obj\\n" "$BUILD_ID" >> "$EVENT_LOG"\n'
        'printf "%s\\n" "$BUILD_ID" > "$object"\n'
        'printf "%s\\n" "$BUILD_ID" > "$receipt"\n',
    )

    fake_ar = tool_dir / "ar"
    _write_executable(
        fake_ar,
        "#!/bin/sh\n"
        "set -eu\n"
        "archive=$2\n"
        "object=$3\n"
        'IFS= read -r object_id < "$object"\n'
        'test "$object_id" = "$BUILD_ID"\n'
        'IFS= read -r receipt_id < "$object.provenance.json"\n'
        'test "$receipt_id" = "$BUILD_ID"\n'
        'printf "%s:archive\\n" "$BUILD_ID" >> "$EVENT_LOG"\n'
        'printf "%s\\n" "$BUILD_ID" > "$archive"\n',
    )

    fake_ranlib = tool_dir / "ranlib"
    _write_executable(
        fake_ranlib,
        "#!/bin/sh\n"
        "set -eu\n"
        'test -f "$1"\n',
    )

    fake_nm = tool_dir / "nm"
    _write_executable(
        fake_nm,
        "#!/bin/sh\n"
        "set -eu\n"
        'archive=$2\n'
        'IFS= read -r build_id < "$archive"\n'
        'test "$build_id" = "$BUILD_ID"\n'
        'printf "%s:nm\\n" "$BUILD_ID" >> "$EVENT_LOG"\n'
        'printf "0000000000000000 T Py%s\\n" "$build_id"\n',
    )

    fake_python = tool_dir / "python"
    _write_executable(
        fake_python,
        _fixture_python_dispatch(tmp_path)
        + "archive=\n"
        "output=\n"
        "capi_inventory=\n"
        "object=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --archive) shift; archive=$1 ;;\n'
        '    --output) shift; output=$1 ;;\n'
        '    --capi-inventory) shift; capi_inventory=$1 ;;\n'
        '    *.o) object=$1 ;;\n'
        "  esac\n"
        "  shift\n"
        "done\n"
        'test -f "$archive"\n'
        'test -f "$capi_inventory"\n'
        'test -f "$object.provenance.json"\n'
        'IFS= read -r build_id < "$archive"\n'
        'IFS= read -r capi_symbol < "$capi_inventory"\n'
        'IFS= read -r receipt_id < "$object.provenance.json"\n'
        'test "$build_id" = "$BUILD_ID"\n'
        'test "$receipt_id" = "$BUILD_ID"\n'
        'test "$capi_symbol" = "Py$build_id"\n'
        'printf "%s:manifest\\n" "$BUILD_ID" >> "$EVENT_LOG"\n'
        'printf "%s:%s\\n" "$build_id" "$capi_symbol" > "$output"\n',
    )

    real_mv = shutil.which("mv")
    assert real_mv is not None
    fake_mv = tool_dir / "mv"
    _write_executable(
        fake_mv,
        "#!/bin/sh\n"
        "set -eu\n"
        "destination=\n"
        'for argument in "$@"; do destination=$argument; done\n'
        'case "$destination" in\n'
        '  runtime.a|runtime.a.capi_syms|runtime.a.provenance.json)\n'
        '    printf "%s:publish:%s\\n" "$BUILD_ID" "$destination" '
        '>> "$EVENT_LOG" ;;\n'
        'esac\n'
        f"exec {shlex.quote(real_mv)} \"$@\"\n",
    )

    archive = tmp_path / "runtime.a"
    command = [
        "make",
        "-rR",
        "-B",
        "-f",
        str(RUNTIME_MAKEFILE),
        "LIB_PCC_PY=runtime.a",
        "PCC_PY_OBJECTS=build_py/member.o",
        "PCC_PY_RECEIPTS=build_py/member.o.provenance.json",
        f"PCC={fake_pcc}",
        f"PCC_IR_TO_OBJ={fake_ir_to_obj}",
        f"AR={fake_ar}",
        f"RANLIB={fake_ranlib}",
        f"PYTHON={fake_python}",
        f"PCC_REPO_ROOT={REPO_ROOT}",
        "runtime.a",
    ]
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": str(tool_dir) + os.pathsep + environment["PATH"],
            "EVENT_LOG": str(event_log),
            "STATE_DIR": str(state_dir),
        }
    )
    return command, environment, state_dir, archive, event_log


def _assert_no_archive_staging(archive: Path) -> None:
    assert not Path(str(archive) + ".build.lock").exists()
    assert not Path(str(archive) + ".tmp").exists()
    assert not Path(str(archive) + ".capi_syms.nm.tmp").exists()
    assert not Path(str(archive) + ".capi_syms.tmp").exists()
    assert not Path(str(archive) + ".provenance.json.tmp").exists()


def test_direct_make_lock_covers_real_runtime_prerequisites_and_publication(
    tmp_path: Path,
) -> None:
    command, environment, state_dir, archive, event_log = _real_direct_make_fixture(
        tmp_path
    )
    assert not (tmp_path / "build_py" / "member.o").exists()

    first = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=environment | {"BUILD_ID": "A"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    _wait_for_file(state_dir / "pcc-A-start", first)
    second = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=environment | {"BUILD_ID": "B"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    first_result = _finish(first)
    second_result = _finish(second)

    assert (first_result[0], second_result[0]) == (0, 0), (
        "first make:\n"
        + first_result[1]
        + first_result[2]
        + "\nsecond make:\n"
        + second_result[1]
        + second_result[2]
    )
    assert archive.read_text(encoding="utf-8") == "B\n"
    assert Path(str(archive) + ".capi_syms").read_text(encoding="utf-8") == "PyB\n"
    assert Path(str(archive) + ".provenance.json").read_text(
        encoding="utf-8"
    ) == "B:PyB\n"
    assert (tmp_path / "build_py" / "member.o").read_text(
        encoding="utf-8"
    ) == "B\n"
    assert (tmp_path / "build_py" / "member.o.provenance.json").read_text(
        encoding="utf-8"
    ) == "B\n"
    assert event_log.read_text(encoding="utf-8").splitlines() == [
        "A:pcc:start",
        "A:pcc:end",
        "A:ir-to-obj",
        "A:archive",
        "A:nm",
        "A:manifest",
        "A:publish:runtime.a",
        "A:publish:runtime.a.capi_syms",
        "A:publish:runtime.a.provenance.json",
        "B:pcc:start",
        "B:pcc:end",
        "B:ir-to-obj",
        "B:archive",
        "B:nm",
        "B:manifest",
        "B:publish:runtime.a",
        "B:publish:runtime.a.capi_syms",
        "B:publish:runtime.a.provenance.json",
    ]
    _assert_no_archive_staging(archive)


def test_direct_make_recovers_a_lock_owned_by_a_dead_process(
    tmp_path: Path,
) -> None:
    command, environment, _, archive, _ = _real_direct_make_fixture(tmp_path)
    lock = Path(str(archive) + ".build.lock")
    lock.mkdir()
    (lock / "owner").write_text("99999999\n", encoding="ascii")

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment | {"BUILD_ID": "STALE"},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert archive.read_text(encoding="utf-8") == "STALE\n"
    _assert_no_archive_staging(archive)


def test_direct_make_bounds_wait_for_a_live_archive_lock(
    tmp_path: Path,
) -> None:
    command, environment, _, archive, event_log = _real_direct_make_fixture(tmp_path)
    lock = Path(str(archive) + ".build.lock")
    lock.mkdir()
    (lock / "owner").write_text(f"{os.getpid()}\n", encoding="ascii")
    bounded_command = [
        *command[:-1],
        "PCC_RUNTIME_ARCHIVE_LOCK_MAX_WAITS=2",
        "PCC_RUNTIME_ARCHIVE_LOCK_SLEEP=0.01",
        command[-1],
    ]

    result = subprocess.run(
        bounded_command,
        cwd=tmp_path,
        env=environment | {"BUILD_ID": "BLOCKED"},
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode != 0
    assert "timed out waiting for pcc runtime archive lock" in (
        result.stdout + result.stderr
    )
    assert lock.is_dir()
    assert not event_log.exists()
    assert not (tmp_path / "build_py" / "member.o").exists()
    assert not archive.exists()


def test_production_make_plan_locks_before_prerequisites_and_publishes_manifest_last(
) -> None:
    source = RUNTIME_MAKEFILE.read_text(encoding="utf-8")
    public_target = "$(LIB_PCC_PY): _pcc_runtime_archive_lock_force"
    recursive_entry = 'PCC_RUNTIME_ARCHIVE_LOCK_HELD=1 "$@"'
    locked_target = "$(LIB_PCC_PY): $(PCC_PY_OBJECTS) $(PCC_PY_RECEIPTS)"

    assert source.index(public_target) < source.index(recursive_entry)
    assert source.index(recursive_entry) < source.index(locked_target)
    locked_recipe = source[source.index(locked_target) :]
    assert locked_recipe.index('mv -f "$@.tmp" "$@"') < locked_recipe.index(
        'mv -f "$@.capi_syms.tmp" "$@.capi_syms"'
    )
    assert locked_recipe.index(
        'mv -f "$@.capi_syms.tmp" "$@.capi_syms"'
    ) < locked_recipe.index(
        'mv -f "$@.provenance.json.tmp" "$@.provenance.json"'
    )
