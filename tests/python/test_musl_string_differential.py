"""Differential test: pcc-compiled musl string functions vs host libc.

LIBC-P2-MEM-STR / LIBC-P3-HARD-SINGLETONS vendor musl 1.2.5 string, stdlib
and ctype functions (tests/fixtures/libc/musl-1.2.5/, sha256-pinned in
VENDOR.json)
and compiles them WITH PCC so the static link owns the symbols instead of
importing libSystem's.

The comparison runs as two probe BINARIES, not through ctypes: a shared
library exporting memset/memcpy/bzero would interpose the test process's
own libc. Build A is the probe compiled by cc against host libc (the
oracle); build B is the same probe compiled by pcc together with the
vendored musl sources, so every call resolves to the pcc-compiled musl
implementation. Both print a digest of every case and the digests must
match exactly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROBE = r"""
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <stdlib.h>
#include <ctype.h>

static void dump(const char *tag, const unsigned char *p, size_t n) {
    printf("%s:", tag);
    for (size_t i = 0; i < n; i++) printf("%02x", p[i]);
    printf("\n");
}

static void pattern(unsigned char *p, size_t n, int seed) {
    for (size_t i = 0; i < n; i++) p[i] = (unsigned char)((i * 131 + seed * 29) & 0xFF);
}

int main(void) {
    static const size_t lens[] = {0, 1, 2, 3, 7, 8, 9, 15, 16, 17, 31, 32, 63, 64, 65, 255, 512};
    static const size_t offs[] = {0, 1, 3, 7, 8};
    unsigned char src[1024], dst[1024];

    for (size_t li = 0; li < sizeof(lens) / sizeof(lens[0]); li++) {
        for (size_t oi = 0; oi < sizeof(offs) / sizeof(offs[0]); oi++) {
            size_t n = lens[li], off = offs[oi];
            pattern(src, n + off, 3);
            memset(dst, 0xEE, sizeof(dst));
            memcpy(dst + off, src, n);
            printf("memcpy n=%zu off=%zu ", n, off);
            dump("d", dst + off, n < 24 ? n : 24);

            memset(dst, 0xAB, n);
            printf("memset n=%zu ", n);
            dump("d", dst, n < 24 ? n : 24);

            bzero(dst, n);
            printf("bzero n=%zu ", n);
            dump("d", dst, n < 24 ? n : 24);
        }
    }

    for (size_t li = 0; li < sizeof(lens) / sizeof(lens[0]); li++) {
        for (size_t si = 1; si < 4; si++) {
            size_t n = lens[li], shift = si * 3;
            unsigned char buf[1024];
            pattern(buf, n + shift, 5);
            memmove(buf + shift, buf, n);
            printf("memmove_fwd n=%zu s=%zu ", n, shift);
            dump("d", buf + shift, n < 24 ? n : 24);
            pattern(buf, n + shift, 5);
            memmove(buf, buf + shift, n);
            printf("memmove_bwd n=%zu s=%zu ", n, shift);
            dump("d", buf, n < 24 ? n : 24);
        }
    }

    static const char *strs[] = {
        "", "a", "hello, world", "abcabcabc",
        "the quick brown fox jumps over the lazy dog",
        "\x7f\x7e\x01\x02trailing",
    };
    static const int chars[] = {0, 1, 'a', 'o', 0x7F, 0xFF};
    for (size_t i = 0; i < sizeof(strs) / sizeof(strs[0]); i++) {
        const char *s = strs[i];
        printf("strlen[%zu]=%zu\n", i, strlen(s));
        for (size_t ci = 0; ci < sizeof(chars) / sizeof(chars[0]); ci++) {
            int c = chars[ci];
            const char *f = strchr(s, c);
            const char *r = strrchr(s, c);
            printf("strchr[%zu,%d]=%ld strrchr=%ld\n", i, c,
                   f ? (long)(f - s) : -1L, r ? (long)(r - s) : -1L);
            const void *m = memchr(s, c, strlen(s));
            printf("memchr[%zu,%d]=%ld\n", i, c,
                   m ? (long)((const char *)m - s) : -1L);
        }
        for (size_t j = 0; j < sizeof(strs) / sizeof(strs[0]); j++) {
            int sc = strcmp(s, strs[j]);
            printf("strcmp[%zu,%zu]=%d\n", i, j, sc > 0 ? 1 : (sc < 0 ? -1 : 0));
            for (size_t n = 0; n <= 8; n += 4) {
                int nc = strncmp(s, strs[j], n);
                printf("strncmp[%zu,%zu,%zu]=%d\n", i, j, n,
                       nc > 0 ? 1 : (nc < 0 ? -1 : 0));
            }
        }
    }

    static const char *nums[] = {
        "0", "1", "-1", "+42", "  \t 123", "2147483647", "-2147483648",
        "007", "12abc", "abc", "", "-", "+", "  -0", "99999999999999",
    };
    for (size_t i = 0; i < sizeof(nums) / sizeof(nums[0]); i++) {
        printf("atoi[%zu]=%d\n", i, atoi(nums[i]));
    }
    for (int c = -2; c < 260; c++) {
        printf("ctype[%d]=%d,%d\n", c, isspace(c) ? 1 : 0, isdigit(c) ? 1 : 0);
    }

    return 0;
}
"""


PROBE_STRTOD = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(void) {
    /* strtod: correctly-rounded decimal->double. The corpus covers exact
     * halfway cases, subnormals, hex floats, extreme exponents, infinities,
     * NaN, and partial parses (endptr position matters as much as the
     * value). Bits are compared, not the printed decimal. */
    static const char *fps[] = {
        "0", "-0", "1", "-1", "0.5", "1e10", "1e-10", "1e308", "1e309",
        "1e-308", "1e-323", "4.9406564584124654e-324", "2.2250738585072011e-308",
        "9007199254740993", "9007199254740992", "0.1", "0.2", "0.3",
        "1.7976931348623157e308", "1.7976931348623159e308",
        "2.5", "3.5", "0.49999999999999994", "0.5000000000000001",
        "0x1p0", "0x1.8p1", "0x1p-1074", "0x1.fffffffffffffp1023",
        "inf", "-inf", "infinity", "nan", "  12.5xyz", "abc", "", ".", "-.",
        ".5", "5.", "1e", "1e+", "1e-", "12345678901234567890",
        "1.000000000000000000000000000000000000001",
    };
    for (size_t i = 0; i < sizeof(fps) / sizeof(fps[0]); i++) {
        char *end = NULL;
        double v = strtod(fps[i], &end);
        unsigned long long bits;
        memcpy(&bits, &v, sizeof(bits));
        printf("strtod[%zu]=%016llx end=%ld\n", i, bits,
               (long)(end - fps[i]));
    }
    return 0;
}
"""


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


REPO = _repo_root()
VENDOR = REPO / "tests" / "fixtures" / "libc" / "musl-1.2.5" / "string"


def _run(binary: Path) -> str:
    out = subprocess.run([str(binary)], text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip(), "probe printed nothing"
    return out.stdout


def test_pcc_compiled_musl_matches_host_libc(tmp_path):
    src = tmp_path / "probe.c"
    src.write_text(PROBE, encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)

    cc_bin = tmp_path / "probe_cc"
    cc = subprocess.run(
        ["cc", "-O1", "-o", str(cc_bin), str(src)],
        text=True, capture_output=True, timeout=120, env=env,
    )
    assert cc.returncode == 0, cc.stderr

    # PCC_NO_BUILTIN keeps the probe's own calls as real calls so they land in
    # the vendored implementations instead of being folded into intrinsics.
    os.environ["PCC_NO_BUILTIN"] = "1"
    try:
        from pcc.api import build

        artifact = build(
            [str(src)] + sorted(str(p) for p in VENDOR.parent.glob("*/*.c")),
            kind="exe",
            out_dir=str(tmp_path),
            optimize=0,
            # The vendored sources are compiled at two different levels by the
            # Makefile; a cached object from the other level would be wrong.
            use_compile_cache=False,
        )
    finally:
        os.environ.pop("PCC_NO_BUILTIN", None)
    pcc_bin = Path(str(artifact.output_path))

    oracle = _run(cc_bin)
    ours = _run(pcc_bin)
    if ours != oracle:
        o_lines, m_lines = oracle.splitlines(), ours.splitlines()
        diffs = [
            f"{i}: host={o!r} pcc={m!r}"
            for i, (o, m) in enumerate(zip(o_lines, m_lines))
            if o != m
        ][:10]
        raise AssertionError(
            f"pcc-compiled musl diverges from host libc ({len(diffs)} shown):\n"
            + "\n".join(diffs)
        )


def test_pcc_compiled_musl_strtod_matches_host_libc(tmp_path):
    """strtod's own group: the scan helpers must be built at the default
    optimization level (they lose a double return value at -O0, tracked as
    BUG-P1-CC-O0-DOUBLE-RETURN-LOST), while the mem primitives in the other
    case require -O0. Two probes keep each group at its correct level."""
    src = tmp_path / "probe_fp.c"
    src.write_text(PROBE_STRTOD, encoding="utf-8")
    env = os.environ.copy()
    env.pop("LC_ALL", None)

    tree = VENDOR.parent
    cc_bin = tmp_path / "probe_fp_cc"
    cc = subprocess.run(
        ["cc", "-O1", "-o", str(cc_bin), str(src)],
        text=True, capture_output=True, timeout=120, env=env,
    )
    assert cc.returncode == 0, cc.stderr

    scan_sources = [
        str(tree / "stdlib" / "strtod.c"),
        str(tree / "internal" / "floatscan.c"),
        str(tree / "internal" / "shgetc.c"),
        str(tree / "internal" / "pcc_scan_uflow.c"),
        str(tree / "math" / "scalbn.c"),
        str(tree / "math" / "fma.c"),
        str(tree / "math" / "fmod.c"),
        str(tree / "math" / "copysign.c"),
        str(tree / "math" / "fabs.c"),
    ]
    # The CLI is what the runtime Makefile uses ($(PCC)), and it compiles and
    # runs a C input directly, so the gate exercises the path that actually
    # builds the shipped archive. (The in-process pcc.api.build path once
    # produced wrong doubles for this corpus; it no longer does, and the two
    # paths are pinned to the same object by
    # tests/c/test_api_cli_object_parity.py.)
    env["PCC_NO_BUILTIN"] = "1"
    run = subprocess.run(
        ["uv", "run", "pcc", "--cpp-arg=-I" + str(tree / "internal"), str(src)]
        + scan_sources,
        text=True, capture_output=True, timeout=560, env=env,
        cwd=str(REPO),
    )
    assert run.returncode == 0, run.stderr[-2000:]

    oracle = _run(cc_bin)
    ours = run.stdout

    if ours != oracle:
        diffs = [
            f"{i}: host={o!r} pcc={m!r}"
            for i, (o, m) in enumerate(zip(oracle.splitlines(), ours.splitlines()))
            if o != m
        ][:10]
        raise AssertionError("strtod diverges from host libc:\n" + "\n".join(diffs))


def test_vendor_manifest_pins_every_source():
    import json

    manifest = json.loads((VENDOR / "VENDOR.json").read_text(encoding="utf-8"))
    tree = VENDOR.parent
    on_disk = {
        f"{p.parent.name}/{p.name}" for p in tree.glob("*/*.c")
    }
    assert set(manifest["files"]) == on_disk
    assert all(len(v) == 64 for v in manifest["files"].values())
    assert "musl 1.2.5" in manifest["upstream"]
