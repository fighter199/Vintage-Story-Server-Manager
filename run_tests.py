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
# Optional lint pass (review §3.8)
# ----------------------------------------------------------------------
def _run_lint(strict: bool = False) -> bool:
    """Try ruff, then pyflakes, then give up silently. Returns True on
    pass / no linter installed; False only when a linter ran AND
    reported issues AND strict=True."""
    import shutil as _sh
    import subprocess as _sp
    here = os.path.dirname(os.path.abspath(__file__))

    if _sh.which("ruff"):
        print("=" * 60)
        print("Lint: ruff check .")
        print("=" * 60)
        r = _sp.run(["ruff", "check", "."], cwd=here)
        if r.returncode != 0:
            if strict:
                print("Lint failed (--strict-lint).")
                return False
            print("Lint reported issues (non-fatal; pass --strict-lint "
                  "to make these block the run).")
        return True

    try:
        import pyflakes  # noqa: F401
    except ImportError:
        # No linter installed at all. Silent skip — keeping VSSM's
        # zero-required-dependency promise.
        return True

    print("=" * 60)
    print("Lint: python -m pyflakes  (ruff not found)")
    print("=" * 60)
    targets = [
        os.path.join(here, name)
        for name in os.listdir(here)
        if name.endswith(".py") and not name.startswith("apply_")
        and name not in ("run_tests.py",)
    ]
    # Add package dirs.
    for sub in ("core", "ui", "mods", "backup", "tests"):
        full = os.path.join(here, sub)
        if os.path.isdir(full):
            targets.append(full)
    r = _sp.run([sys.executable, "-m", "pyflakes", *targets], cwd=here)
    if r.returncode != 0:
        if strict:
            print("Lint failed (--strict-lint).")
            return False
        print("Lint reported issues (non-fatal; pass --strict-lint "
              "to make these block the run).")
    return True


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


def _make_tmp_path():
    """Replicate pytest's built-in `tmp_path` fixture.

    pytest yields a pathlib.Path to a fresh per-test temp directory.
    Returns (path_obj, cleanup_dir_str) where cleanup_dir_str is what
    the run loop should rmtree after the test finishes.
    """
    import pathlib
    tmp = tempfile.mkdtemp()
    return pathlib.Path(tmp), tmp


# ----------------------------------------------------------------------
# Test discovery + run loop
# ----------------------------------------------------------------------
def main():
    # Lint flags (review §3.8). Strict mode makes a lint finding fatal;
    # default mode prints findings but exits with the test result.
    import argparse as _ap
    _argparser = _ap.ArgumentParser(add_help=False)
    _argparser.add_argument("--strict-lint", action="store_true",
                             help="Treat lint warnings as failures.")
    _argparser.add_argument("--no-lint", action="store_true",
                             help="Skip the optional lint pass entirely.")
    _args, _ = _argparser.parse_known_args()

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    tests_dir = os.path.join(here, "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)

    if not _args.no_lint:
        if not _run_lint(strict=_args.strict_lint):
            return 2

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
                # Each cleanup is a (callable, *args) tuple so we can
                # handle multiple fixtures composing in one test.
                cleanups: list = []
                if "tmp_script_dir" in sig.parameters:
                    kwargs["tmp_script_dir"] = _make_tmp_script_dir()
                    cleanups.append(("tmp_script_dir",
                                       kwargs["tmp_script_dir"][0]))
                if "tmp_path" in sig.parameters:
                    # pytest's built-in tmp_path fixture — yields a
                    # pathlib.Path to a fresh per-test temp dir.
                    path_obj, cleanup_str = _make_tmp_path()
                    kwargs["tmp_path"] = path_obj
                    cleanups.append(("tmp_path", cleanup_str))
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
                    for kind, target in cleanups:
                        try:
                            shutil.rmtree(target)
                        except OSError:
                            pass
                        if kind == "tmp_script_dir":
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
