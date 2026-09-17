from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from firebase import get_db

router = APIRouter(prefix="/history", tags=["History"])


def _to_dt(firestore_ts):
    if firestore_ts is None:
        return None
    if hasattr(firestore_ts, "ToDatetime"):
        return firestore_ts.ToDatetime(tzinfo=timezone.utc)
    if isinstance(firestore_ts, datetime):
        return firestore_ts if firestore_ts.tzinfo else firestore_ts.replace(tzinfo=timezone.utc)
    return None


def _location_to_string(location: Any) -> str:
    if isinstance(location, str):
        return location
    if isinstance(location, dict):
        address = location.get("address") or location.get("name") or ""
        if address:
            return address
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is not None and lng is not None:
            return f"{lat}, {lng}"
    if hasattr(location, "latitude"):
        return f"{location.latitude}, {location.longitude}"
    return "Unknown location"


def _normalize_level(level: Any) -> str:
    if isinstance(level, str) and level.lower().startswith("level"):
        return level
    if level in (None, ""):
        return "Level 3"
    return f"Level {level}"


def _format_time(created_dt: datetime | None) -> str:
    if not created_dt:
        return "Just now"
    now = datetime.now(timezone.utc)
    delta = now - created_dt
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "Just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "Yesterday"
    return created_dt.strftime("%b %d")


def _history_label(created_dt: datetime | None) -> str:
    if not created_dt:
        return "Today"
    now = datetime.now(timezone.utc)
    delta = now - created_dt
    days = int(delta.total_seconds() // 86400)
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return created_dt.strftime("%B %d")


@router.get("")
def get_history_reports():
    """Frontend history page: archived incident list."""
    db = get_db()
    docs = list(db.collection("Emergencies").stream())

    reports = []
    for doc in docs:
        data = doc.to_dict() or {}
        created_dt = _to_dt(data.get("createdAt"))
        reports.append({
            "id": doc.id,
            "type": data.get("incidentType", "Unknown"),
            "location": _location_to_string(data.get("location")),
            "time": _format_time(created_dt),
            "urgency": _normalize_level(data.get("level")),
            "date": _history_label(created_dt),
        })

    return {"reports": reports[:10]}
