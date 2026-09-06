/* pcc/py_runtime/src/py_gen.c
 *
 * Native generator shell for pcc-compiled Python. The frontend owns the
 * generator state machine; the runtime only stores the resume thunk, frame
 * object, and current state.
 */

#include "py_internal.h"
#include <stdlib.h>


static PyObject *gen_require_result(
    PyObject *result,
    const char *helper_name,
    const char *message
) {
    if (result == NULL) {
        py_runtime_error_if_unset(helper_name, message);
    }
    return result;
}


PyObject *py_gen_frame_new(int64_t slot_count) {
    if (slot_count < 0 || slot_count > 134217728) {
        return gen_require_result(NULL, "py_gen_frame_new", "invalid generator frame size");
    }
    PyListObject *frame = (PyListObject *)pcc_gc_alloc(sizeof(PyListObject), PY_TYPE_LIST, 0);
    if (frame == NULL) {
        return gen_require_result(NULL, "pcc_gc_alloc", "generator frame allocation failed");
    }
    int64_t capacity = slot_count < 4 ? 4 : slot_count;
    frame->length = 0;
    frame->capacity = capacity;
    frame->items = NULL;
    frame->items = (PyObject **)malloc((size_t)capacity * sizeof(PyObject *));
    if (frame->items == NULL) {
        py_decref((PyObject *)frame);
        return gen_require_result(NULL, "malloc", "generator frame slots allocation failed");
    }
    for (int64_t index = 0; index < slot_count; index++) frame->items[index] = py_None;
    frame->length = slot_count;
    (void)pcc_gc_backend4_zpage_register_owner_payload_span(
        (PyObject *)frame, frame->items, capacity * (int64_t)sizeof(PyObject *)
    );
    py_gc_track((PyObject *)frame);
    pcc_gc_publish_initialized((PyObject *)frame);
    return (PyObject *)frame;
}

PyObject *py_gen_new(void *resume, PyObject *frame) {
    if (resume == NULL || frame == NULL) {
        return gen_require_result(
            NULL,
            "py_gen_new",
            "generator construction received a NULL resume thunk or frame"
        );
    }
    PyGenObject *g = (PyGenObject *)pcc_gc_alloc(
        (int64_t)sizeof(PyGenObject), PY_TYPE_GEN, 0
    );
    if (g == NULL) {
        return gen_require_result(
            NULL,
            "pcc_gc_alloc",
            "generator construction could not allocate generator state"
        );
    }
    g->resume = (PyNativeGenResume)resume;
    g->frame = NULL;
    g->state = 0;
    g->done = 0;
    g->send_value = NULL;
    pcc_gc_store_ptr((PyObject *)g, &g->frame, frame);
    pcc_gc_store_ptr((PyObject *)g, &g->send_value, py_None);
    py_gc_track((PyObject *)g);
    pcc_gc_publish_initialized((PyObject *)g);
    return (PyObject *)g;
}


void py_dealloc_gen(PyObject *o) {
    PyGenObject *g = (PyGenObject *)o;
    PyObject *frame = pcc_gc_load_ptr(o, &g->frame);
    PyObject *send_value = pcc_gc_load_ptr(o, &g->send_value);
    if (frame != NULL) py_decref(frame);
    if (send_value != NULL) py_decref(send_value);
    pcc_gc_free_object_memory(o);
}


static PyGenObject *checked_gen(PyObject *gen) {
    if (gen == NULL || PY_IS_TAGGED_INT(gen)) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a generator"));
        return NULL;
    }
    PyObjectHeader *h = py_header(gen);
    if (h->type_tag != PY_TYPE_GEN) {
        py_raise_owned(py_exc_new(PY_EXC_TYPEERROR, "object is not a generator"));
        return NULL;
    }
    return (PyGenObject *)gen;
}


void py_gen_set_may_park(PyObject *gen) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return;
    py_header_flags_or(&g->h, PY_FLAG_GEN_MAY_PARK);
}


int64_t py_gen_is_may_park(PyObject *gen) {
    if (gen == NULL || PY_IS_TAGGED_INT(gen)) return 0;
    PyObjectHeader *h = py_header(gen);
    if (h->type_tag != PY_TYPE_GEN) return 0;
    return (py_header_flags_load(h) & PY_FLAG_GEN_MAY_PARK) != 0 ? 1 : 0;
}


static int py_gen_set_send_value(PyGenObject *g, PyObject *value) {
    if (value == NULL) value = py_None;
    pcc_gc_store_ptr((PyObject *)g, &g->send_value, value);
    return 0;
}


int64_t py_gen_state(PyObject *gen) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return -1;
    return g->state;
}


void py_gen_set_state(PyObject *gen, int64_t state) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return;
    g->state = state;
}


void py_gen_set_done(PyObject *gen) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return;
    g->done = 1;
}


int64_t py_gen_is_done(PyObject *gen) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return 1;
    return g->done != 0 ? 1 : 0;
}


PyObject *py_gen_finish(PyObject *gen, PyObject *value) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return NULL;
    g->done = 1;
    if (value == NULL) value = py_None;
    PyObject *stop = py_exc_new_with_value(PY_EXC_STOPITERATION, value);
    if (stop == NULL) {
        return gen_require_result(
            NULL,
            "py_exc_new_with_value",
            "generator finish could not allocate StopIteration"
        );
    }
    py_raise(stop);
    py_decref(stop);
    return NULL;
}


static PyObject *completed_resume(PyObject *gen, PyObject *value) {
    if (py_current_exception() != NULL) {
        py_gen_set_done(gen);
        return NULL;
    }
    return py_gen_finish(gen, value);
}


PyObject *py_gen_completed(PyObject *value) {
    if (value == NULL) value = py_None;
    PyObject *gen = py_gen_new((void *)completed_resume, value);
    if (gen != NULL) py_gen_set_may_park(gen);
    return gen;
}


PyObject *py_gen_next(PyObject *gen) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return NULL;
    if (g->done != 0 || g->resume == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_STOPITERATION, ""));
        return NULL;
    }
    py_gen_set_send_value(g, py_None);
    PyObject *frame = pcc_gc_load_ptr((PyObject *)g, &g->frame);
    return gen_require_result(
        g->resume(gen, frame),
        "py_gen_next",
        "generator resume returned NULL without StopIteration or an exception"
    );
}


PyObject *py_gen_send(PyObject *gen, PyObject *value) {
    if (gen != NULL && !PY_IS_TAGGED_INT(gen)
        && py_type_of(gen) == PY_TYPE_COROUTINE) {
        if (value != NULL && value != py_None) {
            py_raise_owned(py_exc_new(
                PY_EXC_TYPEERROR,
                "can't send non-None value to a just-started coroutine"
            ));
            return NULL;
        }
        return gen_require_result(
            py_coroutine_run(gen),
            "py_coroutine_run",
            "coroutine send returned NULL without setting an exception"
        );
    }
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return NULL;
    if (g->done != 0 || g->resume == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_STOPITERATION, ""));
        return NULL;
    }
    if (g->state == 0 && value != NULL && value != py_None) {
        py_raise_owned(py_exc_new(
            PY_EXC_TYPEERROR,
            "can't send non-None value to a just-started generator"
        ));
        return NULL;
    }
    py_gen_set_send_value(g, value);
    PyObject *frame = pcc_gc_load_ptr((PyObject *)g, &g->frame);
    return gen_require_result(
        g->resume(gen, frame),
        "py_gen_send",
        "generator send returned NULL without StopIteration or an exception"
    );
}


PyObject *py_gen_throw(PyObject *gen, PyObject *exc) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return NULL;
    if (g->done != 0 || g->resume == NULL) {
        py_raise_owned(py_exc_new(PY_EXC_STOPITERATION, ""));
        return NULL;
    }
    py_gen_set_send_value(g, py_None);
    py_raise(exc);
    PyObject *frame = pcc_gc_load_ptr((PyObject *)g, &g->frame);
    return gen_require_result(
        g->resume(gen, frame),
        "py_gen_throw",
        "generator throw returned NULL without setting an exception"
    );
}


PyObject *py_gen_close(PyObject *gen) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return NULL;
    if (g->done == 0 && g->resume != NULL) {
        /* Closing runs user ``finally`` code.  That code may allocate and
         * safepoint, so neither the generator nor the identity-bearing
         * GeneratorExit may remain as an untracked C local under GC4. */
        PyObject *rooted_gen = NULL;
        PyObject *injected = NULL;
        void *gen_root = pcc_gc_scheduler_root_register_handle(&rooted_gen);
        if (gen_root == NULL) {
            py_raise_owned(py_exc_new(
                PY_EXC_MEMORYERROR,
                "could not root generator during close"
            ));
            return NULL;
        }
        pcc_gc_store_root(&rooted_gen, (PyObject *)g);
        void *exc_root = pcc_gc_scheduler_root_register_handle(&injected);
        if (exc_root == NULL) {
            pcc_gc_store_root(&rooted_gen, NULL);
            pcc_gc_scheduler_root_unregister_handle(gen_root);
            py_raise_owned(py_exc_new(
                PY_EXC_MEMORYERROR,
                "could not root GeneratorExit during close"
            ));
            return NULL;
        }
        PyObject *exc = py_exc_new(PY_EXC_BASE, "GeneratorExit");
        if (exc == NULL) {
            pcc_gc_store_root(&injected, NULL);
            pcc_gc_scheduler_root_unregister_handle(exc_root);
            pcc_gc_store_root(&rooted_gen, NULL);
            pcc_gc_scheduler_root_unregister_handle(gen_root);
            return NULL;
        }
        pcc_gc_store_root(&injected, exc);
        g = (PyGenObject *)rooted_gen;
        py_gen_set_send_value(g, py_None);
        py_raise(injected);
        py_decref(injected);  /* TLS and the updateable root now own it. */
        g = (PyGenObject *)rooted_gen;
        PyObject *frame = pcc_gc_load_ptr((PyObject *)g, &g->frame);
        PyNativeGenResume resume = g->resume;
        PyObject *result = resume(rooted_gen, frame);
        if (result != NULL) {
            py_decref(result);
            g = (PyGenObject *)rooted_gen;
            g->done = 1;
            pcc_gc_store_root(&injected, NULL);
            pcc_gc_scheduler_root_unregister_handle(exc_root);
            pcc_gc_store_root(&rooted_gen, NULL);
            pcc_gc_scheduler_root_unregister_handle(gen_root);
            py_raise_owned(py_exc_new(
                PY_EXC_RUNTIMEERROR,
                "generator ignored GeneratorExit"
            ));
            return NULL;
        }
        if (!py_err_occurred()) {
            gen_require_result(
                NULL,
                "py_gen_close",
                "generator close resume returned NULL without setting an exception"
            );
        }
        PyObject *cur = py_current_exception();
        PyObject *stop_cls = (PyObject *)py_exc_builtin_class(
            PY_EXC_STOPITERATION
        );
        int injected_propagated = cur == injected;
        int stopped = !injected_propagated && py_exc_matches(cur, stop_cls);
        g = (PyGenObject *)rooted_gen;
        if (stopped) {
            g->done = 1;
            py_clear_exception();
        } else if (injected_propagated) {
            /* Our injected GeneratorExit propagated back unhandled:
             * that IS the normal close path in CPython — swallow it.
             * (Previously it stayed pending and detonated at the next
             * err check.) Any OTHER exception raised by the generator
             * body keeps propagating. */
            g->done = 1;
            py_clear_exception();
        } else {
            /* Any exception escaping the generator terminates it, including
             * an error raised by synchronous cancellation cleanup. */
            g->done = 1;
            pcc_gc_store_root(&injected, NULL);
            pcc_gc_scheduler_root_unregister_handle(exc_root);
            pcc_gc_store_root(&rooted_gen, NULL);
            pcc_gc_scheduler_root_unregister_handle(gen_root);
            return NULL;
        }
        pcc_gc_store_root(&injected, NULL);
        pcc_gc_scheduler_root_unregister_handle(exc_root);
        pcc_gc_store_root(&rooted_gen, NULL);
        pcc_gc_scheduler_root_unregister_handle(gen_root);
    }
    py_incref(py_None);
    return py_None;
}


int64_t py_gen_close_preserving_exception(PyObject *gen) {
    /* ``yield from``-style virtual-thread delegation must close the active
     * child before the parent continues unwinding.  Run child close with a
     * clean TLS slot, then restore the parent's exact exception object.  The
     * updateable root keeps that object live and relocation-safe while child
     * cleanup allocates or reaches safepoints. */
    PyObject *rooted_gen = NULL;
    void *gen_root = pcc_gc_scheduler_root_register_handle(&rooted_gen);
    if (gen_root == NULL) return -1;
    pcc_gc_store_root(&rooted_gen, gen);
    PyObject *borrowed = py_current_exception();
    PyObject *saved = NULL;
    void *saved_root = NULL;
    if (borrowed != NULL) {
        saved_root = pcc_gc_scheduler_root_register_handle(&saved);
        if (saved_root == NULL) {
            pcc_gc_store_root(&rooted_gen, NULL);
            pcc_gc_scheduler_root_unregister_handle(gen_root);
            return -1;
        }
        pcc_gc_store_root(&saved, borrowed);
        py_clear_exception();
    }

    PyObject *closed = py_gen_close(rooted_gen);
    if (closed != NULL) {
        py_decref(closed);
        if (saved != NULL) py_raise(saved);
        if (saved_root != NULL) {
            pcc_gc_store_root(&saved, NULL);
            pcc_gc_scheduler_root_unregister_handle(saved_root);
        }
        pcc_gc_store_root(&rooted_gen, NULL);
        pcc_gc_scheduler_root_unregister_handle(gen_root);
        return 0;
    }

    PyObject *cleanup_error = py_current_exception();
    if (
        cleanup_error != NULL
        && saved != NULL
        && cleanup_error != saved
    ) {
        py_exc_set_context(cleanup_error, saved);
    }
    if (saved_root != NULL) {
        pcc_gc_store_root(&saved, NULL);
        pcc_gc_scheduler_root_unregister_handle(saved_root);
    }
    pcc_gc_store_root(&rooted_gen, NULL);
    pcc_gc_scheduler_root_unregister_handle(gen_root);
    return -1;
}


PyObject *py_gen_take_send(PyObject *gen) {
    PyGenObject *g = checked_gen(gen);
    if (g == NULL) return NULL;
    PyObject *value = pcc_gc_load_ptr((PyObject *)g, &g->send_value);
    if (value == NULL) value = py_None;
    py_incref(value);
    pcc_gc_store_ptr((PyObject *)g, &g->send_value, py_None);
    return value;
}
