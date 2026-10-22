"""subprocess.run captures and redirects streams natively, as CPython does.

The owned provider used to ignore ``capture_output``: ``run`` returned a
CompletedProcess whose stdout and stderr were None, so pcc1's package
build-exec (``(proc.stdout or proc.stderr).strip()``) died with
AttributeError, and ``cwd=`` or ``env=`` with ``timeout=`` raised
NotImplementedError. The program below re-runs itself as the child and
prints what run/check_output report; the native binary must print exactly
what CPython prints, under every collector.
"""
from __future__ import annotations

import os
import subprocess
import sys


PROGRAM = r'''
import os
import subprocess
import sys
import time


def self_command():
    if sys.implementation.name == "cpython":
        return [sys.executable, sys.argv[0]]
    return [sys.argv[0]]


def child(mode):
    if mode == "echo":
        sys.stdout.write("out-line\r\nsecond\n")
        sys.stdout.flush()
        sys.stderr.write("err-line\n")
        sys.stderr.flush()
        sys.exit(3)
    if mode == "quiet":
        sys.stdout.write("fine\n")
        sys.stdout.flush()
        return
    if mode == "listing":
        print(",".join(sorted(os.listdir("."))))
        return
    if mode == "env":
        print(os.environ.get("PCC_CAPTURE_PROBE", "missing"))
        return
    if mode == "sleep":
        time.sleep(5)
        return
    if mode == "big":
        sys.stdout.write("o" * 1048576)
        sys.stdout.flush()
        sys.stderr.write("e" * 1048576)
        sys.stderr.flush()
        return


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "child":
        child(sys.argv[2])
        return
    workdir = sys.argv[1]
    command = self_command()

    result = subprocess.run(command + ["child", "echo"], capture_output=True)
    print("bytes", result.returncode, repr(result.stdout), repr(result.stderr))
    result = subprocess.run(command + ["child", "echo"], capture_output=True, text=True)
    print("text", result.returncode, repr(result.stdout), repr(result.stderr))
    result = subprocess.run(command + ["child", "echo"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    print("merged", result.returncode, repr(result.stdout), repr(result.stderr))
    result = subprocess.run(command + ["child", "echo"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    print("devnull", result.returncode, repr(result.stdout), repr(result.stderr))
    result = subprocess.run(command + ["child", "listing"], cwd=workdir,
                            capture_output=True, text=True)
    print("cwd", result.returncode, repr(result.stdout))
    environment = {"PCC_CAPTURE_PROBE": "replaced", "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(command + ["child", "env"], env=environment, timeout=30,
                            capture_output=True, text=True)
    print("env", result.returncode, repr(result.stdout))
    result = subprocess.run(command + ["child", "big"], capture_output=True)
    print("big", result.returncode, len(result.stdout), len(result.stderr),
          result.stdout[:3], result.stderr[-3:])
    try:
        subprocess.run(command + ["child", "sleep"], capture_output=True, timeout=0.5)
        print("timeout missing")
    except subprocess.TimeoutExpired as error:
        print("timeout", error.timeout)
    try:
        subprocess.run(command + ["child", "echo"], capture_output=True, text=True,
                       check=True)
        print("check missing")
    except subprocess.CalledProcessError as error:
        print("check", error.returncode, repr(error.stdout), repr(error.stderr))
        print("message", str(error).replace(repr(command + ["child", "echo"]), "CMD"))
    try:
        subprocess.run(["pcc-no-such-program-7f6e"], capture_output=True)
        print("missing ran")
    except FileNotFoundError as error:
        print("missing", error.errno)
    print("check_output", repr(subprocess.check_output(command + ["child", "quiet"],
                                                       text=True)))


main()
'''


def test_native_subprocess_run_captures_like_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "capture.py"
    exe = tmp_path / "capture.out"
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "marker-a").write_text("a")
    (workdir / "marker-b").write_text("b")
    src.write_text(PROGRAM)
    ref = subprocess.run(
        [sys.executable, str(src), str(workdir)],
        capture_output=True, text=True, timeout=120,
    )
    assert ref.returncode == 0, ref.stderr
    assert "missing 2" in ref.stdout and "timeout 0.5" in ref.stdout
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    for backend in range(5):
        native = subprocess.run(
            [str(exe), str(workdir)], capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert native.returncode == 0, f"GC{backend}: {native.stderr}"
        assert native.stdout == ref.stdout, f"GC{backend}"
