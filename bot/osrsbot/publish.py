"""Publish a live status snapshot to the myprojects.cc hub.

The bot pushes a small, non-identifying summary - runtime, actions, failures
and xp gained this session - to the site, so the OSRS Bot page shows whether a
run is going and how it is doing. No account name, no world, no character data
leaves here; only the numbers the run's own summary already prints.

Nothing here is required. With no token set, publishing is simply skipped and
the run is unaffected.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

SITE_API = "https://api.github.com/repos/{repo}/dispatches"


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


def build_contract(script: str, stats, state=None, status: str = "ok") -> dict:
    """Shape the run into the site's data contract (see DATA_CONTRACT.md)."""
    tiles = [
        {"label": "Session", "value": _clock(stats.runtime)},
        {"label": "Actions", "value": str(stats.actions)},
    ]
    if stats.failures:
        tiles.append({"label": "Failures", "value": str(stats.failures)})
    for key, value in sorted(getattr(stats, "counters", {}).items()):
        tiles.append({"label": key.title(), "value": str(value)})

    xp = {}
    try:
        xp = stats.xp_gained(state) if state is not None else {}
    except Exception:
        xp = {}
    for skill, gained in sorted(xp.items()):
        tiles.append({"label": skill.title() + " xp", "value": f"{gained:,}"})

    summary = (f"Running {script} — {_clock(stats.runtime)}, "
               f"{stats.actions} actions."
               if status == "ok" else
               f"Last run: {script} — {_clock(stats.runtime)}, "
               f"{stats.actions} actions.")

    return {
        "project": "osrs",
        "title": "OSRS Bot",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "summary": summary,
        "stats": tiles,
        "note": "Status only — no account or character data is published.",
        "source": "https://github.com/mtc53/osrs-bot",
    }


class Publisher:
    """Dispatches contract snapshots to the site, best-effort and throttled by
    the caller (the Runner's heartbeat interval)."""

    def __init__(self, script: str, repo: str = "", token: str = "", log=print):
        self.script = script
        self.repo = (repo or os.environ.get("MYPROJECTS_REPO", "mtc53/myprojects-site")).strip()
        self.token = (token or os.environ.get("MYPROJECTS_TOKEN", "")).strip()
        self.log = log
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.repo and self.token)

    def beat(self, stats, state=None, final: bool = False) -> None:
        if not self.enabled:
            return
        data = build_contract(self.script, stats, state,
                              status="offline" if final else "ok")
        body = json.dumps({
            "event_type": "publish-data",
            "client_payload": {"project": "osrs", "data": data},
        }).encode("utf-8")
        req = urllib.request.Request(
            SITE_API.format(repo=self.repo), data=body, method="POST", headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "osrs-bot-publisher",
            })
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                if res.status not in (200, 204) and not self._warned:
                    self.log(f"  (hub publish: {self.repo} returned {res.status})")
                    self._warned = True
        except (urllib.error.URLError, OSError) as exc:
            if not self._warned:
                self.log(f"  (hub publish unavailable: {exc})")
                self._warned = True
