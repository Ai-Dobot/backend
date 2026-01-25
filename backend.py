"""
Telemedicine Backend - Robot to Doctor
Uses direct FCM HTTP API (no firebase-admin needed)
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import requests
import os
import uvicorn

# ==================================================
# 🔐 FIREBASE SERVER KEY
# ==================================================
FIREBASE_SERVER_KEY = os.getenv("FIREBASE_SERVER_KEY", "")

if not FIREBASE_SERVER_KEY:
    print("⚠️  WARNING: FIREBASE_SERVER_KEY not set!")
    print("Set it in Render environment variables")

# ==================================================
# 🚀 FASTAPI APP
# ==================================================
app = FastAPI(title="Telemedicine Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================================================
# 📦 DATA MODELS
# ==================================================
class PatientCall(BaseModel):
    patient_name: str
    patient_id: str
    symptom: str

class DoctorToken(BaseModel):
    token: str

# ==================================================
# 🧠 STORAGE
# ==================================================
doctor_tokens: List[str] = []

# ==================================================
# 🔔 REGISTER DOCTOR TOKEN
# ==================================================
@app.post("/api/doctor/register-token")
def register_doctor_token(data: DoctorToken):
    if data.token not in doctor_tokens:
        doctor_tokens.append(data.token)
        print(f"✅ Doctor token registered (total: {len(doctor_tokens)})")
    return {"status": "success", "message": "Token registered"}

# ==================================================
# 📞 PATIENT CALL
# ==================================================
@app.post("/api/calls/initiate")
def initiate_call(call: PatientCall):
    print(f"📞 Call from: {call.patient_name} (ID: {call.patient_id})")
    
    if not doctor_tokens:
        raise HTTPException(
            status_code=503,
            detail="No doctors online"
        )
    
    if not FIREBASE_SERVER_KEY:
        raise HTTPException(
            status_code=500,
            detail="Firebase not configured. Set FIREBASE_SERVER_KEY environment variable."
        )
    
    # Send to all doctors
    success_count = 0
    for token in doctor_tokens:
        result = send_fcm_notification(
            token=token,
            title="🚨 New Patient Call",
            body=f"{call.patient_name} (ID: {call.patient_id})\n{call.symptom}",
            data={
                "patient_name": call.patient_name,
                "patient_id": call.patient_id,
                "symptom": call.symptom
            }
        )
        if result:
            success_count += 1
    
    return {
        "status": "success",
        "message": f"Notified {success_count}/{len(doctor_tokens)} doctor(s)"
    }

# ==================================================
# 🔥 SEND FCM NOTIFICATION
# ==================================================
def send_fcm_notification(token: str, title: str, body: str, data: dict):
    url = "https://fcm.googleapis.com/fcm/send"
    headers = {
        "Authorization": f"key={FIREBASE_SERVER_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "to": token,
        "notification": {
            "title": title,
            "body": body,
            "sound": "default"
        },
        "data": data,
        "priority": "high"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            print(f"✅ FCM sent successfully")
            return True
        else:
            print(f"❌ FCM failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ FCM error: {e}")
        return False

# ==================================================
# ✅ HEALTH CHECK
# ==================================================
@app.get("/")
def health_check():
    return {
        "status": "online",
        "message": "Telemedicine Backend Running",
        "doctors_online": len(doctor_tokens),
        "firebase_configured": bool(FIREBASE_SERVER_KEY)
    }

# ==================================================
# 🏁 START SERVER
# ==================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("=" * 60)
    print("🏥 TELEMEDICINE BACKEND STARTING")
    print("=" * 60)
    print(f"📍 Port: {port}")
    print(f"🔥 Firebase: {'✅ Configured' if FIREBASE_SERVER_KEY else '❌ Not configured'}")
    print("=" * 60)
    
    uvicorn.run(app, host="0.0.0.0", port=port)
