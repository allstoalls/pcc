# Investigation: Stage1 links exceed the compact stack-map memory budget

## Status
active — 2026-10-14. Measured on CI run 38584467472 (`9cbcb1e9`); the Mach-O
link holds about two copies of its inputs at its peak. Proposals below.

## Problem Description
Stage1 codegen finishes on Windows (~3044 s) and macOS (~4920 s), then the
in-process link refuses: `9cbcb1e9` added the refused sizes to the error.

- Windows, `pe_x86_64.py` "PE final stack-map snapshot": `raw=10945047087
  retained=8343004237 limit=10201337856` (artifact `bootstrap-windows-x86_64`,
  `platform-qualification/shared/stage1.log:21236`). Over by 7%.
- macOS, `macho_link.py` "Mach-O input stack-map snapshot": `raw=3711958786
  limit=2485518336` (artifact `bootstrap-macos-arm64`,
  `worker-prior-gate/stage1.log:20020`). Over by 49%.

Expected: the link fits the runner. On macOS the limit comes from the runner,
not from configuration: `profile/stage1.result.json` records 3355852800
reclaimable bytes at start, a 536870912 reserve and `max_tree_rss_bytes
2818981888`. The process-tree samples (`stage1.process.*/samples.tsv`) show
the coordinator at about 1.98 GB RSS when the link refused.

Downloaded artifacts:
`/Users/jiamo/.claude/jobs/c3084f8b/tmp/art_38584467472_{win,mac}` (copy in
the recovery archive, `2026-10-13/link-memory/`).

## Analysis (source, `9cbcb1e9`)
`link_relocatable_native` (`macho_link.py`):
1. inspects every input in parallel and keeps all of them
   (`parsed_objects`, `inspected_inputs`, `objects`) until the end;
2. appends each section payload to `data_parts` without copying: a packed
   native input's `section_data` is a zero-copy memoryview into its encoded
   buffer, a plain native section returns its own bytes;
3. joins each merged section (`b"".join(data_parts)`): a full private copy
   while every input is still alive;
4. converts rebased sections to `bytearray` and packages each section with
   `bytes(m.data)`, which copies a bytearray once more;
5. retires the inputs only after packaging.

So the merge peaks at about inputs + merged copy. The input stack-map check
estimates `input_raw_bytes + merged data_size + ...`, which counts the shared
views twice, so it refuses earlier than step 3 would; the later "merged
stack-map buffers" check measures a real two-copy state.

`_prepare_executable_image` (`macho_exec.py`) then copies every merged section
into a mutable `bytearray` while the merged source view keeps its payloads:
a second two-copy peak.

## Proposals
- No.1 release Mach-O inputs and merged payloads as soon as they are copied
  [pending]
- No.2 the PE link's final stack-map snapshot (Windows, 7% over) [pending]

## No.1 release Mach-O inputs and merged payloads as soon as they are copied [pending]
Changes (macho_link.py, native_object.py, macho_exec.py):
- the input stack-map snapshot estimate counts only merged bytes that do not
  share an input buffer (padding, patched bytearrays, sliced copies), not the
  zero-copy payload views a second time;
- consumed inputs are retired right after the merge loop, before the join;
  data sections are joined before the text chunks, so each chunk's join can
  free the inputs whose text ends in it, and `input_raw_bytes` drops to zero
  after the joins;
- packaging drops each rebased bytearray once its bytes copy exists;
- the executable link takes each payload out of the single-use source view
  (`take_section_data`) as it copies it, and `retained_source_bytes` for the
  final image is measured after preparation (relocation rows only).

Identity: `link_identity.py` (recovery archive `link-memory/`) links the 181
members of a runtime archive plus a minimal `_main` with the old and the new
linker: same image sha256 `6f8e6508...`.

Memory on that benchmark is not lower: tracemalloc peak 200393205 bytes
before, 199870019 after, about 5.9 times the 33867744 input bytes. Those
inputs are parsed Mach-O objects, whose relocation and symbol records are
Python objects; the payload bytes these changes release are a small part of
that peak. Stage1 links packed native objects instead, so the effect there is
measured on CI: the refusing estimate no longer double counts, and the join
no longer holds every input.

## No.2 the PE link's final stack-map snapshot (Windows, 7% over) [pending]
`_link_selected_objects` copied the final stack-map table twice (a bytearray
slice, then bytes()) to validate an immutable snapshot: 2 x 1.3 GB. On CPython
it now slices a memoryview, so bytes() makes the one copy the validator
requires, and the budget charges one copy; the native runtime keeps the old
path. Expected effect on run 38584467472's numbers: 8343004237 retained plus
one 1.3 GB copy, under the 10201337856 limit.
