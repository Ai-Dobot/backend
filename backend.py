"""
backend.py — Telemedicine Backend
===================================
Deploy this single file to Render.

ENVIRONMENT VARIABLES to set in Render:
  FIREBASE_SERVICE_ACCOUNT  →  paste your full Firebase service account JSON
  FIREBASE_PROJECT_ID       →  ai-dobot

HOW IT CONNECTS:
  Doctor (GitHub Pages) ──sign in──► POST /api/doctor/signin   ← registers profile + FCM token
  Patient (Local Robot) ──────────► GET  /api/doctors/online   ← fetches live doctor list
  Patient (Local Robot) ──call───► POST /api/calls/initiate    ← fires FCM to specific doctor
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import requests, os, json, uvicorn, uuid, time
from google.oauth2 import service_account
from google.auth.transport.requests import Request


# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
PROJECT_ID           = os.getenv("FIREBASE_PROJECT_ID", "ai-dobot")

app = FastAPI(title="AI Dobot — Telemedicine Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════
class DoctorSignIn(BaseModel):
    name:      str
    specialty: str
    avatar:    str          # emoji chosen on sign-in screen
    token:     str          # FCM token
    doctor_id: Optional[str] = None   # re-use ID on refresh

class DoctorSignOut(BaseModel):
    doctor_id: str

class PatientCall(BaseModel):
    patient_name: str
    patient_id:   str
    symptom:      str
    doctor_id:    Optional[str] = None   # call specific doctor; None = all online


# ══════════════════════════════════════════════════════════
# IN-MEMORY STORAGE
# doctors:       { doctor_id: {...} }
# pending_calls: { call_id:   { doctor_id, patient_name, ... } }
# ══════════════════════════════════════════════════════════
doctors:       Dict[str, dict] = {}
pending_calls: Dict[str, dict] = {}


# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════
def _online_doctors():
    """Return list of doctors whose heartbeat is < 90 seconds old."""
    cutoff = time.time() - 90
    return [
        {**d, "doctor_id": did, "available": True}
        for did, d in doctors.items()
        if d.get("last_seen", 0) > cutoff
    ]


def get_access_token():
    try:
        if not SERVICE_ACCOUNT_JSON:
            print("❌ FIREBASE_SERVICE_ACCOUNT not set"); return None
        js = SERVICE_ACCOUNT_JSON.strip()
        if not js.startswith('{'):
            js = js[js.find('{'):]
        if not js.endswith('}'):
            js = js[:js.rfind('}')+1]
        sa = json.loads(js)
        for f in ['type','project_id','private_key','client_email']:
            if f not in sa:
                print(f"❌ Missing: {f}"); return None
        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        print(f"❌ Token error: {e}"); return None


def send_fcm(token: str, title: str, body: str, data: dict) -> bool:
    at = get_access_token()
    if not at: return False
    resp = requests.post(
        f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send",
        headers={"Authorization": f"Bearer {at}", "Content-Type": "application/json"},
        json={"message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in data.items()},
            "webpush": {"notification": {"title": title, "body": body, "requireInteraction": True}},
        }},
        timeout=10
    )
    if resp.status_code == 200:
        print(f"✅ FCM sent to …{token[-8:]}"); return True
    print(f"❌ FCM failed [{resp.status_code}]: {resp.text}")
    # Remove dead tokens
    if "UNREGISTERED" in resp.text or resp.status_code == 404:
        for did, d in list(doctors.items()):
            if d.get("token") == token:
                del doctors[did]
                print(f"   Removed offline doctor {d['name']}")
    return False


# ══════════════════════════════════════════════════════════
# ENDPOINT: POLL PENDING CALLS  (doctor polls every 5s)
# Catches calls that FCM missed due to background tab
# ══════════════════════════════════════════════════════════
@app.get("/api/calls/pending/{doctor_id}")
def get_pending_calls(doctor_id: str):
    cutoff = time.time() - 120
    my_calls = [
        {**v, "call_id": k}
        for k, v in pending_calls.items()
        if v.get("doctor_id") == doctor_id and v.get("created_at", 0) > cutoff
    ]
    return {"calls": my_calls}


@app.delete("/api/calls/pending/{call_id}")
def acknowledge_call(call_id: str):
    pending_calls.pop(call_id, None)
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════
# ENDPOINT: HEALTH CHECK
# ══════════════════════════════════════════════════════════
@app.get("/")
def health_check():
    online = _online_doctors()
    return {
        "status":             "online",
        "service":            "AI Dobot Telemedicine Backend",
        "doctors_online":     len(online),
        "firebase_configured": bool(SERVICE_ACCOUNT_JSON),
    }


# ══════════════════════════════════════════════════════════
# ENDPOINT: DOCTOR SIGN IN
# Called by doctor-app/index.html when doctor submits sign-in form.
# ══════════════════════════════════════════════════════════
@app.post("/api/doctor/signin")
def doctor_signin(data: DoctorSignIn):
    # Re-use existing ID if doctor is refreshing
    doctor_id = data.doctor_id or str(uuid.uuid4())[:8]

    doctors[doctor_id] = {
        "name":        data.name.strip(),
        "specialty":   data.specialty.strip(),
        "avatar":      data.avatar,
        "token":       data.token.strip(),
        "signed_in_at": time.time(),
        "last_seen":   time.time(),
    }
    print(f"✅ Doctor signed in: {data.name} ({data.specialty})  id={doctor_id}  online={len(_online_doctors())}")
    return {"status": "success", "doctor_id": doctor_id, "doctors_online": len(_online_doctors())}


# ══════════════════════════════════════════════════════════
# ENDPOINT: DOCTOR HEARTBEAT  (called every 30s while tab open)
# ══════════════════════════════════════════════════════════
@app.post("/api/doctor/heartbeat")
def doctor_heartbeat(data: DoctorSignOut):   # reuses {doctor_id} shape
    if data.doctor_id in doctors:
        doctors[data.doctor_id]["last_seen"] = time.time()
        return {"status": "ok", "doctors_online": len(_online_doctors())}
    raise HTTPException(status_code=404, detail="Doctor not found — please sign in again")


# ══════════════════════════════════════════════════════════
# ENDPOINT: DOCTOR SIGN OUT
# ══════════════════════════════════════════════════════════
@app.post("/api/doctor/signout")
def doctor_signout(data: DoctorSignOut):
    if data.doctor_id in doctors:
        name = doctors[data.doctor_id]["name"]
        del doctors[data.doctor_id]
        print(f"👋 Doctor signed out: {name}  online={len(_online_doctors())}")
    return {"status": "success", "doctors_online": len(_online_doctors())}


# ══════════════════════════════════════════════════════════
# ENDPOINT: GET ONLINE DOCTORS
# Called by local patient Flask app to populate doctor list.
# ══════════════════════════════════════════════════════════
@app.get("/api/doctors/online")
def get_online_doctors():
    online = _online_doctors()
    # Don't expose FCM tokens to patient side
    safe = [{k: v for k, v in d.items() if k != "token"} for d in online]
    return {"status": "success", "doctors": safe, "count": len(safe)}


# ══════════════════════════════════════════════════════════
# ENDPOINT: PATIENT INITIATES CALL
# ══════════════════════════════════════════════════════════
@app.post("/api/calls/initiate")
def initiate_call(call: PatientCall):
    print(f"\n📞 Call from {call.patient_name}  →  doctor_id={call.doctor_id or 'ALL'}")
    online = _online_doctors()
    if not online:
        raise HTTPException(status_code=503, detail="No doctors online right now.")
    if not SERVICE_ACCOUNT_JSON:
        raise HTTPException(status_code=500, detail="Firebase not configured on server.")

    call_id        = str(uuid.uuid4())[:8]
    video_call_url = f"https://meet.jit.si/ai-dobot-{call_id}"

    # Target: specific doctor if doctor_id given, otherwise all online
    targets = (
        [doctors[call.doctor_id]] if call.doctor_id and call.doctor_id in doctors
        else [doctors[d["doctor_id"]] for d in online]
    )

    success = 0
    for d in targets:
        ok = send_fcm(
            token = d["token"],
            title = "🚨 New Patient Call",
            body  = f"{call.patient_name} · {call.symptom[:120]}",
            data  = {
                "patient_name":  call.patient_name,
                "patient_id":    call.patient_id,
                "symptom":       call.symptom,
                "video_call_url": video_call_url,
            }
        )
        if ok: success += 1

    # Store pending so polling catches what FCM misses
    for d in targets:
        target_id = next((did for did, doc in doctors.items() if doc is d), None)
        if target_id:
            pending_calls[call_id + "_" + target_id] = {
                "doctor_id":      target_id,
                "patient_name":   call.patient_name,
                "patient_id":     call.patient_id,
                "symptom":        call.symptom,
                "video_call_url": video_call_url,
                "created_at":     time.time(),
            }

    print(f"   Notified {success}/{len(targets)} doctor(s)  room={video_call_url}\n")
    return {"status": "success", "message": f"Notified {success} doctor(s)",
            "video_call_url": video_call_url, "call_id": call_id}


# ══════════════════════════════════════════════════════════
# LEGACY: old register-token endpoint (keeps compatibility)
# ══════════════════════════════════════════════════════════
@app.post("/api/doctor/register-token")
def register_doctor_token(data: dict):
    return {"status": "success", "message": "Use /api/doctor/signin instead"}


# ══════════════════════════════════════════════════════════
# START SERVER
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("=" * 60)
    print("  🏥  AI DOBOT — TELEMEDICINE BACKEND")
    print("=" * 60)
    print(f"  Port     : {port}")
    print(f"  Firebase : {'✅ Configured' if SERVICE_ACCOUNT_JSON else '❌ NOT SET'}")
    print(f"  Project  : {PROJECT_ID}")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=port)
