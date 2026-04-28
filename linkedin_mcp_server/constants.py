"""Project-wide constants."""

# Default tool timeout (browser-heavy ops can be slow).
TOOL_TIMEOUT_SECONDS: float = 90.0

# Operation-specific timeouts used by the safe_tool wrapper.
# Profile/company page navigation is slow but bounded.
TIMEOUT_PROFILE_S: float = 90.0
# Search ops iterate multiple pages.
TIMEOUT_SEARCH_S: float = 120.0
# Auth/session ops include browser launch + cookie bridging.
TIMEOUT_AUTH_S: float = 180.0
