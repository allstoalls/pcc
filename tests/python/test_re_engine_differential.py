"""Differential tests for the E0 regex engine subset (freestanding_re_engine.py).

The production runtime archive's engine members are linked into a dylib and
their byte-offset results compared against CPython ``re`` for every
supported construct, plus assert that the strict parser REJECTS everything
outside the step-1 subset instead of guessing (the engine must never
silently diverge).
"""

from __future__ import annotations

import ctypes
import re
import subprocess

import pytest


ENGINE_SYMBOLS = (
    "pcc_re_engine_compile_count",
    "pcc_re_engine_run",
    "pcc_re_engine_run_flags",
    "pcc_re_engine_supported",
)

MATCH = 1
NOMATCH = 0
UNSUPPORTED = -1
LIMIT = -2
BADARGS = -3
NONASCII = -4

MAX_GROUPS = 32


@pytest.fixture(scope="session")
def engine(tmp_path_factory, pcc_runtime_archive):
    out_dir = tmp_path_factory.mktemp("re_engine")
    dylib = out_dir / "libpccre.dylib"
    # Pull only the archive members the engine entry points need.
    undefined = [f"-Wl,-u,_{symbol}" for symbol in ENGINE_SYMBOLS]
    proc = subprocess.run(
        [
            "cc",
            "-dynamiclib",
            *undefined,
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(dylib),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    lib = ctypes.CDLL(str(dylib))
    lib.pcc_re_engine_run.restype = ctypes.c_int
    lib.pcc_re_engine_run.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int64,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int64),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int64),
    ]
    lib.pcc_re_engine_supported.restype = ctypes.c_int
    lib.pcc_re_engine_supported.argtypes = [ctypes.c_char_p]
    lib.pcc_re_engine_compile_count.restype = ctypes.c_int64
    lib.pcc_re_engine_compile_count.argtypes = []
    lib.pcc_re_engine_run_flags.restype = ctypes.c_int
    lib.pcc_re_engine_run_flags.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int64,
        ctypes.c_char_p,
        ctypes.c_int64,
        ctypes.c_int64,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int64),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int64),
    ]
    return lib


def run_engine_flags(lib, pattern: str, flags: int, text: str, search: bool):
    caps = (ctypes.c_int64 * (2 * MAX_GROUPS))()
    ngroups = ctypes.c_int64(0)
    raw = text.encode("utf-8")
    status = lib.pcc_re_engine_run_flags(
        pattern.encode("utf-8"),
        flags,
        raw,
        len(raw),
        0,
        1 if search else 0,
        caps,
        len(caps),
        ctypes.byref(ngroups),
    )
    spans = []
    if status == MATCH:
        for g in range(ngroups.value + 1):
            lo, hi = caps[2 * g], caps[2 * g + 1]
            spans.append(None if lo < 0 or hi < 0 else (lo, hi))
    return status, spans


def test_engine_reuses_compiled_program(engine):
    pattern = r"pcc_cache_probe_(?P<word>[a-z]+)_847263"
    before = engine.pcc_re_engine_compile_count()
    assert engine.pcc_re_engine_supported(pattern.encode("utf-8")) == 1
    for _ in range(100):
        status, spans = run_engine(engine, pattern, "pcc_cache_probe_abc_847263", False)
        assert status == MATCH
        assert spans == [(0, 26), (16, 19)]
    after = engine.pcc_re_engine_compile_count()
    assert after - before == 1


def run_engine(lib, pattern: str, text: str, search: bool):
    caps = (ctypes.c_int64 * (2 * MAX_GROUPS))()
    ngroups = ctypes.c_int64(0)
    raw = text.encode("utf-8")
    status = lib.pcc_re_engine_run(
        pattern.encode("utf-8"),
        raw,
        len(raw),
        1 if search else 0,
        caps,
        len(caps),
        ctypes.byref(ngroups),
    )
    spans = []
    if status == MATCH:
        for g in range(ngroups.value + 1):
            lo, hi = caps[2 * g], caps[2 * g + 1]
            spans.append(None if lo < 0 or hi < 0 else (lo, hi))
    return status, spans


def cpython_spans(pattern: str, text: str, search: bool):
    m = (re.search if search else re.match)(pattern, text)
    if m is None:
        return NOMATCH, []
    spans = []
    for g in range(m.re.groups + 1):
        s = m.span(g)
        spans.append(None if s == (-1, -1) else s)
    return MATCH, spans


# Every entry is differentially compared for BOTH match and search.
SUPPORTED_CASES = [
    # Backreferences and lookahead are supported by the production engine.
    (r"(a)\1", "aa"),
    (r"(a)\1", "ab"),
    (r"(ab)\1c", "xababc"),
    (r"(?=a)", "ba"),
    (r"(?=a)a", "a"),
    (r"(?!a)", "ab"),
    (r"x(?!a)", "xaxb"),
    # literals
    ("abc", "abc"),
    ("abc", "abcd"),
    ("abc", "zabc"),
    ("abc", "ab"),
    ("", "abc"),
    ("a", ""),
    # dot
    ("a.c", "abc"),
    ("a.c", "a\nc"),
    (".", "x"),
    (".", "\n"),
    # escapes
    (r"a\.c", "a.c"),
    (r"a\.c", "abc"),
    (r"\d\d", "a42z"),
    (r"\D+", "12ab34"),
    (r"\w+", "  foo_bar9  "),
    (r"\W", "ab c"),
    (r"\s\S", "a b"),
    (r"a\tb", "a\tb"),
    (r"a\nb", "a\nb"),
    # anchors
    (r"^abc", "abc"),
    (r"^abc", "zabc"),
    (r"abc$", "abc"),
    (r"abc$", "abcz"),
    (r"abc$", "abc\n"),
    (r"^$", ""),
    (r"^$", "x"),
    # absolute anchors
    (r"\Afoo", "foo"),
    (r"\Afoo", "xfoo"),
    (r"foo\Z", "foo"),
    (r"foo\Z", "foo\n"),
    (r"foo$", "foo\n"),
    (r"\w+\Z", "a\nbc"),
    (r"\s*(?P<name>\w+)\s*\Z", "  ab  "),
    # word boundaries
    (r"\bfoo\b", "a foo b"),
    (r"\bfoo\b", "afoob"),
    (r"\Boo", "foo"),
    (r"\Boo", "oo"),
    (r"\bver\d+\b", "name ver12 end"),
    # classes
    (r"[abc]+", "zzabccba!"),
    (r"[a-f]+", "xdeadbeefy"),
    (r"[^a-z]+", "abc123def"),
    (r"[a\-c]+", "a-c-b"),
    (r"[]a]+", "]a]"),
    (r"[\d]+", "a42z"),
    (r"[\w.]+", "mod.sub.name rest"),
    (r"[^\s]+", "  token  "),
    (r"[A-Za-z_][A-Za-z0-9_]*", "  _ident9 more"),
    # quantifiers greedy/lazy
    (r"a*", "aaa"),
    (r"a*", "bbb"),
    (r"a+", "aaa"),
    (r"a+", "baa"),
    (r"a?b", "ab"),
    (r"a?b", "b"),
    (r"a*?b", "aaab"),
    (r"a+?b", "aaab"),
    (r"a??b", "ab"),
    (r"<.*>", "<a><b>"),
    (r"<.*?>", "<a><b>"),
    (r"\w+?\d", "abc12"),
    # alternation
    (r"cat|dog", "hotdog"),
    (r"cat|dog", "catalog"),
    (r"a|", "b"),
    (r"x|y|z", "wzv"),
    (r"foo(bar|baz)qux", "foobazqux"),
    # groups
    (r"a(b+)c", "abbbc"),
    (r"a(b+)c", "ac"),
    (r"(a)(b)(c)", "abc"),
    (r"(a(b)c)d", "abcd"),
    (r"(?:ab)+c", "ababc"),
    (r"(ab)+c", "ababc"),
    (r"(a|b)+x", "abbax"),
    (r"(foo)?bar", "bar"),
    (r"(foo)?bar", "foobar"),
    (r"v(\d+)\.(\d+)", "v12.34 tail"),
    (r"([a-z]+)=([a-z]+)", "key=value"),
    # named groups (spans equal positional semantics)
    (r"(?P<ver>[0-9]+)\.(?P<minor>[0-9]+)", "12.34 t"),
    (r"(?P<a>x)?(?P<b>y)", "y"),
    (r"(?P<w>\w+) (\d+)", "ab 12"),
    (r"(?:(?P<k>[a-z]+)=)?(?P<v>\d+)", "n=5"),
    # backtracking pressure
    (r"a*a*b", "aaaab"),
    (r"(a|ab)(c|bcd)", "abcd"),
    (r".*foo", "xfooyfoo"),
    # counted repeats
    (r"a{3}", "aaaa"),
    (r"a{3}", "aa"),
    (r"a{2,3}", "aaaa"),
    (r"a{2,3}?", "aaaa"),
    (r"a{2,}", "aaaa"),
    (r"a{2,}?", "aaaa"),
    (r"a{0,2}b", "aab"),
    (r"a{,3}", "aaaa"),
    (r"\d{2,4}", "a12345z"),
    (r"[ab]{3}", "abba"),
    (r"x{0}y", "y"),
    (r"v\d{1,3}\.\d{1,3}", "v12.345 tail"),
    (r"\x41+", "AAZ"),
    (r"[\x00-\x1f]+", "\t\nA"),
    (r"[^\x20-\x7e]+", "\tA"),
    # malformed braces are literals (CPython-compatible)
    (r"a{x}", "a{x}"),
    (r"a{2", "a{2"),
    (r"a{", "a{"),
    # quantified nullable bodies (CPython empty-iteration rule)
    (r"(a?)*b", "aab"),
    (r"(a?)*", ""),
    (r"(a*)*", "aa"),
    (r"(a?)+", "b"),
    (r"(a*)+", "b"),
    (r"(a|)+x", "aax"),
    (r"(?:ab?)*", "abaab"),
    (r"(a|b|)*c", "abc"),
]

UNSUPPORTED_PATTERNS = [
    # counted repeats over group/multi-op bodies: CPython's MIN/MAX_UNTIL
    # backtracks only the deepest iteration, which diverges from full DFS
    # (minimized: (.{,3}){,3}?[a] on '    a_ba') — rejected, never guessed
    r"(ab){2}",
    r"(a|b){2,3}",
    r"(a){0}",
    r"(?:ab){1,2}",
    r"(.{,3}){,3}?[a]",
    # a VALID brace with nothing to repeat is a CPython re.error
    r"{3}",
    r"(?P<a>x)(?P<a>y)",
    r"(?P=name)",
    r"(?P<1bad>x)",
    r"(?P<>x)",
    r"(?<=a)b",
    r"(?i)abc",
    r"a**",
    r"a{2}{3}",
    r"a{99}",
    r"^{2}",
    r"(abc",
    r"abc)",
    r"[abc",
    r"café",
    "café",
]


@pytest.mark.parametrize("pattern,text", SUPPORTED_CASES)
@pytest.mark.parametrize("search", [False, True])
def test_engine_matches_cpython(engine, pattern, text, search):
    got_status, got_spans = run_engine(engine, pattern, text, search)
    want_status, want_spans = cpython_spans(pattern, text, search)
    assert got_status == want_status, (pattern, text, search, got_status, want_status)
    if want_status == MATCH:
        assert got_spans == want_spans, (pattern, text, search, got_spans, want_spans)


@pytest.mark.parametrize("pattern", UNSUPPORTED_PATTERNS)
def test_engine_rejects_outside_subset(engine, pattern):
    status, _ = run_engine(engine, pattern, "irrelevant", False)
    assert status == UNSUPPORTED
    assert engine.pcc_re_engine_supported(pattern.encode("utf-8")) == 0


FLAG_CASES = [
    # (pattern, flags, text)
    ("color", re.I, "COLOR"),
    ("color", re.I, "colour"),
    ("[a-f]+", re.I, "DEADbeef!"),
    ("[^a]", re.I, "A"),
    ("[^a]", re.I, "b"),
    (r"\w+", re.I, "MiXeD_9"),
    ("a(b+)c", re.I, "ABBC"),
    ("ab{2,3}", re.I, "ABBB"),
    ("a|B", re.I, "b"),
    ("^ab$", re.M, "x\nab\ny"),
    ("^ab$", re.M, "xab\ny"),
    ("^.", re.M, "ab\ncd"),
    (r"b$", re.M, "ab\ncd"),
    ("^$", re.M, "a\n\nb"),
    ("a.c", re.S, "a\nc"),
    ("a.c", 0, "a\nc"),
    ("a.*c", re.S, "a\nx\nc"),
    ("^a.b$", re.I | re.M | re.S, "X\nA\nB\ny"),
    ("<.*?>", re.S, "<a\nb><c>"),
    (r"b\Z", re.M, "a\nb"),
    (r"b\Z", re.M, "b\nc"),
]


@pytest.mark.parametrize("pattern,flags,text", FLAG_CASES)
@pytest.mark.parametrize("search", [False, True])
def test_engine_flags_match_cpython(engine, pattern, flags, text, search):
    got_status, got_spans = run_engine_flags(engine, pattern, int(flags), text, search)
    want_status, want_spans = cpython_spans_flags(pattern, int(flags), text, search)
    assert got_status == want_status, (pattern, flags, text, search)
    if want_status == MATCH:
        assert got_spans == want_spans, (
            pattern,
            flags,
            text,
            search,
            got_spans,
            want_spans,
        )


def cpython_spans_flags(pattern: str, flags: int, text: str, search: bool):
    m = (re.search if search else re.match)(pattern, text, flags)
    if m is None:
        return NOMATCH, []
    spans = []
    for g in range(m.re.groups + 1):
        s = m.span(g)
        spans.append(None if s == (-1, -1) else s)
    return MATCH, spans


def test_engine_rejects_out_of_mask_flags(engine):
    status, _ = run_engine_flags(engine, "a", 64, "a", False)  # re.X
    assert status == UNSUPPORTED


def test_engine_declines_non_ascii_text(engine):
    status, _ = run_engine(engine, r"\w+", "café", True)
    assert status == NONASCII


def test_frontend_checker_subset_of_engine(engine):
    """The frontend's conservative checker must never approve a pattern the
    engine rejects (checker-approved => engine-supported), or the E1
    compile-time gate would turn into construction-time raises."""
    from pcc.frontends.python.codegen.native_text_modules import (
        NativeTextModulesLoweringMixin,
    )

    checker = NativeTextModulesLoweringMixin._re_engine_subset_supported

    pats = [p for p, _ in SUPPORTED_CASES] + UNSUPPORTED_PATTERNS
    # plus a deterministic fuzz sweep over the generator grammar
    import random

    rng = random.Random(20260612)
    atoms = [
        "a",
        "b",
        "1",
        "_",
        ".",
        r"\d",
        r"\w",
        r"\s",
        "[ab]",
        "[^a]",
        "[a-z]",
        "[z-a]",
    ]
    quants = ["", "*", "+", "?", "*?", "{2}", "{1,3}", "{,2}", "{2,}"]
    for _ in range(400):
        parts = []
        for _ in range(rng.randint(1, 5)):
            body = rng.choice(atoms)
            if rng.random() < 0.2:
                body = "(" + body + rng.choice(["", "|b"]) + ")"
            q = rng.choice(quants)
            if "{" in q and body.startswith("("):
                q = ""
            parts.append(body + q)
        pats.append("".join(parts))

    approved = 0
    for p in pats:
        if checker(p):
            approved += 1
            assert engine.pcc_re_engine_supported(p.encode("utf-8")) == 1, p
    assert approved >= 40, approved


def test_engine_supported_probe(engine):
    assert engine.pcc_re_engine_supported(rb"v(\d+)\.(\d+)") == 1
    assert engine.pcc_re_engine_supported(rb"a{2}") == 1
    assert engine.pcc_re_engine_supported(rb"a{99}") == 0
    assert engine.pcc_re_engine_supported(rb"(ab){2}") == 0


@pytest.mark.parametrize("seed", [20260610, 20260611])
def test_engine_fuzz_differential(engine, seed):
    import random

    rng = random.Random(seed)
    atoms = ["a", "b", "1", "_", ".", r"\d", r"\w", r"\s", "[ab]", "[^a]", "[a-z]"]
    quants = [
        "",
        "",
        "",
        "*",
        "+",
        "?",
        "*?",
        "+?",
        "??",
        "{2}",
        "{1,2}",
        "{0,2}",
        "{2,}",
        "{1,3}?",
    ]

    def gen_piece(depth: int) -> str:
        if depth > 0 and rng.random() < 0.25:
            inner = gen_alt(depth - 1)
            body = ("(?:" + inner + ")") if rng.random() < 0.5 else ("(" + inner + ")")
        else:
            body = rng.choice(atoms)
        q = rng.choice(quants)
        # counted repeats are single-byte-atom-only in the engine subset
        if "{" in q and body.startswith("("):
            q = rng.choice(["", "*", "+", "?", "*?", "+?", "??"])
        return body + q

    def gen_cat(depth: int) -> str:
        return "".join(gen_piece(depth) for _ in range(rng.randint(1, 4)))

    def gen_alt(depth: int) -> str:
        n = rng.randint(1, 3)
        return "|".join(gen_cat(depth) for _ in range(n))

    unsupported = 0
    checked = 0
    for _ in range(300):
        pattern = gen_alt(2)
        text = "".join(rng.choice("ab1_ ") for _ in range(rng.randint(0, 12)))
        for search in (False, True):
            got_status, got_spans = run_engine(engine, pattern, text, search)
            if got_status == UNSUPPORTED:
                unsupported += 1
                continue
            assert got_status in (MATCH, NOMATCH), (pattern, text, got_status)
            want_status, want_spans = cpython_spans(pattern, text, search)
            assert got_status == want_status, (pattern, text, search)
            if want_status == MATCH:
                assert got_spans == want_spans, (
                    pattern,
                    text,
                    search,
                    got_spans,
                    want_spans,
                )
            checked += 1
    assert checked >= 400, (checked, unsupported)
