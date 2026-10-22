"""Focused contracts for the extracted import/libpython pipeline seams."""

from __future__ import annotations


def test_pipeline_import_policy_facade_reexports_single_table_owners():
    from pcc.frontends.python import pipeline
    from pcc.frontends.python import pipeline_import_policy as policy

    assert pipeline._COMPILE_TIME_ONLY_IMPORT_FROMS is policy.COMPILE_TIME_ONLY_IMPORT_FROMS
    assert pipeline._COMPILE_TIME_ONLY_IMPORT_MODULES is policy.COMPILE_TIME_ONLY_IMPORT_MODULES
    assert pipeline._TEST_FACADE_IMPORT_MODULES is policy.TEST_FACADE_IMPORT_MODULES
    assert pipeline._ANNOTATION_ONLY_IMPORT_MODULES is policy.ANNOTATION_ONLY_IMPORT_MODULES
    assert pipeline._NATIVE_BUILTIN_IMPORTS is policy.NATIVE_BUILTIN_IMPORTS
    assert pipeline._NATIVE_IMPORT_FROMS is policy.NATIVE_IMPORT_FROMS
    assert pipeline._SCAFFOLD_IMPORT_MODULES is policy.SCAFFOLD_IMPORT_MODULES


def test_pipeline_libpython_ir_and_lifecycle_facade_has_one_owner():
    from pcc.frontends.python import pipeline
    from pcc.frontends.python import pipeline_libpython

    assert pipeline._ir_needs_libpython is pipeline_libpython.ir_needs_libpython
    assert (
        pipeline._ensure_libpython_main_thread_init
        is pipeline_libpython.ensure_main_thread_init
    )
    assert (
        pipeline._resolve_python_config_command
        is pipeline_libpython.resolve_python_config_command
    )


def test_libpython_link_flags_honor_explicit_environment(monkeypatch):
    from pcc.frontends.python import pipeline_libpython

    monkeypatch.setenv("PCC_PYTHON_LDFLAGS", "-L/test/python -lpython9.9")
    assert pipeline_libpython.link_flags() == [
        "-L/test/python",
        "-lpython9.9",
    ]


def test_libpython_link_flags_add_interpreter_libdir_when_config_omits_it(
    monkeypatch,
    tmp_path,
):
    import sysconfig

    from pcc.frontends.python import pipeline_libpython

    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "libpython3.15.dylib").write_bytes(b"")
    values = {
        "LIBDIR": str(libdir),
        "LDLIBRARY": "libpython3.15.dylib",
    }
    monkeypatch.delenv("PCC_PYTHON_LDFLAGS", raising=False)
    monkeypatch.setattr(
        pipeline_libpython,
        "resolve_python_config_command",
        lambda: "/fake/python3.15-config",
    )
    monkeypatch.setattr(
        pipeline_libpython.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "-lpython3.15 -ldl -framework CoreFoundation",
    )
    monkeypatch.setattr(
        sysconfig,
        "get_config_var",
        lambda name: values.get(name),
    )

    assert pipeline_libpython.link_flags() == [
        "-L" + str(libdir),
        "-lpython3.15",
        "-ldl",
        "-framework",
        "CoreFoundation",
    ]


def test_libpython_link_flags_keep_existing_interpreter_libdir_once(
    monkeypatch,
    tmp_path,
):
    import sysconfig

    from pcc.frontends.python import pipeline_libpython

    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "libpython3.15.dylib").write_bytes(b"")
    values = {
        "LIBDIR": str(libdir),
        "LDLIBRARY": "libpython3.15.dylib",
    }
    monkeypatch.delenv("PCC_PYTHON_LDFLAGS", raising=False)
    monkeypatch.setattr(
        pipeline_libpython,
        "resolve_python_config_command",
        lambda: "/fake/python3.15-config",
    )
    monkeypatch.setattr(
        pipeline_libpython.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "-L" + str(libdir) + " -lpython3.15",
    )
    monkeypatch.setattr(
        sysconfig,
        "get_config_var",
        lambda name: values.get(name),
    )

    flags = pipeline_libpython.link_flags()
    assert flags == ["-L" + str(libdir), "-lpython3.15"]
    assert flags.count("-L" + str(libdir)) == 1


def test_libpython_ir_scan_ignores_declarations_and_finds_calls():
    from pcc.frontends.python.pipeline_libpython import ir_needs_libpython

    assert not ir_needs_libpython("declare ptr @py_cpy_import(ptr)\n")
    assert ir_needs_libpython("%value = call ptr @py_cpy_import(ptr %name)\n")


def test_main_thread_init_is_idempotent_and_follows_program_args():
    from pcc.frontends.python.pipeline_libpython import ensure_main_thread_init

    ir = """\
define i32 @main(i32 %argc, ptr %argv) {
entry:
  call void @py_cpy_ensure_init()
  call void @py_set_program_args(i32 %argc, ptr %argv)
  call void @user_entry()
  ret i32 0
}
"""
    once = ensure_main_thread_init(ir)
    twice = ensure_main_thread_init(once)
    assert once == twice
    assert once.count("call void @py_cpy_ensure_init()") == 1
    assert once.index("@py_set_program_args") < once.index("@py_cpy_ensure_init")
    assert once.index("@py_cpy_ensure_init") < once.index("@user_entry")
