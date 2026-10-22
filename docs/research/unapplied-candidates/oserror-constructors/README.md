# OSError constructor candidate

Status: UNRUN source candidate. No Python imports, tests, runtime builds or
native execution have been performed for this patch. Static checks do not
qualify the implementation.

## Scope and mechanism

This narrowly repairs ordinary positional construction of `OSError` and
`FileNotFoundError`, including `IOError`/`EnvironmentError` aliases. Direct calls
use the existing `py_obj_call_slots` argument/result ownership protocol;
first-class calls select the same new runtime constructor.

The runtime keeps the existing 64-byte exception layout, class tags, MRO and
handler matching. Its already traced message slot owns the existing five-item
OS record: args, errno, strerror, filename, filename2. No collector-specific
payload or new object-header flag is added.

The intended ordinary POSIX behavior is:

- Preserve every positional operand and its evaluation order/identity.
- Populate metadata for 2–5 arguments; preserve all arguments with unset
  metadata for 0, 1 or more than 5 arguments.
- Remap an integer errno only for exact OSError/aliases. Explicit
  FileNotFoundError retains its class even with another errno.
- Preserve large integers without truncation and arbitrary errno/message
  objects without coercion or formatting.
- Preserve filename str/bytes/object identity. A non-None filename retains
  only the first two arguments in `.args`; None retains the original tuple.
- Reject nonempty keyword arguments after evaluating the call operands.
- Keep args and metadata independently mutable through the existing owner.

The reference contract is Python 3.15's
[OSError documentation](https://docs.python.org/3.15/library/exceptions.html#OSError)
and [constructor implementation](https://github.com/python/cpython/blob/3.15/Objects/exceptions.c).

## Identities and static verification

The source snapshot was supplied as production `818187ba`; later reported
`ef550d29` changes are documentation-only. The four exact modified preimages
are identified by `baseline.sha256`. The five outputs, including the new
`tests/python/test_os_error_constructors.py`, are in `postimage.sha256`.

- Patch application to a separate baseline copy passed.
- Every applied output matched the recorded postimage hash.
- Reverse application and whitespace/error checks passed.
- The active source and runtime artifacts were not modified by preparation.

The separate open-errno packet changes disjoint files and is not a prerequisite
for this constructor packet.

## Required qualification

Use only the single validation coordinator's verified, pinned interpreter,
isolated source-matched archive, explicit native compiler and watchdog/RSS/lock
procedure. Do not provision environments or overlapping builds. Public commands
do not unnecessarily unset locale variables. The reused execution helper removes
`LC_ALL` from subprocess environments; record that effective environment and all
other effective options.

Host command shape, with the verified absolute interpreter path substituted:

```text
<verified-python> -m pytest -o addopts= -x -n0 -vv --tb=short tests/python/test_os_error_constructors.py -m "not integration"
```

1. Execute the actual-body model over the declared arities and relocation
   phases, plus allocation-failure cleanup and the NULL/empty-args boundary.
2. Inspect emitted IR for direct/alias/starred/keyword calls. Demand the
   authoritative slot-call route and absence of stringification/libpython.
3. Compile changed runtime modules to owned IR and inspect the new ABI:
   `py_os_error_new(i64, ptr) -> ptr`, returning a NEW owner.
4. Explicitly select `test_os_error_constructor_native_five_gc` with
   `-o addopts=` so repository defaults cannot silently exclude integration.
   First use pcc0, then the current native pcc1; both must emit and execute the
   changed source program. Set `PCC_WITH_THREADS=1` only for an archive with
   verified `threads: true` provenance; otherwise match its recorded setting.
5. Run relevant existing exception construction/projection/raise ownership
   tests, sensitive native integrations, then final bootstrap/fallback gates
   after source stability. Existing receipts do not qualify the new source.

## Deliberately unqualified boundaries

- This is not full OSError-family constructor parity. Other explicitly named
  builtin subclasses and user-defined OSError subclasses keep existing routes.
- `BlockingIOError.characters_written` is not implemented. The ordinary
  integer/bool/float/complex third argument remains absent from filename and
  stays in args; its detailed validation is unqualified. A strict expected-
  failure regression records the missing integer `characters_written` field.
- Numeric-protocol objects in that BlockingIOError third position are not
  qualified. No full-family parity may be claimed from this packet.
- Windows `winerror`, Windows-specific conversion and execution are
  unqualified. The ordinary POSIX constructor treats the fourth argument as
  ignored metadata, retaining it only when the original args tuple is retained.
- Runtime model results cannot qualify native ABI or actual collectors.
- No performance, fixed-point, release or completion claim is made.
