# Cold slot-status reporter: owner-edge test successor

This is an unapplied source/test candidate. Production is not activated by this
packet. Its original five-file recovery patch remains byte-for-byte unchanged in
commit `fe054fd02931c1e616a9c287b6d32362290b5e1d`.

The successor changes only `tests/python/test_slot_call_status_reporting_native.py`
relative to that archived candidate. All three production postimages and the host
regression file are identical. The ordinary native program, runtime requirements,
five-GC loop and child deadlines are unchanged. The pcc0-only test now supplies
its explicit current host target, selecting the supported production in-process
frontend branch for the same full import closure. Default frontend subprocesses
would not inherit its observation wrappers. The recorded target and route make
this boundary explicit; it is not default-worker or native-pcc1 evidence.

This also supersedes an unexecuted intermediate test packet whose parent-process
capture would miss the default frontend workers. Its structural driver is unchanged.

The strengthened test observes the three main-function keyword call sites and
parses actual instructions. Each call result must feed its signed-negative branch,
whose error successor calls the private reporter and enters the exact pre-existing
cleanup block. It also checks that the normal successor has no reporter call and
that exception-preserving root cleanup retains its original sequence and slot
identity, including distinct bitcast aliases. Declarations and unrelated functions
cannot satisfy these assertions. Observations and verified edges are saved as
separate result files when the test eventually runs.

## Artifacts and application

- `owner-edge-successor.patch` applies after the archived candidate patch.
- Apply the archived `candidate.patch` first, then this successor. The archived
  patch SHA-256 is
  `4dd5955455944d3b905fd7f5998ac874fd0b815c22d45af13092f9c582259136`.
  This small packet intentionally does not duplicate that complete source patch.
  The manifest also records the reconstructed combined patch identity.
- `manifest.json` lists every exact preimage and postimage, patch identities and
  bounded gate scope. A new ordinary source copy is required. Do not modify the
  canonical recovered tree or use Python import overlays.
- `structural_input.py` and `structural_experiment.py` define the first proposed
  experiment. No test, compiler, native program or profiling run has been made
  while preparing this packet.

Independent source review is complete for the helper's caller-root/safepoint
consumers, actual keyword-call route, and corrected explicit-target observation.
It found no remaining source blocker in this scoped version. Runtime, native compiler, all five collectors and Stage1
performance are still unqualified. An emitted IR reduction must be measured before
using any earlier source-count estimate as evidence of savings.

## First bounded experiment

The coordinator first verifies the restored interpreter against `.python-version`
(`3.15.0rc1`), copies/verifies the canonical source, applies the candidate and checks
all five postimages. All source and packet identities are guarded before and after.

With `PYTHON` denoting that verified interpreter, `PACKET` this directory,
`CANDIDATE` the ordinary candidate source, and `OUT` a fresh result directory:

```sh
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B \
  "$PACKET/structural_experiment.py" --source "$CANDIDATE" --output "$OUT"
```

Run once under the coordinator's shared exclusive performance lock, with the
unchanged 300-second / 4-GiB tree-RSS limit and a 512-MiB free-space floor. The gate
uses normal host parsing, inference and L1 generation for eight real keyword
object calls. Both arms use the same host frontend; the baseline restores only the
reviewed old inline method through the existing test reference class. It records
raw and post-owned-pass function/block/instruction counts, verifies the emitted IR,
and requires the private helper to survive the existing owned inlining pipeline.
It performs no object emission, runtime build or native execution. These small-
program counts do not establish a heavy-module or whole-Stage1 speedup.

If that experiment passes and the coordinator admits the next focused gate, from
`CANDIDATE` run once under its own 300-second / 4-GiB bound:

```sh
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B -m pytest \
  -x -n0 -vv --tb=short -o addopts= \
  tests/python/test_slot_call_status_reporting.py
```

Preserve terminal failures rather than rerunning or widening limits. Native test
nodes are a later, separately admitted phase requiring a source-matched runtime;
their existing combined node limit is 300 seconds / 4 GiB, with 10-second native
children. No native claim follows from the structural or host gate.
