"""
backend.py — Telemedicine Backend
===================================
Deploy this single file to Render.

ENVIRONMENT VARIABLES to set in Render:
  FIREBASE_SERVICE_ACCOUNT  →  paste your full Firebase service account JSON
  FIREBASE_PROJECT_ID       →  ai-dobot  (or your project ID)

HOW IT CONNECTS:
  ┌─────────────────────┐       POST /api/calls/initiate
  │  Patient (Robot)    │ ──────────────────────────────────► Render Backend
  │  Local Flask app    │                                         │
  └─────────────────────┘                                   Firebase FCM V1
                                                                  │
  ┌─────────────────────┐       Push Notification                 ▼
  │  Doctor App         │ ◄──────────────────────────────── Firebase Cloud
  │  GitHub Pages       │                                    Messaging
  └─────────────────────┘

ENDPOINTS:
  GET  /                           Health check — shows doctors online count
  POST /api/doctor/register-token  Doctor app calls this on load to register FCM token
  POST /api/calls/initiate         Robot/patient calls this to notify doctor
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import requests
import os
import json
import uvicorn
import uuid
from google.oauth2 import service_account
from google.auth.transport.requests import Request


# ══════════════════════════════════════════════════════════
# FIREBASE CONFIGURATION
# Set these as environment variables in Render dashboard.
# ══════════════════════════════════════════════════════════
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
PROJECT_ID           = os.getenv("FIREBASE_PROJECT_ID", "ai-dobot")


# ══════════════════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════════════════
app = FastAPI(title="AI Dobot — Telemedicine Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # allow both robot (local) and doctor app (GitHub Pages)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════
class PatientCall(BaseModel):
    patient_name: str
    patient_id:   str
    symptom:      str

class DoctorToken(BaseModel):
    token: str


# ══════════════════════════════════════════════════════════
# IN-MEMORY STORAGE
# Doctor tokens are stored here while the server is running.
# NOTE: Render free tier restarts periodically — doctors
# re-register automatically each time they open their app.
# ══════════════════════════════════════════════════════════
doctor_tokens: List[str] = []


# ══════════════════════════════════════════════════════════
# GET FCM ACCESS TOKEN  (OAuth2 for FCM V1 API)
# ══════════════════════════════════════════════════════════
def get_access_token() -> str | None:
    """Exchange service account credentials for a short-lived OAuth2 token."""
    try:
        if not SERVICE_ACCOUNT_JSON:
            print("❌ FIREBASE_SERVICE_ACCOUNT environment variable is not set")
            return None

        # Clean up the JSON string (handles extra whitespace from env var paste)
        json_str = SERVICE_ACCOUNT_JSON.strip()
        if not json_str.startswith('{'):
            idx = json_str.find('{')
            if idx != -1:
                json_str = json_str[idx:]
        if not json_str.endswith('}'):
            idx = json_str.rfind('}')
            if idx != -1:
                json_str = json_str[:idx + 1]

        sa_info = json.loads(json_str)

        # Validate required fields
        for field in ['type', 'project_id', 'private_key', 'client_email']:
            if field not in sa_info:
                print(f"❌ Missing field in service account JSON: {field}")
                return None

        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        creds.refresh(Request())
        print("✅ Firebase access token obtained")
        return creds.token

    except json.JSONDecodeError as e:
        print(f"❌ JSON parse error: {e}")
        print(f"   First 100 chars: {SERVICE_ACCOUNT_JSON[:100]}")
        return None
    except Exception as e:
        print(f"❌ Token error: {e}")
        import traceback; traceback.print_exc()
        return None


# ══════════════════════════════════════════════════════════
# SEND FCM V1 NOTIFICATION
# ══════════════════════════════════════════════════════════
def send_fcm_notification(token: str, title: str, body: str, data: dict) -> bool:
    """Send a push notification to one doctor device via FCM V1 API."""
    access_token = get_access_token()
    if not access_token:
        return False

    url     = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }

    # All data values must be strings for FCM
    str_data = {k: str(v) for k, v in data.items()}

    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body":  body,
            },
            "data": str_data,
            "webpush": {
                "notification": {
                    "title":            title,
                    "body":             body,
                    "requireInteraction": True,
                    "icon":             "/icon.png",
                },
                # Pass all data as webpush fcmOptions so notification click carries it
                "fcm_options": {
                    "link": "/"
                }
            },
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            print(f"✅ FCM notification sent to token …{token[-8:]}")
            return True
        else:
            print(f"❌ FCM failed [{resp.status_code}]: {resp.text}")
            # Remove invalid/expired tokens automatically
            if resp.status_code == 404 or "UNREGISTERED" in resp.text:
                print(f"   Removing expired token …{token[-8:]}")
                if token in doctor_tokens:
                    doctor_tokens.remove(token)
            return False
    except Exception as e:
        print(f"❌ FCM request error: {e}")
        return False


# ══════════════════════════════════════════════════════════
# ENDPOINT: HEALTH CHECK
# ══════════════════════════════════════════════════════════
@app.get("/")
def health_check():
    return {
        "status":             "online",
        "service":            "AI Dobot Telemedicine Backend",
        "doctors_online":     len(doctor_tokens),
        "firebase_configured": bool(SERVICE_ACCOUNT_JSON),
    }


# ══════════════════════════════════════════════════════════
# ENDPOINT: REGISTER DOCTOR TOKEN
# Called by doctor-app/index.html on every page load.
# ══════════════════════════════════════════════════════════
@app.post("/api/doctor/register-token")
def register_doctor_token(data: DoctorToken):
    token = data.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token cannot be empty")

    if token not in doctor_tokens:
        doctor_tokens.append(token)
        print(f"✅ Doctor token registered  (total online: {len(doctor_tokens)})")
    else:
        print(f"ℹ️  Doctor token refreshed   (total online: {len(doctor_tokens)})")

    return {
        "status":  "success",
        "message": "Token registered",
        "doctors_online": len(doctor_tokens),
    }


# ══════════════════════════════════════════════════════════
# ENDPOINT: PATIENT INITIATES CALL
# Called by the local robot Flask app (/doctor/call route).
# ══════════════════════════════════════════════════════════
@app.post("/api/calls/initiate")
def initiate_call(call: PatientCall):
    print(f"\n📞 Incoming call — Patient: {call.patient_name}  ID: {call.patient_id}")
    print(f"   Symptoms: {call.symptom[:120]}")

    if not doctor_tokens:
        raise HTTPException(
            status_code=503,
            detail="No doctors online. Doctor must open their dashboard first."
        )

    if not SERVICE_ACCOUNT_JSON:
        raise HTTPException(
            status_code=500,
            detail="Firebase not configured on server. Set FIREBASE_SERVICE_ACCOUNT env var in Render."
        )

    # Generate a unique Jitsi room for this call
    call_id        = str(uuid.uuid4())[:8]
    video_call_url = f"https://meet.jit.si/ai-dobot-call-{call_id}"
    print(f"   🎥 Room: {video_call_url}")

    # Notify every registered doctor
    success_count = 0
    for token in list(doctor_tokens):   # copy list — send_fcm may remove expired tokens
        ok = send_fcm_notification(
            token = token,
            title = "🚨 New Patient Call",
            body  = f"{call.patient_name} (ID: {call.patient_id})\n{call.symptom[:200]}",
            data  = {
                "patient_name":  call.patient_name,
                "patient_id":    call.patient_id,
                "symptom":       call.symptom,
                "video_call_url":video_call_url,
            }
        )
        if ok:
            success_count += 1

    print(f"   Notified {success_count}/{len(doctor_tokens)} doctor(s)\n")

    return {
        "status":        "success",
        "message":       f"Notified {success_count} doctor(s)",
        "video_call_url": video_call_url,
        "call_id":        call_id,
    }


# ══════════════════════════════════════════════════════════
# OPTIONAL ENDPOINT: LIST ACTIVE TOKENS (for debugging)
# ══════════════════════════════════════════════════════════
@app.get("/api/doctor/status")
def doctor_status():
    return {
        "doctors_online": len(doctor_tokens),
        "tokens":         [f"…{t[-8:]}" for t in doctor_tokens],  # show only last 8 chars
    }


# ══════════════════════════════════════════════════════════
# START SERVER (Render uses $PORT env var automatically)
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("=" * 60)
    print("  🏥  AI DOBOT — TELEMEDICINE BACKEND")
    print("=" * 60)
    print(f"  Port     : {port}")
    print(f"  Firebase : {'✅ Configured' if SERVICE_ACCOUNT_JSON else '❌ NOT SET — set FIREBASE_SERVICE_ACCOUNT in Render'}")
    print(f"  Project  : {PROJECT_ID}")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=port)
