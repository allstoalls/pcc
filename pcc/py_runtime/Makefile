# pcc/py_runtime/Makefile
#
# Build libpy_runtime.a, the C runtime for pcc's Python frontend.
#
# Public header:  include/py_runtime.h
# Source:         src/*.c  +  py/*.py  (Phase 4c runtime-high migrations)
# Output:         libpy_runtime.a (in this directory)
#
# Usage:
#   make                          - build libpy_runtime.a (cc baseline)
#   make libpy_runtime_pcc.a      - build pcc-emitted runtime archive (Phase 1/2)
#   make libpy_runtime_pcc_py.a   - build archive with pcc-Python runtime-high (Phase 4c)
#   make clean                    - remove all objects + archives
#   make distclean                - clean + drop pcc-emitted trees too

.DEFAULT_GOAL := all

CC       ?= cc
AR       ?= ar
RANLIB   ?= ranlib
PCC      ?= pcc
CLANG    ?= clang
PYTHON   ?= python3
PCC_IR_TO_OBJ ?= $(PYTHON) -m pcc.tools.ir_to_obj
PCC_REPO_ROOT ?= $(abspath ../..)

CFLAGS   ?= -O2 -fPIC -Wall -Wextra -std=c11
CPPFLAGS ?=
INCLUDES  = -Iinclude
PCC_WITH_THREADS ?= 0
PCC_REFCOUNT_KIND ?=
RUNTIME_HEADERS = Makefile include/py_runtime.h $(SRCDIR)/py_internal.h $(SRCDIR)/py_timer_heap.h $(SRCDIR)/py_io_waitset.h
PCC_STDATOMIC_HEADER = $(PCC_REPO_ROOT)/utils/fake_libc_include/stdatomic.h

# Phase 4: optional libpython fallback. Build with
#   make PCC_WITH_LIBPYTHON=1
# to enable the CPython C-API path in py_libpython.c. Off by default
# so the default runtime has no libpython dependency.
# Linux/glibc: -std=c11 defines __STRICT_ANSI__, which switches off glibc's
# default feature set and hides POSIX/BSD declarations (CLOCK_MONOTONIC,
# struct addrinfo, struct stat::st_mtim, ...). Restore the default set in ONE
# place instead of sprinkling feature macros per file; _DEFAULT_SOURCE does not
# enable any GNU-only compiler extension, so the strict C11 dialect is kept.
# Darwin's headers declare these unconditionally and need nothing.
ifneq ($(shell uname),Darwin)
CPPFLAGS += -D_DEFAULT_SOURCE
endif

ifeq ($(PCC_WITH_THREADS),1)
CPPFLAGS += -DPCC_WITH_THREADS=1
CFLAGS   += -pthread
else
CPPFLAGS += -DPCC_WITH_THREADS=0
endif

ifneq ($(PCC_REFCOUNT_KIND),)
CPPFLAGS += -DPCC_REFCOUNT_STRATEGY=$(PCC_REFCOUNT_KIND)
endif

ifeq ($(PCC_WITH_LIBPYTHON),1)
CFLAGS   += -DPCC_WITH_LIBPYTHON=1 $(shell python3-config --includes)
endif

SRCDIR       = src
PYDIR        = py
OBJDIR       = build
OBJDIR_PCC   = build_pcc
OBJDIR_PY    = build_py
OBJDIR_LIBPY = build_libpython

# Runtime modules that exist as Python ports under py/ and are used by
# the libpy_runtime_pcc_py.a variant in place of the cc-built .o.
# Keep in sync with the list of @c_abi_export-decorated modules in py/.
PY_MODULES = py_tuple py_tuple_slice py_obj_stubs py_exc_tls py_exc_objects py_exc_match py_exc_table py_exc_traceback py_obj_gc py_gc_telemetry py_gc_backend py_set py_os_env py_os_system py_os_path py_os_substrate py_process py_process_substrate py_process_timeout py_print_sys py_print_fmt py_format_runtime py_obj py_obj_dealloc py_dict py_list py_list_set_slice py_iter py_gen py_coroutine py_context_runtime py_call_splat_runtime py_module_attrs_runtime py_compiled_module_runtime py_dlpack_runtime py_runtime_log py_json_runtime py_pickle_copy_runtime py_protocol_runtime py_extension_loader_runtime py_http_runtime py_asyncio_io_runtime py_re_engine_runtime freestanding_io_waitset py_func py_re py_dunder py_weakref py_file py_threading py_class py_obj_ops_dispatch py_obj_ops_slice py_obj_ops_set_slice py_obj_ops_mod py_obj_ops_compare py_str py_str_accessors py_str_slice py_int_core py_int_decimal py_int_shift py_int_bitwise py_int_addsub py_int_mul py_int_bigint_pow py_int_ops py_int py_int_parse py_int_convert py_int_bigint_convert py_substrate
PY_MODULES += py_virtual_thread_runtime freestanding_runtime_debug py_capi_core_runtime py_capi_memory_runtime py_capi_stdio_runtime py_capi_numeric_runtime py_capi_collections_runtime py_capi_exc_runtime py_capi_dict_runtime py_capi_object_runtime py_capi_type_runtime py_capi_unicode_runtime py_capi_capsule_runtime py_capi_set_runtime py_capi_misc_runtime py_capi_cfunction_runtime py_capi_object_call_runtime py_capi_method_bridge_runtime py_capi_object_attr_runtime py_capi_number_runtime py_capi_sequence_runtime py_capi_mapping_runtime py_capi_import_runtime py_capi_unicode_search_runtime py_capi_unicode_writer_runtime py_capi_buffer_runtime py_capi_module_runtime py_capi_cext_runtime py_capi_module_state_runtime py_capi_private_runtime py_capi_slice_runtime py_capi_seqiter_runtime py_capi_contextvar_runtime py_capi_call_runtime py_capi_arg_runtime py_capi_str_conv_runtime py_capi_buildvalue_runtime py_capi_type_descriptor_runtime py_capi_visit_runtime
ifeq ($(PCC_WITH_THREADS),1)
PY_MODULES += freestanding_thread_kernel_pthread
endif

# C objects that are replaced indirectly by a Python port with a different
# module name. py_obj_stubs.py owns the exported bytes helpers in the
# pcc-Python runtime archive, so the C py_bytes.o must not remain there.
PY_REPLACED_C_MODULES = $(PY_MODULES) py_bytes py_call_splat py_compiled_module py_context py_module_attrs py_gc_index_table pcc_gc_external_resource py_os_native py_complex_pow py_float_fromhex py_json py_pickle_copy py_protocol py_extension_loader py_http py_asyncio_io py_io_waitset py_class_attrs py_format

SRCS = \
	$(SRCDIR)/py_obj.c \
	$(SRCDIR)/py_dunder.c \
	$(SRCDIR)/py_obj_dealloc.c \
	$(SRCDIR)/py_gc_index_table.c \
	$(SRCDIR)/py_os_native.c \
	$(SRCDIR)/py_gc_backend.c \
	$(SRCDIR)/pcc_threads.c \
	$(SRCDIR)/py_timer_heap.c \
	$(SRCDIR)/py_io_waitset.c \
	$(SRCDIR)/pcc_runtime_log.c \
	$(SRCDIR)/pcc_metal_runtime.c \
	$(SRCDIR)/pcc_gc_external_resource.c \
	$(SRCDIR)/pcc_dlpack_runtime.c \
	$(SRCDIR)/py_obj_gc.c \
	$(SRCDIR)/py_protocol.c \
	$(SRCDIR)/py_obj_ops_compare.c \
	$(SRCDIR)/py_obj_ops_dispatch.c \
	$(SRCDIR)/py_func.c \
	$(SRCDIR)/py_int_core.c \
	$(SRCDIR)/py_int_decimal.c \
	$(SRCDIR)/py_int_shift.c \
	$(SRCDIR)/py_int_bitwise.c \
	$(SRCDIR)/py_int_addsub.c \
	$(SRCDIR)/py_int_mul.c \
	$(SRCDIR)/py_int_bigint_pow.c \
	$(SRCDIR)/py_int_ops.c \
	$(SRCDIR)/py_int.c \
	$(SRCDIR)/py_int_parse.c \
	$(SRCDIR)/py_int_convert.c \
	$(SRCDIR)/py_int_bigint_convert.c \
	$(SRCDIR)/py_str.c \
	$(SRCDIR)/py_str_accessors.c \
	$(SRCDIR)/py_json.c \
	$(SRCDIR)/py_pickle_copy.c \
	$(SRCDIR)/py_bytes.c \
	$(SRCDIR)/py_list.c \
	$(SRCDIR)/py_iter.c \
	$(SRCDIR)/py_gen.c \
	$(SRCDIR)/py_coroutine.c \
	$(SRCDIR)/py_asyncio_io.c \
	$(SRCDIR)/py_context.c \
		$(SRCDIR)/py_format.c \
		$(SRCDIR)/py_float_fromhex.c \
		$(SRCDIR)/py_complex_pow.c \
		$(SRCDIR)/py_call_splat.c \
		$(SRCDIR)/py_module_attrs.c \
		$(SRCDIR)/py_compiled_module.c \
		$(PY_CAPI_ORACLE_SRC) \
		$(SRCDIR)/py_extension_loader.c \
		$(SRCDIR)/py_tuple.c \
	$(SRCDIR)/py_dict.c \
	$(SRCDIR)/py_set.c \
	$(SRCDIR)/py_class.c \
	$(SRCDIR)/py_class_attrs.c \
	$(SRCDIR)/py_exc_tls.c \
	$(SRCDIR)/py_exc_table.c \
	$(SRCDIR)/py_exc_objects.c \
	$(SRCDIR)/py_exc_match.c \
	$(SRCDIR)/py_exc_traceback.c \
	$(SRCDIR)/py_os_env.c \
	$(SRCDIR)/py_os_path.c \
	$(SRCDIR)/py_os_substrate.c \
	$(SRCDIR)/py_http.c \
	$(SRCDIR)/py_file.c \
	$(SRCDIR)/py_process.c \
	$(SRCDIR)/py_process_substrate.c \
	$(SRCDIR)/py_process_timeout.c \
	$(SRCDIR)/py_re.c \
	$(SRCDIR)/py_re_engine.c \
	$(SRCDIR)/py_re_engine_obj.c \
	$(SRCDIR)/py_cpy_handle.c \
	$(SRCDIR)/py_os_rss.c \
	$(SRCDIR)/py_os_heap.c \
	$(SRCDIR)/py_enumerate.c \
	$(SRCDIR)/py_int_bytes.c \
	$(SRCDIR)/py_weakref.c \
	$(SRCDIR)/py_threading.c \
	$(SRCDIR)/py_print_fmt.c \
	$(SRCDIR)/py_print_sys.c \
	$(SRCDIR)/py_obj_stubs.c \
	$(SRCDIR)/py_substrate.c \
	$(addprefix $(SRCDIR)/,$(LIBPYTHON_SRCS))

LIBPYTHON_SRCS =
ifeq ($(PCC_WITH_LIBPYTHON),1)
LIBPYTHON_SRCS = py_libpython.c
endif

PY_CAPI_ORACLE_SRC =
ifneq ($(PCC_WITH_LIBPYTHON),1)
PY_CAPI_ORACLE_SRC = $(SRCDIR)/py_capi_shim_oracle.c
endif

# Whether this archive is expected to DEFINE the CPython API surface.
#
# The no-libpython builds compile py_capi_shim_oracle.c and therefore define
# ~444 `Py*` symbols, which the inventory below records. A
# PCC_WITH_LIBPYTHON=1 archive defines none of them: they are undefined here
# and resolved against the real libpython at link time, and the `nm` filter
# deliberately drops `U`. An empty inventory is the CORRECT result in that
# mode, so asserting it non-empty failed every libpython runtime build -- the
# production `--python-libpython=on` path as well as the cc/pcc oracle-diff
# fixtures. What the completion marker actually has to prove is that nm ran
# and the archive is not object-less; that is asserted separately.
ifeq ($(PCC_WITH_LIBPYTHON),1)
REQUIRE_CAPI_SHIM_INVENTORY = 0
else
REQUIRE_CAPI_SHIM_INVENTORY = 1
endif

OBJS     = $(patsubst $(SRCDIR)/%.c,$(OBJDIR)/%.o,$(SRCS))
OBJS_PCC = $(patsubst $(SRCDIR)/%.c,$(OBJDIR_PCC)/%.o,$(SRCS))

# LIBC-P2-MEM-STR: vendored musl string/memory functions, compiled BY PCC
# and archived so the static link resolves them here instead of importing
# libSystem's (the libc import ratchet counts the drop). Upstream pins and
# the single local patch live in vendor/musl-1.2.5/string/VENDOR.json. The
# cc oracle archive (libpy_runtime.a) intentionally stays pure host libc so
# differential tests keep a reference.
# One vendor tree, several musl subdirectories (string, stdlib, ctype, ...).
# vpath lets a single pattern rule cover all of them; basenames are unique.
VENDOR_MUSL_DIRS = vendor/musl-1.2.5/string vendor/musl-1.2.5/stdlib vendor/musl-1.2.5/ctype vendor/musl-1.2.5/internal vendor/musl-1.2.5/math
# musl's scan helpers include each other through internal headers.
VENDOR_MUSL_INCLUDES = --cpp-arg=-I$(CURDIR)/vendor/musl-1.2.5/internal
# Only the units that DEFINE a mem primitive need -O0: at higher levels LLVM
# rewrites their bodies back into the very primitive being defined
# (BUG-P1-SELF-MEM-INTRINSIC-LIBCALL-SELF-BRANCH). Everything else keeps the
# default level — the scan helpers in particular MISCOMPILE at -O0 (a double
# return value is lost, tracked as BUG-P1-CC-O0-DOUBLE-RETURN-LOST).
# The mem primitives no longer need -O0: PCC_NO_BUILTIN stops pcc's own
# libcall recognition, and with it in place LLVM no longer rewrites their
# bodies into the primitive being defined at any level (verified: bzero at
# -O2 emits a normal `U _memset` tail call, not a self-branch). The scan
# helpers MUST NOT use -O0 anyway (a double return value is lost there).
VENDOR_MUSL_O0_SRCS =
VENDOR_STRING_SRCS = $(foreach d,$(VENDOR_MUSL_DIRS),$(wildcard $(d)/*.c))
VENDOR_STRING_OBJS_PCC = $(patsubst %.c,$(OBJDIR_PCC)/vendor_%.o,$(notdir $(VENDOR_STRING_SRCS)))
VENDOR_MEM_STR_SRCS = $(wildcard vendor/musl-1.2.5/string/*.c)
VENDOR_MEM_STR_OBJS_PCC = $(patsubst %.c,$(OBJDIR_PCC)/vendor_%.o,$(notdir $(VENDOR_MEM_STR_SRCS)))
VENDOR_NON_MEM_STR_OBJS_PCC = $(filter-out $(VENDOR_MEM_STR_OBJS_PCC),$(VENDOR_STRING_OBJS_PCC))
vpath %.c $(VENDOR_MUSL_DIRS)

# pcc-owned _FORTIFY_SOURCE wrappers (__memcpy_chk & friends). Compiled by
# pcc from SRCS like every other runtime source; also archived into the
# default port archive so the fortify symbols stop being libSystem imports.
FORTIFY_OBJ_PCC = $(OBJDIR_PCC)/py_libc_fortify.o

# Not part of SRCS: the cc oracle archive must stay pure host libc, so the
# wrappers are compiled only for the pcc archives (they exist to remove
# libSystem imports from pcc-produced artifacts).
$(OBJDIR_PCC)/py_libc_fortify.o: $(SRCDIR)/py_libc_fortify.c $(RUNTIME_HEADERS) | $(OBJDIR_PCC)
	PCC_HOST_PYTHON=$(PYTHON) $(PCC) --cpp-arg=-DPCC_USE_FREESTANDING_PLATFORM_PROCESS=1 --cpp-arg=-I$(CURDIR)/include --cpp-arg=-I$(CURDIR)/src --emit-obj $@ $<

$(OBJDIR_PCC)/vendor_%.o: %.c | $(OBJDIR_PCC)
	# PCC_NO_BUILTIN=1 is pcc's -fno-builtin: a unit that DEFINES a libc
	# primitive must not have its body recognized back into that same
	# primitive (llvm.memset(p,0,n) becomes a `bzero` call on Darwin, so
	# libc's own bzero would recurse forever). -O0 additionally keeps the
	# loop-idiom pass from re-synthesizing the intrinsics.
	PCC_HOST_PYTHON=$(PYTHON) PCC_NO_BUILTIN=1 $(PCC) $(if $(filter $(notdir $<),$(VENDOR_MUSL_O0_SRCS)),-O0,) $(VENDOR_MUSL_INCLUDES) --emit-obj $@ $<
OBJS_PY  = $(patsubst %,$(OBJDIR_PY)/%.o,$(PY_MODULES))
# Raw C-ABI modules are authored in pcc-Python like the runtime-high ports,
# but their strict freestanding contract forbids managed-runtime references.
# Keep the list separate so PY_REPLACED_C_MODULES remains a semantic-runtime
# mapping rather than accidentally treating retained oracle names as ports.
FREESTANDING_PY_MODULES = freestanding_mem_str freestanding_lfq_spsc freestanding_iobuf_pool freestanding_uring freestanding_libc_numeric freestanding_strtoint freestanding_re_engine freestanding_allocator freestanding_timer_heap freestanding_runtime_high_substrate freestanding_gc_index_table freestanding_gc_external_resource freestanding_gc_state freestanding_gc_control freestanding_gc_tracking freestanding_gc_root_registry freestanding_gc_frame_registry freestanding_gc_root_operations freestanding_gc_mapped_roots freestanding_gc_object_root_seeding freestanding_gc_refcount_roots freestanding_gc_object_slots freestanding_gc_common_mark_cycle freestanding_gc_backend0_slots freestanding_gc_sweep_slots freestanding_gc_tracing_sweep_collector freestanding_gc_public_collection freestanding_gc_incremental_concurrent_scheduler freestanding_gc_forwarding_identity freestanding_gc_object_nodes freestanding_gc_generational_oldification freestanding_gc_generational_remembered_owners freestanding_gc_generational_promotion freestanding_gc_generational_scheduler freestanding_gc_relocation_remap freestanding_gc_relocation_payload freestanding_gc_relocation_copy freestanding_gc_relocation_selector freestanding_gc_relocation_drain freestanding_gc_zpage_mechanics freestanding_gc_zpage_lifecycle freestanding_gc_forwarding_retirement freestanding_gc_barrier_dispatcher freestanding_gc_zpage_allocation freestanding_gc_backend0_collector freestanding_gc_root_introspection freestanding_platform_io freestanding_platform_fs freestanding_platform_env freestanding_platform_system freestanding_platform_time freestanding_platform_process freestanding_platform_socket freestanding_platform_rss freestanding_errno freestanding_metal_runtime freestanding_stdio
FREESTANDING_PY_MODULES += freestanding_gateway_control
ifneq ($(PCC_WITH_THREADS),1)
FREESTANDING_PY_MODULES += freestanding_thread_kernel
endif
FREESTANDING_OBJS_PY = $(patsubst %,$(OBJDIR_PY)/%.o,$(FREESTANDING_PY_MODULES))
PCC_PY_OBJECTS = $(OBJS_PY) $(FREESTANDING_OBJS_PY)
PCC_PY_RECEIPTS = $(addsuffix .provenance.json,$(PCC_PY_OBJECTS))

LIB        = libpy_runtime.a
LIB_PCC    = libpy_runtime_pcc.a
LIB_PCC_PY = libpy_runtime_pcc_py.a
LIB_PCC_PY_LIBPYTHON = libpy_runtime_pcc_py_libpython.a
PCC_RUNTIME_ARCHIVE_LOCK_HELD ?= 0
PCC_RUNTIME_ARCHIVE_LOCK_MAX_WAITS ?= 3000
PCC_RUNTIME_ARCHIVE_LOCK_OWNER_GRACE_WAITS ?= 50
PCC_RUNTIME_ARCHIVE_LOCK_SLEEP ?= 0.1
PCC_RUNTIME_ARCHIVE_MAKEFILE := $(abspath $(firstword $(MAKEFILE_LIST)))
# The default archive owns the single-thread kernel in freestanding
# pcc-Python. Explicit PCC_WITH_THREADS=1 selects the pthread ABI owner module;
# both modes share the pcc-Python virtual-thread scheduler and debug guards.

.PHONY: all clean distclean _pcc_runtime_archive_lock_force

# The C-API inventory is the completion marker consumed by pipeline.py.  Make
# nm failure or an object-less archive fail before either payload is published,
# rather than warning later after an unrelated undefined-symbol link failure.
all: $(LIB)

$(LIB): $(OBJS)
	@set -eu; \
	rm -f "$@.tmp" "$@.capi_syms.nm.tmp" "$@.capi_syms.tmp" "$@.capi_syms"; \
	$(AR) rcs "$@.tmp" $^; \
	$(RANLIB) "$@.tmp"; \
	nm -g "$@.tmp" > "$@.capi_syms.nm.tmp" 2>/dev/null; \
	awk '$$2!="U" && $$2 ~ /^[A-Z]$$/ && ($$3 ~ /^_?Py/ || $$3 ~ /^_?_Py/) {print $$3}' "$@.capi_syms.nm.tmp" | LC_ALL=C sort -u > "$@.capi_syms.tmp"; \
	test -s "$@.capi_syms.nm.tmp"; \
	if [ "$(REQUIRE_CAPI_SHIM_INVENTORY)" = 1 ]; then \
		test -s "$@.capi_syms.tmp"; \
	fi; \
	rm -f "$@.capi_syms.nm.tmp"; \
	mv -f "$@.tmp" "$@"; \
	mv -f "$@.capi_syms.tmp" "$@.capi_syms"

$(OBJDIR)/%.o: $(SRCDIR)/%.c $(RUNTIME_HEADERS) | $(OBJDIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR):
	mkdir -p $(OBJDIR)

# Phase 1/2 track: same source compiled by pcc itself. Produces a
# distinct archive so the two builds never collide. The pcc invocation
# uses --cpp-arg=-I for header lookup because pcc does not (yet)
# consume -I directly; --emit-obj writes the object file in place.
# The pipeline treats this inventory as the completion signal for the
# transitional pcc-C archive, matching the host-C oracle archive above.
$(LIB_PCC): $(OBJS_PCC) $(VENDOR_STRING_OBJS_PCC)
	@set -eu; \
	rm -f "$@.tmp" "$@.capi_syms.nm.tmp" "$@.capi_syms.tmp" "$@.capi_syms"; \
	$(AR) rcs "$@.tmp" $^; \
	$(RANLIB) "$@.tmp"; \
	nm -g "$@.tmp" > "$@.capi_syms.nm.tmp" 2>/dev/null; \
	awk '$$2!="U" && $$2 ~ /^[A-Z]$$/ && ($$3 ~ /^_?Py/ || $$3 ~ /^_?_Py/) {print $$3}' "$@.capi_syms.nm.tmp" | LC_ALL=C sort -u > "$@.capi_syms.tmp"; \
	test -s "$@.capi_syms.nm.tmp"; \
	if [ "$(REQUIRE_CAPI_SHIM_INVENTORY)" = 1 ]; then \
		test -s "$@.capi_syms.tmp"; \
	fi; \
	rm -f "$@.capi_syms.nm.tmp"; \
	mv -f "$@.tmp" "$@"; \
	mv -f "$@.capi_syms.tmp" "$@.capi_syms"

# PCC_HOST_PYTHON has to be handed over the way the pcc-Python rules do it: the
# C driver delegates to a host pcc entrypoint, and without it that lookup falls
# back to whichever python3 is first on PATH. When that interpreter has no pcc
# module the real compile error is replaced by
# PCC-CPY-UNSUPPORTED-L3-TOOLING-C-DRIVER, which hid a genuine frontend bug.
$(OBJDIR_PCC)/%.o: $(SRCDIR)/%.c $(RUNTIME_HEADERS) | $(OBJDIR_PCC)
	PCC_HOST_PYTHON=$(PYTHON) $(PCC) --cpp-arg=-I$(CURDIR)/include --cpp-arg=-I$(CURDIR)/src --emit-obj $@ $<

# py_re_engine.c uses C11 atomics through pcc's parser-facing fake header.
# Track that preprocessing input so an atomic-surface edit cannot leave a stale
# pcc-emitted object/archive behind.
$(OBJDIR_PCC)/py_re_engine.o: $(PCC_STDATOMIC_HEADER)

# C-kernel platform-syscall helpers (PCC_CC_ONLY_SRCS): these read OS-specific,
# ABI-sensitive structs through SDK headers that use modern syntax pcc's C
# frontend does not parse (e.g. macOS <malloc/malloc.h> / <mach/mach.h> use C23
# `enum : uint64_t` and bool bitfields; <sys/event.h>'s kevent has no fake-libc
# shadow). fake-libc shadows would have to duplicate the OS struct layout
# exactly, which is fragile; per the runtime layering doctrine these stay in the
# C kernel. Their symbols may be referenced by other pcc-emitted objects in the
# SAME archive (e.g. py_asyncio_io.o calls pcc_io_waitset_kqueue_available), and
# that still links because the CC-built object is archived alongside them.
# Explicit rules override the generic pcc pattern rule above.
PCC_CC_ONLY_SRCS = py_os_rss py_os_heap py_io_waitset pcc_metal_runtime
$(OBJDIR_PCC)/pcc_metal_runtime.o: $(SRCDIR)/pcc_metal_runtime.c $(RUNTIME_HEADERS) | $(OBJDIR_PCC)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@
$(OBJDIR_PCC)/py_io_waitset.o: $(SRCDIR)/py_io_waitset.c $(RUNTIME_HEADERS) | $(OBJDIR_PCC)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PCC):
	mkdir -p $(OBJDIR_PCC)

# Phase 4c track: runtime-high modules ported from C to pcc-compilable
# Python under py/. pcc emits library-shaped LLVM IR for each module
# without synthesizing a program @main, then ir_to_obj lowers it to a
# relocatable object. The resulting archive is built directly from the
# Python-built objects: the no-C closure must not depend on successfully
# compiling the replaced C runtime sources on every target.
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
	nm -g "$@.tmp" > "$@.capi_syms.nm.tmp" 2>/dev/null; \
	awk '$$2!="U" && $$2 ~ /^[A-Z]$$/ && ($$3 ~ /^_?Py/ || $$3 ~ /^_?_Py/) {print $$3}' "$@.capi_syms.nm.tmp" | LC_ALL=C sort -u > "$@.capi_syms.tmp"; \
	rm -f "$@.capi_syms.nm.tmp"; \
	PYTHONPATH=$(PCC_REPO_ROOT) $(PYTHON) -m pcc.tools.runtime_archive_provenance assemble --archive "$@.tmp" --runtime-root $(CURDIR) --output "$@.provenance.json.tmp" --capi-inventory "$@.capi_syms.tmp" --ar $(AR) $(PCC_PY_OBJECTS); \
	mv -f "$@.tmp" "$@"; \
	mv -f "$@.capi_syms.tmp" "$@.capi_syms"; \
	mv -f "$@.provenance.json.tmp" "$@.provenance.json"
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
	nm -g "$@.tmp" > "$@.capi_syms.nm.tmp" 2>/dev/null; \
	awk '$$2!="U" && $$2 ~ /^[A-Z]$$/ && ($$3 ~ /^_?Py/ || $$3 ~ /^_?_Py/) {print $$3}' "$@.capi_syms.nm.tmp" | LC_ALL=C sort -u > "$@.capi_syms.tmp"; \
	test -s "$@.capi_syms.nm.tmp"; \
	if [ "$(REQUIRE_CAPI_SHIM_INVENTORY)" = 1 ]; then \
		test -s "$@.capi_syms.tmp"; \
	fi; \
	rm -f "$@.capi_syms.nm.tmp"; \
	mv -f "$@.tmp" "$@"; \
	mv -f "$@.capi_syms.tmp" "$@.capi_syms"

$(OBJDIR_LIBPY)/py_libpython.o: $(SRCDIR)/py_libpython.c $(RUNTIME_HEADERS) | $(OBJDIR_LIBPY)
	$(CC) $(CPPFLAGS) $(CFLAGS) -DPCC_WITH_LIBPYTHON=1 $(shell python3-config --includes) $(INCLUDES) -c $< -o $@

# Callers build the libpython archive with OBJDIR=build_libpython, which makes
# this target identical to $(OBJDIR) above; defining it twice made make warn
# about overriding commands on every libpython build.
ifneq ($(OBJDIR_LIBPY),$(OBJDIR))
$(OBJDIR_LIBPY):
	mkdir -p $(OBJDIR_LIBPY)
endif

# Recompile a pcc-Python runtime module when that module changes. The archive
# target already tracks additions/removals through OBJS_PY/FREESTANDING_OBJS_PY;
# making every object depend on the entire Makefile turned any helper-list edit
# into a full runtime rebuild. Compiler/target/config invalidation is owned by
# pipeline._ensure_runtime's explicit full-rebuild decision.
$(OBJDIR_PY)/%.o: $(PYDIR)/%.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) $(PCC) --python-library --emit-llvm=$(OBJDIR_PY)/$*.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/$*.ll $@ --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F)

# The receipt is a completion sidecar emitted with the object.  Give it an
# explicit dependency edge so deleting or losing a receipt cannot leave an
# apparently up-to-date object eligible for the production archive.  The
# recursive rebuild runs only for that recovery case; ordinary object builds
# publish both files in one emitter process.
$(OBJDIR_PY)/%.o.provenance.json: $(OBJDIR_PY)/%.o
	test -f $@ || $(MAKE) -B $(@D)/$*.o
	test -f $@

# This module implements pcc_thread_safepoint itself. Compiling it with the
# ordinary PCC_WITH_THREADS=1 frontend setting would inject a function-entry
# call to pcc_thread_safepoint into pcc_thread_safepoint (and every helper),
# causing immediate unbounded recursion. Its blocking loops already contain
# the required explicit polls; disable automatic insertion for this one object.
$(OBJDIR_PY)/freestanding_thread_kernel_pthread.o: $(PYDIR)/freestanding_thread_kernel_pthread.py | $(OBJDIR_PY)
	PYTHONPATH=$(PCC_REPO_ROOT) PCC_HOST_PYTHON=$(PYTHON) PCC_WITH_THREADS=0 $(PCC) --python-library --emit-llvm=$(OBJDIR_PY)/freestanding_thread_kernel_pthread.ll $<
	PYTHONPATH=$(PCC_REPO_ROOT) $(PCC_IR_TO_OBJ) $(OBJDIR_PY)/freestanding_thread_kernel_pthread.ll $@ --provenance $@.provenance.json --source $< --runtime-root $(CURDIR) --member $(@F)

$(OBJDIR_PY):
	mkdir -p $(OBJDIR_PY)

$(OBJDIR_PY)/py_io_waitset.o: $(SRCDIR)/py_io_waitset.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_int_modexp.o: $(SRCDIR)/py_int_modexp.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_obj_min_max.o: $(SRCDIR)/py_obj_min_max.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_tuple_methods.o: $(SRCDIR)/py_tuple_methods.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_cpy_handle.o: $(SRCDIR)/py_cpy_handle.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_os_heap.o: $(SRCDIR)/py_os_heap.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_enumerate.o: $(SRCDIR)/py_enumerate.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_int_bytes.o: $(SRCDIR)/py_int_bytes.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_timer_heap.o: $(SRCDIR)/py_timer_heap.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/pcc_threads.o: $(SRCDIR)/pcc_threads.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/pcc_gc_external_resource.o: $(SRCDIR)/pcc_gc_external_resource.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_runtime_high_substrate.o: $(SRCDIR)/py_runtime_high_substrate.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_context.o: $(SRCDIR)/py_context.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

# Retained explicit C-oracle rule; this object is not in the production
# pcc-Python archive inventory.
$(OBJDIR_PY)/py_float_fromhex.o: $(SRCDIR)/py_float_fromhex.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

# Retained explicit C-oracle rule for py_complex_pow.o. This overrides the generic
# $(OBJDIR_PY)/%.o: $(PYDIR)/%.py pattern rule so the co-located reference
# py_complex_pow.py (which is NOT a linked port module) is never compiled.
$(OBJDIR_PY)/py_complex_pow.o: $(SRCDIR)/py_complex_pow.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_call_splat.o: $(SRCDIR)/py_call_splat.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_module_attrs.o: $(SRCDIR)/py_module_attrs.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

$(OBJDIR_PY)/py_compiled_module.o: $(SRCDIR)/py_compiled_module.c $(RUNTIME_HEADERS) | $(OBJDIR_PY)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(INCLUDES) -c $< -o $@

clean:
	rm -rf $(OBJDIR) $(LIB) $(LIB).capi_syms

distclean: clean
	rm -rf $(OBJDIR_PCC) $(LIB_PCC) $(LIB_PCC).capi_syms \
		$(OBJDIR_PY) $(LIB_PCC_PY) \
		$(LIB_PCC_PY).capi_syms \
		$(LIB_PCC_PY).provenance.json \
		$(OBJDIR_LIBPY) $(LIB_PCC_PY_LIBPYTHON) \
		$(LIB_PCC_PY_LIBPYTHON).capi_syms
