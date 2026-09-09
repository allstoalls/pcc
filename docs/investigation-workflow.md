# Investigation workflow

Use current source, effective configuration and execution as first-hand
evidence. An investigation records how a claim was tested; it does not become
current truth because it says `[CONFIRMED]` or appears in an index.

## Find the relevant record

1. Locate the actual implementation and reproduce the concrete symptom.
2. Search [INDEX.md](investigations/INDEX.md) or `docs/knowledge/` by symbol or
   error. Read the matching experiment, denial and later corrections; expand
   the surrounding history only when needed. No full-corpus reading requirement.
3. Check the recorded revision, mode, command and artifacts against current
   code. Reuse a denial when its prerequisites still apply. When they changed,
   identify the difference and run the smallest discriminating test.

Continue an existing matching investigation; use a new specific file only for
an independent failure mechanism, linking its predecessor. GitHub issues track
work. Do not recreate the retired task board or a per-slice evidence tree.

## Record an experiment

Keep these sections concise; existing longer records need not be rewritten:

```markdown
# Investigation: specific symptom

## Status
active | resolved | superseded by <link> — date and remaining boundary

## Problem Description
Concrete observed behavior and expected behavior.

## Repro
Source revision/worktree identity, effective options, exact command,
compiler/runtime/input hashes and durable output locations.

## Test [CONFIRMED|N/A]
Observed failure and smallest regression. CONFIRMED means actually reproduced.

## Proposals
One mechanism, discriminating check and pending/confirmed/denied verdict.

## Update: <date and mechanism>
Code change, executed check, observed result and claim limits.

## Report
When closing: what was fixed, gates completed, remaining scope and links.
```

Preserve historical observations and raw measurements. Append dated corrections
or supersession links instead of silently rewriting a former conclusion. An old
resolved record may receive a clearly labeled correction; update its status
when reopening. A decision belongs beside the experiment, not only in chat.

Keep one hypothesis per diagnostic change; run focused checks before stacking
shared-code edits. Distinguish untested proposals, reproduced failures and
qualified fixes. Do not claim a test passed from partial output. Follow
[validation-workflow.md](validation-workflow.md) for expensive/native gates.
Documentation does not authorize commit/push or reverting shared work.

After editing an investigation, regenerate in order:

```bash
env -u LC_ALL uv run python scripts/regen_investigations_index.py
env -u LC_ALL uv run python scripts/distill_investigations.py
```

`tests/test_knowledge_pages_are_current.py` checks the generated pages. These
pages extract text mechanically; they do not reconcile old contradictory
verdicts. Keep the current source/experiment check in the reasoning loop.
