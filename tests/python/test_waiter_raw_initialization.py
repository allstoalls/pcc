"""Waiter initialization must not consume uninitialized storage as an owner.

The external C harness calls the Python port's raw helper on deterministic
poisoned storage; public waiter reuse/cleanup is covered by the C/Python pool gate.
"""
import os
from pathlib import Path
import subprocess

SOURCE = r'''
#include "py_runtime.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
extern void user_py_threading__waiter_clear(void *node);
extern int64_t pcc_gc_unmanaged_refcount_ops;
extern int64_t pcc_refcount_load(void *);
int main(int argc, char **argv) {
    if(argc != 2 || pcc_gc_set_backend(atoi(argv[1])) != 0) return 2;
    uint64_t node[4]={UINT64_C(0xa5a5a5a5a5a5a5a4),1,2,3};
    int64_t before=pcc_gc_unmanaged_refcount_ops;
    user_py_threading__waiter_clear(node);
    if(node[0] || node[1] || node[2] || node[3]) return 3;
    PyObject *keeper=py_list_new(0);
    py_incref(keeper);
    int64_t references=pcc_refcount_load(keeper);
    node[0]=(uintptr_t)keeper; /* Raw bits do not confer an owning reference. */
    user_py_threading__waiter_clear(node);
    if(pcc_refcount_load(keeper)!=references) return 5;
    py_decref(keeper); py_decref(keeper);
    printf("unmanaged_delta=%lld\n",(long long)(pcc_gc_unmanaged_refcount_ops-before));
    return pcc_gc_unmanaged_refcount_ops==before ? 0 : 4;
}
'''

def test_waiter_clear_does_not_release_raw_storage(tmp_path: Path, pcc_py_runtime_archive):
    source=tmp_path/"waiter_poison.c"
    source.write_text(SOURCE)
    binary=tmp_path/"waiter_poison"
    archive=Path(pcc_py_runtime_archive)
    built=subprocess.run([os.environ.get("CC","cc"),"-I"+str(archive.parent/"include"),str(source),str(archive),"-pthread","-o",str(binary)],capture_output=True,text=True,timeout=30)
    assert built.returncode==0,built.stdout+built.stderr
    for backend in range(5):
        ran=subprocess.run([str(binary),str(backend)],capture_output=True,text=True,timeout=10)
        assert ran.returncode==0,f"GC{backend}: "+ran.stdout+ran.stderr
        assert ran.stdout.strip()=="unmanaged_delta=0"
