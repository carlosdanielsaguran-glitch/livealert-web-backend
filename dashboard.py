from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from math import radians, sin, cos, sqrt, atan2
from firebase import get_db

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


class AccountCreate(BaseModel):
    name: str
    role: str
    group: str = "Alpha 1"
    status: str = "on"


class AccountUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    group: str | None = None
    status: str | None = None


class ReportUpdate(BaseModel):
    status: str | None = None
    level: str | None = None
    incidentType: str | None = None
    description: str | None = None
    nearestStationId: str | None = None


def _to_dt(firestore_ts):
    if firestore_ts is None:
        return None
    if hasattr(firestore_ts, "ToDatetime"):
        return firestore_ts.ToDatetime(tzinfo=timezone.utc)
    if isinstance(firestore_ts, datetime):
        return firestore_ts if firestore_ts.tzinfo else firestore_ts.replace(tzinfo=timezone.utc)
    return None


def _haversine(lat1, lng1, lat2, lng2) -> float:
    """Calculate distance in km between two coordinates."""
    R = 6371
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


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


def _active_call_lat_lng(location: Any) -> tuple[Any, Any]:
    if isinstance(location, dict):
        lat = location.get("latitude", location.get("lat"))
        lng = location.get("longitude", location.get("lng"))
        return lat, lng
    if hasattr(location, "latitude"):
        return location.latitude, location.longitude
    return None, None


def _build_report_payload(doc_id: str, data: dict[str, Any], db: Any) -> dict[str, Any]:
    """Builds the shared report shape from an ActiveCalls document."""
    created_dt = _to_dt(data.get("timestamp") or data.get("createdAt"))
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    location_str = _location_to_string(data.get("location"))

    assigned_station = data.get("assignedStation") or {}
    incident_type = data.get("incidentType") or data.get("channelName") or "Unknown"
    description = (
        data.get("description")
        or data.get("ai_assessment")
        or f"{incident_type} reported at {location_str}."
    )
    lat, lng = _active_call_lat_lng(location)

    return {
        "id": doc_id,
        "type": incident_type,
        "location": location_str,
        "time": _format_time(created_dt),
        "urgency": _normalize_level(data.get("level")),
        "reporter": _get_reporter_name(db, data.get("reporterUuid") or data.get("reportedBy")),
        "description": description,
        "lat": data.get("lat") or lat,
        "lng": data.get("lng") or lng,
        "date": _history_label(created_dt),
        "status": data.get("status", "pending"),
        "nearestStationId": data.get("nearestStationId") or assigned_station.get("stationId", ""),
        "streamChannelId": data.get("streamChannelId") or data.get("channelName", ""),
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats():
    """
    Returns all 4 dashboard stat cards:
      todayReports, unitsAvailable, unitsDeployed, reportedAreas, recentReports
    """
    db = get_db()
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    call_docs = list(db.collection("ActiveCalls").stream())

    today_reports = 0
    reported_station_ids = set()
    recent_reports = []

    for doc in call_docs:
        data = doc.to_dict()
        created_dt = _to_dt(data.get("timestamp") or data.get("createdAt"))

        if created_dt and created_dt >= today_start:
            today_reports += 1

        assigned_station = data.get("assignedStation") or {}
        sid = (data.get("nearestStationId") or assigned_station.get("stationId", "")).strip()
        if sid and data.get("status") not in ("resolved", "closed", "completed", "cancelled"):
            reported_station_ids.add(sid)

        recent_reports.append({
            "id": doc.id,
            "incidentType": data.get("incidentType") or data.get("channelName") or "Unknown",
            "nearestStationId": data.get("nearestStationId") or assigned_station.get("stationId", ""),
            "status": data.get("status", "pending"),
            "createdAt": created_dt.isoformat() if created_dt else None,
        })

    recent_reports.sort(key=lambda r: r["createdAt"] or "", reverse=True)
    recent_reports = recent_reports[:10]

    responder_docs = list(db.collection("Responders").stream())
    units_available = sum(1 for d in responder_docs if d.to_dict().get("status") == "available")
    units_deployed  = sum(1 for d in responder_docs if d.to_dict().get("status") == "deployed")

    return {
        "todayReports": today_reports,
        "unitsAvailable": units_available,
        "unitsDeployed": units_deployed,
        "reportedAreas": len(reported_station_ids),
        "recentReports": recent_reports,
    }


# ── Active Emergencies ────────────────────────────────────────────────────────

@router.get("/active")
def get_active():
    """Left panel — active calls. Called directly by the frontend's
    getActiveEmergencies(), which is what report.js polls to find the next
    incident to open. Note: since the ActiveCalls collection only ever
    contains calls that are currently in progress, we don't filter by a
    "pending"/"active" status the way the old Emergencies-based version did
    (ActiveCalls documents use their own status values, e.g. "evaluating").
    We just exclude anything explicitly marked as finished."""
    db = get_db()
    docs = list(db.collection("ActiveCalls").stream())

    active = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("status") in ("resolved", "closed", "completed", "cancelled"):
            continue
        assigned_station = data.get("assignedStation") or {}
        created_dt = _to_dt(data.get("timestamp") or data.get("createdAt"))
        active.append({
            "id": doc.id,
            "incidentType": data.get("incidentType") or data.get("channelName") or "Unknown",
            "nearestStationId": data.get("nearestStationId") or assigned_station.get("stationId", ""),
            "reportedBy": data.get("reporterUuid") or data.get("reportedBy", ""),
            "respondersInvolved": data.get("respondersInvolved", []),
            "status": data.get("status", "pending"),
            "streamChannelId": data.get("streamChannelId") or data.get("channelName", ""),
            "createdAt": created_dt,
        })

    # Previously this returned whatever order Firestore happened to stream
    # documents in — which is arbitrary, not chronological. The frontend
    # (report.js, dashboard.js) assumes index 0 is the most recent active
    # call, so an old/stale doc could keep "winning" indefinitely just by
    # virtue of Firestore's internal ordering, regardless of what's actually
    # newest. Sort by real timestamp, newest first, so that assumption holds.
    active.sort(key=lambda item: item["createdAt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return {"active": active, "count": len(active)}


# ── Recent Reports ────────────────────────────────────────────────────────────

@router.get("/recent")
def get_recent(limit: int = 10):
    """Right panel — most recent reports sorted by createdAt."""
    db = get_db()
    docs = list(
        db.collection("ActiveCalls")
        .order_by("timestamp", direction="DESCENDING")
        .limit(limit)
        .stream()
    )

    reports = []
    for doc in docs:
        data = doc.to_dict()
        created_dt = _to_dt(data.get("timestamp") or data.get("createdAt"))
        assigned_station = data.get("assignedStation") or {}
        reports.append({
            "id": doc.id,
            "incidentType": data.get("incidentType") or data.get("channelName") or "Unknown",
            "nearestStationId": data.get("nearestStationId") or assigned_station.get("stationId", ""),
            "status": data.get("status", "pending"),
            "createdAt": created_dt.isoformat() if created_dt else None,
        })

    return {"reports": reports}

# NOTE: this file used to also define /dashboard/reports/recent,
# /dashboard/reports/{report_id}, /dashboard/history, and a PATCH on
# /dashboard/reports/{report_id}. Those were dead code left over from before
# report.py and history.py existed as their own routers (this router's prefix
# is "/dashboard", so they never actually collided with /reports/... or
# /history, they just duplicated stale Emergencies-based logic that nothing
# called). Removed here — use the routes in report.py / history.py instead,
# which already read from ActiveCalls.


@router.get("/accounts")
def list_accounts():
    """Frontend accounts page: operational unit roster."""
    db = get_db()
    docs = list(db.collection("Responders").stream())

    accounts = []
    for doc in docs:
        data = doc.to_dict() or {}
        status = data.get("status", "available")
        if status in ("available", "active"):
            normalized_status = "on"
        elif status == "deployed":
            normalized_status = "off"
        else:
            normalized_status = "inactive"

        accounts.append({
            "id": doc.id,
            "name": data.get("name") or f"{data.get('firstName', '')} {data.get('lastName', '')}".strip() or "Unknown",
            "role": data.get("role") or data.get("position") or "Firefighter",
            "group": data.get("group") or data.get("station") or "Alpha 1",
            "status": normalized_status,
        })

    return {"accounts": accounts[:20]}


@router.post("/accounts", status_code=201)
def create_account(payload: AccountCreate):
    db = get_db()
    account_id = str(uuid4())
    account_data = {
        "name": payload.name,
        "role": payload.role,
        "group": payload.group,
        "status": payload.status,
    }
    db.collection("Responders").document(account_id).set(account_data)
    return {"id": account_id, **account_data}


@router.patch("/accounts/{account_id}")
def update_account(account_id: str, payload: AccountUpdate):
    db = get_db()
    ref = db.collection("Responders").document(account_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Account not found")
    data = payload.model_dump(exclude_unset=True)
    if data:
        ref.update(data)
    return {"updated": True, "accountId": account_id}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str):
    db = get_db()
    ref = db.collection("Responders").document(account_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Account not found")
    ref.delete()
    return {"deleted": True, "accountId": account_id}


# ── Modify Nearest Station Modal ──────────────────────────────────────────────

@router.get("/stations/{emergency_id}")
def get_stations_for_emergency(emergency_id: str):
    """
    Returns all stations with their distance from the emergency location.
    Used to populate the 'Modify Nearest Station' modal map.
    """
    db = get_db()

    # Get the active call
    doc = db.collection("ActiveCalls").document(emergency_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Active call not found")

    data = doc.to_dict()
    assigned_station = data.get("assignedStation") or {}
    current_nearest = data.get("nearestStationId") or assigned_station.get("stationId", "")
    emergency_location = data.get("location")

    # Get coordinates if available (ActiveCalls stores latitude/longitude)
    e_lat, e_lng = None, None
    if emergency_location:
        if hasattr(emergency_location, "latitude"):
            e_lat, e_lng = emergency_location.latitude, emergency_location.longitude
        elif isinstance(emergency_location, dict):
            e_lat = emergency_location.get("latitude", emergency_location.get("lat"))
            e_lng = emergency_location.get("longitude", emergency_location.get("lng"))

    # Get all stations
    station_docs = list(db.collection("Stations").stream())
    stations = []

    for s in station_docs:
        sd = s.to_dict()
        loc = sd.get("location")

        s_lat, s_lng = None, None
        if loc:
            if hasattr(loc, "latitude"):
                s_lat, s_lng = loc.latitude, loc.longitude
            elif isinstance(loc, dict):
                s_lat = loc.get("lat")
                s_lng = loc.get("lng")

        # Calculate distance if both coordinates available
        distance_km = None
        if e_lat and e_lng and s_lat and s_lng:
            distance_km = round(_haversine(e_lat, e_lng, s_lat, s_lng), 1)

        stations.append({
            "id": s.id,
            "stationName": sd.get("stationName", "Unknown"),
            "chiefName": sd.get("chiefName", ""),
            "lat": s_lat,
            "lng": s_lng,
            "distanceKm": distance_km,
            "isNearest": s.id == current_nearest,
        })

    # Sort by distance
    stations.sort(key=lambda x: x["distanceKm"] if x["distanceKm"] is not None else 9999)

    return {
        "emergencyId": emergency_id,
        "currentNearestStationId": current_nearest,
        "stations": stations,
    }


class UpdateNearestStation(BaseModel):
    nearestStationId: str


@router.patch("/stations/{emergency_id}")
def update_nearest_station(emergency_id: str, body: UpdateNearestStation):
    """
    Updates the nearest station for an active call.
    Called when user confirms a new station in the modal.
    """
    db = get_db()

    ref = db.collection("ActiveCalls").document(emergency_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Active call not found")

    # Verify the station exists
    station_ref = db.collection("Stations").document(body.nearestStationId)
    station_doc = station_ref.get()
    if not station_doc.exists:
        raise HTTPException(status_code=404, detail="Station not found")

    station_data = station_doc.to_dict() or {}

    # Write both the flat field (used as a fallback by report payload builders)
    # and update the nested assignedStation, which is ActiveCalls' native shape.
    existing = doc.to_dict() or {}
    assigned_station = dict(existing.get("assignedStation") or {})
    assigned_station["stationId"] = body.nearestStationId
    assigned_station["stationName"] = station_data.get("stationName", "Unknown")
    assigned_station["commanderName"] = station_data.get("chiefName", assigned_station.get("commanderName", ""))

    ref.update({
        "nearestStationId": body.nearestStationId,
        "assignedStation": assigned_station,
    })

    return {
        "emergencyId": emergency_id,
        "updatedNearestStationId": body.nearestStationId,
        "stationName": station_data.get("stationName", "Unknown"),
    }