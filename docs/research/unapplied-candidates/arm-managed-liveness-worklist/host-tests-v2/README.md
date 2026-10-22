# ARM worklist host fixture: retained unreachable blocks

Test-only successor to `host-tests-v1`. The production worklist postimage
`f1882fa4c29ad15c8843d30a9bf4d4bebf5b52307d0dc4daa155157b8d8289fe`
is unchanged. This successor is unapplied and execution is UNRUN.

## Preserved first result and correction

The first host gate collected 51 cases and stopped with 27 PASS, 1 FAIL and
23 UNRUN. The failing zero-tracked malformed case asserted that the kernel
still contained `unreachable_bad`; its actual block list was `entry, exit`.
It failed before the malformed scalar edits or liveness helper ran. The
failed payload SHA256 is
`5a24d9dacb2357fb53c33c9e7a24646e41b8e004a436aa295efab175d7b9330a`;
the final qualification SHA256 is
`1276dbddd29065f23ddee07b19cec71f02593ce70824ed53b10dc13be8199d0b`.
The original result is preserved in commit
`2e17768d069bb9a8a48cc0e3384636054c5e7c0a`.

In the exact production parser, `_parse_blocks` calls
`_finish_indexed_block_plane`, which calls `_filter_reachable_blocks_indexed`
before publishing the complete kernel. Therefore both normal direct capture
and textual parsing can remove unreachable blocks. The earlier supplement's
claim that textual parsing alone retained those blocks was incorrect. Its
cold-tail cases also did not establish unreachable-block coverage, although
their other assertions passed. This successor preserves that historical
result and makes the intended coverage explicit.

The new test-only construction starts with ordinary valid text in which the
entry's conditional branch reaches both the real path and a cold tail. The
parser must produce the complete block domain, and a read-only CFG traversal
must initially reach every block. On the frozen kernel, the test redirects
only the entry's second successor to its first successor. The conditional
terminator kind, condition operand, use facts and value IDs stay coherent.
A second traversal must equal the exact block domain minus the named tail.
No parser or solver method is replaced to retain an otherwise removed block.

- Each chain's cold tail is a simple chain ending in `ret ptr null`. Both the
  fresh independent set oracle and fresh native-helper kernel use the same
  target-only detachment. Exact block names and B=1+length+cold are asserted.
- The zero-tracked case now explicitly retains all 11 intended blocks.
- The malformed case first proves the bad block is retained and unreachable,
  then injects the original competing successor IDs 77, 99 and 88. The exact
  BackendUnavailable message naming 99 and all owner-cleanup checks remain.
- Duplicated entry targets are legal and union idempotently. The tracked
  domain excludes the added branch condition. The existing visit equations,
  independent live-state oracle, edge-index count, exception identity and
  owner-lifetime assertions remain unchanged.

There are still 33 new test cases plus 18 unchanged PHI cases. The other
self-loop, injected-error, empty-domain and inline-trigger coverage is kept.
No production code, test assertion or gate requirement is removed.

## Recovery and proposed gate

The public set contains only `tests.patch`, `README.md` and `manifest.json`.
`tests.patch` is a delta from the original test SHA256
`53d25748a4963fe3c24b5c399a2adce90d6c48d8afad8110c7fadce11effdb09`
to `c461144abc54e4633feff94d818a46e7576839ce426dc69a01e3df7ca2c0c3b5`.
It is not a replacement for the original complete test patch. To reproduce
an isolated candidate, apply the separately preserved production patch,
then `host-tests-v1/tests.patch`, then this delta, checking each preimage and
postimage. Do not apply this unapplied research artifact to active production
as part of recovery. The draft postimage and complete source copy are not
bundled here.

After separate admission, run the same bounded selection:

`python -m pytest -x -n0 -vv --tb=short tests/python/test_native_liveness_worklist.py tests/python/test_managed_phi_liveness.py`

Require actual collection of 51 and all 51 PASS, with no exclusions,
skips/xfails or audit denials. Keep 300 seconds, hard AS4GiB/NPROC0, the shared
exclusive lock and 4GiB free reserve. Retain no-auto-pcc1/no-native-provisioning
and both live supervisor reservation markers. Verify complete source
inventories before and after. These are host list-storage/helper tests;
no ctypes/process/runtime/native capability is needed.

Real-module mechanism, ARM object equivalence, native raw-memory/lowering,
whole-pipeline timing and full Stage1 remain outside this host gate. Synthetic
row reductions still do not establish real workload or end-to-end savings.
