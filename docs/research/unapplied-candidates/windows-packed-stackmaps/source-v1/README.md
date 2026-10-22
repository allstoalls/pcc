# Windows packed stack maps: source candidate

Status: unapplied source candidate. Independent source review is
**CLEAR WITH LIMITS**; no source blocker remains in the reviewed patch.
Every import, compilation, test, native execution, benchmark and Stage1 run
for this candidate is **UNRUN**. No performance improvement is claimed.

## Baseline and recovery

- Repository: `ikshengmin/pcc`
- Baseline commit: `db1830cf83174428c48607819ae98e84f887c9b9`
- Baseline scope: the verified compiler/runtime/build compilation closure,
  not a complete repository snapshot.
- `candidate.patch` contains six production changes and one new test file.
  `source/` contains only those seven postimages, not a standalone compiler.
- Apply the unified diff to an independent copy of the exact baseline.
  Check each preimage and postimage against `MANIFEST.json`; do not substitute
  files from another source revision. Applying this patch does not run tests.

## Change and checks still required

Windows object emission passes existing structured stack-map plans through
the owned assembler after ordinary SEH processing. Final code offsets,
COFF relocation conversion, retained unwind labels and packed schema checks
remain authoritative. Ordinary text assembly and ASM output keep their
existing route. The shared assembler and metadata schema are not rewritten;
resource limits and scheduling are unchanged.

Malformed structured plans can fail at a different validation stage from
the text route, so exact first-error precedence is not claimed. Final packed
semantic and aggregate resource-limit validation still precedes COFF output.

The new test file declares 17 parametrized host object/dispatch cases. They
have not been collected or run. Intended coverage includes full COFF-byte
equality, SEH sections and labels, stack-map and external relocations, high
integer IDs, malformed plans, plan consumption, timing boundaries, and
indexed-file ASM/PCO selection. Host object checks alone cannot qualify native
compiler behavior, Windows execution, five-GC semantics or Stage1 throughput.
The worker case includes the actual Windows frontend-worker path and a
text-control object oracle. The weak-reference case concerns CPython-host
plan reclamation only; both remain unexecuted.

## Combining with the stage bridge

Both candidates modify these files and require hunk-level composition:

- `pcc/frontends/python/pipeline_frontend_worker_execution.py`
- `pcc/backend/self_backend_indexed_emit.py`

Keep the bridge's sidecar exclusion, handoff validation and phase timing.
Add Windows to the existing packed-x86 selection and pass its plans through
COFF object emission. Do not replace a bridge postimage with this candidate's
whole db183-based file. Review and verify the final combined source identity
before any qualification or publication.
