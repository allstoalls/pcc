"""Exact real-host single-pass assembler controls for the existing CI invocation."""

from tests.python.test_x86_64_assembler_lifetime import (
    test_single_pass_payload_chunks_bound_storage_and_preserve_order,
    test_single_pass_object_matches_two_pass_driver_on_same_input,
    test_single_pass_retires_payload_chunks_when_publishing,
    test_reference_validation_precedes_deferred_zero_fills,
    test_local_branch_patches_preserve_signed_rel32_values,
    test_label_dense_sections_match_two_pass_driver,
)
from tests.python.test_x86_64_numeric_data_batching import (
    test_small_or_disabled_bounds_preserve_mixed_width_complete_object,
    test_symbol_label_alignment_zero_size_and_section_barriers,
    test_full_coff_bytes_keep_symbol_relocation_stackmap_and_unwind_sections,
)
