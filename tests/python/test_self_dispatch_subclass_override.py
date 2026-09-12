"""An inherited method body must not hard-bind `self.m()` to a subclass.

A method body is emitted once and shared by the defining class and every
subclass that inherits it, so the runtime receiver is any class in that
subtree.  `_self_receiver_class_name()` reports the class type inference
pinned to `self`, which for an inherited body can be the *subclass*; the
override-safety check then ran against the subclass, found nothing below it,
and lowered a direct call to the subclass's override.

`pcc/py_stdlib/hashlib.py` is the worked example.  `_SHA256.hexdigest` is
inherited by `_SHA224`, so `self` inferred as `_SHA224`; `_SHA224` has no
subclass, so `self.digest()` became a hard `bl _SHA224.digest`.  A plain
`_SHA256` receiver then ran it:

    hashlib.sha256(b"abc").hexdigest()
    -> ba7816bf...b410ff61        (56 hex chars, _SHA224's 28-byte truncation)
    expected ba7816bf...f20015ad  (64 hex chars)

Wrong, silent, and for every caller.  Direct dispatch is sound only when
every class the body can run under resolves the name to the same function.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

_PROGRAM = textwrap.dedent(
    '''\
    class Base:
        def digest(self):
            return b"\\x01" * 32

        def hexdigest(self):
            return "".join(f"{b:02x}" for b in self.digest())

        def size(self):
            return len(self.digest())


    class Sub(Base):
        def digest(self):
            return b"\\x02" * 28


    class Other:
        def digest(self):
            return b"\\x03" * 20

        def hexdigest(self):
            return "".join(f"{b:02x}" for b in self.digest())


    def make_base():
        return Base()


    def make_sub():
        return Sub()


    def main() -> int:
        b = make_base()
        s = make_sub()
        o = Other()
        # The inherited body runs under both classes; each must see its own
        # digest.  Exercise Sub first so inference has every reason to pin
        # `self` to the subclass.
        print("sub  :", s.size(), len(s.hexdigest()))
        print("base :", b.size(), len(b.hexdigest()))
        print("other:", len(o.hexdigest()))
        return 0


    main()
    '''
)

_EXPECTED = "sub  : 28 56\nbase : 32 64\nother: 40\n"


@pytest.mark.pcc_gate(probe="self_backend")
def test_inherited_body_dispatches_on_the_actual_receiver(tmp_path):
    source = tmp_path / "selfdispatch.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    binary = tmp_path / "selfdispatch"

    build = subprocess.run(
        [
            sys.executable, "-m", "pcc",
            "--ir-scaffold=on", "--backend", "self",
            "--python-libpython", "off",
            str(source), "-o", str(binary),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED, (
        "`base : 28 56` means the Base receiver ran Sub's override\n"
        f"got {run.stdout!r}"
    )


_HASHLIB_PROGRAM = textwrap.dedent(
    '''\
    import hashlib


    def main() -> int:
        print(hashlib.sha256(b"abc").hexdigest())
        print(hashlib.sha256(b"").hexdigest())
        print(hashlib.sha256(b"y" * 4096).hexdigest())
        h = hashlib.sha256(b"abc")
        h.update(b"def")
        print(h.hexdigest())
        return 0


    main()
    '''
)


@pytest.mark.pcc_gate(probe="self_backend")
def test_hashlib_sha256_matches_cpython(tmp_path):
    """The shape that found this, pinned against CPython's own hashlib."""
    source = tmp_path / "sha.py"
    source.write_text(_HASHLIB_PROGRAM, encoding="utf-8")
    binary = tmp_path / "sha"

    build = subprocess.run(
        [
            sys.executable, "-m", "pcc",
            "--ir-scaffold=on", "--backend", "self",
            "--python-libpython", "off",
            str(source), "-o", str(binary),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    # A timeout here is the padding loop spinning on a dropped `update`.
    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr

    expected = "\n".join(
        [
            hashlib.sha256(b"abc").hexdigest(),
            hashlib.sha256(b"").hexdigest(),
            hashlib.sha256(b"y" * 4096).hexdigest(),
            hashlib.sha256(b"abcdef").hexdigest(),
        ]
    ) + "\n"
    assert run.stdout == expected, run.stdout


def test_cpython_reference_behaviour(tmp_path):
    """The same program under CPython, so the contract is not self-defined."""
    source = tmp_path / "selfdispatch.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED
