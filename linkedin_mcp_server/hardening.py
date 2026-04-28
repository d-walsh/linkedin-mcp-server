"""
LinkedIn MCP Server — crash-hardening infrastructure.

Provides:
- safe_tool() decorator: timeout-aware wrapper that returns structured errors
  instead of propagating exceptions that could crash the server process.
- Structured logging to ~/.cache/linkedin-mcp/server.log
- Server-level diagnostics (uptime, call count, last error)
- Top-level exception hooks (sys.excepthook + asyncio handler)
- 60-second heartbeat coroutine
- linkedin_health_self() health-check tool data

Only stdlib + fastmcp are imported here.
"""

import asyncio
import functools
import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

LOG_PATH = Path.home() / ".cache" / "linkedin-mcp" / "server.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("linkedin-mcp")

# ---------------------------------------------------------------------------
# Server-level diagnostics (updated by safe_tool wrapper)
# ---------------------------------------------------------------------------

_server_start_time: float = time.monotonic()
_tool_call_count: int = 0
_last_error_ts: Optional[float] = None
_last_error_tool: Optional[str] = None
_last_error_msg: Optional[str] = None


# ---------------------------------------------------------------------------
# safe_tool decorator — never-crash guarantee for every tool handler
# ---------------------------------------------------------------------------

def safe_tool(timeout_s: float = 60.0) -> Callable:
    """Wrap a tool handler with timeout + total exception capture.

    Even unhandled errors return a structured error dict to the MCP client
    instead of propagating an exception that could crash the server process.
    Timeouts and exceptions are logged to LOG_PATH for postmortem analysis.

    Usage::

        @safe_tool(timeout_s=90.0)
        async def my_tool(...) -> dict:
            ...

    The decorator preserves the original function's __name__, __doc__, and
    __wrapped__ attribute so FastMCP introspection is unaffected.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            global _tool_call_count, _last_error_ts, _last_error_tool, _last_error_msg
            tool_name = fn.__name__
            _tool_call_count += 1
            call_n = _tool_call_count
            logger.info("CALL #%d %s", call_n, tool_name)
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_s)
                elapsed = time.monotonic() - t0
                logger.info("OK   #%d %s (%.1fs)", call_n, tool_name, elapsed)
                return result
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                _last_error_ts = time.time()
                _last_error_tool = tool_name
                _last_error_msg = f"timeout after {elapsed:.1f}s (limit {timeout_s:.0f}s)"
                logger.warning(
                    "TIMEOUT #%d %s after %.1fs (limit %.0fs)",
                    call_n, tool_name, elapsed, timeout_s,
                )
                return {
                    "error": "timeout",
                    "tool": tool_name,
                    "timeout_s": timeout_s,
                    "message": (
                        f"{tool_name} did not complete within {timeout_s:.0f}s. "
                        "LinkedIn page navigation may be slow or the browser session is stale."
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - t0
                _last_error_ts = time.time()
                _last_error_tool = tool_name
                _last_error_msg = f"{type(exc).__name__}: {exc}"
                tb = traceback.format_exc()
                logger.error(
                    "CRASH #%d %s after %.1fs: %s\n%s",
                    call_n, tool_name, elapsed, exc, tb,
                )
                return {
                    "error": "exception",
                    "tool": tool_name,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Unhandled-exception hooks — forensic logging before process dies
# ---------------------------------------------------------------------------

def _excepthook(exc_type: type, exc_value: BaseException, exc_tb: Any) -> None:
    """Log any uncaught synchronous exception to LOG_PATH before process exits."""
    logger.critical(
        "UNCAUGHT EXCEPTION: %s: %s\n%s",
        exc_type.__name__,
        exc_value,
        "".join(traceback.format_tb(exc_tb)),
    )
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Log unhandled asyncio coroutine exceptions before they are silently dropped."""
    exc = context.get("exception")
    msg = context.get("message", "no message")
    if exc is not None:
        logger.error(
            "ASYNCIO UNHANDLED: %s: %s\n%s",
            type(exc).__name__,
            exc,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
    else:
        logger.error(
            "ASYNCIO UNHANDLED (no exception object): %s | context: %s", msg, context
        )


def install_exception_hooks() -> None:
    """Install sys.excepthook and schedule asyncio exception handler.

    Call once at server startup (before the event loop runs tools).
    """
    sys.excepthook = _excepthook
    logger.info("Exception hooks installed")

    # Schedule the asyncio handler for when a loop is available.
    # If the loop is already running, set it immediately; otherwise
    # register it to be set when it starts.
    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(asyncio_exception_handler)
    except RuntimeError:
        # No loop yet — install via a startup task in the lifespan instead.
        pass


# ---------------------------------------------------------------------------
# Heartbeat — logs "alive" every 60s while the event loop is running
# ---------------------------------------------------------------------------

async def heartbeat() -> None:
    """Log a heartbeat every 60 seconds. Provides forensic timeline if the server crashes."""
    while True:
        uptime = int(time.monotonic() - _server_start_time)
        last_err = (
            f"{int(time.time() - _last_error_ts)}s ago on {_last_error_tool}"
            if _last_error_ts
            else "none"
        )
        logger.info(
            "HEARTBEAT uptime=%ds calls=%d last_error=%s",
            uptime,
            _tool_call_count,
            last_err,
        )
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Health data helper (used by linkedin_health_self tool)
# ---------------------------------------------------------------------------

def health_data(browser_session_alive: Optional[bool] = None) -> dict[str, Any]:
    """Return structured health diagnostics for the linkedin_health_self tool."""
    uptime_s = int(time.monotonic() - _server_start_time)
    last_err: Optional[dict[str, Any]] = None
    if _last_error_ts is not None:
        last_err = {
            "seconds_ago": int(time.time() - _last_error_ts),
            "tool": _last_error_tool,
            "message": _last_error_msg,
        }
    return {
        "status": "ok",
        "uptime_seconds": uptime_s,
        "total_tool_calls": _tool_call_count,
        "last_error": last_err,
        "log_path": str(LOG_PATH),
        "browser_session_alive": browser_session_alive,
    }
