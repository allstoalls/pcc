/* pcc/py_runtime/src/py_coroutine.c
 *
 * Native coroutine object for pcc-compiled Python. The object stores
 * the same generic adapter ABI as py_func.c:
 *
 *     PyObject *entry(PyObject *captures_tuple, PyObject *args_tuple)
 *
 * The first await runs the adapter, caches its result, and subsequent
 * awaits return the cached object. There is intentionally no event-loop
 * scheduler here yet; this is the synchronous no-suspension subset that
 * async def / await / __await__ can share without pulling in libpython.
 */

#include "py_internal.h"
#include <stdlib.h>

typedef struct {
    PyObjectHeader h;
    const char *name;
    PyNativeFuncEntry entry;
    PyObject *captures;
    PyObject *args;
    PyObject *result;
    int32_t closed;
    int32_t done;
} PyCoroutineObject;

static PyObject *coroutine_require_result(
    PyObject *result,
    const char *helper_name,
    const char *message
) {
    if (result == NULL) {
        py_runtime_error_if_unset(helper_name, message);
    }
    return result;
}

PyObject *py_coroutine_class(void) {
    static PyObject *cls = NULL;
    if (cls != NULL) return cls;
    cls = (PyObject *)py_class_new("coroutine", NULL, 0, NULL, 0);
    if (cls != NULL) {
        py_header(cls)->flags |= PY_FLAG_IMMORTAL;
    }
    return cls;
}

PyObject *py_coroutine_new(const char *name) {
    return py_coroutine_new_native(name, NULL, NULL, NULL);
}

PyObject *py_coroutine_new_native(
    const char *name, void *entry, PyObject *captures_tuple,
    PyObject *args_tuple
) {
    PyCoroutineObject *c = (PyCoroutineObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyCoroutineObject), PY_TYPE_COROUTINE, 0
    );
    if (c == NULL) {
        return coroutine_require_result(
            NULL,
            "pcc_gc_alloc",
            "coroutine construction could not allocate coroutine state"
        );
    }
    c->name = name;
    c->entry = (PyNativeFuncEntry)entry;
    c->captures = NULL;
    c->args = NULL;
    c->result = NULL;
    c->closed = 0;
    c->done = 0;

    int made_captures = captures_tuple == NULL;
    PyObject *captures = made_captures ? py_tuple_new(0) : captures_tuple;
    if (captures == NULL) {
        coroutine_require_result(
            NULL,
            "py_tuple_new",
            "coroutine construction could not allocate captures tuple"
        );
        pcc_gc_release((PyObject *)c);
        return NULL;
    }
    int made_args = args_tuple == NULL;
    PyObject *args = made_args ? py_tuple_new(0) : args_tuple;
    if (args == NULL) {
        coroutine_require_result(
            NULL,
            "py_tuple_new",
            "coroutine construction could not allocate arguments tuple"
        );
        if (made_captures) py_decref(captures);
        pcc_gc_release((PyObject *)c);
        return NULL;
    }

    pcc_gc_store_ptr((PyObject *)c, &c->captures, captures);
    pcc_gc_store_ptr((PyObject *)c, &c->args, args);
    if (made_captures) py_decref(captures);
    if (made_args) py_decref(args);
    py_gc_track((PyObject *)c);
    pcc_gc_publish_initialized((PyObject *)c);
    return (PyObject *)c;
}

static PyCoroutineObject *checked_coroutine(PyObject *coro) {
    if (coro == NULL || PY_IS_TAGGED_INT(coro)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a coroutine"));
        return NULL;
    }
    if (py_type_of(coro) != PY_TYPE_COROUTINE) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a coroutine"));
        return NULL;
    }
    return (PyCoroutineObject *)coro;
}

PyObject *py_coroutine_run(PyObject *coro) {
    PyCoroutineObject *c = checked_coroutine(coro);
    if (c == NULL) return NULL;
    if (c->closed != 0) {
        py_raise_owned(py_exc_new(PY_EXC_RUNTIMEERROR, "cannot reuse closed coroutine"));
        return NULL;
    }
    if (c->done != 0) {
        py_raise_owned(py_exc_new(
            PY_EXC_RUNTIMEERROR,
            "cannot reuse already awaited coroutine"
        ));
        return NULL;
    }
    PyObject *result = py_None;
    if (c->entry != NULL) {
        PyObject *captures = pcc_gc_load_ptr((PyObject *)c, &c->captures);
        PyObject *args = pcc_gc_load_ptr((PyObject *)c, &c->args);
        result = c->entry(captures, args);
        if (result == NULL) {
            return coroutine_require_result(
                NULL,
                c->name != NULL ? c->name : "coroutine entry",
                "coroutine entry returned NULL without setting an exception"
            );
        }
    } else {
        py_incref(result);
    }
    pcc_gc_store_ptr((PyObject *)c, &c->result, result);
    c->done = 1;
    return result;
}

int64_t py_coroutine_is_done(PyObject *coro) {
    PyCoroutineObject *c = checked_coroutine(coro);
    if (c == NULL) return 1;
    return c->done != 0 ? 1 : 0;
}

PyObject *py_coroutine_get_result(PyObject *coro) {
    PyCoroutineObject *c = checked_coroutine(coro);
    if (c == NULL) return NULL;
    PyObject *result = pcc_gc_load_ptr(coro, &c->result);
    if (result == NULL) result = py_None;
    py_incref(result);
    return result;
}

PyObject *py_coroutine_get_args(PyObject *coro) {
    PyCoroutineObject *c = checked_coroutine(coro);
    if (c == NULL) return NULL;
    PyObject *args = pcc_gc_load_ptr(coro, &c->args);
    if (args == NULL) args = py_None;
    py_incref(args);
    return args;
}

PyObject *py_coroutine_close(PyObject *coro) {
    if (coro == NULL || PY_IS_TAGGED_INT(coro)) return py_None;
    if (py_type_of(coro) == PY_TYPE_COROUTINE) {
        ((PyCoroutineObject *)coro)->closed = 1;
    }
    return py_None;
}

static PyObject *await_iterator(PyObject *it) {
    if (it == NULL) {
        return coroutine_require_result(
            NULL,
            "await_iterator",
            "await iterator received NULL iterator"
        );
    }
    while (1) {
        PyObject *item = py_obj_next(it);
        if (item != NULL) {
            py_decref(item);
            continue;
        }
        PyObject *cur = py_current_exception();
        PyObject *stop_cls = (PyObject *)py_exc_builtin_class(
            PY_EXC_STOPITERATION
        );
        if (py_exc_matches(cur, stop_cls)) {
            PyObject *value = py_exc_get_message(cur);
            if (value == NULL) value = py_None;
            py_incref(value);
            py_clear_exception();
            return value;
        }
        return NULL;
    }
}

PyObject *py_await(PyObject *awaitable) {
    if (awaitable == NULL) {
        return coroutine_require_result(
            NULL,
            "py_await",
            "py_await received NULL awaitable"
        );
    }
    if (!PY_IS_TAGGED_INT(awaitable) && py_type_of(awaitable) == PY_TYPE_COROUTINE) {
        return py_coroutine_run(awaitable);
    }
    if (!PY_IS_TAGGED_INT(awaitable) && py_type_of(awaitable) == PY_TYPE_GEN) {
        return await_iterator(awaitable);
    }
    PyObject *method = py_obj_getattr(awaitable, "__await__");
    if (method != NULL) {
        PyObject *args = py_tuple_new(0);
        if (args == NULL) {
            coroutine_require_result(
                NULL,
                "py_tuple_new",
                "__await__ could not allocate its argument tuple"
            );
            py_decref(method);
            return NULL;
        }
        PyObject *iter = py_obj_call(method, args, py_None);
        coroutine_require_result(
            iter,
            "__await__",
            "__await__ returned NULL without setting an exception"
        );
        py_decref(args);
        py_decref(method);
        if (iter == NULL) return NULL;
        PyObject *result = await_iterator(iter);
        py_decref(iter);
        return result;
    }
    py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not awaitable"));
    return NULL;
}

PyObject *py_asyncio_sleep(PyObject *delay) {
    (void)delay;
    /* Real virtual-thread suspension lowers through py_virtual_thread_sleep();
     * this coroutine shell remains the synchronous fallback path. */
    return py_coroutine_new_native("sleep", NULL, NULL, NULL);
}

static int64_t continuation_slot_count_from_map(const void *frame_map) {
    if (frame_map == NULL) return 0;
    int64_t n = (int64_t)(*((const int32_t *)frame_map));
    return n > 0 ? n : 0;
}

static PyObject **continuation_chunk_slots(PyContinuationObject *c) {
    if (c == NULL || c->stack_chunk == NULL) return NULL;
    return c->stack_chunk->slots;
}

static const void *continuation_chunk_frame_map(PyContinuationObject *c) {
    if (c == NULL || c->stack_chunk == NULL) return NULL;
    return &c->stack_chunk->root_map_slot_count;
}

static PyContinuationObject *checked_continuation(PyObject *cont) {
    if (cont == NULL || PY_IS_TAGGED_INT(cont)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a continuation"));
        return NULL;
    }
    cont = pcc_gc_note_relocation_read(cont);
    if (py_type_of(cont) != PY_TYPE_CONTINUATION) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a continuation"));
        return NULL;
    }
    return (PyContinuationObject *)cont;
}

PyObject *py_continuation_class(void) {
    static PyObject *cls = NULL;
    if (cls != NULL) return cls;
    cls = (PyObject *)py_class_new("continuation", NULL, 0, NULL, 0);
    if (cls != NULL) {
        py_header(cls)->flags |= PY_FLAG_IMMORTAL;
    }
    return cls;
}

static PyObject *py_continuation_new_with_abi(
    const void *frame_map,
    PyObject **slots,
    void *resume_pc,
    int64_t resume_abi
) {
    int64_t n_slots = continuation_slot_count_from_map(frame_map);
    if (n_slots > 0 && slots == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "continuation slots are null"));
        return NULL;
    }

    PyContinuationStackChunk *stack_chunk = (
        PyContinuationStackChunk *
    )calloc(1, sizeof(PyContinuationStackChunk));
    if (stack_chunk == NULL) return NULL;
    stack_chunk->root_map_slot_count = (int32_t)n_slots;
    stack_chunk->slot_count = n_slots;
    if (n_slots > 0) {
        stack_chunk->slots = (PyObject **)calloc((size_t)n_slots, sizeof(PyObject *));
        if (stack_chunk->slots == NULL) {
            free(stack_chunk);
            return NULL;
        }
    }

    PyContinuationObject *c = (PyContinuationObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyContinuationObject), PY_TYPE_CONTINUATION, 0
    );
    if (c == NULL) {
        free(stack_chunk->slots);
        free(stack_chunk);
        return NULL;
    }
    c->resume_pc = resume_pc;
    c->stack_chunk = stack_chunk;
    c->mounted = 1;
    c->resume_abi = resume_abi;
    if (stack_chunk->slots != NULL && n_slots > 0) {
        (void)pcc_gc_backend4_zpage_register_owner_payload_span(
            (PyObject *)c,
            stack_chunk->slots,
            n_slots * (int64_t)sizeof(PyObject *)
        );
    }
    for (int64_t i = 0; i < n_slots; i++) {
        pcc_gc_store_ptr((PyObject *)c, &stack_chunk->slots[i], slots[i]);
    }
    py_gc_track((PyObject *)c);
    pcc_gc_publish_initialized((PyObject *)c);
    if (py_continuation_unmount((PyObject *)c, NULL, resume_pc) != 0) {
        py_decref((PyObject *)c);
        return NULL;
    }
    return (PyObject *)c;
}

PyObject *py_continuation_new(
    const void *frame_map,
    PyObject **slots,
    void *resume_pc
) {
    return py_continuation_new_with_abi(
        frame_map,
        slots,
        resume_pc,
        PCC_CONTINUATION_RESUME_ABI_LEGACY_NOARG
    );
}

PyObject *py_continuation_new_typed(
    const void *frame_map,
    PyObject **slots,
    void *resume_pc
) {
    return py_continuation_new_with_abi(
        frame_map,
        slots,
        resume_pc,
        PCC_CONTINUATION_RESUME_ABI_VTHREAD
    );
}

int64_t py_continuation_mount(PyObject *cont, PyObject **slots_out) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL || c->stack_chunk == NULL) return -1;
    PyObject **slots = continuation_chunk_slots(c);
    int64_t n_slots = c->stack_chunk->slot_count;
    if (c->mounted == 0) {
        void *root_node = c->stack_chunk->root_node;
        c->stack_chunk->root_node = NULL;
        if (root_node == NULL) {
            pcc_gc_unregister_continuation_root(slots);
        } else {
            pcc_gc_unregister_continuation_root_node(root_node);
        }
    }
    if (slots_out != NULL) {
        for (int64_t i = 0; i < n_slots; i++) {
            pcc_gc_store_root(&slots_out[i], slots[i]);
        }
    }
    c->mounted = 1;
    return 0;
}

int64_t py_continuation_unmount(
    PyObject *cont,
    PyObject **slots_in,
    void *resume_pc
) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL || c->stack_chunk == NULL) return -1;
    PyObject **slots = continuation_chunk_slots(c);
    int64_t n_slots = c->stack_chunk->slot_count;
    if (slots_in != NULL) {
        for (int64_t i = 0; i < n_slots; i++) {
            pcc_gc_store_ptr((PyObject *)c, &slots[i], slots_in[i]);
        }
    }
    c->resume_pc = resume_pc;
    if (c->mounted != 0) {
        c->stack_chunk->root_node = pcc_gc_register_continuation_root_node(
            continuation_chunk_frame_map(c), slots
        );
    }
    c->mounted = 0;
    return 0;
}

int64_t py_continuation_is_mounted(PyObject *cont) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL) return 0;
    return c->mounted != 0 ? 1 : 0;
}

void *py_continuation_resume_pc(PyObject *cont) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL) return NULL;
    return c->resume_pc;
}

int64_t py_continuation_resume_abi(PyObject *cont) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL) return PCC_CONTINUATION_RESUME_ABI_LEGACY_NOARG;
    return c->resume_abi;
}

int64_t py_continuation_slot_count(PyObject *cont) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL || c->stack_chunk == NULL) return 0;
    return c->stack_chunk->slot_count;
}

PyObject *py_continuation_get_slot(PyObject *cont, int64_t index) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL || c->stack_chunk == NULL) return NULL;
    if (index < 0 || index >= c->stack_chunk->slot_count) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "continuation slot out of range"));
        return NULL;
    }
    PyObject *value = pcc_gc_load_ptr(cont, &c->stack_chunk->slots[index]);
    if (value == NULL) value = py_None;
    py_incref(value);
    return value;
}

int64_t py_continuation_set_slot(
    PyObject *cont,
    int64_t index,
    PyObject *value
) {
    PyContinuationObject *c = checked_continuation(cont);
    if (c == NULL || c->stack_chunk == NULL) return -1;
    if (index < 0 || index >= c->stack_chunk->slot_count) {
        py_raise_owned(py_exc_new(PY_EXC_INDEXERROR, "continuation slot out of range"));
        return -1;
    }
    pcc_gc_store_ptr((PyObject *)c, &c->stack_chunk->slots[index], value);
    return 0;
}

static PyTaskObject *checked_task(PyObject *task) {
    PyObject *original = task;
    if (task == NULL || PY_IS_TAGGED_INT(task)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a task"));
        return NULL;
    }
    task = pcc_gc_note_relocation_read(task);
    if (py_type_of(task) != PY_TYPE_TASK) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a task"));
        return NULL;
    }
    if (task != original && py_type_of(original) == PY_TYPE_TASK) {
        PyTaskObject *shadow = (PyTaskObject *)original;
        pcc_gc_store_ptr(original, &shadow->coro, NULL);
        pcc_gc_store_ptr(original, &shadow->result, NULL);
        pcc_gc_store_ptr(original, &shadow->waiter, NULL);
    }
    return (PyTaskObject *)task;
}

PyObject *py_task_new(PyObject *coro) {
    PyTaskObject *t = (PyTaskObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyTaskObject), PY_TYPE_TASK, 0
    );
    if (t == NULL) return NULL;
    t->coro = NULL;
    t->result = NULL;
    t->waiter = NULL;
    t->done = 0;
    pcc_gc_store_ptr((PyObject *)t, &t->coro, coro == NULL ? py_None : coro);
    py_gc_track((PyObject *)t);
    pcc_gc_publish_initialized((PyObject *)t);
    return (PyObject *)t;
}

PyObject *py_task_step(PyObject *task) {
    PyTaskObject *t = checked_task(task);
    if (t == NULL) return NULL;
    if (t->done != 0) {
        PyObject *result = pcc_gc_load_ptr(task, &t->result);
        if (result == NULL) result = py_None;
        py_incref(result);
        return result;
    }
    PyObject *coro = pcc_gc_load_ptr(task, &t->coro);
    PyObject *result = py_await(coro);
    if (result == NULL) return NULL;
    pcc_gc_store_ptr(task, &t->result, result);
    pcc_gc_store_ptr(task, &t->waiter, NULL);
    t->done = 1;
    return result;
}

int64_t py_task_is_done(PyObject *task) {
    PyTaskObject *t = checked_task(task);
    if (t == NULL) return 1;
    return t->done != 0 ? 1 : 0;
}

void py_task_set_result(PyObject *task, PyObject *result) {
    PyTaskObject *t = checked_task(task);
    if (t == NULL) return;
    pcc_gc_store_ptr(task, &t->result, result == NULL ? py_None : result);
    pcc_gc_store_ptr(task, &t->waiter, NULL);
    t->done = 1;
}

void py_task_set_waiter(PyObject *task, PyObject *waiter) {
    PyTaskObject *t = checked_task(task);
    if (t == NULL) return;
    pcc_gc_store_ptr(task, &t->waiter, waiter);
}

PyObject *py_task_get_coro(PyObject *task) {
    PyTaskObject *t = checked_task(task);
    if (t == NULL) return NULL;
    PyObject *coro = pcc_gc_load_ptr(task, &t->coro);
    if (coro == NULL) coro = py_None;
    py_incref(coro);
    return coro;
}

PyObject *py_task_get_result(PyObject *task) {
    PyTaskObject *t = checked_task(task);
    if (t == NULL) return NULL;
    PyObject *result = pcc_gc_load_ptr(task, &t->result);
    if (result == NULL) result = py_None;
    py_incref(result);
    return result;
}

PyObject *py_task_get_waiter(PyObject *task) {
    PyTaskObject *t = checked_task(task);
    if (t == NULL) return NULL;
    PyObject *waiter = pcc_gc_load_ptr(task, &t->waiter);
    if (waiter == NULL) waiter = py_None;
    py_incref(waiter);
    return waiter;
}

void py_dealloc_task(PyObject *o) {
    PyTaskObject *t = (PyTaskObject *)o;
    PyObject *coro = pcc_gc_load_ptr(o, &t->coro);
    PyObject *result = pcc_gc_load_ptr(o, &t->result);
    PyObject *waiter = pcc_gc_load_ptr(o, &t->waiter);
    if (coro != NULL) py_decref(coro);
    if (result != NULL) py_decref(result);
    if (waiter != NULL) py_decref(waiter);
    pcc_gc_free_object_memory(o);
}

void py_dealloc_coroutine(PyObject *o) {
    PyCoroutineObject *c = (PyCoroutineObject *)o;
    PyObject *captures = pcc_gc_load_ptr(o, &c->captures);
    PyObject *args = pcc_gc_load_ptr(o, &c->args);
    PyObject *result = pcc_gc_load_ptr(o, &c->result);
    if (captures != NULL) py_decref(captures);
    if (args != NULL) py_decref(args);
    if (result != NULL) py_decref(result);
    pcc_gc_free_object_memory(o);
}

void py_dealloc_continuation(PyObject *o) {
    PyContinuationObject *c = (PyContinuationObject *)o;
    PyContinuationStackChunk *stack_chunk = c->stack_chunk;
    if (stack_chunk != NULL) {
        if (c->mounted == 0) {
            void *root_node = stack_chunk->root_node;
            stack_chunk->root_node = NULL;
            if (root_node == NULL) {
                pcc_gc_unregister_continuation_root(stack_chunk->slots);
            } else {
                pcc_gc_unregister_continuation_root_node(root_node);
            }
        }
        if (stack_chunk->slots != NULL) {
            for (int64_t i = 0; i < stack_chunk->slot_count; i++) {
                PyObject *slot = pcc_gc_load_ptr(o, &stack_chunk->slots[i]);
                if (slot != NULL) py_decref(slot);
            }
            free(stack_chunk->slots);
        }
        free(stack_chunk);
    }
    c->stack_chunk = NULL;
    pcc_gc_free_object_memory(o);
}
