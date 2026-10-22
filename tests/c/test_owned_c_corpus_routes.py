"""Corpus product routes own preprocessing/emission/linking; oracles stay separate."""
from importlib import import_module
from pathlib import Path
import subprocess

import pytest

from pcc.backend import BackendUnavailable
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from tests import owned_c_corpus


@pytest.fixture(autouse=True)
def no_external_build_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external process in host route model")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", "/model/libpy_runtime_pcc_py.a")


class EvaluatorModel:
    backend = "self"
    is_cross = False
    _normalize_opt_level = staticmethod(CEvaluator._normalize_opt_level)

    def __init__(self, failure=None):
        self.events = []
        self.failure = failure
        self.compiled = object()

    def compile_translation_units(self, units, **options):
        self.events.append(("compile", units, options))
        if self.failure == "compile":
            raise RuntimeError("original compile failure")
        return self.compiled

    def emit_executable(self, compiled, output, **options):
        self.events.append(("emit", compiled, output, options))
        if self.failure == "emit":
            raise RuntimeError("original emit failure")
        assert compiled is self.compiled
        Path(output).write_bytes(b"model image; never executed")

    def run_translation_units_with_system_cc(self, *args, **kwargs):
        raise AssertionError("corpus product selected the host assembler/linker")


@pytest.mark.parametrize("returncode", [0, 7, -6])
def test_owned_runner_preserves_source_options_and_child_outcome(tmp_path, monkeypatch, returncode):
    evaluator = EvaluatorModel()
    units = [TranslationUnit("case.c", str(tmp_path / "case.c"), "int main(void) { return 7; }\n")]
    calls = []
    def execute(argv, **options):
        assert Path(argv[0]).read_bytes() == b"model image; never executed"
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, returncode, "original stdout\n", "original stderr\n")
    monkeypatch.setattr(subprocess, "run", execute)
    result = owned_c_corpus.run_owned_c_corpus(
        evaluator, units, base_dir=str(tmp_path), include_dirs=["include"],
        cpp_args=["-std=gnu89", "-DFEATURE=1"], timeout=17, jobs=3,
    )
    assert evaluator.events[0] == ("compile", units, {
        "base_dir": str(tmp_path), "use_system_cpp": False,
        "include_dirs": ["include"], "cpp_args": ["-std=gnu89", "-DFEATURE=1"],
        "frontend_opt_level": 2, "jobs": 3,
    })
    assert evaluator.events[1][0] == "emit"
    assert evaluator.events[1][3] == {"optimize": 2, "link_args": None}
    assert calls[0][0] == [evaluator.events[1][2]]
    assert calls[0][1] == {"cwd": str(tmp_path), "timeout": 17, "capture_output": True, "text": True}
    assert (result.returncode, result.stdout, result.stderr) == (returncode, "original stdout\n", "original stderr\n")
    assert units[0].source == "int main(void) { return 7; }\n"
    assert not Path(evaluator.events[1][2]).exists()


@pytest.mark.parametrize("failure", ["compile", "emit"])
def test_owned_runner_preserves_build_errors_without_system_fallback(monkeypatch, failure):
    evaluator = EvaluatorModel(failure)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("executed after failed build"))
    with pytest.raises(RuntimeError, match="original " + failure + " failure"):
        owned_c_corpus.run_owned_c_corpus(evaluator, [])
    assert [event[0] for event in evaluator.events] == (["compile"] if failure == "compile" else ["compile", "emit"])


@pytest.mark.parametrize("bad_backend,cross,diagnostic", [("llvm", False, "owned self backend"), ("self", True, "host target")])
def test_owned_runner_rejects_wrong_execution_owner_or_target(bad_backend, cross, diagnostic):
    evaluator = EvaluatorModel()
    evaluator.backend, evaluator.is_cross = bad_backend, cross
    with pytest.raises(RuntimeError, match=diagnostic):
        owned_c_corpus.run_owned_c_corpus(evaluator, [])
    assert evaluator.events == []


def test_owned_runner_honors_disabled_implicit_runtime_provisioning(monkeypatch):
    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    evaluator = EvaluatorModel()
    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        owned_c_corpus.run_owned_c_corpus(evaluator, [])
    assert evaluator.events == []


def test_owned_runner_does_not_discard_unsupported_product_link_options():
    evaluator = EvaluatorModel()
    evaluator.emit_executable = lambda *a, **kw: CEvaluator.emit_executable(evaluator, *a, **kw)
    with pytest.raises(BackendUnavailable, match="unsupported owned C executable link argument: -lcustom"):
        owned_c_corpus.run_owned_c_corpus(evaluator, [], link_args=["-lcustom"])
    assert [event[0] for event in evaluator.events] == ["compile"]


PRODUCTS = (
    ("tests.gcc_torture_cases", "worker"),
    ("tests.clang_c_cases", "worker"),
    ("tests.c_testsuite_cases", "worker"),
    ("tests.c.test_csmith", "csmith"),
    ("tests.c.test_gcc_torture_self", "direct"),
    ("tests.c.test_c_testsuite_self", "direct"),
)


class ConnectionModel:
    def __init__(self):
        self.payload = None
        self.closed = False
    def send(self, value):
        self.payload = value
    def close(self):
        self.closed = True


@pytest.mark.parametrize("module_name,kind", PRODUCTS)
def test_each_product_entry_preserves_original_units_and_uses_owned_runner(tmp_path, monkeypatch, module_name, kind):
    module = import_module(module_name)
    source = "/* original fixture source */\nint main(void) { return 0; }\n"
    path = tmp_path / "case.c"
    path.write_text(source)
    calls = []
    evaluator = EvaluatorModel()
    monkeypatch.setattr(module, "CEvaluator", lambda *args, **kwargs: evaluator)
    def owned(instance, units, **options):
        assert instance is evaluator
        assert len(units) == 1 and units[0].source == source and units[0].path == str(path)
        assert options["timeout"] == 19
        assert "link_args" not in options  # host -lm never enters product linking
        calls.append((units, options))
        return subprocess.CompletedProcess(["owned product"], 7, "out\n", "err\n")
    monkeypatch.setattr(module, "run_owned_c_corpus", owned)
    if kind == "direct":
        module._run_backend.cache_clear()
        result = module._run_backend(path, backend="self", timeout=19)
        observed = (result.returncode, result.stdout, result.stderr)
    else:
        connection = ConnectionModel()
        if kind == "csmith":
            module._pcc_worker_entry(str(path), 19, connection)
        else:
            module._pcc_worker_entry("run", str(path), 19, connection)
        if kind != "csmith":
            assert connection.closed
        # Csmith's existing worker adapter lets its process owner close the pipe.
        observed = tuple(connection.payload[key] for key in ("returncode", "stdout", "stderr"))
    assert len(calls) == 1
    assert observed == (7, "out\n", "err\n")
    assert path.read_text() == source


def test_gcc_reference_keeps_its_external_compiler_and_math_linkage(tmp_path, monkeypatch):
    from tests import gcc_torture_cases as module
    path = tmp_path / "oracle.c"
    path.write_text("int main(void) { return 0; }\n")
    calls = []
    monkeypatch.setattr(module, "_host_cc", lambda: "/oracle/cc")
    monkeypatch.setattr(module.sys, "platform", "linux")
    def reference(argv, **options):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", reference)
    module.compile_native.cache_clear()
    assert module.compile_native(path, tmp_path).returncode == 0
    assert calls[0][0] == "/oracle/cc"
    assert "-lm" in calls[0] and str(path) in calls[0]


def test_csmith_seed_passes_identical_generated_source_to_product_and_reference(monkeypatch):
    from tests.c import test_csmith as module

    original = '#include "csmith.h"\nint main(void) { return CSMITH_VALUE; }\n'
    observed = []

    def generate(seed, path):
        assert seed == 23
        Path(path).write_text(original)

    def reference(path):
        observed.append(("reference", path, Path(path).read_text()))
        return subprocess.CompletedProcess(["external reference oracle"], 0, "checksum\n", "")

    def product(path):
        observed.append(("product", path, Path(path).read_text()))
        return 0, "checksum\n", ""

    monkeypatch.setattr(module, "_generate", generate)
    monkeypatch.setattr(module, "_run_native", reference)
    monkeypatch.setattr(module, "_run_pcc", product)
    result = module._run_seed(23)
    assert observed[0][0] == "reference" and observed[1][0] == "product"
    assert observed[0][1:] == observed[1][1:]
    assert observed[1][2] == original
    assert (result.seed, result.native_returncode, result.pcc_returncode) == (23, 0, 0)
    assert result.native_stdout == result.pcc_stdout == "checksum"


def test_csmith_product_uses_owned_preprocessor_with_original_header_path(tmp_path, monkeypatch):
    from tests.c import test_csmith as module

    headers = tmp_path / "generator-headers"
    headers.mkdir()
    (headers / "csmith.h").write_text("#define CSMITH_VALUE 42\n")
    original = '#include "csmith.h"\nint main(void) { return CSMITH_VALUE; }\n'
    source = tmp_path / "original.c"
    source.write_text(original)
    monkeypatch.setattr(module, "CSMITH_INCLUDE", str(headers))

    def external_forbidden(*args, **kwargs):
        pytest.fail("Csmith product requested an external compiler/preprocessor")

    monkeypatch.setattr(module, "_host_cc", external_forbidden)
    monkeypatch.setattr(CEvaluator, "_system_cpp", external_forbidden)
    monkeypatch.setattr(CEvaluator, "_system_cc", external_forbidden)
    emissions = []

    def emit(self, units, output, **options):
        emissions.append(units)
        assert any("42" in unit[1] for unit in units)
        assert all("CSMITH_VALUE" not in unit[1] for unit in units)
        Path(output).write_bytes(b"owned emission model; never executed")

    def execute(argv, **options):
        assert len(argv) == 1
        assert Path(argv[0]).read_bytes() == b"owned emission model; never executed"
        return subprocess.CompletedProcess(argv, 42, "", "")

    monkeypatch.setattr(CEvaluator, "emit_executable", emit)
    monkeypatch.setattr(subprocess, "run", execute)
    connection = ConnectionModel()
    module._pcc_worker_entry(str(source), 19, connection)
    assert connection.payload == {"returncode": 42, "stdout": "", "stderr": ""}
    assert len(emissions) == 1
    assert source.read_text() == original


@pytest.mark.parametrize("test_name", [
    "test_ssa_branch_prune_preserves_short_circuit_global_side_effect_runtime",
    "test_ssa_branch_prune_preserves_short_circuit_value_side_effect_runtime",
])
def test_short_circuit_product_preserves_compile_options_and_uses_owned_runner(monkeypatch, test_name):
    from tests.c import test_ssa_branch_prune as module

    evaluator = EvaluatorModel()
    monkeypatch.setattr(module, "CEvaluator", lambda: evaluator)
    executions = []

    def execute(argv, **options):
        assert len(argv) == 1
        assert Path(argv[0]).read_bytes() == b"model image; never executed"
        executions.append(options)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", execute)
    getattr(module, test_name)()
    assert [event[0] for event in evaluator.events] == ["compile", "emit"]
    assert evaluator.events[0][2] == {
        "base_dir": ".", "use_system_cpp": False, "include_dirs": None,
        "cpp_args": None, "frontend_opt_level": 2, "jobs": 1,
    }
    assert evaluator.events[1][3] == {"optimize": 2, "link_args": None}
    assert executions == [{"cwd": ".", "timeout": 120, "capture_output": True, "text": True}]


@pytest.mark.parametrize("module_name", ["tests.gcc_torture_cases", "tests.c.test_gcc_torture_self"])
def test_gcc_product_rejects_unmodeled_link_dependency_without_fallback(tmp_path, monkeypatch, module_name):
    from tests.gcc_torture_cases import GccTortureComparisonOptions
    module = import_module(module_name)
    path = tmp_path / "dependency.c"
    path.write_text("int main(void) { return 0; }\n")
    monkeypatch.setattr(module, "gcc_torture_case_options", lambda unused: GccTortureComparisonOptions("-std=gnu17", ("-lcustom",), ()))
    monkeypatch.setattr(module, "run_owned_c_corpus", lambda *a, **kw: pytest.fail("discarded unmodeled dependency"))
    if module_name.endswith("_self"):
        module._run_backend.cache_clear()
        result = module._run_backend(path, backend="self")
        status, error = result.returncode, result.stderr
    else:
        connection = ConnectionModel()
        module._pcc_worker_entry("run", str(path), 19, connection)
        assert connection.closed
        status, error = connection.payload["returncode"], connection.payload["stderr"]
    assert status == 1
    assert "unsupported owned corpus link dependency" in error and "-lcustom" in error


@pytest.mark.parametrize("backend", [None, "self"], ids=["default-self", "explicit-self"])
def test_pointer_initializer_product_uses_owned_frontend_and_emitter(tmp_path, monkeypatch, backend):
    from tests.c import test_pointer_constant_initializers as module

    constructors = []
    compilations = []
    emissions = []
    executions = []
    monkeypatch.setenv("PCC_DISABLE_COMPILE_CACHE", "1")

    def external_forbidden(*args, **kwargs):
        pytest.fail("pointer initializer product requested an external compiler/preprocessor")

    monkeypatch.setattr(CEvaluator, "_system_cpp", external_forbidden)
    monkeypatch.setattr(CEvaluator, "_system_cc", external_forbidden)
    monkeypatch.setattr(CEvaluator, "run_translation_units_with_system_cc", external_forbidden)

    class ObservedEvaluator(CEvaluator):
        def __init__(self, **options):
            constructors.append(options)
            super().__init__(**options)
            assert self.backend == "self"

        def compile_translation_units(self, units, **options):
            compiled = super().compile_translation_units(units, **options)
            compilations.append((units, options, compiled))
            return compiled

        def emit_executable(self, compiled, output, **options):
            assert compiled is compilations[0][2]
            emissions.append((output, options))
            Path(output).write_bytes(b"owned pointer initializer emission model; never executed")

    def execute(argv, **options):
        assert len(argv) == 1 and argv[0] == emissions[0][0]
        assert Path(argv[0]).read_bytes() == b"owned pointer initializer emission model; never executed"
        executions.append(options)
        return subprocess.CompletedProcess(argv, 0, "5 1 1 21\n", "")

    monkeypatch.setattr(module, "CEvaluator", ObservedEvaluator)
    monkeypatch.setattr(subprocess, "run", execute)
    module.test_integer_constant_pointer_initializers(tmp_path, backend)

    assert constructors == ([{}] if backend is None else [{"backend": "self", "allow_unimplemented_backend": True}])
    assert len(compilations) == 1
    units, options, compiled = compilations[0]
    assert len(units) == 1
    assert (units[0].name, units[0].path, units[0].source) == ("main.c", str(tmp_path / "main.c"), module.SOURCE)
    assert options == {
        "base_dir": None, "use_system_cpp": False, "include_dirs": None,
        "cpp_args": None, "frontend_opt_level": 2, "jobs": 1,
    }
    assert len(compiled) == 1
    assert emissions == [(emissions[0][0], {"optimize": 2, "link_args": None})]
    assert executions == [{"cwd": str(Path.cwd()), "timeout": 60, "capture_output": True, "text": True}]
    assert not Path(emissions[0][0]).exists()
