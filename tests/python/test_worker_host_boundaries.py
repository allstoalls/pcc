"""Host worker compatibility and native import boundaries; no native builds."""

import ast
import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcc.frontends.python import pipeline_frontend_workers as workers
from pcc.frontends.python import pipeline_import_scan as scan
from pcc.frontends.python import worker_process_pool as pool


ROOT = Path(__file__).resolve().parents[2]
POOL_HOST = "pcc.frontends.python.worker_process_host_pool"
RSS_HOST = "pcc.frontends.python.worker_resource_host_rss"


def test_pool_host_models_keep_dependency_and_callback_boundaries(monkeypatch):
    class Process:
        def __init__(self, pid, status):
            self.pid, self.status = pid, status
            self.returncode = None
            self.waited = False

        def poll(self):
            self.returncode = self.status
            return self.returncode

        def terminate(self):
            self.status = 0

        kill = terminate

        def wait(self, timeout):
            assert timeout == 2
            self.waited = True
            return self.poll()

    for platform in ("linux", "win32"):
        for kind in ("plain", "weighted", "chained"):
            launches, sleeps, processes = [], [], []

            def launch(argv, **options):
                launches.append((argv, options))
                process = Process(100 + len(processes), 7 if not processes else None)
                processes.append(process)
                return process

            def killpg(pid, number):
                process = processes[pid - 100]
                if process.status is None:
                    process.status = 0
                    raise PermissionError("leader exited during group signal")
                raise ProcessLookupError("leader already reaped")

            with monkeypatch.context() as selected:
                selected.setattr(pool, "subprocess", SimpleNamespace(Popen=launch))
                selected.setattr(pool, "os", SimpleNamespace(killpg=killpg))
                selected.setattr(pool, "time", SimpleNamespace(sleep=sleeps.append))
                selected.setattr(pool, "sys", SimpleNamespace(platform=platform))
                specs = [(["failure"], ["KEY=value"]), (["peer"], [])]
                if kind == "plain":
                    result = pool._host_pool(specs, 2)
                elif kind == "weighted":
                    result = pool._host_weighted_pool(specs, [1, 1], 2, 2)
                else:
                    result = pool._host_chained_pool(
                        specs, [1, 1], specs, ["a", "b"], (1, 0, 2), 2, 2,
                    )
            assert result == (1 << 32) | 7
            assert len(launches) == 2 and processes[1].waited
            assert processes[1].returncode == 0 and 0.2 in sleeps
            key, value = ("creationflags", 512) if platform == "win32" else ("process_group", 0)
            assert launches[0][1] == {"env": {"KEY": "value"}, key: value}

    def unavailable(*args, **kwargs):
        raise OSError("spawn failed")

    with monkeypatch.context() as selected:
        selected.setattr(pool, "subprocess", SimpleNamespace(Popen=unavailable))
        selected.setattr(pool, "time", SimpleNamespace(sleep=lambda seconds: None))
        specs = [(["missing"], [])]
        assert pool._host_pool(specs, 1) == (1 << 32) | 127
        assert pool._host_weighted_pool(specs, [1], 1, 1) == (1 << 32) | 127
        assert pool._host_chained_pool(specs, [1], specs, ["a"], (1, 0, 1), 1, 1) == (1 << 32) | 127

    calls, launches = [], []

    def success(argv, **options):
        launches.append(argv)
        return Process(200 + len(launches), 0)

    def reserve(path, floor):
        calls.append((path, floor))
        return 3

    with monkeypatch.context() as selected:
        selected.setattr(pool, "subprocess", SimpleNamespace(Popen=success))
        selected.setattr(pool, "os", SimpleNamespace(killpg=lambda *args: None))
        selected.setattr(pool, "time", SimpleNamespace(sleep=lambda seconds: None))
        selected.setattr(pool, "sys", SimpleNamespace(platform="linux"))
        selected.setattr(pool, "_followup_reservation", reserve)
        assert pool._host_chained_pool(
            [(["primary"], [])], [1], [(["followup"], [])], ["output"],
            (2, 3, 4), 1, 10,
        ) == 0
        assert calls == [("output", (2, 3, 4))]
        assert launches == [["primary"], ["followup"]]
        # An otherwise oversized task retains the idle-pool admission rule.
        assert pool._host_weighted_pool([(["oversized"], [])], [11], 1, 10) == 0


def test_rss_host_models_keep_units_errors_and_windows_callback(monkeypatch):
    import sys

    for platform, expected in (("darwin", 37), ("linux", 37 * 1024)):
        with monkeypatch.context() as selected:
            selected.setattr(workers, "sys", SimpleNamespace(platform=platform))
            selected.setitem(sys.modules, "resource", SimpleNamespace(
                RUSAGE_SELF=11,
                getrusage=lambda who: SimpleNamespace(ru_maxrss=37) if who == 11 else None,
            ))
            assert workers._host_coordinator_rss_bytes() == expected

    for error in (ImportError, OSError, ValueError, AttributeError):
        def failing(who):
            raise error("unavailable sampler")

        with monkeypatch.context() as selected:
            selected.setattr(workers, "sys", SimpleNamespace(platform="linux"))
            selected.setitem(sys.modules, "resource", SimpleNamespace(RUSAGE_SELF=0, getrusage=failing))
            assert workers._host_coordinator_rss_bytes() == -1

    calls = []
    with monkeypatch.context() as selected:
        selected.setattr(workers, "sys", SimpleNamespace(platform="win32"))
        selected.setattr(workers, "_host_windows_rss_bytes", lambda peak: calls.append(peak) or 91)
        assert workers._host_coordinator_rss_bytes() == 91 and calls == [True]

        def failing_windows(peak):
            raise RuntimeError("callback error remains visible")

        selected.setattr(workers, "_host_windows_rss_bytes", failing_windows)
        with pytest.raises(RuntimeError, match="callback error"):
            workers._host_coordinator_rss_bytes()

    def current_process():
        return 123

    def memory_info(handle, counters, size):
        assert handle == 123 and size == counters.cb == 80
        counters.peak_working_set, counters.working_set = 400, 200
        return 1

    kernel = SimpleNamespace(GetCurrentProcess=current_process, K32GetProcessMemoryInfo=memory_info)

    def library(name, *, use_last_error):
        assert name == "kernel32" and use_last_error is True
        return kernel

    ctypes = SimpleNamespace(
        Structure=object, c_uint32=object(), c_size_t=object(), c_void_p=object(),
        c_int32=object(), WinDLL=library, sizeof=lambda value: 80, byref=lambda value: value,
    )
    with monkeypatch.context() as selected:
        selected.setitem(sys.modules, "ctypes", ctypes)
        assert workers._host_windows_rss_bytes(True) == 400
        assert workers._host_windows_rss_bytes(False) == 200
        kernel.K32GetProcessMemoryInfo = lambda *args: 0
        assert workers._host_windows_rss_bytes(True) == -1
        ctypes.WinDLL = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no provider"))
        assert workers._host_windows_rss_bytes(False) == -1


def test_wrappers_preserve_all_non_pcc_dispatch_and_private_hooks(monkeypatch):
    original_import = builtins.__import__
    entries = (
        (pool, "_host_pool", POOL_HOST, "host_pool", ([], 2)),
        (pool, "_host_weighted_pool", POOL_HOST, "host_weighted_pool", ([], [], 2, 4)),
        (pool, "_host_chained_pool", POOL_HOST, "host_chained_pool", ([], [], [], [], (1, 0, 4), 2, 4)),
        (workers, "_host_coordinator_rss_bytes", RSS_HOST, "coordinator_rss_bytes", ()),
        (workers, "_host_windows_rss_bytes", RSS_HOST, "windows_rss_bytes", (True,)),
    )
    for implementation in ("cpython", "pypy", "other_runtime", "pcc"):
        for owner, private, host_module, helper, arguments in entries:
            imports, forwarded = [], []

            def call(*args, **kwargs):
                forwarded.append((args, kwargs))
                return 97

            def importing(name, *args, **kwargs):
                imports.append(name)
                if name == "sys":
                    return SimpleNamespace(implementation=SimpleNamespace(name=implementation))
                if name == host_module:
                    return SimpleNamespace(**{helper: call})
                return original_import(name, *args, **kwargs)

            with monkeypatch.context() as selected:
                selected.setattr(builtins, "__import__", importing)
                if implementation == "pcc":
                    with pytest.raises(RuntimeError, match="unavailable under native pcc"):
                        getattr(owner, private)(*arguments)
                else:
                    assert getattr(owner, private)(*arguments) == 97
            assert imports == (["sys"] if implementation == "pcc" else ["sys", host_module])
            if implementation == "pcc":
                assert not forwarded
            elif owner is pool:
                args, kwargs = forwarded[0]
                assert args == arguments
                assert kwargs["os"] is pool.os and kwargs["subprocess"] is pool.subprocess
                assert kwargs["sys"] is pool.sys and kwargs["time"] is pool.time
                if private == "_host_chained_pool":
                    assert kwargs["followup_reservation"] is pool._followup_reservation
            elif private == "_host_coordinator_rss_bytes":
                assert forwarded == [((workers.sys, workers._host_windows_rss_bytes), {})]
            else:
                assert forwarded == [((True,), {})]

    hooks = []
    with monkeypatch.context() as selected:
        selected.setattr(pool, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pypy")))
        selected.setattr(workers, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pypy")))
        for name in ("_host_pool", "_host_weighted_pool", "_host_chained_pool"):
            selected.setattr(pool, name, lambda *args, name=name: hooks.append(name) or 0)
        selected.setattr(workers, "_host_coordinator_rss_bytes", lambda: hooks.append("rss") or 71)
        pool.run_worker_processes([], 1)
        pool.run_weighted_worker_processes([], [], 1, 1)
        pool.run_chained_worker_processes([], [], [], [], (1, 0, 1), 1, 1)
        assert workers._coordinator_rss_bytes() == workers._worker_peak_rss_bytes() == 71
    assert hooks == ["_host_pool", "_host_weighted_pool", "_host_chained_pool", "rss", "rss"]

    native_hooks = []

    def unexpected_host(*args, **kwargs):
        raise AssertionError("native dispatch entered a host wrapper")

    with monkeypatch.context() as selected:
        selected.setattr(pool, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pcc")))
        selected.setattr(workers, "sys", SimpleNamespace(implementation=SimpleNamespace(name="pcc")))
        for name in ("_host_pool", "_host_weighted_pool", "_host_chained_pool"):
            selected.setattr(pool, name, unexpected_host)
        for name in ("_native_pool", "_native_weighted_pool", "_native_chained_pool"):
            selected.setattr(pool, name, lambda *args, name=name: native_hooks.append(name) or 0)
        selected.setattr(workers, "_host_coordinator_rss_bytes", unexpected_host)
        selected.setattr(workers, "_native_current_rss_bytes", lambda: 85)
        selected.setattr(workers, "_native_peak_rss_bytes", lambda: 86)
        pool.run_worker_processes([], 1)
        pool.run_weighted_worker_processes([], [], 1, 1)
        pool.run_chained_worker_processes([], [], [], [], (1, 0, 1), 1, 1)
        assert workers._coordinator_rss_bytes() == 85 and workers._worker_peak_rss_bytes() == 86
    assert native_hooks == ["_native_pool", "_native_weighted_pool", "_native_chained_pool"]


def test_actual_wrapper_imports_keep_host_modules_outside_native_closure():
    from pcc.frontends.python.pipeline_dependency_closure import _package_import_targets

    for module, host, wrappers in (
        (pool, POOL_HOST, ("_host_pool", "_host_weighted_pool", "_host_chained_pool")),
        (workers, RSS_HOST, ("_host_coordinator_rss_bytes", "_host_windows_rss_bytes")),
    ):
        path = Path(module.__file__)
        source = path.read_text()
        native = scan._without_inactive_runtime_imports(source)
        assert host not in native
        for implementation in ("cpython", "pypy", "other_runtime"):
            assert host in scan._without_inactive_implementation_imports(source, implementation=implementation)
        names = [name for _, name in _package_import_targets(str(path), module.__name__, root_dir=str(ROOT))]
        assert host not in names
        functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
        for name in wrappers:
            statements = functions[name].body[1:]  # Compatibility docstring.
            assert ast.unparse(statements[0]) == "import sys as execution_runtime"
            assert ast.unparse(statements[1].test) == "execution_runtime.implementation.name != 'pcc'"
            assert isinstance(statements[2], ast.Raise)
        for changed in (
            source.replace('if execution_runtime.implementation.name != "pcc":',
                           'if execution_runtime.implementation.name != "pcc" and enabled:'),
            source.replace("    import sys as execution_runtime\n",
                           "    import sys as execution_runtime\n    execution_runtime = other\n"),
            "import sys as execution_runtime\n" + source.replace("    import sys as execution_runtime\n", ""),
        ):
            assert host in scan._without_inactive_runtime_imports(changed)
        if module is pool:
            assert "import hashlib" in native and "run_resource_worker_processes" in native
        else:
            assert "import ctypes" not in native and "_native_current_rss_bytes" in native
