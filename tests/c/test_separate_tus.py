import os

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import collect_translation_units


PROJECT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
LUA_PROJECT_DIR = os.path.join(PROJECT_DIR, "projects", "lua-5.5.0")
LUA_MATH_TEST = os.path.join(LUA_PROJECT_DIR, "testes", "math.lua")
LUA_CPP_ARGS = ("-DLUA_USE_JUMPTABLE=0", "-DLUA_NOBUILTIN")


def _evaluate_project(
    path,
    jobs=1,
    optimize=False,
    prog_args=None,
    sources_from_make=None,
    cpp_args=None,
):
    units, base_dir = collect_translation_units(
        path, sources_from_make=sources_from_make
    )
    return CEvaluator().evaluate_translation_units(
        units,
        optimize=optimize,
        base_dir=base_dir,
        prog_args=prog_args or [],
        jobs=jobs,
        cpp_args=cpp_args,
    )


def _run_project_system_link(
    path,
    jobs=1,
    optimize=False,
    prog_args=None,
    sources_from_make=None,
    link_args=None,
    cpp_args=None,
):
    units, base_dir = collect_translation_units(
        path, sources_from_make=sources_from_make
    )
    return CEvaluator().run_translation_units_with_system_cc(
        units,
        optimize=optimize,
        base_dir=base_dir,
        prog_args=prog_args or [],
        jobs=jobs,
        link_args=link_args or [],
        cpp_args=cpp_args,
    )


def test_separate_tus_jobs1_runtime_smoke(tmp_path):
    (tmp_path / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "int main(void) { return helper() == 41 ? 0 : 1; }\n"
    , encoding="utf-8")

    assert _evaluate_project(
        str(tmp_path),
        jobs=1,
        optimize=True,
    ) == 0


def test_separate_tus_lua_math_runtime_jobs2():
    assert _evaluate_project(
        LUA_PROJECT_DIR,
        jobs=2,
        optimize=True,
        prog_args=[LUA_MATH_TEST],
        sources_from_make="lua",
        cpp_args=LUA_CPP_ARGS,
    ) == 0


def test_separate_tus_rejects_amalgamation_directory(tmp_path):
    (tmp_path / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (tmp_path / "bundle.c").write_text('#include "helper.c"\n', encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\n"
        "int main(void) { return helper(); }\n"
    , encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate external function definition"):
        _evaluate_project(
            str(tmp_path),
            jobs=2,
            optimize=False,
        )


def test_separate_tus_links_across_files(tmp_path):
    (tmp_path / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\nint main(void) { return helper() + 1; }\n"
    , encoding="utf-8")

    assert _evaluate_project(str(tmp_path), jobs=1) == 42


def test_separate_tus_resolves_extern_globals(tmp_path):
    (tmp_path / "common.h").write_text("extern int shared;\n", encoding="utf-8")
    (tmp_path / "helper.c").write_text(
        '#include "common.h"\nint read_shared(void) { return shared; }\n'
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        '#include "common.h"\n'
        "int read_shared(void);\n"
        "int shared = 9;\n"
        "int main(void) { return read_shared(); }\n"
    , encoding="utf-8")

    assert _evaluate_project(str(tmp_path), jobs=1) == 9


def test_separate_tus_resolves_inferred_array_size_across_files(tmp_path):
    (tmp_path / "table.c").write_text(
        "const unsigned char lengths[] = {1, 2 + 2, 5};\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "extern const unsigned char lengths[];\n"
        "int main(void) {\n"
        "  return !(lengths[0] == 1 && lengths[1] == 4 && lengths[2] == 5);\n"
        "}\n"
    , encoding="utf-8")

    assert _evaluate_project(str(tmp_path), jobs=2) == 0
    assert _run_project_system_link(str(tmp_path), jobs=2).returncode == 0


def test_separate_tus_resolves_inferred_pointer_array_size_across_files(tmp_path):
    (tmp_path / "table.c").write_text(
        'const char *names[] = {"a", "bc"};\n'
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "extern const char *names[];\n"
        "int main(void) {\n"
        "  return !(\n"
        "    names[0][0] == 'a' && names[0][1] == '\\0' &&\n"
        "    names[1][0] == 'b' && names[1][1] == 'c' && names[1][2] == '\\0'\n"
        "  );\n"
        "}\n"
    , encoding="utf-8")

    assert _evaluate_project(str(tmp_path), jobs=2) == 0
    assert _run_project_system_link(str(tmp_path), jobs=2).returncode == 0


def test_separate_tus_reject_duplicate_external_functions(tmp_path):
    (tmp_path / "left.c").write_text("int helper(void) { return 11; }\n", encoding="utf-8")
    (tmp_path / "right.c").write_text(
        "int helper(void) { return 31; }\n"
        "int main(void) { return helper(); }\n"
    , encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate external function definition"):
        _evaluate_project(str(tmp_path), jobs=2)


def test_separate_tus_keep_file_scope_static_symbols_distinct(tmp_path):
    (tmp_path / "left.c").write_text(
        "static int value = 17;\n"
        "static int helper(void) { return value; }\n"
        "int left(void) { return helper(); }\n"
    , encoding="utf-8")
    (tmp_path / "right.c").write_text(
        "static int value = 25;\n"
        "static int helper(void) { return value; }\n"
        "int right(void) { return helper(); }\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int left(void);\nint right(void);\nint main(void) { return left() + right(); }\n"
    , encoding="utf-8")

    assert _evaluate_project(str(tmp_path), jobs=2) == 42


def test_separate_tus_static_local_incomplete_array_string_literal(tmp_path):
    (tmp_path / "version.c").write_text(
        'const char *get_version(void) {\n'
        '  static const char my_version[] = "1.3.1";\n'
        "  return my_version;\n"
        "}\n"
    , encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "const char *get_version(void);\n"
        "int main(void) {\n"
        "  const char *p = get_version();\n"
        "  return !(p[0] == '1' && p[1] == '.' && p[4] == '1' && p[5] == '\\0');\n"
        "}\n"
    , encoding="utf-8")

    assert _evaluate_project(str(tmp_path), jobs=2) == 0
    assert _run_project_system_link(str(tmp_path), jobs=2).returncode == 0


def test_separate_tus_system_cc_linker_runtime(tmp_path):
    (tmp_path / "helper.c").write_text("int helper(void) { return 41; }\n", encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "int helper(void);\nint main(void) { return helper() + 1; }\n"
    , encoding="utf-8")

    result = _run_project_system_link(str(tmp_path), jobs=2)
    assert result.returncode == 42
