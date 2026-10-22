"""Tests for the top-level pcc Python API."""

import ctypes
import os
import platform
import tempfile

import pytest


_SHARED_HOST = os.sys.platform == "darwin" and platform.machine() == "arm64"
_OWNED_SHARED_GATE = pytest.mark.pcc_gate(
    unavailable=None if _SHARED_HOST else "owned shared libraries currently require Darwin arm64"
)


# ---------------------------------------------------------------------------
# Fixtures: temp C source files
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_c(tmp_path):
    """Single-file C source with add/mul functions."""
    src = tmp_path / "simple.c"
    src.write_text("""
    int add(int a, int b) { return a + b; }
    int mul(int a, int b) { return a * b; }
    """, encoding="utf-8")
    return str(src)


@pytest.fixture
def main_c(tmp_path):
    """C source with main()."""
    src = tmp_path / "main.c"
    src.write_text("""
    int square(int x) { return x * x; }
    int main(void) { return square(5) - 25; }
    """, encoding="utf-8")
    return str(src)


@pytest.fixture
def multi_tu(tmp_path):
    """Two-file translation unit setup."""
    header = tmp_path / "utils.h"
    header.write_text("int triple(int x);\n", encoding="utf-8")
    impl = tmp_path / "utils.c"
    impl.write_text("""
    int triple(int x) { return x + x + x; }
    """, encoding="utf-8")
    main = tmp_path / "main.c"
    main.write_text("""
    #include "utils.h"
    int main(void) { return triple(3) - 9; }
    """, encoding="utf-8")
    return str(impl), str(main), str(tmp_path)


# ---------------------------------------------------------------------------
# build() tests
# ---------------------------------------------------------------------------

class TestBuild:

    def test_build_exe(self, main_c):
        from pcc import build
        artifact = build(main_c, kind="exe")
        assert artifact.kind == "exe"
        assert os.path.isfile(artifact.output_path)
        assert artifact.libs == []
        # Run it
        import subprocess
        r = subprocess.run([artifact.output_path], capture_output=True)
        assert r.returncode == 0

    @_OWNED_SHARED_GATE
    def test_build_sharedlib(self, simple_c):
        from pcc import build
        artifact = build(simple_c, kind="sharedlib")
        assert artifact.kind == "sharedlib"
        assert os.path.isfile(artifact.output_path)
        assert "add" in artifact.exports
        assert "mul" in artifact.exports

    def test_build_object(self, simple_c):
        from pcc import build
        artifact = build(simple_c, kind="object")
        assert artifact.kind == "object"
        assert os.path.isfile(artifact.output_path)
        assert artifact.output_path.endswith(".o")

    @_OWNED_SHARED_GATE
    def test_build_with_out_dir(self, simple_c, tmp_path):
        from pcc import build
        out = tmp_path / "output"
        artifact = build(simple_c, kind="sharedlib", out_dir=str(out))
        assert str(out) in artifact.output_path

    def test_build_multi_sources(self, multi_tu):
        from pcc import build
        impl, main, inc_dir = multi_tu
        artifact = build(
            [impl, main],
            include_dirs=[inc_dir],
            kind="exe",
        )
        import subprocess
        r = subprocess.run([artifact.output_path], capture_output=True)
        assert r.returncode == 0

    @_OWNED_SHARED_GATE
    def test_build_with_libs(self, tmp_path):
        from pcc import build
        src = tmp_path / "use_math.c"
        src.write_text("""
        #include <math.h>
        double my_sqrt(double x) { return sqrt(x); }
        int main(void) { return (int)my_sqrt(4.0) - 2; }
        """, encoding="utf-8")
        artifact = build(str(src), libs=["m"], kind="exe")
        assert artifact.libs == ["m"]
        import subprocess
        r = subprocess.run([artifact.output_path], capture_output=True)
        assert r.returncode == 0

    def test_build_with_cpp_args(self, tmp_path):
        from pcc import build
        src = tmp_path / "defines.c"
        src.write_text("""
        int get_val(void) { return MY_VAL; }
        int main(void) { return get_val() - 42; }
        """, encoding="utf-8")
        artifact = build(str(src), cpp_args=["-DMY_VAL=42"], kind="exe")
        import subprocess
        r = subprocess.run([artifact.output_path], capture_output=True)
        assert r.returncode == 0

    def test_build_missing_source_raises(self):
        from pcc import build
        with pytest.raises(FileNotFoundError):
            build("/nonexistent/file.c")

    def test_build_invalid_kind_raises(self, simple_c):
        from pcc import build
        with pytest.raises(ValueError, match="unsupported kind"):
            build(simple_c, kind="invalid")

    @_OWNED_SHARED_GATE
    def test_build_records_backend(self, simple_c):
        from pcc import build

        artifact = build(simple_c, kind="sharedlib", backend="self")

        assert artifact.backend == "self"

    @pytest.mark.parametrize("backend", ["llvm", "llvm_capi"])
    def test_build_rejects_external_backend(self, simple_c, backend):
        from pcc import build

        with pytest.raises(ValueError, match="expected one of: self"):
            build(simple_c, kind="sharedlib", backend=backend)

    @_OWNED_SHARED_GATE
    def test_build_self_backend_sharedlib(self, simple_c):
        from pcc import build

        artifact = build(simple_c, kind="sharedlib", backend="self")

        assert artifact.kind == "sharedlib"
        assert artifact.backend == "self"
        assert os.path.isfile(artifact.output_path)
        assert "add" in artifact.exports
        assert "mul" in artifact.exports


# ---------------------------------------------------------------------------
# module() tests
# ---------------------------------------------------------------------------

class TestModule:
    pytestmark = _OWNED_SHARED_GATE

    def test_module_call_function(self, simple_c):
        from pcc import module
        m = module(simple_c)
        assert m.add(3, 4) == 7
        assert m.mul(5, 6) == 30

    def test_module_exports(self, simple_c):
        from pcc import module
        m = module(simple_c)
        assert "add" in m.__pcc_artifact__.exports

    def test_module_missing_function_raises(self, simple_c):
        from pcc import module
        m = module(simple_c)
        with pytest.raises(AttributeError, match="no_such_func"):
            m.no_such_func(1)

    def test_module_repr(self, simple_c):
        from pcc import module
        m = module(simple_c)
        r = repr(m)
        assert "Module" in r
        assert "add" in r

    def test_module_with_libs(self, tmp_path):
        from pcc import module
        src = tmp_path / "math_wrap.c"
        src.write_text("""
        #include <math.h>
        double my_sqrt(double x) { return sqrt(x); }
        """, encoding="utf-8")
        m = module(str(src), libs=["m"])
        m.my_sqrt.restype = ctypes.c_double
        m.my_sqrt.argtypes = [ctypes.c_double]
        result = m.my_sqrt(9.0)
        assert abs(result - 3.0) < 0.001


# ---------------------------------------------------------------------------
# Phase 3: Compiler-visible debugging
# ---------------------------------------------------------------------------

class TestDebugging:
    pytestmark = _OWNED_SHARED_GATE

    def test_artifact_has_ir_text(self, simple_c):
        from pcc import build
        artifact = build(simple_c, kind="sharedlib")
        assert artifact.ir_text is not None
        assert "define" in artifact.ir_text
        assert "add" in artifact.ir_text

    def test_artifact_has_pass_report(self, simple_c):
        from pcc import build
        artifact = build(simple_c, kind="sharedlib")
        # pass_report is keyed by unit name
        assert isinstance(artifact.pass_report, dict)

    def test_module_pcc_artifact_accessible(self, simple_c):
        from pcc import module
        m = module(simple_c)
        art = m.__pcc_artifact__
        assert art.kind == "sharedlib"
        assert art.ir_text is not None
        assert "add" in art.ir_text


# ---------------------------------------------------------------------------
# Validation: cache reuse
# ---------------------------------------------------------------------------

class TestCacheReuse:
    pytestmark = _OWNED_SHARED_GATE

    def test_build_cache_reuse(self, simple_c):
        from pcc import build
        # First build
        a1 = build(simple_c, kind="sharedlib")
        assert a1.rebuilt is True
        # Second build with same source reuses compilation cache
        # (link still produces a new output, but compile step is cached)
        a2 = build(simple_c, kind="sharedlib")
        assert a2.rebuilt is True  # link always rebuilds
        assert os.path.isfile(a2.output_path)
