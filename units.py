from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from firebase import get_db

router = APIRouter(prefix="/units", tags=["Units"])


class UnitCreate(BaseModel):
    stationId: str
    unitName: str
    status: str = "on"


class UnitUpdate(BaseModel):
    stationId: str | None = None
    unitName: str | None = None
    status: str | None = None


def _serialize_unit(doc) -> dict:
    data = doc.to_dict() or {}
    return {
        "id": doc.id,
        "stationId": data.get("stationId", ""),
        "unitName": data.get("unitName", ""),
        "status": data.get("status", "on"),
    }


@router.get("")
def list_units():
    """Frontend units page: all units (callsigns/teams), client-filtered by stationId."""
    db = get_db()
    docs = list(db.collection("Units").stream())
    units = [_serialize_unit(doc) for doc in docs]
    return {"units": units}


@router.post("", status_code=201)
def create_unit(payload: UnitCreate):
    db = get_db()

    # Units live under a station — fail fast with a clear error instead of
    # silently writing a unit that references a station that doesn't exist,
    # which would otherwise show up as a "phantom" unit nothing can reach.
    station_ref = db.collection("Stations").document(payload.stationId)
    if not station_ref.get().exists:
        raise HTTPException(status_code=404, detail=f"Station '{payload.stationId}' not found")

    unit_data = {
        "stationId": payload.stationId,
        "unitName": payload.unitName,
        "status": payload.status,
    }
    update_time, doc_ref = db.collection("Units").add(unit_data)
    return {"id": doc_ref.id, **unit_data}


@router.patch("/{unit_id}")
def update_unit(unit_id: str, payload: UnitUpdate):
    db = get_db()
    ref = db.collection("Units").document(unit_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Unit not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "stationId" in update_data:
        station_ref = db.collection("Stations").document(update_data["stationId"])
        if not station_ref.get().exists:
            raise HTTPException(status_code=404, detail=f"Station '{update_data['stationId']}' not found")

    if update_data:
        ref.update(update_data)

    return {"updated": True, "unitId": unit_id}


@router.delete("/{unit_id}")
def delete_unit(unit_id: str):
    db = get_db()
    ref = db.collection("Units").document(unit_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="Unit not found")

    # Deleting a unit doesn't cascade to the Responders assigned to it —
    # their unitId would just point at a doc that no longer exists. The
    # frontend's Accounts/Units pages both look unit names up by id and
    # already handle a miss by falling back to "Unassigned" labels, so this
    # is a soft failure rather than a broken reference, but it's worth
    # knowing about if a proper cascade-unassign is wanted later.
    ref.delete()
    return {"deleted": True, "unitId": unit_id}