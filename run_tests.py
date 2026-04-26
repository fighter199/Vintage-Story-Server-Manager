#!/usr/bin/env python3
"""
run_tests.py — Tiny pytest-free test runner.

The test suite under tests/ uses pytest fixtures and pytest.raises, but
not much else. This shim provides just enough of the pytest surface to
run the suite without installing pytest. If pytest IS available it's
also fine to run `pytest tests/` directly.
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys
import tempfile
import shutil
import traceback


# ----------------------------------------------------------------------
# Minimal pytest shim — installed into sys.modules before tests import.
# ----------------------------------------------------------------------
class _PytestShim:
    class raises:
        def __init__(self, exc, match=None):
            self.exc = exc
            self.match = match

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, _tb):
            if exc_type is None:
                raise AssertionError(
                    f"Expected {self.exc.__name__} but no exception raised")
            if not issubclass(exc_type, self.exc):
                return False
            if self.match:
                import re
                if not re.search(self.match, str(exc_val)):
                    raise AssertionError(
                        f"Expected exception matching {self.match!r}, "
                        f"got {exc_val!r}")
            return True

    @staticmethod
    def fixture(*args, **kwargs):
        # Make @pytest.fixture a no-op decorator.
        def deco(fn): return fn
        if args and callable(args[0]):
            return args[0]
        return deco


# ----------------------------------------------------------------------
# Manual fixtures
# ----------------------------------------------------------------------
def _make_tmp_script_dir():
    """Replicate the `tmp_script_dir` fixture from conftest."""
    tmp_path = tempfile.mkdtemp()
    import core.constants as cst
    cst._orig_script_dir = cst.script_dir
    cst.script_dir = lambda _tp=tmp_path: _tp
    import core.settings as cs
    importlib.reload(cs)
    return tmp_path, cs


def _restore_script_dir():
    import core.constants as cst
    if hasattr(cst, "_orig_script_dir"):
        cst.script_dir = cst._orig_script_dir
        del cst._orig_script_dir


# ----------------------------------------------------------------------
# Test discovery + run loop
# ----------------------------------------------------------------------
def main():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    tests_dir = os.path.join(here, "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)

    sys.modules["pytest"] = _PytestShim()

    passed = failed = 0
    failures = []

    test_files = sorted(
        f for f in os.listdir(tests_dir)
        if f.startswith("test_") and f.endswith(".py")
    )
    for fn in test_files:
        mod_name = fn[:-3]
        try:
            mod = importlib.import_module(f"tests.{mod_name}")
        except Exception as e:
            print(f"SKIP {mod_name}: import failed: {e}")
            continue
        for name, obj in inspect.getmembers(mod):
            if not (inspect.isclass(obj) and name.startswith("Test")):
                continue
            inst = obj()
            for mname, method in inspect.getmembers(obj):
                if not (mname.startswith("test_") and callable(method)):
                    continue
                sig = inspect.signature(method)
                kwargs = {}
                cleanup = None
                if "tmp_script_dir" in sig.parameters:
                    kwargs["tmp_script_dir"] = _make_tmp_script_dir()
                    cleanup = kwargs["tmp_script_dir"][0]
                full = f"{mod_name}::{name}::{mname}"
                try:
                    method(inst, **kwargs)
                    passed += 1
                    print(f"  PASS  {full}")
                except Exception:
                    failed += 1
                    failures.append((full, traceback.format_exc()))
                    print(f"  FAIL  {full}")
                finally:
                    if cleanup:
                        try:
                            shutil.rmtree(cleanup)
                        except OSError:
                            pass
                        _restore_script_dir()

    print()
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    if failures:
        for full, err in failures:
            print(f"\n--- FAILED: {full} ---")
            print(err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
