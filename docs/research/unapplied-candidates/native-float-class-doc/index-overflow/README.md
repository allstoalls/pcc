# Float index overflow correction (unapplied recovery source)

This two-path patch is based on production commit
`db1830cf83174428c48607819ae98e84f887c9b9`. It is preserved for recovery;
no production application or native qualification is implied.

`changes.patch`: 4,511 bytes, SHA256
`2727fc0515d31bafd80d935c0534a34331fdaf1156c37a606e87a214be10a4f9`.
All paths use mode 100644. Apply only to these exact preimages:

- `pcc/runtime/py/py_protocol_runtime.py`: SHA256
  `65770540a5c983dd8ec3f9ecb62f9e45cb3a016b4428afa56d7135358dec80ec`
  to `760f5d2c6469223d58dcce60b8f3e927c6fe069e1c8e8bd11a62ed1757f794f4`
  (93,267 bytes).
- `tests/python/test_native_float_protocol.py`: SHA256
  `65b102016b23f0f7613426c68542e9d13b9e4170c85c1b5196bdee698d384d1f`
  to `bce4fbf2d9ed1709e3e9197004b8e1f558eccbbcbe1e5f6dd4c6060489409bf1`
  (10,102 bytes).

The original db183 CI run 38031496275 failed the unchanged float PROGRAM's
GC0 OverflowError assertion for an `__index__` result of `10**400`. The
underlying bigint-to-double helper returns infinity without setting an error.
This candidate detects positive or negative infinity only for the index
conversion while its result is still leased, and records owned OverflowError
(tag 15). The float-protocol conversion continues to permit infinity and NaN.
The underlying bigint helper and the original native PROGRAM are unchanged.
PROGRAM SHA256:
`fa615ddddedced81966d7479c6504c6c3b2691b005d11e6003961582ede99924`.

Independent source review is clear. A bounded, strict single-process host gate
executed these exact selectors once: `test_float_protocol_result_ownership`
(12 cases) and `test_float_index_overflow_preserves_owned_error` (24 cases),
both in `tests/python/test_native_float_protocol.py`: **36 PASS**. The source
inventory was sealed before and after, with zero denied audit events and clean
supervisor completion. These models supply raw finite/infinite/NaN doubles;
they verify the new guard and ownership/error preservation, including a model
cleanup error. They do not execute arbitrary-precision rounding or native GC.

The original real CI failure remains a failure. Native execution of this
correction, rebuilt matched runtime, GC0-4, pcc1, full async/gateway regression
and Stage1 completion are **UNRUN for this correction**. A future source-matched
CI run must establish those outcomes; the existing runtime archive cannot
qualify changed runtime source.
