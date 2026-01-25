"""
Telemedicine Backend - Robot to Doctor
Uses FCM V1 API (new method)
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import requests
import os
import json
import uvicorn
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ==================================================
# 🔐 FIREBASE CONFIGURATION
# ==================================================
# Set service account JSON as environment variable
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "ai-dobot")

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
# 🔑 GET FCM ACCESS TOKEN
# ==================================================
def get_access_token():
    """Get OAuth2 access token for FCM V1 API"""
    try:
        if not SERVICE_ACCOUNT_JSON:
            return None
            
        service_account_info = json.loads(SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        credentials.refresh(Request())
        return credentials.token
    except Exception as e:
        print(f"❌ Token error: {e}")
        return None

# ==================================================
# 🔔 REGISTER DOCTOR TOKEN
# ==================================================
@app.post("/api/doctor/register-token")
def register_doctor_token(data: DoctorToken):
    if data.token not in doctor_tokens:
        doctor_tokens.append(data.token)
        print(f"✅ Doctor registered (total: {len(doctor_tokens)})")
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
    
    if not SERVICE_ACCOUNT_JSON:
        raise HTTPException(
            status_code=500,
            detail="Firebase not configured"
        )
    
    # Send to all doctors
    success_count = 0
    for token in doctor_tokens:
        result = send_fcm_v1_notification(
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
# 🔥 SEND FCM V1 NOTIFICATION
# ==================================================
def send_fcm_v1_notification(token: str, title: str, body: str, data: dict):
    access_token = get_access_token()
    if not access_token:
        print("❌ Cannot get access token")
        return False
    
    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body
            },
            "data": data,
            "webpush": {
                "notification": {
                    "title": title,
                    "body": body,
                    "requireInteraction": True
                }
            }
        }
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            print(f"✅ FCM sent")
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
        "message": "Telemedicine Backend",
        "doctors_online": len(doctor_tokens),
        "firebase_configured": bool(SERVICE_ACCOUNT_JSON)
    }

# ==================================================
# 🏁 START SERVER
# ==================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print("=" * 60)
    print("🏥 TELEMEDICINE BACKEND")
    print("=" * 60)
    print(f"📍 Port: {port}")
    print(f"🔥 Firebase: {'✅' if SERVICE_ACCOUNT_JSON else '❌'}")
    print("=" * 60)
    
    uvicorn.run(app, host="0.0.0.0", port=port)

