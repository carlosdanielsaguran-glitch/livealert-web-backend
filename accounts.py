from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from firebase import get_db

router = APIRouter(prefix="/accounts", tags=["Accounts"])

# ---------------------------------------------------------------------------
# Page permissions
#
# Rule: only true Super Admins (see FULL_ACCESS_ROLES below) get every page.
# Everyone else — Responders and any account made through the Accounts page
# regardless of role — gets exactly dashboard, report, history, units.
#
# Identity: this project doesn't verify a signed token on every request, so
# the caller is identified the same way the rest of this file already treats
# accounts — by Firestore doc id. The frontend sends that id back on every
# request as the X-Account-Id header (set right after login). That's fine
# for gating which *pages* someone sees, but it's UX, not real security —
# anyone who can set a request header can claim to be any account id. Put a
# real auth check (signed token / session cookie) in front of this once one
# exists.
# ---------------------------------------------------------------------------

# A role missing from this dict has full access (see get_allowed_pages).
# ---------------------------------------------------------------------------
# Rule: the ONLY unrestricted accounts are true Super Admins — ones created
# through the login page's "Create Admin Account" form (auth.js's
# handleRegisterRequest, which tags the resulting doc role: "super_admin"
# right after signup — see that file).
#
# Everything else gets the same four pages, no matter what role string was
# typed into the Accounts page's role dropdown when the account was made
# there (Responder, Admin, Dispatcher, whatever) — an account created
# through accounts.py/accounts.html is never a Super Admin, full stop.
# That's a default-DENY: a missing/blank role is restricted too, not
# unrestricted.
# ---------------------------------------------------------------------------
FULL_ACCESS_ROLES = {"super_admin"}
RESTRICTED_PAGES = {"dashboard", "report", "history", "units"}


def get_allowed_pages(role: str) -> set[str] | None:
    """None means unrestricted (super_admin only); otherwise the fixed allow-list."""
    normalized = (role or "").strip().lower()
    if normalized in FULL_ACCESS_ROLES:
        return None
    return RESTRICTED_PAGES


def get_current_account(x_account_id: str = Header(None, alias="X-Account-Id")) -> dict:
    """
    Resolves the calling account from the X-Account-Id header, which the
    frontend sets to the id it got back from login/create_account.

    Raises 401 if the header is missing, 404 if no account with that id
    exists in either collection.
    """
    if not x_account_id:
        raise HTTPException(status_code=401, detail="Missing X-Account-Id header")

    db = get_db()
    for collection in (RESPONDERS, USERS):
        snapshot = db.collection(collection).document(x_account_id).get()
        if snapshot.exists:
            data = snapshot.to_dict() or {}
            return {
                "id": x_account_id,
                "collection": collection,
                "role": data.get("role") or data.get("position") or "Responder",
                "status": data.get("status"),
            }

    raise HTTPException(status_code=404, detail="Account not found")


def require_page(page: str):
    """
    Dependency factory for guarding routes in OTHER routers, e.g. in your
    units router:

        from accounts import require_page

        @router.get("/units", dependencies=[Depends(require_page("units"))])
        def list_units():
            ...

    Raises 403 if the caller's role isn't allowed on that page.
    """

    def _check(account: dict = Depends(get_current_account)) -> dict:
        allowed = get_allowed_pages(account["role"])
        if allowed is not None and page not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Your role does not have access to '{page}'",
            )
        return account

    return _check


# ---------------------------------------------------------------------------
# Collection routing
#
#   role == "Responder"  -> Responders collection ONLY
#   any other role       -> Users collection ONLY
#
# Responders is the operational roster (people who work shifts and can be
# dispatched). Users is everyone else — admins, dispatchers, station chiefs.
# An account lives in exactly one of the two, never both, so there is a single
# source of truth per account and no pair of docs to drift apart.
#
# Two independent fields, do not conflate them:
#   status -> "on" / "off" / "inactive": account status, as before.
#   duty   -> "on_duty" / "off_duty": is this responder CURRENTLY on shift?
#             Only meaningful for responders.
# ---------------------------------------------------------------------------

RESPONDERS = "Responders"
USERS = "Users"

# Same default auth.py already applies when signup() is called with no
# password (see the comment in accounts.js about DEFAULT_RESPONDER_PASSWORD).
# Kept here too so /accounts/{id}/reset-password can put an account back to
# the same starting point without needing to import from auth.py.
DEFAULT_RESPONDER_PASSWORD = "default123"

try:
    from firebase_admin import auth as firebase_auth
except ImportError:  # pragma: no cover - firebase_admin should already be a
    firebase_auth = None  # project dependency; this only guards import order


class AccountCreate(BaseModel):
    id: str | None = None  # if provided, reuses this doc id (e.g. a Firebase Auth UID from /auth/signup) instead of generating a new one — see create_account() below
    firstName: str = ""
    lastName: str = ""
    badge: str = ""
    email: str = ""
    role: str = ""
    stationId: str = ""
    status: str = ""            # "on" / "off" / "inactive", as before
    duty: str = "off_duty"      # shift state; new accounts start off shift
    unitId: str | None = None
    is_new: bool = True


class AccountUpdate(BaseModel):
    firstName: str | None = None
    lastName: str | None = None
    badge: str | None = None
    email: str | None = None
    role: str | None = None
    stationId: str | None = None
    status: str | None = None
    duty: str | None = None
    unitId: str | None = None
    is_new: bool | None = None


class DutyUpdate(BaseModel):
    duty: str | None = None  # omit to flip whatever is currently stored


def _is_responder(role: str) -> bool:
    """A blank role defaults to Responder, matching the list endpoint's fallback."""
    return (role or "Responder").strip().lower() == "responder"


def _collection_for_role(role: str) -> str:
    return RESPONDERS if _is_responder(role) else USERS


def _normalize_status(raw_status: str) -> str:
    """
    Normalizes a responder's stored status into one of "on" / "off" / "inactive".

    Kept exactly as it was before duty was introduced: the Accounts admin UI
    writes "on" / "off" / "inactive" directly (from its status <select>).
    Other write paths (e.g. a mobile self-check-in flow) may still use
    "available" / "active" / "deployed" / "standby", so those are mapped for
    backward compatibility. This is unrelated to duty — duty tracks whether a
    responder is currently on shift; status is the account state.
    """
    value = (raw_status or "available").strip().lower()
    if value in ("on", "active", "available"):
        return "on"
    if value in ("off", "deployed", "standby"):
        return "off"
    return "inactive"


def _normalize_duty(raw_duty) -> str:
    """
    Normalizes a responder's shift state into "on_duty" / "off_duty".

    Firestore stores "on_duty" / "off_duty" directly (matching the sample
    doc), but shorthand from other write paths is tolerated.
    """
    if isinstance(raw_duty, bool):
        return "on_duty" if raw_duty else "off_duty"
    value = str(raw_duty or "off_duty").strip().lower()
    if value in ("on_duty", "onduty", "on", "duty", "deployed", "true", "1"):
        return "on_duty"
    return "off_duty"


def _serialize(doc_id: str, data: dict, collection: str) -> dict:
    """Shared response shape, used for docs from either collection."""
    is_responder = collection == RESPONDERS

    account = {
        "id": doc_id,
        "collection": collection,
        "firstName": data.get("firstName", ""),
        "lastName": data.get("lastName", ""),
        "name": f"{data.get('firstName', '')} {data.get('lastName', '')}".strip() or data.get("name") or "Unknown",
        "badge": data.get("badge", ""),
        "email": data.get("email", ""),
        "role": data.get("role") or data.get("position") or "Responder",
        "stationId": data.get("stationId", ""),
        "unitId": data.get("unitId") or "",
        "status": _normalize_status(data.get("status")),
        "is_new": data.get("is_new", False),
    }

    # duty is a responder-only concept; non-responders report null so the UI
    # can render a dash instead of a misleading "Off duty" badge.
    account["duty"] = _normalize_duty(data.get("duty")) if is_responder else None
    account["dutyChangedAt"] = data.get("dutyChangedAt") if is_responder else None

    return account


def _find_account(db, account_id: str):
    """
    Locates an account in whichever collection holds it.

    Returns (doc_ref, snapshot, collection_name). Responders is checked first
    since it is the hot path for duty toggles.
    """
    for collection in (RESPONDERS, USERS):
        ref = db.collection(collection).document(account_id)
        snapshot = ref.get()
        if snapshot.exists:
            return ref, snapshot, collection
    raise HTTPException(status_code=404, detail="Account not found")


@router.get("/me")
def get_me(account: dict = Depends(get_current_account)):
    """
    Full profile for the logged-in caller, resolved from the X-Account-Id
    header. Frontend calls this right after login to know who it's talking to.
    """
    db = get_db()
    ref = db.collection(account["collection"]).document(account["id"])
    data = ref.get().to_dict() or {}
    return _serialize(account["id"], data, account["collection"])


@router.get("/me/permissions")
def get_my_permissions(account: dict = Depends(get_current_account)):
    """
    Which pages the logged-in caller may see. `pages: null` means
    unrestricted (any page); otherwise it's the exact allow-list — e.g.
    Responders get exactly ["dashboard", "report", "history", "units"].

    Frontend calls this after login and hides any nav item not in the list.
    The backend still enforces this on each route via require_page(), so
    hiding the nav link is a convenience, not the actual security boundary.
    """
    allowed = get_allowed_pages(account["role"])
    return {
        "role": account["role"],
        "pages": sorted(allowed) if allowed is not None else None,
    }


@router.get("")
def list_accounts(duty: str | None = None, status: str | None = None, role: str | None = None):
    """
    Frontend accounts page: the full roster, merged from both collections.

    Optional filters, normalized the same way as the stored values so
    ?duty=on and ?duty=on_duty behave identically:
      /accounts?duty=on_duty      -> responders currently on shift
      /accounts?status=on         -> only accounts with status "on"
      /accounts?role=Responder    -> Responders collection only
    """
    db = get_db()

    duty_filter = _normalize_duty(duty) if duty else None
    status_filter = _normalize_status(status) if status else None
    role_filter = role.strip().lower() if role else None

    accounts = []
    seen: set[str] = set()

    for collection in (RESPONDERS, USERS):
        for doc in db.collection(collection).stream():
            # A doc id could exist in both collections if an older build wrote
            # to both, or mid-migration. Responders is streamed first and wins.
            if doc.id in seen:
                continue
            seen.add(doc.id)

            account = _serialize(doc.id, doc.to_dict() or {}, collection)

            if role_filter and account["role"].strip().lower() != role_filter:
                continue
            if status_filter and account["status"] != status_filter:
                continue
            # A duty filter only ever matches responders, since duty is None
            # for everyone else.
            if duty_filter and account["duty"] != duty_filter:
                continue

            accounts.append(account)

    responders = [a for a in accounts if a["collection"] == RESPONDERS]
    on_duty = [a for a in responders if a["duty"] == "on_duty"]

    return {
        "accounts": accounts,
        "counts": {
            "total": len(accounts),
            "responders": len(responders),
            "users": len(accounts) - len(responders),
            "onDuty": len(on_duty),
            "offDuty": len(responders) - len(on_duty),
        },
    }


@router.post("", status_code=201)
def create_account(payload: AccountCreate):
    db = get_db()
    # If this account was created via /auth/signup first (the normal flow from
    # the Accounts page — see accounts.js), payload.id is that Firebase Auth
    # UID, and signup() has already written a doc under it (with an older,
    # different field shape). Reusing that same id here means the write below
    # correctly fills in the real profile schema, rather than creating a
    # second, disconnected record that isn't tied to any login credential.
    account_id = payload.id or str(uuid4())

    collection = _collection_for_role(payload.role)
    is_responder = collection == RESPONDERS

    account_data = {
        "firstName": payload.firstName,
        "lastName": payload.lastName,
        "badge": payload.badge,
        "email": payload.email,
        "role": payload.role or "Responder",
        "stationId": payload.stationId,
        "status": _normalize_status(payload.status),
        "unitId": payload.unitId,
        "is_new": payload.is_new,
    }

    if is_responder:
        account_data["duty"] = _normalize_duty(payload.duty)
        account_data["dutyChangedAt"] = datetime.now(timezone.utc)

    # merge=True (not a plain .set()) so this doesn't wipe out the "username"
    # field that /auth/signup already wrote for this same doc — login-by-
    # username depends on that field still being present.
    db.collection(collection).document(account_id).set({
        **account_data,
        "createdAt": datetime.now(timezone.utc),
    }, merge=True)

    # signup() writes before it knows the role, so the other collection may be
    # left holding a stray half-populated doc. Clear it, so the
    # one-account-one-collection rule actually holds.
    other = USERS if is_responder else RESPONDERS
    other_ref = db.collection(other).document(account_id)
    if other_ref.get().exists:
        other_ref.delete()

    return {"id": account_id, "collection": collection, **account_data}


@router.patch("/{account_id}")
def update_account(account_id: str, payload: AccountUpdate):
    db = get_db()
    ref, snapshot, collection = _find_account(db, account_id)
    existing = snapshot.to_dict() or {}

    data = payload.model_dump(exclude_unset=True)

    # Normalize before writing so an admin UI sending "On Duty" or a mobile
    # client sending "on" can't put an unrecognized string into Firestore.
    if "status" in data:
        data["status"] = _normalize_status(data["status"])
    if "duty" in data:
        data["duty"] = _normalize_duty(data["duty"])
        data["dutyChangedAt"] = datetime.now(timezone.utc)

    if not data:
        return {"updated": True, "accountId": account_id, "collection": collection}

    # Changing the role can move the account between collections. Copy the
    # full merged doc across, then drop the original, so nothing is lost and
    # the account never exists in two places at once.
    target = _collection_for_role(data["role"]) if "role" in data else collection

    if target != collection:
        merged = {**existing, **data}
        if target == RESPONDERS:
            merged.setdefault("duty", "off_duty")
            merged.setdefault("dutyChangedAt", datetime.now(timezone.utc))
        else:
            # Leaving the operational roster ends any open shift.
            merged.pop("duty", None)
            merged.pop("dutyChangedAt", None)

        db.collection(target).document(account_id).set(merged, merge=True)
        ref.delete()
        return {
            "updated": True,
            "accountId": account_id,
            "collection": target,
            "movedFrom": collection,
        }

    # Non-responders have no duty field; ignore it rather than writing a
    # meaningless one.
    if collection == USERS:
        data.pop("duty", None)
        data.pop("dutyChangedAt", None)

    # The doc is guaranteed to exist (_find_account raised otherwise), but
    # set(..., merge=True) is still preferred over .update(): update() throws
    # google.api_core.exceptions.NotFound if the doc disappears between the
    # read and the write, which previously bubbled up as an unhandled 500 and
    # produced the "changed in Firestore but the frontend looks stale" symptom.
    ref.set(data, merge=True)

    return {"updated": True, "accountId": account_id, "collection": collection, **data}


@router.patch("/{account_id}/duty")
def set_duty(account_id: str, payload: DutyUpdate):
    """
    Shift toggle. Send {"duty": "on_duty"} / {"duty": "off_duty"} to set an
    explicit state, or an empty body to flip whatever is currently stored.

    Responders only. A deactivated account cannot go on duty — that is the
    one place where status and duty interact.
    """
    db = get_db()
    ref, snapshot, collection = _find_account(db, account_id)

    if collection != RESPONDERS:
        raise HTTPException(status_code=409, detail="Only responders have a duty state")

    existing = snapshot.to_dict() or {}
    current_duty = _normalize_duty(existing.get("duty"))
    account_status = _normalize_status(existing.get("status"))

    if payload.duty is None:
        new_duty = "off_duty" if current_duty == "on_duty" else "on_duty"
    else:
        new_duty = _normalize_duty(payload.duty)

    if new_duty == "on_duty" and account_status == "inactive":
        raise HTTPException(
            status_code=409,
            detail="Account is deactivated and cannot be placed on duty",
        )

    changed_at = datetime.now(timezone.utc)
    ref.set({"duty": new_duty, "dutyChangedAt": changed_at}, merge=True)

    return {
        "accountId": account_id,
        "duty": new_duty,
        "previousDuty": current_duty,
        "changed": new_duty != current_duty,
        "dutyChangedAt": changed_at,
    }


@router.patch("/{account_id}/status")
def set_status(account_id: str, payload: AccountUpdate):
    """
    Set account status. Send {"status": "on"}, {"status": "off"}, or
    {"status": "inactive"} — same values the Accounts admin UI has always
    used.

    Deactivating a responder (status "inactive") force-ends any open shift,
    so a disabled account can't be left sitting in the on-duty roster.
    """
    if payload.status is None:
        raise HTTPException(status_code=422, detail="status is required")

    db = get_db()
    ref, snapshot, collection = _find_account(db, account_id)

    new_status = _normalize_status(payload.status)
    status_data = {"status": new_status}

    if new_status == "inactive" and collection == RESPONDERS:
        existing = snapshot.to_dict() or {}
        if _normalize_duty(existing.get("duty")) == "on_duty":
            status_data["duty"] = "off_duty"
            status_data["dutyChangedAt"] = datetime.now(timezone.utc)

    ref.set(status_data, merge=True)

    return {"accountId": account_id, "collection": collection, **status_data}


@router.post("/{account_id}/reset-password")
def reset_password(account_id: str):
    """
    Resets an account's login password back to the same default new accounts
    get (see DEFAULT_RESPONDER_PASSWORD) and re-arms is_new so they're forced
    to change it on next login — same effect as the "Force password reset"
    toggle in the Accounts UI, but this is what actually changes the
    Firebase Auth credential; the toggle alone only flips the is_new flag on
    the Firestore doc.
    """
    if firebase_auth is None:
        raise HTTPException(status_code=500, detail="firebase_admin auth is not available")

    db = get_db()
    ref, _, collection = _find_account(db, account_id)

    try:
        firebase_auth.update_user(account_id, password=DEFAULT_RESPONDER_PASSWORD)
    except Exception as err:
        # Covers firebase_admin.auth.UserNotFoundError (doc exists in
        # Firestore but has no matching Auth credential — e.g. a manually
        # seeded record) and any other Admin SDK failure.
        raise HTTPException(status_code=502, detail=f"Could not reset password: {err}")

    ref.set({"is_new": True}, merge=True)

    return {"accountId": account_id, "collection": collection, "reset": True, "is_new": True}


@router.delete("/{account_id}")
def delete_account(account_id: str):
    db = get_db()
    ref, _, collection = _find_account(db, account_id)
    ref.delete()

    # Belt and braces: clear any leftover doc in the other collection from
    # before the split (older builds wrote to both).
    other = USERS if collection == RESPONDERS else RESPONDERS
    other_ref = db.collection(other).document(account_id)
    if other_ref.get().exists:
        other_ref.delete()

    return {"deleted": True, "accountId": account_id, "collection": collection}