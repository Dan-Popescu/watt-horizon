"""Every module imports on its own (first, in a fresh module table): catches import cycles that only show up
depending on which module is imported first (mypy does not see them)."""

import importlib
import pkgutil
import sys

import radar


def test_each_module_imports_first() -> None:
    names = sorted(m.name for m in pkgutil.walk_packages(radar.__path__, "radar."))
    failures = []
    saved = {k: v for k, v in sys.modules.items() if k == "radar" or k.startswith("radar.")}
    try:
        for name in names:
            for k in [k for k in sys.modules if k.startswith("radar.")]:
                del sys.modules[k]
            try:
                importlib.import_module(name)
            except ImportError as exc:
                failures.append(f"{name}: {exc}")
    finally:
        sys.modules.update(saved)
    assert failures == []
