# Cohort V4: current workflow assertion correction

This is a test-only successor to the preserved cohort V3 candidate in commit
92c787b62061611d5df755e8622c805e4444b6cb. All execution remains UNRUN.
The three V3 production/test postimages are unchanged; this adds one test-file
postimage. Source review found the conflict before any cohort test execution.

The current remote diagnostic test (Git blob ea8593f6d5d9ed7f2cc31cf4cd76e061c2d0f2d3)
still required a fixed MAX RSS environment variable. The actual current workflow
SHA-256 5e28d557eb5b7030158d709d20901970c59797769049da025a7e7c61a63a01af
uses an automatic 4 GiB ceiling, 2 GiB admission floor and 512 MiB host reserve.
The exact remote test matches the retained test bytes; this is not a stale-local
copy assumption. The patch requires all three actual budget protections and
absence of the obsolete fixed MAX override. Timing retention and 2400-second
assertions remain. No workflow, budget, safety rule, scheduler or test is removed.

Recovery: verify the V3 manifest SHA-256
17bebcdcc3e376c02acebd9d97c5b0931633be2aabd93928752e15a4485043e8,
apply its three patches from ../source-v3 in the declared order, then apply this
test-only.patch after checking the bound current-test preimage in manifest.json.
Check all four final postimages. These three public files accompany the existing
V3 recovery chain; they do not replace or overwrite it.

The proposed bounded host selection is unchanged: the two worker cohort/admission
diagnostic test files, 56 selected cases and one real-child node excluded and
explicitly UNRUN. Use the already reviewed 300-second/4-GiB/NPROC=0 serialized
guard with 4 GiB free reserve. Runtime/native pcc1, actual child lifecycle,
Stage1 and performance are still unqualified. Follow the focused gate with
ordinary exact-source CI if admitted, without raising caps or adding offline
corpus experiments.
