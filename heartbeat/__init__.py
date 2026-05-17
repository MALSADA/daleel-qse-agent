"""
HeartBeat — shared heartbeat writer imported by Muraqib pipeline stages.

Usage in news_pipeline.py / news_analyzer.py:
    from heartbeat import write_heartbeat, clear_heartbeat
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_PATH = Path(__file__).parent / "muraqib_heartbeat.json"

_state: dict = {}
_lock = threading.Lock()


def write_heartbeat(**kwargs) -> None:
    """
    Update heartbeat file with current pipeline progress.
    Thread-safe. Never raises — heartbeat failure must never break the pipeline.
    """
    global _state
    with _lock:
        _state.update(kwargs)
        _state["last_heartbeat_utc"] = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        _state.setdefault("pid", os.getpid())
        _state.setdefault("pipeline_running", True)
        try:
            HEARTBEAT_PATH.write_text(json.dumps(_state, indent=2))
        except Exception:
            pass


def clear_heartbeat() -> None:
    """Mark pipeline as cleanly finished. Call on normal exit."""
    write_heartbeat(pipeline_running=False, current_stage="done", current_symbol=None)
