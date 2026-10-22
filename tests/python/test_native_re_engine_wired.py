"""Strict no-libpython gates for the E1a engine wiring of native re.match/search.

Before the wiring, the native ``re.match``/``re.search`` lowering routed ANY
pattern through the toy Pike-style matcher, which treats ``|``, ``[...]``,
``(...)`` etc. as literal characters — silently diverging from CPython
(e.g. ``re.match("a|b", "b")`` was None). With the wiring, flags==0 patterns
inside the engine subset run on the faithful engine; flags==0 patterns
OUTSIDE the subset raise NotImplementedError instead of silently
mismatching; flags!=0 keeps the legacy toy behavior (documented gap).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

from pcc.frontends.python.pipeline import compile_python


def _build_and_run(tmp_path: Path, source: str) -> list[str]:
    src = tmp_path / "re_engine_wired_probe.py"
    exe = tmp_path / "re_engine_wired_probe"
    src.write_text(dedent(source), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def test_native_re_match_engine_subset_matches_cpython(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        def main():
            print(bool(re.match("a|b", "b")))
            print(bool(re.match("a|b", "c")))
            print(bool(re.match("(ab)+c", "ababc")))
            print(bool(re.search("v[0-9][0-9]?", "name v12 end")))
            print(bool(re.match("[a-z]+_[0-9]", "abc_9")))
            print(bool(re.match("colou?r", "color")))
            print(bool(re.search("end$", "the end")))

        main()
        """,
    )
    # CPython ground truth for the same prints
    assert out == ["True", "False", "True", "True", "True", "True", "True"]


def test_native_re_match_unsupported_pattern_raises(tmp_path):
    # Lookahead joined the engine subset; lookbehind is still outside it.
    out = _build_and_run(
        tmp_path,
        """
        import re

        def main():
            try:
                re.match("(?<=a)b", "ab")
                print("no-raise")
            except NotImplementedError:
                print("raised")

        main()
        """,
    )
    assert out == ["raised"]


def test_native_re_match_object_methods(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        def main():
            m = re.match("v([0-9]+)\\\\.([0-9]+)", "v12.34 rest")
            if m:
                print(m.group(0))
                print(m.group(1))
                print(m.group(2))
                print(m.group())
                print(m.start(1))
                print(m.end(2))
                print(m.span(0)[0])
                print(m.span(0)[1])
                gs = m.groups()
                print(gs[0])
                print(gs[1])
            m2 = re.search("(b+)", "aabbbcc")
            if m2:
                print(m2.start())
                print(m2.end())
            m3 = re.match("(a)?(b)", "b")
            if m3:
                print(m3.group(1) is None)
                print(m3.group(2))
                print(m3.start(1))
            m4 = re.match("v([0-9]+)", "v7x")
            try:
                m4.group(5)
                print("no-idx-raise")
            except IndexError:
                print("idx-raised")

        main()
        """,
    )
    # CPython ground truth for the same program
    assert out == [
        "v12.34",
        "12",
        "34",
        "v12.34",
        "1",
        "6",
        "0",
        "6",
        "12",
        "34",
        "2",
        "5",
        "True",
        "b",
        "-1",
        "idx-raised",
    ]


def test_native_re_fullmatch_and_chained_groups_no_libpython(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re
        from re import fullmatch

        def main():
            m = re.fullmatch(r"\\[([0-9a-fA-F:]*)\\](?::(\\d+)?)?", "[::1]:8080")
            if m:
                host, port = m.groups()
                print(host)
                print(port)
            print(re.fullmatch("abc", "abc").group(0))
            print(re.fullmatch("abc", "abcd") is None)
            m2 = fullmatch("x([0-9]+)", "x42")
            if m2:
                print(m2.groups()[0])

        main()
        """,
    )
    assert out == ["::1", "8080", "abc", "True", "42"]


def test_native_re_compile_pattern_object(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        PAT = re.compile("v([0-9]+)\\\\.([0-9]+)")

        def version_of(p, s: str) -> str:
            m = p.match(s)
            if m:
                return m.group(1)
            return "none"

        def main():
            m = PAT.match("v12.34 rest")
            if m:
                print(m.group(0))
                print(m.group(2))
            print(version_of(PAT, "v7.8"))
            print(version_of(PAT, "nope"))
            table = {"ver": PAT}
            m2 = table["ver"].search("say v1.2!")
            if m2:
                print(m2.start())
            print(PAT.pattern)
            local = re.compile("(b+)c")
            m3 = local.search("abbbc")
            if m3:
                print(m3.group(1))

        main()
        """,
    )
    # Patterns the frontend checker REJECTS (e.g. lookaround) keep today's
    # compile-time fallback boundary and are not probed here.
    assert out == [
        "v12.34",
        "34",
        "7",
        "none",
        "4",
        "v([0-9]+)\\.([0-9]+)",
        "bbb",
    ]


def test_native_re_compile_runtime_composed_pattern_no_libpython(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        PREFIX = "^v"
        PAT = re.compile(PREFIX + "([0-9]+)$", re.MULTILINE)

        def main():
            matched = PAT.search("x\\nv42")
            print(matched.group(1) if matched else "none")

        main()
        """,
    )
    assert out == ["42"]


def test_native_re_findall_engine_semantics(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        PAT = re.compile("[0-9]+")

        def main():
            xs = re.findall("[0-9]+", "a1 bb22 c333")
            for x in xs:
                print("<" + x + ">")
            pairs = re.findall("([a-z]+)=([0-9]+)", "x=1, yy=22")
            for pr in pairs:
                print("<" + pr[0] + "|" + pr[1] + ">")
            opt = re.findall("(a)?b", "ab b")
            for x in opt:
                print("<" + x + ">")
            stars = re.findall("a*", "baa")
            for x in stars:
                print("<" + x + ">")
            ys = PAT.findall("v1.22")
            for y in ys:
                print("<" + y + ">")
            print(len(re.findall("(?=x)", "xax")))
            try:
                re.findall("(?<=a)b", "ab")
                print("no-raise")
            except NotImplementedError:
                print("raised")

        main()
        """,
    )
    assert out == [
        "<1>",
        "<22>",
        "<333>",
        "<x|1>",
        "<yy|22>",
        "<a>",
        "<>",
        "<>",
        "<aa>",
        "<>",
        "<1>",
        "<22>",
        "2",
        "raised",
    ]


def test_native_re_sub_split_engine_semantics(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        PAT = re.compile("a(b+)c")

        def scrub(s: str) -> str:
            return PAT.sub("X", s)

        def main():
            print(scrub("zabbczabc"))
            print(re.sub("x*", "-", "abc"))
            print(re.sub("x*", "-", "xabc"))
            print(re.sub("a", "-", "aaaa", 2))
            xs = re.split("[,;]", "a,b;c")
            for x in xs:
                print("<" + x + ">")
            ys = re.split("([,;])", "a,b;c")
            for y in ys:
                print("<" + y + ">")
            zs = re.split("x*", "abc")
            for z in zs:
                print("<" + z + ">")
            ws = PAT.split("zabbczabc")
            for w in ws:
                print("<" + w + ">")
            try:
                re.sub("a", "\\\\1", "aa")
                print("no-raise")
            except NotImplementedError:
                print("tmpl-raised")

        main()
        """,
    )
    assert out == [
        "zXzX",
        "-a-b-c-",
        "--a-b-c-",
        "--aa",
        "<a>",
        "<b>",
        "<c>",
        "<a>",
        "<,>",
        "<b>",
        "<;>",
        "<c>",
        "<>",
        "<a>",
        "<b>",
        "<c>",
        "<>",
        "<z>",
        "<bb>",
        "<z>",
        "<b>",
        "<>",
        "tmpl-raised",
    ]


def test_native_re_named_groups(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        VER = re.compile("(?P<major>[0-9]+)\\.(?P<minor>[0-9]+)")

        def main():
            m = VER.match("12.34 rest")
            if m:
                print(m.group("major"))
                print(m.group("minor"))
                print(m.group(1))
                print(m.start("minor"))
                gd = m.groupdict()
                print(gd["major"])
                print(gd["minor"])
            m2 = re.match("(?P<a>x)?(?P<b>y)", "y")
            if m2:
                print(m2.group("a") is None)
                print(m2.group("b"))
                gd2 = m2.groupdict()
                print(gd2["a"] is None)
            m3 = VER.match("7.8")
            if m3:
                try:
                    m3.group("nope")
                    print("no-raise")
                except IndexError:
                    print("idx-raised")

        main()
        """,
    )
    assert out == [
        "12",
        "34",
        "12",
        "3",
        "12",
        "34",
        "True",
        "y",
        "True",
        "idx-raised",
    ]


def test_native_re_flags_engine_semantics(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        import re

        UPPER = re.compile("a(b+)c", re.I)

        def main():
            print(bool(re.match("color", "COLOR", 2)))
            print(bool(re.match("[^a]", "A", 2)))
            print(bool(re.search("^ab$", "x\\nab\\ny", 8)))
            print(bool(re.match("a.c", "a\\nc", 16)))
            print(bool(re.match("a.c", "a\\nc")))
            m = UPPER.match("ABBC")
            if m:
                print(m.group(1))
            xs = re.findall("^.", "ab\\ncd", 8)
            for x in xs:
                print("<" + x + ">")
            try:
                re.match("a", "a", 64)
                print("no-raise")
            except NotImplementedError:
                print("flags-raised")

        main()
        """,
    )
    # CPython ground truth: True, False ([^a] folds before negation),
    # True (re.M ^ab$ mid-string), True (re.S), False (no S), "BB"
    # (re.I compiled pattern group), M-mode findall ['a','c'], and re.X
    # (64) is outside the engine mask -> honest raise.
    assert out == [
        "True",
        "False",
        "True",
        "True",
        "False",
        "BB",
        "<a>",
        "<c>",
        "flags-raised",
    ]


def test_native_re_module_calls_accept_flag_expressions_and_keywords(tmp_path):
    # re.compile already folded ``re.I | re.S`` and accepted ``flags=``; the
    # module-level match/search/fullmatch/finditer calls fell back to CPython
    # for both shapes, which no-libpython builds turn into a runtime stub.
    out = _build_and_run(
        tmp_path,
        """
        import re

        OPTIONS = "option('allow-noblas', type: 'boolean', value: true)\\nOPTION(\\"debug_x\\",\\n  type: 'BOOLEAN')"

        def main():
            m = re.search("o(\\\\d)", "xO1 o2", re.I | re.S)
            print(m.group(1) if m else None)
            print(bool(re.match("x.o", "X\\nO1", re.I | re.S)))
            print(bool(re.match("x.o", "X\\nO1", re.I)))
            print(bool(re.fullmatch("a.b", "A\\nB", flags=re.I | re.DOTALL)))
            print([m.group(1) for m in re.finditer("o(\\\\d)", "xO1 o2 O3", flags=re.IGNORECASE)])
            print(re.search(pattern="b", string="abc").span())
            print(re.search("B", "abc", flags=0))
            for match in re.finditer(
                r"option\\(\\s*['\\"]([^'\\"]+)['\\"](?P<body>.*?)\\)",
                OPTIONS,
                re.IGNORECASE | re.DOTALL,
            ):
                print(match.group(1), repr(match.group("body")))

        main()
        """,
    )
    assert out == [
        "1",
        "True",
        "False",
        "True",
        "['1', '2', '3']",
        "(1, 2)",
        "None",
        "allow-noblas \", type: 'boolean', value: true\"",
        "debug_x \",\\n  type: 'BOOLEAN'\"",
    ]
