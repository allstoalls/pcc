"""Dependency-light signatures shared by unsafe lowering and discovery.

Only this bounded zero-argument subset has provider-independent inference as
well as intrinsic lowering. Other known intrinsics can still consume wrapper
parameter/return exports and must not acquire this dependency exemption.
"""

UNSAFE_PROVIDER_INDEPENDENT_ZERO_ARG_RETURNS = {
    "null": "pointer",
    "frame_address": "pointer",
    "darwin_current_rss_bytes": "int",
    "darwin_peak_rss_bytes": "int",
    "getpid": "int",
    "initial_environ": "pointer",
    "target_sys_platform": "dynamic",
    "target_platform_machine": "dynamic",
    "va_start": "pointer",
    "darwin_errno_location": "pointer",
    "kqueue_create": "int",
    "thread_safepoint": "none",
    "gc_backend_current": "int",
}
