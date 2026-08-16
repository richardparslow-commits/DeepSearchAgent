"""Research tool registry (Phase 2: tool routing).

A named registry of the mechanisms the research pipeline can invoke, so plan
execution selects a tool by name rather than hardcoding a callable. The default
registry wires the three tools the pipeline uses today:

* ``search`` -- fan out a query across all configured providers
* ``extract`` -- fetch and parse a source page for structured case details
* ``citation_graph`` -- follow CourtListener citation trails (multi-hop)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .fetch import fetch_case_details
from .providers import search_all, traverse_citations


@dataclass(frozen=True)
class Tool:
    """One named, invokable research mechanism."""

    name: str
    description: str
    fn: Callable[..., Any]


class ToolRegistry:
    """A name -> Tool mapping with an explicit, friendly lookup error."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, fn: Callable[..., Any]) -> Tool:
        """Register *fn* under *name* and return the created :class:`Tool`."""
        if not name:
            raise ValueError("Tool name must be non-empty")
        tool = Tool(name=name, description=description, fn=fn)
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        """Return the named tool, raising a helpful error for unknown names."""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(
                f"Unknown tool {name!r}; available: {', '.join(sorted(self._tools))}"
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Return the registered tool names, sorted."""
        return tuple(sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return name in self._tools


def default_tools() -> ToolRegistry:
    """Return a fresh registry with the pipeline's built-in tools."""
    registry = ToolRegistry()
    registry.register(
        "search",
        "Run one query across every configured search provider and merge results.",
        search_all,
    )
    registry.register(
        "extract",
        "Fetch a case source page (HTML or PDF) and extract structured details.",
        fetch_case_details,
    )
    registry.register(
        "citation_graph",
        "Follow CourtListener citation trails (citing and cited opinions).",
        traverse_citations,
    )
    return registry
