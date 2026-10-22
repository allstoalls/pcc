# Chapter 7: The Object Model

Everything in the runtime begins with the question "what does a Python value look like in memory?" pcc's object model answers three things: what object header every heap object shares; how classes and instances are laid out and how attributes are looked up; and — the most unusual one — why the production pcc-Python implementation must remain byte-for-byte compatible with the C ABI layout and differential oracle. This chapter covers only the static structure of objects and the attribute protocol: reference counting and the ownership contract are Chapter 9, the exception protocol is Chapter 8, and how the five GC backends traverse and relocate these objects is Chapters 10 and 11. By the end of this chapter you should be able to draw the 120 bytes of `PyClassObject` from [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) from memory, and explain why the triage order for "the object clearly has this attribute, yet it raises AttributeError" is layout, barriers, error checks — and not the frontend.

## Chapter Overview: Start at the Object Header

The chapter has many details, but the entry point is simple: every heap object begins with the same kind of header, then the type tag and concrete layout decide what the rest of the object means. Attributes, methods, and GC all depend on the header, tags, slot access, and agreement between the production pcc-Python owner and the C ABI oracle.

- For object bugs, first ask what type tag the pointer actually refers to.
- For class or instance bugs, compare the production pcc-Python layout and the C ABI/oracle field by field.
- For backend #3/#4-only bugs, first check whether object slots are read and written through GC barriers.

## 7.1 The Problem and the Design Space

A Python runtime's object model has to serve four clients at once: the interpreted or compiled code (reading and writing fields, calling methods), the memory manager (finding the pointers inside an object), the diagnostics machinery (deciding, given a raw address, "what is this?"), and — unique to pcc — the bootstrap chain (pcc-Python must be able to restate the same layout). CPython's well-known answer is `ob_refcnt` plus an `ob_type` pointer: every object's header points at a `PyTypeObject`, and all of a type's behavior — method tables, allocators, the buffer protocol — hangs off that type object.

pcc did not copy this model. The object header in [pcc/runtime/include/py_runtime.h](../../pcc/runtime/include/py_runtime.h) is:

```c
typedef struct {
    int64_t refcount;
    int32_t  type_tag;
    int32_t  flags;        /* bit 0 = immortal, bit 1 = gc-tracked, ... */
} PyObjectHeader;
```

Type information is a 32-bit integer tag, not a pointer. The reasons for this choice are worth spelling out one by one, because they constrain every later chapter:

1. **A tag can be consumed without dereferencing a second object.** Most runtime dispatch — dealloc, comparison, formatting, `py_obj_getattr` — is a `switch` on `type_tag`. Handed a suspicious pointer at a crash site, you can read the 4 bytes at `obj + 8` and judge whether it even resembles an object; `py_type_tag_is_valid()` and `py_pointer_can_have_header()` in [pcc/runtime/src/py_obj.c](../../pcc/runtime/src/py_obj.c) perform exactly this kind of defensive validation. If the type were a pointer, validating one object would first require validating another object, and the foundation under diagnostics would turn to sand.
2. **Five GC backends share one header.** The low bits of `flags` are reserved for object semantics (immortal / gc-tracked / finalized); the remaining bits belong to the five GC backends — colors, age, relocation state (Chapter 10). A 16-byte header is the physical common denominator behind the five-backend "production equality rule."
3. **The mirroring obligation.** The pcc-Python port has to restate the same layout with raw memory accesses like `load_i32(o, 8)` (Section 7.5). An integer tag is flat; mirroring it is one line. A type-pointer graph would mean the port has to mirror a second object graph.
4. **Self-backend emittability.** A tag comparison is one integer instruction; it needs no cleverness from LLVM.

The costs deserve to be written down just as plainly: the tag space is managed by hand (Section 7.2 shows user-class tags allocated monotonically from 104, while `PY_TYPE_VALUEBOX = 200` is embedded in the same space — a real sharp edge, see Exercise 2); and a type's behavior cannot hang off slots in a type object the way CPython's does, but is scattered across runtime `switch`es, so adding a built-in type means touching several dispatch sites (the introduction record for `CpyHandle`, tag 32, counted them: two C dealloc switches, the relocation whitelist, and the port windows). pcc accepts this cost because its goal is not "the maximally extensible type system" but **the smallest auditable, mirrorable, bootstrappable object kernel**.

The second major design decision is the **tagged small-int lane**. Bit 0 of every `PyObject *` is conscripted: when it is 1, the value is not a pointer but a 63-bit signed integer shifted left by one; when it is 0, it is a genuine heap pointer. The comment in `py_internal.h` supplies the justification: malloc is at least 8-byte aligned on every target platform, so a real pointer's bit 0 is always 0. This is the physical face of the value model of Chapter 16: `int`'s semantic type is arbitrary precision, its value projection is the tagged lane, its object projection is the `PyIntObject` bignum; overflowing the tagged range boxes — it never wraps. For this chapter, only two consequences for the object model matter: every runtime function that accepts a `PyObject *` must first ask `PY_IS_TAGGED_INT`, and `py_incref`/`py_decref` return immediately for tagged values — a tagged integer has no object header, no reference count, and no identity.

The third decision: **class metadata lives in raw C arrays, and instance fields live in static slots**. The method table inside `PyClassObject` is a linear array, not a hash table, and the comment in [pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c) states the reason outright: "Classes have small method tables so this is faster than a dict in the common case. A future phase can swap to a hashmap." Instance fields have their slot indices fixed at compile time by codegen: `self.field` lowers to `py_instance_get_field(self, idx)` rather than a dict lookup — the module header of [pcc/frontends/python/codegen/class_gen.py](../../pcc/frontends/python/codegen/class_gen.py) states this contract explicitly. Dynamism is not abolished; it is deprioritized: declared fields go through slots, undeclared attributes go through a hidden per-instance dict slot (Section 7.4). This is "performance as a consequence of proven semantics" made concrete in the object model: things are made static only where the semantics can be proven static, and every remaining path keeps full Python behavior.

## 7.2 The Object Header, Tagged Integers, and the Type-Tag Space

### The object header

```text
bytes:   0               8       12      16
        +---------------+-------+-------+
        |   refcount    |type_  |flags  |    PyObjectHeader, 16 bytes
        |   (int64)     |tag i32|  i32  |
        +---------------+-------+-------+
        |  type-specific fields start here ...
```

`refcount` sits at offset 0, `type_tag` at offset 8 (int32), `flags` at offset 12 (int32). These three numbers are a hard contract throughout the repository — [AGENTS.md](../../AGENTS.md) puts them in the mandatory startup reading, and the pcc-Python port reads and writes them as bare literals.

The object-semantics bits of `flags` are defined in [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h):

```c
#define PY_FLAG_IMMORTAL    0x1
#define PY_FLAG_GC_TRACKED  0x2
#define PY_FLAG_FINALIZED   0x4
```

- `PY_FLAG_IMMORTAL`: `py_incref`/`py_decref` return immediately for objects carrying this bit. `py_None`, `py_True`, `py_False`, and the lazily constructed root class `object` (`object_root()` in `py_class.c`) are all immortal.
- `PY_FLAG_GC_TRACKED`: the object has been registered in the cycle collector's side table (set by `py_gc_track()` in `py_obj_gc.c`). Which types register and when is Chapter 10's business; for this chapter it is enough that instances register at the end of `py_instance_new()`, and that **class objects never register** — a fact that turns into an interesting bit reuse in Section 7.3.
- `PY_FLAG_FINALIZED`: the finalizer `__del__` has already been dispatched. `py_user_del_dispatch()` in [pcc/runtime/src/py_dunder.c](../../pcc/runtime/src/py_dunder.c) sets the bit before invoking `__del__`; from then on, even if the object is resurrected inside the finalizer and its refcount later drops to zero again, the second dealloc skips the finalizer. This is the one bit the object model pays so that "a finalizer runs at most once" holds.

Every bit from `0x8` upward (`PY_FLAG_GC_WHITE/GRAY/BLACK/PINNED/GC_YOUNG/GC_OLD/...` up through `0x10000`) belongs to the GC backends and is deferred to Chapters 10 and 11.

All reads and writes of `flags` go through the inline atomic accessors in `py_internal.h`: `py_header_flags_load/store/or/and` (`__atomic_*`, acquire/release ordering) plus the CAS-loop `py_header_flags_update()`. `refcount`, in turn, goes through `pcc_refcount_incref/decref`, whose policy (`PCC_REFCOUNT_KIND_NONATOMIC/ATOMIC/BIASED/DEFERRED`) is selected by the threading substrate — details in Chapter 9.

### Tagged integers and defensive pointer classification

```text
The two readings of a PyObject* (distinguished by bit 0):

  ...xxxx xxx1    tagged small int: arithmetic shift right by 1
                  = 63-bit signed payload
                  (PY_TAGGED_INT_MIN = INT64_MIN>>1, MAX = INT64_MAX>>1)
  ...xxxx x000    real heap pointer: malloc's >= 8-byte alignment
                  guarantees the low 3 bits are 0; it points at an
                  allocation that begins with a PyObjectHeader
```

Encoding and decoding are `py_tag_int()` (shift left by one, set the low bit) and `py_untag_int()` (arithmetic shift right, preserving the sign) in `py_internal.h`. `py_type_of()` returns `PY_TYPE_INT` for tagged values and reads the header tag for real pointers — so "dispatch on tag" looks uniform to callers, and taggedness is checked exactly once, at the boundary.

The runtime trusts no incoming pointer. `py_pointer_can_have_header()` in `py_obj.c` (with an independent reimplementation, `pointer_can_have_header()`, in `py_class.c`) applies four exclusions: NULL; bit 0 set (a tagged integer); addresses below 0x1000 (the null page); misaligned to 8 bytes; and nonzero top 16 bits (non-canonical addresses). `py_incref`/`py_decref` further validate that `type_tag` falls in the legal set (`py_type_tag_is_valid()`), and when the `PCC_DEBUG_RUNTIME` environment variable is set, a bad pointer or bad tag triggers an immediate `abort()` with the crime scene printed. These checks are not fastidiousness: on the bootstrap chain, a codegen bug in pcc1 first manifests as "something decremented a thing that is not an object," and having the runtime shout at the first scene is far cheaper than letting the heap rot for three more steps before crashing (Debugging Playbook §8).

### The type-tag space

The anonymous enum at the top of `py_runtime.h` lays out the built-in tags: `PY_TYPE_NONE = 0`, `PY_TYPE_BOOL = 1`, `PY_TYPE_INT = 2` (the bignum form), `PY_TYPE_FLOAT = 3`, `PY_TYPE_STR = 4`, `PY_TYPE_LIST = 5`, `PY_TYPE_DICT = 6`, `PY_TYPE_TUPLE = 7`, `PY_TYPE_SET = 8`, `PY_TYPE_FUNC = 9`, `PY_TYPE_CLASS = 10`, `PY_TYPE_INSTANCE = 11`, `PY_TYPE_EXC = 12` … all the way to `PY_TYPE_CPY_HANDLE = 32` (the owning handle for foreign CPython references, Chapter 17). Then comes a deliberate gap:

```text
 0 .. 32      built-in type tags
100           PY_TYPE_USER        start of the user domain
                                  (>= 100 means "user-class instance")
101           PY_TYPE_PROPERTY    -+
102           PY_TYPE_CLASSMETHOD  | descriptor wrapper objects
103           PY_TYPE_STATICMETHOD-+ (py_internal.h)
104           PY_TYPE_USER_CLASS_START   first user-class tag;
              g_next_user_tag in py_class.c increments monotonically
              from here
200           PY_TYPE_VALUEBOX    value-class boxing object —
                                  embedded inside the user domain
```

Every user class claims a unique tag in `py_class_new()` and stores it in `type_tag_alloc`; its instances write that tag into their headers. The fast path of `isinstance` and the dealloc dispatch (`pcc_dealloc_dispatch()` sends everything `>= PY_TYPE_USER` to `py_instance_dealloc`) therefore never chase a pointer. The comment records the tradeoff honestly: "Using a single allocator keeps tags unique across modules. In a future phase this can be per-module." — process-wide monotonic allocation, traded for cross-module uniqueness. `PY_TYPE_VALUEBOX = 200` sits inside that same space, and the allocator has no avoidance logic; that is an open sharp edge readers should audit for themselves (Exercise 2).

When an object dies, `pcc_dealloc_dispatch()` in `py_obj.c` hands the object, keyed by tag, to its type-specific deallocator (`py_dealloc_list`, `py_class_dealloc`, `py_instance_dealloc`, …). To keep deeply nested container chains from overflowing the C stack, `py_decref` maintains a thread-local deferral queue (`PccTrashNode`, the analogue of CPython's "trashcan"): during nested dealloc, container-like objects are enqueued first, and the outermost level drains the queue. Ownership semantics belong to Chapter 9; what matters here is that the key to dealloc dispatch is `type_tag`.

## 7.3 Class Objects: The 120 Bytes of PyClassObject

User classes are emitted by codegen at module initialization: every `ClassDef` corresponds to a global variable `.class.<module>.<name>`; the module init function gathers the base-class pointers and the field-name array, calls `py_class_new()`, then calls `py_class_add_method()` for each method (the contract in the `class_gen.py` module header). Classes built dynamically at runtime (`type(name, bases, ns)`) go through `py_class_new_from_objects()` in `py_class_attrs.c` and end up at the same `py_class_new()`.

The layout from `py_internal.h`, with offsets computed by hand:

```text
PyClassObject — 120 bytes total (LP64)
offset size field             meaning
  0     16  h                 PyObjectHeader (type_tag = PY_TYPE_CLASS = 10)
 16      8  name              borrowed C string (points into the emitting
                              module's read-only segment)
 24      4  n_bases           number of direct bases       (+4 padding)
 32      8  bases             PyClassObject** direct bases in
                              declaration order
 40      4  n_mro             MRO length                   (+4 padding)
 48      8  mro               C3 linearization; mro[0] == this class
 56      4  n_methods                                      (+4 padding)
 64      8  methods           PyClassMethod[]: {name, func},
                              16 bytes per entry
 72      4  n_fields          instance fields declared on this class
                                                            (+4 padding)
 80      8  field_names       const char** field names in slot order
 88      4  instance_size     total instance size in bytes
 92      4  type_tag_alloc    the type tag carried by this class's
                              instances
 96      8  del_method        cached __del__ (borrowed)
104      8  attrs             class-level attribute dict (owned)
112      8  metaclass         metaclass (borrowed)
```

Each of the four `int32` fields is followed by 4 bytes of padding (the next 8-byte field must be aligned), while the pair `instance_size`/`type_tag_alloc` packs neatly into one 8-byte unit, which puts `del_method` at 96 — these are the three numbers [AGENTS.md](../../AGENTS.md) keeps repeating: `del_method@96`, `attrs@104`, `metaclass@112`, 120 bytes total. The module docstring at the top of the pcc-Python port, [pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py), carries the same table line by line; that docstring is, in practice, the cross-language specification of this struct (Section 7.5).

The design points, field by field:

**`bases` and `mro`.** `py_class_new()` shallow-copies the base array (`copy_class_array()`), then runs `c3_linearize()` — the PEP 3119 merge algorithm: the candidate head is "the head that appears in no other sequence's tail"; if no candidate can be extracted, the MRO is inconsistent, the function returns -1, and the caller surfaces it as a TypeError. A class with zero bases that is not itself the root gets the lazily constructed `object_root()` appended to the end of its MRO — a calloc'd, immortal, minimal class with `type_tag_alloc = PY_TYPE_INSTANCE`. Note that everything these arrays hold is a **borrowed reference**: a class lives as long as the process (codegen stores it in a global), so bases, methods, and field names take no part in reference counting. But "borrowed" does not mean "invisible to GC" — these slots are still edges of the object graph, and a moving backend must be able to rewrite them. That is exactly the subject of the first case study in Section 7.7.

**`methods`, and the dual nature of method values.** `PyClassMethod` is `{const char *name; PyObject *func}`, and the comment on `func` does not pretend otherwise: "borrowed — points at a user_* LLVM function". Most entries in the method table are not heap function objects; they are raw function pointers emitted by codegen and cast to `PyObject *`. The call helpers (`class_call_binary_method()` and friends in `py_class.c`) start with header sniffing — only if the pointer can carry an object header *and* its tag is `PY_TYPE_FUNC` is it treated as a `PyFuncObject` and routed through `py_func_call()`; otherwise it is cast back to a function pointer and called directly. What this buys is a zero-allocation static method table (module init only realloc-appends); what it costs is that the runtime must reliably distinguish "heap object" from "code address" — the defensive pointer classification described above is here not a diagnostic aid but a correctness dependency. `py_class_lookup()` walks the MRO and scans each class's method table linearly, short-circuiting the string comparison with a pointer-equality test first (field and method names are usually the same rodata literal); `__name__` and `__mro__` are special-cased and synthesized here.

**`del_method`.** Finalizer lookup sits on the dealloc hot path, so it is cached at a fixed offset. The historical C oracle's `py_class_new()` pre-fills it at the end with `py_class_lookup(c, "__del__")` (which also finds inherited finalizers); `py_class_add_method()` updates the cache when it sees `"__del__"`; and `py_user_del_dispatch()` in `py_dunder.c` lazily fills the slot once if it finds it empty. The current pcc-Python production owner's `py_class_new` does *not* pre-fill it (memset leaves NULL), instead reaching the same observable behavior through the lazy fill. This is a clean example of "the layout must be byte-identical; oracle and production behavior may differ in pacing, but must converge" (Exercise 3).

**`attrs`: the class-level attribute dict.** This is the youngest slot in the layout, and its origin is a three-act play (Section 7.7). Its present form: `attrs` is a dict the class **owns**, holding class variables like `class C: x = 1` and the namespace of the three-argument `type()` form. The header comment of [pcc/runtime/src/py_class_attrs.c](../../pcc/runtime/src/py_class_attrs.c) states that the old pointer-keyed side table (the `PccClassAttrsNode` chain) has been demoted to an index that no longer owns the dicts — putting the edge inside the object itself is what lets moving collectors trace and rewrite it directly. The class attribute read path, `py_class_getattr()`, proceeds: `__dict__` special case → data descriptors on the metaclass → each class's `attrs` dict along the MRO (a classmethod hit binds; a descriptor hit calls `__get__`) → fall back to the `py_class_lookup()` method table. The write path, `py_class_setattr()`, first asks the metaclass's data descriptor `__set__`, otherwise writes into this class's own `attrs`.

**`metaclass`.** A borrowed pointer, set by `py_class_set_metaclass()`. pcc's metaclass support is narrow: a metaclass participates in the class attribute get/set/delete protocol (the lookup order above), but not in the class *creation* protocol. This is an honest "this is how far the implementation goes" boundary — do not read it as full CPython metaclass semantics.

**A bit reuse worth knowing about.** `py_class.c` defines `PY_CLASS_FLAG_SLOTS_ONLY` as 2 — numerically the same bit as `PY_FLAG_GC_TRACKED`. `py_class_mark_slots_only()` sets it on the **class object's** header, meaning "instances of this class have no dynamic attribute dict" (Section 7.4). This does not conflict with the GC bit only because the runtime never calls `py_gc_track()` on a `PY_TYPE_CLASS` object (grep confirms it: every call site of `py_gc_track` is in instance, container, and function constructors). This is bit reuse that relies on *non-overlapping usage*, not on any isolation provided by the type system — and if you do not know this while reading a flags dump, you will misread a slots-only class as "a GC-tracked class."

## 7.4 Instances and the Attribute Protocol

### Instance layout

```text
PyInstanceObject (instance_size = 24 + 8*(n_fields+1) bytes)
offset  0   PyObjectHeader     type_tag = cls->type_tag_alloc
offset 16   cls                -> PyClassObject (borrowed in ownership
                               terms, but read through the barrier)
offset 24   fields[0]          -+ declared field slots: owned references,
            ...                 | NULL = "not yet assigned" sentinel
            fields[n_fields-1] -+
            fields[n_fields]   hidden slot: the dynamic attribute dict
                               (__dict__), created lazily
```

`py_instance_new()` allocates `cls->n_fields + 1` slots — the extra one is the **hidden dynamic-attribute-dict slot**, and it is where the `+ 1` in `py_class_new()`'s `instance_size` computation goes. All slots are zeroed: NULL serves both as the "unassigned" sentinel and as what lets dealloc unconditionally apply "decrement only if non-NULL." The header of an instance carries the class's allocated `type_tag_alloc`, so from any instance pointer, a single load reveals which class tag it belongs to; `py_isinstance()` compares class pointers first, then scans the MRO linearly.

**Field slot indices are fixed at compile time.** The comment on `lookup_field_index()` in `py_class.c` clears up an easy misunderstanding: field lookup consults only the most-derived class's own `field_names`, with no aggregation along the MRO — because "the codegen merges the declaration sets": when codegen emits class metadata, it has already merged the field declarations of the entire inheritance chain into the leaf class's field table. Runtime MRO field aggregation therefore does not need to exist. It also means the field index is **a property of the merged table, not of any single source file** — in the third story of Section 7.7, a test that inferred indices from AST source order died on exactly this point. `py_instance_get_field()`/`py_instance_set_field()` still keep defensive bounds checks (out-of-range index returns NULL / no-ops), with a comment stating the reason: so that "malformed IR cannot segfault us."

### The seven layers of attribute lookup

`py_obj_getattr()` in [pcc/runtime/src/py_obj_ops_dispatch.c](../../pcc/runtime/src/py_obj_ops_dispatch.c) is the unified entry point: it dispatches on tag — instance tags go to `py_instance_getattr()`, the class tag goes to `py_class_getattr()`, and functions, weakrefs, complex numbers, and exceptions each get a small special case. When everything fails and no exception is pending in TLS, `py_obj_missing_attr()` constructs the `AttributeError` — **this is the birthplace of the "object has no attribute X" message**. The instance path, unrolled:

```text
py_instance_getattr(inst, name)                    py_class.c
 |- does the class define __getattribute__?
 |    call it; if it raises AttributeError and the class has
 |    __getattr__, clear the exception and call that instead
 +- default path py_instance_getattr_default:
      1. "__class__" / "__dict__" special cases (the latter lazily
         creates the hidden-slot dict)
      2. MRO attrs-dict hit that is a DATA descriptor?
         (PY_TYPE_PROPERTY, or the class defines __set__/__delete__)
         -> __get__
      3. declared field slots: lookup_field_index -> fields[idx]
         (return if non-NULL)
      4. dynamic attribute dict (the hidden slot), look up name
      5. the plain class attribute found in step 2: function -> bind;
         non-data descriptor -> __get__; otherwise return as-is
      6. MRO method table: py_class_lookup
         -> py_instance_bind_method to bind
      7. __getattr__ as the last resort
 returns NULL with no pending exception
   -> py_obj_missing_attr -> AttributeError(name)
```

This ordering is an isomorphic port of CPython's descriptor protocol: data descriptors override instance storage, and instance storage overrides non-data descriptors and plain class attributes. pcc's "instance storage" has merely been split into two layers — static field slots (3) and the dynamic dict (4). The write path, `py_instance_setattr()`, is the same logic compressed to three layers: descriptors with `__set__` → field slots (via `pcc_gc_store_ptr()`; the balanced decrement-old/increment-new contract belongs to Chapter 9) → the dynamic dict (created lazily; if the class is marked slots-only, this layer does not exist and the call returns -1).

**Bound methods are synthesized trampolines.** The method obtained in step 6 is usually a raw function pointer and cannot be returned as a value directly. `py_instance_bind_method()` in `py_class_attrs.c` packs `{method, self}` into a captures tuple and wraps it with the uniform entry point `pcc_instance_bound_method_entry()`, producing a real `PyFuncObject`. The entry point prepends `self` and dispatches by argument count: there are direct-call branches for 0, 1, and 2 arguments, and the comment on the 3-argument branch records its provenance — `__exit__(self, exc_type, exc, tb)` once failed with "exit returned NULL" because this branch was missing; one more case of "holes in the object model surface as crashes in real programs." `classmethod` goes through the parallel `pcc_classmethod_bind()`, which prepends the class object instead of the instance.

### Finalization and resurrection (the object-model side)

The first two lines of `py_instance_dealloc()` (in `py_class.c`) decide the object-model half of Python's finalization semantics: first `py_weakref_invalidate()` clears weak references, then `py_user_del_dispatch()` dispatches `__del__`; afterwards the code checks `refcount > 0` — the finalizer may have stashed `self` somewhere and **resurrected** the object, in which case it is re-registered with `py_gc_track()` and the function returns without freeing. Because `py_user_del_dispatch()` set `PY_FLAG_FINALIZED` at dispatch time, the resurrected object's next death will not enter the finalizer again. An exception raised by a finalizer is swallowed by `py_clear_exception()` (CPython's unraisable semantics; a warning channel is a future diagnostics task). Why the refcount is trustworthy at this moment, and how that is guaranteed across backends, is Chapters 9 and 10.

## 7.5 One Layout, One Production Owner: The pcc-Python ↔ C-Oracle Mirroring Discipline

pcc's runtime layering (Chapters 1 and 14) has moved this production ownership into pcc-Python: [pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py) exports **the same symbols with the same ABI** through decorators such as `@c_abi_export("py_class_lookup")`, and the current production archive links pcc-Python objects rather than treating [pcc/runtime/src/py_class.c](../../pcc/runtime/src/py_class.c) as a second production implementation. The C declaration and historical implementation retain two jobs: defining the external ABI layout and serving as a migration-time differential oracle. `PyClassObject` therefore remains a **shared contract**, but pcc-Python is its sole production owner.

The port has no struct to lean on; it restates the layout with raw memory accesses:

```python
n_fields_i32: int = load_i32(cls, 72)
field_names = load_ptr(cls, 80)
...
store_ptr(cls, 96, func)          # del_method
store_ptr(cls, 112, metaclass)    # metaclass
```

Every numeric literal is an act of blind faith in the C struct. This is why [AGENTS.md](../../AGENTS.md) states it as iron law: "The pcc-Python mirror in [pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py) must match the C `PyClassObject` in [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h) exactly. Layout drift between them is a recurring class of bug." Change the C struct without changing the port and there is no compile error and no link error — only, at runtime, some offset whose "field" has silently become its neighbor's bytes.

Reading the port also shows that mirroring is not transcription; it is **the same predicate restated in a different coordinate system**. The C `pointer_can_have_header()` checks `bits < 0x1000`, `bits & 0x7`, `bits >> 48`; the port's `_ptr_can_have_header()` receives `untag_int(o)` (the pointer arithmetically shifted right by one), so the same checks become `bits < 2048`, `bits & 3`, `bits >= 2**47` — every constant divided by two, because the coordinate system has shifted by one bit. Miscopy any one of them and the predicate silently admits (or rejects) a whole class of pointers. Likewise, the port inlines constants like `PY_TYPE_CLASS = 10`, 120, and the offsets **at the point of use** rather than reading module-level constants, and its docstring records the i32/i64 ABI details: a C-ABI `int32` parameter is forced to i32 in pcc-Python by the `: int` annotation, while arithmetic inside a function body runs at pcc's default i64 — to avoid width mismatches at call boundaries, the port prefers inlining logic over calling helper functions that take int parameters. All of this is texture that the bootstrap chain (Chapter 15) trod out the hard way.

The discipline therefore has three operational forms:

1. **A layout change is a bilateral commit.** The C struct and the port's docstring/literals move in the same change; the offset table in the `py_class.py` module header is the de facto spec — update the table first, then the code.
2. **Test in default mode, not under `PCC_RUNTIME_CC=cc`.** If your bug fix lands in a C file that belongs to `PY_MODULES` (where the default build links the port), default mode never executes your C fix at all — the repository has on record four slices that got false confidence from cc-mode testing (Chapter 14).
3. **Behavior may differ convergently; layout may not differ at all.** Pre-filling `del_method` (C oracle) versus lazy filling (the pcc-Python production implementation) is a legitimate difference, because the observable ABI behavior is identical; offset 96 versus offset 88 is not a difference, it is corruption.

## 7.6 "The Class Clearly Defines X": The Three-Cause Check Order

Stack the machinery of Sections 7.2–7.5 together and the triage mantra from [AGENTS.md](../../AGENTS.md) explains itself. Symptom: a real program reports `AttributeError: object has no attribute X`, and the class source plainly defines `X`. From Section 7.4 we know this message is produced in exactly one place, `py_obj_missing_attr()`, which means **all seven** lookup layers came up empty. The three causes, ordered by hit probability and verification cost:

1. **Layout drift (the C ABI `PyClassObject` vs. `py_class.py`).** If the ABI declaration and production implementation disagree about any of `n_fields@72`, `field_names@80`, or `methods@64`, then `lookup_field_index()` scans over garbage and `_class_lookup_in_mro()` reads the wrong method table — the lookup does not crash, it just never hits. Verification: check `py_internal.h` against the offset table in the `py_class.py` docstring line by line; check whether a recent diff touched the layout on one side only.
2. **A missing `pcc_gc_load_ptr()` barrier (backends #3/#4).** Pointer slots must be read through `pcc_gc_load_ptr()` and written through `pcc_gc_store_ptr()`; a bare `obj->slot` read is perfectly fine on the default backend #0 but may, on generational #3 or relocating #4, return the pre-move address — and the "class" at the old address yields a method table full of noise. Verification: rerun with `PCC_GC_BACKEND=0`; if the symptom disappears, you can almost certainly pin it on a recently added bare slot access, then hunt for the unbarriered read or write on the implicated path. (Barrier semantics themselves are Chapter 10.)
3. **A missing `py_err_occurred()` check.** pcc's exceptions are "store in TLS, return normally" (Chapter 8): after an earlier call fails, if generated code or the runtime skips the `py_err_occurred()` check, the NULL propagates as if it meant "not found" and the error finally surfaces on an unrelated attribute — or, the other way around, a stale pending exception makes `py_obj_missing_attr()` decline to raise, and the symptom drifts even further away. Verification: from the symptom site, walk back to the most recent call that can raise and check that every call site after it has an err-check branch.

The mantra's closing sentence matters just as much: "Check in that order before suspecting frontend codegen." All three causes live in the object model / runtime layer, and all three are more common than "the frontend lowered the attribute name wrong." Eliminate the cheap, high-probability causes first; only then open the code generator.

## 7.7 History and Lessons

All three stories in this section come from the records in [docs/investigations/](../../docs/investigations), in the format the style contract demands: symptom → wrong hypothesis → evidence chain → real root cause → the invariant left behind.

### Story one: the class-variable play in three acts — how the `attrs` slot grew

The runtime storage for class-level variables (`class C: x = 1`) went through three forms, and the sequence is a complete demonstration of how the mirroring discipline constrains design.

Act one (the "Why v2" section of [docs/investigations/goal-data-model-b3-classvar-v2-0416-0425.md](../../docs/investigations/goal-data-model-b3-classvar-v2-0416-0425.md)): the first attempt simply added an `attrs` field to `PyClassObject`. It was rejected — the stated reason is blunt: "That was wrong for pcc because `py_class.py` mirrors `PyClassObject` using hard-coded offsets. Changing the C layout would silently desynchronize the pcc-Python runtime mirror." Note that this is not "the layout can never change"; it is that this particular change was not prepared to pay the bilateral cost.

Act two (same file): v2 switched to a C side table, `PccClassAttrsNode{cls, attrs, next}`, with the dicts pinned by `pcc_gc_pin()` to give the tracing backends a stable root, "without adding a new class-layout trace edge" — the layout stays put, at the cost of an object-graph edge hidden inside a side table.

Act three (today's `py_class_attrs.c` header comment plus `py_internal.h`): `attrs` moved into `PyClassObject` after all (offset 104, an owned reference), and the side table was demoted to a non-owning pointer index. The motive is also in the comment — "so moving collectors can trace and update the edge directly": a moving collector needs this edge to live in the object itself. This time both sides moved together: the port's docstring and every literal were synchronized to 120 bytes. The companion Backend #3 productionization investigation ([docs/investigations/gc-backend3-class-metadata-slot-rewrite.md](../../docs/investigations/gc-backend3-class-metadata-slot-rewrite.md)) supplied the other half: the **borrowed** slots — `bases[]`, `mro[]`, `methods[].func`, `del_method` — take no part in reference counting, yet must be rewritten to the post-move addresses at generational promotion. A focused test first demonstrated the failure empirically as `['1','0','0','0']` (the method was installed; the slot was never rewritten); after the fix, the C and port runtimes each passed their own gate.

**The invariant left behind:** the layout is shared property between mirrors, and changing it is a bilateral transaction; "borrowed reference" describes ownership, not GC visibility — every class-metadata slot is an edge of the object graph.

### Story two: pcc1 loses `_generator_ctx` — the default-None slot and the setattr trap (2026-05-11)

Symptom: a bootstrap regression. `pcc1` (the stage1 self-produced compiler) failed to compile any file containing a generator, reporting `Layer 1 unknown function _yield`; the identical source compiled fine under stage0 (host CPython running pcc). The baseline had been green on 2026-05-01, with roughly thirty commits in between ([docs/investigations/pcc1-self-host-generator-ctx-slot.md](../../docs/investigations/pcc1-self-host-generator-ctx-slot.md)).

Wrong hypothesis one (Proposal No.1, DENIED): suspected corruption in a newly added yield-sentinel cache. Short-circuiting the cache left the symptom unchanged, and a probe proved the detection function returned the correct result every time — the bug lived downstream, in the emission path.

Wrong hypothesis two (Proposal No.2, DENIED): pre-declare `self._generator_ctx = None` in `L1CodeGen.__init__` so the attribute "exists." After rebuilding pcc1, stderr probes showed: `_emit_generator_resume_function` demonstrably executed `self._generator_ctx = {...}`, yet at every subsequent statement-emission site the read-back was still `None` — **the assignment did not persist**. The investigation matched it to an already-recorded pattern: a default-None, dataclass-style slot that is later overwritten with `obj.attr = value` is unreliable under pcc1's runtime setattr path; pre-filling `None` does not reserve a writable slot, it just buries the trap earlier.

The fix (Proposal No.3, CONFIRMED): switch to `self._generator_ctx_stack: list = []` — give the slot a **real container object** at construction time, then only `append`/`pop` in place, never reassigning the attribute. The value identity of the slot (that one list) never changes for the object's lifetime, bypassing the setattr path entirely. pcc1 could then compile generators; the `Value.bitcast` argument-count error exposed immediately afterward was proven to be a **second, independent failure** (the Update section of the same file: assignment-time registration of the local alias `tmp_builder = ir.IRBuilder(entry)` did not take effect under pcc1; the workaround was to write through `self.builder` directly). The two evidence chains were recorded separately — a textbook application of bootstrap-regression discipline rule 3, "separate stacked failures."

The honest part must be written in full: **the underlying root cause has never been isolated**. The investigation explicitly labels both the list-as-stack change and the direct builder write as workarounds; the divergence of `_emit_assign` under pcc1 remains an open problem, and the follow-up audit points at every attribute whose first `self.X = ...` happens inside a method without a declaration in `__init__`.

**The invariant left behind:** in bootstrap-sensitive code, bind an instance slot to its final container object at construction time and mutate in place instead of reassigning the attribute; one bootstrap regression = one boundary + one evidence chain — stacked failures get recorded apart.

### Story three: who owns a field index — the drift of the schema test

Symptom: `tests/python/test_py_class_export_schema.py::test_pcc_cross_module_class_schema_matches_local_layout` failed after the layer1 split (Chapter 6): `L1CodeGen.__init__.self.env not found` ([docs/investigations/python-class-export-schema-test-mixin-init-drift.md](../../docs/investigations/python-class-export-schema-test-mixin-init-drift.md)).

The test originally parsed `layer1.py` as an AST, inferred the field index of `self.env` from its source order of appearance inside `__init__`, and then asserted that the IR's `py_instance_get_field` used the same index. After the mixin refactoring moved `__init__` into `layer1_entrypoints.py`/`layer1_init.py`, repointing the parser was only a partial fix (PARTIAL): AST source order said 38; the IR actually used 94. Root cause: pcc's class layout comes from **`ClassInfo.field_names` as merged across the entire mixin stack** (the other face of Section 7.4's "codegen merges the declaration sets"), not from the statement order of any single `__init__`. The final fix (CONFIRMED) rewrote the assertion as cross-emission consistency: collect every `%self.env.* = @py_instance_get_field(...)` line and assert that **all readers use the same index** — preserving the invariant actually worth defending (cross-module agreement) while discarding the coupling that had gone stale (source position).

**The invariant left behind:** a field index is a property of the merged field table; any tool that infers slot positions from source location — a test, a debug script, a human — will drift in the presence of inheritance and mixins. What is conserved is "all emission sites agree," not "equals some source order."

## 7.8 Summary

pcc's object model is built from four interlocking decisions. The 16-byte object header (`refcount@0`, `type_tag@8`, `flags@12`) uses an integer tag instead of a type pointer, buying dereference-free dispatch, defensible pointer validation, a header format shared by five GCs, and mirrorability. The tagged small-int lane conscripts pointer bit 0, making `int`'s value projection allocation-free, at the price of a runtime-wide "ask tagged first" discipline. The 120 bytes of `PyClassObject` pack the MRO, a linear method table, a static field-name table, and the three tail slots `del_method`/`attrs`/`metaclass` into one ABI layout; an instance is "a class pointer + static slots + one hidden dict slot," the attribute protocol stratifies into seven layers by descriptor precedence, and `AttributeError` has exactly one birthplace. Today pcc-Python owns the production implementation while C remains the ABI declaration and differential oracle; byte-for-byte agreement is still a contract enforced by discipline. That is why the triage order for a missing attribute remains layout, barriers, error checks — and only then the frontend.

For how objects die and how references are counted, turn to Chapter 9; for how these slots are traversed, rewritten, and relocated by the five collectors, turn to Chapters 10 and 11.

## Exercises

1. **(Read the source)** Working from [pcc/runtime/src/py_internal.h](../../pcc/runtime/src/py_internal.h), compute by hand the offsets of all 15 fields of `PyClassObject`, mark the four 4-byte padding holes, and verify 120 bytes total with `del_method@96/attrs@104/metaclass@112`; then check the docstring and the literals in [pcc/runtime/py/py_class.py](../../pcc/runtime/py/py_class.py) against your table, and find every site where the port reads or writes each slot.
2. **(Audit)** `PY_TYPE_VALUEBOX = 200` sits inside the user-class tag space, while `g_next_user_tag` in `py_class.c` increments monotonically from 104 with no avoidance logic. The 97th user class to claim a tag will collide with it. Read every consumer of `PY_TYPE_VALUEBOX` in `py_obj_ops_compare.c`, `py_weakref.c`, and `py_format.c`, and describe the observable symptoms after a collision; propose two fixes (skip the number in the allocator / relocate the VALUEBOX tag) and argue the cost of each with respect to the mirror and to already-emitted code.
3. **(Convergence proof)** The C oracle's `py_class_new()` pre-fills `del_method`; the pcc-Python production implementation does not. Using the lazy-fill logic in `py_user_del_dispatch()`, prove that the two are indistinguishable for any user program; then construct a point of difference (observable only with runtime-internal probes) and explain why "observable ABI equivalence" is a sounder differential standard than "statement-for-statement equivalence."
4. **(Design tradeoff)** `PY_CLASS_FLAG_SLOTS_ONLY` reuses the `PY_FLAG_GC_TRACKED` bit `0x2`, with safety resting on the premise that class objects never enter `py_gc_track()`. Suppose a future change lets class objects participate in cycle collection (for example, to support runtime class unloading). List the symptoms through which this bit reuse would surface, and propose a migration plan (hint: which bits remain free in the `py_internal.h` flags space? how many `flags & 2` sites in the port would need synchronized changes?).
5. **(Measurement design)** `py_class_lookup()` is a linear scan, and its comment promises that a "future phase can swap to a hashmap." Design an experiment to decide whether the swap is worth it: which realistic workloads would you measure (hint: the bootstrap stage2 compile, the class-heavy cases under [tests/python/](../../tests/python)), which distributions would you collect (methods per class, lookup hit depth), and at what threshold does the conclusion hold? Explain why a microbenchmark would mislead here.
