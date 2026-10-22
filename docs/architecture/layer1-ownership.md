# layer1.py ownership split

Goal item No.17 requires the previous monolithic `layer1.py` ownership to be
split so GC, threading, coroutine, and native-module lowering no longer live in
one unreviewable file.

## Rule

`pcc/frontends/python/codegen/layer1.py` is a compatibility façade only. New logic
must live in smaller `pcc/frontends/python/codegen/*.py` modules.

## Ownership areas

- GC/root/frame lowering: codegen files that mention roots, frames, or GC.
- Threading/lock lowering: files that mention threads, locks, conditions.
- Coroutine/generator lowering: files that mention generators, coroutine, async.
- Native module/import lowering: files that mention native modules or imports.

## Gate

```bash
python scripts/check_layer1_ownership.py
```
