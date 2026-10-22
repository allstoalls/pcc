"""Host RSS projections excluded from native worker dependency discovery."""


def coordinator_rss_bytes(sys, windows_rss) -> int:
    """Non-PCC conservative projection; never called by native pcc.

    ru_maxrss is a high-water mark, so it can over-reserve but cannot understate
    the host's resident high water. No subprocess or third-party sampler is used.
    """
    if sys.platform == "win32":
        return windows_rss(True)
    try:
        import resource
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError, AttributeError):
        return -1
    return value if sys.platform == "darwin" else value * 1024


def windows_rss_bytes(peak: bool) -> int:
    """Host-only Win32 projection of the same working-set API as pcc1."""
    try:
        import ctypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_uint32), ("faults", ctypes.c_uint32)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working_set", "working_set", "peak_paged", "paged",
                    "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        kernel.K32GetProcessMemoryInfo.restype = ctypes.c_int32
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return -1
        return int(counters.peak_working_set if peak else counters.working_set)
    except (ImportError, OSError, ValueError, AttributeError):
        return -1
