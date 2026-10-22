"""Only the existing fake sampler controls, within the default invocation."""

from tests.python.test_process_tree_sample_tool import (
    test_process_table_keeps_full_worker_command_and_manifest,
    test_safety_table_avoids_all_process_argv_and_queries_only_largest,
    test_safety_process_table_failure_uses_only_bounded_retries,
    test_mac_ci_guard_auto_selection_reaches_receipt_environment_and_state,
    test_mac_ci_guard_rejection_persists_observation_without_launch,
    test_guard_publishes_adopted_attempt_start_and_original_recovery_deadline,
    test_stage1_clean_environment_forwards_recovery_only_with_guard_state,
)
