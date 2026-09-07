# Investigation: owned simplifycfg reuses a value name after mem2reg/sroa

## Status
active

## Problem Description
Adding the owned `simplifycfg` to the runtime archive's pass list makes the
archive build fail. `pcc/native_ir/simplifycfg.py` emits two definitions of the
same local value when it runs *after* a memory-promotion pass in the same
pipeline, and LLVM rejects the module before an object can be produced:

```
error: multiple definition of local value named 'neg.65.23'
  %neg.65.23 = sub i64 0, 1
```

The failure is an interaction, not a defect in either pass alone. `simplifycfg`
by itself is valid; `simplifycfg` run twice is valid; `mem2reg,sroa` by itself
is valid. Only the composition collides, which points at a fresh-name search
that does not consult the function's current namespace: promotion deletes
definitions and rewrites uses, so the set of names in the function when
`simplifycfg` clones a block is not the set the naming scheme assumed.

The double numeric suffix in `neg.65.23` is the shape to look at. A base name
that already carries one generated suffix is being suffixed again, and two
distinct clone sites can land on the same result.

This is the same family as
[owned-inliner-block-namespace](owned-inliner-block-namespace.md), whose
pending proposal already states the rule this pass also needs: check the
current block/value namespace before choosing a name, and fall back to a
unique per-site prefix on collision. That investigation covers the inliner's
*block* labels; this one is the `simplifycfg` *value* namespace, so the fix
sites are different even though the rule is shared.

## Repro
Reproduces on a real archive module, in-process, with no build:

```python
from pcc.py_frontend.compiled_owned_passes import run_owned_passes
import llvmlite.binding as llvm

src = open("pcc/py_runtime/build_py/py_int_parse.ll").read()   # un-promoted IR
for names in (["mem2reg", "sroa"], ["mem2reg", "sroa", "simplifycfg"], ["simplifycfg"]):
    out = run_owned_passes(src, names, True)
    llvm.parse_assembly(out).verify()      # raises only for the middle list
```

Whole-build form, which is how it was found:

```bash
find /Users/jiamo/my/pcc/pcc/py_runtime/build_py -maxdepth 1 -name '*.o' -delete
env -u LC_ALL PCC_RUNTIME_PYTHON_IR_PASSES=mem2reg,sroa,simplifycfg \
    uv run pcc /tmp/anything.py -o /tmp/anything
# make[1]: *** [build_py/py_int_parse.o] Error 1
```

Note that the `.ll` on disk must be the un-promoted IR for this repro; a
default build leaves promoted IR there, and re-promoting it does not collide.
`$SCRATCH/rt_control/build_py/py_int_parse.ll` in the originating session was a
`PCC_RUNTIME_PYTHON_IR_PASSES=off` snapshot.

## Test [CONFIRMED]
Observed under the commands above on 2026-09-08. The collision is
deterministic and identical from both promotion tiers:

```
weak textual tier -> simplifycfg     FAIL  multiple definition of 'neg.65.23'
owned mem2reg     -> simplifycfg     FAIL  multiple definition of 'neg.65.23'
simplifycfg       -> simplifycfg     OK
```

Both tiers failing is the attribution: the defect predates
`pcc/native_ir/mem2reg.py` and is not caused by it. The owned `instsimplify`,
`instcombine` and `dce` were each built into a full runtime archive and ran the
gateway workload correctly, so `simplifycfg` is the only member of the owned
set that cannot be added today.

## Proposals
- No.1 allocate clone value names against the function's live namespace [pending]

## No.1 allocate clone value names against the function's live namespace
### Code Change
Not written. The shape the sibling investigation settled on applies: derive the
taken-name set from the function as it stands at the moment of cloning rather
than from a cached or pre-pass view, never suffix a name that already carries a
generated suffix, and fall back to a unique per-clone-site prefix when the
preferred name is taken. Do not relax a verifier to accept the duplicate.

### Cost of not fixing it
Bounded and currently small. Measured on the gateway workload with one
compiler and matched archives, adding `instsimplify,instcombine,dce` to the
default `mem2reg,sroa` manifest is worth +2.9% QPS (39812 -> 40981) while
shrinking the archive's static instruction count by 9.8% (369341 -> 333311
lines). `simplifycfg` was expected to be the larger share of that gap, since on
the five hottest modules the four extra passes together close most of the
distance to LLVM `default<O2>`:

```
module           un-promoted   mem2reg,sroa   +4 owned   LLVM O2
py_obj                  5536           4570       3969      3455
py_list                10328           7219       5929      6888
py_gen                  2105           1579       1291      1174
py_gc_backend          21103          17782      14811     14807
py_class               12282           9535       7762      7421
```

`py_list` and `py_gc_backend` reach or beat `default<O2>` with the owned set,
so this naming bug is what stands between the owned pipeline and LLVM parity
beyond mem2reg. It is not on the critical path for the mem2reg work recorded in
[runtime-module-optimizer-throughput](runtime-module-optimizer-throughput.md).
