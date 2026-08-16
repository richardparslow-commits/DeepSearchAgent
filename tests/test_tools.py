"""Tests for the research tool registry (va_legal_agent.tools)."""

import pytest

from va_legal_agent.tools import Tool, ToolRegistry, default_tools


def test_register_and_get_returns_same_tool():
    registry = ToolRegistry()
    tool = registry.register("search", "Run a query", lambda q: q)

    assert registry.get("search") is tool
    assert isinstance(tool, Tool)
    assert tool.name == "search"
    assert tool.description == "Run a query"
    assert tool.fn("x") == "x"


def test_register_rejects_empty_name():
    registry = ToolRegistry()

    with pytest.raises(ValueError) as excinfo:
        registry.register("", "desc", lambda: None)

    assert str(excinfo.value) == "Tool name must be non-empty"


def test_get_unknown_tool_lists_available():
    registry = ToolRegistry()
    registry.register("extract", "e", lambda: None)
    registry.register("search", "s", lambda: None)

    with pytest.raises(KeyError) as excinfo:
        registry.get("code")

    assert excinfo.value.args[0] == "Unknown tool 'code'; available: extract, search"


def test_names_are_sorted_and_contains():
    registry = ToolRegistry()
    registry.register("extract", "e", lambda: None)
    registry.register("search", "s", lambda: None)

    assert registry.names() == ("extract", "search")
    assert "search" in registry
    assert "code" not in registry


def test_default_tools_wires_the_pipeline_callables():
    from va_legal_agent.fetch import fetch_case_details
    from va_legal_agent.providers import search_all, traverse_citations

    registry = default_tools()

    assert registry.names() == ("citation_graph", "extract", "search")
    # The registry wires the actual pipeline callables, not placeholders.
    assert registry.get("search").fn is search_all
    assert registry.get("extract").fn is fetch_case_details
    assert registry.get("citation_graph").fn is traverse_citations
    # And each tool carries its human-readable description verbatim.
    assert (
        registry.get("search").description
        == "Run one query across every configured search provider and merge results."
    )
    assert (
        registry.get("extract").description
        == "Fetch a case source page (HTML or PDF) and extract structured details."
    )
    assert (
        registry.get("citation_graph").description
        == "Follow CourtListener citation trails (citing and cited opinions)."
    )
