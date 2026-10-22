"""strace runs a separate process: importing this skips."""

import unittest

raise unittest.SkipTest("strace needs a separate process")
