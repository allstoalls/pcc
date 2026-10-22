import json
import os
import sqlite3
import subprocess
import sys

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import collect_translation_units, translation_unit_include_dirs
from tests.parallel_jobs import translation_unit_jobs


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
PROJECTS_DIR = os.path.join(PROJECT_DIR, "projects")
SQLITE_DIR = os.path.join(PROJECTS_DIR, "sqlite-amalgamation-3490100")
SQLITE_TEST_MAIN = os.path.join(PROJECTS_DIR, "test_sqlite_main.c")
SQLITE_CPP_ARGS = (
    "-U__APPLE__",
    "-U__MACH__",
    "-U__DARWIN__",
    "-DSQLITE_THREADSAFE=0",
    "-DSQLITE_OMIT_WAL=1",
    "-DSQLITE_MAX_MMAP_SIZE=0",
)

pytestmark = pytest.mark.xdist_group(name="sqlite")


def _sqlite_units():
    return collect_translation_units(
        SQLITE_TEST_MAIN,
        dependencies=[os.path.join(SQLITE_DIR, "sqlite3.c")],
    )


def _sqlite_db_path(tmp_path):
    return tmp_path / "runtime.sqlite3"


def _run_sqlite_prepare_probe(tmp_path, sqlite_pcc_object, sql):
    sql_literal = json.dumps(sql)
    main_path = tmp_path / "main.c"
    main_path.write_text(
        f"""
#include <stdio.h>
#include "sqlite-amalgamation-3490100/sqlite3.h"

int main(void) {{
    sqlite3 *db = 0;
    sqlite3_stmt *stmt = 0;
    const char *tail = 0;
    const char *sql = {sql_literal};
    int rc = sqlite3_open(":memory:", &db);
    if (rc != SQLITE_OK) {{
        fprintf(stderr, "open rc=%d msg=%s\\n", rc, sqlite3_errmsg(db));
        return 1;
    }}

    rc = sqlite3_prepare_v2(db, sql, -1, &stmt, &tail);
    if (rc != SQLITE_OK) {{
        fprintf(
            stderr,
            "prepare rc=%d errcode=%d ext=%d offset=%d tail_off=%ld msg=%s\\n",
            rc,
            sqlite3_errcode(db),
            sqlite3_extended_errcode(db),
            sqlite3_error_offset(db),
            tail ? (long)(tail - sql) : -1L,
            sqlite3_errmsg(db)
        );
        fprintf(stderr, "tail=%s\\n", tail ? tail : "<null>");
        sqlite3_close(db);
        return 2;
    }}

    sqlite3_finalize(stmt);
    sqlite3_close(db);
    return 0;
}}
""".lstrip(),
        encoding="utf-8",
    )
    bin_path = tmp_path / "probe.out"
    compile_run = subprocess.run(
        [
            "cc",
            "-I",
            PROJECTS_DIR,
            str(main_path),
            str(sqlite_pcc_object),
            "-o",
            str(bin_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        compile_run.returncode == 0
    ), f"sqlite probe build failed:\n{compile_run.stdout}\n{compile_run.stderr}"
    return subprocess.run(
        [str(bin_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_sqlite_db_contents(db_path):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "select id, name, score from t order by id"
        ).fetchall()
        assert rows == [(1, "hello", 17), (2, "world", 20)]

        count, total, maxlen = conn.execute(
            "select count(*), sum(score), max(length(name)) from t"
        ).fetchone()
        assert (count, total, maxlen) == (2, 37, 5)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def sqlite_compiled_units():
    units, base_dir = _sqlite_units()
    compiled_units = CEvaluator().compile_translation_units(
        units,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=SQLITE_CPP_ARGS,
    )
    return compiled_units, base_dir


@pytest.fixture(scope="module")
def sqlite_pcc_object(tmp_path_factory):
    units, base_dir = _sqlite_units()
    sqlite_units = [unit for unit in units if unit.name == "sqlite3.c"]
    compiled_units = CEvaluator().compile_translation_units(
        sqlite_units,
        base_dir=base_dir,
        jobs=1,
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=SQLITE_CPP_ARGS,
    )
    obj_dir = tmp_path_factory.mktemp("sqlite_prepare_obj")
    obj_path = obj_dir / "sqlite3.o"
    CEvaluator().emit_compiled_units(
        compiled_units,
        emit_obj=str(obj_path),
        optimize=False,
    )
    return obj_path


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(SQLITE_DIR) else "sqlite-amalgamation-3490100 not found")
@pytest.mark.integration
def test_sqlite_prepare_select_literal_regression(tmp_path, sqlite_pcc_object):
    result = _run_sqlite_prepare_probe(tmp_path, sqlite_pcc_object, "select 1;")

    assert (
        result.returncode == 0
    ), f"sqlite prepare(select 1) failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(SQLITE_DIR) else "sqlite-amalgamation-3490100 not found")
@pytest.mark.integration
def test_sqlite_prepare_string_literal_regression(tmp_path, sqlite_pcc_object):
    result = _run_sqlite_prepare_probe(tmp_path, sqlite_pcc_object, "select 'x';")

    assert (
        result.returncode == 0
    ), f"sqlite prepare(select 'x') failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(SQLITE_DIR) else "sqlite-amalgamation-3490100 not found")
@pytest.mark.integration
def test_sqlite_prepare_create_table_regression(tmp_path, sqlite_pcc_object):
    result = _run_sqlite_prepare_probe(
        tmp_path,
        sqlite_pcc_object,
        "create table t(id integer primary key, name text, score integer);",
    )

    assert (
        result.returncode == 0
    ), f"sqlite prepare(create table) failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(SQLITE_DIR) else "sqlite-amalgamation-3490100 not found")
@pytest.mark.integration
def test_sqlite_runtime_with_mcjit_depends_on(tmp_path, sqlite_compiled_units):
    compiled_units, _base_dir = sqlite_compiled_units
    db_path = _sqlite_db_path(tmp_path)

    result = CEvaluator().evaluate_compiled_translation_units(
        compiled_units,
        optimize=True,
        prog_args=[str(db_path)],
    )

    assert result == 0
    assert db_path.exists()
    _assert_sqlite_db_contents(db_path)


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(SQLITE_DIR) else "sqlite-amalgamation-3490100 not found")
@pytest.mark.integration
def test_sqlite_runtime_with_system_link_depends_on(tmp_path, sqlite_compiled_units):
    compiled_units, base_dir = sqlite_compiled_units
    db_path = _sqlite_db_path(tmp_path)

    result = CEvaluator().run_compiled_translation_units_with_system_cc(
        compiled_units,
        optimize=True,
        base_dir=base_dir,
        prog_args=[str(db_path)],
    )

    assert (
        result.returncode == 0
    ), f"sqlite system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "sqlite version " in result.stdout
    assert "insert rowids: 1 2" in result.stdout
    assert "changes: insert=1 update=1" in result.stdout
    assert "selected row: world 20" in result.stdout
    assert "aggregate: count=2 sum=37 maxlen=5" in result.stdout
    assert "updated score: 17" in result.stdout
    assert "persisted score: 17" in result.stdout
    assert "OK" in result.stdout
    assert db_path.exists()
    _assert_sqlite_db_contents(db_path)


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(SQLITE_DIR) else "sqlite-amalgamation-3490100 not found")
@pytest.mark.integration
def test_sqlite_runtime_with_freestanding_libc(tmp_path, sqlite_compiled_units):
    compiled_units, base_dir = sqlite_compiled_units
    db_path = tmp_path / "freestanding-runtime.sqlite3"
    link_map = tmp_path / "sqlite-freestanding.map"
    map_flag = (
        "-Wl,-map," + str(link_map)
        if sys.platform == "darwin"
        else "-Wl,-Map," + str(link_map)
    )

    result = CEvaluator().run_compiled_translation_units_with_system_cc(
        compiled_units,
        optimize=True,
        base_dir=base_dir,
        prog_args=[str(db_path)],
        link_args=[map_flag],
        freestanding_libc=True,
    )

    assert result.returncode == 0, (
        "sqlite freestanding-libc runtime failed:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "selected row: world 20" in result.stdout
    assert "persisted score: 17" in result.stdout
    assert "OK" in result.stdout
    _assert_sqlite_db_contents(db_path)
    ownership = link_map.read_text(encoding="utf-8")
    assert "freestanding_mem_str.o" in ownership
    assert "freestanding_allocator.o" in ownership
    assert "freestanding_stdio.o" in ownership
    assert "vendor_" not in ownership


@pytest.mark.pcc_gate(unavailable=None if os.path.isdir(SQLITE_DIR) else "sqlite-amalgamation-3490100 not found")
def test_sqlite_depends_on_collects_amalgamation_and_main():
    units, base_dir = _sqlite_units()

    names = [unit.name for unit in units]

    assert base_dir == os.path.abspath(PROJECTS_DIR)
    assert names == ["sqlite3.c", "test_sqlite_main.c"]
