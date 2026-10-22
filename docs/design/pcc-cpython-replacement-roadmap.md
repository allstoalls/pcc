# Product roadmap: pcc1 replaces CPython

Status: human-approved mission and task decomposition; acceptance contracts and
all product gates remain open.

## Mission

The product goal is for `pcc1` to replace CPython as the Python execution root,
not merely accelerate selected functions. Replacement means the published
artifact executes through the self backend and pcc-owned runtime without
starting host Python, linking libpython, silently selecting a CPython fallback,
or relabeling a CPython extension as pcc-native.

The implementation remains faithful to Python semantics. Compatibility is not
obtained by weakening arbitrary-precision `int`, object identity, exceptions,
reflection, finalization, weak references, dynamic behavior or the five-GC
contract.

## Three cumulative product levels

### Level 1 — pcc-native pure-Python service replacement

A supported pure-Python service can install, build, start, serve long-running
work and shut down with `pcc1` as its only Python executable. Script, module,
configuration, file/network/process, concurrency, diagnostics and deployment
paths used by the canary are pcc-owned and pass under GC0..4. The same workload
has a CPython behavioral oracle, but CPython is not present in the pcc run.

### Level 2 — common scientific and build ecosystem replacement

Level 1 expands to a version-pinned scientific/build corpus: NumPy, one build
tool closure and at least one second real native-extension family. Acquisition,
dependency projection, source builds, extension ABI, import, execution and
cleanup are generic mechanisms. Host Python is unavailable for the entire
install/build/run chain.

### Level 3 — supported `python3` drop-in replacement

For one frozen Python language version and platform matrix, `pcc1` satisfies a
published language, stdlib, CLI/import, packaging/extension, diagnostics and
operations compatibility suite. The executable can occupy the supported
`python3` role for that matrix. Any surface outside the frozen matrix is named;
there is no universal claim based on a few applications and no silent escape
to CPython.

The levels are sequencing and evidence boundaries, not reductions of the final
mission. Level 3 is the final product goal.

## Frozen v1 compatibility boundary

The first finite contract targets Python language line `3.13` and uses exact
CPython `3.13.2` behavior as its oracle. A newer CPython patch or language line
is a new reviewed contract revision; it cannot silently change an existing
result.

The initial platform matrix is deliberately narrow:

- `arm64-apple-darwin`, whose production OS boundary is the named libSystem
  ABI. This target never carries a Linux-style zero-libc claim.
- `x86_64-unknown-linux-gnu`, whose final production claim requires a static
  zero-C-runtime/zero-libc artifact. Merely running on a GNU userland does not
  prove that boundary.

Both targets, all three levels and every GC backend remain unverified until a
current clean pcc1 evidence bundle passes the machine-readable contract. The
contract inventory and workload catalog live under `docs/compat/`; their
existence defines the finite work, not a support claim.

The matrix uses three ideas that must not be collapsed:

- `required` means the surface is inside a promised level and must have passing
  evidence before that level can be claimed.
- `unsupported` means the surface is deliberately outside the frozen promise
  and must fail with the matrix's stable diagnostic, without fallback.
- `unverified` is the initial implementation state of every required surface.
  It is neither support nor a permanent exclusion.

A replacement verdict binds the exact contract digest, workload-catalog
digest, source/artifact identities and target. Copying a result to a different
contract revision, platform or pcc1 build invalidates it. Lower levels are
cumulative prerequisites: a Level-3 verdict includes the complete Level-1 and
Level-2 evidence rather than superseding it with a broader but shallower run.

## Evidence rule

Every product gate records:

- exact pcc0/pcc1/pcc2/pcc3 source and artifact identity;
- `backend=self`, `python-libpython=off`, pcc-Python runtime owner and GC kind;
- executed process tree and binary linkage proving no host Python/libpython;
- runtime archive provenance, including the named Darwin libSystem boundary or
  Linux zero-libc boundary;
- CPython oracle version and corpus digest;
- fallback, unsupported-surface and package capability reports;
- correctness, long-running RSS/pause/throughput and stable diagnostic results.

Historical green results and host-pcc runs do not satisfy a current-pcc1
product gate.

The strict bundle format is `pcc.cpython-replacement.evidence.v1`, validated by
`scripts/qualification/cpython_replacement/evidence.py`.  A bundle is valid only when it covers
the exact cumulative workload × target × GC Cartesian product for its claimed
level, binds every run to its workload artifact, records pcc0 through pcc3,
and proves the normalized pcc2/pcc3 fixed point.  Host-built package artifacts,
host/CPython process owners, libpython, CPython-extension ABI linkage, LLVM
runtime fallback, dirty source, or a partial matrix are rejected rather than
being relabelled as replacement evidence.

## Task graph

The `CPY-P0-*` rows in `docs/goal/task-board.yaml` own this mission. Existing
self-host, runtime, semantics, stdlib and package tasks remain the mechanism
owners; the replacement rows join those mechanisms into product-level evidence
instead of duplicating their implementation work.
