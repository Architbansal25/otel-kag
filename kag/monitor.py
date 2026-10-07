"""Background health monitor: records exactly when each service goes DOWN / UP.

Traces can say when errors started, but a crashed process emits nothing at all,
so "when did it die?" needs someone to have been watching. The console starts
this thread; every transition is appended to health_events.json, which the graph
builder reads, so the CLI benefits too.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests
import yaml

import graph as graph_mod

INTERVAL_S = 2.0
MAX_EVENTS = 200


def _services() -> Dict[str, int]:
    facts = yaml.safe_load((graph_mod.HERE / "facts.yaml").read_text(encoding="utf-8"))
    return {s["id"]: s["port"] for s in facts.get("services", []) if s.get("port")}


def check(port: int) -> tuple:
    """Returns (state, detail) where state is UP, DEGRADED or DOWN."""
    url = f"http://{graph_mod.SERVICE_HOST}:{port}/actuator/health"
    try:
        body = requests.get(url, timeout=2).json()
    except requests.RequestException as exc:
        return "DOWN", "connection refused" if "refused" in str(exc).lower() else "no response"
    except ValueError:
        return "DEGRADED", "health endpoint returned garbage"
    if body.get("status") == "UP":
        return "UP", ""
    down = [k for k, v in (body.get("components") or {}).items()
            if (v or {}).get("status") != "UP"]
    return "DEGRADED", "failing: " + ", ".join(down) if down else body.get("status", "")


class HealthMonitor(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="health-monitor")
        self.services = _services()
        self.state: Dict[str, str] = {}
        self.lock = threading.Lock()

    def run(self) -> None:
        while True:
            for name, port in self.services.items():
                state, detail = check(port)
                with self.lock:
                    previous: Optional[str] = self.state.get(name)
                    self.state[name] = state
                # The first observation is a baseline, not an event -- except a
                # service that is already down when we start watching.
                if previous != state and (previous is not None or state != "UP"):
                    self._record(name, previous or "UNKNOWN", state, detail)
            time.sleep(INTERVAL_S)

    def _record(self, service: str, before: str, after: str, detail: str) -> None:
        events: List[dict] = graph_mod.load_health_events()
        events.append({"service": service, "from": before, "to": after, "detail": detail,
                       "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "epoch": time.time()})
        try:
            graph_mod.HEALTH_EVENTS.write_text(json.dumps(events[-MAX_EVENTS:], indent=2),
                                               encoding="utf-8")
        except OSError:
            pass

    def snapshot(self) -> Dict[str, str]:
        with self.lock:
            return dict(self.state)


def recent_events(limit: int = 20) -> List[dict]:
    return graph_mod.load_health_events()[-limit:]
