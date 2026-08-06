"""Durable run state: the once-daily guard plus known campaign IDs for delta/resume.

state.json holds only bookkeeping (last_run, run count, which campaign IDs we've
already fully scraped). The actual campaign data lives in campaigns.json.
"""
import json
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc)


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"last_run": None, "runs": 0, "campaigns": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                # Corrupt state should never sink a run — start clean instead.
                pass
        self.data.setdefault("campaigns", {})

    @property
    def last_run(self):
        ts = self.data.get("last_run")
        return datetime.fromisoformat(ts) if ts else None

    def hours_since_last_run(self):
        lr = self.last_run
        return None if lr is None else (_now() - lr).total_seconds() / 3600.0

    def is_known(self, campaign_id):
        return campaign_id in self.data["campaigns"]

    def mark_scraped(self, campaign_id, url):
        self.data["campaigns"][campaign_id] = {
            "scraped_at": _now().isoformat(),
            "url": url,
        }

    @property
    def known_count(self):
        return len(self.data["campaigns"])

    def finish_run(self):
        self.data["last_run"] = _now().isoformat()
        self.data["runs"] = self.data.get("runs", 0) + 1
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
