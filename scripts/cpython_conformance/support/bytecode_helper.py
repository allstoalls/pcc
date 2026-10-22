"""Bytecode is a CPython implementation detail: importing this skips."""

import unittest

raise unittest.SkipTest("bytecode is a CPython implementation detail")
