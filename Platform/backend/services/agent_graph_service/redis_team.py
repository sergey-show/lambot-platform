"""Optional: Redis — latest run_id by project_id for quick hints."""

from __future__ import annotations

import os

REDIS_URL = os.getenv("REDIS_URL", "").strip()
_TTL_SEC = int(os.getenv("AI_TEAM_REDIS_TTL_SEC", str(7 * 86400)))


def cache_latest_run_id(project_id: str, run_id: str) -> None:
    if not REDIS_URL:
        return
    try:
        import redis

        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.set(f"lambot:ai_team:latest:{project_id}", run_id, ex=_TTL_SEC)
    except Exception:
        pass


def get_cached_latest_run_id(project_id: str) -> str | None:
    if not REDIS_URL:
        return None
    try:
        import redis

        r = redis.from_url(REDIS_URL, decode_responses=True)
        v = r.get(f"lambot:ai_team:latest:{project_id}")
        return str(v) if v else None
    except Exception:
        return None
