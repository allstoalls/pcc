/* pcc/runtime/src/py_libpython.c
 *
 * Phase 4 CPython C-API fallback shim.
 *
 * Strategy: compiled code that imports arbitrary third-party packages
 * (numpy, pandas, requests, ...) trampolines through libpython's
 * ``PyImport_ImportModule`` / ``PyObject_CallObject`` / ``PyObject_GetAttr``
 * instead of our own runtime. The wrappers in this file hide the
 * lifecycle details (Py_Initialize, GIL) so the pcc-emitted IR only
 * needs to see a small, stable set of symbols.
 *
 * Design decisions:
 *
 *   - The CPython ``PyObject *`` type is DISTINCT from pcc's own
 *     ``PyObject *`` (the small-tagged-int + user-class layout defined
 *     in ``py_internal.h``). We expose the CPython type to codegen as
 *     opaque ``void *``; the two pointer namespaces never alias.
 *
 *   - Py_Initialize is called lazily on first import. The embedded
 *     interpreter intentionally remains alive until process teardown:
 *     bridge-owned references and release hooks may still be reachable from
 *     pcc worker threads during C atexit handlers, so calling Py_Finalize
 *     there would create an unsafe finalization race.
 *
 *   - Initialization releases the GIL after publishing a fully-resolved
 *     bridge. Every public bridge entry reacquires it with the real
 *     libpython PyGILState API, so pcc worker threads never enter CPython
 *     with a missing thread state.
 */

#include "py_runtime.h"
#include "py_internal.h"
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern int py_runtime_program_argc;
extern const char **py_runtime_program_argv;
extern void (*py_runtime_program_args_hook)(void);
extern int (*py_format_cpy_object_hook)(int fd, void *obj);

#ifdef PCC_WITH_LIBPYTHON
#include <dlfcn.h>
#include <pthread.h>

/* Forward declarations from libpython. We intentionally do NOT
 * ``#include <Python.h>`` here because the runtime build deliberately
 * avoids depending on CPython headers when the libpython fallback is
 * disabled at build time (see Makefile). */
typedef struct _object CPyObject;

static void *g_libpython_handle = NULL;

/* A pcc-native extension expects pcc's PyObject layout, while libpython mode
 * reserves extension Py* bindings for CPython's layout.  Reject this mixed ABI
 * before dlopen/PyInit so an opaque CPython pointer can never reach pcc GC or
 * refcount operations. */
static PyObject *reject_pcc_native_extension_in_libpython_mode(void) {
    PyObject *exc = py_exc_new(
        PY_EXC_RUNTIMEERROR,
        "pcc-native extension imports cannot be combined with libpython mode"
    );
    py_raise(exc);
    return NULL;
}

PyObject *py_native_extension_import(
    const char *module_name,
    const char *path
) {
    (void)module_name;
    (void)path;
    return reject_pcc_native_extension_in_libpython_mode();
}

PyObject *py_native_extension_import_by_name(const char *module_name) {
    (void)module_name;
    return reject_pcc_native_extension_in_libpython_mode();
}

static CPyObject *(*p_PyImport_ImportModule)(const char *name);
static void (*p_Py_Initialize)(void);
static int (*p_Py_IsInitialized)(void);
typedef int PccPyGILState;
static PccPyGILState (*p_PyGILState_Ensure)(void);
static void (*p_PyGILState_Release)(PccPyGILState state);
static void *(*p_PyEval_SaveThread)(void);
static void (*p_PyEval_RestoreThread)(void *thread_state);
static CPyObject *(*p_PyObject_GetAttrString)(CPyObject *o, const char *attr);
static int (*p_PyObject_SetAttrString)(CPyObject *o, const char *attr, CPyObject *v);
static CPyObject *(*p_PyObject_CallNoArgs)(CPyObject *callable);
static CPyObject *(*p_PyObject_CallOneArg)(CPyObject *callable, CPyObject *arg);
static CPyObject *(*p_PyObject_CallFunctionObjArgs)(CPyObject *callable, ...);
static CPyObject *(*p_PyObject_Call)(CPyObject *callable, CPyObject *args, CPyObject *kwargs);
static CPyObject *(*p_PyImport_AddModule)(const char *name);
static CPyObject *(*p_PyModule_GetDict)(CPyObject *module);
static CPyObject *(*p_PyRun_StringFlags)(
    const char *source,
    int start,
    CPyObject *globals,
    CPyObject *locals,
    void *flags
);
static CPyObject *(*p_PyTuple_New)(long size);
static int (*p_PyTuple_SetItem)(CPyObject *tup, long index, CPyObject *item);
static CPyObject *(*p_PyList_New)(long size);
static int (*p_PyList_SetItem)(CPyObject *lst, long i, CPyObject *item);
static long (*p_PyObject_Length)(CPyObject *o);
static CPyObject *(*p_PyObject_GetItem)(CPyObject *o, CPyObject *key);
static int (*p_PyObject_SetItem)(CPyObject *o, CPyObject *key, CPyObject *value);
static int (*p_PyObject_IsTrue)(CPyObject *o);
static CPyObject *(*p_PyObject_GetIter)(CPyObject *o);
static CPyObject *(*p_PyIter_Next)(CPyObject *it);
static CPyObject *(*p_PyObject_Str)(CPyObject *o);
static int (*p_PyObject_IsInstance)(CPyObject *inst, CPyObject *cls);
static CPyObject *(*p_PyNumber_Index)(CPyObject *o);
static CPyObject *(*p_PyNumber_Long)(CPyObject *o);
static CPyObject *(*p_PyNumber_Float)(CPyObject *o);
static CPyObject *(*p_PyNumber_Add)(CPyObject *a, CPyObject *b);
static CPyObject *(*p_PyNumber_Subtract)(CPyObject *a, CPyObject *b);
static CPyObject *(*p_PyNumber_Multiply)(CPyObject *a, CPyObject *b);
static CPyObject *(*p_PyNumber_TrueDivide)(CPyObject *a, CPyObject *b);
static CPyObject *(*p_PyNumber_FloorDivide)(CPyObject *a, CPyObject *b);
static CPyObject *(*p_PyNumber_Remainder)(CPyObject *a, CPyObject *b);
/* PyNumber_Power is ternary: (base, exp, modulus); plain ``a ** b`` passes
 * Py_None (&_Py_NoneStruct) as the modulus. */
static CPyObject *(*p_PyNumber_Power)(CPyObject *a, CPyObject *b, CPyObject *c);
static CPyObject *(*p_PyNumber_MatrixMultiply)(CPyObject *a, CPyObject *b);
static CPyObject *(*p_PyErr_Occurred)(void);
static void (*p_PyErr_Fetch)(CPyObject **ptype, CPyObject **pvalue, CPyObject **ptraceback);
static void (*p_PyErr_NormalizeException)(CPyObject **ptype, CPyObject **pvalue, CPyObject **ptraceback);
static void (*p_PyErr_Restore)(CPyObject *type, CPyObject *value, CPyObject *traceback);
static void (*p_PyErr_Clear)(void);
static void (*p_PyErr_Print)(void);
static void (*p_PyErr_SetString)(CPyObject *type, const char *message);
static int (*p_PyErr_GivenExceptionMatches)(CPyObject *given, CPyObject *exc);
static CPyObject *(*p_PyLong_FromLongLong)(long long value);
static CPyObject *(*p_PyLong_FromString)(char *value, char **end, int base);
static long long (*p_PyLong_AsLongLong)(CPyObject *o);
static CPyObject *(*p_PyFloat_FromDouble)(double value);
static double (*p_PyFloat_AsDouble)(CPyObject *o);
static CPyObject *(*p_PyUnicode_FromStringAndSize)(const char *u, long len);
static const char *(*p_PyUnicode_AsUTF8)(CPyObject *unicode);
static const char *(*p_PyUnicode_AsUTF8AndSize)(CPyObject *unicode, long *size);
static long (*p_PyList_Size)(CPyObject *lst);
static CPyObject *(*p_PyList_GetItem)(CPyObject *lst, long i);
static long (*p_PyTuple_Size)(CPyObject *tup);
static CPyObject *(*p_PyTuple_GetItem)(CPyObject *tup, long i);
static int (*p_PyDict_Next)(CPyObject *d, long *pos, CPyObject **key, CPyObject **value);
static void (*p_Py_DecRef)(CPyObject *o);
static void (*p_Py_IncRef)(CPyObject *o);

static CPyObject *p__Py_NoneStruct = NULL;
static CPyObject *p_PyBool_Type = NULL;
static CPyObject *p_PyLong_Type = NULL;
static CPyObject *p_PyFloat_Type = NULL;
static CPyObject *p_PyUnicode_Type = NULL;
static CPyObject *p_PyList_Type = NULL;
static CPyObject *p_PyTuple_Type = NULL;
static CPyObject *p_PyDict_Type = NULL;
static CPyObject *p_PySet_Type = NULL;

static CPyObject *(*p_PyCapsule_New)(void *pointer, const char *name, void *destructor);
static void *(*p_PyCapsule_GetPointer)(CPyObject *capsule, const char *name);
typedef struct _pcc_PyMethodDef {
    const char *ml_name;
    void *ml_meth;           /* CPyObject *(*)(CPyObject *, CPyObject *) */
    int ml_flags;
    const char *ml_doc;
} PccPyMethodDef;
static CPyObject *(*p_PyCFunction_NewEx)(PccPyMethodDef *ml, CPyObject *self, CPyObject *module);
static int (*p_PyArg_UnpackTuple)(CPyObject *args, const char *name, long min, long max, ...);
static CPyObject **p_PyExc_SystemExit = NULL;
static CPyObject **p_PyExc_RuntimeError = NULL;
static CPyObject **p_PyExc_TypeError = NULL;

static CPyObject *(*p_PyBool_FromLong)(long v);
static CPyObject *(*p_PyBytes_FromStringAndSize)(const char *v, long len);
static CPyObject *(*p_PyDict_Copy)(CPyObject *d);
static CPyObject *(*p_PyDict_New)(void);
static int (*p_PyDict_SetItem)(CPyObject *d, CPyObject *k, CPyObject *v);
static int (*p_PyDict_SetItemString)(CPyObject *dp, const char *key, CPyObject *item);
static int (*p_PyDict_Contains)(CPyObject *d, CPyObject *key);
static CPyObject *(*p_PySequence_Tuple)(CPyObject *v);
static int (*p_PySet_Add)(CPyObject *s, CPyObject *item);
static CPyObject *(*p_PySet_New)(CPyObject *iterable);

#define Py_Initialize p_Py_Initialize
#define Py_IsInitialized p_Py_IsInitialized
#define PyImport_ImportModule p_PyImport_ImportModule
#define PyObject_GetAttrString p_PyObject_GetAttrString
#define PyObject_SetAttrString p_PyObject_SetAttrString
#define PyObject_CallNoArgs p_PyObject_CallNoArgs
#define PyObject_CallOneArg p_PyObject_CallOneArg
#define PyObject_CallFunctionObjArgs p_PyObject_CallFunctionObjArgs
#define PyObject_Call p_PyObject_Call
#define PyImport_AddModule p_PyImport_AddModule
#define PyModule_GetDict p_PyModule_GetDict
#define PyRun_StringFlags p_PyRun_StringFlags
#define PyTuple_New p_PyTuple_New
#define PyTuple_SetItem p_PyTuple_SetItem
#define PyList_New p_PyList_New
#define PyList_SetItem p_PyList_SetItem
#define PyObject_Length p_PyObject_Length
#define PyObject_GetItem p_PyObject_GetItem
#define PyObject_SetItem p_PyObject_SetItem
#define PyObject_IsTrue p_PyObject_IsTrue
#define PyObject_GetIter p_PyObject_GetIter
#define PyIter_Next p_PyIter_Next
#define PyObject_Str p_PyObject_Str
#define PyObject_IsInstance p_PyObject_IsInstance
#define PyNumber_Index p_PyNumber_Index
#define PyNumber_Long p_PyNumber_Long
#define PyNumber_Float p_PyNumber_Float
#define PyNumber_Add p_PyNumber_Add
#define PyNumber_Subtract p_PyNumber_Subtract
#define PyNumber_Multiply p_PyNumber_Multiply
#define PyNumber_TrueDivide p_PyNumber_TrueDivide
#define PyNumber_FloorDivide p_PyNumber_FloorDivide
#define PyNumber_Remainder p_PyNumber_Remainder
#define PyNumber_Power p_PyNumber_Power
#define PyNumber_MatrixMultiply p_PyNumber_MatrixMultiply
#define PyErr_Occurred p_PyErr_Occurred
#define PyErr_Fetch p_PyErr_Fetch
#define PyErr_NormalizeException p_PyErr_NormalizeException
#define PyErr_Restore p_PyErr_Restore
#define PyErr_Clear p_PyErr_Clear
#define PyErr_Print p_PyErr_Print
#define PyErr_SetString p_PyErr_SetString
#define PyErr_GivenExceptionMatches p_PyErr_GivenExceptionMatches
#define PyLong_FromLongLong p_PyLong_FromLongLong
#define PyLong_FromString p_PyLong_FromString
#define PyLong_AsLongLong p_PyLong_AsLongLong
#define PyFloat_FromDouble p_PyFloat_FromDouble
#define PyFloat_AsDouble p_PyFloat_AsDouble
#define PyUnicode_FromStringAndSize p_PyUnicode_FromStringAndSize
#define PyUnicode_AsUTF8 p_PyUnicode_AsUTF8
#define PyUnicode_AsUTF8AndSize p_PyUnicode_AsUTF8AndSize
#define PyList_Size p_PyList_Size
#define PyList_GetItem p_PyList_GetItem
#define PyTuple_Size p_PyTuple_Size
#define PyTuple_GetItem p_PyTuple_GetItem
#define PyDict_Next p_PyDict_Next
#define Py_DecRef p_Py_DecRef
#define Py_IncRef p_Py_IncRef
#define PyCapsule_New p_PyCapsule_New
#define PyCapsule_GetPointer p_PyCapsule_GetPointer
#define PyCFunction_NewEx p_PyCFunction_NewEx
#define PyArg_UnpackTuple p_PyArg_UnpackTuple
#define PyBool_FromLong p_PyBool_FromLong
#define PyBytes_FromStringAndSize p_PyBytes_FromStringAndSize
#define PyDict_Copy p_PyDict_Copy
#define PyDict_New p_PyDict_New
#define PyDict_SetItem p_PyDict_SetItem
#define PyDict_SetItemString p_PyDict_SetItemString
#define PyDict_Contains p_PyDict_Contains
#define PySequence_Tuple p_PySequence_Tuple
#define PySet_Add p_PySet_Add
#define PySet_New p_PySet_New
#define _Py_NoneStruct (*p__Py_NoneStruct)
#define PyBool_Type (*p_PyBool_Type)
#define PyLong_Type (*p_PyLong_Type)
#define PyFloat_Type (*p_PyFloat_Type)
#define PyUnicode_Type (*p_PyUnicode_Type)
#define PyList_Type (*p_PyList_Type)
#define PyTuple_Type (*p_PyTuple_Type)
#define PyDict_Type (*p_PyDict_Type)
#define PySet_Type (*p_PySet_Type)
#define PyExc_SystemExit (*p_PyExc_SystemExit)
#define PyExc_RuntimeError (*p_PyExc_RuntimeError)
#define PyExc_TypeError (*p_PyExc_TypeError)

/* Symbol Resolution Model for CPython Fallback Bridge
 * ===================================================
 *
 * Motivation:
 * When pcc-native programs link against libpy_runtime_pcc_py_libpython.a, both
 * pcc-Python py_capi_*.o objects (defining the pcc-native C-API surface) and
 * py_libpython.o (wrapping real CPython fallback calls) are linked.
 *
 * Because static archive symbols take precedence over dynamically linked symbols
 * on macOS, standard extern calls (e.g., PyErr_Occurred, PyImport_ImportModule)
 * in py_libpython.c would resolve to the local pcc-native stubs in py_capi_shim.c
 * rather than CPython's real implementations. This would corrupt data layouts and
 * trigger segfaults.
 *
 * Solution:
 * We dynamically load CPython C-API symbols at runtime using dlsym.
 * 1. Find the real Py_Initialize function address via RTLD_DEFAULT (since it
 *    does not exist in our stubs, it correctly resolves to CPython's dylib).
 * 2. Query its image path using dladdr.
 * 3. dlopen the CPython dylib specifically using that path.
 * 4. Resolve all needed CPython symbols specifically from that handle using dlsym.
 * 5. Define macro redirects (e.g. #define PyImport_ImportModule p_PyImport_ImportModule)
 *    so the main bridge code reads naturally while invoking the resolved pointers.
 *
 * Types and Exceptions:
 * Every CPython type/singleton/exception object used here is resolved from the
 * specific libpython handle.  The pcc runtime owns same-named public symbols
 * with a different object layout, so static archive resolution is never safe
 * for data objects either.
 *
 * Lifecycle & Handle Lifetime:
 * g_libpython_handle is opened on first import and intentionally remains open
 * for the lifetime of the process, ensuring resolved function pointers remain
 * valid.
 *
 * Thread-Safety:
 * py_cpy_resolve_symbols() is called exclusively by the thread that changes
 * g_init_state from UNINITIALIZED to INITIALIZING. READY is release-published
 * only after symbol resolution, interpreter setup, and initial GIL release.
 *
 * Failure Recovery:
 * If any symbol fails to resolve, we print a fatal error to stderr and abort()
 * immediately. This fails loudly and prevents downstream undefined behavior or
 * segfaults.
 */

static void py_cpy_resolve_symbols(void) {
    if (g_libpython_handle != NULL) return;

    /* Get the address of Py_Initialize using RTLD_DEFAULT.
     * Since Py_Initialize is not defined in the executable, RTLD_DEFAULT resolves
     * it to the real libpython dynamic library. */
    void *(*fn_Py_Initialize)(void) = (void *(*)(void))dlsym(RTLD_DEFAULT, "Py_Initialize");
    if (!fn_Py_Initialize) {
        fprintf(stderr, "pcc runtime error: could not locate Py_Initialize via RTLD_DEFAULT\n");
        abort();
    }

    /* Query the image/filename of Py_Initialize to find the real CPython library. */
    Dl_info info;
    if (dladdr((void *)fn_Py_Initialize, &info) == 0 || !info.dli_fname) {
        fprintf(stderr, "pcc runtime error: dladdr failed for Py_Initialize\n");
        abort();
    }

    /* Open the CPython dynamic library specifically to bypass local stubs. */
    g_libpython_handle = dlopen(info.dli_fname, RTLD_LAZY | RTLD_LOCAL);
    if (!g_libpython_handle) {
        fprintf(stderr, "pcc runtime error: dlopen failed for %s: %s\n", info.dli_fname, dlerror());
        abort();
    }

#define RESOLVE(name) \
    p_##name = dlsym(g_libpython_handle, #name); \
    if (!p_##name) { \
        fprintf(stderr, "pcc runtime error: failed to resolve symbol %s\n", #name); \
        abort(); \
    }

    RESOLVE(Py_Initialize);
    RESOLVE(Py_IsInitialized);
    RESOLVE(PyGILState_Ensure);
    RESOLVE(PyGILState_Release);
    RESOLVE(PyEval_SaveThread);
    RESOLVE(PyEval_RestoreThread);
    RESOLVE(PyImport_ImportModule);
    RESOLVE(PyObject_GetAttrString);
    RESOLVE(PyObject_SetAttrString);
    RESOLVE(PyObject_CallNoArgs);
    RESOLVE(PyObject_CallOneArg);
    RESOLVE(PyObject_CallFunctionObjArgs);
    RESOLVE(PyObject_Call);
    RESOLVE(PyImport_AddModule);
    RESOLVE(PyModule_GetDict);
    RESOLVE(PyRun_StringFlags);
    RESOLVE(PyTuple_New);
    RESOLVE(PyTuple_SetItem);
    RESOLVE(PyList_New);
    RESOLVE(PyList_SetItem);
    RESOLVE(PyObject_Length);
    RESOLVE(PyObject_GetItem);
    RESOLVE(PyObject_SetItem);
    RESOLVE(PyObject_IsTrue);
    RESOLVE(PyObject_GetIter);
    RESOLVE(PyIter_Next);
    RESOLVE(PyObject_Str);
    RESOLVE(PyObject_IsInstance);
    RESOLVE(PyNumber_Index);
    RESOLVE(PyNumber_Long);
    RESOLVE(PyNumber_Float);
    RESOLVE(PyNumber_Add);
    RESOLVE(PyNumber_Subtract);
    RESOLVE(PyNumber_Multiply);
    RESOLVE(PyNumber_TrueDivide);
    RESOLVE(PyNumber_FloorDivide);
    RESOLVE(PyNumber_Remainder);
    RESOLVE(PyNumber_Power);
    RESOLVE(PyNumber_MatrixMultiply);
    RESOLVE(PyErr_Occurred);
    RESOLVE(PyErr_Fetch);
    RESOLVE(PyErr_NormalizeException);
    RESOLVE(PyErr_Restore);
    RESOLVE(PyErr_Clear);
    RESOLVE(PyErr_Print);
    RESOLVE(PyErr_SetString);
    RESOLVE(PyErr_GivenExceptionMatches);
    RESOLVE(PyLong_FromLongLong);
    RESOLVE(PyLong_FromString);
    RESOLVE(PyLong_AsLongLong);
    RESOLVE(PyFloat_FromDouble);
    RESOLVE(PyFloat_AsDouble);
    RESOLVE(PyUnicode_FromStringAndSize);
    RESOLVE(PyUnicode_AsUTF8);
    RESOLVE(PyUnicode_AsUTF8AndSize);
    RESOLVE(PyList_Size);
    RESOLVE(PyList_GetItem);
    RESOLVE(PyTuple_Size);
    RESOLVE(PyTuple_GetItem);
    RESOLVE(PyDict_Next);
    RESOLVE(Py_DecRef);
    RESOLVE(Py_IncRef);
    RESOLVE(PyCapsule_New);
    RESOLVE(PyCapsule_GetPointer);
    RESOLVE(PyCFunction_NewEx);
    RESOLVE(PyArg_UnpackTuple);
    RESOLVE(PyBool_FromLong);
    RESOLVE(PyBytes_FromStringAndSize);
    RESOLVE(PyDict_Copy);
    RESOLVE(PyDict_New);
    RESOLVE(PyDict_SetItem);
    RESOLVE(PyDict_SetItemString);
    RESOLVE(PyDict_Contains);
    RESOLVE(PySequence_Tuple);
    RESOLVE(PySet_Add);
    RESOLVE(PySet_New);

#define RESOLVE_OBJECT(name) \
    p_##name = (CPyObject *)dlsym(g_libpython_handle, #name); \
    if (!p_##name) { \
        fprintf(stderr, "pcc runtime error: failed to resolve symbol %s\n", #name); \
        abort(); \
    }

    RESOLVE_OBJECT(_Py_NoneStruct);
    RESOLVE_OBJECT(PyBool_Type);
    RESOLVE_OBJECT(PyLong_Type);
    RESOLVE_OBJECT(PyFloat_Type);
    RESOLVE_OBJECT(PyUnicode_Type);
    RESOLVE_OBJECT(PyList_Type);
    RESOLVE_OBJECT(PyTuple_Type);
    RESOLVE_OBJECT(PyDict_Type);
    RESOLVE_OBJECT(PySet_Type);

    p_PyExc_SystemExit =
        (CPyObject **)dlsym(g_libpython_handle, "PyExc_SystemExit");
    if (!p_PyExc_SystemExit) {
        fprintf(
            stderr,
            "pcc runtime error: failed to resolve symbol PyExc_SystemExit\n"
        );
        abort();
    }
    p_PyExc_RuntimeError =
        (CPyObject **)dlsym(g_libpython_handle, "PyExc_RuntimeError");
    if (!p_PyExc_RuntimeError) {
        fprintf(
            stderr,
            "pcc runtime error: failed to resolve symbol PyExc_RuntimeError\n"
        );
        abort();
    }
    p_PyExc_TypeError =
        (CPyObject **)dlsym(g_libpython_handle, "PyExc_TypeError");
    if (!p_PyExc_TypeError) {
        fprintf(
            stderr,
            "pcc runtime error: failed to resolve symbol PyExc_TypeError\n"
        );
        abort();
    }
#undef RESOLVE_OBJECT
#undef RESOLVE
}

enum {
    PCC_CPY_UNINITIALIZED = 0,
    PCC_CPY_INITIALIZING = 1,
    PCC_CPY_READY = 2,
};

static atomic_int g_init_state = PCC_CPY_UNINITIALIZED;

typedef struct {
    int active;
    int outermost;
    PccPyGILState state;
    uint64_t scope_id;
    uint64_t previous_scope_id;
} PccCpyGILGuard;

typedef struct {
    int saved_depth;
    void *thread_state;
    uint64_t scope_id;
} PccCpyGILSuspension;

typedef struct PccCpyCallbackErrorContext {
    uint64_t scope_id;
    PyObject *pcc_exception;
    void *pcc_exception_root;
    CPyObject *cpy_exception;
    struct PccCpyCallbackErrorContext *next;
} PccCpyCallbackErrorContext;

typedef struct {
    CPyObject *type;
    CPyObject *value;
    CPyObject *traceback;
} PccCpyPendingError;

static _Thread_local int g_cpy_gil_depth = 0;
static _Thread_local uint64_t g_cpy_current_scope_id = 0;
static _Thread_local uint64_t g_cpy_next_scope_id = 0;
static _Thread_local PccCpyCallbackErrorContext *g_cpy_callback_errors = NULL;
static pthread_key_t g_cpy_pending_error_key;
static int g_cpy_pending_error_key_ready = 0;

void py_cpy_ensure_init(void);
static PccCpyGILGuard py_cpy_gil_enter(void);
static void py_cpy_gil_leave(PccCpyGILGuard *guard);
static PccCpyGILSuspension py_cpy_gil_suspend_for_callback(void);
static CPyObject *py_cpy_gil_resume_after_callback(
    PccCpyGILSuspension suspension,
    CPyObject *result
);

#if !defined(__GNUC__) && !defined(__clang__)
#error "py_libpython.c requires cleanup-attribute support for scoped GIL guards"
#endif

#define PCC_CPY_GIL_GUARD() \
    PccCpyGILGuard pcc_cpy_gil_guard \
        __attribute__((cleanup(py_cpy_gil_leave))) = py_cpy_gil_enter()

static void py_cpy_sync_sys_argv_with_gil(void);

static int py_cpy_debug_errors_enabled(void) {
    const char *flag = getenv("PCC_CPY_DEBUG_ERRORS");
    return flag != NULL && flag[0] != '\0' && flag[0] != '0';
}

static void py_cpy_debug_current_error(const char *where) {
    if (!py_cpy_debug_errors_enabled() || PyErr_Occurred() == NULL) {
        return;
    }
    CPyObject *etype = NULL;
    CPyObject *evalue = NULL;
    CPyObject *etb = NULL;
    PyErr_Fetch(&etype, &evalue, &etb);
    PyErr_NormalizeException(&etype, &evalue, &etb);
    const char *type_utf8 = "<null>";
    const char *value_utf8 = "<null>";
    CPyObject *type_str = etype != NULL ? PyObject_Str(etype) : NULL;
    CPyObject *value_str = evalue != NULL ? PyObject_Str(evalue) : NULL;
    if (type_str != NULL) {
        const char *s = PyUnicode_AsUTF8(type_str);
        if (s != NULL) type_utf8 = s;
    }
    if (value_str != NULL) {
        const char *s = PyUnicode_AsUTF8(value_str);
        if (s != NULL) value_utf8 = s;
    }
    fprintf(stderr, "pcc cpy error in %s: %s: %s\n", where, type_utf8, value_utf8);
    if (type_str != NULL) Py_DecRef(type_str);
    if (value_str != NULL) Py_DecRef(value_str);
    PyErr_Restore(etype, evalue, etb);
}

static void py_cpy_debug_result_state(const char *where, CPyObject *res) {
    if (res == NULL || PyErr_Occurred() != NULL) {
        py_cpy_debug_current_error(where);
    }
}

static int py_cpy_system_exit_code(CPyObject *exc_value) {
    int code = 1;
    if (exc_value == NULL) {
        return code;
    }

    CPyObject *code_obj = PyObject_GetAttrString(exc_value, "code");
    if (code_obj == NULL) {
        PyErr_Clear();
        return code;
    }

    if (code_obj == &_Py_NoneStruct) {
        Py_DecRef(code_obj);
        return 0;
    }

    long long ll = PyLong_AsLongLong(code_obj);
    if (PyErr_Occurred() == NULL) {
        Py_DecRef(code_obj);
        return (int)ll;
    }

    PyErr_Clear();
    CPyObject *text = PyObject_Str(code_obj);
    if (text != NULL) {
        const char *utf8 = PyUnicode_AsUTF8(text);
        if (utf8 != NULL && utf8[0] != '\0') {
            fprintf(stderr, "%s\n", utf8);
        }
        Py_DecRef(text);
    } else {
        PyErr_Clear();
    }
    Py_DecRef(code_obj);
    return code;
}

int py_cpy_main_exitcode(void) {
    int state = atomic_load_explicit(&g_init_state, memory_order_acquire);
    if (state == PCC_CPY_UNINITIALIZED) {
        return 0;
    }
    py_cpy_ensure_init();
    PCC_CPY_GIL_GUARD();
    if (!Py_IsInitialized()) return 0;
    if (PyErr_Occurred() == NULL) {
        return 0;
    }

    CPyObject *etype = NULL;
    CPyObject *evalue = NULL;
    CPyObject *etb = NULL;
    PyErr_Fetch(&etype, &evalue, &etb);

    if (etype == NULL) {
        if (evalue != NULL) Py_DecRef(evalue);
        if (etb != NULL) Py_DecRef(etb);
        fprintf(stderr, "pcc runtime error: corrupt CPython exception state\n");
        return 1;
    }


    /* Check for SystemExit WITHOUT normalizing first.  etype is the
     * exception class; PyErr_GivenExceptionMatches works on classes. */
    if (PyErr_GivenExceptionMatches(etype, PyExc_SystemExit)) {
        /* Normalize only for SystemExit so we can extract the code. */
        PyErr_NormalizeException(&etype, &evalue, &etb);
        int code = py_cpy_system_exit_code(evalue);
        if (etype != NULL) Py_DecRef(etype);
        if (evalue != NULL) Py_DecRef(evalue);
        if (etb != NULL) Py_DecRef(etb);
        return code;
    }

    /* Restore the owned triple so CPython can render the traceback and
     * consume it through its normal error-display path. Public C-API
     * isolation guarantees these are CPython objects, not pcc layouts. */
    PyErr_Restore(etype, evalue, etb);
    PyErr_Print();
    return 1;
}

static int py_cpy_sync_sys_argv(void) {
    int argc = py_runtime_program_argc > 0 ? py_runtime_program_argc : 1;
    CPyObject *sys_mod = PyImport_ImportModule("sys");
    if (sys_mod == NULL) return -1;
    CPyObject *argv_list = PyList_New((long)argc);
    if (argv_list == NULL) {
        Py_DecRef(sys_mod);
        return -1;
    }
    for (int i = 0; i < argc; i++) {
        const char *arg = "";
        if (
            py_runtime_program_argv != NULL
            && i < py_runtime_program_argc
            && py_runtime_program_argv[i] != NULL
        ) {
            arg = py_runtime_program_argv[i];
        }
        size_t n = 0;
        while (arg[n] != '\0') n++;
        CPyObject *arg_obj = PyUnicode_FromStringAndSize(arg, (long)n);
        if (arg_obj == NULL) {
            Py_DecRef(argv_list);
            Py_DecRef(sys_mod);
            return -1;
        }
        if (PyList_SetItem(argv_list, (long)i, arg_obj) != 0) {
            /* PyList_SetItem steals arg_obj even when it reports failure. */
            Py_DecRef(argv_list);
            Py_DecRef(sys_mod);
            return -1;
        }
    }
    int rc = PyObject_SetAttrString(sys_mod, "argv", argv_list);
    Py_DecRef(argv_list);
    Py_DecRef(sys_mod);
    return rc;
}

static int py_cpy_seed_sys_path(void) {
    CPyObject *main_module = PyImport_AddModule("__main__");
    if (main_module == NULL) return -1;
    CPyObject *main_dict = PyModule_GetDict(main_module);
    if (main_dict == NULL) return -1;
    CPyObject *result = PyRun_StringFlags(
        "import glob, os, sys\n"
        "def _pcc_add_path(path):\n"
        "    if path and path not in sys.path:\n"
        "        sys.path.insert(0, path)\n"
        "def _pcc_seed_root(root):\n"
        "    if not root:\n"
        "        return\n"
        "    _pcc_add_path(root)\n"
        "    venv_lib = os.path.join(root, '.venv', 'lib')\n"
        "    if os.path.isdir(venv_lib):\n"
        "        for site in glob.glob(os.path.join(venv_lib, 'python*', 'site-packages')):\n"
        "            _pcc_add_path(site)\n"
        "for _pcc_pkg_site in os.environ.get('PCC_PACKAGE_SITE', '').split(os.pathsep):\n"
        "    _pcc_add_path(_pcc_pkg_site.strip())\n"
        "cwd = os.getcwd()\n"
        "if cwd:\n"
        "    _pcc_seed_root(cwd)\n"
        "if sys.argv:\n"
        "    argv0_dir = os.path.dirname(os.path.abspath(sys.argv[0]))\n"
        "    _pcc_add_path(argv0_dir)\n"
        "    _pcc_seed_root(os.path.dirname(argv0_dir))\n"
        "    _pcc_seed_root(os.path.dirname(os.path.dirname(argv0_dir)))\n",
        257,  /* Py_file_input */
        main_dict,
        main_dict,
        NULL
    );
    if (result == NULL) return -1;
    Py_DecRef(result);
    return 0;
}

static void py_cpy_setup_fatal(const char *phase) {
    if (PyErr_Occurred() != NULL) {
        /* PyErr_Print treats SystemExit specially and can terminate with its
         * requested status.  Fetch and intentionally leak the exception on
         * this abort-only path so setup can never false-succeed via exit(0). */
        CPyObject *etype = NULL;
        CPyObject *evalue = NULL;
        CPyObject *etraceback = NULL;
        PyErr_Fetch(&etype, &evalue, &etraceback);
    }
    fprintf(
        stderr,
        "pcc runtime error: libpython bridge %s setup failed\n",
        phase
    );
    abort();
}

static void py_cpy_pending_error_destroy(void *opaque) {
    PccCpyPendingError *pending = (PccCpyPendingError *)opaque;
    if (pending == NULL) return;
    if (
        pending->type != NULL
        || pending->value != NULL
        || pending->traceback != NULL
    ) {
        PccPyGILState state = p_PyGILState_Ensure();
        if (pending->type != NULL) Py_DecRef(pending->type);
        if (pending->value != NULL) Py_DecRef(pending->value);
        if (pending->traceback != NULL) Py_DecRef(pending->traceback);
        p_PyGILState_Release(state);
    }
    free(pending);
}

static PccCpyPendingError *py_cpy_pending_error_state(int create) {
    if (!g_cpy_pending_error_key_ready) {
        fprintf(stderr, "pcc runtime error: CPython error key is unavailable\n");
        abort();
    }
    PccCpyPendingError *pending = pthread_getspecific(
        g_cpy_pending_error_key
    );
    if (pending == NULL && create) {
        pending = calloc(1, sizeof(*pending));
        if (
            pending == NULL
            || pthread_setspecific(g_cpy_pending_error_key, pending) != 0
        ) {
            free(pending);
            fprintf(stderr, "pcc runtime error: CPython error state failed\n");
            abort();
        }
    }
    return pending;
}

static void py_cpy_restore_pending_error(void) {
    PccCpyPendingError *pending = py_cpy_pending_error_state(0);
    if (
        pending == NULL
        || (
            pending->type == NULL
            && pending->value == NULL
            && pending->traceback == NULL
        )
    ) {
        return;
    }
    PyErr_Restore(pending->type, pending->value, pending->traceback);
    pending->type = NULL;
    pending->value = NULL;
    pending->traceback = NULL;
}

static void py_cpy_store_pending_error(void) {
    PccCpyPendingError *pending = py_cpy_pending_error_state(1);
    if (
        pending->type != NULL
        || pending->value != NULL
        || pending->traceback != NULL
    ) {
        fprintf(stderr, "pcc runtime error: CPython error state overwrite\n");
        abort();
    }
    PyErr_Fetch(&pending->type, &pending->value, &pending->traceback);
}

/* Hook used by py_format()'s default-branch to render a CPython
 * PyObject via PyObject_Str instead of the opaque "<object tag=N>"
 * fallback. Installed in py_cpy_ensure_init below. */
static int py_format_cpy_object_via_str(int fd, void *obj) {
    if (obj == NULL) return 0;
    PCC_CPY_GIL_GUARD();
    CPyObject *s = PyObject_Str((CPyObject *)obj);
    if (s == NULL) {
        if (PyErr_Occurred() != NULL) PyErr_Clear();
        return 0;
    }
    long len = 0;
    const char *utf8 = PyUnicode_AsUTF8AndSize(s, &len);
    if (utf8 != NULL && len > 0) {
        /* fputs/write tolerate short writes here; the caller flushed
         * the surrounding stream context. */
        (void)write(fd, utf8, (size_t)len);
    }
    Py_DecRef(s);
    return 1;
}

void py_cpy_ensure_init(void) {
    int expected = 0;
    if (atomic_compare_exchange_strong_explicit(
            &g_init_state,
            &expected,
            PCC_CPY_INITIALIZING,
            memory_order_acq_rel,
            memory_order_acquire
        )) {
        py_cpy_resolve_symbols();
        if (
            pthread_key_create(
                &g_cpy_pending_error_key,
                py_cpy_pending_error_destroy
            ) != 0
        ) {
            fprintf(stderr, "pcc runtime error: CPython error key failed\n");
            abort();
        }
        g_cpy_pending_error_key_ready = 1;
        Py_Initialize();
        py_runtime_program_args_hook = py_cpy_sync_sys_argv_with_gil;
        py_format_cpy_object_hook = py_format_cpy_object_via_str;
        /* CpyHandle boxes (J2') release their foreign refs through
         * this hook; registered here so the main runtime archive
         * never references libpython-archive symbols directly. */
        py_cpy_handle_set_release_fn(py_cpy_decref);
        if (py_cpy_sync_sys_argv() != 0) py_cpy_setup_fatal("sys.argv");
        if (py_cpy_seed_sys_path() != 0) py_cpy_setup_fatal("sys.path");
        if (PyErr_Occurred() != NULL) py_cpy_setup_fatal("post-init");
        if (p_PyEval_SaveThread() == NULL) {
            fprintf(
                stderr,
                "pcc runtime error: PyEval_SaveThread returned NULL\n"
            );
            abort();
        }
        atomic_store_explicit(
            &g_init_state, PCC_CPY_READY, memory_order_release
        );
        return;
    }

    while (expected == PCC_CPY_INITIALIZING) {
        sched_yield();
        expected = atomic_load_explicit(
            &g_init_state, memory_order_acquire
        );
    }
    if (expected != PCC_CPY_READY) {
        fprintf(stderr, "pcc runtime error: invalid libpython init state\n");
        abort();
    }
}

static void py_cpy_decref_if_not_null(CPyObject *obj) {
    if (obj != NULL) Py_DecRef(obj);
}

static void py_cpy_translate_fetched_error_to_pcc(
    CPyObject *etype,
    CPyObject *evalue,
    CPyObject *etraceback
) {
    PyErr_NormalizeException(&etype, &evalue, &etraceback);
    CPyObject *display = evalue != NULL ? evalue : etype;
    CPyObject *text = display != NULL ? PyObject_Str(display) : NULL;
    const char *message = text != NULL ? PyUnicode_AsUTF8(text) : NULL;
    PyObject *pcc_error = py_exc_new(
        PY_EXC_RUNTIMEERROR,
        message != NULL ? message : "CPython callback raised an exception"
    );
    if (PyErr_Occurred() != NULL) PyErr_Clear();
    py_cpy_decref_if_not_null(text);
    py_cpy_decref_if_not_null(etype);
    py_cpy_decref_if_not_null(evalue);
    py_cpy_decref_if_not_null(etraceback);
    py_raise_owned(pcc_error);
}

static int py_cpy_finish_callback_error_scope(uint64_t scope_id) {
    int found = 0;
    PccCpyCallbackErrorContext *cursor = g_cpy_callback_errors;
    while (cursor != NULL) {
        if (cursor->scope_id == scope_id) found = 1;
        cursor = cursor->next;
    }
    if (!found) return 0;

    CPyObject *etype = NULL;
    CPyObject *evalue = NULL;
    CPyObject *etraceback = NULL;
    if (PyErr_Occurred() != NULL) {
        PyErr_Fetch(&etype, &evalue, &etraceback);
        PyErr_NormalizeException(&etype, &evalue, &etraceback);
    }

    PccCpyCallbackErrorContext *matched = NULL;
    cursor = g_cpy_callback_errors;
    while (cursor != NULL) {
        if (
            cursor->scope_id == scope_id
            && cursor->cpy_exception == evalue
        ) {
            matched = cursor;
            break;
        }
        cursor = cursor->next;
    }

    PccCpyCallbackErrorContext **link = &g_cpy_callback_errors;
    while (*link != NULL) {
        PccCpyCallbackErrorContext *context = *link;
        if (context->scope_id != scope_id) {
            link = &context->next;
            continue;
        }
        *link = context->next;
        if (context == matched) {
            int64_t lease = pcc_gc_foreign_lease_acquire(&context->pcc_exception);
            if (lease < 0) {
                fprintf(stderr, "pcc runtime error: callback exception lease failed\n");
                abort();
            }
            py_raise(pcc_gc_load_ptr(NULL, &context->pcc_exception));
            if (pcc_gc_foreign_lease_release(&context->pcc_exception, lease) < 0) {
                fprintf(stderr, "pcc runtime error: callback exception lease cleanup failed\n");
                abort();
            }
        }
        pcc_gc_store_root(&context->pcc_exception, NULL);
        pcc_gc_scheduler_root_unregister_handle(context->pcc_exception_root);
        Py_DecRef(context->cpy_exception);
        free(context);
    }

    if (matched != NULL) {
        py_cpy_decref_if_not_null(etype);
        py_cpy_decref_if_not_null(evalue);
        py_cpy_decref_if_not_null(etraceback);
    } else if (etype != NULL || evalue != NULL || etraceback != NULL) {
        py_cpy_translate_fetched_error_to_pcc(etype, evalue, etraceback);
    }
    return 1;
}

static CPyObject *py_cpy_set_normalized_runtime_error(const char *message) {
    if (PyErr_Occurred() != NULL) PyErr_Clear();
    PyErr_SetString(PyExc_RuntimeError, message);
    CPyObject *etype = NULL;
    CPyObject *evalue = NULL;
    CPyObject *etraceback = NULL;
    PyErr_Fetch(&etype, &evalue, &etraceback);
    PyErr_NormalizeException(&etype, &evalue, &etraceback);
    if (evalue == NULL) {
        fprintf(stderr, "pcc runtime error: callback exception normalization failed\n");
        abort();
    }
    Py_IncRef(evalue);
    PyErr_Restore(etype, evalue, etraceback);
    return evalue;
}

static PccCpyGILGuard py_cpy_gil_enter(void) {
    py_cpy_ensure_init();
    PccCpyGILGuard guard = {
        .active = 1,
        .outermost = 0,
        .state = 0,
        .scope_id = g_cpy_current_scope_id,
        .previous_scope_id = g_cpy_current_scope_id,
    };
    if (g_cpy_gil_depth == 0) {
        guard.state = p_PyGILState_Ensure();
        guard.outermost = 1;
        g_cpy_next_scope_id++;
        if (g_cpy_next_scope_id == 0) g_cpy_next_scope_id++;
        guard.scope_id = g_cpy_next_scope_id;
        g_cpy_current_scope_id = guard.scope_id;
        py_cpy_restore_pending_error();
    }
    g_cpy_gil_depth++;
    return guard;
}

static void py_cpy_gil_leave(PccCpyGILGuard *guard) {
    if (guard == NULL || !guard->active) return;
    if (g_cpy_gil_depth <= 0) {
        fprintf(stderr, "pcc runtime error: unbalanced CPython GIL guard\n");
        abort();
    }
    g_cpy_gil_depth--;
    if (guard->outermost) {
        if (g_cpy_gil_depth != 0) {
            fprintf(stderr, "pcc runtime error: nested CPython GIL leak\n");
            abort();
        }
        int handled_callback_error = py_cpy_finish_callback_error_scope(
            guard->scope_id
        );
        if (!handled_callback_error && PyErr_Occurred() != NULL) {
            py_cpy_store_pending_error();
        }
        p_PyGILState_Release(guard->state);
        g_cpy_current_scope_id = guard->previous_scope_id;
    }
    guard->active = 0;
}

static PccCpyGILSuspension py_cpy_gil_suspend_for_callback(void) {
    /* CPython may invoke a stored callback on one of its own threads. In that
     * case the real GIL is held although no pcc bridge guard contributed to
     * g_cpy_gil_depth, so a saved depth of zero is valid. */
    PccCpyGILSuspension suspension = {
        .saved_depth = g_cpy_gil_depth,
        .thread_state = p_PyEval_SaveThread(),
        .scope_id = g_cpy_current_scope_id,
    };
    if (suspension.thread_state == NULL) {
        fprintf(stderr, "pcc runtime error: callback GIL release failed\n");
        abort();
    }
    g_cpy_gil_depth = 0;
    g_cpy_current_scope_id = 0;
    return suspension;
}

static CPyObject *py_cpy_gil_resume_after_callback(
    PccCpyGILSuspension suspension,
    CPyObject *result
) {
    if (g_cpy_gil_depth != 0 || suspension.saved_depth < 0) {
        fprintf(stderr, "pcc runtime error: callback CPython GIL imbalance\n");
        abort();
    }
    static const int32_t callback_error_roots_map = 2;
    PyObject *error_roots[2] = {NULL, NULL};
    int roots_active = 0;
    char *pcc_message = NULL;
    if (py_err_occurred()) {
        /* Register empty owned roots first, then capture the authoritative
         * TLS exception under the graph lease. No borrowed raw exception is
         * carried through root registration or user __str__ callbacks. */
        pcc_gc_frame_enter(&callback_error_roots_map, error_roots);
        roots_active = 1;
        pcc_py_gc_minor_graph_lock();
        pcc_gc_store_root(&error_roots[0], py_current_exception());
        pcc_py_gc_minor_graph_unlock();
        py_clear_exception();
        if (pcc_gc_load_ptr(NULL, &error_roots[0]) != NULL) {
            int64_t lease = pcc_gc_foreign_lease_acquire(&error_roots[0]);
            if (lease >= 0) {
                /* args[0] is not display text for structured exceptions. */
                error_roots[1] = py_obj_str(pcc_gc_load_ptr(NULL, &error_roots[0]));
                if (pcc_gc_foreign_lease_release(&error_roots[0], lease) < 0) {
                    fprintf(stderr, "pcc runtime error: callback display lease cleanup failed\n");
                    abort();
                }
            }
            if (pcc_gc_load_ptr(NULL, &error_roots[1]) != NULL) {
                int64_t text_lease = pcc_gc_foreign_lease_acquire(&error_roots[1]);
                if (text_lease >= 0) {
                    PyObject *text = pcc_gc_load_ptr(NULL, &error_roots[1]);
                    if (!PY_IS_TAGGED_INT(text) && py_type_of(text) == PY_TYPE_STR) {
                        int64_t length = py_str_byte_len(text);
                        if (length >= 0 && (uint64_t)length < SIZE_MAX) {
                            pcc_message = malloc((size_t)length + 1);
                            if (pcc_message != NULL) {
                                memcpy(pcc_message, py_str_utf8(text), (size_t)length);
                                pcc_message[length] = '\0';
                            }
                        }
                    }
                    if (pcc_gc_foreign_lease_release(&error_roots[1], text_lease) < 0) {
                        fprintf(stderr, "pcc runtime error: callback text lease cleanup failed\n");
                        abort();
                    }
                }
            }
        }
        /* A failed display conversion is secondary to the original callback
         * failure, which remains in error_roots[0]. */
        py_clear_exception();
    }

    p_PyEval_RestoreThread(suspension.thread_state);
    g_cpy_gil_depth = suspension.saved_depth;
    g_cpy_current_scope_id = suspension.scope_id;
    py_cpy_restore_pending_error();
    if (roots_active && pcc_gc_load_ptr(NULL, &error_roots[0]) != NULL) {
        const char *message = pcc_message != NULL
            ? pcc_message
            : "pcc callback raised an exception";
        if (suspension.saved_depth == 0) {
            if (PyErr_Occurred() != NULL) PyErr_Clear();
            PyErr_SetString(PyExc_RuntimeError, message);
        } else {
            CPyObject *synthetic = py_cpy_set_normalized_runtime_error(message);
            PccCpyCallbackErrorContext *context = malloc(sizeof(*context));
            if (context == NULL) {
                fprintf(stderr, "pcc runtime error: callback context allocation failed\n");
                abort();
            }
            context->scope_id = suspension.scope_id;
            context->pcc_exception = NULL;
            context->pcc_exception_root = pcc_gc_scheduler_root_register_handle(
                &context->pcc_exception
            );
            if (context->pcc_exception_root == NULL) {
                fprintf(stderr, "pcc runtime error: callback exception root failed\n");
                abort();
            }
            pcc_py_gc_minor_graph_lock();
            pcc_gc_store_root(&context->pcc_exception,
                             pcc_gc_load_ptr(NULL, &error_roots[0]));
            pcc_py_gc_minor_graph_unlock();
            context->cpy_exception = synthetic;
            context->next = g_cpy_callback_errors;
            g_cpy_callback_errors = context;
        }
        if (result != NULL) {
            Py_DecRef(result);
            result = NULL;
        }
    }
    if (result != NULL && PyErr_Occurred() != NULL) {
        Py_DecRef(result);
        result = NULL;
    } else if (result == NULL && PyErr_Occurred() == NULL) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "pcc callback returned NULL without a CPython exception"
        );
    }
    free(pcc_message);
    if (roots_active) {
        pcc_gc_store_root(&error_roots[1], NULL);
        pcc_gc_store_root(&error_roots[0], NULL);
        pcc_gc_frame_leave(error_roots);
    }

    return result;
}

static void py_cpy_sync_sys_argv_with_gil(void) {
    PCC_CPY_GIL_GUARD();
    if (py_cpy_sync_sys_argv() != 0) py_cpy_setup_fatal("sys.argv hook");
}

void *py_cpy_import(const char *name) {
    PCC_CPY_GIL_GUARD();
    CPyObject *res = PyImport_ImportModule(name);
    if (py_cpy_debug_errors_enabled() && (res == NULL || PyErr_Occurred() != NULL)) {
        fprintf(
            stderr, "pcc cpy import target: %s\n",
            name != NULL ? name : "<null>"
        );
    }
    py_cpy_debug_result_state("py_cpy_import", res);
    return (void *)res;
}

void *py_cpy_getattr(void *obj, const char *name) {
    if (obj == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *res = PyObject_GetAttrString((CPyObject *)obj, name);
    py_cpy_debug_result_state("py_cpy_getattr", res);
    return (void *)res;
}

/* Binary numeric operators on CPython values, dispatched to libpython's
 * PyNumber_* (so e.g. ``numpy_array + numpy_array`` works). op codes:
 * 0=+ 1=- 2=* 3=/ 4=// 5=%. Returns a new CPython reference (or NULL on
 * error / unknown op). */
void *py_cpy_binop(int64_t op, void *a, void *b) {
    if (a == NULL || b == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *la = (CPyObject *)a;
    CPyObject *rb = (CPyObject *)b;
    CPyObject *res = NULL;
    switch (op) {
        case 0: res = PyNumber_Add(la, rb); break;
        case 1: res = PyNumber_Subtract(la, rb); break;
        case 2: res = PyNumber_Multiply(la, rb); break;
        case 3: res = PyNumber_TrueDivide(la, rb); break;
        case 4: res = PyNumber_FloorDivide(la, rb); break;
        case 5: res = PyNumber_Remainder(la, rb); break;
        case 6: res = PyNumber_Power(la, rb, &_Py_NoneStruct); break;
        case 7: res = PyNumber_MatrixMultiply(la, rb); break;
        default: return NULL;
    }
    py_cpy_debug_result_state("py_cpy_binop", res);
    return (void *)res;
}

int py_cpy_setattr(void *obj, const char *name, void *value) {
    if (obj == NULL) return -1;
    PCC_CPY_GIL_GUARD();
    int rc = PyObject_SetAttrString(
        (CPyObject *)obj, name, (CPyObject *)value
    );
    if (rc != 0) py_cpy_debug_current_error("py_cpy_setattr");
    return rc;
}

/* The CPython C-API forbids entering a call with an exception pending.
 * A pending error here is stale state from an earlier failed bridge
 * call whose NULL result the program already flowed past (see
 * py_cpy_call1); left set it corrupts unrelated calls with
 * ``SystemError: ... returned a result with an exception set``. Log it
 * under PCC_CPY_DEBUG_ERRORS, then clear. */
static void py_cpy_clear_stale_error(const char *where) {
    if (PyErr_Occurred() == NULL) return;
    py_cpy_debug_current_error(where);
    PyErr_Clear();
}

void *py_cpy_call_noargs(void *callable) {
    if (callable == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    py_cpy_clear_stale_error("py_cpy_call_noargs(stale)");
    CPyObject *res = PyObject_CallNoArgs((CPyObject *)callable);
    py_cpy_debug_result_state("py_cpy_call_noargs", res);
    return (void *)res;
}

/* Dup the CPython object's str() into a freshly allocated pcc
 * PyStrObject so callers can interop with our native str routines
 * (py_print, py_str_concat, etc.). Returns NULL on failure; the
 * CPython error indicator is left set for the caller to inspect via
 * py_cpy_error_check. */
PyObject *py_cpy_to_pcc_str(void *cpy_obj) {
    if (cpy_obj == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *s = PyObject_Str((CPyObject *)cpy_obj);
    if (s == NULL) return NULL;
    const char *utf8 = PyUnicode_AsUTF8(s);
    if (utf8 == NULL) {
        Py_DecRef(s);
        return NULL;
    }
    /* py_str_new takes a ptr+len and copies. */
    size_t n = 0;
    while (utf8[n] != '\0') n++;
    PyObject *out = py_str_new((const char *)utf8, (int64_t)n);
    Py_DecRef(s);
    return out;
}

static int py_cpy_is_instance(CPyObject *obj, CPyObject *type_obj) {
    int rc = PyObject_IsInstance(obj, type_obj);
    if (rc < 0) {
        PyErr_Clear();
        return 0;
    }
    return rc != 0;
}

static int py_cpy_longlong_from_long_obj(CPyObject *obj, long long *out) {
    long long value = PyLong_AsLongLong(obj);
    if (PyErr_Occurred() != NULL) {
        PyErr_Clear();
        return 0;
    }
    *out = value;
    return 1;
}

static PyObject *py_cpy_long_to_pcc(CPyObject *obj) {
    long long value = 0;
    if (py_cpy_longlong_from_long_obj(obj, &value)) {
        return py_int_from_i64((int64_t)value);
    }

    CPyObject *text = PyObject_Str(obj);
    if (text == NULL) return NULL;
    const char *utf8 = PyUnicode_AsUTF8(text);
    if (utf8 == NULL) {
        Py_DecRef(text);
        return NULL;
    }
    PyObject *result = py_int_from_cstr(utf8, 10);
    Py_DecRef(text);
    return result;
}

static PyObject *py_cpy_index_to_pcc(CPyObject *obj) {
    CPyObject *index_obj = PyNumber_Index(obj);
    if (index_obj == NULL) {
        PyErr_Clear();
        return NULL;
    }
    PyObject *result = py_cpy_long_to_pcc(index_obj);
    Py_DecRef(index_obj);
    return result;
}

static int py_cpy_float_as_double(CPyObject *obj, double *out) {
    CPyObject *flt = PyNumber_Float(obj);
    if (flt == NULL) {
        PyErr_Clear();
        return 0;
    }
    double value = PyFloat_AsDouble(flt);
    Py_DecRef(flt);
    if (PyErr_Occurred() != NULL) {
        PyErr_Clear();
        return 0;
    }
    *out = value;
    return 1;
}

typedef struct {
    PyObject *object;
    void *handle;
} PccCpyTemporaryRoot;

static int py_cpy_temporary_root_take_owned(
    PccCpyTemporaryRoot *root,
    PyObject *object
);
static PyObject *py_cpy_temporary_root_current(PccCpyTemporaryRoot *root);
static void py_cpy_temporary_root_clear(PccCpyTemporaryRoot *root);

typedef struct PccCpyReverseConversionMemo {
    CPyObject *cpy_object;  /* owned stable key while recursive code runs */
    PyObject *pcc_object;   /* registered, relocation-updated owning root */
    void *root_handle;
    struct PccCpyReverseConversionMemo *next;
} PccCpyReverseConversionMemo;

static PyObject *py_cpy_reverse_memo_new_ref(
    PccCpyReverseConversionMemo *entry
) {
    if (entry == NULL) return NULL;
    PyObject *current = pcc_gc_load_ptr(NULL, &entry->pcc_object);
    if (current == NULL) return NULL;
    py_incref(current);
    return current;
}

static PccCpyReverseConversionMemo *py_cpy_reverse_memo_find(
    PccCpyReverseConversionMemo *memo,
    CPyObject *cpy_object
) {
    while (memo != NULL) {
        if (memo->cpy_object == cpy_object) return memo;
        memo = memo->next;
    }
    return NULL;
}

static PccCpyReverseConversionMemo *py_cpy_reverse_memo_begin(
    PccCpyReverseConversionMemo **memo,
    CPyObject *cpy_object
) {
    PccCpyReverseConversionMemo *entry = calloc(1, sizeof(*entry));
    if (entry == NULL) {
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "out of memory recording a reverse bridge conversion"
            );
        }
        return NULL;
    }
    entry->root_handle = pcc_gc_scheduler_root_register_handle(
        &entry->pcc_object
    );
    if (entry->root_handle == NULL) {
        free(entry);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "failed to register a reverse bridge conversion root"
            );
        }
        return NULL;
    }
    Py_IncRef(cpy_object);
    entry->cpy_object = cpy_object;
    entry->next = *memo;
    *memo = entry;
    return entry;
}

/* Transfer an owned pcc result into the relocation-updated memo root. */
static int py_cpy_reverse_memo_publish_owned(
    PccCpyReverseConversionMemo *entry,
    PyObject *object
) {
    if (entry == NULL || object == NULL) return -1;
    pcc_gc_store_root(&entry->pcc_object, object);
    py_decref(object);
    return 0;
}

static PyObject *py_cpy_to_pcc_obj_inner(
    CPyObject *obj,
    PccCpyReverseConversionMemo **memo
) {
    if (obj == NULL) return NULL;
    PccCpyReverseConversionMemo *entry = py_cpy_reverse_memo_find(*memo, obj);
    if (entry != NULL) return py_cpy_reverse_memo_new_ref(entry);
    entry = py_cpy_reverse_memo_begin(memo, obj);
    if (entry == NULL) return NULL;

    PyObject *created = NULL;
    if (obj == &_Py_NoneStruct) {
        py_incref(py_None);
        created = py_None;
    } else if (py_cpy_is_instance(obj, &PyBool_Type)) {
        int truth = PyObject_IsTrue(obj);
        if (truth < 0) return NULL;
        created = py_bool_from_bit(truth != 0);
    } else if (py_cpy_is_instance(obj, &PyLong_Type)) {
        created = py_cpy_long_to_pcc(obj);
    } else if (py_cpy_is_instance(obj, &PyFloat_Type)) {
        double value = PyFloat_AsDouble(obj);
        if (PyErr_Occurred() != NULL) return NULL;
        created = py_float_from_f64(value);
    } else if (py_cpy_is_instance(obj, &PyUnicode_Type)) {
        long n = 0;
        const char *utf8 = PyUnicode_AsUTF8AndSize(obj, &n);
        if (utf8 == NULL) return NULL;
        created = py_str_new(utf8, (int64_t)n);
    } else if (py_cpy_is_instance(obj, &PyList_Type)) {
        long n = PyList_Size(obj);
        if (n < 0) return NULL;
        created = py_list_new((int64_t)n);
        if (created == NULL) return NULL;
        if (py_cpy_reverse_memo_publish_owned(entry, created) < 0) return NULL;
        for (long i = 0; i < n; i++) {
            CPyObject *item = PyList_GetItem(obj, i);  /* borrowed */
            PyObject *pcc_item = py_cpy_to_pcc_obj_inner(item, memo);
            if (pcc_item == NULL) return NULL;
            PccCpyTemporaryRoot item_root;
            if (py_cpy_temporary_root_take_owned(&item_root, pcc_item) < 0) {
                return NULL;
            }
            py_list_append(
                pcc_gc_load_ptr(NULL, &entry->pcc_object),
                py_cpy_temporary_root_current(&item_root)
            );
            py_cpy_temporary_root_clear(&item_root);
            if (py_err_occurred()) return NULL;
        }
        return py_cpy_reverse_memo_new_ref(entry);
    } else if (py_cpy_is_instance(obj, &PyTuple_Type)) {
        long n = PyTuple_Size(obj);
        if (n < 0) return NULL;
        created = py_tuple_new((int64_t)n);
        if (created == NULL) return NULL;
        if (py_cpy_reverse_memo_publish_owned(entry, created) < 0) return NULL;
        for (long i = 0; i < n; i++) {
            CPyObject *item = PyTuple_GetItem(obj, i);  /* borrowed */
            PyObject *pcc_item = py_cpy_to_pcc_obj_inner(item, memo);
            if (pcc_item == NULL) return NULL;
            PccCpyTemporaryRoot item_root;
            if (py_cpy_temporary_root_take_owned(&item_root, pcc_item) < 0) {
                return NULL;
            }
            py_tuple_set_item(
                pcc_gc_load_ptr(NULL, &entry->pcc_object),
                (int64_t)i,
                py_cpy_temporary_root_current(&item_root)
            );
            py_cpy_temporary_root_clear(&item_root);
            if (py_err_occurred()) return NULL;
        }
        return py_cpy_reverse_memo_new_ref(entry);
    } else if (py_cpy_is_instance(obj, &PyDict_Type)) {
        created = py_dict_new();
        if (created == NULL) return NULL;
        if (py_cpy_reverse_memo_publish_owned(entry, created) < 0) return NULL;
        CPyObject *snapshot = PyDict_Copy(obj);
        if (snapshot == NULL) return NULL;
        long pos = 0;
        CPyObject *key = NULL;
        CPyObject *value = NULL;
        while (PyDict_Next(snapshot, &pos, &key, &value)) {
            PyObject *pcc_key = py_cpy_to_pcc_obj_inner(key, memo);
            if (pcc_key == NULL) {
                Py_DecRef(snapshot);
                return NULL;
            }
            PccCpyTemporaryRoot key_root;
            if (py_cpy_temporary_root_take_owned(&key_root, pcc_key) < 0) {
                Py_DecRef(snapshot);
                return NULL;
            }
            PyObject *pcc_value = py_cpy_to_pcc_obj_inner(value, memo);
            if (pcc_value == NULL) {
                py_cpy_temporary_root_clear(&key_root);
                Py_DecRef(snapshot);
                return NULL;
            }
            PccCpyTemporaryRoot value_root;
            if (py_cpy_temporary_root_take_owned(&value_root, pcc_value) < 0) {
                py_cpy_temporary_root_clear(&key_root);
                Py_DecRef(snapshot);
                return NULL;
            }
            py_dict_set(
                pcc_gc_load_ptr(NULL, &entry->pcc_object),
                py_cpy_temporary_root_current(&key_root),
                py_cpy_temporary_root_current(&value_root)
            );
            py_cpy_temporary_root_clear(&value_root);
            py_cpy_temporary_root_clear(&key_root);
            if (py_err_occurred()) {
                Py_DecRef(snapshot);
                return NULL;
            }
        }
        Py_DecRef(snapshot);
        if (PyErr_Occurred() != NULL) return NULL;
        return py_cpy_reverse_memo_new_ref(entry);
    } else if (py_cpy_is_instance(obj, &PySet_Type)) {
        CPyObject *it = PyObject_GetIter(obj);
        if (it == NULL) return NULL;
        created = py_set_new();
        if (created == NULL) {
            Py_DecRef(it);
            return NULL;
        }
        if (py_cpy_reverse_memo_publish_owned(entry, created) < 0) {
            Py_DecRef(it);
            return NULL;
        }
        for (;;) {
            CPyObject *item = PyIter_Next(it);
            if (item == NULL) break;
            PyObject *pcc_item = py_cpy_to_pcc_obj_inner(item, memo);
            Py_DecRef(item);
            if (pcc_item == NULL) {
                Py_DecRef(it);
                return NULL;
            }
            PccCpyTemporaryRoot item_root;
            if (py_cpy_temporary_root_take_owned(&item_root, pcc_item) < 0) {
                Py_DecRef(it);
                return NULL;
            }
            py_set_add(
                pcc_gc_load_ptr(NULL, &entry->pcc_object),
                py_cpy_temporary_root_current(&item_root)
            );
            py_cpy_temporary_root_clear(&item_root);
            if (py_err_occurred()) {
                Py_DecRef(it);
                return NULL;
            }
        }
        Py_DecRef(it);
        if (PyErr_Occurred() != NULL) return NULL;
        return py_cpy_reverse_memo_new_ref(entry);
    } else {
        created = py_cpy_index_to_pcc(obj);
        if (created == NULL) {
            double float_value = 0.0;
            if (py_cpy_float_as_double(obj, &float_value)) {
                created = py_float_from_f64(float_value);
            } else {
                created = py_cpy_to_pcc_str(obj);
            }
        }
    }

    if (created == NULL) return NULL;
    if (py_cpy_reverse_memo_publish_owned(entry, created) < 0) return NULL;
    return py_cpy_reverse_memo_new_ref(entry);
}

PyObject *py_cpy_to_pcc_obj(void *cpy_obj) {
    if (cpy_obj == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    PccCpyReverseConversionMemo *memo = NULL;
    PyObject *result = py_cpy_to_pcc_obj_inner(
        (CPyObject *)cpy_obj, &memo
    );
    PccCpyTemporaryRoot output_root = {0};
    if (
        result != NULL
        && py_cpy_temporary_root_take_owned(&output_root, result) < 0
    ) {
        result = NULL;
    }
    while (memo != NULL) {
        PccCpyReverseConversionMemo *next = memo->next;
        pcc_gc_store_root(&memo->pcc_object, NULL);
        pcc_gc_scheduler_root_unregister_handle(memo->root_handle);
        Py_DecRef(memo->cpy_object);
        free(memo);
        memo = next;
    }
    if (output_root.handle != NULL) {
        result = py_cpy_temporary_root_current(&output_root);
        py_incref(result);
        py_cpy_temporary_root_clear(&output_root);
    }
    return result;
}

void py_cpy_decref(void *obj) {
    if (obj == NULL) return;
    PCC_CPY_GIL_GUARD();
    Py_DecRef((CPyObject *)obj);
}

void py_cpy_incref(void *obj) {
    if (obj == NULL) return;
    PCC_CPY_GIL_GUARD();
    Py_IncRef((CPyObject *)obj);
}

void *py_cpy_from_i64(int64_t value) {
    PCC_CPY_GIL_GUARD();
    return (void *)PyLong_FromLongLong((long long)value);
}

int64_t py_cpy_to_i64(void *obj) {
    if (obj == NULL) return 0;
    PCC_CPY_GIL_GUARD();
    CPyObject *long_obj = PyNumber_Long((CPyObject *)obj);
    if (long_obj == NULL) {
        PyErr_Clear();
        return 0;
    }
    long long value = 0;
    int ok = py_cpy_longlong_from_long_obj(long_obj, &value);
    Py_DecRef(long_obj);
    return ok ? (int64_t)value : 0;
}

void *py_cpy_from_f64(double value) {
    PCC_CPY_GIL_GUARD();
    return (void *)PyFloat_FromDouble(value);
}

double py_cpy_to_f64(void *obj) {
    if (obj == NULL) return 0.0;
    PCC_CPY_GIL_GUARD();
    return PyFloat_AsDouble((CPyObject *)obj);
}

/* Convert a pcc PyStrObject* to a CPython unicode object. The caller
 * retains ownership of the pcc string; this function returns a new
 * owned CPython reference. */
void *py_cpy_from_pccstr(PyObject *s) {
    if (s == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    /* Our py_str API exposes data + length via accessors in py_str.c. */
    extern const char *py_str_utf8(PyObject *s);
    extern int64_t     py_str_byte_len(PyObject *s);
    const char *data = py_str_utf8(s);
    int64_t len = py_str_byte_len(s);
    if (data == NULL || len < 0) return NULL;
    return (void *)PyUnicode_FromStringAndSize(data, (long)len);
}

/* Universal pcc → CPython converter. Dispatches on the pcc type tag
 * and rebuilds the object using CPython C API. Recurses through list
 * / tuple / dict / set. Returns a new CPython owned ref (caller must
 * ``py_cpy_decref``). A per-call memo preserves aliases and cycles among
 * mutable containers instead of recursively duplicating or overflowing.
 * NULL input → NULL. */

typedef struct PccCpyConversionMemo {
    PyObject *pcc_object;       /* registered, relocation-updated owning root */
    CPyObject *cpy_object;      /* borrowed from the constructed object graph */
    void *root_handle;
    int constructing_tuple;
    struct PccCpyConversionMemo *next;
} PccCpyConversionMemo;

/* Transfer one owned pcc reference into a relocation-updated root slot. */
static int py_cpy_temporary_root_take_owned(
    PccCpyTemporaryRoot *root,
    PyObject *object
) {
    root->object = NULL;
    root->handle = pcc_gc_scheduler_root_register_handle(&root->object);
    if (root->handle == NULL) {
        py_decref(object);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "failed to register a temporary libpython bridge root"
            );
        }
        return -1;
    }
    pcc_gc_store_root(&root->object, object);
    py_decref(object);
    return 0;
}

static PyObject *py_cpy_temporary_root_current(PccCpyTemporaryRoot *root) {
    if (root == NULL || root->handle == NULL) return NULL;
    return pcc_gc_load_ptr(NULL, &root->object);
}

static void py_cpy_temporary_root_clear(PccCpyTemporaryRoot *root) {
    if (root->handle == NULL) return;
    pcc_gc_store_root(&root->object, NULL);
    pcc_gc_scheduler_root_unregister_handle(root->handle);
    root->object = NULL;
    root->handle = NULL;
}

static int py_cpy_conversion_memo_lookup(
    PccCpyConversionMemo *memo,
    PyObject *object,
    CPyObject **result
) {
    *result = NULL;
    while (memo != NULL) {
        if (memo->pcc_object == object) {
            if (memo->cpy_object == NULL) {
                if (PyErr_Occurred() == NULL) {
                    PyErr_SetString(
                        PyExc_RuntimeError,
                        "recursive bridge conversion before container creation"
                    );
                }
                return -1;
            }
            if (memo->constructing_tuple) {
                if (PyErr_Occurred() == NULL) {
                    PyErr_SetString(
                        PyExc_RuntimeError,
                        "tuple-mediated cycles are not supported by the "
                        "libpython bridge"
                    );
                }
                return -1;
            }
            Py_IncRef(memo->cpy_object);
            *result = memo->cpy_object;
            return 1;
        }
        memo = memo->next;
    }
    return 0;
}

/* Register a relocation-updated pcc root before any recursive pcc allocation.
 * The root owns one temporary pcc reference and is released with the memo. */
static PccCpyConversionMemo *py_cpy_conversion_memo_begin(
    PccCpyConversionMemo **memo,
    PyObject *pcc_object
) {
    PccCpyConversionMemo *entry = calloc(1, sizeof(*entry));
    if (entry == NULL) {
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "out of memory recording a libpython bridge conversion"
            );
        }
        return NULL;
    }
    entry->root_handle = pcc_gc_scheduler_root_register_handle(
        &entry->pcc_object
    );
    if (entry->root_handle == NULL) {
        free(entry);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "failed to register a libpython bridge conversion root"
            );
        }
        return NULL;
    }
    pcc_gc_store_root(&entry->pcc_object, pcc_object);
    entry->next = *memo;
    *memo = entry;
    return entry;
}

/* Attach an already-owned CPython result to its memo entry without taking
 * another reference. On failure this consumes the result. */
static CPyObject *py_cpy_conversion_memo_finish(
    PccCpyConversionMemo *entry,
    CPyObject *cpy_object
) {
    if (cpy_object == NULL) return NULL;
    entry->cpy_object = cpy_object;
    return cpy_object;
}

static CPyObject *py_cpy_from_pcc_obj_inner(
    PyObject *o,
    PccCpyConversionMemo **memo
) {
    if (o == NULL) return NULL;
    CPyObject *memoized = NULL;
    int memo_status = py_cpy_conversion_memo_lookup(*memo, o, &memoized);
    if (memo_status > 0) return memoized;
    if (memo_status < 0) return NULL;
    PccCpyConversionMemo *entry = py_cpy_conversion_memo_begin(memo, o);
    if (entry == NULL) return NULL;
    o = entry->pcc_object;
    int32_t tag = py_type_of(o);
    switch (tag) {
    case PY_TYPE_NONE: {
        Py_IncRef(&_Py_NoneStruct);
        return py_cpy_conversion_memo_finish(entry, &_Py_NoneStruct);
    }
    case PY_TYPE_BOOL: {
        /* Tagged-int path; re-use bool conversion via int→bool in CPython. */
        int64_t v = py_int_to_i64(o, NULL);
        return py_cpy_conversion_memo_finish(
            entry, PyBool_FromLong((long)v)
        );
    }
    case PY_TYPE_INT: {
        if (PY_IS_TAGGED_INT(o)) {
            return py_cpy_conversion_memo_finish(
                entry,
                PyLong_FromLongLong((long long)py_untag_int(o))
            );
        }
        PyObject *text = py_int_to_str_obj(entry->pcc_object);
        if (text == NULL) return NULL;
        const char *utf8 = py_str_utf8(text);
        CPyObject *result = utf8 != NULL
            ? PyLong_FromString((char *)utf8, NULL, 10)
            : NULL;
        py_decref(text);
        return py_cpy_conversion_memo_finish(entry, result);
    }
    case PY_TYPE_FLOAT: {
        double v = py_float_to_f64(o);
        return py_cpy_conversion_memo_finish(
            entry, PyFloat_FromDouble(v)
        );
    }
    case PY_TYPE_STR:
        return py_cpy_conversion_memo_finish(
            entry, (CPyObject *)py_cpy_from_pccstr(entry->pcc_object)
        );
    case PY_TYPE_BYTES: {
        PyBytesObject *b = (PyBytesObject *)o;
        return py_cpy_conversion_memo_finish(
            entry,
            PyBytes_FromStringAndSize(b->data, (long)b->byte_len)
        );
    }
    case PY_TYPE_BYTEARRAY: {
        PyByteArrayObject *b = (PyByteArrayObject *)o;
        return py_cpy_conversion_memo_finish(
            entry,
            PyBytes_FromStringAndSize(b->data, (long)b->byte_len)
        );
    }
    case PY_TYPE_MEMORYVIEW: {
        PyMemoryViewObject *m = (PyMemoryViewObject *)o;
        PyObject *base = pcc_gc_load_ptr(o, &m->base);
        CPyObject *result = py_cpy_from_pcc_obj_inner(base, memo);
        return py_cpy_conversion_memo_finish(entry, result);
    }
    case PY_TYPE_LIST: {
        int64_t n = py_list_len(o);
        CPyObject *lst = PyList_New((long)n);
        if (lst == NULL) return NULL;
        if (py_cpy_conversion_memo_finish(entry, lst) == NULL) return NULL;
        for (int64_t i = 0; i < n; i++) {
            PyObject *elem = py_list_get(entry->pcc_object, i);
            if (elem == NULL) {
                Py_DecRef(lst);
                return NULL;
            }
            PccCpyTemporaryRoot elem_root;
            if (py_cpy_temporary_root_take_owned(&elem_root, elem) < 0) {
                Py_DecRef(lst);
                return NULL;
            }
            CPyObject *c = py_cpy_from_pcc_obj_inner(elem_root.object, memo);
            py_cpy_temporary_root_clear(&elem_root);
            if (c == NULL) {
                Py_DecRef(lst);
                return NULL;
            }
            /* PyList_SetItem consumes ``c`` on both success and failure. */
            if (PyList_SetItem(lst, (long)i, c) < 0) {
                Py_DecRef(lst);
                return NULL;
            }
        }
        return lst;
    }
    case PY_TYPE_TUPLE: {
        int64_t n = py_tuple_len(o);
        CPyObject *tup = PyTuple_New((long)n);
        if (tup == NULL) return NULL;
        entry->constructing_tuple = 1;
        if (py_cpy_conversion_memo_finish(entry, tup) == NULL) return NULL;
        for (int64_t i = 0; i < n; i++) {
            PyObject *elem = py_tuple_get(entry->pcc_object, i);
            if (elem == NULL) {
                Py_DecRef(tup);
                return NULL;
            }
            PccCpyTemporaryRoot elem_root;
            if (py_cpy_temporary_root_take_owned(&elem_root, elem) < 0) {
                Py_DecRef(tup);
                return NULL;
            }
            CPyObject *c = py_cpy_from_pcc_obj_inner(elem_root.object, memo);
            py_cpy_temporary_root_clear(&elem_root);
            if (c == NULL) {
                Py_DecRef(tup);
                return NULL;
            }
            /* PyTuple_SetItem consumes ``c`` on both success and failure. */
            if (PyTuple_SetItem(tup, (long)i, c) < 0) {
                Py_DecRef(tup);
                return NULL;
            }
        }
        entry->constructing_tuple = 0;
        return tup;
    }
    case PY_TYPE_DICT: {
        CPyObject *d = PyDict_New();
        if (d == NULL) return NULL;
        if (py_cpy_conversion_memo_finish(entry, d) == NULL) return NULL;
        PyObject *keys = py_dict_keys(entry->pcc_object);  /* new list ref */
        if (keys == NULL) {
            Py_DecRef(d);
            return NULL;
        }
        PccCpyTemporaryRoot keys_root;
        if (py_cpy_temporary_root_take_owned(&keys_root, keys) < 0) {
            Py_DecRef(d);
            return NULL;
        }
        int64_t n = py_list_len(keys_root.object);
        for (int64_t i = 0; i < n; i++) {
            PyObject *k = py_list_get(keys_root.object, i);
            if (k == NULL) {
                py_cpy_temporary_root_clear(&keys_root);
                Py_DecRef(d);
                return NULL;
            }
            PccCpyTemporaryRoot key_root;
            if (py_cpy_temporary_root_take_owned(&key_root, k) < 0) {
                py_cpy_temporary_root_clear(&keys_root);
                Py_DecRef(d);
                return NULL;
            }
            PyObject *v = py_dict_get(entry->pcc_object, key_root.object);
            if (v == NULL) {
                py_cpy_temporary_root_clear(&key_root);
                py_cpy_temporary_root_clear(&keys_root);
                Py_DecRef(d);
                return NULL;
            }
            PccCpyTemporaryRoot value_root;
            if (py_cpy_temporary_root_take_owned(&value_root, v) < 0) {
                py_cpy_temporary_root_clear(&key_root);
                py_cpy_temporary_root_clear(&keys_root);
                Py_DecRef(d);
                return NULL;
            }
            CPyObject *ck = py_cpy_from_pcc_obj_inner(key_root.object, memo);
            CPyObject *cv = ck != NULL
                ? py_cpy_from_pcc_obj_inner(value_root.object, memo)
                : NULL;
            py_cpy_temporary_root_clear(&key_root);
            py_cpy_temporary_root_clear(&value_root);
            if (ck == NULL || cv == NULL) {
                py_cpy_decref_if_not_null(ck);
                py_cpy_decref_if_not_null(cv);
                py_cpy_temporary_root_clear(&keys_root);
                Py_DecRef(d);
                return NULL;
            }
            int set_status = PyDict_SetItem(d, ck, cv);
            Py_DecRef(ck);
            Py_DecRef(cv);
            if (set_status < 0) {
                py_cpy_temporary_root_clear(&keys_root);
                Py_DecRef(d);
                return NULL;
            }
        }
        py_cpy_temporary_root_clear(&keys_root);
        return d;
    }
    case PY_TYPE_SET: {
        CPyObject *set = PySet_New(NULL);
        if (set == NULL) return NULL;
        if (py_cpy_conversion_memo_finish(entry, set) == NULL) return NULL;
        PyObject *items = py_set_items(entry->pcc_object);  /* new pcc list ref */
        if (items == NULL) {
            Py_DecRef(set);
            return NULL;
        }
        PccCpyTemporaryRoot items_root;
        if (py_cpy_temporary_root_take_owned(&items_root, items) < 0) {
            Py_DecRef(set);
            return NULL;
        }
        int64_t n = py_list_len(items_root.object);
        for (int64_t i = 0; i < n; i++) {
            PyObject *item = py_list_get(items_root.object, i);
            if (item == NULL) {
                py_cpy_temporary_root_clear(&items_root);
                Py_DecRef(set);
                return NULL;
            }
            PccCpyTemporaryRoot item_root;
            if (py_cpy_temporary_root_take_owned(&item_root, item) < 0) {
                py_cpy_temporary_root_clear(&items_root);
                Py_DecRef(set);
                return NULL;
            }
            CPyObject *cpy_item = py_cpy_from_pcc_obj_inner(
                item_root.object, memo
            );
            py_cpy_temporary_root_clear(&item_root);
            if (cpy_item == NULL) {
                py_cpy_temporary_root_clear(&items_root);
                Py_DecRef(set);
                return NULL;
            }
            int add_status = PySet_Add(set, cpy_item);
            Py_DecRef(cpy_item);  /* PySet_Add borrows and retains on success. */
            if (add_status < 0) {
                py_cpy_temporary_root_clear(&items_root);
                Py_DecRef(set);
                return NULL;
            }
        }
        py_cpy_temporary_root_clear(&items_root);
        return set;
    }
    default: {
        /* Unknown tag — best effort: str(o) → CPython unicode. Prevents a
         * hard crash when passing a class instance or similar. */
        extern PyObject *py_obj_repr(PyObject *o);
        PyObject *r = py_obj_repr(entry->pcc_object);
        if (r == NULL) return NULL;
        CPyObject *res = (CPyObject *)py_cpy_from_pccstr(r);
        py_decref(r);
        return py_cpy_conversion_memo_finish(entry, res);
    }
    }
}

void *py_cpy_from_pcc_obj(PyObject *o) {
    if (o == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    PccCpyConversionMemo *memo = NULL;
    CPyObject *result = py_cpy_from_pcc_obj_inner(o, &memo);
    while (memo != NULL) {
        PccCpyConversionMemo *next = memo->next;
        pcc_gc_store_root(&memo->pcc_object, NULL);
        pcc_gc_scheduler_root_unregister_handle(memo->root_handle);
        free(memo);
        memo = next;
    }
    return (void *)result;
}

/* NULL args are failed results from an earlier bridge call flowing
 * through (the emitted IR does not branch on cpy results). Calling
 * CPython's call APIs with a NULL argument is undefined behavior, so
 * skip the call and let the NULL keep flowing, matching the
 * py_cpy_getitem/py_cpy_setitem guards. */
void *py_cpy_call1(void *callable, void *a) {
    if (callable == NULL || a == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    py_cpy_clear_stale_error("py_cpy_call1(stale)");
    CPyObject *res = PyObject_CallOneArg((CPyObject *)callable, (CPyObject *)a);
    py_cpy_debug_result_state("py_cpy_call1", res);
    return (void *)res;
}

void *py_cpy_call2(void *callable, void *a, void *b) {
    if (callable == NULL || a == NULL || b == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    py_cpy_clear_stale_error("py_cpy_call2(stale)");
    CPyObject *res = PyObject_CallFunctionObjArgs(
        (CPyObject *)callable, (CPyObject *)a, (CPyObject *)b, (CPyObject *)NULL
    );
    py_cpy_debug_result_state("py_cpy_call2", res);
    return (void *)res;
}

void *py_cpy_call3(void *callable, void *a, void *b, void *c) {
    if (callable == NULL || a == NULL || b == NULL || c == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    py_cpy_clear_stale_error("py_cpy_call3(stale)");
    CPyObject *res = PyObject_CallFunctionObjArgs(
        (CPyObject *)callable,
        (CPyObject *)a, (CPyObject *)b, (CPyObject *)c,
        (CPyObject *)NULL
    );
    py_cpy_debug_result_state("py_cpy_call3", res);
    return (void *)res;
}

int64_t py_cpy_len(void *obj) {
    if (obj == NULL) return 0;
    PCC_CPY_GIL_GUARD();
    return (int64_t)PyObject_Length((CPyObject *)obj);
}

void *py_cpy_getitem(void *obj, void *key) {
    if (obj == NULL || key == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    return (void *)PyObject_GetItem((CPyObject *)obj, (CPyObject *)key);
}

int py_cpy_setitem(void *obj, void *key, void *val) {
    if (obj == NULL || key == NULL) return -1;
    PCC_CPY_GIL_GUARD();
    return PyObject_SetItem((CPyObject *)obj, (CPyObject *)key, (CPyObject *)val);
}

int py_cpy_truthy(void *obj) {
    if (obj == NULL) return 0;
    PCC_CPY_GIL_GUARD();
    return PyObject_IsTrue((CPyObject *)obj);
}

void *py_cpy_iter(void *obj) {
    if (obj == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    return (void *)PyObject_GetIter((CPyObject *)obj);
}

/* Return the next item (new ref) or NULL on end-of-iteration. */
void *py_cpy_iter_next(void *it) {
    if (it == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    return (void *)PyIter_Next((CPyObject *)it);
}

/* Tuple-based call for arbitrary arity. Each arg in the flat argv
 * array is handed off to PyTuple_SetItem which STEALS the ref, so
 * the caller must not decref its argv entries after this returns. */
static void py_cpy_release_owned_argv(int64_t n, void **argv) {
    if (n <= 0 || argv == NULL) return;
    for (int64_t i = 0; i < n; i++) {
        if (argv[i] != NULL) Py_DecRef((CPyObject *)argv[i]);
    }
}

static int py_cpy_validate_owned_argv(int64_t n, void **argv) {
    if (n < 0 || (n > 0 && argv == NULL)) {
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "invalid positional argument array in libpython bridge"
            );
        }
        return -1;
    }
    for (int64_t i = 0; i < n; i++) {
        if (argv[i] != NULL) continue;
        py_cpy_release_owned_argv(n, argv);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "NULL positional argument in libpython bridge"
            );
        }
        return -1;
    }
    return 0;
}

/* Consume every owned argv entry and either return the tuple that now owns
 * them or NULL.  PyTuple_SetItem consumes its current item even on failure;
 * the tuple releases earlier items and this helper releases the untouched
 * suffix.  This makes the stealing contract independent of the failure
 * boundary. */
static CPyObject *py_cpy_tuple_from_owned_argv(int64_t n, void **argv) {
    CPyObject *tuple = PyTuple_New((long)n);
    if (tuple == NULL) {
        py_cpy_release_owned_argv(n, argv);
        return NULL;
    }
    for (int64_t i = 0; i < n; i++) {
        if (PyTuple_SetItem(tuple, (long)i, (CPyObject *)argv[i]) < 0) {
            py_cpy_release_owned_argv(n - i - 1, argv + i + 1);
            Py_DecRef(tuple);
            return NULL;
        }
    }
    return tuple;
}

void *py_cpy_call_argv(void *callable, int64_t n, void **argv) {
    PCC_CPY_GIL_GUARD();
    if (callable == NULL) {
        py_cpy_release_owned_argv(n, argv);
        return NULL;
    }
    if (py_cpy_validate_owned_argv(n, argv) < 0) return NULL;
    py_cpy_clear_stale_error("py_cpy_call_argv(stale)");
    CPyObject *tup = py_cpy_tuple_from_owned_argv(n, argv);
    if (tup == NULL) return NULL;
    CPyObject *result = PyObject_Call(
        (CPyObject *)callable, tup, (CPyObject *)NULL
    );
    Py_DecRef(tup);
    py_cpy_debug_result_state("py_cpy_call_argv", result);
    return (void *)result;
}

/* Tuple + dict call for positional + keyword arguments.
 *
 * Positional argv[0..n_pos) is stolen into a PyTuple.
 * Keyword kw_vals[0..n_kw) is borrowed by PyDict_SetItem (dict
 * increfs). The caller still owns each kw_vals entry and must decref
 * after this returns. */
/* Dispatch ``fn(*args)`` where ``args`` is a pcc list/tuple. Convert
 * the pcc container to a CPython tuple (PyObject_Call requires the
 * positional-args container to be a tuple; a list or other sequence
 * would trip ``_PyFunction_Vectorcall`` in CPython 3.11+) and dispatch
 * via ``PyObject_Call``. Returns a new owned ref or NULL on error. */
void *py_cpy_call_list(void *callable, PyObject *args) {
    if (callable == NULL || args == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    py_cpy_clear_stale_error("py_cpy_call_list(stale)");
    CPyObject *seq = (CPyObject *)py_cpy_from_pcc_obj(args);
    if (seq == NULL) return NULL;
    CPyObject *tup = PySequence_Tuple(seq);
    Py_DecRef(seq);
    if (tup == NULL) return NULL;
    CPyObject *result = PyObject_Call(
        (CPyObject *)callable, tup, (CPyObject *)NULL
    );
    Py_DecRef(tup);
    py_cpy_debug_result_state("py_cpy_call_list", result);
    return (void *)result;
}

void *py_cpy_call_list_kwdict(void *callable, PyObject *args, void *kwargs_dict) {
    if (callable == NULL || args == NULL || kwargs_dict == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    py_cpy_clear_stale_error("py_cpy_call_list_kwdict(stale)");
    CPyObject *seq = (CPyObject *)py_cpy_from_pcc_obj(args);
    if (seq == NULL) return NULL;
    CPyObject *tup = PySequence_Tuple(seq);
    Py_DecRef(seq);
    if (tup == NULL) return NULL;
    CPyObject *result = PyObject_Call(
        (CPyObject *)callable, tup, (CPyObject *)kwargs_dict
    );
    Py_DecRef(tup);
    py_cpy_debug_result_state("py_cpy_call_list_kwdict", result);
    return (void *)result;
}

/* ---- Lambda wrapping: pcc FuncDef → CPython callable ---------------- */

/* Typed function pointer shape for a pcc FuncDef with ABI
 * ``CPyObject *(CPyObject *)`` — a single DynType-in DynType-out.
 * pcc's codegen lowers this signature as ``ptr(ptr)`` (PyObject* /
 * opaque), matching CPython's ``PyObject *`` layout exactly. */
typedef CPyObject *(*_pcc_1arg_fn_t)(CPyObject *);

/* PyCFunction trampoline: PyCFunction signature is
 * ``PyObject *(*)(PyObject *self, PyObject *args)`` where ``self`` is
 * the ``m_self`` we passed to ``PyCFunction_NewEx`` (a PyCapsule
 * holding the pcc function pointer). ``args`` is a positional tuple —
 * use ``PyArg_UnpackTuple`` to get the single positional arg. */
static CPyObject *_pcc_1arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *arg;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 1, 1, &arg)) return NULL;
    /* Forward the arg to the pcc function. The pcc emit for this
     * body tags the incoming PyObject* as a CPython value so attr /
     * method ops route through ``py_cpy_getattr`` rather than the
     * pcc-native ``py_obj_getattr``. */
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_1arg_fn_t)fn_ptr)(arg);
    return py_cpy_gil_resume_after_callback(suspension, result);
}

static PccPyMethodDef _pcc_1arg_methdef = {
    .ml_name = "pcc_lambda",
    .ml_meth = (void *)_pcc_1arg_trampoline,
    .ml_flags = 0x1,   /* METH_VARARGS */
    .ml_doc = NULL,
};

/* Wrap a pcc FuncDef function pointer (signature CPyObject* <- CPyObject*)
 * as a CPython PyCFunction. The caller uses the returned value the same
 * as any CPython callable (``PyObject_Call`` / ``PyObject_CallOneArg``
 * etc.). Returns NULL on failure. Caller owns the returned ref. */
void *py_cpy_wrap_pcc_1arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_1arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);  /* PyCFunction holds its own ref via m_self. */
    return (void *)callable;
}

/* 0-arg variant. Signature: ``CPyObject *(void)``. */
typedef CPyObject *(*_pcc_0arg_fn_t)(void);
static CPyObject *_pcc_0arg_trampoline(CPyObject *self, CPyObject *args) {
    (void)args;
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_0arg_fn_t)fn_ptr)();
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_0arg_methdef = {
    .ml_name = "pcc_lambda_0",
    .ml_meth = (void *)_pcc_0arg_trampoline,
    .ml_flags = 0x4,   /* METH_NOARGS */
    .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_0arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_0arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 2-arg variant. Signature: ``CPyObject *(CPyObject *, CPyObject *)``. */
typedef CPyObject *(*_pcc_2arg_fn_t)(CPyObject *, CPyObject *);
static CPyObject *_pcc_2arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 2, 2, &a1, &a2)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_2arg_fn_t)fn_ptr)(a1, a2);
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_2arg_methdef = {
    .ml_name = "pcc_lambda_2",
    .ml_meth = (void *)_pcc_2arg_trampoline,
    .ml_flags = 0x1,
    .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_2arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_2arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 3-arg variant. Signature: ``CPyObject *(CPyObject *, CPyObject *, CPyObject *)``. */
typedef CPyObject *(*_pcc_3arg_fn_t)(CPyObject *, CPyObject *, CPyObject *);
static CPyObject *_pcc_3arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2, *a3;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 3, 3, &a1, &a2, &a3)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_3arg_fn_t)fn_ptr)(a1, a2, a3);
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_3arg_methdef = {
    .ml_name = "pcc_lambda_3",
    .ml_meth = (void *)_pcc_3arg_trampoline,
    .ml_flags = 0x1,
    .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_3arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_3arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 4-arg variant. */
typedef CPyObject *(*_pcc_4arg_fn_t)(CPyObject *, CPyObject *, CPyObject *, CPyObject *);
static CPyObject *_pcc_4arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2, *a3, *a4;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 4, 4, &a1, &a2, &a3, &a4)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_4arg_fn_t)fn_ptr)(a1, a2, a3, a4);
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_4arg_methdef = {
    .ml_name = "pcc_lambda_4",
    .ml_meth = (void *)_pcc_4arg_trampoline,
    .ml_flags = 0x1, .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_4arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_4arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 5-arg variant. */
typedef CPyObject *(*_pcc_5arg_fn_t)(CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *);
static CPyObject *_pcc_5arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2, *a3, *a4, *a5;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 5, 5, &a1, &a2, &a3, &a4, &a5)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_5arg_fn_t)fn_ptr)(a1, a2, a3, a4, a5);
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_5arg_methdef = {
    .ml_name = "pcc_lambda_5",
    .ml_meth = (void *)_pcc_5arg_trampoline,
    .ml_flags = 0x1, .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_5arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_5arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 6-arg variant. */
typedef CPyObject *(*_pcc_6arg_fn_t)(CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *);
static CPyObject *_pcc_6arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2, *a3, *a4, *a5, *a6;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 6, 6, &a1, &a2, &a3, &a4, &a5, &a6)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_6arg_fn_t)fn_ptr)(a1, a2, a3, a4, a5, a6);
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_6arg_methdef = {
    .ml_name = "pcc_lambda_6",
    .ml_meth = (void *)_pcc_6arg_trampoline,
    .ml_flags = 0x1, .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_6arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_6arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 7-arg variant. */
typedef CPyObject *(*_pcc_7arg_fn_t)(CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *);
static CPyObject *_pcc_7arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2, *a3, *a4, *a5, *a6, *a7;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 7, 7, &a1, &a2, &a3, &a4, &a5, &a6, &a7)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_7arg_fn_t)fn_ptr)(a1, a2, a3, a4, a5, a6, a7);
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_7arg_methdef = {
    .ml_name = "pcc_lambda_7",
    .ml_meth = (void *)_pcc_7arg_trampoline,
    .ml_flags = 0x1, .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_7arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_7arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 8-arg variant. */
typedef CPyObject *(*_pcc_8arg_fn_t)(CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *);
static CPyObject *_pcc_8arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2, *a3, *a4, *a5, *a6, *a7, *a8;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 8, 8, &a1, &a2, &a3, &a4, &a5, &a6, &a7, &a8)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_8arg_fn_t)fn_ptr)(
        a1, a2, a3, a4, a5, a6, a7, a8
    );
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_8arg_methdef = {
    .ml_name = "pcc_lambda_8",
    .ml_meth = (void *)_pcc_8arg_trampoline,
    .ml_flags = 0x1, .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_8arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_8arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

/* 9-arg variant. */
typedef CPyObject *(*_pcc_9arg_fn_t)(CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *, CPyObject *);
static CPyObject *_pcc_9arg_trampoline(CPyObject *self, CPyObject *args) {
    void *fn_ptr = PyCapsule_GetPointer(self, NULL);
    if (fn_ptr == NULL) return NULL;
    CPyObject *a1, *a2, *a3, *a4, *a5, *a6, *a7, *a8, *a9;
    if (!PyArg_UnpackTuple(args, "pcc_lambda", 9, 9, &a1, &a2, &a3, &a4, &a5, &a6, &a7, &a8, &a9)) return NULL;
    PccCpyGILSuspension suspension = py_cpy_gil_suspend_for_callback();
    CPyObject *result = ((_pcc_9arg_fn_t)fn_ptr)(
        a1, a2, a3, a4, a5, a6, a7, a8, a9
    );
    return py_cpy_gil_resume_after_callback(suspension, result);
}
static PccPyMethodDef _pcc_9arg_methdef = {
    .ml_name = "pcc_lambda_9",
    .ml_meth = (void *)_pcc_9arg_trampoline,
    .ml_flags = 0x1, .ml_doc = NULL,
};
void *py_cpy_wrap_pcc_9arg(void *fn_ptr) {
    if (fn_ptr == NULL) return NULL;
    PCC_CPY_GIL_GUARD();
    CPyObject *cap = PyCapsule_New(fn_ptr, NULL, NULL);
    if (cap == NULL) return NULL;
    CPyObject *callable = PyCFunction_NewEx(
        &_pcc_9arg_methdef, cap, (CPyObject *)NULL
    );
    Py_DecRef(cap);
    return (void *)callable;
}

void *py_cpy_call_kw(void *callable,
                     int64_t n_pos, void **argv,
                     int64_t n_kw, const char **kw_names, void **kw_vals) {
    PCC_CPY_GIL_GUARD();
    if (callable == NULL) {
        py_cpy_release_owned_argv(n_pos, argv);
        return NULL;
    }
    if (py_cpy_validate_owned_argv(n_pos, argv) < 0) return NULL;
    if (n_kw < 0 || (n_kw > 0 && (kw_names == NULL || kw_vals == NULL))) {
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "invalid keyword argument array in libpython bridge"
            );
        }
        py_cpy_release_owned_argv(n_pos, argv);
        return NULL;
    }
    for (int64_t i = 0; i < n_kw; i++) {
        if (kw_names[i] != NULL && kw_vals[i] != NULL) continue;
        py_cpy_release_owned_argv(n_pos, argv);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "NULL keyword argument in libpython bridge"
            );
        }
        return NULL;
    }
    py_cpy_clear_stale_error("py_cpy_call_kw(stale)");
    CPyObject *tup = py_cpy_tuple_from_owned_argv(n_pos, argv);
    if (tup == NULL) return NULL;
    CPyObject *kwargs = NULL;
    if (n_kw > 0) {
        kwargs = PyDict_New();
        if (kwargs == NULL) {
            Py_DecRef(tup);
            return NULL;
        }
        for (int64_t i = 0; i < n_kw; i++) {
            if (
                PyDict_SetItemString(
                    kwargs, kw_names[i], (CPyObject *)kw_vals[i]
                ) < 0
            ) {
                Py_DecRef(tup);
                Py_DecRef(kwargs);
                return NULL;
            }
        }
    }
    CPyObject *result = PyObject_Call((CPyObject *)callable, tup, kwargs);
    Py_DecRef(tup);
    if (kwargs != NULL) Py_DecRef(kwargs);
    py_cpy_debug_result_state("py_cpy_call_kw", result);
    return (void *)result;
}

void *py_cpy_call_kwdict(void *callable,
                         int64_t n_pos, void **argv,
                         void *kwargs_dict) {
    PCC_CPY_GIL_GUARD();
    if (callable == NULL) {
        py_cpy_release_owned_argv(n_pos, argv);
        return NULL;
    }
    if (py_cpy_validate_owned_argv(n_pos, argv) < 0) return NULL;
    if (kwargs_dict == NULL) {
        py_cpy_release_owned_argv(n_pos, argv);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "NULL keyword mapping in libpython bridge"
            );
        }
        return NULL;
    }
    py_cpy_clear_stale_error("py_cpy_call_kwdict(stale)");
    CPyObject *tup = py_cpy_tuple_from_owned_argv(n_pos, argv);
    if (tup == NULL) return NULL;
    CPyObject *result = PyObject_Call(
        (CPyObject *)callable, tup, (CPyObject *)kwargs_dict
    );
    Py_DecRef(tup);
    py_cpy_debug_result_state("py_cpy_call_kwdict", result);
    return (void *)result;
}

void *py_cpy_call_kwdict_plus(void *callable,
                              int64_t n_pos, void **argv,
                              int64_t n_kw,
                              const char **kw_names, void **kw_vals,
                              void *kwargs_dict) {
    PCC_CPY_GIL_GUARD();
    if (callable == NULL) {
        py_cpy_release_owned_argv(n_pos, argv);
        return NULL;
    }
    if (py_cpy_validate_owned_argv(n_pos, argv) < 0) return NULL;
    if (kwargs_dict == NULL) {
        py_cpy_release_owned_argv(n_pos, argv);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "NULL keyword mapping in libpython bridge"
            );
        }
        return NULL;
    }
    if (n_kw < 0 || (n_kw > 0 && (kw_names == NULL || kw_vals == NULL))) {
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "invalid keyword argument array in libpython bridge"
            );
        }
        py_cpy_release_owned_argv(n_pos, argv);
        return NULL;
    }
    for (int64_t i = 0; i < n_kw; i++) {
        if (kw_names[i] != NULL && kw_vals[i] != NULL) continue;
        py_cpy_release_owned_argv(n_pos, argv);
        if (PyErr_Occurred() == NULL) {
            PyErr_SetString(
                PyExc_RuntimeError,
                "NULL keyword argument in libpython bridge"
            );
        }
        return NULL;
    }
    py_cpy_clear_stale_error("py_cpy_call_kwdict_plus(stale)");
    CPyObject *tup = py_cpy_tuple_from_owned_argv(n_pos, argv);
    if (tup == NULL) return NULL;
    CPyObject *kwargs = kwargs_dict != NULL
        ? PyDict_Copy((CPyObject *)kwargs_dict)
        : PyDict_New();
    if (kwargs == NULL) {
        Py_DecRef(tup);
        return NULL;
    }
    for (int64_t i = 0; i < n_kw; i++) {
        CPyObject *key = PyUnicode_FromStringAndSize(
            kw_names[i], (long)strlen(kw_names[i])
        );
        if (key == NULL) {
            Py_DecRef(tup);
            Py_DecRef(kwargs);
            return NULL;
        }
        int contains = PyDict_Contains(kwargs, key);
        if (contains > 0) {
            PyErr_SetString(
                PyExc_TypeError,
                "multiple values for keyword argument"
            );
        }
        int set_status = contains == 0
            ? PyDict_SetItem(kwargs, key, (CPyObject *)kw_vals[i])
            : -1;
        Py_DecRef(key);
        if (set_status < 0) {
            Py_DecRef(tup);
            Py_DecRef(kwargs);
            return NULL;
        }
    }
    CPyObject *result = PyObject_Call((CPyObject *)callable, tup, kwargs);
    Py_DecRef(tup);
    Py_DecRef(kwargs);
    py_cpy_debug_result_state("py_cpy_call_kwdict_plus", result);
    return (void *)result;
}

#else /* !PCC_WITH_LIBPYTHON */

/* Build variant that does not link libpython. All CPython fallback
 * entry points abort on use so mis-compiled programs fail loudly
 * rather than silently returning NULL. */

void py_cpy_ensure_init(void) {
    fprintf(stderr, "pcc: import fell through to the CPython fallback "
                    "but the runtime was built without libpython support "
                    "(rebuild with PCC_WITH_LIBPYTHON=1)\n");
    abort();
}

void *py_cpy_import(const char *name) {
    (void)name;
    py_cpy_ensure_init();
    return NULL;
}

void *py_cpy_getattr(void *obj, const char *name) {
    (void)obj; (void)name;
    py_cpy_ensure_init();
    return NULL;
}

void *py_cpy_binop(int64_t op, void *a, void *b) {
    (void)op; (void)a; (void)b;
    py_cpy_ensure_init();
    return NULL;
}

int py_cpy_setattr(void *obj, const char *name, void *value) {
    (void)obj; (void)name; (void)value;
    py_cpy_ensure_init();
    return -1;
}

int py_cpy_main_exitcode(void) {
    return 0;
}

void *py_cpy_call_noargs(void *callable) {
    (void)callable;
    py_cpy_ensure_init();
    return NULL;
}

PyObject *py_cpy_to_pcc_str(void *cpy_obj) {
    (void)cpy_obj;
    py_cpy_ensure_init();
    return NULL;
}

PyObject *py_cpy_to_pcc_obj(void *cpy_obj) {
    (void)cpy_obj;
    py_cpy_ensure_init();
    return NULL;
}

void py_cpy_decref(void *obj) {
    (void)obj;
    py_cpy_ensure_init();
}

void py_cpy_incref(void *obj) {
    (void)obj;
    py_cpy_ensure_init();
}

void *py_cpy_from_i64(int64_t v) {
    (void)v; py_cpy_ensure_init(); return NULL;
}
int64_t py_cpy_to_i64(void *o) {
    (void)o; py_cpy_ensure_init(); return 0;
}
void *py_cpy_from_f64(double v) {
    (void)v; py_cpy_ensure_init(); return NULL;
}
double py_cpy_to_f64(void *o) {
    (void)o; py_cpy_ensure_init(); return 0.0;
}
void *py_cpy_from_pccstr(PyObject *s) {
    (void)s; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_call1(void *c, void *a) {
    (void)c; (void)a; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_call2(void *c, void *a, void *b) {
    (void)c; (void)a; (void)b; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_call3(void *c, void *a, void *b, void *d) {
    (void)c; (void)a; (void)b; (void)d; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_call_argv(void *c, int64_t n, void **argv) {
    (void)c; (void)n; (void)argv; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_call_list(void *c, PyObject *args) {
    (void)c; (void)args; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_call_list_kwdict(void *c, PyObject *args, void *kwargs_dict) {
    (void)c; (void)args; (void)kwargs_dict;
    py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_0arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_1arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_2arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_3arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_4arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_5arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_6arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_7arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_8arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_wrap_pcc_9arg(void *fn_ptr) {
    (void)fn_ptr; py_cpy_ensure_init(); return NULL;
}
int64_t py_cpy_len(void *o) {
    (void)o; py_cpy_ensure_init(); return 0;
}
void *py_cpy_getitem(void *o, void *k) {
    (void)o; (void)k; py_cpy_ensure_init(); return NULL;
}
int py_cpy_setitem(void *o, void *k, void *v) {
    (void)o; (void)k; (void)v; py_cpy_ensure_init(); return -1;
}
int py_cpy_truthy(void *o) {
    (void)o; py_cpy_ensure_init(); return 0;
}
void *py_cpy_iter(void *o) {
    (void)o; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_iter_next(void *i) {
    (void)i; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_from_pcc_obj(PyObject *o) {
    (void)o; py_cpy_ensure_init(); return NULL;
}
void *py_cpy_call_kw(void *c, int64_t n_pos, void **argv,
                     int64_t n_kw, const char **kw_names, void **kw_vals) {
    (void)c; (void)n_pos; (void)argv;
    (void)n_kw; (void)kw_names; (void)kw_vals;
    py_cpy_ensure_init();
    return NULL;
}

void *py_cpy_call_kwdict(void *c, int64_t n_pos, void **argv, void *kwargs_dict) {
    (void)c; (void)n_pos; (void)argv; (void)kwargs_dict;
    py_cpy_ensure_init();
    return NULL;
}

void *py_cpy_call_kwdict_plus(void *c, int64_t n_pos, void **argv,
                              int64_t n_kw,
                              const char **kw_names, void **kw_vals,
                              void *kwargs_dict) {
    (void)c; (void)n_pos; (void)argv;
    (void)n_kw; (void)kw_names; (void)kw_vals; (void)kwargs_dict;
    py_cpy_ensure_init();
    return NULL;
}

#endif /* PCC_WITH_LIBPYTHON */
