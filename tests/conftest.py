"""
Pytest configuration shared across the test suite.
Adds the package root to sys.path so `import core.parsers` etc. works
regardless of where pytest is invoked from.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
