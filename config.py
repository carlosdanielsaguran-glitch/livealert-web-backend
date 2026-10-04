"""
LiveAlert — Firebase Config
============================
Firebase project configuration.
Used as reference for frontend devs and for any REST API calls.

NOTE: This is the web SDK config (public-facing).
      The backend uses serviceAccountKey.json for admin access.
"""

import os
from dotenv import load_dotenv

# Load variables from a local .env file (AGORA_APP_ID, AGORA_APP_CERTIFICATE,
# etc.) into the environment, if one exists. This means credentials no
# longer have to be hardcoded/edited directly in source — just update .env.
load_dotenv()



# Shortcut constants used across the backend
PROJECT_ID  = os.getenv("PROJECT_ID")
API_KEY     = os.getenv("API_KEY")

# These now come from .env (see load_dotenv() above) rather than being
# hardcoded here. If AGORA_APP_ID / AGORA_APP_CERTIFICATE are missing from
# the environment entirely, fail loudly at import time instead of silently
# falling back to stale/borrowed values that produce confusing
# "invalid token, authorized failed" errors at runtime.
AGORA_APP_ID = os.getenv("AGORA_APP_ID")
AGORA_APP_CERTIFICATE = os.getenv("AGORA_APP_CERTIFICATE")

if not AGORA_APP_ID or not AGORA_APP_CERTIFICATE:
    raise RuntimeError(
        "AGORA_APP_ID and/or AGORA_APP_CERTIFICATE are not set. "
        "Create a .env file in the project root with:\n"
        "  AGORA_APP_ID=your_app_id\n"
        "  AGORA_APP_CERTIFICATE=your_app_certificate"
    )

# Firebase REST base URLs (useful if calling Firebase Auth REST API)
# NOTE: these are read directly from .env as plain strings — .env does not
# support Python f-string interpolation, so FIREBASE_DB_URL already has the
# project ID baked into it in .env rather than using {PROJECT_ID} here.
FIREBASE_AUTH_URL = os.getenv("FIREBASE_AUTH_URL")
FIREBASE_DB_URL   = os.getenv("FIREBASE_DB_URL")

# Web SDK config (public-facing) — reassembled from flat .env vars, since
# .env can't hold a multi-line dict directly.
FIREBASE_CONFIG = {
    "apiKey": os.getenv("FIREBASE_API_KEY"),
    "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN"),
    "projectId": os.getenv("FIREBASE_PROJECT_ID"),
    "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET"),
    "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID"),
    "appId": os.getenv("FIREBASE_APP_ID"),
    "measurementId": os.getenv("FIREBASE_MEASUREMENT_ID"),
}