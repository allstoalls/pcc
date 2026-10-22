PCC native float and class documentation successor

Status: UNRUN; SOURCE_REVIEW_ACCEPTED, pending execution. Independent source
review found no remaining blocking defect in the four successor source files.
No tests, compiler builds, native executions, bootstrap runs, or performance
measurements have been performed for this successor. It is not qualified for
production application. The earlier eight-path candidate remains blocked alone.

Dependencies and application boundary:
- Earlier native-float/class-doc patch SHA256:
  808e9f4314f7c35cee27b4b42e591299f9e53aa3602ff151687480c2efd0a748
- Coordinated OSError change, whose py_obj_ops_dispatch.py postimage is:
  893e1bf8f9566d6d4e13dd96895d1003ae66911438b244f76825c2010d880929
- This is a four-path DELTA over those dependencies, not a standalone patch
  against master. Verify all listed preimages before any application. Assemble
  both dependencies and this successor in a separate qualification tree before
  executing tests. Do not apply the earlier float/doc patch on its own.
- The only overlap with the OSError change is py_obj_ops_dispatch.py. Its exact
  OSError postimage is the required preimage; this delta preserves the tag 14/34
  py_os_error_new constructor routing. Do not replace it with an older full file.
- The original baseline pcc tree is 930263bf204e578796c72dab5d4aa31fd7ab27ae.
  No whole-checkout equality to current master is claimed. Per-file hashes are
  the authoritative inputs.

Four-path scope:
1. py_obj_ops_dispatch.py reuses the existing rooted descriptor-constructor frame
   for canonical first-class float calls. The operand is independently rooted
   and leased across user callbacks and invalid-return cleanup; errors are
   checked before boxing, with the existing protected cleanup/result handoff.
2. class_gen.py avoids replaying already-executed __doc__ attribute events after
   metaclass construction and reads the live class documentation namespace.
   Method events named __doc__, valueclass/runtime exclusions, and class-init
   outlining remain intact.
3. test_native_class_docstrings.py covers metaclass documentation replacement,
   deletion, live mutation, and a callable method named __doc__.
4. test_native_float_callback_movement.py adds an actual native GC4 witness for
   canonical float callbacks and invalid-return destructor reentry. Each phase
   requires positive production relocation-forward progress; it also checks
   nested conversions, the original TypeError, and exactly-once finalization.

Validation still required:
- Run the earlier focused host/reference/model checks against the assembled
  source, including the extended documentation regression.
- Build an explicitly source-matched owned runtime and execute the existing
  pcc0 five-GC float/doc regressions, then the GC4 movement witness. Qualify pcc1
  separately with an explicitly matched compiler; no auto-provisioning.
- The movement test uses existing production collector APIs and writes source,
  runtime, binary, stdout/stderr, and status receipts. Its one select/step call
  per phase has not yet been shown to produce movement. Zero forwards means
  the witness was not established and must be diagnosed; it cannot be counted
  as a pass or replaced by a host model.
- Recheck existing float-string/bignum, named-protocol, constructor, class, and
  metaclass coverage. No full runtime or bootstrap qualification is claimed.

Python syntax is unchanged. The earlier candidate's explicit scope limits for
float subclasses, bytes-like parsing, and owned warning behavior remain.

Minimal preservation set:
- changes.patch: complete successor delta
- BASELINE_SHA256.tsv: three required preimages and one required absent path
- CANDIDATE_SHA256.tsv: all four resulting source hashes
- PUBLIC_README.txt: dependencies, scope, review, and UNRUN status
