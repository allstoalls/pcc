import os
import warnings

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import (
    collect_cpp_args,
    collect_project,
    collect_translation_units,
    translation_unit_include_dirs,
)


PROJECT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
LUA_WITH_ONELUA_DIR = os.path.join(PROJECT_DIR, "projects", "lua-5.5.0")
LUA_CPP_ARGS = ("-DLUA_USE_JUMPTABLE=0", "-DLUA_NOBUILTIN")


def test_collect_translation_units_accepts_with_onelua_directory():
    units, base_dir = collect_translation_units(LUA_WITH_ONELUA_DIR)

    assert base_dir == os.path.abspath(LUA_WITH_ONELUA_DIR)
    assert any(unit.name == "onelua.c" for unit in units)
    assert units[-1].name == "lua.c"


def test_collect_translation_units_accepts_make_selected_lua_sources():
    units, base_dir = collect_translation_units(
        LUA_WITH_ONELUA_DIR, sources_from_make="lua"
    )

    assert base_dir == os.path.abspath(LUA_WITH_ONELUA_DIR)
    assert units[-1].name == "lua.c"
    assert all(unit.name != "onelua.c" for unit in units)


def test_collect_translation_units_rejects_multiple_mains(tmp_path):
    (tmp_path / "a.c").write_text("int main(void) { return 1; }\n", encoding="utf-8")
    (tmp_path / "b.c").write_text("int main(void) { return 2; }\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Multiple main"):
        collect_translation_units(str(tmp_path))


def test_collect_project_merged_directory_reports_same_tu_redefinitions(tmp_path):
    (tmp_path / "helper.c").write_text("int helper(void) { return 1; }\n", encoding="utf-8")
    (tmp_path / "main.c").write_text(
        '#include "helper.c"\nint main(void) { return helper(); }\n'
    , encoding="utf-8")

    source, base_dir = collect_project(str(tmp_path))

    with pytest.raises(ValueError, match="redefinition of function 'helper'"):
        CEvaluator().evaluate(source, base_dir=base_dir)


def test_system_cpp_does_not_leave_temp_c_files_in_source_dir(tmp_path):
    (tmp_path / "local.h").write_text("#define VALUE 42\n", encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())

    output = CEvaluator._system_cpp(
        '#include "local.h"\nint main(void) { return VALUE; }\n',
        base_dir=str(tmp_path),
    )

    after = sorted(p.name for p in tmp_path.iterdir())

    assert "return 42;" in output
    assert after == before


def test_system_cpp_accepts_custom_cpp_args(tmp_path):
    inc_dir = tmp_path / "inc"
    inc_dir.mkdir()
    (inc_dir / "local.h").write_text("#define BASE 41\n", encoding="utf-8")

    output = CEvaluator._system_cpp(
        '#include "local.h"\n#ifndef EXTRA\n#error missing EXTRA\n#endif\nint main(void) { return BASE + EXTRA; }\n',
        base_dir=str(tmp_path),
        cpp_args=[f"-I{inc_dir}", "-DEXTRA=1"],
    )

    assert "return 41 + 1;" in output


def test_collect_project_make_goal_filters_c_sources(tmp_path):
    (tmp_path / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\nint main(void) { return helper() + 1; }\n"
    , encoding="utf-8")
    (tmp_path / "ignored.c").write_text("int ignored(void) { return 99; }\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "CPPFLAGS = -DVALUE=41\n"
        "app: helper.o main.o\n"
        "\tcc -o app helper.o main.o\n\n"
        "helper.o: helper.c\n"
        "\tcc $(CPPFLAGS) -c -o helper.o helper.c\n\n"
        "main.o: main.c\n"
        "\tcc $(CPPFLAGS) -c -o main.o main.c\n\n"
        "ignored.o: ignored.c\n"
        "\tcc -c -o ignored.o ignored.c\n"
    , encoding="utf-8")

    source, base_dir = collect_project(str(tmp_path), sources_from_make="app")

    assert base_dir == os.path.abspath(str(tmp_path))
    assert "// --- helper.c ---" in source
    assert "// --- ignored.c ---" not in source
    assert CEvaluator().evaluate(source, base_dir=base_dir) == 42


def test_collect_project_make_goal_supports_cpp_args_runtime(tmp_path):
    (tmp_path / "helper.c").write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() + 1; }\n"
    , encoding="utf-8")
    (tmp_path / "ignored.c").write_text("int ignored(void) { return 99; }\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "CPPFLAGS = -DVALUE=41\n"
        "app: helper.o main.o\n"
        "\tcc -o app helper.o main.o\n\n"
        "helper.o: helper.c\n"
        "\tcc $(CPPFLAGS) -c -o helper.o helper.c\n\n"
        "main.o: main.c\n"
        "\tcc $(CPPFLAGS) -c -o main.o main.c\n\n"
        "ignored.o: ignored.c\n"
        "\tcc -c -o ignored.o ignored.c\n"
    , encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        source, base_dir = collect_project(str(tmp_path), sources_from_make="app")

    assert not caught

    assert "// --- ignored.c ---" not in source
    assert (
        CEvaluator().evaluate(
            source,
            base_dir=base_dir,
            cpp_args=["-DVALUE=41"],
        )
        == 42
    )

    inferred_cpp_args = collect_cpp_args(str(tmp_path), sources_from_make="app")
    assert "-DVALUE=41" in inferred_cpp_args
    assert (
        CEvaluator().evaluate(
            source,
            base_dir=base_dir,
            cpp_args=inferred_cpp_args,
        )
        == 42
    )


def test_collect_project_make_selected_lua_sources_runtime():
    source, base_dir = collect_project(LUA_WITH_ONELUA_DIR, sources_from_make="lua")

    assert base_dir == os.path.abspath(LUA_WITH_ONELUA_DIR)
    assert "// --- onelua.c ---" not in source
    assert "// --- lua.c (main) ---" in source
    assert "int main" in source


def test_collect_translation_units_accepts_file_with_dependency_make_goal(tmp_path):
    dep_dir = tmp_path / "lib"
    dep_dir.mkdir()
    (dep_dir / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (dep_dir / "ignored.c").write_text("int ignored(void) { return 99; }\n", encoding="utf-8")
    (dep_dir / "Makefile").write_text(
        "lib: helper.o\n"
        "\tcc -o lib helper.o\n\n"
        "helper.o: helper.c\n"
        "\tcc -c -o helper.o helper.c\n\n"
        "ignored.o: ignored.c\n"
        "\tcc -c -o ignored.o ignored.c\n"
    , encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")

    units, base_dir = collect_translation_units(
        str(main_path),
        dependencies=[f"{dep_dir}=lib"],
    )

    assert base_dir == os.path.abspath(str(tmp_path))
    assert [unit.name for unit in units] == ["helper.c", "main.c"]
    assert (
        CEvaluator().evaluate_translation_units(
            units,
            base_dir=base_dir,
            jobs=2,
            include_dirs=translation_unit_include_dirs(units),
        )
        == 0
    )


def test_collect_translation_units_main_detection_uses_explicit_cpp_args(tmp_path):
    helper_path = tmp_path / "helper.c"
    helper_path.write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() == VALUE ? 0 : 1; }\n"
    , encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        units, base_dir = collect_translation_units(
            str(main_path),
            dependencies=[str(helper_path)],
            cpp_args=["-DVALUE=41"],
        )

    assert not caught
    assert base_dir == os.path.abspath(str(tmp_path))
    assert [unit.name for unit in units] == ["helper.c", "main.c"]
    assert (
        CEvaluator().evaluate_translation_units(
            units,
            base_dir=base_dir,
            jobs=2,
            include_dirs=translation_unit_include_dirs(units),
            cpp_args=["-DVALUE=41"],
        )
        == 0
    )


def test_collect_translation_units_make_goal_supports_cpp_args_runtime(tmp_path):
    (tmp_path / "helper.c").write_text(
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int helper(void) { return VALUE; }\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "#ifndef VALUE\n"
        "#error missing VALUE\n"
        "#endif\n"
        "int main(void) { return helper() + 1; }\n"
    , encoding="utf-8")
    (tmp_path / "ignored.c").write_text("int ignored(void) { return 99; }\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "CPPFLAGS = -DVALUE=41\n"
        "app: helper.o main.o\n"
        "\tcc -o app helper.o main.o\n\n"
        "helper.o: helper.c\n"
        "\tcc $(CPPFLAGS) -c -o helper.o helper.c\n\n"
        "main.o: main.c\n"
        "\tcc $(CPPFLAGS) -c -o main.o main.c\n\n"
        "ignored.o: ignored.c\n"
        "\tcc -c -o ignored.o ignored.c\n"
    , encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        units, base_dir = collect_translation_units(
            str(tmp_path),
            sources_from_make="app",
        )

    assert not caught

    assert [unit.name for unit in units] == ["helper.c", "main.c"]
    assert (
        CEvaluator().evaluate_translation_units(
            units,
            base_dir=base_dir,
            jobs=2,
            include_dirs=translation_unit_include_dirs(units),
            cpp_args=["-DVALUE=41"],
        )
        == 42
    )

    inferred_cpp_args = collect_cpp_args(str(tmp_path), sources_from_make="app")
    assert "-DVALUE=41" in inferred_cpp_args
    assert (
        CEvaluator().evaluate_translation_units(
            units,
            base_dir=base_dir,
            jobs=2,
            include_dirs=translation_unit_include_dirs(units),
            cpp_args=inferred_cpp_args,
        )
        == 42
    )


def test_collect_cpp_args_ignore_recursive_make_subdir_flags(tmp_path):
    dep_dir = tmp_path / "dep"
    dep_dir.mkdir()
    (dep_dir / "dep.c").write_text(
        "#ifndef DEP_ONLY\n"
        "#error missing DEP_ONLY\n"
        "#endif\n"
        "int dep(void) { return DEP_ONLY; }\n"
    , encoding="utf-8")
    (dep_dir / "Makefile").write_text(
        "all: dep.o\n\n"
        "dep.o: dep.c\n"
        "\tcc -DDEP_ONLY=7 -Idep_include -c -o dep.o dep.c\n"
    , encoding="utf-8")
    (tmp_path / "app.c").write_text(
        "#ifndef APP_ONLY\n"
        "#error missing APP_ONLY\n"
        "#endif\n"
        "int main(void) { return APP_ONLY == 41 ? 0 : 1; }\n"
    , encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "app:\n"
        "\t$(MAKE) -C dep all\n"
        "\tcc -DAPP_ONLY=41 -Iapp_include -c -o app.o app.c\n"
    , encoding="utf-8")

    inferred_cpp_args = collect_cpp_args(str(tmp_path), sources_from_make="app")

    assert "-DAPP_ONLY=41" in inferred_cpp_args
    assert "-DDEP_ONLY=7" not in inferred_cpp_args
    assert str(tmp_path / "app_include") in inferred_cpp_args
    assert str(tmp_path / "dep" / "dep_include") not in inferred_cpp_args


def test_collect_translation_units_make_goal_handles_absolute_source_paths(tmp_path):
    dep_dir = tmp_path / "dep"
    dep_dir.mkdir()
    helper_path = dep_dir / "helper.c"
    helper_path.write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (dep_dir / "Makefile").write_text(
        "lib: helper.o\n"
        "\tcc -o lib helper.o\n\n"
        "helper.o:\n"
        f"\tcc -c -o helper.o {helper_path.resolve()}\n"
    , encoding="utf-8")
    main_path = tmp_path / "main.c"
    main_path.write_text(
        "int helper(void);\n"
        "int main(void) { return helper() + 1; }\n"
    , encoding="utf-8")

    units, base_dir = collect_translation_units(
        str(main_path),
        dependencies=[f"{dep_dir}=lib"],
    )

    assert base_dir == os.path.abspath(str(tmp_path))
    assert units[0].path == str(helper_path.resolve())
    assert [unit.name for unit in units] == ["helper.c", "main.c"]
    assert (
        CEvaluator().evaluate_translation_units(
            units,
            base_dir=base_dir,
            jobs=2,
            include_dirs=translation_unit_include_dirs(units),
        )
        == 42
    )


def test_collect_translation_units_make_goal_falls_back_to_clean_when_goal_is_uptodate(tmp_path):
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    include_dir = tmp_path / "include"
    include_dir.mkdir()
    (tmp_path / "configure").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_path / "config.status").write_text("ok\n", encoding="utf-8")
    (tmp_path / "main.o").write_text("", encoding="utf-8")
    (tmp_path / "app").write_text("", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "CPPFLAGS = -DVALUE=41 -Iinclude\n"
        "app: config.status main.o\n"
        "\tcc -o app main.o\n\n"
        "config.status: configure\n"
        "\t@echo configure failure 1>&2\n"
        "\t@exit 2\n\n"
        "main.o: main.c\n"
        "\tcc $(CPPFLAGS) -c -o main.o main.c\n\n"
        "clean:\n"
        "\trm -f app main.o\n"
    , encoding="utf-8")

    units, base_dir = collect_translation_units(
        str(tmp_path),
        sources_from_make="app",
    )

    assert base_dir == os.path.abspath(str(tmp_path))
    assert [unit.name for unit in units] == ["main.c"]
    inferred_cpp_args = collect_cpp_args(str(tmp_path), sources_from_make="app")
    assert "-DVALUE=41" in inferred_cpp_args
    assert str(include_dir) in inferred_cpp_args


def test_collect_make_goal_falls_back_to_static_makefile_parse_with_missing_include(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    support_dir = tmp_path / "support"
    support_dir.mkdir()

    (project_dir / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (project_dir / "main.c").write_text(
        "int helper(void);\n"
        "int main(void) { return helper() + 1; }\n"
    , encoding="utf-8")
    (project_dir / "Makefile").write_text(
        "top_builddir = ..\n"
        "include $(top_builddir)/Makefile.global\n"
        "OBJS = helper.o main.o\n"
        "override CPPFLAGS := -I$(srcdir) $(CPPFLAGS) -I$(top_builddir)/support\n"
        "override CFLAGS += $(PTHREAD_CFLAGS)\n"
    , encoding="utf-8")
    (tmp_path / "Makefile.global.in").write_text(
        "srcdir = .\n"
        "CPPFLAGS := -I$(top_builddir)/include $(CPPFLAGS)\n"
        "PTHREAD_CFLAGS = @PTHREAD_CFLAGS@\n"
    , encoding="utf-8")
    (tmp_path / "configure.ac").write_text(
        'PTHREAD_CFLAGS="$PTHREAD_CFLAGS -D_REENTRANT -D_THREAD_SAFE"\n'
    , encoding="utf-8")

    units, base_dir = collect_translation_units(str(project_dir), sources_from_make="app")
    names = [unit.name for unit in units]

    assert base_dir == os.path.abspath(str(project_dir))
    assert names == ["helper.c", "main.c"]

    inferred_cpp_args = collect_cpp_args(str(project_dir), sources_from_make="app")
    assert str(project_dir) in inferred_cpp_args
    assert str(tmp_path / "include") in inferred_cpp_args
    assert str(tmp_path / "support") in inferred_cpp_args
    assert "-D_REENTRANT" in inferred_cpp_args
    assert "-D_THREAD_SAFE" in inferred_cpp_args
