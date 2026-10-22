import sys
sys.modules["cache_failed_escape"] = sys.modules[__name__]
raise ValueError("cache failure")
