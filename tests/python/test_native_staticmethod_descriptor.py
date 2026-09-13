"""Dynamic staticmethod lookup must preserve the explicit argument list."""
import os
import subprocess
import sys

from pcc.py_frontend.pipeline import compile_python


def test_dynamic_staticmethod_lookup(tmp_path, pcc_py_runtime_archive):
    source = tmp_path / "static_lookup.py"
    source.write_text('''import gc
class Mixin:
    def render(self, value):
        return self.key(value)
class Base(Mixin):
    @staticmethod
    def key(value, suffix="!"):
        return value + suffix
class Derived(Base):
    @staticmethod
    def key(value, suffix="?"):
        return suffix + value
def passthrough(value):
    return value
def main():
    for cls in [Base, Derived]:
        obj = cls()
        print(getattr(obj, "key")("x"))
        print(getattr(cls, "key")("y", suffix="."))
        callback = getattr(obj, "key")
        gc.collect()
        print(callback("z"), obj.render("m"))
        try:
            callback()
        except TypeError:
            print("missing argument")
        descriptor = cls.__dict__["key"]
        print(callable(descriptor), descriptor("d"))
        print(descriptor.__func__ is descriptor.__wrapped__)
        print(getattr(cls, "key") is descriptor.__func__)
        setattr(obj, "key", passthrough)
        print(getattr(obj, "key")("instance"))
    wrapper = staticmethod(passthrough)
    print(Base.key is getattr(Base, "key"))
    print(wrapper is passthrough, callable(wrapper), wrapper(42))
    print(wrapper.__func__ is passthrough, wrapper.__wrapped__ is passthrough)
    invalid = staticmethod(42)
    print(callable(invalid))
    try:
        invalid()
    except TypeError:
        print("not callable")
main()
''', encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True,
                              text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "static_lookup"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_py_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}; {result.stdout}"
        assert result.stdout == expected.stdout, f"GC{gc}: {result.stdout}"
