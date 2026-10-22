# Knowledge: search historical evidence

**Current code and matching execution are first-hand evidence.** This directory
helps locate prior experiments; it is not a required reading bundle or a task
queue. Maintainer requirements live in [AGENTS.md](../../AGENTS.md) and
[compiler-contract.md](../compiler-contract.md).

Start from the actual entrypoint and implementation. Search a concrete symbol
or symptom, then read the matching experiment and its later corrections:

```bash
rg -n 'symbol_or_error' docs/investigations/INDEX.md docs/knowledge/
```

The generated pages are lexical indexes, not validated current conclusions:

- [denied-experiments.md](denied-experiments.md): recorded failures and denials.
- [confirmed-root-causes.md](confirmed-root-causes.md): recorded confirmation
  markers, including conclusions that later updates may supersede.
- [symptom-routing.md](symptom-routing.md): document titles, recorded statuses
  and search terms. An old `active` status does not select today's task.

Before applying a historical verdict, compare its source revision, command,
options, runtime/compiler identity and prerequisites with the current path.
Do not repeat an applicable failed experiment without new evidence; do not
reject a changed implementation merely because an older one failed.

[Current status](../status.md) is the single maintained progress page. Historical
handoffs are preserved outside the working repository with checksums and restore
instructions; see its evidence and recovery section. Dated assessments remain
snapshots: their source, blockers and commands must be rechecked against current
code and the user's direction. The
[optimizer pipeline audit](2026-09-08-optimizer-pipeline-audit.md) records one
example of failing to turn a documented limitation into a source/effect check.

After an investigation edit, regenerate both indexes using the commands in
[the investigation workflow](../investigation-workflow.md). Generated pages
must stay reproducible; do not patch them by hand.
