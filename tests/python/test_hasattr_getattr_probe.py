"""hasattr / 3-arg getattr semantics across the attribute-probe runtime path.

Pins the observable contract while the runtime miss path changes from
"construct an AttributeError, then clear it" to a no-raise probe
(``py_obj_getattr_maybe``): user ``__getattr__`` still runs, its
AttributeError still means False/default, and the plain 2-arg ``getattr``
miss still raises.  See
``docs/investigations/pcc1-worker-object-protocol-tax.md`` candidate 1.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


_PROGRAM = """\
class Box:
    def __init__(self):
        self.present = 41


class Dyn:
    def __init__(self):
        self.base = 1

    def __getattr__(self, name):
        if name == "virtual":
            return 99
        raise AttributeError(name)


def main():
    b = Box()
    print(hasattr(b, "present"))
    print(hasattr(b, "missing"))
    print(getattr(b, "present", 7))
    print(getattr(b, "missing", 7))
    d = Dyn()
    print(hasattr(d, "virtual"))
    print(hasattr(d, "nope"))
    print(getattr(d, "virtual", 5))
    print(getattr(d, "nope", 5))
    print(hasattr(Box, "missing"))
    print(getattr(Box, "missing", 3))


main()
"""

_EXPECTED = [
    "True",
    "False",
    "41",
    "7",
    "True",
    "False",
    "99",
    "5",
    "False",
    "3",
]

# Pre-existing, distinct bug (NOT the probe path): the AttributeError raised
# by a bare 2-arg ``getattr(obj, "missing")`` escapes an enclosing
# ``except AttributeError`` handler and kills the process.  CPython prints
# "raised".  Same family as the known self.attr/try-except-AttributeError
# codegen gap.  Strict xfail so a fix flips this loudly.
_RAISE_PROGRAM = """\
class Box:
    def __init__(self):
        self.present = 41


def main():
    b = Box()
    try:
        getattr(b, "missing")
    except AttributeError:
        print("raised")


main()
"""


def _build(tmp_path: Path, program: str = _PROGRAM) -> Path:
    src = tmp_path / "prog.py"
    src.write_text(program, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(src), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert build.returncode == 0, build.stderr
    return exe


def test_attribute_probe_semantics_match_cpython_on_all_gc_backends(tmp_path):
    exe = _build(tmp_path)
    for backend in ("0", "1", "2", "3", "4"):
        env = os.environ.copy()
        env.pop("LC_ALL", None)
        env["PCC_GC_BACKEND"] = backend
        run = subprocess.run(
            [str(exe)], text=True, capture_output=True, timeout=30, env=env
        )
        assert run.returncode == 0, (backend, run.stderr)
        assert run.stdout.splitlines() == _EXPECTED, (backend, run.stdout)


_LEAK_PROGRAM = """\
def main():
    xs = [1, 2, 3]
    hits = 0
    i = 0
    while i < 4000000:
        if hasattr(xs, "pop"):
            hits += 1
        i += 1
    print(hits)


main()
"""


def test_hasattr_probe_releases_fabricated_attributes(tmp_path):
    """hasattr(lst, "pop") fabricates a bound method per probe; the present
    branch must release it or a 4M-iteration loop leaks >200MB."""
    exe = _build(tmp_path, _LEAK_PROGRAM)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    run = subprocess.run(
        ["/usr/bin/time", "-l", str(exe)],
        text=True, capture_output=True, timeout=120, env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "4000000", run.stdout
    max_rss = 0
    for line in run.stderr.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "maximum" and parts[0].isdigit():
            max_rss = int(parts[0])
    assert max_rss > 0, run.stderr
    assert max_rss < 160 * 1024 * 1024, (
        f"hasattr probe leaked: max RSS {max_rss / 1e6:.0f}MB"
    )


def test_two_arg_getattr_miss_is_catchable(tmp_path):
    exe = _build(tmp_path, _RAISE_PROGRAM)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["raised"], run.stdout


# ---------------------------------------------------------------------------
# BUG-P1-GETATTR-BUILTIN-RESULT-OWNERSHIP: the builtin ``getattr(...)`` result
# on the generic (non-native-module, non-cpython) path is a NEW owned reference
# on every edge (type-class incref, instance/class attr incref, fabricated
# bound method).  The emitter records it owned via ``_note_owned_object_value``
# so a discarded/rebound result is released exactly once, without flipping the
# AST classifier (the native-module/cpython getattr branches keep their own
# unaudited ownership and must not be over-released).
# ---------------------------------------------------------------------------

_GETATTR_OWNERSHIP_PROGRAM = "class Attr:\n    def __init__(self, tag):\n        self.tag = tag\n    def __del__(self):\n        print('freed', self.tag)\n\n\nclass Holder:\n    def __init__(self):\n        self.a = Attr('a')\n        self.b = Attr('b')\n\n\ndef probe(h) -> None:\n    # 2-arg hit, bare-statement discard: owned result freed at once, but the\n    # SHARED attribute h.a must NOT be over-released (still alive after).\n    getattr(h, 'a')\n    print('after 2arg discard; h.a:', h.a.tag)\n\n    # 2-arg hit, local bound then dropped: owned result released on rebind.\n    x = getattr(h, 'b')\n    print('x:', x.tag)\n    x = Attr('x2')          # rebind: previous getattr result released\n    print('after x rebind; h.b:', h.b.tag)\n    x = None\n    print('after x=None')\n\n    # 3-arg hit with an owned-temp default: attr result owned; the unused\n    # default is released on the present edge (freed during the call).\n    r1 = getattr(h, 'a', Attr('d1'))\n    print('r1:', r1.tag)\n    r1 = None\n    print('after r1=None')\n\n    # 3-arg miss with an owned-temp default: default becomes the owned result.\n    r2 = getattr(h, 'missing', Attr('d2'))\n    print('r2:', r2.tag)\n    r2 = None\n    print('after r2=None')\n\n    # 3-arg miss with a borrowed local default: result must not free it.\n    keep = Attr('keep')\n    r3 = getattr(h, 'missing', keep)\n    print('r3:', r3.tag)\n    r3 = None\n    print('keep:', keep.tag)\n\n\ndef main() -> None:\n    h = Holder()\n    probe(h)\n    print('after probe; h.a/h.b:', h.a.tag, h.b.tag)\n\n\nmain()\nprint('end')\n"

_GETATTR_OWNERSHIP_EXPECTED = ['after 2arg discard; h.a: a', 'x: b', 'after x rebind; h.b: b', 'freed x2', 'after x=None', 'freed d1', 'r1: a', 'after r1=None', 'r2: d2', 'freed d2', 'after r2=None', 'r3: keep', 'keep: keep', 'freed keep', 'after probe; h.a/h.b: a b', 'freed a', 'freed b', 'end']


def test_getattr_result_ownership_matches_cpython_on_gc0_3_4(tmp_path):
    """Owned getattr results are released once, shared attrs are not over-released.

    Covers 2-arg hit (bare discard + rebind), 3-arg hit (owned-temp default
    released on the present edge), 3-arg miss (owned-temp default becomes the
    owned result), and 3-arg miss with a borrowed local default (result must
    not free it).  Verified against CPython on refcount/generational/relocating.
    """
    exe = _build(tmp_path, _GETATTR_OWNERSHIP_PROGRAM)
    for backend in ("0", "3", "4"):
        env = os.environ.copy()
        env.pop("LC_ALL", None)
        env["PCC_GC_BACKEND"] = backend
        run = subprocess.run(
            [str(exe)], text=True, capture_output=True, timeout=30, env=env
        )
        assert run.returncode == 0, (backend, run.stderr)
        assert run.stdout.splitlines() == _GETATTR_OWNERSHIP_EXPECTED, (
            backend,
            run.stdout,
        )


_GETATTR_POP_FABRICATION_PROGRAM = "# getattr(lst, 'pop') fabricates a fresh bound-method object per call (the\n# py_builtin_pop_bound path).  Before the ownership fix each fabricated object\n# leaked; the result is now released so a 4M-iteration loop stays bounded.\ndef main() -> None:\n    lst = [1, 2, 3]\n    total = 0\n    i = 0\n    while i < 4000000:\n        m = getattr(lst, 'pop')\n        total += 1\n        i += 1\n    print('done', total)\n\n\nmain()\n"


def test_getattr_pop_fabrication_result_is_released(tmp_path):
    """``getattr(lst, 'pop')`` fabricates a bound method per call; the rebound
    local result must be released or a 4M-iteration loop leaks >200MB."""
    exe = _build(tmp_path, _GETATTR_POP_FABRICATION_PROGRAM)
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    run = subprocess.run(
        ["/usr/bin/time", "-l", str(exe)],
        text=True, capture_output=True, timeout=120, env=env,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "done 4000000", run.stdout
    max_rss = 0
    for line in run.stderr.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "maximum" and parts[0].isdigit():
            max_rss = int(parts[0])
    assert max_rss > 0, run.stderr
    assert max_rss < 160 * 1024 * 1024, (
        f"getattr pop fabrication leaked: max RSS {max_rss / 1e6:.0f}MB"
    )
