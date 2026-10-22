"""Execute pytest startup/module code without compilers or POSIX modules."""

import builtins
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_platform_import(monkeypatch):
    original_import = builtins.__import__
    environments = {}
    missing_imports = []

    def unexpected(*args, **kwargs):
        raise AssertionError("platform collection must not access files or run tools")

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".")[0] in {"fcntl", "resource", "pwd", "grp", "termios", "pty"}:
            missing_imports.append(name)
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        environment = environments.get((globals or {}).get("__name__"))
        if environment is not None and name in environment.imports:
            return environment.imports[name]
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    # The blocker must apply even if the host has already cached fcntl.
    with pytest.raises(ModuleNotFoundError):
        builtins.__import__("fcntl")
    missing_imports.clear()

    def load(relative, *, platform="nt", configured=False, auto_clean=False):
        name = f"_pcc_platform_import_{len(environments)}"
        calls = []
        path = SimpleNamespace(**vars(os.path))
        path.isdir = lambda _path: True
        path.isfile = lambda _path: configured
        host_os = SimpleNamespace(**vars(os))
        host_os.name = platform
        host_os.path = path
        host_os.environ = {"PCC_PYTEST_AUTO_CLEAN": "1"} if auto_clean else {}
        host_os.makedirs = unexpected
        imports = {
            "os": host_os,
            "subprocess": SimpleNamespace(run=unexpected),
            "tempfile": SimpleNamespace(gettempdir=lambda: "/unused-pcc-platform-locks"),
            "run": SimpleNamespace(clean=unexpected),
            "tests.owned_ir_validation": SimpleNamespace(verify_ir_text=unexpected),
            "pcc.ir": SimpleNamespace(ir=SimpleNamespace()),
            "pcc.frontends.c.evaluator.c_evaluator": SimpleNamespace(CEvaluator=unexpected),
            "pcc.frontends.c.parse.c_parser": SimpleNamespace(
                CParser=lambda: calls.append("parser")
            ),
            "pcc.frontends.c.codegen.c_codegen": SimpleNamespace(
                CCodeGenerator=unexpected, postprocess_ir_text=unexpected
            ),
            "pcc.driver.project": SimpleNamespace(
                TranslationUnit=unexpected,
                collect_translation_units=unexpected,
                translation_unit_include_dirs=unexpected,
                _scan_make_goal=unexpected,
            ),
            "tests.parallel_jobs": SimpleNamespace(translation_unit_jobs=unexpected),
            "tests.integration.test_nginx": SimpleNamespace(
                _ensure_nginx_configured=lambda: calls.append("nginx")
            ),
        }
        environments[name] = SimpleNamespace(imports=imports)
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        module.__builtins__ = dict(vars(builtins), open=unexpected)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module, calls, missing_imports

    yield load
    # monkeypatch restores imports, bytecode policy, and each temporary module.


@pytest.mark.parametrize("workers", [0, 6])
@pytest.mark.parametrize("configured", [False, True])
def test_windows_root_conftest_import_and_warmup_without_posix_modules(
    isolated_platform_import, workers, configured
):
    module, calls, missing = isolated_platform_import(
        "conftest.py", configured=configured
    )
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=workers))
    module.pytest_configure(config)
    module.pytest_sessionfinish(SimpleNamespace(config=config), 0)
    assert calls == (["parser"] if workers else [])
    assert missing == []


@pytest.mark.parametrize("workers", [0, 6])
def test_windows_auto_clean_rejects_before_posix_imports(
    isolated_platform_import, workers
):
    module, calls, missing = isolated_platform_import("conftest.py", auto_clean=True)
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=workers))
    with pytest.raises(RuntimeError, match="POSIX shared file locks"):
        module.pytest_configure(config)
    assert calls == []
    assert missing == []


def test_posix_root_conftest_retains_parser_and_nginx_warmup(isolated_platform_import):
    module, calls, missing = isolated_platform_import("conftest.py", platform="posix")
    module.pytest_configure(SimpleNamespace(option=SimpleNamespace(numprocesses=6)))
    assert calls == ["parser", "nginx"]
    assert missing == []


@pytest.mark.parametrize("configured", [False, True])
def test_windows_nginx_module_import_and_explicit_operations_fail_closed(
    isolated_platform_import, configured
):
    module, calls, missing = isolated_platform_import(
        "tests/integration/test_nginx.py", configured=configured
    )
    assert module.NGINX_SOURCE_FILES == []
    for operation, args in (
        (module._file_lock, ("unused.lock",)),
        (module._ensure_nginx_configured, ()),
        (module._ensure_nginx_built_natively, ()),
        (module._nginx_cpp_args, ()),
        (module._nginx_source_files, ()),
        (module._nginx_units, ()),
        (module._nginx_preprocessed_units, ()),
        (module._nginx_link_args, ()),
        (module._compile_nginx_file, ("src/core/nginx.c",)),
        (module.test_nginx_make_goal_collects_source_files, ()),
        (module.test_nginx_source_compile, ("src/core/nginx.c",)),
        (module.test_nginx_native_build, ()),
        (module.test_nginx_full_system_link, ()),
    ):
        with pytest.raises(RuntimeError, match="nginx integration requires POSIX"):
            operation(*args)
    assert calls == []
    assert missing == []
