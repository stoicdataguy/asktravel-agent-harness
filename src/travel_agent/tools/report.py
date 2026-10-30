"""Progress from inside a slow tool, shown to the user while the tool is still running.

`report(runtime, "dates set")` writes to LangGraph's custom stream; the API renders it as an
indented line under the tool's call in the progress block (see `api/progress.py`). Outside a
stream (evals, tests, a direct `ainvoke`) there is no writer and it does nothing.

Each report is also logged with the time since the tool started, which is how slow steps are
found.
"""

import logging
import time

from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)
_started: dict[str, float] = {}


def report(runtime: ToolRuntime | None, text: str) -> None:
    call_id = getattr(runtime, "tool_call_id", None) or ""
    began = _started.setdefault(call_id, time.monotonic())
    logger.debug("%s +%.1fs %s", call_id or "tool", time.monotonic() - began, text)
    writer = getattr(runtime, "stream_writer", None)
    if writer is None:
        return
    try:
        writer({"progress": {"tool_call_id": call_id, "text": text}})
    except Exception:  # progress must never break the tool
        logger.debug("progress report dropped", exc_info=True)


def done(runtime: ToolRuntime | None) -> None:
    """Forget the tool's start time once it has finished."""
    _started.pop(getattr(runtime, "tool_call_id", None) or "", None)
