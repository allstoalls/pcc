"""pcc_py codegen package.

Three-tier codegen for the Python frontend, matching Section 7
("Layer Discipline") of docs/plans/python-frontend-interfaces.md:

- ``layer1``: typed fast path, native LLVM IR only.
- ``layer2``: typed with occasional PyObject escapes (future).
- ``layer3``: fully dynamic via the runtime lib (future).
"""
