"""Tests for scripts/_apply_mutant.py's class-aware method resolution.

mutmut 3.7's `mutmut apply` locates the function to replace with
``find_top_level_function_or_method(module, name)``, which returns the FIRST
method with that name in the module and ignores the class in the
``xǁClassNameǁmethod__mutmut_N`` key. ``providers.py`` has five ``search``
methods, so applying a ``BvaLocalIndexProviderǁsearch`` mutant via ``mutmut
apply`` corrupts ``SearchProvider.search`` instead. These tests pin the
corrected resolution so the re-verification path can never regress.
"""

import importlib.util
from pathlib import Path

import pytest

# libcst and mutmut are dev-only tools installed by the `dev` extra, which the
# regular CI job (pytest + pytest-cov + ruff only) does not install. Skip this
# module rather than erroring at collection so `make test-w` stays green on the
# CI runner; the mutation-kill-gate job installs the full dev extra and this
# helper is exercised there.
cst = pytest.importorskip("libcst")
pytest.importorskip("mutmut")

_SPEC = importlib.util.spec_from_file_location(
    "apply_mutant",
    Path(__file__).resolve().parent.parent / "scripts" / "_apply_mutant.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_apply_mutant = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_apply_mutant)


def _parse(code: str) -> cst.Module:
    return cst.parse_module(code)


def test_find_method_in_class_scopes_to_named_class():
    """A same-named method on an earlier class must not win."""
    module = _parse(
        """
class First:
    def search(self):
        return "first"

class Second:
    def search(self):
        return "second"
"""
    )

    found = _apply_mutant._find_method_in_class(module, "Second", "search")

    assert found is not None
    assert found.name.value == "search"
    assert found.body.body[0].body[0].value.value == '"second"'


def test_find_method_in_class_returns_none_for_missing_class():
    module = _parse(
        """
class First:
    def search(self):
        return "first"
"""
    )

    assert _apply_mutant._find_method_in_class(module, "Missing", "search") is None


def test_find_method_in_class_returns_none_for_missing_method():
    module = _parse(
        """
class First:
    def search(self):
        return "first"
"""
    )

    assert _apply_mutant._find_method_in_class(module, "First", "other") is None


def test_find_top_level_function_returns_top_level_only():
    """Top-level resolution must not descend into any class."""
    module = _parse(
        """
class First:
    def search(self):
        return "first"

def search():
    return "top"
"""
    )

    found = _apply_mutant._find_top_level_function(module, "search")

    assert found is not None
    assert found.name.value == "search"
    assert found.body.body[0].body[0].value.value == '"top"'


def test_find_top_level_function_returns_none_when_only_methods():
    module = _parse(
        """
class First:
    def search(self):
        return "first"
"""
    )

    assert _apply_mutant._find_top_level_function(module, "search") is None
