from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from firebase import get_db

try:
    from google.cloud import firestore  # type: ignore
except ImportError:  # pragma: no cover - depends on which SDK the project uses
    from firebase_admin import firestore  # type: ignore

router = APIRouter(prefix="/reports", tags=["Reports"])


class ReportUpdate(BaseModel):
    status: str | None = None
    level: str | None = None
    incidentType: str | None = None
    description: str | None = None
    nearestStationId: str | None = None
    unitId: str | None = None


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


def _get_reporter_name(db: Any, reporter_id: str | None) -> str:
    """Try to resolve a reporter UUID to a user's name; fall back to the raw value."""
    if not reporter_id:
        return "Unknown"
    try:
        user_doc = db.collection("Users").document(reporter_id).get()
        if user_doc.exists:
            data = user_doc.to_dict() or {}
            first = data.get("firstName", "")
            last = data.get("lastName", "")
            if first or last:
                return f"{first} {last}".strip()
            if data.get("name"):
                return data["name"]
    except Exception:
        pass
    # ActiveCalls.reporterUuid isn't always a real Users doc id (e.g. test data),
    # so fall back to showing the raw value instead of "Unknown".
    return reporter_id


def _get_unit_name(db: Any, unit_id: str | None) -> str | None:
    """Resolve an assigned unit id to its display name, for report detail views."""
    if not unit_id:
        return None
    try:
        unit_doc = db.collection("Units").document(unit_id).get()
        if unit_doc.exists:
            return (unit_doc.to_dict() or {}).get("unitName")
    except Exception:
        pass
    return None


def _build_report_payload(doc_id: str, data: dict[str, Any], db: Any) -> dict[str, Any]:
    created_dt = _to_dt(data.get("timestamp") or data.get("createdAt"))
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    location_str = _location_to_string(data.get("location"))

    assigned_station = data.get("assignedStation") or {}
    # unitId/unitName live inside assignedStation now. Fall back to the old
    # top-level "unitId" field for docs written before this change.
    unit_id = assigned_station.get("unitId") or data.get("unitId")
    incident_type = (
        data.get("incidentType")
        or data.get("channelName")
        or "Unknown"
    )
    description = (
        data.get("description")
        or data.get("ai_assessment")
        or f"{incident_type} reported at {location_str}."
    )

    return {
        "id": doc_id,
        "type": incident_type,
        "location": location_str,
        "time": _format_time(created_dt),
        "urgency": _normalize_level(data.get("level")),
        "reporter": _get_reporter_name(db, data.get("reporterUuid") or data.get("reportedBy")),
        "description": description,
        "lat": data.get("lat") or location.get("latitude") or location.get("lat"),
        "lng": data.get("lng") or location.get("longitude") or location.get("lng"),
        "date": _history_label(created_dt),
        "status": data.get("status", "pending"),
        "nearestStationId": data.get("nearestStationId") or assigned_station.get("stationId", ""),
        # Alias of nearestStationId under the name the frontend's unit-assignment
        # dropdown (report.js) actually reads, so a report can be filtered down
        # to the units that belong to its own station.
        "stationId": data.get("nearestStationId") or assigned_station.get("stationId", ""),
        # Kept flat too for any older frontend code paths reading report.unitId
        # directly; the nested assignedStation.unitId below is the source of truth.
        "unitId": unit_id,
        "unitName": _get_unit_name(db, unit_id),
        "assignedStation": {
            **assigned_station,
            "unitId": unit_id,
            "unitName": _get_unit_name(db, unit_id),
        },
        "streamChannelId": data.get("streamChannelId") or data.get("channelName", ""),
    }


@router.get("/recent")
def get_recent_reports():
    """Frontend dashboard page: recent incidents list. Returns ALL reports,
    most recent first."""
    db = get_db()
    docs = list(db.collection("ActiveCalls").stream())

    # Sort by the actual timestamp rather than the formatted "time" string
    # (the old version sorted strings like "5m ago" vs "2h ago" vs "Jul 19",
    # which doesn't sort chronologically). Docs with no timestamp sort last.
    def _sort_key(doc):
        data = doc.to_dict() or {}
        created_dt = _to_dt(data.get("timestamp") or data.get("createdAt"))
        return created_dt or datetime.min.replace(tzinfo=timezone.utc)

    docs.sort(key=_sort_key, reverse=True)

    reports = [_build_report_payload(doc.id, doc.to_dict() or {}, db) for doc in docs]
    return {"reports": reports}


@router.get("/{report_id}")
def get_report_detail(report_id: str):
    """Frontend report detail page."""
    db = get_db()
    doc = db.collection("ActiveCalls").document(report_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Report not found")
    return _build_report_payload(doc.id, doc.to_dict() or {}, db)


@router.patch("/{report_id}")
def update_report(report_id: str, payload: ReportUpdate):
    db = get_db()
    ref = db.collection("ActiveCalls").document(report_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Report not found")

    data = payload.model_dump(exclude_unset=True)
    existing_data = doc.to_dict() or {}

    # Assigning a report to a unit — validate the unit actually exists and
    # belongs to the same station this report is assigned to. Without this,
    # a stale/typo'd unitId would silently attach and the frontend dropdown
    # would just show it as unassigned again on next load (since it filters
    # units by stationId and would never find a match). Unassigning (unitId
    # explicitly set to null) skips this check entirely.
    if "unitId" in data:
        unit_id = data.pop("unitId")

        if unit_id:
            unit_doc = db.collection("Units").document(unit_id).get()
            if not unit_doc.exists:
                raise HTTPException(status_code=404, detail=f"Unit '{unit_id}' not found")

            unit_data = unit_doc.to_dict() or {}
            report_station_id = data.get("nearestStationId") or existing_data.get("nearestStationId")

            if report_station_id and unit_data.get("stationId") != report_station_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unit '{unit_id}' belongs to a different station than this report is assigned to.",
                )

        # unitId lives inside the assignedStation map, not as a top-level
        # field. Read-merge-write against the live doc (rather than trusting
        # a client-supplied assignedStation object) so we never clobber
        # sibling fields like stationName/commanderName/dispatchStatus with
        # a stale copy.
        assigned_station = dict(existing_data.get("assignedStation") or {})
        assigned_station["unitId"] = unit_id
        data["assignedStation"] = assigned_station

        # Clean up the legacy top-level "unitId" field left over from before
        # this field moved into assignedStation, so old docs stop showing it
        # in both places once they're touched again.
        if "unitId" in existing_data:
            data["unitId"] = firestore.DELETE_FIELD

    if data:
        ref.update(data)
    return {"updated": True, "reportId": report_id}