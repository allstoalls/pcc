"""Closure-safe pcc1 ``--pytest`` launcher and source harness.

This is the deliberately small pytest-compatible subset used by the native
bootstrap compiler.  It discovers top-level ``test_*`` functions, rewrites a
test file into an explicit ``pcc.test_runner`` entry, asks the current pcc1 to
compile that entry without libpython, and executes the result.

Keep this module self-contained and inside the stage1 source closure.  In
particular it must not import :mod:`pcc.driver.cli_bootstrap`: that would turn the
extraction into a cycle and make the native launcher depend on host-Python
attribute callbacks.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _pcc1_pytest_write_text(
    text: str,
    *,
    err: bool = False,
    nl: bool = True,
) -> None:
    if nl:
        if text.endswith("\n"):
            if err:
                sys.stderr.write(text)
            else:
                sys.stdout.write(text)
        else:
            if err:
                sys.stderr.write(text + "\n")
            else:
                sys.stdout.write(text + "\n")
    else:
        if err:
            sys.stderr.write(text)
        else:
            sys.stdout.write(text)


def _pcc1_pytest_find_from(text: str, needle: str, start: int) -> int:
    """Bootstrap-safe substring search without a dynamic ``str.find`` call."""
    if needle == "":
        return start
    i = start
    limit = len(text) - len(needle)
    while i <= limit:
        j = 0
        matched = True
        while j < len(needle):
            if text[i + j] != needle[j]:
                matched = False
                break
            j += 1
        if matched:
            return i
        i += 1
    return -1


def _pcc1_pytest_sanitize_tag(text: str) -> str:
    out = ""
    i = 0
    while i < len(text):
        ch = text[i]
        ok = (
            ("a" <= ch <= "z")
            or ("A" <= ch <= "Z")
            or ("0" <= ch <= "9")
            or ch == "_"
        )
        out += ch if ok else "_"
        i += 1
    return out or "unknown"


def _pcc1_pytest_run_checked(args, timeout_seconds: int) -> None:
    if timeout_seconds <= 0:
        raise ValueError("pcc1 pytest subprocess timeout must be positive")
    subprocess.run(args, check=True, timeout=timeout_seconds)


def _pytest_marker_arg(pytest_args) -> str:
    marker = ""
    i = 0
    while i < len(pytest_args):
        arg = pytest_args[i]
        if arg == "-m":
            if i + 1 < len(pytest_args):
                marker = pytest_args[i + 1]
                i += 1
        elif arg.startswith("-m") and len(arg) > 2:
            marker = arg[2:]
        i += 1
    if marker == "":
        marker = "not integration"
    if len(marker) >= 2:
        first = marker[0]
        last = marker[len(marker) - 1]
        if (first == "'" and last == "'") or (first == '"' and last == '"'):
            marker = marker[1 : len(marker) - 1]
    return marker


def _pytest_path_args(pytest_args):
    paths = []
    i = 0
    while i < len(pytest_args):
        arg = pytest_args[i]
        if arg in ("-m", "-k", "-n", "--maxfail", "--tb"):
            i += 2
            continue
        if (
            arg == "-q"
            or arg == "-s"
            or arg == "-v"
            or arg == "-n0"
            or arg.startswith("--")
            or arg.startswith("-m")
            or arg.startswith("-k")
            or arg.startswith("--tb=")
            or arg.startswith("--maxfail=")
        ):
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        paths.append(arg)
        i += 1
    if len(paths) == 0:
        paths.append("tests")
    return paths


def _pcc1_pytest_is_test_file(path: str) -> bool:
    name = os.path.basename(path)
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def _pcc1_pytest_collect_files_from(path: str, out) -> None:
    if os.path.isfile(path):
        if _pcc1_pytest_is_test_file(path):
            out.append(path)
        return
    if not os.path.isdir(path):
        return
    try:
        names = sorted(os.listdir(path))
    except Exception:
        return
    i = 0
    while i < len(names):
        name = names[i]
        child = os.path.join(path, name)
        if (
            name == "__pycache__"
            or name == ".pytest_cache"
            or name == ".git"
            or name == "build"
            or name == "build_py"
            or name == "projects"
        ):
            i += 1
            continue
        if os.path.isdir(child):
            _pcc1_pytest_collect_files_from(child, out)
        elif _pcc1_pytest_is_test_file(child):
            out.append(child)
        i += 1


def _pcc1_pytest_collect_files(paths):
    out = []
    i = 0
    while i < len(paths):
        _pcc1_pytest_collect_files_from(paths[i], out)
        i += 1
    return out


def _pcc1_pytest_module_is_integration(text: str) -> bool:
    return (
        _pcc1_pytest_find_from(
            text,
            "pytestmark = pytest.mark.integration",
            0,
        )
        >= 0
        or _pcc1_pytest_find_from(
            text,
            "pytestmark=pytest.mark.integration",
            0,
        )
        >= 0
        or _pcc1_pytest_find_from(
            text,
            "pytestmark = [pytest.mark.integration",
            0,
        )
        >= 0
        or _pcc1_pytest_find_from(
            text,
            "pytestmark=[pytest.mark.integration",
            0,
        )
        >= 0
    )


def _pcc1_pytest_include_by_marker(is_integration: bool, marker: str) -> bool:
    if marker == "integration":
        return is_integration
    if marker == "not integration":
        return not is_integration
    return True


def _pcc1_pytest_skipif_literal(stripped: str):
    prefix = "@pytest.mark.skipif("
    if not stripped.startswith(prefix):
        return None
    rest = stripped[len(prefix) :]
    if rest.startswith("True") and (
        len(rest) == 4 or rest[4] == "," or rest[4] == ")"
    ):
        return True
    if rest.startswith("False") and (
        len(rest) == 5 or rest[5] == "," or rest[5] == ")"
    ):
        return False
    return None


def _pcc1_pytest_is_skip_decorator(stripped: str) -> bool:
    return stripped == "@pytest.mark.skip" or stripped.startswith(
        "@pytest.mark.skip("
    )


def _pcc1_pytest_def_name(line: str):
    if not line.startswith("def test_"):
        return None
    paren = _pcc1_pytest_find_from(line, "(", 0)
    if paren < 0:
        return None
    return line[4:paren]


def _pcc1_pytest_discover_funcs(text: str, marker: str):
    funcs = []
    module_integration = _pcc1_pytest_module_is_integration(text)
    pending_integration = False
    pending_skip = False
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if stripped.startswith("@pytest.mark.integration"):
            pending_integration = True
            i += 1
            continue
        if _pcc1_pytest_is_skip_decorator(stripped):
            pending_skip = True
            i += 1
            continue
        skipif = _pcc1_pytest_skipif_literal(stripped)
        if skipif is not None:
            if skipif:
                pending_skip = True
            i += 1
            continue
        name = None
        if raw.startswith("def test_"):
            name = _pcc1_pytest_def_name(raw)
        if name is not None:
            is_integration = module_integration or pending_integration
            if not pending_skip and _pcc1_pytest_include_by_marker(
                is_integration,
                marker,
            ):
                funcs.append(name)
            pending_integration = False
            pending_skip = False
            i += 1
            continue
        if stripped.startswith("@"):
            i += 1
            continue
        if stripped != "" and not stripped.startswith("#"):
            pending_integration = False
            pending_skip = False
        i += 1
    return funcs


def _pcc1_pytest_rewrite_metadata_assignments(text: str) -> str:
    out = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if (
            raw == stripped
            and stripped.startswith("pytestmark")
            and _pcc1_pytest_find_from(
                stripped,
                "pytest.mark.integration",
                0,
            )
            >= 0
        ):
            out.append("pytestmark = None")
        else:
            out.append(raw)
        i += 1
    return "\n".join(out)


def _pcc1_pytest_write_runner_source(
    src_path: str,
    dest_path: str,
    marker: str,
):
    try:
        with open(src_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        _pcc1_pytest_write_text(
            "Error: pcc1 pytest could not read " + src_path,
            err=True,
        )
        return 0
    funcs = _pcc1_pytest_discover_funcs(text, marker)
    if len(funcs) == 0:
        return 0
    text = _pcc1_pytest_rewrite_metadata_assignments(text)
    with open(dest_path, "w", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")
        fh.write("\nfrom pcc.test_runner import run_tests\n")
        fh.write('\nif __name__ == "__main__":\n')
        fh.write("    run_tests([")
        i = 0
        while i < len(funcs):
            if i > 0:
                fh.write(", ")
            fh.write(funcs[i])
            i += 1
        fh.write("])\n")
    return len(funcs)


def run_pcc1_pytest(argv, executable: str, timeout_seconds: int) -> int:
    """Run the native pytest subset using ``executable`` as the compiler.

    ``argv`` retains the launcher token (``--pytest`` or ``pytest``) in slot
    zero.  The caller supplies both the current native compiler path and the
    repository-wide bounded subprocess timeout, keeping stage/timeout policy
    in the CLI facade while this module owns the harness transaction.
    """
    pytest_args = []
    i = 1
    while i < len(argv):
        pytest_args.append(argv[i])
        i += 1
    marker = _pytest_marker_arg(pytest_args)
    if marker != "integration" and marker != "not integration":
        _pcc1_pytest_write_text(
            "Error: pcc1 pytest subset supports only -m integration or "
            "-m 'not integration'",
            err=True,
        )
        return 2
    files = _pcc1_pytest_collect_files(_pytest_path_args(pytest_args))
    if len(files) == 0:
        _pcc1_pytest_write_text("pcc1 pytest: no tests collected")
        return 5
    root = os.environ.get("TMPDIR") or "/tmp"
    scratch = os.path.join(root, "pcc1-pytest-" + str(os.getpid()))
    try:
        _pcc1_pytest_run_checked(
            ["mkdir", "-p", scratch],
            timeout_seconds,
        )
    except Exception:
        _pcc1_pytest_write_text(
            "Error: pcc1 pytest could not create scratch directory",
            err=True,
        )
        return 1
    compiled = 0
    failed = 0
    i = 0
    while i < len(files):
        src = files[i]
        tag = _pcc1_pytest_sanitize_tag(src)
        runner_src = os.path.join(
            scratch,
            "runner_" + str(i) + "_" + tag + ".py",
        )
        exe = os.path.join(
            scratch,
            "runner_" + str(i) + "_" + tag + ".out",
        )
        count = _pcc1_pytest_write_runner_source(src, runner_src, marker)
        if count <= 0:
            i += 1
            continue
        compiled += 1
        try:
            _pcc1_pytest_run_checked(
                [
                    executable,
                    runner_src,
                    "-o",
                    exe,
                    "--python-libpython=off",
                    "--ir-scaffold=on",
                ],
                timeout_seconds,
            )
            _pcc1_pytest_run_checked([exe], timeout_seconds)
        except Exception:
            failed += 1
        i += 1
    if compiled == 0:
        _pcc1_pytest_write_text("pcc1 pytest: no tests selected")
        return 5
    _pcc1_pytest_write_text(
        str(compiled - failed)
        + " pcc1 pytest file(s) passed, "
        + str(failed)
        + " failed"
    )
    if failed != 0:
        return 1
    return 0
