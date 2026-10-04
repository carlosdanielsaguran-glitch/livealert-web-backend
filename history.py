from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends

from accounts import get_station_scope, route_unassigned
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

def _created_datetime(data: dict[str, Any]) -> datetime | None:
    return _to_dt(data.get("timestamp") or data.get("createdAt"))


# A report is live until it has ended or a day has passed; after that it
# belongs in History. (report.py, dashboard.py and history.py share this rule.)
ARCHIVE_AFTER = timedelta(hours=24)
ENDED_STATUSES = frozenset({"resolved", "closed", "completed", "cancelled", "ended"})


def _has_ended(data: dict[str, Any]) -> bool:
    return str(data.get("status") or "").strip().lower() in ENDED_STATUSES


def _is_archived(data: dict[str, Any]) -> bool:
    if _has_ended(data):
        return True
    created = _created_datetime(data)
    return created is not None and datetime.now(timezone.utc) - created >= ARCHIVE_AFTER


def _report_station_id(data: dict[str, Any]) -> str:
    """The station a report was routed to (same precedence report.py uses)."""
    assigned = data.get("assignedStation") or {}
    return str(data.get("nearestStationId") or assigned.get("stationId") or "").strip()


def _matches_station(data: dict[str, Any], station_id: str | None) -> bool:
    """True if the report belongs to station_id. No station_id = no filtering."""
    if not station_id or not station_id.strip():
        return True
    return _report_station_id(data) == station_id.strip()



def _location_to_string(location: Any) -> str:
    if isinstance(location, str):
        return location
    if isinstance(location, dict):
        address = location.get("address") or location.get("name") or ""
        if address:
            return address
        lat = location.get("latitude", location.get("lat"))
        lng = location.get("longitude", location.get("lng"))
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


def _history_item(doc_id: str, data: dict[str, Any]) -> dict[str, Any]:
    created_dt = _created_datetime(data)
    return {
        "id": doc_id,
        "type": data.get("incidentType") or data.get("channelName") or "Unknown",
        "location": _location_to_string(data.get("location")),
        "time": _format_time(created_dt),
        "urgency": _normalize_level(data.get("level")),
        "date": _history_label(created_dt),
        "status": data.get("status", "pending"),
        # True when the report actually ended; False when it was archived only
        # because a day passed. The History page's "Resolved" filter uses this.
        "ended": _has_ended(data),
        "createdAt": created_dt.isoformat() if created_dt else None,
    }


@router.get("")
def get_history_reports(limit: int | None = None, station_id: str | None = Depends(get_station_scope)):
    """Frontend history page: every incident that is no longer live, newest first.

    A report lands here once it has ended or a day has passed since it was
    created (see _is_archived). Reports still in the legacy Emergencies
    collection are included too so nothing that used to be in History vanishes;
    an ActiveCalls copy wins if the same id exists in both.
    """
    db = get_db()
    entries: dict[str, tuple[datetime, dict[str, Any]]] = {}
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    for doc in db.collection("Emergencies").stream():
        data = doc.to_dict() or {}
        if not _matches_station(data, station_id):
            continue
        entries[doc.id] = (_created_datetime(data) or oldest, _history_item(doc.id, data))

    for doc in route_unassigned(db, list(db.collection("ActiveCalls").stream())):
        data = doc.to_dict() or {}
        if not _is_archived(data) or not _matches_station(data, station_id):
            continue
        entries[doc.id] = (_created_datetime(data) or oldest, _history_item(doc.id, data))

    ordered = sorted(entries.values(), key=lambda entry: entry[0], reverse=True)
    reports = [item for _, item in ordered]
    if limit:
        reports = reports[:limit]

    return {"reports": reports}