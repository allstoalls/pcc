"""Tar metadata: fake-boundary models, separate object and Windows controls.

The model selectors import no compiler, runtime module or FFI. They execute
selected unchanged source definitions over explicit fake boundaries. They
are not native Windows evidence. Object/native selectors require separate
qualification and the native selector requires a source-matched archive.
"""

import ast
import os
from pathlib import Path
import platform
import re
import sys
from types import SimpleNamespace

import pytest

from tests.python.owned_regression_support import explicit_owned_runtime


ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "pcc/runtime/py/freestanding_windows.py"
TARFILE = ROOT / "pcc/stdlib/tarfile.py"
TARGET = "x86_64-pc-windows-msvc"


_TAR_CONTROL_FUNCTIONS = '''
def _tar_control_report(first_error, label, case_id, member_id, operation, status, number):
    try:
        if label == 'TAR_CONTROL_STATUS':
            print(label, case_id, member_id, operation, status, number, flush=True)
        else:
            print(label, case_id, member_id, operation, flush=True)
    except BaseException as output_error:
        if first_error is None:
            return output_error
    return first_error


def _tar_control_case(archive, member, member_id, case_id, operation, target,
                      seconds_override, expected_status, expected_errno, first_error):
    first_error = _tar_control_report(first_error, 'TAR_CONTROL_BEGIN', case_id,
                                      member_id, operation, 0, 0)
    try:
        if operation == 'chmod':
            mode = int(member.mode) & 0o777
            status = chmod_file(_control_utf8(target), mode)
            # Retain a managed use after the borrowed UTF-8 call returns.
            if not target:
                raise AssertionError('controlled target must not be empty')
            if status != expected_status and first_error is None:
                first_error = AssertionError('unexpected controlled chmod status')
            first_error = _tar_control_report(first_error, 'TAR_CONTROL_STATUS', case_id,
                                              member_id, operation, status, 0)
        elif operation == 'utime':
            seconds = int(member.mtime) if case_id < 6 else seconds_override
            if seconds < -9223372036854775808 or seconds > 9223372036854775807:
                raise AssertionError('control seconds exceed signed i64')
            times = malloc(16)
            if ptr_is_null(times):
                raise MemoryError('unable to allocate controlled timestamps')
            result_ready = False
            try:
                try:
                    store_i64(times, 0, seconds)
                    store_i64(times, 8, seconds)
                    status = utime_file(_control_utf8(target), times)
                    number = _control_errno()
                    if not target:
                        raise AssertionError('controlled target must not be empty')
                    reported_errno = number if status != 0 else 0
                    if (status != expected_status or reported_errno != expected_errno) and first_error is None:
                        first_error = AssertionError('unexpected controlled utime status or errno')
                    result_ready = True
                except BaseException as operation_error:
                    if first_error is None:
                        first_error = operation_error
                    first_error = _tar_control_report(first_error, 'TAR_CONTROL_ERROR', case_id,
                                                      member_id, operation, 0, 0)
            finally:
                try:
                    free(times)
                except BaseException as cleanup_error:
                    if first_error is None:
                        first_error = cleanup_error
                    first_error = _tar_control_report(first_error, 'TAR_CONTROL_CLEANUP_ERROR', case_id,
                                                      member_id, operation, 0, 0)
            if result_ready:
                first_error = _tar_control_report(first_error, 'TAR_CONTROL_STATUS', case_id,
                                                  member_id, operation, status, reported_errno)
        else:
            if case_id == 13:
                try:
                    archive._set_attrs(member, target)
                except _TarExtractError:
                    first_error = _tar_control_report(first_error, 'TAR_CONTROL_EXPECTED_ERROR', case_id,
                                                      member_id, operation, 0, 0)
                else:
                    raise AssertionError('controlled NUL target did not fail')
            else:
                archive._set_attrs(member, target)
                first_error = _tar_control_report(first_error, 'TAR_CONTROL_WRAPPER_OK', case_id,
                                                  member_id, operation, 0, 0)
    except BaseException as control_error:
        if first_error is None:
            first_error = control_error
        first_error = _tar_control_report(first_error, 'TAR_CONTROL_ERROR', case_id,
                                          member_id, operation, 0, 0)
    return first_error


'''

_TAR_CONTROL_CASES = (
    (0, 'chmod', 'leaf0', 0, 0, 0),
    (0, 'utime', 'leaf0', 0, 0, 0),
    (1, 'chmod', 'leaf1', 0, 0, 0),
    (1, 'utime', 'leaf1', 0, 0, 0),
    (2, 'chmod', 'leaf2', 0, 0, 0),
    (2, 'utime', 'leaf2', 0, 0, 0),
    (0, 'set_attrs', 'wrapper0', 0, 0, 0),
    (1, 'set_attrs', 'wrapper1', 0, 0, 0),
    (2, 'set_attrs', 'wrapper2', 0, 0, 0),
    (0, 'chmod', 'missing', 0, -2, 0),
    (0, 'utime', 'missing', 0, -1, 2),
    (0, 'utime', 'negative', -11644473600, -1, 75),
    (0, 'utime', 'negative', 910692730086, -1, 75),
    (0, 'set_attrs', 'nul', 0, 0, 0),
)

_TAR_CONTROL_SUCCESS_STDOUT = (
    'TAR_CONTROL_BEGIN 0 0 chmod\nTAR_CONTROL_STATUS 0 0 chmod 0 0\n'
    'TAR_CONTROL_BEGIN 1 0 utime\nTAR_CONTROL_STATUS 1 0 utime 0 0\n'
    'TAR_CONTROL_BEGIN 2 1 chmod\nTAR_CONTROL_STATUS 2 1 chmod 0 0\n'
    'TAR_CONTROL_BEGIN 3 1 utime\nTAR_CONTROL_STATUS 3 1 utime 0 0\n'
    'TAR_CONTROL_BEGIN 4 2 chmod\nTAR_CONTROL_STATUS 4 2 chmod 0 0\n'
    'TAR_CONTROL_BEGIN 5 2 utime\nTAR_CONTROL_STATUS 5 2 utime 0 0\n'
    'TAR_CONTROL_BEGIN 6 0 set_attrs\nTAR_CONTROL_WRAPPER_OK 6 0 set_attrs\n'
    'TAR_CONTROL_BEGIN 7 1 set_attrs\nTAR_CONTROL_WRAPPER_OK 7 1 set_attrs\n'
    'TAR_CONTROL_BEGIN 8 2 set_attrs\nTAR_CONTROL_WRAPPER_OK 8 2 set_attrs\n'
    'TAR_CONTROL_BEGIN 9 0 chmod\nTAR_CONTROL_STATUS 9 0 chmod -2 0\n'
    'TAR_CONTROL_BEGIN 10 0 utime\nTAR_CONTROL_STATUS 10 0 utime -1 2\n'
    'TAR_CONTROL_BEGIN 11 0 utime\nTAR_CONTROL_STATUS 11 0 utime -1 75\n'
    'TAR_CONTROL_BEGIN 12 0 utime\nTAR_CONTROL_STATUS 12 0 utime -1 75\n'
    'TAR_CONTROL_BEGIN 13 0 set_attrs\nTAR_CONTROL_EXPECTED_ERROR 13 0 set_attrs\n'
    'TAR_CONTROLS_OK\n'
)


def _tar_control_program(paths):
    text = _TAR_CONTROL_FUNCTIONS + 'def _tar_run_controls(archive):\n'
    text += '    members = archive.getmembers()\n    first_error = None\n'
    for case_id, (member_id, operation, key, seconds, status, number) in enumerate(_TAR_CONTROL_CASES):
        text += (f'    first_error = _tar_control_case(archive, members[{member_id}], '
                 f'{member_id}, {case_id}, {operation!r}, {str(paths[key])!r}, '
                 f'{seconds}, {status}, {number}, first_error)\n')
    text += ('    if first_error is not None:\n        raise first_error\n'
             "    print('TAR_CONTROLS_OK', flush=True)\n\n")
    return text


_TAR_NATIVE_SUCCESS_STDOUT = (
    "TAR_OPEN_BEGIN\nTAR_OPEN_OK\n"
    "TAR_PREFLIGHT_BEGIN 0\nTAR_PREFLIGHT_OK 0\n"
    "TAR_PREFLIGHT_BEGIN 1\nTAR_PREFLIGHT_OK 1\n"
    "TAR_PREFLIGHT_BEGIN 2\nTAR_PREFLIGHT_OK 2\n"
    "TAR_EXTRACT_BEGIN\nTAR_EXTRACT_OK\n"
    + _TAR_CONTROL_SUCCESS_STDOUT +
    "TAR_CLOSE_BEGIN\nTAR_CLOSE_OK\nTAR_METADATA_OK\n"
)


def _tar_attrs_native_program(import_style, source_tar, destination, control_paths=None):
    """One full extraction, with read-only preflight and observable phases.

    OPEN includes __enter__. The extra data_filter pass does not materialize
    files or replace extractall's own checks. A later EXTRACT failure does
    not identify chmod, utime or committed-timestamp cleanup specifically.
    """
    assert import_style in ("module", "static")
    imports = ("import tarfile\n\n" if import_style == "module" else
               "from tarfile import open as tar_open, data_filter as tar_data_filter\n\n")
    opener = "tarfile.open" if import_style == "module" else "tar_open"
    preflight = "tarfile.data_filter" if import_style == "module" else "tar_data_filter"
    if control_paths is None:
        control_paths = {key: key for key in ("leaf0", "leaf1", "leaf2", "wrapper0", "wrapper1",
                                             "wrapper2", "negative", "missing")}
        control_paths["nul"] = "negative\0suffix"
    scaffold = ("from tarfile import ExtractError as _TarExtractError\n"
                "from pcc.extern import c_int, c_ptr, c_rawptr, extern\n"
                "from pcc.unsafe import (chmod_file, utime_file, malloc, free, ptr_is_null, store_i64)\n"
                '_control_utf8: "extern" = extern("py_str_utf8", (c_ptr,), c_rawptr)\n'
                '_control_errno: "extern" = extern("pcc_errno_get", (), c_int)\n\n')
    return (imports + scaffold + _tar_control_program(control_paths) +
            "def main():\n"
            "    phase = 'OPEN'\n"
            "    primary_error = None\n"
            "    extract_attempted = False\n"
            "    print('TAR_OPEN_BEGIN', flush=True)\n"
            "    try:\n"
            f"        with {opener}({str(source_tar)!r}) as archive:\n"
            "            print('TAR_OPEN_OK', flush=True)\n"
            "            try:\n"
            "                phase = 'PREFLIGHT'\n"
            "                index = 0\n"
            "                for member in archive.getmembers():\n"
            "                    print('TAR_PREFLIGHT_BEGIN', index, flush=True)\n"
            f"                    {preflight}(member, {str(destination)!r})\n"
            "                    print('TAR_PREFLIGHT_OK', index, flush=True)\n"
            "                    index += 1\n"
            "                phase = 'EXTRACT'\n"
            "                print('TAR_EXTRACT_BEGIN', flush=True)\n"
            "                extract_attempted = True\n"
            f"                archive.extractall({str(destination)!r})\n"
            "                print('TAR_EXTRACT_OK', flush=True)\n"
            "            except BaseException as error:\n"
            "                primary_error = error\n"
            "                try:\n"
            "                    print('TAR_FAILURE', phase, flush=True)\n"
            "                except BaseException:\n"
            "                    pass\n"
            "                try:\n"
            "                    if extract_attempted:\n"
            "                        _tar_run_controls(archive)\n"
            "                except BaseException:\n"
            "                    try:\n"
            "                        print('TAR_CONTROLS_FAILED_AFTER_EXTRACT', flush=True)\n"
            "                    except BaseException:\n"
            "                        pass\n"
            "                raise\n"
            "            phase = 'CONTROLS'\n"
            "            _tar_run_controls(archive)\n"
            "            print('TAR_CLOSE_BEGIN', flush=True)\n"
            "            phase = 'CLOSE'\n"
            "    except BaseException as error:\n"
            "        try:\n"
            "            if primary_error is None:\n"
            "                print('TAR_FAILURE', phase, flush=True)\n"
            "            elif error is not primary_error:\n"
            "                print('TAR_CLOSE_FAILED_AFTER_ERROR', flush=True)\n"
            "        except BaseException:\n"
            "            pass\n"
            "        raise\n"
            "    print('TAR_CLOSE_OK', flush=True)\n"
            "    print('TAR_METADATA_OK', flush=True)\n\nmain()\n")


def _check_tar_attrs_native_phase_witness():
    """Execute only the generated main over explicit memory-only fake owners."""
    for import_style in ("module", "static"):
        program = ast.parse(_tar_attrs_native_program(import_style, "metadata.tar", "extracted"))
        main = next(node for node in program.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)]
        extract_calls = [node for node in calls if isinstance(node.func, ast.Attribute)
                         and node.func.attr == "extractall"]
        assert len(extract_calls) == 1 and not extract_calls[0].keywords
        assert ast.literal_eval(extract_calls[0].args[0]) == "extracted"
        for node in calls:
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                assert [(item.arg, ast.literal_eval(item.value)) for item in node.keywords] == [("flush", True)]
        raises = [node for node in ast.walk(main) if isinstance(node, ast.Raise)]
        assert len(raises) == 2 and all(node.exc is None and node.cause is None for node in raises)
        assert not any(isinstance(node, ast.Attribute) and node.attr in ("args", "__traceback__")
                       for node in ast.walk(main))

        for failure, marker_failure in (
            (stage, broken) for stage in (
                None, "open", "enter", "members", "preflight:0", "preflight:1",
                "preflight:2", "extract_begin", "extract", "close", "extract+close",
            ) for broken in (False, True)
        ):
            events, lines = [], []
            original = RuntimeError("original failure")
            replacement = OSError("close replacement")
            diagnostic = RuntimeError("diagnostic output failure")
            members = [object(), object(), object()]

            class Archive:
                def __enter__(self):
                    events.append("enter")
                    if failure == "enter":
                        raise original
                    return self

                def __exit__(self, kind, error, traceback):
                    events.append(("exit", error))
                    if failure == "close":
                        raise original
                    if failure == "extract+close":
                        assert error is original
                        raise replacement
                    return False

                def getmembers(self):
                    events.append("members")
                    if failure == "members":
                        raise original
                    return members

                def extractall(self, destination):
                    assert destination == "extracted"
                    events.append("extractall")
                    if failure in ("extract", "extract+close"):
                        raise original

            def opener(source):
                assert source == "metadata.tar"
                events.append("open")
                if failure == "open":
                    raise original
                return Archive()

            def preflight(member, destination):
                assert destination == "extracted"
                index = members.index(member)
                events.append(("preflight", index))
                if failure == "preflight:" + str(index):
                    raise original
                return member

            def marker(*values, flush=False):
                assert flush is True
                if failure == "extract_begin" and values[0] == "TAR_EXTRACT_BEGIN":
                    raise original
                if marker_failure and values[0] in ("TAR_FAILURE", "TAR_CLOSE_FAILED_AFTER_ERROR"):
                    raise diagnostic
                lines.append(" ".join(str(value) for value in values))

            def controls(archive):
                events.append("controls")
                lines.extend(_TAR_CONTROL_SUCCESS_STDOUT.splitlines())

            namespace = {"tarfile": SimpleNamespace(open=opener, data_filter=preflight),
                         "tar_open": opener, "tar_data_filter": preflight, "print": marker,
                         "_tar_run_controls": controls}
            exec(compile(ast.Module(body=[main], type_ignores=[]), "<tar-phase-model>", "exec"), namespace)
            if failure is None:
                namespace["main"]()
                assert "\n".join(lines) + "\n" == _TAR_NATIVE_SUCCESS_STDOUT
                assert events == ["open", "enter", "members", ("preflight", 0),
                                  ("preflight", 1), ("preflight", 2), "extractall", "controls", ("exit", None)]
                continue
            with pytest.raises(BaseException) as caught:
                namespace["main"]()
            assert caught.value is (replacement if failure == "extract+close" else original)
            expected = ["TAR_OPEN_BEGIN"]
            if failure in ("open", "enter"):
                expected.append("TAR_FAILURE OPEN")
                assert events == (["open"] if failure == "open" else ["open", "enter"])
            else:
                expected.append("TAR_OPEN_OK")
                count = 0 if failure == "members" else 3
                for index in range(count):
                    expected.append("TAR_PREFLIGHT_BEGIN " + str(index))
                    if failure == "preflight:" + str(index):
                        break
                    expected.append("TAR_PREFLIGHT_OK " + str(index))
                if failure == "members" or failure.startswith("preflight:"):
                    expected.append("TAR_FAILURE PREFLIGHT")
                    assert "extractall" not in events
                elif failure == "extract_begin":
                    expected.append("TAR_FAILURE EXTRACT")
                    assert "extractall" not in events and "controls" not in events
                else:
                    expected.append("TAR_EXTRACT_BEGIN")
                    assert events.count("extractall") == 1
                    if failure == "close":
                        expected.append("TAR_EXTRACT_OK")
                        expected.extend(_TAR_CONTROL_SUCCESS_STDOUT.splitlines())
                        expected.extend(("TAR_CLOSE_BEGIN", "TAR_FAILURE CLOSE"))
                    else:
                        expected.append("TAR_FAILURE EXTRACT")
                        expected.extend(_TAR_CONTROL_SUCCESS_STDOUT.splitlines())
                        if failure == "extract+close":
                            expected.append("TAR_CLOSE_FAILED_AFTER_ERROR")
                assert events[-1] == ("exit", None if failure == "close" else original)
            if marker_failure:
                expected = [line for line in expected if not line.startswith(("TAR_FAILURE", "TAR_CLOSE_FAILED_AFTER_ERROR"))]
            assert lines == expected
            assert "TAR_METADATA_OK" not in lines


def _check_tar_metadata_control_models():
    paths = {key: key for key in ("leaf0", "leaf1", "leaf2", "wrapper0", "wrapper1",
                                 "wrapper2", "negative", "missing")}
    paths["nul"] = "negative\0suffix"
    tree = ast.parse(_tar_control_program(paths))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert [node.name for node in functions] == ["_tar_control_report", "_tar_control_case", "_tar_run_controls"]
    case = functions[1]
    calls = [node for node in ast.walk(case) if isinstance(node, ast.Call)]
    for name in ("chmod_file", "utime_file"):
        call = next(node for node in calls if isinstance(node.func, ast.Name) and node.func.id == name)
        assert isinstance(call.args[0], ast.Call) and call.args[0].func.id == "_control_utf8"
    assert [(ast.literal_eval(node.args[1]),) for node in calls
            if isinstance(node.func, ast.Name) and node.func.id == "store_i64"] == [(0,), (8,)]
    # Source-bound direct calls, not imported project code or interpreted FFI.
    scenarios = ("success", "chmod_status", "utime_primary", "utime_committed_close",
                 "missing_chmod_status", "missing_utime_status", "sentinel_status", "upper_status",
                 "nul_missing_error", "nul_wrong_error", "wrapper_error", "allocation_null",
                 "allocation_error", "store_error", "utf8_error", "utime_error", "errno_error",
                 "free_error", "utime_and_free", "errno_and_free", "report_begin", "report_status",
                 "utime_error_report", "final_report", "mtime_overflow", "chmod_status_report",
                 "utime_primary_report", "utime_primary_free", "utime_committed_free")
    assert len(scenarios) == 29 and len(_TAR_CONTROL_CASES) == 14
    for scenario in scenarios:
        events, lines, buffers = [], [], {}
        state = SimpleNamespace(case=-1, number=777, next_pointer=100)
        original = RuntimeError("first controlled operation failure")
        cleanup = RuntimeError("controlled cleanup failure")
        diagnostic = RuntimeError("controlled output failure")
        members = [SimpleNamespace(mode=0o640, mtime=-1), SimpleNamespace(mode=0o444, mtime=0),
                   SimpleNamespace(mode=0o755, mtime=123456789)]
        if scenario == "mtime_overflow":
            members[0].mtime = 9223372036854775808
        before_members = [(member.mode, member.mtime) for member in members]

        class StatusFailure(AssertionError):
            def __init__(self, message):
                events.append(("status_failure", state.case))
                super().__init__(message)

        def marker(*values, flush=False):
            assert flush is True
            if values[0] == "TAR_CONTROL_BEGIN":
                state.case = values[1]
            events.append(("print", state.case, values[0]))
            state.number = 999  # A print would overwrite an uncaptured errno.
            if ((scenario == "report_begin" and values[:2] == ("TAR_CONTROL_BEGIN", 0))
                    or (scenario == "report_status" and values[:2] == ("TAR_CONTROL_STATUS", 1))
                    or (scenario == "utime_error_report" and values[:2] == ("TAR_CONTROL_ERROR", 1))
                    or (scenario == "chmod_status_report" and values[:2] == ("TAR_CONTROL_STATUS", 0))
                    or (scenario == "utime_primary_report" and values[:2] == ("TAR_CONTROL_STATUS", 1))
                    or (scenario == "final_report" and values[0] == "TAR_CONTROLS_OK")):
                raise diagnostic
            lines.append(" ".join(str(value) for value in values))

        def utf8(target):
            assert isinstance(target, str) and "\0" not in target
            events.append(("utf8", state.case, target))
            if scenario == "utf8_error" and state.case == 1:
                raise original
            return target

        def allocate(size):
            assert size == 16
            events.append(("malloc", state.case))
            if state.case == 1 and scenario == "allocation_error":
                raise original
            if state.case == 1 and scenario == "allocation_null":
                return 0
            state.next_pointer += 1
            buffers[state.next_pointer] = [None, None]
            return state.next_pointer

        def store(pointer, offset, value):
            assert offset in (0, 8) and -9223372036854775808 <= value <= 9223372036854775807
            events.append(("store", state.case, offset, value))
            if scenario == "store_error" and state.case == 1:
                raise original
            buffers[pointer][offset // 8] = value

        def release(pointer):
            assert pointer in buffers
            del buffers[pointer]
            events.append(("free", state.case))
            state.number = 888  # Snapshot must precede cleanup, too.
            if state.case == 1 and scenario in ("free_error", "utime_and_free", "errno_and_free",
                                               "utime_primary_free", "utime_committed_free"):
                raise cleanup

        def chmod(target, mode):
            events.append(("chmod", state.case, target, mode))
            assert mode == (int(members[_TAR_CONTROL_CASES[state.case][0]].mode) & 0o777)
            if scenario in ("chmod_status", "chmod_status_report") and state.case == 0:
                return -13
            if state.case == 9:
                return 0 if scenario == "missing_chmod_status" else -2
            return 0

        def utime(target, pointer):
            seconds = buffers[pointer]
            assert seconds[0] == seconds[1]
            events.append(("utime", state.case, target, seconds[0]))
            if state.case == 1:
                if scenario in ("utime_error", "utime_and_free", "utime_error_report"):
                    raise original
                if scenario in ("utime_primary", "utime_primary_report", "utime_primary_free"):
                    state.number = 13
                    return -1
                if scenario in ("utime_committed_close", "utime_committed_free"):
                    state.number = 9
                    return 1
            expected = {10: (-1, 2), 11: (-1, 75), 12: (-1, 75)}
            status, state.number = expected.get(state.case, (0, 777))
            if (state.case, scenario) in ((10, "missing_utime_status"), (11, "sentinel_status"),
                                          (12, "upper_status")):
                return 0
            return status

        def errno():
            events.append(("errno", state.case, state.number))
            if state.case == 1 and scenario in ("errno_error", "errno_and_free"):
                raise original
            return state.number

        class Archive:
            def getmembers(self):
                return list(members)

            def _set_attrs(self, member, target):
                events.append(("wrapper", state.case, target))
                assert member is members[_TAR_CONTROL_CASES[state.case][0]]
                if state.case == 6 and scenario == "wrapper_error":
                    raise original
                if state.case == 13:
                    assert target == paths["nul"]
                    if scenario == "nul_missing_error":
                        return
                    if scenario == "nul_wrong_error":
                        raise original
                    raise _ExtractError("expected NUL rejection")

        namespace = {"print": marker, "_control_utf8": utf8, "chmod_file": chmod,
                     "utime_file": utime, "_control_errno": errno, "malloc": allocate,
                     "free": release, "ptr_is_null": lambda pointer: pointer == 0,
                     "store_i64": store, "_TarExtractError": _ExtractError, "AssertionError": StatusFailure}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "<tar-control-model>", "exec"), namespace)
        if scenario == "success":
            namespace["_tar_run_controls"](Archive())
            assert "\n".join(lines) + "\n" == _TAR_CONTROL_SUCCESS_STDOUT
        else:
            with pytest.raises(BaseException) as caught:
                namespace["_tar_run_controls"](Archive())
            if scenario in ("wrapper_error", "allocation_error", "store_error", "utf8_error", "utime_error",
                            "errno_error", "utime_and_free", "errno_and_free", "utime_error_report", "nul_wrong_error"):
                assert caught.value is original
            elif scenario == "free_error":
                assert caught.value is cleanup
            elif scenario in ("report_begin", "report_status", "final_report"):
                assert caught.value is diagnostic
            elif scenario == "allocation_null":
                assert isinstance(caught.value, MemoryError)
            else:
                assert isinstance(caught.value, AssertionError)
            assert "TAR_CONTROLS_OK" not in lines
        assert [event[1] for event in events if event[0] == "print" and event[2] == "TAR_CONTROL_BEGIN"] == list(range(14))
        assert not buffers and [(member.mode, member.mtime) for member in members] == before_members
        for case_id in (1, 3, 5, 10, 11, 12):
            subset = [event[0] for event in events if event[1] == case_id and event[0] != "print"]
            assert subset.count("free") == (0 if case_id == 1 and scenario in (
                "allocation_null", "allocation_error", "mtime_overflow") else 1)
            if "errno" in subset:
                assert subset.index("utime") < subset.index("errno") < subset.index("free")
            if "status_failure" in subset and "free" in subset:
                assert subset.index("status_failure") < subset.index("free")
            if "utime" in subset:
                assert subset[subset.index("utime") - 1] == "utf8"
        if scenario == "success":
            values = [event[3] for event in events if event[0] == "utime"]
            assert values == [-1, 0, 123456789, 0, -11644473600, 910692730086]
        if scenario == "utime_primary":
            assert "TAR_CONTROL_STATUS 1 0 utime -1 13" in lines
        if scenario == "utime_committed_close":
            assert "TAR_CONTROL_STATUS 1 0 utime 1 9" in lines


def _check_tar_control_failure_priority():
    # The real generated main runs over fake owners. The real control helper
    # bodies are exercised above; no embedded imports are executed here.
    for import_style in ("module", "static"):
        tree = ast.parse(_tar_attrs_native_program(import_style, "metadata.tar", "extracted"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        for original_fails in (False, True):
            for controls_fail in (False, True):
                for report_fails in (False, True):
                    for close_fails in (False, True):
                        events = []
                        original = RuntimeError("original extraction failure")
                        controlled = RuntimeError("independent control failure")
                        closing = RuntimeError("archive close failure")
                        reporting = RuntimeError("diagnostic print failure")

                        class Archive:
                            def __enter__(self):
                                return self

                            def __exit__(self, kind, error, traceback):
                                events.append(("close", error))
                                if close_fails:
                                    raise closing
                                return False

                            def getmembers(self):
                                return [0, 1, 2]

                            def extractall(self, destination):
                                assert destination == "extracted"
                                events.append("extract")
                                if original_fails:
                                    raise original

                        def controls(archive):
                            events.append("controls")
                            if controls_fail:
                                raise controlled

                        def marker(*values, flush=False):
                            assert flush is True
                            if report_fails and values[0] in ("TAR_FAILURE", "TAR_CONTROLS_FAILED_AFTER_EXTRACT",
                                                             "TAR_CLOSE_FAILED_AFTER_ERROR"):
                                raise reporting

                        namespace = {"tarfile": SimpleNamespace(open=lambda path: Archive(), data_filter=lambda *args: None),
                                     "tar_open": lambda path: Archive(), "tar_data_filter": lambda *args: None,
                                     "print": marker, "_tar_run_controls": controls}
                        exec(compile(ast.Module(body=[main], type_ignores=[]), "<tar-priority-model>", "exec"), namespace)
                        expected = closing if close_fails else original if original_fails else controlled if controls_fail else None
                        if expected is None:
                            namespace["main"]()
                        else:
                            with pytest.raises(BaseException) as caught:
                                namespace["main"]()
                            assert caught.value is expected
                        assert events[:2] == ["extract", "controls"]
                        assert events[2] == ("close", original if original_fails else controlled if controls_fail else None)


class _ExtractError(Exception):
    pass


def _definitions(path, names, bindings, class_name=None):
    tree = ast.parse(path.read_text())
    body = tree.body
    if class_name is not None:
        body = next(node.body for node in body
                    if isinstance(node, ast.ClassDef) and node.name == class_name)
    functions = [node for node in body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == set(names)
    namespace = {
        "i64": int, "c_ptr": object,
        "c_abi_export": lambda *args: lambda function: function,
        "c_abi_typed_export": lambda *args: lambda function: function,
        "ExtractError": _ExtractError, **bindings,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


# Frozen baseline body from 81776ebc5d6ab80918321bd30e7f21ea7708283e.
# Do not regenerate this oracle from the candidate implementation.
_OLD_SET_ATTRS = '''
def old_set_attrs(self, member, target):
    if _chmod(_py_str_utf8(target), int(member.mode) & 0o777) != 0:
        raise ExtractError("could not apply tar member mode")
    times = malloc(16)
    if ptr_is_null(times):
        raise MemoryError("unable to allocate tar timestamp state")
    try:
        timestamp = int(member.mtime)
        store_i64(times, 0, timestamp)
        store_i64(times, 8, timestamp)
        if _utime(_py_str_utf8(target), times) != 0:
            raise ExtractError("could not apply tar member timestamp")
    finally:
        free(times)
'''


class _TarBoundaries:
    def __init__(self, mode_status=0, time_status=0, allocate=True, interpreted=False):
        self.events = []
        self.mode_status = mode_status
        self.time_status = time_status
        self.allocate = allocate
        self.interpreted = interpreted

    def path(self, target):
        self.events.append(("path", target))
        if self.interpreted:
            raise NotImplementedError("interpreted extern boundary")
        return target

    def chmod(self, target, mode):
        self.events.append(("chmod", target, mode))
        return self.mode_status

    def malloc(self, size):
        self.events.append(("malloc", size))
        return 101 if self.allocate else 0

    def utime(self, target, times):
        self.events.append(("utime", target, times))
        return self.time_status

    def bindings(self):
        return dict(
            _py_str_utf8=self.path, _chmod=self.chmod, chmod_file=self.chmod,
            malloc=self.malloc, ptr_is_null=lambda pointer: pointer == 0,
            store_i64=lambda *args: self.events.append(("store", *args)),
            _utime=self.utime, utime_file=self.utime, free=lambda pointer: self.events.append(("free", pointer)),
            ExtractError=_ExtractError,
        )

    def apply(self, member, target="folder/na\u00efve.txt", old=False):
        namespace = self.bindings()
        if old:
            exec(_OLD_SET_ATTRS, namespace)
            method = namespace["old_set_attrs"]
        else:
            method = _definitions(TARFILE, ("_set_attrs",), namespace, "TarFile")["_set_attrs"]
        try:
            method(None, member, target)
        except Exception as error:
            return type(error), str(error)
        return None


@pytest.mark.parametrize("mode_status,time_status,allocate,interpreted,mtime", (
    (0, 0, True, False, 123456789.9),
    (0, 0, True, False, -1.9),
    (0, 0, True, False, "12"),
    (0, 0, True, False, "bad"),
    (-13, 0, True, False, "bad"),
    (0, -1, True, False, 100),
    (0, 0, False, False, "bad"),
    (0, 0, True, True, 100),
))
def test_tar_attrs_model_preserves_frozen_order_errors_and_host_boundary(
    mode_status, time_status, allocate, interpreted, mtime,
):
    old = _TarBoundaries(mode_status, time_status, allocate, interpreted)
    current = _TarBoundaries(mode_status, time_status, allocate, interpreted)
    member = SimpleNamespace(mode=0o7777, mtime=mtime)
    assert current.apply(member) == old.apply(member, old=True)
    assert current.events == old.events
    if not interpreted:
        assert current.events[1] == ("chmod", "folder/na\u00efve.txt", 0o777)


@pytest.mark.parametrize("timestamp", (-(1 << 64), -(1 << 63) - 1, 1 << 63, 1 << 64, 1 << 100))
def test_tar_attrs_model_rejects_integer_wrapping_before_store(timestamp):
    boundary = _TarBoundaries()
    assert boundary.apply(SimpleNamespace(mode=0o644, mtime=timestamp)) == (
        _ExtractError, "could not apply tar member timestamp",
    )
    assert not any(event[0] in ("store", "utime") for event in boundary.events)
    assert boundary.events[-1] == ("free", 101)


@pytest.mark.parametrize("timestamp", (-(1 << 63), -1, 0, (1 << 63) - 1))
def test_tar_attrs_model_preserves_exact_i64_buffer(timestamp):
    boundary = _TarBoundaries()
    assert boundary.apply(SimpleNamespace(mode=0o644, mtime=timestamp)) is None
    assert [event for event in boundary.events if event[0] == "store"] == [
        ("store", 101, 0, timestamp), ("store", 101, 8, timestamp),
    ]


def test_tar_attrs_model_rejects_nul_before_any_metadata_mutation():
    boundary = _TarBoundaries()
    assert boundary.apply(SimpleNamespace(mode=0o644, mtime=0), "safe\0other") == (
        _ExtractError, "tar member target contains NUL",
    )
    assert boundary.events == []


def test_tar_attrs_model_close_failure_reports_committed_timestamp():
    boundary = _TarBoundaries(time_status=1)
    kind, message = boundary.apply(SimpleNamespace(mode=0o644, mtime=0))
    assert kind is _ExtractError
    assert message == "tar member timestamp was applied but its handle could not be closed"
    assert boundary.events[-1] == ("free", 101)


class _WindowsBoundaries:
    def __init__(self, access=0, write=0, wide=201, handle=301, set_ok=1, close_ok=1):
        self.cells = {101: access, 109: write}
        self.events = []
        self.wide, self.handle = wide, handle
        self.set_ok, self.close_ok = set_ok, close_ok
        self.last_error = 0
        self.errno = 987

    def utf16(self, path):
        self.events.append(("utf16", path))
        return self.wide

    def create(self, *args):
        self.events.append(("create", *args))
        self.last_error = 5
        return self.handle

    def free(self, pointer):
        self.events.append(("free", pointer))
        self.last_error = 112  # Cleanup deliberately clobbers GetLastError.

    def set_time(self, handle, creation, access, write):
        self.events.append(("set", handle, creation, access, write))
        self.last_error = 5
        return self.set_ok

    def close(self, handle):
        self.events.append(("close", handle))
        self.last_error = 6
        return self.close_ok

    def error(self):
        self.events.append(("error", self.last_error))
        return self.last_error

    def set_errno(self, value):
        self.events.append(("errno", value))
        self.errno = value

    def store(self, pointer, offset, value):
        self.cells[pointer + offset] = value & 0xffffffff

    def module(self):
        return _definitions(WINDOWS, ("error", "error_code", "filetime_from_seconds", "utime_file"), dict(
            ptr_is_null=lambda pointer: pointer == 0, null=lambda: 0,
            ptr_to_int=lambda pointer: pointer, ptr_add=lambda pointer, offset: pointer + offset,
            load_i64=lambda pointer, offset: self.cells[pointer + offset],
            stack_alloc=lambda size: 401, store_i32=self.store,
            utf16=self.utf16, free=self.free, CreateFileW=self.create,
            SetFileTime=self.set_time, CloseHandle=self.close,
            GetLastError=self.error, pcc_errno_set=self.set_errno,
        ))

    def ticks(self, offset):
        return self.cells[401 + offset] | (self.cells[405 + offset] << 32)


@pytest.mark.parametrize("seconds,expected", (
    (-11644473601, -75), (-11644473600, -75), (-11644473599, 10000000),
    (-1, 116444735990000000), (0, 116444736000000000),
    (1, 116444736010000000), (910692730085, 9223372036850000000),
    (910692730086, -75), (-(1 << 63), -75), ((1 << 63) - 1, -75),
))
def test_windows_tar_time_model_exact_seconds_boundaries(seconds, expected):
    model = _WindowsBoundaries()
    assert model.module()["filetime_from_seconds"](seconds) == expected
    assert model.events == []


def test_windows_tar_time_model_ownership_flags_and_distinct_times():
    model = _WindowsBoundaries(access=-1, write=123456789)
    assert model.module()["utime_file"]("na\u00efve/\U0001f600.txt", 101) == 0
    assert model.events == [
        ("utf16", "na\u00efve/\U0001f600.txt"),
        ("create", 201, 256, 7, 0, 3, 33554432, 0),
        ("free", 201), ("set", 301, 0, 401, 409), ("close", 301),
    ]
    assert model.ticks(0) == 116444735990000000
    assert model.ticks(8) == 117679303890000000
    assert model.errno == 987


@pytest.mark.parametrize("field,seconds", (("access", -11644473600), ("write", -11644473601),
                                            ("access", 910692730086), ("write", (1 << 63) - 1)))
def test_windows_tar_time_model_range_failure_has_no_handle_or_allocation(field, seconds):
    model = _WindowsBoundaries(**{field: seconds})
    assert model.module()["utime_file"]("file", 101) == -1
    assert model.events == [("errno", 75)]


@pytest.mark.parametrize("path,times", ((0, 101), ("file", 0)))
def test_windows_tar_time_model_null_inputs_fail_closed(path, times):
    model = _WindowsBoundaries()
    assert model.module()["utime_file"](path, times) == -1
    assert model.events == [("errno", 22)]


@pytest.mark.parametrize("wide,handle,set_ok,close_ok,status,errno,closes", (
    (0, 301, 1, 1, -1, 22, 0),
    (201, -1, 1, 1, -1, 13, 0),
    (201, 301, 0, 1, -1, 13, 1),
    (201, 301, 0, 0, -1, 13, 1),
    (201, 301, 1, 0, 1, 9, 1),
))
def test_windows_tar_time_model_primary_error_and_cleanup_priority(
    wide, handle, set_ok, close_ok, status, errno, closes,
):
    model = _WindowsBoundaries(wide=wide, handle=handle, set_ok=set_ok, close_ok=close_ok)
    assert model.module()["utime_file"]("file", 101) == status
    assert model.errno == errno
    assert sum(event[0] == "close" for event in model.events) == closes
    assert [event for event in model.events if event[0] == "free"] == (
        [("free", 201)] if wide else []
    )
    if set_ok == 0:
        assert model.events.index(("error", 5)) < model.events.index(("close", 301))
    if handle == -1:
        assert model.events.index(("error", 5)) < model.events.index(("free", 201))


@pytest.mark.parametrize("fail", ("none", "size", "allocation", "conversion"))
def test_windows_tar_time_model_utf16_conversion_owns_one_buffer(fail):
    events = []
    path = "na\u00efve/\U0001f600.txt"
    expected = (path + "\0").encode("utf-16-le")

    def convert(codepage, flags, text, length, output, capacity):
        events.append(("convert", codepage, flags, text, length, output, capacity))
        assert (codepage, flags, text, length) == (65001, 8, path, -1)
        if (fail == "size" and output == 0) or (fail == "conversion" and output != 0):
            return 0
        if output:
            assert capacity == len(expected) // 2
        return len(expected) // 2

    def allocate(size):
        events.append(("malloc", size))
        return 0 if fail == "allocation" else 201

    namespace = _definitions(WINDOWS, ("utf16",), dict(
        ptr_is_null=lambda pointer: pointer == 0, null=lambda: 0,
        MultiByteToWideChar=convert, malloc=allocate,
        free=lambda pointer: events.append(("free", pointer)),
    ))
    assert namespace["utf16"](path) == (201 if fail == "none" else 0)
    assert sum(event[0] == "malloc" for event in events) == (0 if fail == "size" else 1)
    assert [event for event in events if event[0] == "free"] == (
        [("free", 201)] if fail == "conversion" else []
    )
    if fail != "size":
        assert ("malloc", len(expected)) in events


def _check_windows_tar_time_handle_binding():
    """Source/dispatcher-fragment model, not compiler or native execution.

    The current extern c_ptr fast path can prefer a module function over a
    same-named local. Keep the shim outside that unresolved generic defect,
    and retain an explicit old-name counterexample rather than modeling the
    Python interpreter's different binding behavior as native evidence.
    """
    tree = ast.parse(WINDOWS.read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    target = functions["utime_file"]
    original_function = functions["handle"]
    assert any(isinstance(decorator, ast.Call)
               and isinstance(decorator.func, ast.Name)
               and decorator.func.id == "c_abi_export"
               and ast.literal_eval(decorator.args[0]) == "pcc_win_fd_handle"
               for decorator in original_function.decorator_list)
    module_bindings = set(functions)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_bindings.update(item.asname or item.name.split(".")[0] for item in node.names)
        elif isinstance(node, ast.Assign):
            module_bindings.update(item.id for item in node.targets if isinstance(item, ast.Name))
    assert "metadata_handle" not in module_bindings
    # This raw shim has no hidden class/closure or star-import registration.
    # Its explicit scaffold imports cannot introduce an unlisted short name.
    assert sum(isinstance(node, ast.FunctionDef) for node in ast.walk(tree)) == len(functions)
    assert not any(isinstance(node, (ast.ClassDef, ast.Lambda, ast.Import)) for node in ast.walk(tree))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert [node.module for node in imports] == ["pcc", "pcc.extern", "pcc.unsafe"]
    assert all(node in tree.body for node in imports)
    assert not any(item.name == "*" for node in imports for item in node.names)
    handle_names = [node for node in ast.walk(target)
                    if isinstance(node, ast.Name) and node.id in ("handle", "metadata_handle")]
    assert len(handle_names) == 4 and all(node.id == "metadata_handle" for node in handle_names)
    assert sum(isinstance(node.ctx, ast.Store) for node in handle_names) == 1
    calls = {name: [node for node in ast.walk(target) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == name]
             for name in ("CreateFileW", "ptr_to_int", "SetFileTime", "CloseHandle")}
    assert all(len(items) == 1 for items in calls.values())
    created = next(node for node in target.body if isinstance(node, ast.Assign)
                   and node.value is calls["CreateFileW"][0])
    assert len(created.targets) == 1 and created.targets[0].id == "metadata_handle"
    for name in ("ptr_to_int", "SetFileTime", "CloseHandle"):
        assert isinstance(calls[name][0].args[0], ast.Name)
        assert calls[name][0].args[0].id == "metadata_handle"
    assert (calls["CreateFileW"][0].lineno < calls["ptr_to_int"][0].lineno
            < calls["SetFileTime"][0].lineno < calls["CloseHandle"][0].lineno)

    declarations = ast.parse((ROOT / "pcc/frontends/python/codegen/user_function_decl_lowering.py").read_text())
    registration = ast.parse("self.functions[fd.name] = fn").body[0]
    assert any(ast.dump(node) == ast.dump(registration) for node in ast.walk(declarations)
               if isinstance(node, ast.Assign))
    lowering = ast.parse((ROOT / "pcc/frontends/python/codegen/extern_lowering.py").read_text())
    method = next(node for node in ast.walk(lowering)
                  if isinstance(node, ast.FunctionDef) and node.name == "_emit_extern_call_impl")
    # Execute the unchanged parameter loop only, over fake IR values/boundaries.
    # No module imports, function declaration, compiler call or emitted IR runs.
    loop = next(node for node in method.body if isinstance(node, ast.For)
                and ast.dump(node.iter) == ast.dump(ast.parse("enumerate(args)", mode="eval").body))
    fragment = ast.Module(body=[loop], type_ignores=[])
    code = compile(ast.fix_missing_locations(fragment), "<extern-argument-binding-model>", "exec")

    class BindingName:
        def __init__(self, ident):
            self.ident = ident
            self.ty = None

    class BindingExpression:
        def __init__(self, value):
            self.value = value
            self.ty = None

    class Boundary:
        def __init__(self, locals_):
            self.functions = {name: object() for name in functions}
            self.locals = locals_
            self.events = []
            self.builder = SimpleNamespace(bitcast=self.bitcast)
            self._EXTERN_CTYPE_IR = {"c_ptr": "fake-pointer-type"}
            self._try_err_block = None

        def bitcast(self, value, want, name=None):
            self.events.append(("function-pointer", value))
            assert want == "fake-pointer-type"
            return value

        def _fresh(self, name):
            return name

        def _emit_expr_with_cpy_operand_cleanup(self, expr, unused, **keywords):
            assert unused == () and keywords == {"as_object": False, "rooted_pcc_lifetimes": ()}
            self.events.append(("evaluate", expr))
            return self.locals[expr.ident] if isinstance(expr, BindingName) else expr.value

        def _raw_addresses_are_ints(self):
            return False

        def _current_try_err_block(self):
            return None

        def _coerce_to_extern(self, value, source_type, want, ctype):
            assert source_type is None and want == "fake-pointer-type" and ctype == "c_ptr"
            return value

    scenarios = 0
    for api in ("SetFileTime", "CloseHandle"):
        declaration = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                           and any(isinstance(item, ast.Name) and item.id == api for item in node.targets))
        argtypes = tuple(item.id for item in declaration.args[1].elts)
        assert argtypes == ("c_ptr",) * len(calls[api][0].args)
        for local_name in ("metadata_handle", "handle"):
            created_handle = object()
            owner = Boundary({local_name: created_handle})
            first = BindingName(local_name)
            rest = tuple(BindingExpression(object()) for _ in argtypes[1:])
            namespace = {"self": owner, "args": (first,) + rest, "argtype_names": argtypes,
                         "ir_args": [], "object_roots": [], "object_arg_indices": [],
                         "Name": BindingName, "_CSTR": "fake-pointer-type", "ir": SimpleNamespace(Value=object)}
            exec(code, namespace)
            if local_name == "metadata_handle":
                assert namespace["ir_args"][0] is created_handle
                assert owner.events[0] == ("evaluate", first)
                assert not any(event[0] == "function-pointer" for event in owner.events)
            else:
                assert namespace["ir_args"][0] is owner.functions["handle"]
                assert namespace["ir_args"][0] is not created_handle
                assert owner.events[0] == ("function-pointer", owner.functions["handle"])
                assert ("evaluate", first) not in owner.events
            assert namespace["ir_args"][1:] == [item.value for item in rest]
            assert namespace["object_roots"] == [] and namespace["object_arg_indices"] == []
            scenarios += 1
    # A true unshadowed callback still takes this existing raw function lane.
    owner = Boundary({})
    namespace = {"self": owner, "args": (BindingName("handle"),), "argtype_names": ("c_ptr",),
                 "ir_args": [], "object_roots": [], "object_arg_indices": [],
                 "Name": BindingName, "_CSTR": "fake-pointer-type", "ir": SimpleNamespace(Value=object)}
    exec(code, namespace)
    assert namespace["ir_args"] == [owner.functions["handle"]]
    assert owner.events == [("function-pointer", owner.functions["handle"])]
    scenarios += 1
    assert scenarios == 5


def test_tar_attrs_model_source_dispatch_is_finite_and_owned():
    _check_windows_tar_time_handle_binding()
    _check_tar_attrs_native_phase_witness()
    _check_tar_metadata_control_models()
    _check_tar_control_failure_priority()

    def assignment(path, name):
        tree = ast.parse((ROOT / path).read_text())
        return next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == name
                            for target in node.targets))

    signatures = ast.literal_eval(assignment(
        "pcc/frontends/python/codegen/windows_platform.py", "_SIGNATURES",
    ))
    assert signatures["chmod_file"] == ("i", "pi")
    assert signatures["utime_file"] == ("i", "pp")
    tree = ast.parse(TARFILE.read_text())
    imported = {item.name for node in tree.body if isinstance(node, ast.ImportFrom)
                and node.module == "pcc.unsafe" for item in node.names}
    assert {"chmod_file", "utime_file"} <= imported
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "extern" and node.args
                   and isinstance(node.args[0], ast.Constant)
                   and node.args[0].value in ("chmod", "utime") for node in ast.walk(tree))
    machine_abis = ast.literal_eval(assignment(
        "pcc/frontends/python/codegen/platform_machine_abis.py", "PLATFORM_MACHINE_ABIS",
    ))
    assert machine_abis["SetFileTime"] == ("(c_ptr,c_ptr,c_ptr,c_ptr)", "c_int")
    windows_tree = ast.parse(WINDOWS.read_text())
    exports = {decorator.args[0].value for node in windows_tree.body
               if isinstance(node, ast.FunctionDef) for decorator in node.decorator_list
               if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
               and decorator.func.id in ("c_abi_export", "c_abi_typed_export")}
    assert "pcc_win_utime_file" in exports
    assert not {"utime", "chmod"} & exports


@pytest.mark.parametrize("target", (
    "x86_64-pc-windows-msvc", "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu", "arm64-apple-darwin",
))
def test_tar_attrs_owned_platform_route_ir(tmp_path, target):
    """Cross-target compiler evidence, with no native or foreign execution."""
    from pcc.frontends.python import pipeline

    source = tmp_path / "tar_attribute_route.py"
    source.write_text('''
from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import chmod_file, utime_file
__pcc_freestanding__ = True
@c_abi_export("tar_attribute_route")
def apply(path: c_ptr, times: c_ptr) -> i64:
    if chmod_file(path, 420) != 0:
        return -1
    return utime_file(path, times)
''')
    output = tmp_path / "route.ll"
    pipeline.compile_python(str(source), str(output), emit_llvm_only=True,
                            libpython_mode="off", python_library=True, target_triple=target)
    text = output.read_text()
    if "windows" in target:
        assert "@pcc_win_chmod_file(" in text and "@pcc_win_utime_file(" in text
        assert "@chmod(" not in text and "@utime(" not in text
    else:
        assert "@utime(" in text
        assert re.search(r"\bcall i32\b[^\n]*@utime\(", text)
        assert re.search(r"\bsext i32\b[^\n]*to i64", text)
        assert "@pcc_win_chmod_file(" not in text and "@pcc_win_utime_file(" not in text
        assert ("@chmod(" in text) == ("darwin" in target)


def test_windows_tar_attrs_owned_coff_abi_and_static_mode_route(tmp_path):
    """Compiler/object evidence only; does not execute Windows or a native binary."""
    from pcc.backend.coff_x86_64 import CoffError, parse_object
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.backend.pe_x86_64 import system_dll
    from pcc.frontends.python import pipeline
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
    from pcc.ir.optimization.driver import optimize_ir

    runtime = ROOT / "pcc/runtime"
    output = tmp_path / "windows.ll"
    _compile_runtime_module("freestanding_windows", str(WINDOWS), str(output), TARGET)
    text = optimize_ir(output.read_text(), runtime_ir_passes(str(runtime)))
    assert "define external i64 @pcc_win_utime_file(ptr " in text
    obj = parse_object(emit_owned_object(text, TARGET))
    definitions = {symbol.name for symbol in obj.symbols if symbol.external and symbol.section}
    undefined = {symbol.name for symbol in obj.symbols if symbol.external and not symbol.section}
    assert {"pcc_win_utime_file", "pcc_win_filetime_from_seconds", "pcc_win_chmod_file"} <= definitions
    assert "SetFileTime" in undefined
    assert "utime" not in undefined and "chmod" not in undefined
    assert system_dll("SetFileTime") == "KERNEL32.dll"
    for symbol in ("chmod", "utime", "_utime", "_utime64"):
        with pytest.raises(CoffError, match="has no named system DLL ABI"):
            system_dll(symbol)

    # Compile the real changed managed consumer, not merely the provider.
    consumer = tmp_path / "tarfile.ll"
    pipeline.compile_python(str(TARFILE), str(consumer), emit_llvm_only=True,
                            libpython_mode="off", python_library=True, target_triple=TARGET)
    consumer_text = consumer.read_text()
    assert "@pcc_win_chmod_file(" in consumer_text
    assert "@pcc_win_utime_file(" in consumer_text
    assert "@utime(" not in consumer_text
    assert "@chmod(" not in consumer_text
    assert "@_utime" not in consumer_text


@pytest.mark.integration
@pytest.mark.parametrize("import_style", ("module", "static"))
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64"))
def test_windows_tar_attrs_native_files_directories_unicode_and_negative_epoch(
    tmp_path, explicit_owned_runtime, capfd, import_style,
):
    """Normal Windows CI only: run the actual tarfile consumer with owned runtime."""
    import io
    import stat
    import tarfile
    from pcc.frontends.python.pipeline import compile_python
    from scripts.verify_nolibpython import linked_libraries
    from tests.python.process_timeout import run_process_group_timeout

    source_tar = tmp_path / "metadata.tar"
    destination = tmp_path / "extracted"
    entries = (
        ("na\u00efve-\U0001f600.txt", 0o640, -1),
        ("readonly.txt", 0o444, 0),
        ("directory", 0o755, 123456789),
    )
    with tarfile.open(source_tar, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, mode, seconds in entries:
            member = tarfile.TarInfo(name)
            member.mode, member.mtime = mode, seconds
            member.type = tarfile.DIRTYPE if name == "directory" else tarfile.REGTYPE
            payload = b"metadata fixture"
            member.size = 0 if member.isdir() else len(payload)
            archive.addfile(member, None if member.isdir() else io.BytesIO(payload))
    # The host oracle uses the same filesystem, including its time resolution.
    # This is not a claim that FAT/exFAT can store arbitrary exact FILETIMEs.
    oracle_directory = tmp_path / "oracle"
    oracle_directory.mkdir()
    expected = {}
    for name, mode, seconds in entries:
        oracle = oracle_directory / name
        if name == "directory":
            oracle.mkdir()
        else:
            oracle.write_bytes(b"metadata fixture")
        os.chmod(oracle, mode)
        try:
            os.utime(oracle, (seconds, seconds))
        except OSError as error:
            pytest.fail("Windows timestamp fixture requires filesystem support for "
                        f"Unix second {seconds}: {error}")
        expected[name] = oracle.stat()
    # Controls own disjoint files. They never materialize or modify the
    # original extraction destination, and run only after extractall attempts it.
    control_root = tmp_path / "metadata-controls"
    control_root.mkdir()
    control_paths, control_expected = {}, {}
    for group in ("leaf", "wrapper"):
        directory = control_root / group
        directory.mkdir()
        for index, (name, mode, seconds) in enumerate(entries):
            target = directory / name
            if name == "directory":
                target.mkdir()
            else:
                target.write_bytes(b"controlled metadata")
            os.chmod(target, 0o666)
            os.utime(target, (123000000, 123000000))
            key = group + str(index)
            control_paths[key] = str(target)
            control_expected[key] = expected[name]
    negative = control_root / "unchanged.txt"
    negative.write_bytes(b"negative controls must not mutate this file")
    os.chmod(negative, 0o600)
    os.utime(negative, (123000000, 123000000))
    negative_before = negative.stat()
    control_paths["negative"] = str(negative)
    control_paths["missing"] = str(control_root / "absent.txt")
    control_paths["nul"] = str(negative) + "\0suffix"
    source = tmp_path / "metadata.py"
    source.write_text(_tar_attrs_native_program(import_style, source_tar, destination, control_paths), encoding="utf-8")
    executable = tmp_path / "metadata.exe"
    compile_python(str(source), str(executable), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime))
    captured = capfd.readouterr()
    (tmp_path / "compiler.stdout").write_text(captured.out)
    (tmp_path / "compiler.stderr").write_text(captured.err)
    libraries = [name.lower() for name in linked_libraries(executable.read_bytes())]
    assert not any(bad in library for library in libraries
                   for bad in ("msvcrt", "ucrt", "python", "llvm")), libraries
    result = run_process_group_timeout([str(executable)], env=dict(os.environ), timeout=30)
    (tmp_path / "execution.stdout").write_text(result.stdout)
    (tmp_path / "execution.stderr").write_text(result.stderr)
    observed = (result.returncode, result.stdout, result.stderr)
    # Record read-only control state even when the original extraction failed.
    # A diagnostic stat failure must not replace its captured process result.
    control_state, control_failures = {}, []
    for key, wanted in (*control_expected.items(), ("negative", negative_before)):
        try:
            actual = Path(control_paths[key]).stat()
            found = (actual.st_mtime_ns, actual.st_atime_ns, bool(actual.st_mode & stat.S_IWRITE))
            required = (wanted.st_mtime_ns, wanted.st_atime_ns, bool(wanted.st_mode & stat.S_IWRITE))
            if key == "negative":
                found = (actual.st_mode, actual.st_size, actual.st_mtime_ns,
                         actual.st_atime_ns, actual.st_ctime_ns)
                required = (wanted.st_mode, wanted.st_size, wanted.st_mtime_ns,
                            wanted.st_atime_ns, wanted.st_ctime_ns)
            control_state[key] = found
            if found != required:
                control_failures.append((key, found, required))
        except OSError as error:
            control_failures.append((key, "stat failed", str(error)))
    try:
        missing_exists = Path(control_paths["missing"]).exists()
        control_state["missing_exists"] = missing_exists
        if missing_exists:
            control_failures.append(("missing", "unexpectedly created"))
    except OSError as error:
        control_failures.append(("missing", "stat failed", str(error)))
    assert observed == (0, _TAR_NATIVE_SUCCESS_STDOUT, ""), (observed, control_state, control_failures)
    assert not control_failures, (observed, control_state, control_failures)
    for name, mode, seconds in entries:
        actual = (destination / name).stat()
        assert actual.st_mtime_ns == expected[name].st_mtime_ns
        assert actual.st_atime_ns == expected[name].st_atime_ns
        assert bool(actual.st_mode & stat.S_IWRITE) == bool(mode & stat.S_IWRITE)
