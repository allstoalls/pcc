# Compiler cache retention

The Python frontend IR cache and self-backend object cache share one bounded
lifecycle. Repository bootstraps normally use
`build/bootstrap-pytest-object-cache`; installed compilers default to
`~/.cache/pcc/self-backend-object-cache`. The cache remains content-addressed;
retention metadata never participates in compiler output keys.

Default policy:

- prune when completed entries exceed 10 GiB;
- continue toward an 8 GiB low-water mark;
- expire entries unused for more than 30 days;
- scan at most 512 entries per automatic maintenance pass;
- run automatic maintenance at most once every five minutes per cache root.

Successful cache use writes an explicit `.pcc-last-used` marker. Filesystem
atime is not used. Readers hold adjacent leases owned by the calling compiler
process, active frontend publishers are protected by their PID-bearing lock
directory, and temporary publications are not cache entries. Pruning first
publishes an adjacent `.pcc-evict` marker and rechecks leases/locks; readers and
publishers use the inverse check-create-recheck handshake. A victim is then
renamed into a transaction directory under
`.pcc-cache-retention/quarantine` before deletion. A later pass removes both an
interrupted transaction and its eviction marker. Maintenance failure never
fails compilation—it leaves the cache intact or causes a normal cache miss and
at most one bounded diagnostic.

Automatic inventory walks are persisted by content-key cursor. They generate
the 256 possible shard names directly, list only the current shard, and stop at
the configured entry limit; they do not enumerate the complete cache on every
lookup. A limit-sized final batch uses one subsequent empty pass to certify the
end of its cycle. Before deletion, likely LRU victims re-read their access
markers and lease/lock state, so a recently used entry is not evicted merely
because its persisted inventory row predates that access.

Use the host Python associated with pcc for the manual interface:

```bash
uv run python -m pcc.tools.compiler_cache_retention status \
  --root build/bootstrap-pytest-object-cache

uv run python -m pcc.tools.compiler_cache_retention prune \
  --root build/bootstrap-pytest-object-cache --dry-run

uv run python -m pcc.tools.compiler_cache_retention prune \
  --root build/bootstrap-pytest-object-cache
```

The JSON report includes the root, selected policy, complete/protected entry
counts, bytes before/after, scan progress, deterministic victim list, reclaimed
bytes, and quarantine recovery counts.

Configuration is shared by repository and per-user roots:

| Variable | Default | Meaning |
|---|---:|---|
| `PCC_COMPILER_CACHE_RETENTION` | `on` | Set to `off` to disable automatic pruning only. Manual status/prune remains available. |
| `PCC_COMPILER_CACHE_HIGH_BYTES` | `10737418240` | Combined high-water mark. |
| `PCC_COMPILER_CACHE_LOW_BYTES` | `8589934592` | Target after a size-triggered prune. |
| `PCC_COMPILER_CACHE_MAX_UNUSED_DAYS` | `30` | Explicit unused-age limit. |
| `PCC_COMPILER_CACHE_SCAN_LIMIT` | `512` | Maximum entries inspected by one automatic pass. |
| `PCC_COMPILER_CACHE_AUTO_INTERVAL_SECONDS` | `300` | Minimum interval between automatic passes. |

CLI policy flags override these values for one status/prune invocation. A sole
recent entry larger than the high-water mark is retained to avoid an immediate
publish/prune loop; expired entries remain eligible. The current publisher's
entry is always explicitly protected during automatic maintenance.
