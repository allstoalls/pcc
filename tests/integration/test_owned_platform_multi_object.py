"""Native C multi-unit .o production, native owned linking, then execution.

PCC_PLATFORM_TEST_RUNTIME names the matching owned runtime archive. The
linker driver itself is compiled by the supplied pcc1; host Python only
orchestrates commands and reads their artifacts.
"""

import os
from pathlib import Path

import pytest

from tests.integration.test_owned_platform_native import compile_run, native_compiler

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("gc", ["0", "1", "2", "3", "4"])
def test_native_multi_unit_object_link_and_run(native_compiler, tmp_path, gc):
    archive = os.environ.get("PCC_PLATFORM_TEST_RUNTIME", os.environ.get("PCC_RUNTIME_ARCHIVE", ""))
    if not archive or not Path(archive).is_file():
        pytest.fail("set PCC_PLATFORM_TEST_RUNTIME to the matching owned runtime archive")
    archive = str(Path(archive).resolve())
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "one.c").write_text("""
static long long helper(void) { return 17; }
long long first(void) { return helper(); }
""", encoding="utf-8")
    (sources / "two.c").write_text("""
extern long long first(void);
static long long helper(void) { return 25; }
static _Thread_local long long tls_value = 9;
static long long untouched[16];
int main(void) {
    if (tls_value != 9 || untouched[15] != 0) return 31;
    tls_value = 10;
    return first() + helper() == 42 && tls_value == 10 ? 0 : 32;
}
""", encoding="utf-8")
    _compile_and_link_sources(native_compiler, tmp_path, gc, sources, archive)


def _compile_and_link_sources(native_compiler, tmp_path, gc, sources, archive, threads=False):
    from scripts.bootstrap_platform import dependency_receipt
    from scripts.platform_process_watchdog import run
    from pcc.frontends.python.pipeline_targets import host_target_triple

    output = tmp_path / "combined.o"
    environment = os.environ.copy()
    environment.pop("LC_ALL", None)
    environment.update({"PCC_GC_BACKEND": gc, "PCC_SELF_LINK": "pcc", "PCC_SELF_OBJ": "pcc",
                        "PCC_RUNTIME_ARCHIVE": archive,
                        "PCC_HOST_PYTHON": str(tmp_path / "forbidden-python"),
                        "CC": str(tmp_path / "forbidden-cc")})
    environment["PCC_WITH_THREADS"] = "1" if threads else "0"
    run([str(native_compiler), "--backend", "self", "--separate-tus", "--no-cache", "-O0",
         str(sources), "--emit-obj", str(output)], cwd=tmp_path, env=environment,
        log_path=tmp_path / "object.log", timeout=600, rss_limit=8589934592, native=True)
    executable = tmp_path / ("result.exe" if os.name == "nt" else "result")
    compile_run(native_compiler, tmp_path / "linker", """
        from pcc.backend.owned_link_driver import main
        def link():
            main()
        link()
    """, gc=gc, argv=["--target", host_target_triple(), "--object", str(output),
                     "--archive", archive, "--out", str(executable)],
        extra_env={"PCC_RUNTIME_ARCHIVE": archive, "PCC_WITH_THREADS": "1" if threads else "0"})
    dependency_receipt(executable)
    log = tmp_path / "execute.log"
    run([str(executable)], cwd=tmp_path, env=environment, log_path=log,
        timeout=30, rss_limit=8589934592, native=True)
    assert log.read_bytes() == b""


@pytest.mark.parametrize("gc", ["0", "1", "2", "3", "4"])
def test_native_external_tls_across_units_and_threads(native_compiler, tmp_path, gc):
    archive = os.environ.get("PCC_PLATFORM_THREADED_RUNTIME", "")
    if not archive or not Path(archive).is_file():
        pytest.fail("set PCC_PLATFORM_THREADED_RUNTIME to the matching owned threaded archive")
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "definition.c").write_text(r'''
_Thread_local long long shared_tls = 37;
_Thread_local long long zero_tls;
long long *shared_address(void) { return &shared_tls; }
long long *zero_address(void) { return &zero_tls; }
long long set_shared(long long value) { shared_tls = value; return shared_tls; }
''', encoding="utf-8")
    (sources / "consumer.c").write_text(r'''
#include <pthread.h>
extern _Thread_local long long shared_tls;
extern _Thread_local long long zero_tls;
extern long long *shared_address(void);
extern long long *zero_address(void);
extern long long set_shared(long long value);
static long long *main_address;
static void *worker(void *arg) {
    if (&shared_tls != shared_address() || &zero_tls != zero_address()) return (void *)1;
    if (&shared_tls == main_address || shared_tls != 37 || zero_tls != 0) return (void *)2;
    zero_tls = 13;
    if (set_shared(42) != 42 || shared_tls != 42) return (void *)3;
    return (void *)(shared_tls + zero_tls);
}
int main(void) {
    pthread_t thread;
    void *result = 0;
    if (shared_tls != 37 || zero_tls != 0) return 4;
    main_address = &shared_tls;
    shared_tls = 17;
    zero_tls = 19;
    if (pthread_create(&thread, 0, worker, 0)) return 5;
    if (pthread_join(thread, &result) || (long long)result != 55) return 6;
    if (shared_tls != 17 || zero_tls != 19) return 7;
    return shared_address() == &shared_tls && zero_address() == &zero_tls ? 0 : 8;
}
''', encoding="utf-8")
    _compile_and_link_sources(native_compiler, tmp_path, gc, sources,
                              str(Path(archive).resolve()), threads=True)
