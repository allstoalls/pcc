# Verification status

Updated 2026-10-08. These results describe specific source revisions and
workloads; they are not a release-wide support matrix.

## Cold quick start

The README's three hello commands passed on Linux x86_64 with CPython
3.15.0rc1, starting without a runtime archive or compiler cache. No `PCC_*`
overrides or `PYTHONPATH` were supplied.

- The normal compiler command built its runtime automatically: 190 modules,
  single-threaded, atomic reference counts.
- Compilation returned zero in 432.748 seconds, with a peak process-tree RSS
  of about 318 MiB. Compiler stderr contained optimization progress.
- `./hello` returned zero, printed exactly `Hello, PCC!` and a newline, and
  produced no stderr.
- The result is a statically linked x86-64 ELF executable, with no interpreter
  segment or dynamic section. Runtime dynamic loading was not inspected.

This was one cold compile and one execution, not a performance benchmark.
Clone/interpreter installation and a second warm-cache compile were not run.
The compiler ran on CPython; this does not qualify a native `pcc1` bootstrap.
Other platforms were not exercised by this quick-start check.

Source and artifact identities:

| Item | SHA-256 |
|---|---|
| Compiler input checksum | `f590af144fdc783889ef82d6eb9c7fbf0a53b3f5efbc9b9ccf13d372bc002f0c` |
| Runtime archive | `f6003d5d3c434ba116939422b5bfea498c775df54a05656028e045354e5ac43d` |
| Runtime manifest | `a39dc821e5764e826994443bcd207beb4f75ff11f2f236d9cf2fe8d20140d8d0` |
| Executable | `f76f9bb93b3d7ddc83a53ce4b40ef904d99d7fd006d9698f0a880f85068dde1a` |

The runtime manifest matched the compiler, source, target and configuration.
The source inputs remained unchanged during the run. This checksum identifies
the CI-repair candidate used for the cold check; it is separate from the
earlier revision's results below.

## Earlier Linux checks

At source revision
[`445d1d2f`](https://github.com/ikshengmin/pcc/tree/445d1d2f4c51ac87fe0e1b373aafd1516e98db2e),
the CPython-hosted compiler used the self backend with libpython disabled and
a matching threaded/atomic runtime:

- Five aggregate-ownership cases passed on GC0–4: 25 executions.
- The original async suite passed all 16 tests; the native clock-reader gate
  and 10 native C export/lifecycle tests also passed.
- Gateway checks passed 390 default tests and one structured-scope integration.
  Nineteen other integrations were not run. Host/default results and selected
  native integration execution do not establish a general native gateway pass.
- The function-bearing hello example compiled and ran using the prebuilt
  runtime. That older check did not test a cold startup.

That qualification also retained failures: the L1 host-field baseline,
native resource `spawn_failure` after four GC0 scenarios, and a fault-injection
negative control whose exception and collector-identity checks did not pass.
Later resource scenarios and GC1–4 were not run after the failure. Those
receipts are not replaced by the successful cold hello example.

The earlier runtime archive SHA-256 was
`efa41d7fe5e292bc1ce6d8389103f9a35881106c7f49e732828771e624b02507`;
its compiler checksum was
`74166df92b19d91cfde17f5f698170fb23d5aa95174d7d31c78da286cfff0e04`.

## CI

[Run 37795736435](https://github.com/ikshengmin/pcc/actions/runs/37795736435)
finished with five failed jobs. Both Mac jobs were refused by memory admission
before compilation. Linux x86_64, Linux aarch64 and Windows x86_64 built their
runtimes, then hit the 2,400-second shared GC0 Stage1 deadline. Stage2/Stage3,
five-GC self-host chains and wheel qualification did not complete.

[Run 37804465163](https://github.com/ikshengmin/pcc/actions/runs/37804465163)
also finished with five failed jobs: one Mac CPython GMT-reference failure
and four Stage1 timeouts near 2,400 seconds. Both Mac jobs passed real memory
admission and budget propagation; this did not qualify native compiler builds
or wheels.

## Still open

- A current-source native compiler and Stage1 → Stage2 → Stage3 fixed point
- Broad Python/package compatibility and clean package-install qualification
- GC concurrency, relocation and long-running memory/performance acceptance,
  including earlier GC4 concurrency crashes
- The recorded 300-second HTTP/dashboard compile timeouts
- Native GUI/Metal, platform, package and application-specific acceptance gates

Historical bootstrap/fallback baselines keep their original source identities.
See the [developer reference](developer-reference.md) for component boundaries
and the [project intent](project-intent.md) for the requirements.
