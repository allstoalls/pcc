import os
import socket
import subprocess
from contextlib import contextmanager
import fcntl
import hashlib
import shlex
import tempfile

import pytest

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from tests.cli_support import run_cli
from pcc.driver.project import (
    collect_cpp_args,
    collect_translation_units,
    translation_unit_include_dirs,
)
from tests.parallel_jobs import translation_unit_jobs

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
PROJECTS_DIR = os.path.join(PROJECT_DIR, "projects")
POSTGRES_DIR = os.path.join(PROJECTS_DIR, "postgresql-17.4")
READLINE_DIR = os.path.join(PROJECTS_DIR, "readline-8.2")
ZLIB_DIR = os.path.join(PROJECTS_DIR, "zlib-1.3.1")
POSTGRES_SRC_DIR = os.path.join(POSTGRES_DIR, "src")
POSTGRES_LIBPQ_DIR = os.path.join(POSTGRES_SRC_DIR, "interfaces", "libpq")
POSTGRES_PORT_DIR = os.path.join(POSTGRES_SRC_DIR, "port")
POSTGRES_COMMON_DIR = os.path.join(POSTGRES_SRC_DIR, "common")
POSTGRES_BACKEND_DIR = os.path.join(POSTGRES_SRC_DIR, "backend")
POSTGRES_INCLUDE_CATALOG_DIR = os.path.join(POSTGRES_SRC_DIR, "include", "catalog")
POSTGRES_INITDB_DIR = os.path.join(POSTGRES_SRC_DIR, "bin", "initdb")
POSTGRES_PGCTL_DIR = os.path.join(POSTGRES_SRC_DIR, "bin", "pg_ctl")
POSTGRES_SNOWBALL_DIR = os.path.join(POSTGRES_BACKEND_DIR, "snowball")
POSTGRES_TIMEZONE_DIR = os.path.join(POSTGRES_SRC_DIR, "timezone")
POSTGRES_TEST_MAIN = os.path.join(PROJECTS_DIR, "test_postgres_main.c")
POSTGRES_QUERY_MAIN = os.path.join(PROJECTS_DIR, "test_postgres_query_main.c")
POSTGRES_MAKEFILE = os.path.join(POSTGRES_LIBPQ_DIR, "Makefile")
POSTGRES_MAKEFILE_GLOBAL = os.path.join(POSTGRES_SRC_DIR, "Makefile.global")
POSTGRES_MAKEFILE_PORT = os.path.join(POSTGRES_SRC_DIR, "Makefile.port")
POSTGRES_CONFIG_STATUS = os.path.join(POSTGRES_DIR, "config.status")
POSTGRES_PORT_ARCHIVE = os.path.join(POSTGRES_PORT_DIR, "libpgport_shlib.a")
POSTGRES_COMMON_ARCHIVE = os.path.join(POSTGRES_COMMON_DIR, "libpgcommon_shlib.a")
POSTGRES_PATHS_HEADER = os.path.join(POSTGRES_PORT_DIR, "pg_config_paths.h")
POSTGRES_ERRCODES_HEADER = os.path.join(
    POSTGRES_SRC_DIR, "include", "utils", "errcodes.h"
)
POSTGRES_BACKEND_BIN = os.path.join(POSTGRES_BACKEND_DIR, "postgres")
POSTGRES_INITDB_BIN = os.path.join(POSTGRES_INITDB_DIR, "initdb")
POSTGRES_PGCTL_BIN = os.path.join(POSTGRES_PGCTL_DIR, "pg_ctl")
POSTGRES_ZIC_BIN = os.path.join(POSTGRES_TIMEZONE_DIR, "zic")
POSTGRES_BKI = os.path.join(POSTGRES_INCLUDE_CATALOG_DIR, "postgres.bki")
POSTGRES_SNOWBALL_SQL = os.path.join(
    POSTGRES_BACKEND_DIR, "snowball", "snowball_create.sql"
)
POSTGRES_MAKE_GOAL = "libpq.a"
READLINE_LIB = os.path.join(READLINE_DIR, "libreadline.a")
READLINE_HISTORY_LIB = os.path.join(READLINE_DIR, "libhistory.a")
ZLIB_LIB = os.path.join(ZLIB_DIR, "libz.a")
POSTGRES_CONFIG_LOCK = os.path.join(
    tempfile.gettempdir(),
    f"pcc-postgres-build-{hashlib.sha256(POSTGRES_DIR.encode('utf-8')).hexdigest()[:16]}.lock",
)
READLINE_BUILD_LOCK = os.path.join(
    tempfile.gettempdir(),
    f"pcc-readline-build-{hashlib.sha256(READLINE_DIR.encode('utf-8')).hexdigest()[:16]}.lock",
)
READLINE_INCLUDE_ROOT = os.path.join(
    tempfile.gettempdir(),
    f"pcc-readline-include-{hashlib.sha256(READLINE_DIR.encode('utf-8')).hexdigest()[:16]}",
)
ZLIB_BUILD_LOCK = os.path.join(
    tempfile.gettempdir(),
    f"pcc-zlib-build-{hashlib.sha256(ZLIB_DIR.encode('utf-8')).hexdigest()[:16]}.lock",
)
pytestmark = pytest.mark.xdist_group(name="vendor_builds")
POSTGRES_CONFIGURE_ARGS = (
    "--with-readline",
    "--with-zlib",
    "--without-openssl",
    "--without-icu",
    "--without-ldap",
    "--without-gssapi",
)
POSTGRES_RUNTIME_SHARE_FILES = (
    (POSTGRES_BKI, "postgres.bki"),
    (
        os.path.join(POSTGRES_BACKEND_DIR, "libpq", "pg_hba.conf.sample"),
        "pg_hba.conf.sample",
    ),
    (
        os.path.join(POSTGRES_BACKEND_DIR, "libpq", "pg_ident.conf.sample"),
        "pg_ident.conf.sample",
    ),
    (
        os.path.join(POSTGRES_BACKEND_DIR, "utils", "misc", "postgresql.conf.sample"),
        "postgresql.conf.sample",
    ),
    (POSTGRES_SNOWBALL_SQL, "snowball_create.sql"),
    (
        os.path.join(POSTGRES_BACKEND_DIR, "catalog", "information_schema.sql"),
        "information_schema.sql",
    ),
    (
        os.path.join(POSTGRES_BACKEND_DIR, "catalog", "sql_features.txt"),
        "sql_features.txt",
    ),
    (
        os.path.join(POSTGRES_INCLUDE_CATALOG_DIR, "system_constraints.sql"),
        "system_constraints.sql",
    ),
    (
        os.path.join(POSTGRES_BACKEND_DIR, "catalog", "system_functions.sql"),
        "system_functions.sql",
    ),
    (
        os.path.join(POSTGRES_BACKEND_DIR, "catalog", "system_views.sql"),
        "system_views.sql",
    ),
)


def _postgres_dependencies(include_zlib_project=False):
    deps = [f"{POSTGRES_LIBPQ_DIR}={POSTGRES_MAKE_GOAL}"]
    if include_zlib_project:
        deps.append(f"{ZLIB_DIR}=libz.a")
    return deps


def _collect_postgres_cpp_args(main_path, include_zlib_project=False):
    _ensure_postgres_configured()
    _ensure_postgres_path_config_header()
    return tuple(
        collect_cpp_args(
            main_path,
            dependencies=_postgres_dependencies(
                include_zlib_project=include_zlib_project
            ),
        )
    )


def _collect_postgres_units(main_path, include_zlib_project=False):
    _ensure_postgres_configured()
    _ensure_postgres_path_config_header()
    return collect_translation_units(
        main_path,
        dependencies=_postgres_dependencies(include_zlib_project=include_zlib_project),
    )


def _postgres_cpp_args():
    return _collect_postgres_cpp_args(POSTGRES_TEST_MAIN)


def _postgres_query_cpp_args():
    return _collect_postgres_cpp_args(POSTGRES_QUERY_MAIN)


def _postgres_units():
    return _collect_postgres_units(POSTGRES_TEST_MAIN)


def _postgres_query_units():
    return _collect_postgres_units(POSTGRES_QUERY_MAIN)


def _postgres_project_cpp_args():
    return _collect_postgres_cpp_args(POSTGRES_TEST_MAIN, include_zlib_project=True)


def _postgres_project_units():
    return _collect_postgres_units(POSTGRES_TEST_MAIN, include_zlib_project=True)


def _postgres_link_args(include_native_zlib=True):
    link_args = [
        POSTGRES_COMMON_ARCHIVE,
        POSTGRES_PORT_ARCHIVE,
        "-lm",
    ]
    if include_native_zlib:
        link_args.insert(2, ZLIB_LIB)
    return link_args


def _make_env():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


@contextmanager
def _file_lock(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)


@contextmanager
def _postgres_build_lock():
    with _file_lock(POSTGRES_CONFIG_LOCK):
        yield


def _append_flag_words(existing, extras):
    parts = []
    if existing:
        parts.append(existing)
    parts.extend(extras)
    return " ".join(part for part in parts if part)


def _postgres_expected_config_args():
    env = _postgres_configure_env()
    expected = list(POSTGRES_CONFIGURE_ARGS)
    for key in ("LDFLAGS", "CPPFLAGS"):
        value = env.get(key, "").strip()
        if value:
            expected.append(f"{key}={value}")
    return tuple(expected)


def _current_postgres_config_args():
    if not (
        os.path.isfile(POSTGRES_MAKEFILE_GLOBAL)
        and os.path.isfile(POSTGRES_CONFIG_STATUS)
    ):
        return None
    result = subprocess.run(
        ["./config.status", "--config"],
        cwd=POSTGRES_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        env=_make_env(),
    )
    if result.returncode != 0:
        return None
    return tuple(shlex.split(result.stdout.strip()))


def _postgres_config_matches_current():
    if not os.path.isfile(POSTGRES_MAKEFILE_PORT):
        return False
    current_args = _current_postgres_config_args()
    if current_args is None:
        return False
    return current_args == _postgres_expected_config_args()


def _ensure_readline_include_overlay():
    link_path = os.path.join(READLINE_INCLUDE_ROOT, "readline")
    with _file_lock(READLINE_BUILD_LOCK):
        os.makedirs(READLINE_INCLUDE_ROOT, exist_ok=True)
        if os.path.lexists(link_path):
            if os.path.islink(link_path) and os.readlink(link_path) == READLINE_DIR:
                return READLINE_INCLUDE_ROOT
            os.unlink(link_path)
        os.symlink(READLINE_DIR, link_path)
    return READLINE_INCLUDE_ROOT


def _ensure_readline_built():
    with _file_lock(READLINE_BUILD_LOCK):
        if not os.path.isfile(os.path.join(READLINE_DIR, "Makefile")):
            configure = subprocess.run(
                ["./configure"],
                cwd=READLINE_DIR,
                capture_output=True,
                text=True,
                timeout=600,
                env=_make_env(),
            )
            assert (
                configure.returncode == 0
            ), f"readline configure failed:\n{configure.stdout}\n{configure.stderr}"

        if os.path.isfile(READLINE_LIB) and os.path.isfile(READLINE_HISTORY_LIB):
            return

        build = subprocess.run(
            ["make", "-C", READLINE_DIR, "libreadline.a", "libhistory.a", "-j2"],
            capture_output=True,
            text=True,
            timeout=600,
            env=_make_env(),
        )
        assert (
            build.returncode == 0
        ), f"readline build failed:\n{build.stdout}\n{build.stderr}"


def _ensure_zlib_built():
    with _file_lock(ZLIB_BUILD_LOCK):
        if os.path.isfile(ZLIB_LIB):
            return

        configure = subprocess.run(
            ["./configure", "--static"],
            cwd=ZLIB_DIR,
            capture_output=True,
            text=True,
            timeout=600,
            env=_make_env(),
        )
        assert (
            configure.returncode == 0
        ), f"zlib configure failed:\n{configure.stdout}\n{configure.stderr}"

        build = subprocess.run(
            ["make", "-C", ZLIB_DIR, "libz.a", "-j2"],
            capture_output=True,
            text=True,
            timeout=600,
            env=_make_env(),
        )
        assert (
            build.returncode == 0
        ), f"zlib build failed:\n{build.stdout}\n{build.stderr}"


def _ensure_postgres_dependency_projects():
    _ensure_readline_built()
    _ensure_zlib_built()


def _postgres_configure_env():
    env = _make_env()
    readline_include_root = _ensure_readline_include_overlay()
    env["CPPFLAGS"] = _append_flag_words(
        env.get("CPPFLAGS", ""),
        [
            f"-I{readline_include_root}",
            f"-I{ZLIB_DIR}",
        ],
    )
    env["LDFLAGS"] = _append_flag_words(
        env.get("LDFLAGS", ""),
        [
            f"-L{READLINE_DIR}",
            f"-L{ZLIB_DIR}",
        ],
    )
    return env


def _ensure_postgres_configured():
    _ensure_postgres_dependency_projects()
    if _postgres_config_matches_current():
        return

    with _postgres_build_lock():
        _ensure_postgres_dependency_projects()
        if _postgres_config_matches_current():
            return

        current_args = _current_postgres_config_args()
        if (
            current_args is not None
            and current_args != _postgres_expected_config_args()
        ):
            distclean = subprocess.run(
                ["make", "-C", POSTGRES_DIR, "distclean"],
                capture_output=True,
                text=True,
                timeout=1200,
                env=_make_env(),
            )
            assert (
                distclean.returncode == 0
            ), f"postgres distclean failed:\n{distclean.stdout}\n{distclean.stderr}"

        configure = subprocess.run(
            [
                "./configure",
                *POSTGRES_CONFIGURE_ARGS,
            ],
            cwd=POSTGRES_DIR,
            capture_output=True,
            text=True,
            timeout=1200,
            env=_postgres_configure_env(),
        )
        assert (
            configure.returncode == 0
        ), f"postgres configure failed:\n{configure.stdout}\n{configure.stderr}"


def _ensure_postgres_path_config_header():
    _ensure_postgres_configured()
    if os.path.isfile(POSTGRES_PATHS_HEADER):
        return

    with _postgres_build_lock():
        if os.path.isfile(POSTGRES_PATHS_HEADER):
            return

        build = subprocess.run(
            ["make", "-C", POSTGRES_PORT_DIR, "pg_config_paths.h", "-j2"],
            capture_output=True,
            text=True,
            timeout=180,
            env=_make_env(),
        )
        assert (
            build.returncode == 0
        ), f"postgres path config header build failed:\n{build.stdout}\n{build.stderr}"


def _postgres_runtime_env():
    return _postgres_runtime_env_for()


def _postgres_runtime_env_for(*extra_lib_dirs):
    env = _make_env()
    dyld_entries = [os.path.abspath(path) for path in extra_lib_dirs if path]
    dyld_entries.append(os.path.abspath(POSTGRES_LIBPQ_DIR))
    current = env.get("DYLD_LIBRARY_PATH", "")
    if current:
        dyld_entries.extend(entry for entry in current.split(os.pathsep) if entry)

    deduped = []
    seen = set()
    for entry in dyld_entries:
        if entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)

    env["DYLD_LIBRARY_PATH"] = os.pathsep.join(deduped)
    env["TZ"] = "GMT"
    return env


def _pick_unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _prepare_postgres_runtime_tree(root_dir):
    _ensure_postgres_configured()
    _ensure_postgres_generated_headers()
    with _postgres_build_lock():
        install = subprocess.run(
            ["make", "-C", POSTGRES_DIR, f"DESTDIR={root_dir}", "install", "-j2"],
            capture_output=True,
            text=True,
            timeout=1200,
            env=_make_env(),
        )
    assert (
        install.returncode == 0
    ), f"postgres install failed:\n{install.stdout}\n{install.stderr}"

    install_root = os.path.join(root_dir, "usr", "local", "pgsql")
    return (
        os.path.join(install_root, "bin"),
        os.path.join(install_root, "lib"),
    )


def _ensure_postgres_support_archives():
    _ensure_postgres_configured()
    _ensure_postgres_generated_headers()
    if (
        os.path.isfile(POSTGRES_PORT_ARCHIVE)
        and os.path.isfile(POSTGRES_COMMON_ARCHIVE)
        and os.path.isfile(POSTGRES_PATHS_HEADER)
    ):
        return

    with _postgres_build_lock():
        if (
            os.path.isfile(POSTGRES_PORT_ARCHIVE)
            and os.path.isfile(POSTGRES_COMMON_ARCHIVE)
            and os.path.isfile(POSTGRES_PATHS_HEADER)
        ):
            return

        port_build = subprocess.run(
            [
                "make",
                "-C",
                POSTGRES_PORT_DIR,
                "pg_config_paths.h",
                "libpgport_shlib.a",
                "-j2",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=_make_env(),
        )
        common_build = subprocess.run(
            ["make", "-C", POSTGRES_COMMON_DIR, "libpgcommon_shlib.a", "-j2"],
            capture_output=True,
            text=True,
            timeout=180,
            env=_make_env(),
        )
    assert (
        port_build.returncode == 0
    ), f"postgres port support build failed:\n{port_build.stdout}\n{port_build.stderr}"
    assert (
        common_build.returncode == 0
    ), f"postgres common support build failed:\n{common_build.stdout}\n{common_build.stderr}"


def _ensure_postgres_server_binaries():
    _ensure_postgres_configured()
    _ensure_postgres_generated_headers()
    build_steps = (
        (
            POSTGRES_BKI,
            ["make", "-C", POSTGRES_INCLUDE_CATALOG_DIR, "generated-headers", "-j2"],
            300,
            "postgres catalog generated-headers build failed",
        ),
        (
            POSTGRES_SNOWBALL_SQL,
            ["make", "-C", POSTGRES_SNOWBALL_DIR, "snowball_create.sql", "-j2"],
            300,
            "postgres snowball sql build failed",
        ),
        (
            POSTGRES_ZIC_BIN,
            ["make", "-C", POSTGRES_TIMEZONE_DIR, "zic", "-j2"],
            300,
            "postgres timezone zic build failed",
        ),
        (
            POSTGRES_BACKEND_BIN,
            ["make", "-C", POSTGRES_BACKEND_DIR, "postgres", "-j2"],
            1200,
            "postgres backend build failed",
        ),
        (
            POSTGRES_INITDB_BIN,
            ["make", "-C", POSTGRES_INITDB_DIR, "initdb", "-j2"],
            600,
            "postgres initdb build failed",
        ),
        (
            POSTGRES_PGCTL_BIN,
            ["make", "-C", POSTGRES_PGCTL_DIR, "pg_ctl", "-j2"],
            600,
            "postgres pg_ctl build failed",
        ),
    )

    with _postgres_build_lock():
        for binary_path, cmd, timeout, message in build_steps:
            if os.path.isfile(binary_path):
                continue

            build = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_make_env(),
            )
            assert build.returncode == 0, f"{message}:\n{build.stdout}\n{build.stderr}"

        missing_runtime_files = [
            path
            for path, _name in POSTGRES_RUNTIME_SHARE_FILES
            if not os.path.isfile(path)
        ]
        assert (
            not missing_runtime_files
        ), "postgres runtime share files missing after build:\n" + "\n".join(
            missing_runtime_files
        )


def _ensure_postgres_generated_headers():
    if os.path.isfile(POSTGRES_ERRCODES_HEADER):
        return

    with _postgres_build_lock():
        if os.path.isfile(POSTGRES_ERRCODES_HEADER):
            return

        build = subprocess.run(
            ["make", "-C", POSTGRES_BACKEND_DIR, "generated-headers", "-j2"],
            capture_output=True,
            text=True,
            timeout=600,
            env=_make_env(),
        )
        assert (
            build.returncode == 0
        ), f"postgres generated-headers build failed:\n{build.stdout}\n{build.stderr}"


@pytest.mark.integration
def test_postgres_runtime_with_system_link_depends_on_repo_local_zlib_project():
    _ensure_postgres_support_archives()

    units, base_dir = _postgres_project_units()
    names = {unit.name for unit in units}

    assert "fe-connect.c" in names
    assert "adler32.c" in names
    assert "zutil.c" in names

    result = CEvaluator().run_translation_units_with_system_cc(
        units,
        optimize=True,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_postgres_project_cpp_args(),
        link_args=_postgres_link_args(include_native_zlib=False),
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"postgres system-link runtime with repo-local zlib project failed:\n{result.stdout}\n{result.stderr}"
    assert "libpq version 170004" in result.stdout
    assert "conninfo: host=1 port=1 dbname=1" in result.stdout
    assert "OK" in result.stdout


@pytest.mark.integration
def test_postgres_runtime_with_self_backend_system_link_depends_on_repo_local_zlib_project():
    _ensure_postgres_support_archives()

    units, base_dir = _postgres_project_units()

    result = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).run_translation_units_with_system_cc(
        units,
        optimize=True,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_postgres_project_cpp_args(),
        link_args=_postgres_link_args(include_native_zlib=False),
        timeout=180,
    )

    assert result.returncode == 0, (
        "postgres self-backend system-link runtime with repo-local zlib "
        f"project failed:\n{result.stdout}\n{result.stderr}"
    )
    assert "libpq version 170004" in result.stdout
    assert "conninfo: host=1 port=1 dbname=1" in result.stdout
    assert "OK" in result.stdout


@pytest.mark.integration
def test_postgres_cli_system_link_depends_on():
    _ensure_postgres_support_archives()

    result = run_cli(
        [
            "--system-link",
            "--jobs",
            "2",
            "--depends-on",
            f"{POSTGRES_LIBPQ_DIR}={POSTGRES_MAKE_GOAL}",
            f"--link-arg={ZLIB_LIB}",
            f"--link-arg={POSTGRES_COMMON_ARCHIVE}",
            f"--link-arg={POSTGRES_PORT_ARCHIVE}",
            "--link-arg=-lm",
            POSTGRES_TEST_MAIN,
        ],
        timeout=180,
    )

    assert result.returncode == 0, (result.stdout + result.stderr)
    assert "libpq version 170004" in (result.stdout + result.stderr)
    assert "conninfo: host=1 port=1 dbname=1" in (result.stdout + result.stderr)
    assert "OK" in (result.stdout + result.stderr)


@pytest.mark.integration
def test_postgres_cli_self_backend_system_link_depends_on():
    _ensure_postgres_support_archives()

    result = run_cli(
        [
            "--backend=self",
            "--system-link",
            "--jobs",
            "2",
            "--depends-on",
            f"{POSTGRES_LIBPQ_DIR}={POSTGRES_MAKE_GOAL}",
            f"--link-arg={ZLIB_LIB}",
            f"--link-arg={POSTGRES_COMMON_ARCHIVE}",
            f"--link-arg={POSTGRES_PORT_ARCHIVE}",
            "--link-arg=-lm",
            POSTGRES_TEST_MAIN,
        ],
        timeout=180,
    )

    assert result.returncode == 0, (result.stdout + result.stderr)
    assert "libpq version 170004" in (result.stdout + result.stderr)
    assert "conninfo: host=1 port=1 dbname=1" in (result.stdout + result.stderr)
    assert "OK" in (result.stdout + result.stderr)


@pytest.mark.integration
def test_postgres_runtime_query_against_native_server(tmp_path):
    _ensure_postgres_support_archives()
    _ensure_postgres_server_binaries()

    query_units, base_dir = _postgres_query_units()
    query_include_dirs = translation_unit_include_dirs(query_units)
    query_cpp_args = _postgres_query_cpp_args()

    runtime_root = tmp_path / "runtime"
    data_dir = tmp_path / "data"
    log_path = tmp_path / "postgres.log"

    bin_dir, lib_dir = _prepare_postgres_runtime_tree(str(runtime_root))
    env = _postgres_runtime_env_for(lib_dir)
    port = _pick_unused_port()

    initdb = subprocess.run(
        [
            os.path.join(bin_dir, "initdb"),
            "-D",
            str(data_dir),
            "-U",
            "postgres",
            "-A",
            "trust",
            "--no-sync",
            "--no-locale",
            "-E",
            "UTF8",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert (
        initdb.returncode == 0
    ), f"postgres initdb failed:\n{initdb.stdout}\n{initdb.stderr}"

    started = False
    try:
        start = subprocess.run(
            [
                os.path.join(bin_dir, "pg_ctl"),
                "-D",
                str(data_dir),
                "-l",
                str(log_path),
                "-o",
                f"-F -p {port}",
                "-w",
                "start",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert start.returncode == 0, (
            "postgres server start failed:\n"
            f"{start.stdout}\n{start.stderr}\n"
            f"{log_path.read_text(encoding="utf-8") if log_path.exists() else ''}"
        )
        started = True

        result = CEvaluator().run_translation_units_with_system_cc(
            query_units,
            optimize=True,
            base_dir=base_dir,
            jobs=translation_unit_jobs(),
            include_dirs=query_include_dirs,
            cpp_args=query_cpp_args,
            link_args=_postgres_link_args(),
            prog_args=[f"host=127.0.0.1 port={port} dbname=postgres user=postgres"],
            timeout=180,
        )

        assert (
            result.returncode == 0
        ), f"postgres pcc client query failed:\n{result.stdout}\n{result.stderr}"
        assert "server_version_num=170004" in result.stdout
        assert "alpha_score=17" in result.stdout
        assert "sum=52" in result.stdout
        assert "temp_rows=0" in result.stdout
        assert "OK" in result.stdout
    finally:
        if started:
            subprocess.run(
                [
                    os.path.join(bin_dir, "pg_ctl"),
                    "-D",
                    str(data_dir),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )


@pytest.mark.integration
def test_postgres_self_backend_runtime_query_against_native_server(tmp_path):
    _ensure_postgres_support_archives()
    _ensure_postgres_server_binaries()

    query_units, base_dir = _postgres_query_units()
    query_include_dirs = translation_unit_include_dirs(query_units)
    query_cpp_args = _postgres_query_cpp_args()

    runtime_root = tmp_path / "runtime"
    data_dir = tmp_path / "data"
    log_path = tmp_path / "postgres.log"

    bin_dir, lib_dir = _prepare_postgres_runtime_tree(str(runtime_root))
    env = _postgres_runtime_env_for(lib_dir)
    port = _pick_unused_port()

    initdb = subprocess.run(
        [
            os.path.join(bin_dir, "initdb"),
            "-D",
            str(data_dir),
            "-U",
            "postgres",
            "-A",
            "trust",
            "--no-sync",
            "--no-locale",
            "-E",
            "UTF8",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert (
        initdb.returncode == 0
    ), f"postgres initdb failed:\n{initdb.stdout}\n{initdb.stderr}"

    started = False
    try:
        start = subprocess.run(
            [
                os.path.join(bin_dir, "pg_ctl"),
                "-D",
                str(data_dir),
                "-l",
                str(log_path),
                "-o",
                f"-F -p {port}",
                "-w",
                "start",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert start.returncode == 0, (
            "postgres server start failed:\n"
            f"{start.stdout}\n{start.stderr}\n"
            f"{log_path.read_text(encoding="utf-8") if log_path.exists() else ''}"
        )
        started = True

        result = CEvaluator(
            backend="self",
            allow_unimplemented_backend=True,
        ).run_translation_units_with_system_cc(
            query_units,
            optimize=True,
            base_dir=base_dir,
            jobs=translation_unit_jobs(),
            include_dirs=query_include_dirs,
            cpp_args=query_cpp_args,
            link_args=_postgres_link_args(),
            prog_args=[f"host=127.0.0.1 port={port} dbname=postgres user=postgres"],
            timeout=180,
        )

        assert (
            result.returncode == 0
        ), f"postgres self-backend query failed:\n{result.stdout}\n{result.stderr}"
        assert "server_version_num=170004" in result.stdout
        assert "alpha_score=17" in result.stdout
        assert "sum=52" in result.stdout
        assert "temp_rows=0" in result.stdout
        assert "OK" in result.stdout
    finally:
        if started:
            subprocess.run(
                [
                    os.path.join(bin_dir, "pg_ctl"),
                    "-D",
                    str(data_dir),
                    "-m",
                    "fast",
                    "-w",
                    "stop",
                ],
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )


@pytest.mark.integration
def test_postgres_runtime_with_system_link_depends_on():
    _ensure_postgres_support_archives()

    units, base_dir = _postgres_units()

    result = CEvaluator().run_translation_units_with_system_cc(
        units,
        optimize=True,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_postgres_cpp_args(),
        link_args=_postgres_link_args(),
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"postgres system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "libpq version 170004" in result.stdout
    assert "conninfo: host=1 port=1 dbname=1" in result.stdout
    assert "OK" in result.stdout


@pytest.mark.integration
def test_postgres_runtime_with_self_backend_system_link_depends_on():
    _ensure_postgres_support_archives()

    units, base_dir = _postgres_units()

    result = CEvaluator(
        backend="self",
        allow_unimplemented_backend=True,
    ).run_translation_units_with_system_cc(
        units,
        optimize=True,
        base_dir=base_dir,
        jobs=translation_unit_jobs(),
        include_dirs=translation_unit_include_dirs(units),
        cpp_args=_postgres_cpp_args(),
        link_args=_postgres_link_args(),
        timeout=180,
    )

    assert (
        result.returncode == 0
    ), f"postgres self-backend system-link runtime failed:\n{result.stdout}\n{result.stderr}"
    assert "libpq version 170004" in result.stdout
    assert "conninfo: host=1 port=1 dbname=1" in result.stdout
    assert "OK" in result.stdout


def test_postgres_make_goal_dependency_collects_libpq_sources():
    units, base_dir = _postgres_units()

    names = [unit.name for unit in units]

    assert base_dir == os.path.abspath(PROJECTS_DIR)
    assert names[-1] == "test_postgres_main.c"
    assert "fe-connect.c" in names
    assert "fe-exec.c" in names
    assert "fe-misc.c" in names
    assert "pqexpbuffer.c" in names
    assert "common/encnames.c" not in names
    assert "port/path.c" not in names


def test_postgres_make_goal_cpp_args_ignore_recursive_support_library_flags():
    cpp_args = _postgres_cpp_args()

    assert "-D_REENTRANT" in cpp_args
    assert "-D_THREAD_SAFE" in cpp_args
    assert "-DSO_MAJOR_VERSION=5" in cpp_args
    assert "-DUSE_PRIVATE_ENCODING_FUNCS" not in cpp_args
    assert os.path.join(POSTGRES_SRC_DIR, "include") in cpp_args
    assert os.path.join(POSTGRES_SRC_DIR, "port") in cpp_args
    assert os.path.join(POSTGRES_DIR, "src", "src", "include") not in cpp_args
    assert os.path.join(POSTGRES_DIR, "src", "src", "port") not in cpp_args
