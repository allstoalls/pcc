"""format() / f-string / str.format / %-formatting match CPython, no-libpython.

A deterministic matrix of values and format specs runs under CPython and as a
pcc-compiled binary; the outputs (results, or exception type and message)
must be identical.  It pins the rewrite of the runtime formatter onto
CPython's own algorithms:

* int and float specs follow ``formatter_unicode.c``: sign-aware zero
  padding groups the zeros too (``format(1234, "08,d") == "0,001,234"``),
  ``_`` groups bin/oct/hex by four, ``c`` and the float presentation types
  apply to ints, and every error message is CPython's.  The old formatter
  printed a bignum's ``x`` format in decimal and garbled its ``o``/``b``.
* float digits are the exact decimal expansion of the double rounded half to
  even (``format(2.0 ** 60, ".0f")`` used to print ...848169), laid out by a
  port of ``format_float_short`` (``format(100.0, ".3") == "1e+02"``).
* ``%`` formatting follows ``unicodeobject.c`` and ``bytesobject.c``
  including ``*`` widths and the 3.15 error texts; ``"%x" % 2**64`` used to
  saturate to ``7fffffffffffffff``.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent('''
    def show(text):
        print(text)


    def int_values():
        return [0, 1, -1, 7, -7, 255, 1234, -1234, 123456789, 2 ** 62 - 1,
                2 ** 62, -(2 ** 62) - 1, 2 ** 63 - 1, -(2 ** 63), 2 ** 64 + 5,
                -(2 ** 70), 10 ** 30, True, False]


    def float_values():
        return [0.0, -0.0, 1.0, -1.5, 0.1, 2.675, 0.125, 1e16, 1e22, 1e23,
                123456.789, 1234.5678, 1e-5, 5e-324, 1.7976931348623157e308,
                2.0 ** 60, 9.995, float("inf"), float("-inf"), float("nan")]


    def spec_parts():
        aligns = ["", "<", ">", "^", "=", "*<", "*^", "0=", "\\u4e2d>"]
        signs = ["", "+", " ", "-"]
        alts = ["", "#"]
        zeros = ["", "0"]
        widths = ["", "1", "9", "24"]
        groups = ["", ",", "_"]
        precisions = ["", ".0", ".3", ".17"]
        return aligns, signs, alts, zeros, widths, groups, precisions


    def pick(seed, count, limit):
        # Deterministic LCG sample of indices in [0, limit).
        out = []
        state = seed
        for _ in range(count):
            state = (state * 1103515245 + 12345) % 2147483648
            out.append(state % limit)
        return out


    def build_spec(index, types):
        aligns, signs, alts, zeros, widths, groups, precisions = spec_parts()
        spec = aligns[index % len(aligns)]
        index = index // len(aligns)
        spec = spec + signs[index % len(signs)]
        index = index // len(signs)
        spec = spec + alts[index % len(alts)]
        index = index // len(alts)
        spec = spec + zeros[index % len(zeros)]
        index = index // len(zeros)
        spec = spec + widths[index % len(widths)]
        index = index // len(widths)
        spec = spec + groups[index % len(groups)]
        index = index // len(groups)
        spec = spec + precisions[index % len(precisions)]
        index = index // len(precisions)
        return spec + types[index % len(types)]


    def try_format(value, spec):
        try:
            return repr(format(value, spec))
        except Exception as exc:
            return type(exc).__name__ + ": " + str(exc)


    def main():
        int_types = ["", "d", "x", "X", "o", "b", "n", "c", "e", "f", "g", "%", "s", "q"]
        float_types = ["", "e", "E", "f", "F", "g", "G", "n", "%", "d", "x"]
        str_types = ["", "s", "d"]
        ints = int_values()
        floats = float_values()
        strs = ["", "a", "abc", "h\\u00e9llo", "\\u4e2d\\u6587"]
        limit = 9 * 4 * 2 * 2 * 4 * 3 * 4
        for position, index in enumerate(pick(11, 1600, limit * len(int_types))):
            value = ints[position % len(ints)]
            spec = build_spec(index, int_types)
            show("i " + repr(value) + " " + repr(spec) + " " + try_format(value, spec))
        for position, index in enumerate(pick(23, 1600, limit * len(float_types))):
            value = floats[position % len(floats)]
            spec = build_spec(index, float_types)
            show("f " + repr(value) + " " + repr(spec) + " " + try_format(value, spec))
        for position, index in enumerate(pick(37, 500, limit * len(str_types))):
            value = strs[position % len(strs)]
            spec = build_spec(index, str_types)
            show("s " + repr(value) + " " + repr(spec) + " " + try_format(value, spec))
        # Hand-picked shapes: grouping with zero fill, fraction grouping,
        # bignums in every base, presentation-type errors.
        edge = [(1234, "08,d"), (-1234, "08,d"), (255, "#012_b"), (2 ** 64, "_x"),
                (2 ** 70, "#050x"), (-(2 ** 63), "o"), (2 ** 64 - 1, "016x"),
                (5, ",x"), (5, "_n"), (5, "xx"), (5, ".x"), (5, "z"), (5, "+c"),
                (0x110000, "c"), (-1, "c"), (65, "05c"), (5, "\\u4e2d^6x"),
                (1234.5678, ".6,e"), (123456.789, "020,.4_f"), (1.25, ".1_,f"),
                (100.0, ".3"), (1e16, "#"), (-0.0001, "z.2f"), (0.5, ".1%"),
                (2 ** 2000, "e"), ("ab", "=5s"), ("ab", "+5"), ("ab", ",s"),
                (None, ""), (None, "5"), ([1], "")]
        for value, spec in edge:
            show("e " + repr(spec) + " " + try_format(value, spec))
        big = 2 ** 64 + 5
        show(f"{big:x} {big:020x} {big:,} {2 ** 64 - 1:#018x} {-7:=+8.2f}")
        show("{:x}|{:>30_x}|{:+,}|{:^9.3%}".format(2 ** 64, 2 ** 64, -(10 ** 20), 0.125))
        for fmt, args in [("%x", 2 ** 64), ("%08X", 2 ** 70), ("%d", 2 ** 70),
                          ("%o", 2 ** 70), ("%#x", 2 ** 70), ("%30d|", 2 ** 70),
                          ("%-30x|", 2 ** 70), ("%.30d", 2 ** 70), ("%.*f", (-1, 1.5)),
                          ("%*d", (-5, 42)), ("%08.3d", 5), ("%-05d", 5), ("%+05d", 5),
                          ("% 05d", -5), ("%#08x", 255), ("%#-8x|", 255), ("%#o", 8),
                          ("%#.5o", 8), ("%x", 3.5), ("%d", -3.7), ("%d", "a"),
                          ("%c", 0x4e2d), ("%5c|", "\\u00e9"), ("%c", "ab"),
                          ("%c", 0x110000), ("%.2s|", "h\\u00e9llo"), ("%6s|", "h\\u00e9llo"),
                          ("%q", 1), ("%lld", 5), ("%5.1f|", 2.25), ("%#.0f", 1.0),
                          ("%#g", 1.0), ("%f", 5), ("%e", 2 ** 2000), ("%05.1f", -2.5),
                          ("%d %d", (1, "a")), ("%(x)d", {"x": "a"}), ("%s %*d", ("a", 2.0, 3)),
                          ("ab%", 1), ("ab%(x", {"x": 1}), ("%(x)s", 1), ("%s", {"a": 1}),
                          ("%(a)s %s", {"a": 1}), ("%s %(a)s", {"a": 1}), ("%(a)s", {}),
                          ("abc", 5), ("%d", (1, 2)), ("%s %s", 1), ("%d", float("nan")),
                          ("%d", float("inf")), ("%d", 1e20), ("%.1000f", 0.1)]:
            try:
                show("% " + repr(fmt) + " " + repr(fmt % args))
            except Exception as exc:
                show("% " + repr(fmt) + " " + type(exc).__name__ + ": " + str(exc))
        for fmt, args in [(b"%s", b"ab"), (b"%s", "ab"), (b"%c", 65), (b"%c", 256),
                          (b"%c", b"ab"), (b"%r", "\\u00e9"), (b"%5s|", b"ab"),
                          (b"%.1s", b"ab"), (b"%x", 255), (b"%s", 5), (b"abc", (1,)),
                          (b"%(a)s", {b"a": b"z"})]:
            try:
                show("b " + repr(fmt) + " " + repr(fmt % args))
            except Exception as exc:
                show("b " + repr(fmt) + " " + type(exc).__name__ + ": " + str(exc))


    if __name__ == "__main__":
        main()
''').lstrip()


def test_format_matches_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "formats.py"
    exe = tmp_path / "formats.out"
    src.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    got = result.stdout.splitlines()
    want = expected.stdout.splitlines()
    mismatches = [
        (index, want_line, got_line)
        for index, (want_line, got_line) in enumerate(zip(want, got))
        if want_line != got_line
    ]
    assert not mismatches, "\n".join(
        f"line {index}:\n  cpython {want_line}\n  pcc     {got_line}"
        for index, want_line, got_line in mismatches[:20]
    )
    assert len(got) == len(want)
