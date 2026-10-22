"""Native text files accept every codec error handler bytes.decode accepts.

The no-libpython file object decodes through the shared text codec and keeps
a raw-offset map for tell(). Handlers whose output does not mirror the input
(ignore, replace, backslashreplace) used to be rejected at open() because
that map was inferred from the decoded text. The program below runs natively
and under python3; stdout must match, including tell()/seek() resumption
across the 8 KiB decoder chunk boundary.
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap


PROGRAM = textwrap.dedent(
    """
    from pathlib import Path

    BASE = __BASE__
    PATTERN = b"ab\\xffcd\\xe2\\x82xy\\xed\\xa0\\x80z\\r\\nq\\xc3"
    HANDLERS = ("strict", "ignore", "replace", "surrogateescape",
                "backslashreplace", "surrogatepass", "xmlcharrefreplace",
                "namereplace", "bogus")


    def write_sample(path: str, prefix: int, repeat: int) -> None:
        with open(path, "wb") as f:
            f.write(b"p" * prefix + PATTERN * repeat)


    def read_whole(path: str, errors: str) -> str:
        try:
            with open(path, encoding="utf-8", errors=errors) as f:
                return ascii(f.read())
        except Exception as exc:
            return type(exc).__name__


    def read_lines(path: str, errors: str) -> str:
        try:
            with open(path, encoding="utf-8", errors=errors) as f:
                return ascii(f.readlines()[-2:])
        except Exception as exc:
            return type(exc).__name__


    def read_stepwise(path: str, errors: str, total: int) -> str:
        # One character per read() drives the chunked decoder. A tell()
        # cookie taken after a character must resume at the next one; with
        # a total, only every 41st character and the last 5% are sampled.
        try:
            f = open(path, encoding="utf-8", errors=errors)
        except Exception as exc:
            return type(exc).__name__
        result = ""
        try:
            chars = []
            counts = []
            marks = []
            while True:
                ch = f.read(1)
                if not ch:
                    break
                chars.append(ch)
                count = len(chars)
                if total == 0 or count % 41 == 0 or count * 20 >= total * 19:
                    counts.append(count)
                    marks.append(f.tell())
            text = "".join(chars)
            index = 0
            while index < len(marks) and result == "":
                count = counts[index]
                f.seek(marks[index])
                if f.read(3) != text[count:count + 3]:
                    result = "resume mismatch after " + str(count)
                index = index + 1
            if result == "":
                result = str(len(text)) + " " + ascii(text[-24:])
        except Exception as exc:
            result = type(exc).__name__
        f.close()
        return result


    def write_text(path: str, encoding: str, errors: str) -> str:
        try:
            with open(path, "w", encoding=encoding, errors=errors) as f:
                f.write("a\\xe9b")
        except Exception as exc:
            return type(exc).__name__
        with open(path, "rb") as f:
            return ascii(f.read())


    def main() -> None:
        small = BASE + "/small.txt"
        write_sample(small, 0, 1)
        for errors in HANDLERS:
            print(errors, read_whole(small, errors), read_stepwise(small, errors, 0))
        # 482 patterns end just past the decoder's 8 KiB chunk; each prefix
        # splits a different CRLF or multibyte sequence at that boundary.
        for prefix in (1, 4, 5, 8, 9):
            large = BASE + "/large" + str(prefix) + ".txt"
            write_sample(large, prefix, 482)
            for errors in ("ignore", "replace", "surrogateescape", "backslashreplace"):
                with open(large, encoding="utf-8", errors=errors) as f:
                    total = len(f.read())
                print(prefix, errors, read_lines(large, errors),
                      read_stepwise(large, errors, total))
        print(ascii(Path(small).read_text(encoding="utf-8", errors="ignore")))
        out = BASE + "/out.txt"
        for errors in ("strict", "ignore", "replace", "backslashreplace",
                       "xmlcharrefreplace", "bogus"):
            print("write", errors, write_text(out, "ascii", errors))
        print("latin-1", write_text(out, "latin-1", "strict"))
        with open(out, encoding="latin-1") as f:
            print(ascii(f.read(2)), f.tell(), ascii(f.read()))
        for errors in ("xmlcharrefreplace", "namereplace", "bogus"):
            try:
                print(ascii(b"a\\xffb".decode("utf-8", errors)))
            except Exception as exc:
                print("decode", errors, type(exc).__name__)


    main()
    """
).lstrip()


def test_native_text_files_accept_codec_error_handlers(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    native_dir = tmp_path / "native"
    oracle_dir = tmp_path / "oracle"
    native_dir.mkdir()
    oracle_dir.mkdir()
    src = tmp_path / "text_file_errors.py"
    exe = tmp_path / "text_file_errors.out"
    src.write_text(PROGRAM.replace("__BASE__", repr(str(native_dir))))
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    native = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
    assert native.returncode == 0, native.stderr

    python3 = shutil.which("python3")
    assert python3 is not None, "python3 oracle not found on PATH"
    oracle_src = tmp_path / "text_file_errors_oracle.py"
    oracle_src.write_text(PROGRAM.replace("__BASE__", repr(str(oracle_dir))))
    ref = subprocess.run([python3, str(oracle_src)], capture_output=True, text=True, timeout=120)
    assert ref.returncode == 0, ref.stderr
    assert native.stdout == ref.stdout
