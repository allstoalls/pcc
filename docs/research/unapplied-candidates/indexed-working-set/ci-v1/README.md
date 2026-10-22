# Indexed-stage bridge CI gate (source only)

This five-path CI/test increment applies to the db183 production files together
with the separately preserved indexed working-set bridge V1 plus V2 correction.
It does not change the bridge implementation, resource caps or admission rules.
Exact base/postimage hashes are in manifest.json. All new execution is UNRUN.

The existing Mac runner gains one fixed profile, indexed-split:
1. The two unchanged real-child failure/retirement and exclusive-retry tests.
2. Two genuinely importing local modules compiled through the ordinary full
   parallel frontend owner, unsplit then split, with jobs=1 and Stage1's passes
   off. Complete ordered PCO bytes, source/AST/export identities, fresh child
   PIDs, reaping and receipt/input retirement must match their contracts. A
   scoped call-boundary observer delegates to the original pool unchanged.
3. The actual new handoff provider through public no-libpython full-import
   compilation, all 31 own function bodies checked against fallback stubs,
   whole-output owned IR verification, and actual indexed AArch64 emission.

These are host pcc0 object/lifetime and native-lowering checks, not execution
of the emitted helper, runtime/GC validation, pcc1 qualification, or a speed
claim. The separate scheduler helper's broad compiler dependency closure is
not exhaustively lowered by this small provider check. Existing five-GC jobs
remain independent and unchanged.

The independent Mac job retains the existing 1200-second process/25-minute job
limits, automatic 2–4 GiB tree cap and 512 MiB reserve. It builds no runtime.
Only a successful job allows the Mac Stage1 build step to opt into the split
route. The flag is step-scoped: later wheel/parity steps and other platforms
do not inherit it. Stage1 still has its original 2400-second/45-minute limits.
The source default remains off.

Two complete compact JSON receipts join the existing bounded evidence archive;
large IR, objects and runtimes remain on the CI runner. The runner's default
and float-doc orders, fail-closed execution report checks and artifact cap are
unchanged. 39 static runner-unit cases are the only new local host selection;
the real child/compiler nodes belong to CI and must not be counted as executed
by that mock gate.

Independent source review cleared the initial runner/workflow scope, followed
by a narrow step-scope correction. Final review of both real test contracts is
pending. No compiler, tests, runtime build or native execution was run by the
author. Original bridge host evidence remains 75 main nodes PASS by retained
readback with its unmodified accounting-failed outer receipt.
