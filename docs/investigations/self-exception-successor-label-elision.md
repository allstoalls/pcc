# Investigation: target optimization deletes exception stack-map labels

## Status
active

## Problem Description
An empty exceptional-successor block is threaded away and then its label is
deleted as unreferenced. Precise stack-map records still name that block, so
target-final verification rejects the program. This blocks target-on emission
of the sized-bytes runtime repair; target-off emission succeeds. Metadata
references are real references even when no machine branch still uses them.

## Repro
/tmp/pcc_exception_label_repro.py calls py_err_occurred, branches on zero, and
uses an exceptional block that only jumps to a return block. Optimized ASM
emission raises `target-final exception successor label missing for
'check'/'exception'`. The native runtime module reproduces the same shape.

## Test [CONFIRMED]
The minimal reproducer fails with host emission, before editing the backend.
Require successful text and indexed emission, an exceptional-offset record,
and execution of both no-error/error paths after target passes.

## Proposals
- No.1 retain block labels referenced by exceptional stack-map metadata [pending]

## No.1 retain block labels referenced by exceptional stack-map metadata
### Code Change
Collect exceptional successor labels from existing packed or legacy plans and
pass them as additional references to empty-label cleanup. Do not disable
trampoline optimization or weaken the final stack-map verifier. Labels add no
machine instructions. Other unreferenced empty labels remain removable.

### pending
Minimized and sensitive stack-map gates, then native runtime target-on emission.
