"""Literal Python semantic target shared by compiler and runtime providers.

This selects version-dependent behavior and package metadata. It does not
declare complete language support or describe the host Python/C-extension ABI.
"""

PYTHON_TARGET_MAJOR = 3
PYTHON_TARGET_MINOR = 15
PYTHON_TARGET_MICRO = 0
PYTHON_TARGET_VERSION_INFO = (3, 15, 0)
PYTHON_TARGET_VERSION_PARTS = ("3", "15", "0")
PYTHON_TARGET_VERSION = "3.15"
PYTHON_TARGET_FULL_VERSION = "3.15.0"
