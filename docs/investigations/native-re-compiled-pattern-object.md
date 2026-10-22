# Investigation: native re.compile pattern OBJECT (replace literal-alias rewriting; numpy `.cpy.attr.compile`~71)

## Status
active

## Problem Description

`B-P0-PKG` gating feature (a) from the 2026-05-28 NEXT pivot note in
`docs/current-goal-state.md`: the largest counted NumPy pure-Python fallback
family is `@.cpy.attr.compile` (~71 sites in the 2026-05-27 closure
diagnostic). Current native handling is literal-ALIAS rewriting only: a
`re.compile("lit")` bound to a local/module/class alias and used "safely"
with `.match/.search/.findall` (plus `re.split`/`re.findall` pattern-string
storage) is reconstructed per call site; there is NO first-class native
pattern object. Anything outside the alias patterns — `.sub`/`.split`
methods on a stored pattern, patterns passed as arguments, patterns in
containers, dynamic pattern strings, flags via `re.X` constants — falls back
to libpython (`py_cpy_getattr compile`), which strict no-libpython mode
rejects.

2026-06-10 lane re-anchor on the current worktree:

- The opt-in real NumPy first-import boundary gate is green:
  `PCC_RUN_REAL_NUMPY_IMPORT_BOUNDARY=1 PCC_NUMPY_ARTIFACT=$PWD/projects/numpy-2.4.4
  uv run pytest tests/python/test_package_import_path.py::test_pcc1_real_numpy_first_import_boundary_opt_in -q -n0`
  -> 1 passed in 17.38s with the fresh matrix `build/bootstrap-pytest-self/pcc1`.
- The historical count diagnostic form is NOT currently reproducible:
  `pcc1 --backend self --python-libpython=auto --ir-scaffold=on --emit-llvm=DIR main.py`
  (with `PCC_PACKAGE_SITE` pointing at a pcc1-installed real-numpy site, with
  or without `PCC_HOST_PYTHON=/usr/bin/false`) now emits a SINGLE main-module
  IR whose `import numpy` is wholesale-deferred via `@.cpy.modref.numpy` +
  `py_cpy_import` (2 calls), instead of the 2026-05-27 behavior of
  closure-compiling ~149 modules before the `.owned.N` generator emission
  failure. This matches the 2026-05-27 "optional external import fold"
  change; the ~71 count is therefore carried from the 2026-05-27 diagnostic
  and the re-track lowering has not changed since.

## Repro

Strict-mode red anchor for the missing feature (host pcc, current worktree):

```bash
cat > /tmp/pat_probe.py <<'EOF'
import re

PAT = re.compile("a(b+)c")

def scrub(s: str) -> str:
    return PAT.sub("X", s)

def main():
    print(scrub("zabbczabc"))
    m = PAT.match("abbc")
    if m:
        print(m.group(1))

main()
EOF
env -u LC_ALL uv run pcc --backend self --python-libpython=off --ir-scaffold=on /tmp/pat_probe.py -o /tmp/pat_probe_bin
```

Observed (2026-06-10): `PCC-PY-COMPILE-001 ... requires libpython fallback
... generated IR still calls py_cpy_* helpers`. CPython prints `zXz X` then
`bb`.

## Test [CONFIRMED]

The off-mode compile above fails with `PCC-PY-COMPILE-001` on the current
worktree (observed 2026-06-10). Focused regressions for each implementation
phase are to be added under `tests/python/` as the phases land.

## Proposals
- No.1 Phase (i): runtime `py_re_compile` returning a first-class native pattern object + dynamic `.match/.search/.findall` dispatch   [pending]
- No.2 Phase (ii): pattern-object `.sub/.split/.finditer` + match-object `.group/.groups/.start/.end/.span`   [pending]
- No.3 Phase (iii): flags — native `re.I/re.M/re.S/re.X` int constants and `re.compile(pat, flags)` honoring them   [pending]
- No.4 Phase (iv): retire the literal-alias rewriting in favor of the object (keep as optimization only if byte-identical semantics proven)   [pending]

## No.1 Phase (i) design

### Code Change (planned)

- Runtime: C-only helper file (per the repo's C-only OBJ_PY_CC_HELPERS
  pattern for helpers the pcc-Python port would awkwardly reimplement):
  a `PyPatternObject` { header, pattern str*, flags i64 } with a dedicated
  type tag; `py_re_compile(PyObject *pattern, int64_t flags)` returns it;
  `py_re_pattern_match/search/findall(PyObject *pat_obj, PyObject *s)`
  delegate to the existing native regex engine entry points that already
  power the alias rewriting. Generic method dispatch for the new type tag is
  registered where the runtime resolves builtin-object methods (same
  mechanism as str/list method dispatch), so `p.match(s)` works through
  `py_obj_call_method*` with no frontend special case at the call site.
- Frontend: `native_text_modules.py::_emit_native_re_call` lowers
  `re.compile(pattern[, flags])` to `py_re_compile(...)` for ARBITRARY
  pattern expressions (not just literals); the result is an ordinary
  PyObject* value, storable in globals/locals/containers and passable as an
  argument. `runtime_abi.py` + allow-list entries.
- The existing literal-alias rewriting stays untouched in phase (i) (it
  short-circuits before generic lowering); phase (iv) decides its fate.

Gates per phase: focused differential regressions vs CPython (pattern
stored at module level, passed as arg, in dict; match/search/findall
results), fallback/no-libpython baselines, GC production contract (new
object type traces its str slot), and the five-GC bootstrap matrix
(runtime + shared lowering touched).

### pending

No code yet; this file records the design and the red anchor for the next
implementation turns.

## Update (2026-06-10): phase (i) as originally designed is DENIED by engine-fidelity inspection; dependency order revised

Code inspection of `pcc/py_runtime/src/py_re.c` (336 lines) shows the
"existing native regex engine" assumed by the No.1 design is a Pike-style
toy matcher, not an engine:

- `py_re_match_impl` returns **`py_True`/`py_None`** — there is NO match
  object and NO group capture anywhere in the runtime; `m.group(1)` has no
  native path.
- The matcher supports literals, `.`, `*`, `+`, leading `^`, an
  ignore-case/dot-all flag pair — no alternation, no `{m,n}`, no character
  classes beyond the atom parser's subset, no groups, no lazy quantifiers.
- `py_re_findall_flags` is hard-coded for exactly two pattern strings
  (`\\b[a-z][\\w$]*\\b` and `\\(.*?\\)`).
- `py_re_compile_method(pattern, flags, method_kind)` builds a per-METHOD
  native closure over a `(pattern, flags, kind)` captures tuple — this is
  the alias-rewriting backend, and the alias restriction is what keeps it
  semantically safe: it only fires for proven (pattern, method) shapes.

Consequence: lowering ARBITRARY `re.compile(...)` to a first-class native
object over this engine would move the safety boundary from compile time to
runtime — `.sub` would raise `AttributeError` where CPython succeeds, and
general patterns would silently mismatch (no groups/alternation). That
weakens Python semantics for a coverage win, which the north star forbids.
The original No.1 phase (i) is therefore **DENIED as designed**.

Revised dependency order (replaces the No.1-No.4 phasing):

- **Phase E0 (gating, multi-turn): faithful regex engine subset.** A real
  parser + backtracking matcher in C supporting literals, character classes
  (`[...]`, ranges, negation), escapes (`\\w \\s \\d \\b` + literal
  escapes), anchors `^ $`, `.`, quantifiers `* + ? {m,n}` (greedy + lazy),
  alternation `|`, and capturing/non-capturing groups — with a STRICT
  parser that returns "unsupported" for anything else (lookaround,
  backrefs, named groups, conditionals) instead of guessing. Built
  test-first with CPython differential gates per feature; no frontend
  wiring until the subset is proven.
- **Phase E1: compile-time subset gating.** For LITERAL patterns the
  frontend asks the engine's parser whether the pattern is inside the
  supported subset; only then lower `re.compile` natively (pattern object).
  Dynamic or unsupported patterns keep today's compile-time cpy fallback —
  no runtime surprises, counts shrink monotonically as the subset grows.
- **Phase E2: match OBJECT** (`group/groups/start/end/span`) backed by the
  engine's capture slots; then pattern-object `match/search/findall`.
- **Phase E3: `sub/split/finditer`** over the same engine.
- **Phase E4: flags constants** (`re.I/M/S/X`) and alias-rewrite
  retirement.

The numpy `.cpy.attr.compile`~71 criterion is gated by E0+E1 at minimum;
this is the honest size of feature (a). The 2026-06-10 boundary-gate green
and the red anchor above remain valid.

## Update (2026-06-10): E0 step 1 landed — standalone engine core + differential gates green

`pcc/py_runtime/src/py_re_engine.c` now holds the step-1 engine: a strict
recursive-descent parser compiling to a patch-list op program, plus a
recursive backtracking matcher with capture slots, iterative fast ops for
single-byte-atom quantifiers, an explicit depth cap (`PCC_RE_LIMIT`), and
upfront non-ASCII text declination (`PCC_RE_NONASCII`). Supported subset:
literals, `.`, classes (`[...]` ranges/negation/class escapes/leading `]`),
`\\d \\D \\w \\W \\s \\S`, `\\b \\B`, `^ $` (incl. CPython's
$-before-trailing-newline rule), `* + ?` greedy and lazy, `|`, capturing and
`(?:...)` groups (32 max). Strictly rejected: `{m,n}`, backrefs, named
groups, lookaround, inline flags, `\\A \\Z \\x`, double quantifiers,
quantified nullable bodies (`(a?)*`), unbalanced syntax, non-ASCII pattern
bytes.

The file is deliberately NOT in the runtime Makefile and has no lowering
wiring — zero impact on compiled paths (no bootstrap claim needed; the only
references are the engine file and its test). Evidence:
`tests/python/test_re_engine_differential.py` -> **164 passed** — 70
supported cases differentially compared against CPython `re` for BOTH
match and search (status + all group spans), 19 outside-subset rejection
cases, non-ASCII declination, and a deterministic 300-pattern fuzz
differential (>= 400 engine-vs-CPython comparisons, seed 20260610) over the
subset grammar.

Next E0 steps: `{m,n}` counted repeats, CPython's empty-iteration rule for
quantified nullable bodies (replacing the rejection), then the E1
compile-time literal-pattern gating design (frontend asks
`pcc_re_engine_supported(...)` before lowering `re.compile` natively).

## Update (2026-06-10): E0 step 2 landed — {m,n} (single-atom), empty-iteration rule; sre iteration-ordering divergence found and fenced

Step 2 additions to `py_re_engine.c`:

- **Counted repeats `{m}` `{m,}` `{m,n}` `{,n}` (greedy + lazy), but ONLY
  over single-byte atoms (CHAR/CLASS/ANY).** The expansion (m chained
  copies + QUES1/STAR1 tail) is enumeration-order-equivalent to sre's
  REPEAT_ONE. Counts capped at 64. Malformed braces (`a{x}`, `a{2`, `a{`)
  remain literals like CPython; a VALID brace with nothing to repeat
  (`{3}`) is a CPython `re.error: nothing to repeat` and is rejected
  (UNSUPPORTED), not matched literally.
- **CPython's empty-iteration rule for quantified can-match-empty bodies**
  via a GENTER/GCHECK guard-op pair: one trailing empty iteration
  participates (its group SAVEs persist), then the loop stops. Verified
  against CPython ground truth: `(a?)*b` on "aab" -> group (2,2);
  `(a*)*` on "aa" -> (0,2)/(2,2); `(a?)+` on "b" -> (0,0)/(0,0);
  `(a|)+x` -> (2,2); `(a*)+` on "b" -> (0,0).
- **Fenced semantic divergence (the step's key finding):** the seeded fuzz
  found, and shrinking minimized, `(.{,3}){,3}?[a]` on `'    a_ba'` where
  full preference-order DFS yields span (0,5)/group (3,4) but CPython
  yields (0,8)/(6,7). `re.DEBUG` confirms CPython parses it as
  MIN_REPEAT over SUBPATTERN(MAX_REPEAT(ANY)) — sre's MIN/MAX_UNTIL
  backtracks only the deepest iteration's inner choices and does not
  revisit earlier iterations, so a backtracking engine that explores full
  DFS order can pick a different (valid-looking) span. Consequence:
  counted repeats over group/multi-op bodies are REJECTED
  (UNSUPPORTED) until/unless sre's exact repeat-stack semantics are
  implemented. `* + ?` over group bodies stay supported — two fuzz seeds
  (300 patterns each, group quantification enabled) found no divergence
  there.

Evidence: `tests/python/test_re_engine_differential.py` -> **216 passed**
(now ~100 table cases x match/search incl. counted repeats, nullable-body
reps, malformed-brace literals; 24 strict rejections incl. the minimized
divergence pattern; non-ASCII decline; two seeded 300-pattern fuzz
differentials). Engine still unwired (not in the Makefile): zero
compiled-path impact, no bootstrap claim.

Next E0/E1 steps: match-window parameters (pos/endpos) if needed by E2,
then the E1 compile-time literal gating wiring (`re.compile` lowering asks
`pcc_re_engine_supported(...)`), then E2 match object.

## Update (2026-06-10): E1a landed — engine wired into native re.match/search; fixed a LIVE silent-divergence bug

Inspection found that `native_text_modules._emit_native_re_value_call`
lowered `re.match`/`re.search` for ARBITRARY pattern expressions straight
into the toy matcher with no pattern gating — a live correctness bug:
strict no-libpython `re.match("a|b", "b")` compiled natively and printed
`False` where CPython matches (observed red 2026-06-10), and unsupported
patterns silently mismatched instead of failing.

E1a wiring (bootstrap-verified):

- `pcc/py_runtime/src/py_re_engine_obj.c` (new, C-only helper, no port):
  `py_re_engine_truth(pattern, text, search)` returns `py_True`/`py_None`
  via the faithful engine; raises `NotImplementedError` for
  outside-subset patterns / non-ASCII text and `RuntimeError` for the
  engine depth limit, instead of silently diverging.
- `py_re.c` and the pcc-Python port `py_re.py` both route
  `_re_match_impl` with `flags == 0` through that bridge (port via
  `extern`, the C-only-helper pattern); `flags != 0` keeps the legacy
  ignore-case/dot-all toy path (documented gap, engine flags are E4).
- Makefile: engine + bridge added to `SRCS` AND to `OBJ_PY_CC_HELPERS`
  (with explicit `build_py/` rules) so BOTH `libpy_runtime.a` and the
  default-mode `libpy_runtime_pcc_py.a` carry them — first build failed
  with `_py_re_engine_truth` undefined precisely because only `SRCS` was
  updated (the pcc-py archive uses the explicit helper list).
- Frontend: `_emit_post_call_err_check` added after the three direct
  `py_re_match*/py_re_search*` emission sites so the new runtime raises
  surface as Python exceptions instead of festering in TLS.

Observed gates: focused `tests/python/test_native_re_engine_wired.py`
-> 3 passed (red-first: `['False', ...] != ['True', ...]` and
`no-raise != raised`); engine differential + `test_native_re_escape.py`
-> 217 passed; GC common contract -> 130 passed; fallback/no-libpython
baselines -> 18 passed; touched-file `py_compile` + `git diff --check`
clean; mandatory five-GC bootstrap matrix
`tests/python/gc/test_pcc_bootstrap_full_gc{0..4}.py -q -n0` -> 5 passed
in 404.73s (fresh stage2/stage3, normalized `pcc2 == pcc3`).

Not proven: `re.compile` pattern objects (E1 gating + E2 match object
still pending), engine flags support, findall/split engine backing,
`.cpy.attr.compile` count reduction, or `import numpy`.

## Update (2026-06-10): E2 landed — real Match object from flags==0 native re.match/search

`py_re_engine_obj.c` now builds a first-class Match-like object instead of
`py_True`: a `PY_TYPE_INSTANCE` of an immortal C-created `re.Match` class
(`py_class_new` singleton), whose instance attrs hold per-method native
closures (`py_func_new_named` over a `(text, spans, kind)` captures tuple —
the same closure convention as `py_re_compile_method`). Implemented
methods: `group([i])` (str or None for unmatched groups; `IndexError: no
such group` out of range; >=2 args raise NotImplementedError), `start([i])`
/ `end([i])` (CPython's -1 for unmatched), `span([i])` (2-tuple), and
`groups()`. Truthiness stays correct (instance truthy / py_None), so all
E1a truthiness consumers are unchanged; instance-dict functions are called
WITHOUT self-binding, which the closure design relies on.

Observed gates (2026-06-10): focused
`tests/python/test_native_re_engine_wired.py` -> 4 passed (the new
match-object probe checks group 0/1/2/default, start/end, span via
subscripts, groups(), None group, start -1, IndexError — all equal to a
CPython ground-truth run of the same program); engine differential +
re-escape suite -> 217 passed; GC common contract -> 130 passed;
fallback/no-libpython baselines -> 18 passed; `py_compile` +
`git diff --check` clean; mandatory five-GC bootstrap matrix -> 5 passed
in 429.37s (fresh stage2/stage3, normalized `pcc2 == pcc3`).

Not proven / next: E1 `re.compile` pattern OBJECT + compile-time literal
gating (now unblocked: pattern methods can return real Match objects),
engine flags (E4), findall/split engine backing (E3), `m[g]` subscript,
named groups, `.cpy.attr.compile` count reduction, `import numpy`.

## Update (2026-06-10): E1 landed — first-class re.compile pattern object with compile-time subset gating

The feature this investigation was opened for now exists in its first
working form:

- Runtime (`py_re_engine_obj.c`): `py_re_compile_obj(pattern, flags)`
  builds a `PY_TYPE_INSTANCE` of an immortal C-created `re.Pattern` class
  carrying `.pattern` plus native `.match`/`.search` closures (captures
  `(pattern, kind)`) that delegate to the E1a/E2 runner and return real
  Match objects. The constructor re-validates: TypeError for non-str,
  NotImplementedError for flags != 0 or outside-subset patterns
  (construction-site visibility).
- Frontend (`native_text_modules.py`): expression-position
  `re.compile(<literal>[, flags])` lowers to `py_re_compile_obj` ONLY when
  static flags == 0 AND the new conservative checker
  `_re_engine_subset_supported` approves; otherwise the existing fallback
  paths are untouched. The checker mirrors the C parser's accept/reject
  decisions and MUST stay a subset of the engine; the inclusion is pinned
  by `test_frontend_checker_subset_of_engine` (corpus + 400-pattern fuzz
  sweep, which immediately caught one real violation during development:
  `a{99}` — valid syntax over the engine cap is UNSUPPORTED in the engine
  but was initially treated as a literal brace by the checker; also
  quantifier-then-brace is engine-rejected unconditionally).
- Self-host discipline: the checker's first draft used host-Python idioms
  (set unions, typing generics, closures, genexprs) and the fallback
  ratchet immediately flagged `pcc.py_frontend.codegen.native_text_modules`
  at 27 contextual fallbacks vs pinned 0 — rewritten in the bootstrap-safe
  dialect (str-constant membership, int state codes, explicit loops,
  slice compares); baselines back to 18 passed with no recapture.
- The safe-alias rewriting path is untouched and now coexists: alias forms
  keep the compile-time shortcut, while non-alias forms (module globals
  passed as arguments, dict storage, locals) get the real object.

Observed gates (2026-06-10): focused
`test_native_re_engine_wired.py` -> 5 passed including the new
pattern-object probe (module-level PAT, passed as a function argument,
stored in a dict, `.pattern` attr, local compile — all values equal a
CPython ground-truth run); checker pin + wired -> 6 passed; engine
differential suite -> 217 passed; GC common contract -> 130 passed;
fallback/no-libpython baselines -> 18 passed; `git diff --check` clean;
mandatory five-GC bootstrap matrix -> 5 passed in 474.59s.

Not proven / next: `.sub`/`.split`/`.findall`/`.finditer` on the pattern
object (E3, needs engine-backed scan/replace), engine flags (E4),
`m[g]`/named groups, dynamic (non-literal) patterns, `.cpy.attr.compile`
count reduction measurement, `import numpy`.

## Update (2026-06-10): E3 findall landed — engine-backed scan + a pre-existing safe-alias null-modvar trap fixed

- Core: `pcc_re_engine_run_from(..., start, ...)` generalizes the runner
  with a scan start position (old entry delegates with start=0).
- Runtime: `py_re_engine_findall(pattern, text)` implements
  CPython-faithful findall over the engine: group-0 strings for 0 groups,
  group-1 values for 1 group, tuples for >=2, `''` for unmatched groups,
  empty matches advance by one byte (`a*` on "baa" -> `['', 'aa', '']`);
  outside-subset/non-ASCII/limit raise like the truth runner.
  `py_re_findall_flags` (C and the pcc-Python port) routes flags==0
  through it; the legacy two-hard-coded-shape scanner only serves
  flags!=0 now. The pattern object gains `.findall`.
- Frontend: the findall pattern-shape gates are REMOVED at all three
  emission sites (module call, alias method call, compile-method attr) —
  consistent with the E1a precedent, the runtime now decides and raises
  honestly; `_emit_post_call_err_check` added after findall emission.
- **Pre-existing trap found and fixed:** safe-alias assignments
  (`PAT = re.compile(lit)` whose uses are all `PAT.match/search/findall`)
  registered a compile-time alias and SKIPPED emitting the assignment, so
  the modvar stayed NULL; method calls lowered inside function bodies did
  not get intercepted (registration ordering) and dynamically getattr'd a
  null object -> runtime AttributeError. Root-caused via IR inspection
  (`@py_obj_getattr(PAT, "findall")` with no `py_re_compile_obj` call in
  the module). Fix: engine-subset flags==0 patterns now SKIP alias
  registration and fall through to a normal assignment carrying the real
  E1 pattern object; alias rewriting remains only for flags!=0 /
  checker-rejected literals where no runtime object exists.

Observed gates (2026-06-10): focused wired suite -> 6 passed (findall
module + pattern-object probes vs CPython ground truth incl. `['', 'aa',
'']` and the unmatched-group `''` rule; honest NotImplementedError raise
for a lookaround findall); re-adjacent regressions
(`test_native_os_misc`, `test_native_re_escape`, engine differential,
recursive-stdlib, textwrap, no-numpy-special-cases) -> 278 + 17 passed;
GC contract -> 130 passed; fallback baselines -> 18 passed;
`git diff --check` clean; five-GC bootstrap matrix -> 5 passed in 458.10s.

Not proven / next: `.sub`/`.split`/`.finditer` (engine-backed replace /
split semantics), engine flags (E4), dynamic patterns, count reduction
measurement, `import numpy`.

## Update (2026-06-10): E3b sub/split landed — the ORIGINAL red anchor is closed; a str-split dyn-receiver SEGV hazard fixed

- Runtime: `py_re_engine_sub(pattern, repl, text, count)` (CPython-faithful
  scan-replace: `re.sub("x*","-","xabc")` -> `--a-b-c-`, count support,
  LITERAL replacements only — backslash templates raise
  NotImplementedError, never expand wrongly) and
  `py_re_engine_split(pattern, text, maxsplit)` (group values inserted,
  None for unmatched groups, empty-match splits, maxsplit). Pattern object
  gains `.sub` / `.split`; module `re.sub` lowers natively (3-4 args), and
  `re.split` falls back to the engine when the legacy literal-separator
  gate declines.
- **SEGV hazard found and fixed (pre-existing, generic):** the
  dyn-receiver `.split` STR-method fast path lowered `PAT.split(s)` to
  `py_str_split(PAT, s)`, which cast the re.Pattern INSTANCE to
  `PyStrObject*` unchecked -> SIGSEGV (IR-confirmed:
  `@py_str_split(ptr %PAT...)`). Any non-str object with a `.split`
  method hit the same crash. Fix: `py_str_split` / `py_str_split_maxsplit`
  (C AND pcc-Python port) now type-check the receiver and dispatch
  generically via getattr + `py_obj_call` — NOT `py_obj_call_method1`,
  which prepends the receiver and broke instance-dict function attributes
  (observed as TypeError with the instance in the text slot before the
  final fix).
- **The investigation's opening red anchor is CLOSED:** the exact repro
  program (`PAT = re.compile("a(b+)c")` + `PAT.sub("X", s)` +
  `PAT.match(...).group(1)`) that failed off-mode with
  `PCC-PY-COMPILE-001` now compiles strict no-libpython self-backend and
  prints `zXzX` / `bb`, byte-identical to CPython.

Observed gates (2026-06-10): wired + engine + re-adjacent suites -> 285
passed (incl. the new sub/split probe: `zXzX`, `-a-b-c-`, `--a-b-c-`,
count=2, group-inserted split, empty-match split `['','a','b','c','']`,
pattern-object split with groups, template raise); GC contract -> 130
passed; fallback baselines -> 18 passed; `git diff --check` clean;
five-GC bootstrap matrix -> 5 passed in 580.32s.

Not proven / next: `.finditer`, engine flags (E4), dynamic patterns,
backslash replacement templates, named groups, the numpy
`.cpy.attr.compile` count re-measurement, `import numpy`.

## Update (2026-06-10): E4 flags landed — re.I / re.M / re.S across the whole stack; out-of-mask flags raise instead of silently mismatching

- Engine core: `pcc_re_engine_run_flags(pattern, flags, ...)` (older
  entries delegate with flags=0). `re.I` folds character CLASSES at
  compile time BEFORE negation (so `[^a]` correctly rejects `'A'`) and
  folds CHAR atoms at match time; `re.M` makes `^` match after any
  newline and `$` before any newline; `re.S` lets `.` match newline.
  Flags outside the I|M|S mask return UNSUPPORTED.
- Both runtime tiers (`py_re.c` + port `py_re.py`) route mask-subset
  flags through `py_re_engine_truth_flags` / `py_re_engine_findall(flags)`;
  out-of-mask flags now raise NotImplementedError — the legacy toy
  matcher is dead code (kept behind `if (0)` for reference) instead of a
  silent-divergence path. sub/split carry a flags parameter end to end.
- Pattern object captures `(pattern, kind, flags)`; `py_re_compile_obj`
  validates the mask; the frontend compile lowering and the safe-alias
  fall-through accept static `re.I/re.M/re.S` combinations (the
  `_RE_CONSTS` table already mapped I/IGNORECASE/M/MULTILINE/S/DOTALL).
- This unlocks the FLAGS dimension of numpy's `f2py/crackfortran.py`
  cluster (heavy `re.I`); the remaining blockers there are named groups,
  backrefs, and dynamically built patterns.

Observed gates (2026-06-10): engine differential -> 256 passed (19 flag
cases x match/search incl. fold-before-negate, ranges under I, M anchors
mid-string, S dot, I|M|S combo; out-of-mask rejection); wired suite -> 7
passed (flags probe: `[^a]` vs "A" False, M/S probes, `re.compile(p,
re.I)` pattern-object group "BB", M-mode findall, `re.X` honest raise);
re-adjacent 61 passed; GC contract 130 passed; fallback baselines 18
passed; `git diff --check` clean; five-GC bootstrap matrix -> 5 passed in
447.71s.

Not proven / next: `.finditer`, named groups `(?P<...)`, backrefs,
dynamic patterns, backslash replacement templates, re.X/re.A, the numpy
surface re-measurement after flags, `import numpy`.

## Update (2026-06-10): named groups `(?P<name>...)` landed across the stack

- Engine: `(?P<name>...)` parses as a normal capturing group with the name
  recorded in a per-prog name table (identifier syntax enforced, duplicate
  names rejected as UNSUPPORTED — CPython errors on them, 31-char cap);
  `(?P=name)` backrefs and other `(?...` forms stay rejected. New export
  `pcc_re_engine_group_names(pattern, out, len)` writes NUL-separated
  names for groups 1..N (empty string = unnamed).
- Match object: captures extended to `(text, spans, kind, names)`;
  `group/start/end/span` accept a STRING argument resolved through the
  names tuple (unknown names raise CPython's `IndexError: no such group`),
  and a new `groupdict()` method returns `{name: value-or-None}`.
- Frontend checker accepts `(?P<ident>` with the same duplicate-name
  rejection (inclusion still pinned by the differential corpus + fuzz).

Observed gates (2026-06-10): engine differential -> 267 passed (named
patterns compared span-for-span vs CPython incl. optional named groups and
mixed named/positional numbering; `(?P<a>x)(?P<a>y)` / `(?P=name)` /
`(?P<1bad>x)` / `(?P<>x)` rejections); wired -> 8 passed (the named-group
probe checks `group("major")`, positional mixing, `start("minor")`,
`groupdict()` values incl. None, and the unknown-name IndexError — all
equal to CPython ground truth); re-adjacent 61; GC contract 130; fallback
baselines 18; `git diff --check` clean; five-GC bootstrap matrix -> 5
passed in 452.98s.

crackfortran-cluster status: flags (E4) DONE, named groups DONE; the
remaining blockers there are backrefs `\\1`/`(?P=name)` and dynamically
built pattern strings. Not proven: `.finditer`, backrefs, dynamic
patterns, replacement templates, re.X/re.A, the numpy surface
re-measurement after these two, `import numpy`.

## Update (2026-06-10): \\A/\\Z anchors landed; surface re-measured — remaining compile sites are DYNAMIC patterns

Post-flags/named-groups sweep (same per-module recipe): `.cpy.attr.compile`
64 -> 60 -> (after \\A/\\Z) **59**; `.cpy.attr.sub` 3 -> **0**; re family
~103 -> **91**. Classifying crackfortran's 72 `re.compile` sites by AST:
40 literal (21 already approved; **19 rejected, ALL containing \\A/\\Z**,
17 of which become checker-approvable with anchor support — the other 2
are lookaround) plus **32 dynamic/non-literal** sites.

`\\A` (absolute start, ignores re.M) and `\\Z` (absolute end, NO
trailing-newline rule) are now engine ops + checker-accepted; differential
suite covers `foo\\Z` vs "foo\\n" (None where `$` matches) and the re.M
non-interaction -> 283 passed.

KEY measurement insight: the marker count barely moved (60 -> 59) because
the 19 rejected LITERALS never produced `.cpy.attr.compile` markers — safe-
alias registration suppressed the assignment emission entirely, so their
fallback surfaced at USE sites instead. The remaining ~32 compile markers
correspond to the 32 DYNAMIC pattern sites. A minimal probe confirms the
mechanism end to end: a crackfortran-style named-group + \\Z + re.I literal
lowers to `py_re_compile_obj(..., 2)` with zero fallback markers.

CONSEQUENCE for lane priorities: more pattern-syntax features now have
diminishing returns on the numpy surface; the next big lever is the
DYNAMIC-pattern lowering policy (runtime-gated `re.compile` for non-literal
patterns — a semantics-policy design: auto-mode would trade cpy fallback
for runtime NotImplementedError on out-of-subset dynamics), and the two
lookaround literals. Gates: differential 283 passed; wired+re-adjacent 69
passed; GC contract 130; fallback baselines 18; diff clean; five-GC matrix
-> 5 passed in 417.80s.

## Proposal (pending): off-mode-only dynamic `re.compile` lowering — requires libpython-mode plumbing into L1CodeGen

The clean zero-tradeoff design: lower `re.compile(<non-literal>)` to
`py_re_compile_obj` (runtime-validated; raises for out-of-subset) ONLY
when compiling with `--python-libpython=off` — in off mode those sites are
COMPILE ERRORS today, so this is a strict capability gain; auto/on keep
their cpy fallback, so no behavior regresses. BLOCKER found by
inspection: `L1CodeGen` does not receive `libpython_mode` (off-mode
enforcement happens downstream in the pipeline by counting `py_cpy_*` in
the generated IR), so the emitter cannot condition on the mode today.
Implementing this requires plumbing `libpython_mode` through the
`L1CodeGen` constructor (touching the pipeline call sites and the
self-host class-export schema, i.e. a schema-test + bootstrap slice of its
own). Unconditional dynamic lowering was REJECTED: it would convert
auto-mode cpy-working out-of-subset dynamics into runtime
NotImplementedError raises. Status: pending — next package-lane
implementation turn.

## Update (2026-06-10): fresh numpy re-surface measurement — mechanism landed, numpy counts gated by out-of-subset patterns

New reproducible recipe (replaces the unreproducible 2026-05-27 closure
diag): per-module library-mode sweep over the re-using modules of a real
pcc1-installed numpy site —

```bash
# site from: pcc1 -m pip install numpy --no-index --find-links projects/ \
#   --abi cpython-compat --target SITE --cache-dir CACHE --json
# for each non-test numpy module matching \bimport re\b|re.(compile|...):
env -u LC_ALL uv run pcc --python-library --python-libpython=auto \
  --ir-scaffold=on --emit-llvm=MOD.ll MOD.py
# count "@.cpy.attr.<name>" markers in MOD.ll
```

Results on the post-E3b tree (42 re-using modules, 36 compiled, 6 blocked
by non-re issues: `_core/numeric.py`, `_utils/_pep440.py`,
`distutils/fcompiler/gnu.py`, `distutils/misc_util.py`,
`lib/_npyio_impl.py`, `linalg/lapack_lite/fortran.py`):

```text
.cpy.attr.compile 64   .cpy.attr.match 16   .cpy.attr.search 8
.cpy.attr.split   12   .cpy.attr.sub    3   .cpy.attr.findall 0
ALL .cpy.attr.* markers: 3856  (re family ~103 ~= 2.7%)
```

Interpretation (claim hygiene): the seven re-lane closures built the
MECHANISM (subset patterns now run native and faithful end to end), but
numpy's remaining re fallbacks are dominated by OUT-OF-SUBSET patterns
the checker correctly declines — `f2py/crackfortran.py` alone holds 54 of
~103 (named groups `(?P<...)`, `re.I/M/X` flags, dynamically built
patterns, backrefs). Closing the numpy count therefore requires engine
coverage growth — E4 flags first (largest multiplier), then named groups
and lookahead — or those modules legitimately stay on fallback. The
broader numpy no-libpython surface is mostly non-re (3856 total markers),
so the re lane should be judged as a generic-mechanism win, not a numpy
P0 surface win yet. The numbers are not directly comparable to the
2026-05-27 closure-diag 71 (different recipe: standalone per-module vs
multi-module closure), but the magnitude matches.

## Update (2026-06-10 evening): post-unblock re-measurement — 41/43 compile; surface is array-ABI-dominated, re ~2.6%

Same per-module recipe over the same pcc1-installed site, after the four
unblock closures (unary dunder, unbound class-method call, generator
native-iter frame slot, bare imported decorators). List regex this run
matched 43 modules (vs 42 prior — minor grep-criterion drift; magnitude
comparable, not a 1:1 ratchet).

```text
modules compiled: 41/43 (was 36/42)
  both blockers = the SAME single cause: the No.3 generator-cpy guard
  (distutils/misc_util.py, _core/src/common/pythoncapi-compat/upgrade_pythoncapi.py)
re family: compile 70, match 16, search 8, split 12, sub 0, findall 2,
  finditer 2 -> sum 110 of ALL 4249 markers (~2.6%); crackfortran still
  46 (named groups / flags / dynamic patterns — checker correctly declines)
top markers overall: dtype 149, shape 106, ndim 95, __eq__ 72,
  append 71, compile 70, info 66, array 65, asarray 57
```

Interpretation (claim hygiene): compiling 5 more modules ADDED ~393 cpy
markers — compile-pass is NOT native coverage; the newly compiled modules
still lean on the cpy bridge heavily. The dominant marker families are
numpy ARRAY attributes (dtype/shape/ndim/array/asarray) — that is the
C-extension ABI boundary, expected to stay bridged — while `append` 71 /
`__eq__` 72 / `__contains__` 42 are potential pure-Python lowering
candidates. The re lane is no longer the main numpy surface lever;
engine-coverage growth (lookaround etc.) would move ~tens of markers at
most. Data-ordered next lever: the No.3 generator-cpy frame design
(single cause, 2 modules, incl. high-fan-in distutils/misc_util.py).


## 2026-10-01: runtime literals reached through the C ABI

The source15 native Context-isolation control returned the expected output but
failed the provenance audit. This was an import-initialization failure, not a
Context assignment failure. Source15 identity was
`110277bc9286c45b57c6f3196557032536bf613d6cf31577f02292e2dd583949`;
its normal owned archive was
`c1133641c8d0658f2ee034f81b96b98d399ef14768d98e53875139d4284bb369`.
The binary `native-changed-v15/context_owner` had SHA256
`6abe115c5279ade33f671b8bacf9bf333c92a2a5ed7e7fe3f8a9adc9d106351c`.
All paths in this update are under `build/remediation-20261001/`.

With `PCC_GC_BACKEND=0 PCC_GC_REFCOUNT_PROVENANCE_PROBE=3`, the same binary
aborted before main (owned child PID 30903). Its copied crash report and
`context-owner-symbolized.json` identify `py_decref` called from
`_normalize_compile_pattern+1288` during `self_backend_parse` regex setup.
Reading the exact binary's BL instruction at +1284 and the matching runtime
IR identifies release of the static string object for `"i"`. The object is
immortal but was absent from the managed-pointer provenance index.

The library contains `_pcc_py_static_literals_py_re_engine_runtime`, but only
Python module initialization called it. Direct C ABI `py_re_compile_obj`
entry does not import that module. The isolated named-group/inline-flag/
verbose program now tracked by
`test_runtime_regex_literals_have_provenance_on_every_backend` reproduced
this with source15: return code zero, correct `INLINE_REGEX_OWNER_OK`, and
unmanaged-pointer stderr. See `regex-literals-v15-red/behavior-focused.json`.
This result is a failure, despite correct program output.

`LiteralLoweringMixin._finalize_static_literal_init` now publishes the existing
registration function through the standard constructor array for managed
modules with actual C ABI exports. Existing owned emitters translate that
array to each target's initializer pointers. No provenance probe is suppressed
and no per-call registration is added to the exported hot functions.

Source16 (`272025a0cde3db69273eee2370b9fa25ecf67d9757ba1e43d6e01a80d3c20294`)
and its archive built successfully, but the first native regex link failed.
Some optimized members had constructor references with no helper definition;
others, including `py_re_engine_runtime`, retained their helper. The inliner's
`_drop_dead_internal_callees` searched only surviving direct calls and deleted
address-taken functions after inlining. It now counts complete global operand
tokens, including initializer/callback references, while excluding string
contents and comments. The callback, pointer-array and constructor regressions
first failed and now pass; exact token/quoted-name boundaries are covered.

The focused optimizer and production-pass constructor/object checks finished
with **35 passed, 2 integration deselected** in
`inline-address-taken-focused-fixed.log`. The earlier broad selection reached
an unmarked native fixture and failed at a read-only cache lock after 15 tests;
it is not green evidence. The native parser/simplifier cases are now explicitly
marked integration and remain required later. Matching new-source archive
link/execution and pcc1/bootstrap qualification are still pending at this update.
