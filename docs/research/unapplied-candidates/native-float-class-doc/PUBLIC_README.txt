PCC native float protocol and class documentation candidate

Status: UNRUN; SOURCE_REVIEW_BLOCKED. Independent source review found the two
issues below. This is saved proposed source, not an applied, installed, tested,
or production-qualified fix. Do not apply this version.

Confirmed source-review blocker: the canonical first-class float path in
py_obj_ops_dispatch.py extracts an element with py_tuple_get, invokes the new
callback-capable converter, then decrefs the original raw element. The enclosing
tuple lease does not lease that element; a moving collection during __float__
can invalidate it. That caller needs an independent operand owner/address lease.
Direct ordinary float conversion and the named dispatcher do not close this gap.

Second confirmed blocker: prepared/metaclass class documentation is replayed
after __new__ returns. Reusing the cached source literal can overwrite a hook's
replacement or deletion of namespace['__doc__'], or changes made to the returned
class. Documentation must retain the metaclass-returned live namespace rather
than replaying its already-executed source store.

Base production commit: bf1bf82d8242a60a85a06322ceee97fab5346cc0
Base pcc tree: 930263bf204e578796c72dab5d4aa31fd7ab27ae
The same pcc tree was reported at master 54efbf6418db94b3a8ae70eb38ca1d677ec2da25.
Whole-checkout equality to that master revision is not claimed. The per-file
baseline hashes, including the test preimage, are authoritative patch inputs.

Files to retain:
- changes.patch: complete eight-path proposed change
- BASELINE_SHA256.tsv: six existing file preimages and two required absent paths
- CANDIDATE_SHA256.tsv: all eight resulting file hashes
- PUBLIC_README.txt: scope and incomplete validation status

Scope:
- Native float(obj) calls type-level __float__, then __index__ only on absence.
- Callback return validation and errors use the existing owning-root/lease
  dispatcher. Ordinary direct float calls protect the receiver across callbacks.
- Classes publish their leading string as __doc__, with an independent None
  default when absent. Prepared and dynamic namespaces are covered in tests.
- Five production files, two new regression files, and one constructor model
  test update. Existing class-init outlining is retained. Python syntax is unchanged.

Validation required before applying or qualifying:
1. Verify preimages, apply only in a separate checkout, and check the diff.
2. Run focused source-body and CPython checks under an outer timeout:
   env -u LC_ALL uv run pytest -x -n0 -vv --tb=short tests/python/test_native_float_protocol.py tests/python/test_native_class_docstrings.py tests/python/test_class_constructor_roots.py -k 'not native_five_gc and not reaches_owned_emitter and not reach_owned_emitter'
3. Supply a source-matched owned runtime archive and run the new pcc0 native
   tests, then pcc1 tests with an explicitly matched native compiler. Their shared
   verifier executes the exact programs on GC0..4 and saves identity/output receipts.
4. Recheck existing float-string/bignum, named-protocol, constructor, class, and
   metaclass regressions. Native/bootstrap qualification has not been performed.

Not claimed: complete float-subclass conformance, owned DeprecationWarning for
non-exact conversion results, bytes-like/plain-str-subclass parsing, or C/Python
runtime differential equality. There are no C mirror sources for these owners
in this baseline. Returning bool from __index__ retains the existing explicit
owned-warning capability boundary.

References:
https://docs.python.org/3/library/functions.html#float
https://docs.python.org/3/reference/datamodel.html#type.__doc__
https://docs.python.org/3/reference/datamodel.html#special-method-lookup
