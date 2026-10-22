import importlib.util
import os
import subprocess
import sys
import types


def _load_repo_module(name):
    """Load a repo-root module by path.

    A bare ``import conftest`` used to land on the repository root's
    conftest.py only because tests/conftest.py patched path resolution so
    tests/c files looked like tests/ files (TEST-P2-REMOVE-LEGACY-PATH-SHIM).
    With that shim gone, ``tests/`` is on sys.path and the bare import would
    pick tests/conftest.py instead, so load the intended file explicitly.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        f"_pcc_repo_{name}", os.path.join(root, f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


root_conftest = _load_repo_module("conftest")
run_module = _load_repo_module("run")


def _git(tmp_path, *args):
    return subprocess.run(
        ["git", *args],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_clean_restores_tracked_files_and_removes_generated_outputs(tmp_path, monkeypatch):
    dep_dir = tmp_path / "dep"
    dep_dir.mkdir()

    pristine_makefile = (
        "distclean:\n"
        "\trm -f build.o generated.pc\n"
    )
    pristine_header = "#define VALUE 41\n"

    (dep_dir / "Makefile").write_text(pristine_makefile, encoding="utf-8")
    (dep_dir / "config.h").write_text(pristine_header, encoding="utf-8")

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "add", "dep/Makefile", "dep/config.h")
    _git(tmp_path, "commit", "-m", "init")

    (dep_dir / "Makefile").write_text(
        "distclean:\n"
        "\trm -f build.o generated.pc\n"
        "\t@echo cleaned\n"
    , encoding="utf-8")
    (dep_dir / "config.h").write_text("#define VALUE 99\n", encoding="utf-8")
    (dep_dir / "build.o").write_text("object\n", encoding="utf-8")
    (dep_dir / "generated.pc").write_text("pc\n", encoding="utf-8")

    monkeypatch.setattr(
        run_module,
        "CLEAN_TARGETS",
        {
            "fake": run_module.CleanTarget(
                make_dir="dep",
                make_target="distclean",
                make_if_exists=("Makefile",),
                restore_paths=("dep/Makefile", "dep/config.h"),
                remove_paths=("dep/generated.pc",),
            )
        },
    )

    assert (dep_dir / "build.o").exists()
    assert (dep_dir / "generated.pc").exists()

    run_module.clean(["fake"], repo_root=str(tmp_path))

    assert (dep_dir / "Makefile").read_text(encoding="utf-8") == pristine_makefile
    assert (dep_dir / "config.h").read_text(encoding="utf-8") == pristine_header
    assert not (dep_dir / "build.o").exists()
    assert not (dep_dir / "generated.pc").exists()


def test_auto_clean_targets_skip_preexisting_dirty_project_trees():
    targets = root_conftest._auto_clean_targets(
        {
            "projects/zlib-1.3.1/Makefile",
            "README.md",
        },
        {
            "projects/zlib-1.3.1/Makefile",
            "projects/postgresql-17.4/config.status",
            "projects/readline-8.2/Makefile",
        },
    )

    assert "generated" in targets
    assert "zlib" not in targets
    assert "postgres" in targets
    assert "readline" in targets


def test_auto_clean_targets_only_include_newly_dirty_project_trees():
    targets = root_conftest._auto_clean_targets(
        {"README.md"},
        {
            "README.md",
            "projects/readline-8.2/Makefile",
            "projects/readline-8.2/confdefs.h",
        },
    )

    assert targets == ("generated", "readline")


def test_pytest_auto_clean_is_opt_in(monkeypatch):
    monkeypatch.delenv("PCC_PYTEST_AUTO_CLEAN", raising=False)
    assert root_conftest._auto_clean_enabled() is False
    monkeypatch.setenv("PCC_PYTEST_AUTO_CLEAN", "1")
    assert root_conftest._auto_clean_enabled() is True


def test_pytest_sessionfinish_cleans_controller_targets(monkeypatch):
    calls = []

    class FakeConfig:
        _pcc_initial_status_paths = {"README.md"}
        _pcc_auto_clean_enabled = True

    class FakeSession:
        config = FakeConfig()

    def fake_clean(targets, repo_root=None):
        calls.append((targets, repo_root))

    monkeypatch.setattr(root_conftest.pcc_run, "clean", fake_clean)
    monkeypatch.setattr(
        root_conftest,
        "_status_paths",
        lambda _repo_root: {"README.md", "projects/zlib-1.3.1/Makefile"},
    )

    root_conftest.pytest_sessionfinish(FakeSession(), 0)

    assert calls == [(("generated", "zlib"), root_conftest._repo_root())]


def test_pytest_sessionfinish_skips_auto_clean_when_lock_is_busy(monkeypatch):
    calls = []

    class FakeLock:
        def close(self):
            pass

    class FakeConfig:
        _pcc_initial_status_paths = {"README.md"}
        _pcc_auto_clean_lockfile = FakeLock()
        _pcc_auto_clean_enabled = True

    class FakeSession:
        config = FakeConfig()

    def fake_clean(targets, repo_root=None):
        calls.append((targets, repo_root))

    monkeypatch.setattr(root_conftest.pcc_run, "clean", fake_clean)
    monkeypatch.setattr(
        root_conftest,
        "_status_paths",
        lambda _repo_root: {"README.md", "projects/zlib-1.3.1/Makefile"},
    )

    calls_to_flock = {"count": 0}

    def fake_flock(lockfile, op):
        calls_to_flock["count"] += 1
        if calls_to_flock["count"] == 1:
            return None
        raise BlockingIOError

    # conftest imports fcntl inside its lock helpers so Windows can load it;
    # hand that import a fake module rather than patching a module attribute.
    fake_fcntl = types.SimpleNamespace(
        LOCK_SH=1, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=fake_flock,
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)

    root_conftest.pytest_sessionfinish(FakeSession(), 0)

    assert calls_to_flock["count"] == 2
    assert calls == []
