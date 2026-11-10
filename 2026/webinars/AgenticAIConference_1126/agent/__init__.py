"""Demo agent package.

Keeps terminal output readable on stage: library deprecation warnings and Neo4j's
query-planner notifications (e.g. "property key does not exist" before the first
note is written) are informational, so they're hidden here, before anything else
is imported. Neocarta's MCP server logs go to neocarta-mcp.log instead.

Some libraries re-enable their own deprecation warnings with
warnings.simplefilter("always", ...), which outranks an ignore filter, so we drop
them at display time instead.
"""

import logging
import warnings

_show_warning = warnings.showwarning


def _hide_deprecations(message, category, *args, **kwargs):
    if issubclass(category, (DeprecationWarning, PendingDeprecationWarning)):
        return
    _show_warning(message, category, *args, **kwargs)


warnings.showwarning = _hide_deprecations
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
