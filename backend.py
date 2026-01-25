"""
Simple Telemedicine Backend - Firebase V1 API
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import uvicorn
import firebase_admin
from firebase_admin import credentials, messaging

# ==================================================
# 🔐 FIREBASE INITIALIZATION (V1 API)
# ==================================================
cred = credentials.Certificate("ai-dobot-47091f1e04a6.json")
firebase_admin.initialize_app(cred)

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
        print(f"✅ Token registered. Total doctors: {len(doctor_tokens)}")
    return {"status": "success", "message": "Token registered"}

# ==================================================
# 📞 PATIENT INITIATES CALL
# ==================================================
@app.post("/api/calls/initiate")
def initiate_call(call: PatientCall):
    if not doctor_tokens:
        raise HTTPException(
            status_code=503,
            detail="No doctors available"
        )

    # Create notification for each doctor
    success_count = 0
    for token in doctor_tokens:
        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title="🚨 New Patient Call",
                    body=f"{call.patient_name} (ID: {call.patient_id})\n{call.symptom}"
                ),
                data={
                    "patient_name": call.patient_name,
                    "patient_id": call.patient_id,
                    "symptom": call.symptom
                },
                token=token
            )
            
            response = messaging.send(message)
            print(f"✅ Sent to doctor: {response}")
            success_count += 1
            
        except Exception as e:
            print(f"❌ Failed to send: {e}")

    return {
        "status": "success",
        "message": f"Call sent to {success_count} doctor(s)"
    }

# ==================================================
# 🏁 RUN SERVER
# ==================================================
if __name__ == "__main__":
    print("🏥 Telemedicine Backend Starting...")
    print("📡 Listening on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)