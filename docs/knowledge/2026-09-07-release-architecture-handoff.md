# Release qualification, numeric fixes and architecture review — 2026-09-07

> Historical session snapshot, not current task instructions. HEAD, dirty state,
> blockers and artifact paths below describe the recorded session. Check current
> source, `git status` and execution before reusing them; later user instructions
> take precedence. Search relevant sections instead of loading this whole file.

## User-requested checkpoint and pause

The user explicitly requested a temporary commit of all pcc changes and a
push, then a pause. All three agents have stopped editing and have no owned
compiler/test jobs. Do not automatically resume after this checkpoint;
continue from GitHub Issues when the user asks.

Latest state at this checkpoint:
- #191: shared array numeric implementation is in place;57host/source cases
  plus5existing host tests pass. Current host-pcc/self/no-libpython/C-runtime
  helper checks preserve finite precision, compensated reduction and signed/
  unsigned64-bit wrap. The old-pcc1 full CLI-source compile timed out120s with
  no binary and no surviving children; it is not green. Full fresh-pcc1 CLI
  validation and wide/nonfinite float-to-int behavior remain open.
- #193: scoped NaN truth/equality/order/container/C-API/ABI metadata packet
  passed7tests26.09s under host-pcc/self/no-libpython/C runtime. Direct value
  equality is separate from identity-aware container/RichCompareBool policy.
  Exact mixed integer/float implementation has not started; pcc-Python-runtime
  and fresh-pcc1/bootstrap validation remain pending.
- #194: object-local/return integer projection has a partial source repair;
  minimal regression passed1test6.05s. Expanded packet stopped at1passed,
  1failed14.41s: mixed list/tuple staging still narrows. Follow
  `python-boxed-int-object-consumer-projection.md`; do not claim it fixed.
- #192: explicit target-component matching passed44focusedtests; fresh-pcc1
  qualification remains pending.

Checkpoint checks:97changedPythonfiles parse; no credential-pattern hits in
changed text; git diff --check clean. Bootstrap baseline metadata and generated
knowledge packet passed4tests with2deselected in4.85s. This is a WIP snapshot,
not completed fallback/default/integration/five-GC or release qualification.

Publishing preflight found no UV/Twine credential environment variables and
no ~/.pypirc. The existing GitHub release workflow uses PyPI trusted publishing;
it still builds sdists with Python3.13. No publication was attempted. The
user now explicitly authorized this checkpoint commit/push; that does not
turn unfinished code into a qualified0.1.8release.

Current work is tracked in GitHub Issues. The task board and goal_state
resume/finish-check retired on September6; do not restore them. Preserve all
shared edits and staged migration deletions. No resets or publication have
been performed by this work; the user-requested commit/push is the checkpoint
described above.

## User scope and current queue

- #186: verify current pcc/pcc1, default and integration tests, then publish
  python-cc0.1.8. Replace stable pcc1 only after qualification.
- #187: coherent Python3.15 default and native package paths without a Python
  version. Keep target metadata; future3.16 marker selection must invalidate
  lock-sync cache, without a manual path change.
- #191: native array CLI loses float64 precision through six-digit fixed point.
- #192: Windows-GNU falsely classified as Linux; focused source fix now passes.
- #193: current typed/boxed NaN and exact mixed integer/float comparison defects
  found while qualifying #191. Generic numeric fixes are in progress.
- #189 README diagram and #190 architecture/Zig analysis are complete/closed.
  C support is explicitly mandatory. No broad C/IR/directory rewrite is
  authorized by the analysis; native C capability parity already belongs to #171.

The user can still say “continue” or “complete all tasks”; use in-progress and
highest-priority dependency-ready GitHub issues. Markdown remains for design,
investigation and handoff knowledge, not the executable task queue.

## Stable installation and artifacts

`~/.local/bin/pcc1` still points to
`~/.local/share/pcc/toolchains/v84-baseline-c1f4342696e9/bin/pcc1`.
This historical baseline is installed and runnable. It is not the new release.

Main metadata remains0.1.7. The old `/private/tmp/pcc-correctness-20260906-a`
candidate alone was bumped to0.1.8 and is stale. A new empty worktree
`/private/tmp/pcc-correctness-20260906-b` was registered with --no-checkout;
it contains only its Git worktree metadata so far. Do not treat it as a frozen
candidate or run qualification there yet.

Logs live under `build/correctness-20260906-a/`. The earlier first-install-03
snapshot `~/.cache/pcc/installations/source-2gdr4ie9` built Stage1 in183.07s
(7workers), but Stage2 timed out at600s. Its older pcc1/runtime are used only
for explicitly labeled helper capability probes.

Later gateway-v5 receipts are complete but predate the new target/source:
Stage1 `build/gateway-stage1-20260906-v5`:383.25s with2workers,
pcc1SHA c5ae2affdb02f162d793df2e46acfae1f63296a73c028f41ee8cf9a259869ae7.
Stage2 `build/gateway-stage2-20260906-v5`:550.877s compile,559.856s including
publish barrier; pcc2SHA76c704e3a85d9a68d639d8076e46c1af068e6df33c2fb6f63ef0d3fcb2171b7f.
The new installer accepts those actual receipts read-only
(`gateway-v5-installer-readback.json`). They do not qualify current source.

## Completed local changes and evidence

- Installer: `scripts/install_pcc1_toolchain.py`, receipt validation plus
  source build, copied helper/runtime isolation, PYTHONSAFEPATH, stage-only,
  qualification, atomic promotion and recoverable rollback.80focused tests
  passed7.60s. Real sampler/pytest collect+execute/record-gate/qualify handshake
  is covered, as are child env overrides, filtered collections and a damaged
  active candidate. No real promotion ran. Read --help for the actual protocol.
- `scripts/pytest_live_report.py` now records live failures, selected collections,
  effective filters/cache/stepwise/config fields and actual pytest-side compiler/
  source/runtime binding.4subprocess tests passed2.66s. Use it for long gates.
- Package naming/build/scope repairs: shared literal TOML/name parser,
  declarative source policy with hook rejection, host ZIP/tar build preparation,
  proper top-level project import roots, bounded filesystem/archive metadata.
  Host build/selection/reinstall packet80passed,9native cases explicitly
  deferred; metadata26passed. Real host gateway19/GUI18 Python-file installs
  matched their sources in one private site. Updated native CLI install pending.
- `pcc/python_target.py` owns3.15 literals. Package defaults, emitted tuple/attrs,
  sys.version, version guards, platform and sysconfig agree. Registered native
  component/provider admission handles shallow and recursive closure.
  Host-pcc-to-native packet3passed8.57s: self/no-libpython/C-runtime canary and
  platform provider; LLVM-backed unpack oracle. Fresh pcc1 still pending.
- Native environment tag now omits Python version; e.g.
  `pcc_native_v1-arm64_apple_darwin-pcc_native`. CPython modes retain version
  identity. `_sync_key` includes target metadata so markers reselect.
 9exact host tests passed0.29s; no legacy site was modified/deleted.
- Target matcher #192 uses explicit components.44classifier/dispatch cases
  passed0.18s. Its investigation remains active until fresh pcc1 qualification.
- README now embeds responsive `docs/architecture/pcc-overview.svg`, verified
  at full width and820px. Typed-Python whitespace is gone and the complete
  diagram displays by default. Core/gateway/GUI README installation and package
  environment explanations were updated. No online publication claim.

## Active agent ownership at this checkpoint

`installation_conformance`: target repair is source-stable. Currently read-only
audit of C float and PyObject_RichCompare/RichCompareBool/container identity
contracts for #193; no compiler or runtime run.

`package_integration_review`: owns #191: `pcc/array_numeric.py`, scalar helpers
in `pcc/array_core.py`, native CLI consumers in `cli_bootstrap_array_core.py`
and `test_array_numeric_precision.py`. All fixed1e6/six-digit consumers are
removed;48source differential cases and5existing array tests pass. Next is
an old-receipt-bound local-helper native canary under the performance lock.
Old-artifact numeric failures must remain observations, not claims about new
source. Nonfinite and exact mixed comparison must consume #193's generic fixes,
not permanent array-only workarounds.

`patterns_and_claims`: owns #193 frontend truth/comparison lowering plus runtime
compare C/pcc-Python mirrors, necessary public-header/runtime ABI registrations,
and numeric regressions. Separate direct-value equality from identity-or-equality
used by containers/C-API; globally removing the pointer shortcut breaks NaN
membership semantics. Explicit unordered comparison must not become a positive
sort result. Preserve C's usual arithmetic conversions. Source edits/compiles
must coordinate with the shared performance lock and B's canaries.

Root does not start a compiler/bootstrap run while those agents hold the lock,
and must wait for both numeric slices to be source-stable before final freeze.

## Architecture and numeric evidence

Full analysis: `docs/architecture/c-python-ownership-review-2026-09-07.md`.
C/Python share LLVM-shaped IR, optimizer infrastructure and self emitters;
separate semantic frontends do not imply impossible LTO. Real gaps include
pcc1 C delegation, external compiler use in production extension builds and
ambiguous ownership reporting. unsafe is an explicit freestanding intrinsic
contract; do not delete C or its oracles. Counts of confirmations, tokens and
catch clauses are not counts of unique bugs/options.

Zig0.16 official release notes were verified: zig cc/c++ still use Clang21.1.8;
C translation moved to Aro, and cImport is moving toward the build system.
Borrow unified driver/ABI/target/build/cache contracts, not an unacknowledged
permanent Clang owner. https://ziglang.org/download/0.16.0/release-notes.html

`architecture-concrete-repros.json` records host execution of the native-shim
source losing1e-7 and1/3precision, plus Windows-GNU target misclassification.
`array-numeric-current-01/observation.json` separately proves current
host-pcc->self/no-libpython/C-runtime errors in typed NaN truth/!=, boxed NaN
self equality, and mixed large-int/float equality/order. Current C repr(nan)
is correct. The pcc-Python formatter's NaN guard depends on the faulty !=
lowering, so C-formatting success does not exonerate its older runtime result.

## Remaining validation

No complete default/integration/five-GC/Stage3 qualification exists for current
source. No0.1.8 publication or stable replacement. Old focused-03 hit180s with
no final summary; it is not green. Split expensive fallback closure phases
before retry. Source manifests, native canaries and complete reports must bind
the final candidate, not the old a/v84/gateway-v5 snapshots.

Read the installation protocol and tests/host_pcc_pcc1_parity.py before staging
fresh pcc1 tests. The fixture requires its own
pcc.self-host-pcc1-receipt.v1, current checkout/environment source key and
emitter identity; a Stage1 build-receipt alone does not satisfy it.

After investigations change, regenerate both INDEX and distilled knowledge,
then verify freshness. All commands need gtimeout; all pytest commands need-x.
Do not infer ongoing compilation from model time or leave owned children after
a timeout. Do not inspect other sessions' CWD/history or modify/delete their state.
