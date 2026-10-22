"""Private attributes use the lexical class's field, whatever the receiver.

Python mangles ``__name`` with the class whose body contains the access.  The
instance-field slot lookup mangled with the receiver's static class instead,
so in a method of ``Base`` an access ``other.__x`` on a ``Child`` read and
wrote ``_Child__x``.  Through the direct indexed path the receiver ``self``
of ``Base.__init__`` was hinted as the subclass, so ``Base`` stored its
private field in the subclass's slot and ``read_mangled()`` printed
``<null>`` (py corpus ``phase3/inherit_private_like``).
"""

import os
import subprocess
import sys


PROGRAM = """\
class Base:
    def __init__(self) -> None:
        self._protected = 1
        self.__mangled = 2

    def read_mangled(self) -> int:
        return self.__mangled

    def peek(self, other: "Child") -> int:
        return other.__mangled

    def poke(self, other: "Child", value: int) -> None:
        other.__mangled = value


class Child(Base):
    def __init__(self) -> None:
        super().__init__()
        self.__mangled = 99

    def child_mangled(self) -> int:
        return self.__mangled


def main() -> None:
    c = Child()
    b = Base()
    print(c._protected, c._Base__mangled, c._Child__mangled)
    print(b.read_mangled(), c.read_mangled(), c.child_mangled())
    print(b.peek(c))
    b.poke(c, 7)
    print(b.peek(c), c.child_mangled(), c.read_mangled())


main()
"""

_DIRECT_EMIT_ENV = {
    "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
    "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
    "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
    "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
    "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
}


def _reference(source):
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=30,
    )
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == "1 2 99\n2 2 99\n2\n7 99 7\n"
    return reference.stdout


def test_private_fields_follow_the_lexical_class(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    source = tmp_path / "private_fields.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = _reference(source)
    binary = tmp_path / "private_fields"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected, backend


def test_private_fields_follow_the_lexical_class_through_direct_emission(
    tmp_path, pcc_runtime_archive,
):
    source = tmp_path / "private_fields_direct.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = _reference(source)
    binary = tmp_path / "private_fields_direct"
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(_DIRECT_EMIT_ENV)
    env.update(
        PCC_RUNTIME_ARCHIVE=str(pcc_runtime_archive),
        PCC_RUNTIME_CC="/usr/bin/false",
        PCC_NO_AUTO_PCC1="1",
    )
    compiled = subprocess.run(
        [sys.executable, "-m", "pcc", "--backend", "self",
         "--python-libpython", "off", "--ir-scaffold", "on",
         str(source), "-o", str(binary)],
        capture_output=True, text=True, timeout=300, env=env,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=30,
        env=dict(os.environ, PCC_GC_BACKEND="0"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
