# Unapplied cold slot-status reporter candidate

This source/test patch preserves unfinished work after a scratch workspace
replacement. It is an isolated recovery artifact, **not an application to
production code or a qualified performance fix**.

Base commit: `818187ba7dfff54df589b93bc10594f87786fe22`

Base tree: `603baabe2e9d6508b4af2d7e5c1f489d8f9b8562`

Patch SHA-256: `4dd5955455944d3b905fd7f5998ac874fd0b815c22d45af13092f9c582259136`

The recovered 40,469-byte patch exactly matches the independently recorded
pre-reset patch hash. All three production preimages were recovered from the
verified base Git tree; all five candidate files therefore have exact byte
recovery, including the final host-test addition.

## Scope

The patch outlines repeated negative slot-status reporting into one internal
`noinline` IR helper per module. It retains the successful path, original
exception-call sequence and per-site cleanup targets. Two one-line method
contract/export entries accompany the lowering change. Two new test files
cover host IR/trace/stackmap behavior and later real-runtime native behavior.

Postimage SHA-256 values:

- `pcc/frontends/python/codegen/call_object_lowering.py`:
  `162c0a78aae28546fd4326f870e68295bb7b4c8555fa27ce648e5e17fbcd1bde`
- `pcc/frontends/python/codegen/host_contract.py`:
  `23c09a811bf9e82a5804d6296a8662b3250f2464268f65ea2445b394b2d18dae`
- `pcc/frontends/python/codegen/_l1_codegen_static_methods.py`:
  `49d41a9aef23e7c3df10d10d97ef1d2e3d8267fe6a6def75f230a0520942c451`
- `tests/python/test_slot_call_status_reporting.py`:
  `cd8bec179c52c61e6f4e08505298f12b34c3bd79971e8a2bb0cc38ecbf686d5b`
- `tests/python/test_slot_call_status_reporting_native.py`:
  `8f08bc1f255628c1f5b14cdf0edf9bc2b4dab6db669dfadb14c8cf79a4a94ead`

## Unfinished qualification

Host tests, native tests, matching-runtime build, native-pcc1 execution and
heavy-worker measurements are all **UNRUN** for this candidate. No realized
instruction-count reduction or speedup is claimed.

Independent source review was interrupted by input loss. Complete the
safepoint-metadata consumer and ordinary-call dispatch checks before admitting
execution. The native test's IR substring checks can match declarations;
actual generated call-edge evidence is still required at execution. Resume
focused checks with frozen inputs and the original 300-second / 4-GiB guard;
native tests require a source-matched runtime and bound each child at 10 seconds.

The separate label-suffix candidate and its unavailable test payloads are not
part of this recovered patch. Earlier qualification outputs were not recovered
by this artifact and must not be treated as newly reverified evidence.
