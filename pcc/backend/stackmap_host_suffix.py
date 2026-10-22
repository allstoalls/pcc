"""Bounded CPython suffix planning for the budgeted PE stack-map merger.

Only exact byte suffixes share storage. A record's location count, ordering
and relative derived-base indices stay unchanged. Scratch belongs to one
input at a time; no per-location Python objects or global suffix trie exist.
"""

from . import precise_stackmap as wire


def plan_suffixes(payload, scanned, table_start, table_count, table,
                  table_index, table_locations, merge_locations, *,
                  budget, input_bytes, function_bytes, function_count):
    """Populate materialized vectors and return source-range -> output index.

    Reverse-byte sorting makes a suffix a prefix. A right-to-left walk needs
    one exact prefix comparison per vector, rather than comparing all pairs.
    Sorting costs O(V log V) byte comparisons and O(S + V) scratch, where S is
    the admitted sum of distinct source-range lengths (overlaps count fully).

    The global index owns materialized vectors only. A later input can store
    a short vector omitted here, but never more than once; output remains a
    subset of the whole-vector-only merger's unique vectors. This avoids
    keeping detached alias bytes alive after the per-input plan is retired.
    """
    location_size = wire.LOCATION_SIZE
    record_size = wire.RECORD_SIZE
    function_size = wire.FUNCTION_SIZE
    # Preserve the ordinary merger's two-copy construction reservation. Each
    # scanned descriptor and each live container/header also has an entry.
    base_entries = 4 * function_count + len(scanned) + 16

    def admit(scratch_bytes, ranges, prospective_locations, label):
        budget.check_peak(
            input_bytes + 2 * (wire.HEADER_SIZE + function_bytes
                               + prospective_locations * location_size)
            + scratch_bytes,
            # Per range: source dict row, reversed-bytes header/list tuple,
            # sort pointer workspace and representative-destination row.
            entries=base_entries + len(table_index) + 4 * ranges,
            label=label,
        )

    admit(0, 0, table_locations, "suffix plan")
    source_indices = {}
    reverse_vectors = []
    reverse_bytes = 0
    for _fid, fn_start, fn_end in scanned:
        record_count = (fn_end - fn_start - function_size) // record_size
        budget.charge_records(record_count)
        cursor = fn_start + function_size
        for _ in range(record_count):
            count = payload[cursor + 20] | (payload[cursor + 21] << 8)
            index = (payload[cursor + 28] | (payload[cursor + 29] << 8)
                     | (payload[cursor + 30] << 16) | (payload[cursor + 31] << 24))
            if index + count > table_count:
                raise wire.PreciseStackMapError(
                    "stack-map record names locations outside its table"
                )
            source_key = (index << 16) | count
            if count and source_key not in source_indices:
                merge_locations += count
                if merge_locations > wire.MAX_LOCATIONS:
                    raise wire.PreciseStackMapError("too much stack-map location merge work")
                size = count * location_size
                # The source slice and its reversed copy overlap briefly.
                # Admit both before either allocation or any index growth.
                admit(reverse_bytes + 2 * size, len(source_indices) + 1,
                      table_locations, "suffix range")
                start = table_start + index * location_size
                reverse_vectors.append((payload[start:start + size][::-1], source_key))
                source_indices[source_key] = source_key
                reverse_bytes += size
            cursor += record_size
    admit(reverse_bytes, len(source_indices), table_locations, "suffix sort")
    reverse_vectors.sort()
    representative = None
    representative_key = 0
    for vector, source_key in reversed(reverse_vectors):
        if representative is None or not representative.startswith(vector):
            representative = vector
            representative_key = source_key
        source_indices[source_key] = representative_key

    destinations = {}
    # Dict insertion order is the original record traversal order. Do not
    # let lexicographic sort order reorder the retained output vectors.
    for source_key, representative_key in source_indices.items():
        if source_key != representative_key:
            continue
        count = source_key & 0xFFFF
        size = count * location_size
        admit(reverse_bytes + size, len(source_indices), table_locations + count,
              "suffix append")
        start = table_start + (source_key >> 16) * location_size
        key = payload[start:start + size]
        if key in table_index:
            destination = table_index[key]
        else:
            destination = table_locations
            table_index[key] = destination
            table.append(key)
            table_locations += count
            if table_locations > wire.MAX_LOCATIONS:
                raise wire.PreciseStackMapError("too many stack-map locations")
        destinations[source_key] = destination
        # Equal global keys are not owned by the global table; retire them.
        key = None
    for source_key, representative_key in source_indices.items():
        source_indices[source_key] = (destinations[representative_key]
                                     + (representative_key & 0xFFFF)
                                     - (source_key & 0xFFFF))
    return source_indices, table_locations, merge_locations
