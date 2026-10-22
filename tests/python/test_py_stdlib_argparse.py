from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from pcc.stdlib import argparse


def _run_pcc_program(tmp_path: Path, source: str, args: list[str] | None = None) -> str:
    src = tmp_path / "prog.py"
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    exe = tmp_path / "prog"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_GC_BACKEND"] = "4"
    build = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(src),
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=420,
        env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)] + list(args or []),
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_argparse_subset_host_python():
    def convert(text):
        return "item:" + text

    parser = argparse.ArgumentParser(description="demo")
    parser.add_argument("-n", dest="n", type=int, default=0)
    parser.add_argument("-x", dest="items", action="append", default=[], type=convert)
    parser.add_argument("-v", dest="v", action="count")
    parser.add_argument("--flag", action="store_true")
    parser.add_argument("-s", dest="s", default="fa", choices=("fa", "rr"))
    args = parser.parse_args(
        ["-n", "41", "-x", "a", "-x", "b", "-v", "-v", "--flag", "-s", "rr"]
    )

    assert args.n == 41
    assert args.items == ["item:a", "item:b"]
    assert args.v == 2
    assert args.flag is True
    assert args.s == "rr"


def test_builtin_type_callable_values_no_libpython_self_backend(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        """
        def apply(t, value):
            return t(value)

        print(apply(int, "42") + 1)
        print(apply(str, 99))
        """,
    )
    assert out.splitlines() == ["43", "99"]


def test_argparse_subset_no_libpython_self_backend(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        """
        import argparse

        def convert(text):
            return "item:" + text

        parser = argparse.ArgumentParser(description="demo")
        parser.add_argument("-n", dest="n", type=int, default=0)
        parser.add_argument("-x", dest="items", action="append", default=[], type=convert)
        parser.add_argument("-v", dest="v", action="count")
        parser.add_argument("--flag", action="store_true")
        parser.add_argument("-s", dest="s", default="fa", choices=("fa", "rr"))
        args = parser.parse_args()
        print(args.n + 1)
        print(args.items[0])
        print(args.items[1])
        print(args.v)
        print(args.flag)
        print(args.s)
        """,
        ["-n", "41", "-x", "a", "-x", "b", "-v", "-v", "--flag", "-s", "rr"],
    )
    assert out.splitlines() == ["42", "item:a", "item:b", "2", "True", "rr"]
