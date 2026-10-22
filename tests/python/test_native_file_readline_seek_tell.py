"""Native file-object readline()/seek()/tell()/flush() lowering.

S-P0-SELF-FILE-READLINE-SEEK-TELL: the no-libpython file object grows the
CPython positioning/line surface. IR-shape tests prove the calls lower to
the py_file_* runtime helpers (no cpy fallback); the round-trip tests
compile with libpython_mode="off" and diff the native binary's stdout
against real python3 running the same source.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


def test_readline_seek_tell_flush_use_native_file_runtime():
    program = textwrap.dedent(
        """
        def f(path: str):
            fh = open(path, "r")
            line = fh.readline()
            pos = fh.tell()
            fh.seek(0)
            fh.seek(0, 2)
            fh.flush()
            fh.close()
            return line
        """
    )
    ir = _compile_to_ll(program, "native_file_readline_ir", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_file_open" in body, body
    assert "@py_file_readline" in body, body
    assert "@py_file_tell" in body, body
    assert "@py_file_seek" in body, body
    assert "@py_file_flush" in body, body
    assert "@py_file_close" in body, body
    assert "cpy.builtin.open" not in body, body
    assert "cpy.fn.readline" not in body, body
    assert "cpy.fn.seek" not in body, body
    assert "cpy.fn.tell" not in body, body
    assert "cpy.fn.flush" not in body, body


def _run_native_vs_python3(tmp_path, program: str, name: str) -> None:
    """Compile no-libpython, run, and diff against real python3."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / f"{name}.py"
    exe = tmp_path / f"{name}.out"
    src.write_text(program)
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    native = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert native.returncode == 0, native.stderr

    python3 = shutil.which("python3")
    assert python3 is not None, "python3 oracle not found on PATH"
    ref = subprocess.run(
        [python3, str(src)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ref.returncode == 0, ref.stderr
    assert native.stdout == ref.stdout


def test_native_file_readline_seek_tell_round_trip(tmp_path):
    data = tmp_path / "native-file-lines.txt"
    program = textwrap.dedent(
        f"""
        PATH = {str(data)!r}

        def main() -> None:
            f = open(PATH, "w")
            f.write("ab\\ncd\\n")
            f.flush()
            f.close()
            g = open(PATH, "r")
            print(g.readline())
            print(g.tell())
            print(g.seek(0))
            print(g.readline())
            print(g.readline(1))
            print(g.seek(0, 2))
            print(g.readline())
            print(g.seek(2, 0))
            print(g.readline())
            g.close()

        if __name__ == "__main__":
            main()
        """
    ).lstrip()
    _run_native_vs_python3(tmp_path, program, "native_file_readline_rt")


def test_native_file_readline_binary_round_trip(tmp_path):
    data = tmp_path / "native-file-bin.txt"
    program = textwrap.dedent(
        f"""
        PATH = {str(data)!r}

        def main() -> None:
            f = open(PATH, "w")
            f.write("ab\\ncd\\n")
            f.close()
            h = open(PATH, "rb")
            line = h.readline()
            print(len(line))
            print(h.tell())
            print(h.seek(1, 1))
            rest = h.readline()
            print(len(rest))
            print(h.tell())
            h.close()

        if __name__ == "__main__":
            main()
        """
    ).lstrip()
    _run_native_vs_python3(tmp_path, program, "native_file_readline_bin_rt")


def test_native_file_binary_write_preserves_bytes_like_payloads(tmp_path):
    data = tmp_path / "native-file-write.bin"
    program = textwrap.dedent(
        f"""
        PATH = {str(data)!r}

        def main() -> None:
            first = b"\\x00\\xffPCC"
            second = bytearray(b"\\x01\\x02")
            third = memoryview(b"\\x03\\x04")
            with open(PATH, "wb") as stream:
                print(stream.write(first))
                print(stream.write(second))
                print(stream.write(third))
            with open(PATH, "rb") as stream:
                payload = stream.read()
            print(len(payload))
            print(payload.hex())

        if __name__ == "__main__":
            main()
        """
    ).lstrip()
    _run_native_vs_python3(tmp_path, program, "native_file_binary_write_rt")


def test_native_file_iteration_round_trip(tmp_path):
    data = tmp_path / "native-file-iteration.txt"
    program = textwrap.dedent(
        f"""
        PATH = {str(data)!r}

        def main() -> None:
            f = open(PATH, "w")
            f.write("alpha\\n\\nomega")
            f.close()

            text_file = open(PATH, "r")
            print(iter(text_file) is text_file)
            for line in text_file:
                print("text", len(line))
            print(text_file.readline() == "")
            text_file.close()

            binary_file = open(PATH, "rb")
            print(iter(binary_file) is binary_file)
            for line in binary_file:
                print("binary", len(line))
            print(binary_file.readline() == b"")
            binary_file.close()

        if __name__ == "__main__":
            main()
        """
    ).lstrip()
    _run_native_vs_python3(tmp_path, program, "native_file_iteration_rt")


def test_native_file_closed_raises_valueerror_round_trip(tmp_path):
    data = tmp_path / "native-file-closed.txt"
    program = textwrap.dedent(
        f"""
        PATH = {str(data)!r}

        def main() -> None:
            f = open(PATH, "w")
            f.write("ab\\n")
            f.close()
            g = open(PATH, "r")
            g.close()
            try:
                g.readline()
                print("readline: no error")
            except ValueError as e:
                print("readline ValueError:", e)
            try:
                g.tell()
                print("tell: no error")
            except ValueError as e:
                print("tell ValueError:", e)
            try:
                g.seek(0)
                print("seek: no error")
            except ValueError as e:
                print("seek ValueError:", e)
            try:
                g.flush()
                print("flush: no error")
            except ValueError as e:
                print("flush ValueError:", e)

        if __name__ == "__main__":
            main()
        """
    ).lstrip()
    _run_native_vs_python3(tmp_path, program, "native_file_closed_rt")
