"""Small, test-only fault injector with machine-readable diagnostics.

The module is intentionally kept below ``tests/`` so application code cannot
enable these failure points.  ``FAULT_ARTIFACT_PATH`` is optional; CI sets it
to retain a JSONL timeline alongside pytest's JUnit and console output.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


@dataclass
class FaultScenario:
    name: str
    started_at: float = field(default_factory=time.monotonic)
    _hits: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def hit_once(self, fault: str, **evidence: Any) -> bool:
        """Return true for the first named hit and record every decision."""
        with self._lock:
            count = self._hits.get(fault, 0) + 1
            self._hits[fault] = count
        injected = count == 1
        self.record(
            "fault_decision",
            fault=fault,
            hit=count,
            injected=injected,
            **evidence,
        )
        return injected

    def record(self, stage: str, **evidence: Any) -> None:
        entry = {
            "scenario": self.name,
            "stage": stage,
            "elapsed_ms": round((time.monotonic() - self.started_at) * 1000, 3),
            **evidence,
        }
        rendered = json.dumps(entry, sort_keys=True, default=str)
        logger.info("FAULT_DIAGNOSTIC %s", rendered)
        artifact_path = os.getenv("FAULT_ARTIFACT_PATH")
        if not artifact_path:
            return
        path = Path(artifact_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with path.open("a", encoding="utf-8") as artifact:
                artifact.write(rendered + "\n")
