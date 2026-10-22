# Owned open errno candidate, version 2

Status: UNRUN source candidate. No Python import, test, runtime build or native
execution has been performed for this patch. It is not a qualified fix.

## Supersedes version 1

Version 1's source patch SHA-256 was
`9e1104c65bc53104d7ee39b8acb3a798b50a3da53fba510a30031cafb9b859ef`.
It incorrectly treated Darwin `seek_file` failures as negative errno values.
That intrinsic returns raw libSystem `lseek` -1 with errno set, so negating the
result could overwrite the real error with EPERM and raise PermissionError.

Version 2 captures Darwin errno before closing the descriptor and retains
negative-result decoding for Linux. Its regression separately models Linux
-ESPIPE and Darwin -1/ESPIPE, then deliberately overwrites errno during close.
Static re-review found the specific defect resolved and no further finding in
the corrected path. The constructor candidate is separate and unchanged.
No regression test or native execution has run; review is source-only.

## Scope

- Owned `fopen` captures platform failures into the existing errno owner,
  including paths which close an acquired descriptor. It preserves Darwin's
  raw `lseek` -1/errno result and Linux's negative-errno result separately.
- Native path-based `open` snapshots errno before formatting or allocation,
  uses the existing platform-aware exception subclass mapping, and stores
  `errno`, `strerror`, the original str/bytes `filename`, and two-item `args`.
- The existing file-error helper keeps independently owned filename and
  exception slots, counted leases and pending-error-preserving cleanup.
- Descriptor and flush callers use the same helper without a filename.
- Exception layouts, class tags, MRO and handlers are unchanged.

The source snapshot was supplied as production `818187ba`; later reported
`ef550d29` changes are documentation-only. Byte identities, rather than an
unverified checkout revision, are authoritative: `baseline.sha256` identifies
the three modified files, and `postimage.sha256` identifies all four outputs.
`tests/python/test_native_open_errno.py` is a new file, absent from the baseline.

## Static verification

- The patch applied cleanly to a separate copy of the recorded baseline.
- Every applied output matched `postimage.sha256`.
- Reverse apply check passed.
- Whitespace/error checks passed for each modified or new file.
- No existing active source or runtime artifact was changed by preparation.

## Required qualification, in order

The single validation coordinator should use the already verified, pinned
interpreter for host checks and select an isolated, source-matched runtime and
compiler before emitted tests. Do not provision environments or overlapping
builds. Public commands do not unnecessarily unset locale variables. The reused
execution helper removes `LC_ALL` from subprocess environments; record that
effective environment. Use the repository watchdog/RSS/lock procedure, explicit
node selection and a durable log.

Host command shape, with the verified absolute interpreter path substituted:

```text
<verified-python> -m pytest -o addopts= -x -n0 -vv --tb=short tests/python/test_native_open_errno.py -m "not integration"
```

Select native nodes explicitly with `-o addopts=` so repository defaults cannot
silently skip them. Set `PCC_WITH_THREADS=1` only when the selected archive's
verified provenance says `threads: true`; otherwise match its recorded setting.
The native compiler and runtime must both be explicitly selected and their
identities recorded before qualification.

1. Host body regressions in `test_native_open_errno.py`, excluding integration:
   missing paths and representative mapped errnos with both str and bytes;
   metadata allocation failure; Linux/Darwin seek failure preserving errno across close;
   stream allocation failure reporting ENOMEM.
2. Existing `test_owned_fdopen_provider.py`, including invalid descriptor,
   metadata failure and ownership cleanup checks.
3. Compile the changed runtime modules to owned IR, inspect ABI/call ownership,
   and verify absence of libpython fallback.
4. `test_open_errno_native_five_gc`, first with the host compiler and then an
   explicit current native pcc1. Each must emit and execute a real binary.
   It checks exact missing-file handler selection, sibling exclusion, errno,
   strerror, filename type/value, args, formatting, post-collection retention,
   and exclusive-create EEXIST without overwriting the existing file.
5. Relevant file/exception native integration and final bootstrap/fallback
   gates after source stability. Existing baseline receipts are not new proof.

## Known limits

- Constructor argument loss in `OSError(...)` is a separate candidate.
- This is not a repair of every filesystem or OSError-producing API.
- No Windows execution, directory-open semantics, PathLike expansion, or
  locale-wide message parity is claimed.
- The host raw-memory model cannot qualify moving/concurrent collectors;
  emitted and native-compiler gates remain mandatory and unrun.
- No performance, fixed-point or release-readiness claim is made.
