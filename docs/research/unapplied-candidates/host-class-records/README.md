# Deferred host class-record transport: source recovery

This packet preserves a deferred, unexecuted candidate after a workspace reset.
It is intended for a source-recovery commit, not application to production.

## Immutable baseline

- Repository: https://github.com/ikshengmin/pcc
- Commit: `818187ba7dfff54df589b93bc10594f87786fe22`
- Tree: `603baabe2e9d6508b4af2d7e5c1f489d8f9b8562`
- A fresh read on 2026-10-09 confirmed that master still pointed to this commit.

## Recovered source identities

All four production files and the focused test file match their original frozen
SHA-256 values. The regenerated five-file patch and original manifest also
match their frozen hashes byte for byte.

- Patch, 57,450 bytes: `5441b924b523ca8cae5b6b935c891d10ef3e8e3c638ce967983bad075cc4f7b0`
- Manifest: `419f34ec30fbf37807393eed34a62155d79d8051450053172348e51cbeb12d5b`
- `pipeline.py`: `0ad400fd6e8ee3bcb409aa3d08a9d68ddf2b9514a07dc105e51b0cec059562f2`
- `pipeline_exports.py`: `470ce84dee5e24bc0d63c52eb535babfdb097976b6fa253359f5559465c4b25c`
- `pipeline_frontend_parallel.py`: `762a3da547b30163d43d6d3e16a96e9063f885a25156f606d15e93676f011fd2`
- `pipeline_frontend_worker_execution.py`: `f41cb4a454dbc020112e416bdc4cab9fa1155aabb5edac74a7c3c47ff4f557d5`
- `tests/python/test_host_class_record_exports.py`: `ceb652bb630745ccb4fcaaef89834505b3a6c2af67a787bc45bfbc64461a2656`

The production paths above are under `pcc/frontends/python/`. Exact baseline
preimages are retained separately; this packet is not an executable overlay.

## Evidence and limits

A separate external host-only metadata experiment previously passed all six
full-graph reconstruction and mutable-instance isolation checks. On its fixed
historical 456-module input, median decode time was 1.469 to 0.939 seconds and
the wire decreased from 24,287,037 to 13,794,420 bytes. These were warm-read,
metadata-only measurements, not Stage1 or CI speedups.

The integrated candidate and its tests remain UNRUN and unqualified. Source
review found no mandatory correction on the stable-context host route, but
environment/cwd checks are not atomic launch freezing, integrated writer cost
is unmeasured, and real-worker/checkpoint behavior is untested.

Recovery performed no compiler, test, benchmark, or production execution.
Source restoration does not restore missing input data, runtime archives, raw
logs, or process samples. Preserve this work as deferred: the main optimization
priority remains the substantially larger inference/codegen cost.
