# pcc/runtime/Makefile
#
# Build libpy_runtime_pcc_py.a, the runtime of pcc's Python frontend: every
# module is pcc-Python under py/, compiled by pcc itself.
#
# Public header:  include/py_runtime.h (C ABI for harnesses and extensions)
# Source:         py/*.py
# Output:         libpy_runtime_pcc_py.a (in this directory)
#
# Usage:
#   make                                   - build libpy_runtime_pcc_py.a
#   make libpy_runtime_pcc_py_libpython.a  - add the CPython bridge object
#                                            (src/py_libpython.c) for
#                                            --python-libpython builds
#   make clean                             - remove objects + archives

.DEFAULT_GOAL := all

CC       ?= cc
AR       ?= ar
RANLIB   ?= ranlib
# Cross builds must select symbol inspection for the same object target.
NM       ?= nm
PCC      ?= pcc
PYTHON   ?= python3
PCC_IR_TO_OBJ ?= $(PYTHON) -m pcc.tools.ir_to_obj
# ``pcc --emit-llvm`` applies the default tier (mem2reg,sroa) and
# ``ir_to_obj`` documents that it takes already-optimized IR, so nothing ran
# the rest of the owned pipeline over the runtime: the archive every compiled
# program links shipped un-inlined.  ``py_obj.ll`` alone loses 8.8% of its
# lines here and every call to ``_ptr_can_have_header`` -- a one-line wrapper
# around ``pcc_gc_pointer_is_managed`` that a C=100 gateway profile put at
# 1,788 samples.  Same list ``benchmarks/build_runtime_variants.py`` uses,
# which measured +12.3% on that arm.
PCC_RUNTIME_IR_PASSES ?= mem2reg,sroa,instsimplify,inline-defined,instsimplify,instcombine,dce
PCC_REPO_ROOT ?= $(abspath ../..)

CFLAGS   ?= -O2 -fPIC -Wall -Wextra -std=c11
CPPFLAGS ?=
INCLUDES  = -Iinclude
# Every runtime is threaded; the single-thread kernel was removed.
PCC_WITH_THREADS ?= 1
PCC_REFCOUNT_KIND ?= atomic
# Bind the same effective settings at frontend compilation and receipt emission.
# Make defaults are not exported automatically; command-line overrides still win.
export PCC_WITH_THREADS PCC_REFCOUNT_KIND
PCC_RUNTIME_CONFIG_ARGS = --runtime-threads "$(PCC_WITH_THREADS)" --runtime-refcount "$(PCC_REFCOUNT_KIND)"
RUNTIME_HEADERS = Makefile include/py_runtime.h $(SRCDIR)/py_internal.h $(SRCDIR)/py_timer_heap.h $(SRCDIR)/py_io_waitset.h

# Linux/glibc: -std=c11 defines __STRICT_ANSI__, which switches off glibc's
# default feature set and hides POSIX/BSD declarations the CPython bridge
# uses. Darwin's headers declare these unconditionally and need nothing.
ifneq ($(shell uname),Darwin)
CPPFLAGS += -D_DEFAULT_SOURCE
endif

ifneq ($(PCC_WITH_THREADS),1)
$(error PCC_WITH_THREADS=$(PCC_WITH_THREADS): the single-thread runtime was removed; every runtime is threaded)
endif
CPPFLAGS += -DPCC_WITH_THREADS=1
CFLAGS   += -pthread

SRCDIR       = src
PYDIR        = py
OBJDIR_PY    = build_py
OBJDIR_LIBPY = build_libpython

# Runtime modules under py/ that use the managed object model.
PY_MODULES = py_cleanup_runtime py_file_lock py_tuple py_tuple_slice py_obj_stubs py_exc_tls py_exc_objects py_exc_match py_exc_table py_exc_traceback py_obj_gc py_gc_telemetry py_gc_backend py_set py_os_env py_os_system py_os_path py_os_substrate py_process py_process_substrate py_process_timeout py_print_sys py_print_fmt py_format_runtime py_obj py_obj_dealloc py_dict py_list py_list_set_slice py_iter py_gen py_coroutine py_context_runtime py_call_splat_runtime py_module_attrs_runtime py_compiled_module_runtime py_dlpack_runtime py_runtime_log py_json_runtime py_pickle_copy_runtime py_protocol_runtime py_extension_loader_runtime py_hash_runtime py_http_runtime py_asyncio_io_runtime py_re_engine_runtime freestanding_io_waitset py_func py_re py_dunder py_weakref py_file py_threading py_class py_obj_ops_dispatch py_obj_ops_slice py_obj_ops_set_slice py_obj_ops_mod py_obj_ops_compare py_str py_str_accessors py_str_slice py_int_core py_int_decimal py_int_shift py_int_bitwise py_int_addsub py_int_mul py_int_bigint_pow py_int_ops py_int py_int_parse py_int_convert py_int_bigint_convert py_substrate
PY_MODULES += py_tempfile py_os_mkdir py_os_walk
PY_MODULES += py_recursion_runtime
PY_MODULES += py_binary_repeat_runtime
PY_MODULES += py_virtual_thread_runtime freestanding_runtime_debug py_capi_core_runtime py_capi_memory_runtime py_capi_stdio_runtime py_capi_numeric_runtime py_capi_collections_runtime py_capi_exc_runtime py_capi_dict_runtime py_capi_object_runtime py_capi_type_runtime py_capi_unicode_runtime py_capi_capsule_runtime py_capi_set_runtime py_capi_misc_runtime py_capi_cfunction_runtime py_capi_object_call_runtime py_capi_method_bridge_runtime py_capi_object_attr_runtime py_capi_number_runtime py_capi_sequence_runtime py_capi_mapping_runtime py_capi_import_runtime py_capi_unicode_search_runtime py_capi_unicode_writer_runtime py_capi_buffer_runtime py_capi_module_runtime py_capi_cext_runtime py_capi_module_state_runtime py_capi_private_runtime py_capi_slice_runtime py_capi_seqiter_runtime py_capi_contextvar_runtime py_capi_call_runtime py_capi_arg_runtime py_capi_str_conv_runtime py_capi_buildvalue_runtime py_capi_type_descriptor_runtime py_capi_visit_runtime
PY_MODULES += freestanding_thread_kernel_pthread

OBJS_PY  = $(patsubst %,$(OBJDIR_PY)/%.o,$(PY_MODULES))
# Raw C-ABI modules are pcc-Python too, but their strict freestanding
# contract forbids managed-runtime references, so they are listed apart.
FREESTANDING_PY_MODULES = freestanding_mem_str freestanding_lfq_spsc freestanding_iobuf_pool freestanding_uring freestanding_libc_numeric freestanding_strtoint freestanding_re_engine freestanding_allocator freestanding_timer_heap freestanding_runtime_high_substrate freestanding_gc_index_table freestanding_gc_external_resource freestanding_gc_state freestanding_gc_control freestanding_gc_tracking freestanding_gc_root_registry freestanding_gc_frame_registry freestanding_gc_root_operations freestanding_gc_mapped_roots freestanding_gc_object_root_seeding freestanding_gc_refcount_roots freestanding_gc_object_slots freestanding_gc_common_mark_cycle freestanding_gc_backend0_slots freestanding_gc_sweep_slots freestanding_gc_tracing_sweep_collector freestanding_gc_public_collection freestanding_gc_incremental_concurrent_scheduler freestanding_gc_forwarding_identity freestanding_gc_object_nodes freestanding_gc_generational_oldification freestanding_gc_generational_remembered_owners freestanding_gc_generational_promotion freestanding_gc_generational_scheduler freestanding_gc_relocation_remap freestanding_gc_relocation_payload freestanding_gc_relocation_copy freestanding_gc_relocation_selector freestanding_gc_relocation_drain freestanding_gc_zpage_mechanics freestanding_gc_zpage_lifecycle freestanding_gc_forwarding_retirement freestanding_gc_barrier_dispatcher freestanding_gc_zpage_allocation freestanding_gc_backend0_collector freestanding_gc_root_introspection freestanding_platform_io freestanding_platform_fs freestanding_platform_env freestanding_platform_system freestanding_platform_time freestanding_platform_process freestanding_platform_socket freestanding_platform_rss freestanding_errno freestanding_metal_runtime freestanding_stdio
FREESTANDING_PY_MODULES += freestanding_class_namespace
FREESTANDING_PY_MODULES += freestanding_sequence_snapshot
FREESTANDING_PY_MODULES += freestanding_gateway_control
FREESTANDING_PY_MODULES += freestanding_time_calendar
FREESTANDING_OBJS_PY = $(patsubst %,$(OBJDIR_PY)/%.o,$(FREESTANDING_PY_MODULES))
# Keep the explicit host/Make reference builder on the same target-specific
# membership contract as the in-process owned builder. In particular Linux
# startup/libc and optional platform thread modules are not in the common
# source lists above. The helper reads those literal lists without running Make.
PCC_RUNTIME_INVENTORY = PYTHONPATH="$(PCC_REPO_ROOT)" $(PYTHON) -m pcc.tools.runtime_module_inventory --runtime-root "$(CURDIR)"
PCC_RUNTIME_TARGET ?= $(shell $(PCC_RUNTIME_INVENTORY) --field target)
PCC_RUNTIME_TARGET := $(PCC_RUNTIME_TARGET)
ifeq ($(strip $(PCC_RUNTIME_TARGET)),)
$(error cannot determine the runtime target)
endif
PCC_RUNTIME_MODULES := $(shell $(PCC_RUNTIME_INVENTORY) --target "$(PCC_RUNTIME_TARGET)" --threads "$(PCC_WITH_THREADS)")
ifeq ($(strip $(PCC_RUNTIME_MODULES)),)
$(error cannot determine the target runtime module inventory)
endif
PCC_PY_OBJECTS = $(patsubst %,$(OBJDIR_PY)/%.o,$(PCC_RUNTIME_MODULES))
PCC_PY_RECEIPTS = $(addsuffix .provenance.json,$(PCC_PY_OBJECTS))

LIB_PCC_PY = libpy_runtime_pcc_py.a
LIB_PCC_PY_LIBPYTHON = libpy_runtime_pcc_py_libpython.a
PCC_RUNTIME_ARCHIVE_LOCK_HELD ?= 0
PCC_RUNTIME_ARCHIVE_LOCK_MAX_WAITS ?= 3000
PCC_RUNTIME_ARCHIVE_LOCK_OWNER_GRACE_WAITS ?= 50
PCC_RUNTIME_ARCHIVE_LOCK_SLEEP ?= 0.1
PCC_RUNTIME_ARCHIVE_MAKEFILE := $(abspath $(firstword $(MAKEFILE_LIST)))
# Every archive owns the threaded pthread-ABI kernel; the pcc-Python
# virtual-thread scheduler and debug guards run on it.

.PHONY: all clean distclean _pcc_runtime_archive_lock_force

all: $(LIB_PCC_PY)

# pcc emits library-shaped LLVM IR for each py/ module without synthesizing
# a program @main, then ir_to_obj lowers it to a relocatable object; the
# archive is built directly from those objects.
# Build into a temp archive and atomically rename into place. A plain
# `rm $@; ar rcs $@` leaves a window where $@ is absent/partial; a concurrent
# linker (e.g. another xdist worker's clang, which holds no build lock) then
# fails with "no such file: libpy_runtime_pcc_py.a". `mv` is an atomic rename,
# so a linker always opens the old-or-new complete archive, never a gap.
#
# Direct Make and head-truth invocations do not pass through pipeline.py's
# runtime-build lock.  The public target therefore acquires its own lock before
# recursively entering the real object/receipt dependency graph.  Acquiring a
# lock only in the archive recipe is too late: Make builds prerequisites first,
# so two processes could still overwrite the same .ll/.o/receipt files.  Keep
# this lock distinct from the pipeline lock: a pipeline-owned build nests this
# target while already holding that lock.
# The verified provenance manifest binds the exact C-API sidecar bytes and
# remains the final publication/completion marker after both payload files.
ifneq ($(PCC_RUNTIME_ARCHIVE_LOCK_HELD),1)
$(LIB_PCC_PY): _pcc_runtime_archive_lock_force
	+@set -eu; \
	lock="$@.build.lock"; \
	owner="$$lock/owner"; \
	waited=0; \
	ownerless_waited=0; \
	while :; do \
		if mkdir "$$lock" 2>/dev/null; then \
			if ! printf '%s\n' "$$$$" > "$$owner"; then \
				rm -f "$$owner"; \
				rmdir "$$lock" 2>/dev/null || :; \
				exit 1; \
			fi; \
			break; \
		fi; \
		owner_pid=; \
		if [ -r "$$owner" ]; then \
			IFS= read -r owner_pid < "$$owner" || owner_pid=; \
		fi; \
		owner_valid=0; \
		owner_alive=0; \
		case "$$owner_pid" in \
			''|*[!0-9]*) ;; \
			*) owner_valid=1; kill -0 "$$owner_pid" 2>/dev/null && owner_alive=1 || : ;; \
		esac; \
		if [ "$$owner_valid" -eq 1 ] && [ "$$owner_alive" -eq 0 ]; then \
			rm -f "$$owner"; \
			if rmdir "$$lock" 2>/dev/null; then \
				ownerless_waited=0; \
				continue; \
			fi; \
		elif [ "$$owner_valid" -eq 0 ]; then \
			ownerless_waited=$$((ownerless_waited + 1)); \
			if [ "$$ownerless_waited" -ge "$(PCC_RUNTIME_ARCHIVE_LOCK_OWNER_GRACE_WAITS)" ]; then \
				rm -f "$$owner"; \
				if rmdir "$$lock" 2>/dev/null; then \
					ownerless_waited=0; \
					continue; \
				fi; \
			fi; \
		else \
			ownerless_waited=0; \
		fi; \
		waited=$$((waited + 1)); \
		if [ "$$waited" -ge "$(PCC_RUNTIME_ARCHIVE_LOCK_MAX_WAITS)" ]; then \
			echo "timed out waiting for pcc runtime archive lock: $$lock (owner=$${owner_pid:-unknown})" >&2; \
			exit 124; \
		fi; \
		sleep "$(PCC_RUNTIME_ARCHIVE_LOCK_SLEEP)"; \
	done; \
	cleanup_runtime_archive_lock() { \
		rm -f "$$owner"; \
		rmdir "$$lock" 2>/dev/null || :; \
	}; \
	trap cleanup_runtime_archive_lock 0; \
	trap 'exit 129' 1; \
	trap 'exit 130' 2; \
	trap 'exit 143' 15; \
	$(MAKE) --no-print-directory -f "$(PCC_RUNTIME_ARCHIVE_MAKEFILE)" PCC_RUNTIME_ARCHIVE_LOCK_HELD=1 "$@"
else
$(LIB_PCC_PY): $(PCC_PY_OBJECTS) $(PCC_PY_RECEIPTS)
	@set -eu; \
	rm -f "$@.tmp"; \
	rm -f "$@.capi_syms.nm.tmp"; \
	rm -f "$@.capi_syms.tmp"; \
	rm -f "$@.provenance.json.tmp"; \
	$(AR) rcs $@.tmp $(PCC_PY_OBJECTS); \
	$(RANLIB) "$@.tmp"; \
	$(NM) -g "$@.tmp" > "$@.capi_syms.nm.tmp" 2>/dev/null; \
	awk '$$2!="U" && $$2 ~ /^[A-Z]$$/ && ($$3 ~ /^_?Py/ || $$3 ~ /^_?_Py/) {print $$3}' "$@.capi_syms.nm.tmp" | LC_ALL=C sort -u > "$@.capi_syms.tmp"; \
	rm -f "$@.capi_syms.nm.tmp"; \
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.tools.runtime_archive_provenance assemble --archive "$@.tmp" --runtime-root $(CURDIR) --output "$@.provenance.json.tmp" --capi-inventory "$@.capi_syms.tmp" --ar $(AR) $(PCC_PY_OBJECTS); \
	mv -f "$@.tmp" "$@"; \
	mv -f "$@.capi_syms.tmp" "$@.capi_syms"; \
	mv -f "$@.provenance.json.tmp" "$@.provenance.json"
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.tools.runtime_archive_provenance stamp-target --archive "$@"
endif

# Compatibility fallback archive for bootstrap builds that still emit
# py_cpy_* calls. It keeps the pcc-Python runtime replacement archive
# as the base and adds only the CPython bridge object.
# Preserve pcc-native C-API objects for internal object-model semantics.  The
# final link makes their public Py*/_Py* symbols non-exported so a dlopen'd
# CPython extension resolves those names from libpython instead.  The
# pcc-native extension loader is a distinct ABI and is replaced by the
# bridge's fail-closed entrypoints in this mixed-object-model archive.
$(LIB_PCC_PY_LIBPYTHON): $(LIB_PCC_PY) $(OBJDIR_LIBPY)/py_libpython.o
	@set -eu; \
	rm -f "$@.tmp" "$@.capi_syms.nm.tmp" "$@.capi_syms.tmp" "$@.capi_syms"; \
	cp $(LIB_PCC_PY) "$@.tmp"; \
	$(AR) d "$@.tmp" py_extension_loader_runtime.o; \
	$(AR) r "$@.tmp" $(OBJDIR_LIBPY)/py_libpython.o; \
	$(RANLIB) "$@.tmp"; \
	$(NM) -g "$@.tmp" > "$@.capi_syms.nm.tmp" 2>/dev/null; \
	awk '$$2!="U" && $$2 ~ /^[A-Z]$$/ && ($$3 ~ /^_?Py/ || $$3 ~ /^_?_Py/) {print $$3}' "$@.capi_syms.nm.tmp" | LC_ALL=C sort -u > "$@.capi_syms.tmp"; \
	test -s "$@.capi_syms.nm.tmp"; \
	test -s "$@.capi_syms.tmp"; \
	rm -f "$@.capi_syms.nm.tmp"; \
	mv -f "$@.tmp" "$@"; \
	mv -f "$@.capi_syms.tmp" "$@.capi_syms"

$(OBJDIR_LIBPY)/py_libpython.o: $(SRCDIR)/py_libpython.c $(RUNTIME_HEADERS) | $(OBJDIR_LIBPY)
	$(CC) $(CPPFLAGS) $(CFLAGS) -DPCC_WITH_LIBPYTHON=1 $(shell python3-config --includes) $(INCLUDES) -c $< -o $@

$(OBJDIR_LIBPY):
	mkdir -p $(OBJDIR_LIBPY)

# Recompile a pcc-Python runtime module when that module changes. The archive
# target already tracks additions/removals through OBJS_PY/FREESTANDING_OBJS_PY;
# making every object depend on the entire Makefile turned any helper-list edit
# into a full runtime rebuild. Compiler/target/config invalidation is owned by
# pipeline._ensure_runtime's explicit full-rebuild decision.
$(OBJDIR_PY)/%.o: $(PYDIR)/%.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) $(PCC) --python-library --target=$(PCC_RUNTIME_TARGET) --emit-llvm=$(OBJDIR_PY)/$*.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.ir.optimization.driver "$(PCC_RUNTIME_IR_PASSES)" $(OBJDIR_PY)/$*.ll $(OBJDIR_PY)/$*.opt.ll
	mv $(OBJDIR_PY)/$*.opt.ll $(OBJDIR_PY)/$*.ll
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/$*.ll $@ --target=$(PCC_RUNTIME_TARGET) --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F) $(PCC_RUNTIME_CONFIG_ARGS)

# The receipt is a completion sidecar emitted with the object.  Give it an
# explicit dependency edge so deleting or losing a receipt cannot leave an
# apparently up-to-date object eligible for the production archive.  The
# recursive rebuild runs only for that recovery case; ordinary object builds
# publish both files in one emitter process.
# The recovery make goes through a plain variable: a literal $(MAKE) line is
# executed even under ``make -n``, which made every dry-run plan recurse once
# per runtime module (178 sub-makes, ~40 s) instead of printing the plan.
PCC_RECOVERY_MAKE = $(MAKE)
$(OBJDIR_PY)/%.o.provenance.json: $(OBJDIR_PY)/%.o
	test -f $@ || $(PCC_RECOVERY_MAKE) -B $(@D)/$*.o
	test -f $@

# This module implements pcc_thread_safepoint itself. Compiling it with the
# ordinary implicit-poll frontend setting would inject a function-entry
# call to pcc_thread_safepoint into pcc_thread_safepoint (and every helper),
# causing immediate unbounded recursion. Its blocking loops already contain
# the required explicit polls; disable automatic insertion for this one object.
$(OBJDIR_PY)/freestanding_thread_kernel_pthread.o: $(PYDIR)/freestanding_thread_kernel_pthread.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) PCC_IMPLICIT_THREAD_SAFEPOINTS=0 $(PCC) --python-library --target=$(PCC_RUNTIME_TARGET) --emit-llvm=$(OBJDIR_PY)/freestanding_thread_kernel_pthread.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/freestanding_thread_kernel_pthread.ll $@ --target=$(PCC_RUNTIME_TARGET) --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F) $(PCC_RUNTIME_CONFIG_ARGS)

# The logger's raw sink cannot suspend while it owns the sink lock. Suppress
# automatic entry/backedge polls, retaining explicit thread_safepoint calls
# in initialization and write-lock wait loops. Runtime provenance still uses
# the archive's actual thread/refcount configuration below.
$(OBJDIR_PY)/py_runtime_log.o: $(PYDIR)/py_runtime_log.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) PCC_IMPLICIT_THREAD_SAFEPOINTS=0 $(PCC) --python-library --target=$(PCC_RUNTIME_TARGET) --emit-llvm=$(OBJDIR_PY)/py_runtime_log.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.ir.optimization.driver "$(PCC_RUNTIME_IR_PASSES)" $(OBJDIR_PY)/py_runtime_log.ll $(OBJDIR_PY)/py_runtime_log.opt.ll
	mv $(OBJDIR_PY)/py_runtime_log.opt.ll $(OBJDIR_PY)/py_runtime_log.ll
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/py_runtime_log.ll $@ --target=$(PCC_RUNTIME_TARGET) --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F) $(PCC_RUNTIME_CONFIG_ARGS)

# TemporaryDirectory publishes raw NEW returns into explicit root frames.
# Automatic entry polls must not run before that publication. All allocating
# managed calls retain their ordinary runtime safepoints and slot leases.
$(OBJDIR_PY)/py_tempfile.o: $(PYDIR)/py_tempfile.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) PCC_IMPLICIT_THREAD_SAFEPOINTS=0 $(PCC) --python-library --target=$(PCC_RUNTIME_TARGET) --emit-llvm=$(OBJDIR_PY)/py_tempfile.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.ir.optimization.driver "$(PCC_RUNTIME_IR_PASSES)" $(OBJDIR_PY)/py_tempfile.ll $(OBJDIR_PY)/py_tempfile.opt.ll
	mv $(OBJDIR_PY)/py_tempfile.opt.ll $(OBJDIR_PY)/py_tempfile.ll
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/py_tempfile.ll $@ --target=$(PCC_RUNTIME_TARGET) --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F) $(PCC_RUNTIME_CONFIG_ARGS)

# mkdir uses the same explicit NEW-return/root publication discipline.
$(OBJDIR_PY)/py_os_mkdir.o: $(PYDIR)/py_os_mkdir.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) PCC_IMPLICIT_THREAD_SAFEPOINTS=0 $(PCC) --python-library --target=$(PCC_RUNTIME_TARGET) --emit-llvm=$(OBJDIR_PY)/py_os_mkdir.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.ir.optimization.driver "$(PCC_RUNTIME_IR_PASSES)" $(OBJDIR_PY)/py_os_mkdir.ll $(OBJDIR_PY)/py_os_mkdir.opt.ll
	mv $(OBJDIR_PY)/py_os_mkdir.opt.ll $(OBJDIR_PY)/py_os_mkdir.ll
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/py_os_mkdir.ll $@ --target=$(PCC_RUNTIME_TARGET) --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F) $(PCC_RUNTIME_CONFIG_ARGS)

# Exception-preserving cleanup must not add a poll before saving TLS.
$(OBJDIR_PY)/py_cleanup_runtime.o: $(PYDIR)/py_cleanup_runtime.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) PCC_IMPLICIT_THREAD_SAFEPOINTS=0 $(PCC) --python-library --target=$(PCC_RUNTIME_TARGET) --emit-llvm=$(OBJDIR_PY)/py_cleanup_runtime.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.ir.optimization.driver "$(PCC_RUNTIME_IR_PASSES)" $(OBJDIR_PY)/py_cleanup_runtime.ll $(OBJDIR_PY)/py_cleanup_runtime.opt.ll
	mv $(OBJDIR_PY)/py_cleanup_runtime.opt.ll $(OBJDIR_PY)/py_cleanup_runtime.ll
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/py_cleanup_runtime.ll $@ --target=$(PCC_RUNTIME_TARGET) --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F) $(PCC_RUNTIME_CONFIG_ARGS)

# Traceback raw-ABI entry must publish borrowed roots before any poll.
$(OBJDIR_PY)/py_exc_traceback.o: $(PYDIR)/py_exc_traceback.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) PCC_IMPLICIT_THREAD_SAFEPOINTS=0 $(PCC) --python-library --target=$(PCC_RUNTIME_TARGET) --emit-llvm=$(OBJDIR_PY)/py_exc_traceback.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.ir.optimization.driver "$(PCC_RUNTIME_IR_PASSES)" $(OBJDIR_PY)/py_exc_traceback.ll $(OBJDIR_PY)/py_exc_traceback.opt.ll
	mv $(OBJDIR_PY)/py_exc_traceback.opt.ll $(OBJDIR_PY)/py_exc_traceback.ll
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/py_exc_traceback.ll $@ --target=$(PCC_RUNTIME_TARGET) --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F) $(PCC_RUNTIME_CONFIG_ARGS)

$(OBJDIR_PY):
	mkdir -p $(OBJDIR_PY)

clean:
	rm -rf $(OBJDIR_PY) $(OBJDIR_LIBPY) \
		$(LIB_PCC_PY) $(LIB_PCC_PY).capi_syms $(LIB_PCC_PY).provenance.json \
		$(LIB_PCC_PY).target \
		$(LIB_PCC_PY_LIBPYTHON) $(LIB_PCC_PY_LIBPYTHON).capi_syms \
		$(LIB_PCC_PY_LIBPYTHON).target

distclean: clean
