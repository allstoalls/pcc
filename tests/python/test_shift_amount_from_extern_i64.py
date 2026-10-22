"""A shift amount derived inline from an extern's i64 result must be exact.

Minimised from the five-GC bootstrap matrix failing on all five backends. The
chain was: `cmp pcc2 pcc3` differs by 100 bytes in `__TEXT,__text` -> 13 numeric
formatting functions -> `double 1000.0` encoded as `0x408F400000000004` -> `2**52`
fails its `str()` round trip -> the exact-conversion fix could not be written
because `1 << (1075 - e)` is mis-lowered when `e` comes from `f64_bits`.

Three lines reproduce it:

    b = f64_bits(1e-300)            # correct
    e = (b >> 52) & 0x7FF           # 26, prints correctly
    d = 1 << (1075 - e)             # should be 1<<1049 (316 digits), gives 8

Hoisting the amount into its own local makes it correct, which is what pins the
defect to the inline-shift-amount path rather than to `f64_bits`, to big
integers, or to the annotation:

    amt = 1075 - e
    d = 1 << amt                    # correct, 316 digits

Established by bisection (each verified independently):
  big-int shift/mul/div at 1<<1074      exact
  f64_bits bit pattern and fields       exact
  variable shift amounts `1 << n`       exact
  same code with a literal `bits`       exact
  inline `1 << (1075 - e)`, e literal   exact
  only the combination fails

See docs/investigations/pcc1-float-repr-strtod-17-digit-defect.md
"""

from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def _run(tmp_path, source: str) -> list[str]:
    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.split()


def test_inline_shift_amount_from_f64_bits(tmp_path):
    """The three-line reproducer."""
    assert _run(
        tmp_path,
        '''
        from pcc.unsafe import f64_bits


        def main() -> None:
            v = 1e-300
            b: int = f64_bits(v)
            e: int = (b >> 52) & 0x7FF
            d: int = 1 << (1075 - e)
            print(e)
            print(len(str(d)))


        main()
        ''',
    ) == ["26", str(len(str(1 << 1049)))]


def test_hoisted_shift_amount_is_the_working_form(tmp_path):
    """Same computation with the amount in its own local. This passes today and
    is here so a fix that breaks it is caught."""
    assert _run(
        tmp_path,
        '''
        from pcc.unsafe import f64_bits


        def main() -> None:
            v = 1e-300
            b: int = f64_bits(v)
            e: int = (b >> 52) & 0x7FF
            amt: int = 1075 - e
            d: int = 1 << amt
            print(amt)
            print(len(str(d)))


        main()
        ''',
    ) == ["1049", str(len(str(1 << 1049)))]
