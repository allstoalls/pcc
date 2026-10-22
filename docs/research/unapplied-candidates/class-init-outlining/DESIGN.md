# Share class-construction code while preserving both module entrypoints

Status: source-only design. No production patch, compiler run, test, profile or
native execution is part of this packet. The ARM worklist experiment remains
closed as HOLD. This proposal addresses the generated bodies that dominated
that experiment rather than changing its fixed-point schedule again.

## Evidence and proposed mechanism

The exact production `pcc` subtree is `d234174fbac80575ea822f6100f65fe914dd4498`
from commit `02b9bc2d3c071bbc3e547a63e045daa73cc6e1bc`. The retained source
container also contains older unrelated test/docs files; it is not represented
as a complete checkout of that commit. File identities are in the manifest.

The correctly targeted host-generated ARM `c_ast` input has 219,614 indexed
instruction records. Its module-top and class-only module-init functions hold
182,845 of those records (83.2574%) and 99.5315% of the measured dense-row-work
proxy. Both converge in one sweep. This is a representation count, not a
CPU-time share or a predicted saving. The full readback is bound by
`representation-readback.json` SHA256
`0a64d58cac8691839ae7e0af71513a44a2469f46df1756dd5aae9048df843e9b`;
its source-only evidence was preserved at commit `1c43e254`. The original
real-module equality/HOLD receipt is preserved at
`a2c5b5e73e482b52a26f43a26289267964bdf190`.

`generation_lowering.py:900–918` emits class-only init and then module-top for
this library shape. `class_gen.py:4724–4780` and
`stmt_dispatch_lowering.py:474–478` lower class construction into both. The
proposal emits each eligible class construction once, in a small private
function, and calls that function from both existing positions. It therefore
has two potential mechanisms: fewer generated instructions and smaller
function-local tracked-value domains for backend analysis. Neither mechanism's
actual magnitude or wall-time benefit is known yet.

Both external entrypoints remain. `pipeline.py:2254–2259` advertises init and
top-init for embedding. Class-only init has no runtime guard; top-init has its
own existing guard and complete source-order module statements. Emission order
is not a claim that runtime callers necessarily execute init before top. The
program-main docstring's ctor statement is not evidence of an actual call.

## First implementation scope

Use a generic, fail-closed eligibility predicate, with the original direct
module-body position retained before closure hoisting. Do not key eligibility
by object identity after inference, class name alone, or the post-hoist module
body. Reject every name in `_hoisted_class_capture_params`, even if its capture
tuple is empty. Module-block classes inside `if`, `try`, loops or `with` are
outside the first version; `_class_defs` alone is not evidence of direct scope.

Admit only ordinary classes whose definition-time construction is closed:

- No class or method decorators, class header keywords, explicit or inherited
  metaclass, dataclass/factory expansion, valueclass, enum or struct-sequence
  specialization. No function-local or nested class construction.
- Bases are unshadowed `object` or established module-class globals with
  ordinary, transitively metaclass-free ancestry. A base need not itself be
  eligible: its runtime global is loaded at the original construction point.
- The class body contains only plain unannotated single-Name assignments,
  ordinary method definitions, docstrings or pass. No class-body control flow,
  binding aliases or definition-time capture expressions.
- Initially admit only `None`, string and recursively closed tuple literals for
  class attributes/default expressions. Keep all existing allocation and
  publication operations. Names, attributes, calls, lambdas, comprehensions,
  unpacking and factory sentinels stay on the original inline route.
- Method executable bodies remain unchanged. Their later runtime use of
  globals or nested closures is not itself a definition-time dependency.
  Reject special raw-pointer/value-storage or nonordinary method ABI shapes
  until separately reviewed.

A stdlib source-AST census of the exact `c_ast.py` finds 55 of 56 class shapes
and 110 methods within these syntactic restrictions. Their attributes are
closed tuples of strings and their defaults are None. `Node` remains inline
because `show(..., buf=sys.stdout, ...)` reads a live external binding. Its
ordinary construction does not prevent its 54 subclasses from qualifying.
The interleaved module function remains in place.

This is a source-shape census, not proof that a future typed predicate admits
55 classes. Inference writes inferred types into `Arg.annotation`
(`type_infer.py:6641–6686`); testing `annotation is None` afterward would be
incorrect. Current method publication uses normalized types for adapter ABI
and evaluates defaults, not annotation expressions
(`class_gen.py:5932–5963`, `user_function_lowering.py:2710–2775,3002–3055`).
The minimal codegen predicate should preserve those normalized type semantics,
not invent a parser/wire flag solely to recover absence of source annotations.

## Helper ABI and control flow

Proposed internal ABI: `i32 ()`, ordinary calling convention, `noinline`.
Return exactly 1 after complete successful publication; return 0 through the
helper's normal error epilogue. Preserve the external void init/top-init ABIs
and main's existing ABI. This is not a noreturn helper.

Use a collision-checked, deterministic private symbol derived from module and
original definition position. Its spelling must avoid the special continuation
markers used by `self_backend_call_flags.py:40–45`. Shared declaration lookup
must verify module ownership, exact type/linkage and the noinline attribute.

Each wrapper calls the helper at the same point where it previously emitted
that class body. A failed status branches directly to the wrapper's original
error/cleanup destination, with no new source-frame append. The first scope
requires a common native/bridge error destination; if a future site has distinct
ones, it must remain inline until that distinction is represented explicitly.

Why status is necessary: `_guard_cpy_value_not_null` and
`_guard_cpy_status_not_negative` can branch on NULL/-1 while PCC's
`py_err_occurred` remains clear (`cpy_call_lowering.py:334–441`). A void helper
followed only by a PCC TLS check would lose this failure signal. The existing
`_ensure_fn_err_exit` returns integer zero for a non-main function
(`exception_lowering.py:1857–1915`), so the status ABI fits that machinery.

Keep `current_func_def=None` in helper generation. Existing source-span and
frame append logic therefore continues to identify `<module>`
(`exception_lowering.py:1410–1542`). Do not add a synthetic traceback frame at
the outline call. Preserve pending exceptions through the unchanged cleanup
chains; do not translate, clear or rebuild them at the boundary.

Do not invoke `_instrument_python_activation`. It adds observable
`py_recursion_enter/leave`, and can introduce a new RecursionError merely from
moving code (`core_helpers.py:173–206`, `py_recursion_runtime.py:42–79`). A code
outline is not a new Python activation. Its physical roots use the existing GC
frame protocol independently of recursion accounting.

Both owned inline pass modes invoke `inline_module`, which rejects `noinline`
before candidate selection (`compiled_owned_passes.py:91–94`,
`optimization/inline.py:72`). The eventual structural test must verify the
attribute and call edges survive actual default passes, not rely on this
source observation alone.

## Function state, ownership and roots

Builder-only switching is insufficient. The new function needs a named,
reviewable save/reset/restore scope based on the existing function-state
inventory (`user_function_lowering.py:1183–1245,1383–1414,1730–1770`) while
retaining module semantic mode. It must isolate:

- Builder, current function, entry block/cursor, debug scope and inline-edge
  insertion anchor; caller env, representation hints and owned-value flags
- Try/bridge cleanup targets; active handlers, finally/generator/loop contexts;
  class namespace and abort contexts; slot-result sinks and temporary roots
- Per-function ownership/root-name collections and cleanup-function/cache state

Restore enclosing state in a finally path after generation failure too. Keep
module-owned declaration/literal pools and function-keyed physical root, error
exit and traceback registries; helper entries must remain available to later
verification. Do not copy caller-owned allocas or SSA values into helper env.
An owned-IR check must reject every cross-function operand reference other than
normal globals/constants/functions. No broad host-state dictionary snapshot or
new global runtime cache is proposed.

Keep `_emit_class_init`'s body and cleanup order intact:

1. Root the fresh class before metadata or callbacks; preserve the unpublished
   class abort/rollback path (`class_gen.py:5017–5049`).
2. Preserve the fresh class namespace owner and lookup rules (`5091–5105`).
3. Release method and factory owners in actual reverse registration order
   (`5230–5258`), with the same lease and pin transitions.
4. Preserve class-root retirement before decorators and final publication
   (`5283–5324`). Excluded decorator/metaclass paths continue inline unchanged.

With `current_func_def=None`, temporary container roots retain lexical LIFO
registration even when runtime threading is enabled
(`literal_lowering.py:298–378`). The helper's normal/error exits must balance
all its physical roots. Module-mode `_emit_owned_local_cleanup` only leaves
persistent registered roots (`ownership_lowering.py:1487–1492`); it is not a
replacement for lexical LIFO cleanup. No extra release of a class or method is
permitted merely because it now lives in a helper.

The no-argument scalar call transfers no managed owner. Caller roots remain
registered while the helper allocates. Both parsed and indexed backend paths
classify an ordinary helper call as a safepoint
(`self_backend_precise_stackmaps.py:3603–3674`). Do not add it to a leaf/skip
allowlist, remove caller root coverage, or substitute empty live SSA sets for
registered-slot root state.

## Semantic boundaries retained

- Freshness: every actual helper invocation builds a fresh class, callable
  objects and signature/default-vector containers. Default expressions are
  evaluated again by the original operations, preserving singleton and pooled
  literal identity. Compile-time shared code is not runtime object caching.
- Source order: class-only init retains declaration order. Top/main retains
  surrounding imports, assignments and function bindings. Existing top guards,
  module root/literal initialization and failed-import state remain outside.
- Global binding: bases load their existing module-class globals at runtime;
  publication still uses the same global. No early class construction or
  aliasing init to top is allowed.
- Dynamic definitions: `class C: x=(); def f(self,a=x): ...`, rebinding `sys`
  before a default, prepared metaclass namespaces, decorators, local captures
  and factory owners keep the existing route. They cannot be generalized by
  claiming that every Name should read a module global.
- Code sharing changes layout, function boundaries, object bytes, PCs and
  stackmap identities. Byte-identical baseline objects are not an appropriate
  acceptance criterion for this proposal. Runtime behavior, public ABI and
  complete per-output root/relocation/stackmap validation are required instead.

## Smallest falsifiable next experiment

No execution is authorized by this document. First freeze a minimal isolated
production/test patch against the exact listed preimages, independently review
it, and preserve it before an admitted gate.

The first host structural gate should use ordinary production parsing,
inference and owned default passes on a small mixed module. It must show one
body and two retained calls per eligible class; unchanged two-entry signatures
and global bindings; fresh constructor/default/method operations still inside
the helper; status-zero branches to the original wrapper error exits; no extra
Python activation/source frame; and no cross-function owner/SSA references.
Explicit excluded controls must remain inline, including empty-capture local
classes, dynamic defaults, metaclass/decorator and nested control-flow classes.
Force both native-TLS and bridge-NULL error exits in focused structural controls
without presenting those controls as real-runtime execution.

Only after that passes, rebuild the actual ARM-target `c_ast` IR through the
same full single-module import context and ordinary route. The old pidx is a
baseline artifact, not a candidate input, because frontend generation changes.
Report actual eligible/rejected classes, public signatures, total post-pass
instructions/blocks, helper call/body counts, maximum B/T/W and summed dense
work. Measure these separately from time; do not infer saving from source
counts. Predeclare a material mechanism threshold before admission, and stop
if retained helpers or generated-count reductions are absent.

The later native gate must call both real exported entrypoints, including
repeated class-only init and the existing top guard, and check fresh class/callable/
default-container identity while preserving None and pooled-literal identity,
source-order effects, exceptions and source-frame order,
root/refcount/finalizer behavior, and actual selected GC events. Dynamic
excluded cases remain coverage obligations. A new helper ABI needs actual
compiler-generated native call-edge coverage; hand-built IR alone is not it.
Host cross-compilation is not Mac-device performance, pcc1 execution or full
Stage1 qualification. A matched-runtime/native and whole-pipeline measurement
plan requires separate review and admission.

## Remaining risks before implementation

The full save/reset/restore inventory and eligibility implementation have not
been emitted or tested. Syntactic coverage must be confirmed after inference
and hoisting. Normal-return temporary retirement and error cleanup need exact
owned-IR and native evidence. Generic dynamic definitions remain excluded;
expanding them requires matching call-site namespace provenance or explicit
capture-slot parameters. No realized speedup or production readiness is claimed.
