"""
LiveAlert — Frontend Compatibility API
======================================
Provides JSON endpoints that match the structure expected by the existing
frontend pages for dashboard, history, accounts, and report views.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from firebase import get_db

router = APIRouter(tags=["Frontend"])


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
    if days < 7:
        return created_dt.strftime("%B %d")
    return created_dt.strftime("%B %d")


def _get_reporter_name(db: Any, reporter_id: str | None) -> str:
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
            return data.get("name", "Unknown")
    except Exception:
        return "Unknown"
    return "Unknown"


def _build_report_payload(doc_id: str, data: dict[str, Any], db: Any) -> dict[str, Any]:
    created_dt = _to_dt(data.get("createdAt"))
    location = _location_to_string(data.get("location"))
    return {
        "id": doc_id,
        "type": data.get("incidentType", "Unknown"),
        "location": location,
        "time": _format_time(created_dt),
        "urgency": _normalize_level(data.get("level")),
        "reporter": _get_reporter_name(db, data.get("reportedBy")),
        "description": data.get("description") or f"{data.get('incidentType', 'Incident')} reported at {location}.",
        "lat": data.get("lat") or (data.get("location", {}).get("lat") if isinstance(data.get("location"), dict) else None),
        "lng": data.get("lng") or (data.get("location", {}).get("lng") if isinstance(data.get("location"), dict) else None),
        "date": _history_label(created_dt),
        "status": data.get("status", "pending"),
    }


def _get_db_client() -> Any:
    try:
        return get_db()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Firebase unavailable") from exc


def _get_recent_reports_from_firestore() -> list[dict[str, Any]]:
    db = _get_db_client()
    docs = list(db.collection("Emergencies").stream())

    reports = []
    for doc in docs:
        payload = _build_report_payload(doc.id, doc.to_dict() or {}, db)
        reports.append(payload)

    reports.sort(key=lambda item: item.get("time", ""), reverse=True)
    return reports[:6]


def _get_history_reports_from_firestore() -> list[dict[str, Any]]:
    db = _get_db_client()
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

    return reports[:10]


def _get_accounts_from_firestore() -> list[dict[str, Any]]:
    db = _get_db_client()
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

    return accounts[:20]


@router.get("/reports/recent")
def get_recent_reports():
    return {"reports": _get_recent_reports_from_firestore()}


@router.get("/reports/history")
def get_history_reports():
    return {"reports": _get_history_reports_from_firestore()}


@router.get("/reports/{report_id}")
def get_report_detail(report_id: str):
    db = _get_db_client()
    doc = db.collection("Emergencies").document(report_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Report not found")
    return _build_report_payload(doc.id, doc.to_dict() or {}, db)


@router.patch("/reports/{report_id}")
def update_report(report_id: str, payload: ReportUpdate):
    db = _get_db_client()
    ref = db.collection("Emergencies").document(report_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Report not found")
    update_data = payload.model_dump(exclude_unset=True)
    if update_data:
        ref.update(update_data)
    return {"updated": True, "reportId": report_id}


@router.get("/accounts")
def list_accounts():
    return {"accounts": _get_accounts_from_firestore()}


@router.post("/accounts", status_code=201)
def create_account(payload: AccountCreate):
    db = _get_db_client()
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
    db = _get_db_client()
    ref = db.collection("Responders").document(account_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Account not found")
    update_data = payload.model_dump(exclude_unset=True)
    if update_data:
        ref.update(update_data)
    return {"updated": True, "accountId": account_id}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str):
    db = _get_db_client()
    ref = db.collection("Responders").document(account_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Account not found")
    ref.delete()
    return {"deleted": True, "accountId": account_id}
