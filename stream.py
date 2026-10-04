"""
LiveAlert — Stream Router
==========================
Handles live stream channels and report summaries.
Uses Agora Web SDK for live broadcast and viewing.

Endpoints:
    POST /api/v1/stream/create/{emergency_id}   — create a stream channel
    GET  /api/v1/stream/token/{channel_id}       — get dynamic token for channel
    GET  /api/v1/stream/join/{channel_id}        — broadcaster page
    GET  /api/v1/stream/view/{channel_id}        — viewer page
    GET  /api/v1/stream/{emergency_id}/summary   — full report summary with AI
"""

import uuid
import time
from datetime import datetime, timezone
from math import radians, sin, cos, sqrt, atan2

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from agora_token_builder import RtcTokenBuilder

from accounts import get_station_scope, route_unassigned
from config import AGORA_APP_ID, AGORA_APP_CERTIFICATE
from firebase import get_db
from gemini import generate_incident_summary

router = APIRouter(prefix="/stream", tags=["Stream"])


def _to_dt(firestore_ts):
    if firestore_ts is None:
        return None
    if hasattr(firestore_ts, "ToDatetime"):
        return firestore_ts.ToDatetime(tzinfo=timezone.utc)
    if isinstance(firestore_ts, datetime):
        return firestore_ts if firestore_ts.tzinfo else firestore_ts.replace(tzinfo=timezone.utc)
    return None


def _haversine(lat1, lng1, lat2, lng2) -> float:
    R = 6371
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _get_unit_name(db, unit_id: str | None) -> str | None:
    """Resolve an assigned unit id to its display name, mirroring report.py."""
    if not unit_id:
        return None
    try:
        unit_doc = db.collection("Units").document(unit_id).get()
        if unit_doc.exists:
            return (unit_doc.to_dict() or {}).get("unitName")
    except Exception:
        pass
    return None


def _normalize_level(level) -> str:
    """Mirrors report.py's/dashboard.py's _normalize_level(). Without this,
    this endpoint returned the raw Firestore value (e.g. just "3", or
    whatever the AI assessment produced) unformatted, under the "level" key
    only. report.py and dashboard.py both return a properly formatted
    "Level 3" under the "urgency" key. Since this endpoint is what re-fetches
    a report's data every time its detail page is opened, that mismatch
    would overwrite the correctly-formatted cached entry with a raw/
    mislabeled one — which is exactly why a report's displayed level could
    change after visiting its detail page and navigating back to a list view."""
    if isinstance(level, str) and level.lower().startswith("level"):
        return level
    if level in (None, ""):
        return "Level 3"
    return f"Level {level}"


def _generate_agora_token(channel_id: str, uid: int = 0, expiration_seconds: int = 3600) -> str:
    """
    Generate a dynamic token for Agora channel access, using Agora's real
    AccessToken2 spec via the official agora-token-builder library.

    NOTE: This used to be a hand-rolled HMAC-SHA256 token that did NOT match
    Agora's actual token format (which is a "007"-prefixed AccessToken2
    structure with CRC32-hashed channel/uid privileges, not a raw
    version+signature+app_id+channel+timestamp+uid struct). Agora's gateway
    silently rejected that invalid token, which is what caused the
    "CAN_NOT_GET_GATEWAY_SERVER: dynamic use static key" error — Agora
    couldn't parse a valid dynamic token out of it and treated the join as
    tokenless against a certificate-enabled (token-required) project.

    Args:
        channel_id: Agora channel name
        uid: User ID (0 for any user)
        expiration_seconds: Token expiration time in seconds (default 1 hour)

    Returns:
        A valid Agora RTC token string.
    """
    privilege_expire_ts = int(time.time()) + expiration_seconds
    # Role 1 = PUBLISHER. Both broadcaster and viewer pages currently join
    # in publish-capable mode; if you want viewers to be subscribe-only,
    # pass role=2 for the /view page specifically.
    return RtcTokenBuilder.buildTokenWithUid(
        AGORA_APP_ID,
        AGORA_APP_CERTIFICATE,
        channel_id,
        uid,
        1,
        privilege_expire_ts,
    )


# ── Create Stream ─────────────────────────────────────────────────────────────

@router.post("/create/{emergency_id}")
def create_stream(emergency_id: str):
    """Creates a unique stream channel for an active call."""
    db = get_db()
    ref = db.collection("ActiveCalls").document(emergency_id)
    doc = ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Active call not found")

    data = doc.to_dict()
    channel_id = data.get("streamChannelId") or f"livealert-{uuid.uuid4().hex[:10]}"
    ref.update({"streamChannelId": channel_id})

    return {
        "emergencyId": emergency_id,
        "channelId": channel_id,
        "agoraAppId": AGORA_APP_ID,
        "joinUrl": f"/api/v1/stream/join/{channel_id}",
        "viewUrl": f"/api/v1/stream/view/{channel_id}",
    }


# ── Get Token ─────────────────────────────────────────────────────────────────

@router.get("/token/{channel_id}")
def get_stream_token(channel_id: str):
    """Get a dynamic token for joining an Agora channel."""
    try:
        token = _generate_agora_token(channel_id)
        return {
            "token": token,
            "appId": AGORA_APP_ID,
            "channelId": channel_id,
            "uid": 0,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token generation failed: {str(e)}")


# ── Broadcaster Page ──────────────────────────────────────────────────────────

@router.get("/join/{channel_id}", response_class=HTMLResponse)
def join_stream(channel_id: str):
    """Opens a live stream page for the broadcaster."""
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>LiveAlert — Agora Broadcast</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ background: #111; color: #fff; font-family: sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; padding: 24px; }}
    h2 {{ color: #e53e3e; margin-bottom: 8px; }}
    p {{ color: #aaa; font-size: 13px; margin-bottom: 16px; }}
    video {{ width: 720px; max-width: 100%; border-radius: 8px; background: #000; min-height: 360px; }}
    .badge {{ background: #e53e3e; color: #fff; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; margin-bottom: 12px; display: inline-block; }}
    button {{ margin-top: 12px; padding: 10px 24px; background: #e53e3e; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }}
    button:hover {{ background: #c53030; }}
    #status {{ margin-top: 8px; font-size: 12px; color: #aaa; }}
  </style>
</head>
<body>
  <span class="badge">⬤ LIVE</span>
  <h2>LiveAlert — Broadcasting</h2>
  <p>Channel: <strong>{channel_id}</strong></p>
  <video id="local-video" autoplay playsinline muted></video>
  <button onclick="startBroadcast()">Start Broadcasting</button>
  <div id="status">Waiting to start...</div>
  <script src="https://download.agora.io/sdk/release/AgoraRTC_N-4.23.2.js"></script>
  <script>
    const channelName = "{channel_id}";
    const appId = "{AGORA_APP_ID}";
    let client;
    let localTracks = {{ audioTrack: null, videoTrack: null }};

    function status(message) {{
      document.getElementById("status").innerText = message;
    }}

    async function startBroadcast() {{
      if (!window.AgoraRTC) {{
        status("Agora SDK failed to load.");
        return;
      }}

      status("Accessing camera and microphone...");
      
      try {{
        // Fetch dynamic token from backend
        const tokenRes = await fetch(`/api/v1/stream/token/${{channelName}}`);
        if (!tokenRes.ok) throw new Error("Failed to get token");
        const tokenData = await tokenRes.json();
        const token = tokenData.token;

        client = AgoraRTC.createClient({{ mode: "rtc", codec: "vp8" }});

        client.on("user-published", async (user, mediaType) => {{
          await client.subscribe(user, mediaType);
          if (mediaType === "video") {{
            user.videoTrack.play("remote-video");
          }}
        }});

        const uid = Math.floor(Math.random() * 100000);
        await client.join(appId, channelName, token, uid);
        [localTracks.audioTrack, localTracks.videoTrack] = await AgoraRTC.createMicrophoneAndCameraTracks();
        await client.publish([localTracks.audioTrack, localTracks.videoTrack]);
        localTracks.videoTrack.play("local-video");
        status("Broadcasting live with Agora.");
      }} catch (err) {{
        status("Error: " + err.message);
      }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ── Viewer Page ───────────────────────────────────────────────────────────────

@router.get("/view/{channel_id}", response_class=HTMLResponse)
def view_stream(channel_id: str):
    """View-only live stream page."""
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>LiveAlert — Agora Viewing</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ background: #111; color: #fff; font-family: sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; padding: 24px; }}
    h2 {{ color: #e53e3e; margin-bottom: 8px; }}
    p {{ color: #aaa; font-size: 13px; margin-bottom: 16px; }}
    video {{ width: 720px; max-width: 100%; border-radius: 8px; background: #000; min-height: 360px; }}
    .badge {{ background: #e53e3e; color: #fff; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: bold; margin-bottom: 12px; display: inline-block; }}
    #status {{ margin-top: 8px; font-size: 12px; color: #aaa; }}
  </style>
</head>
<body>
  <span class="badge">⬤ LIVE</span>
  <h2>LiveAlert — Viewing</h2>
  <p>Channel: <strong>{channel_id}</strong></p>
  <video id="remote-video" autoplay playsinline></video>
  <div id="status">Connecting to stream...</div>
  <script src="https://download.agora.io/sdk/release/AgoraRTC_N-4.23.2.js"></script>
  <script>
    const channelName = "{channel_id}";
    const appId = "{AGORA_APP_ID}";
    let client;

    function status(message) {{
      document.getElementById("status").innerText = message;
    }}

    async function watchStream() {{
      if (!window.AgoraRTC) {{
        status("Agora SDK failed to load.");
        return;
      }}

      try {{
        // Fetch dynamic token from backend
        const tokenRes = await fetch(`/api/v1/stream/token/${{channelName}}`);
        if (!tokenRes.ok) throw new Error("Failed to get token");
        const tokenData = await tokenRes.json();
        const token = tokenData.token;

        client = AgoraRTC.createClient({{ mode: "rtc", codec: "vp8" }});
        client.on("user-published", async (user, mediaType) => {{
          await client.subscribe(user, mediaType);
          if (mediaType === "video") {{
            user.videoTrack.play("remote-video");
          }}
          status("Watching live with Agora.");
        }});

        const uid = Math.floor(Math.random() * 100000);
        await client.join(appId, channelName, token, uid);
        status("Connected. Waiting for broadcaster...");
      }} catch (err) {{
        status("Stream not available yet: " + err.message);
      }}
    }}

    watchStream();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ── Report Summary ────────────────────────────────────────────────────────────

@router.get("/{emergency_id}/summary")
def get_report_summary(emergency_id: str, station_id: str | None = Depends(get_station_scope)):
    """
    Full Report Summary for 'View Report' page:
      - AI-generated description + severity level (via Gemini)
      - Reported by (user name)
      - Location
      - Nearest stations with distances
      - Stream URLs
    """
    db = get_db()

    # Active call
    doc = db.collection("ActiveCalls").document(emergency_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Active call not found")

    doc = route_unassigned(db, [doc])[0]
    data = doc.to_dict()

    # Station admins may only open reports routed to their own station.
    if station_id:
        assigned = data.get("assignedStation") or {}
        if str(data.get("nearestStationId") or assigned.get("stationId") or "").strip() != station_id:
            raise HTTPException(status_code=404, detail="Active call not found")

    # IMPORTANT: live.py's /get-token endpoint (the one the reporter's phone
    # actually calls to start broadcasting) creates the ActiveCalls document
    # using the real Agora channel name AS THE DOCUMENT ID, and also stores
    # it in the "channelName" field. That is the one true channel the
    # citizen's app is actually publishing video to.
    #
    # This endpoint used to read/generate an entirely separate
    # "streamChannelId" field instead, and sign the returned token for THAT
    # channel — while report.js's getStreamConfig() prioritizes
    # report.channelName over report.streamChannelId and joins the REAL
    # channel. Since Agora tokens are cryptographically bound to a specific
    # channel name, a token signed for channel A is rejected when used to
    # join channel B. That mismatch — not the credentials, not the token
    # format — was the actual cause of
    # "CAN_NOT_GET_GATEWAY_SERVER: invalid token, authorized failed".
    #
    # Fix: always prefer the real channelName (falling back to emergency_id
    # itself, since that IS the channel name for docs created by live.py,
    # and only inventing a placeholder for legacy/test docs that have
    # neither) and generate the token for that exact same value.
    channel_id = data.get("channelName") or data.get("streamChannelId") or emergency_id

    if data.get("streamChannelId") != channel_id:
        db.collection("ActiveCalls").document(emergency_id).update({"streamChannelId": channel_id})

    assigned_station = data.get("assignedStation") or {}
    incident_type = data.get("incidentType") or data.get("channelName") or "Unknown"
    location = data.get("location", "")

    # Location string
    location_str = ""
    if isinstance(location, str):
        location_str = location
    elif isinstance(location, dict):
        location_str = location.get("address", "")
    elif hasattr(location, "latitude"):
        location_str = f"{location.latitude}, {location.longitude}"

    # Reporter (ActiveCalls uses reporterUuid; fall back to reportedBy for
    # compatibility with any older docs)
    reporter_id = data.get("reporterUuid") or data.get("reportedBy", "")
    reporter_name = "Unknown"
    if reporter_id:
        user_doc = db.collection("Users").document(reporter_id).get()
        if user_doc.exists:
            u = user_doc.to_dict()
            first = u.get("firstName", "")
            last = u.get("lastName", "")
            reporter_name = f"{first} {last}".strip() or u.get("name", "Unknown")
        else:
            # Not every reporterUuid on ActiveCalls resolves to a real Users
            # doc (e.g. test data), so fall back to showing the raw value
            # instead of "Unknown".
            reporter_name = reporter_id

    # Active call coordinates — ActiveCalls stores latitude/longitude, not lat/lng
    e_lat, e_lng = None, None
    if hasattr(location, "latitude"):
        e_lat, e_lng = location.latitude, location.longitude
    elif isinstance(location, dict):
        e_lat = location.get("latitude", location.get("lat"))
        e_lng = location.get("longitude", location.get("lng"))

    # Stations with distances
    station_docs = list(db.collection("Stations").stream())
    stations = []
    current_nearest = data.get("nearestStationId") or assigned_station.get("stationId", "")

    for s in station_docs:
        sd = s.to_dict()
        loc = sd.get("Location") if sd.get("Location") is not None else sd.get("location")
        s_lat, s_lng = None, None
        if loc:
            if hasattr(loc, "latitude"):
                s_lat, s_lng = loc.latitude, loc.longitude
            elif isinstance(loc, dict):
                s_lat = loc.get("lat")
                s_lng = loc.get("lng")

        distance_km = None
        if e_lat and e_lng and s_lat and s_lng:
            distance_km = round(_haversine(e_lat, e_lng, s_lat, s_lng), 1)

        stations.append({
            "id": s.id,
            "stationName": sd.get("stationName", "Unknown"),
            "chiefName": sd.get("commanderName") or sd.get("chiefName", ""),
            "lat": s_lat,
            "lng": s_lng,
            "distanceKm": distance_km,
            "isNearest": s.id == current_nearest,
        })

    stations.sort(key=lambda x: x["distanceKm"] if x["distanceKm"] is not None else 9999)

    # Gemini AI — generate description and level
    ai = generate_incident_summary(incident_type, location_str)

    # Save level back to Firestore if not already set
    if not data.get("level"):
        db.collection("ActiveCalls").document(emergency_id).update({"level": ai["level"]})

    normalized_level = _normalize_level(data.get("level") or ai["level"])

    analyzed_at = _to_dt(data.get("analyzed_at") or data.get("analyzedAt"))
    unit_ids = data.get("unitIds")
    if not isinstance(unit_ids, list):
      unit_ids = assigned_station.get("unitIds")
    if not isinstance(unit_ids, list):
      unit_ids = [assigned_station.get("unitId") or data.get("unitId")]
    unit_ids = list(dict.fromkeys(unit_id for unit_id in unit_ids if unit_id))
    unit_id = assigned_station.get("unitId") or data.get("unitId") or (unit_ids[0] if unit_ids else None)
    assigned_station = {
      **{key: value for key, value in assigned_station.items() if key != "unitIds"},
      "unitId": unit_id,
    }

    return {
        "emergencyId": emergency_id,
        "incidentType": incident_type,
        "channelName": data.get("channelName"),
        "status": data.get("status", "pending"),
        "level": normalized_level,
        # Alias under the field name list views (dashboard.js, history.js)
        # actually read — see _normalize_level() above for why this matters.
        "urgency": normalized_level,
        "threatLevel": data.get("threatLevel"),
        "description": data.get("description") or data.get("ai_assessment") or ai["description"],
        "aiAssessment": data.get("ai_assessment"),
        "aiReasoning": data.get("ai_reasoning"),
        "aiSummary": data.get("ai_summary"),
        "analyzedAt": analyzed_at.isoformat() if analyzed_at else None,
        "reportedBy": {
            "id": reporter_id,
            "name": reporter_name,
        },
        "location": location_str,
        "locationRaw": data.get("location") if isinstance(data.get("location"), dict) else None,
        "nearestStations": stations,
        "respondersInvolved": data.get("respondersInvolved", []),
        "assignedStation": assigned_station,
        # Flat alias of nearestStationId / assignedStation.stationId, under the
        # name the frontend's unit-assignment dropdown (report.js, dashboard.js)
        # actually reads. Without this, the dropdown always rendered disabled
        # ("no assigned station yet") for every report opened from the
        # dashboard, since this endpoint — not report.py's /reports/recent —
        # is what ultimately populates the incident tracker modal.
        "stationId": current_nearest,
        "unitId": unit_id,
        "unitName": _get_unit_name(db, unit_id),
        "streamChannelId": channel_id,
        "viewUrl": f"/api/v1/stream/view/{channel_id}" if channel_id else None,
        "joinUrl": f"/api/v1/stream/join/{channel_id}" if channel_id else None,
        "agoraAppId": AGORA_APP_ID,
        # report.js's getStreamConfig() reads this field directly and passes
        # it into client.join() for the inline "Agora Live Incident Feed"
        # panel on the report page. Without it, that join always used null,
        # which Agora's gateway rejects once App Certificate is enabled
        # (CAN_NOT_GET_GATEWAY_SERVER: dynamic use static key) — the same
        # class of bug as the broken _generate_agora_token, just missing
        # from a different response entirely.
        "token": _generate_agora_token(channel_id) if channel_id else None,
        "createdAt": _to_dt(data.get("timestamp") or data.get("createdAt")),
    }