"""
backend.py — AI Dobot Telemedicine Backend
==========================================
Deploy this single file to Render.

ENVIRONMENT VARIABLES:
  FIREBASE_SERVICE_ACCOUNT  → paste full Firebase service account JSON
  FIREBASE_PROJECT_ID       → ai-dobot
  DATABASE_URL              → Neon PostgreSQL connection string

HOW IT CONNECTS:
  Doctor app (GitHub Pages) ──sign in──► POST /api/doctor/signin
  Patient robot ─────────────────────► GET  /api/doctors/online
  Patient robot ──────call──────────► POST /api/calls/initiate  ← fires FCM to doctor
  Hospital/Patient/Pharmacy portals ► /api/hospitals /api/patients /api/pharmacies
"""

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
import requests, os, json, uuid, time
# Auto-install psycopg2-binary if not present (handles Render build cache issues)
import subprocess, sys
try:
    import psycopg2
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'psycopg2-binary', '-q'])
import psycopg2, psycopg2.extras, hashlib, secrets, string, random
from google.oauth2 import service_account
from google.auth.transport.requests import Request


# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
PROJECT_ID           = os.getenv("FIREBASE_PROJECT_ID", "ai-dobot")
DATABASE_URL         = os.getenv("DATABASE_URL",
    "postgresql://neondb_owner:npg_AdjC2Un1YgPe@ep-fancy-pine-aite2ono-pooler.c-4.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

app = FastAPI(title="AI Dobot — Telemedicine Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════
# IN-MEMORY (original doctor system — FCM based)
# ══════════════════════════════════════════════════════════
doctors:       Dict[str, dict] = {}
pending_calls: Dict[str, dict] = {}
ended_calls:   Dict[str, float] = {}


def _online_doctors():
    cutoff = time.time() - 90
    return [
        {**d, "doctor_id": did, "available": True}
        for did, d in doctors.items()
        if d.get("last_seen", 0) > cutoff
    ]


# ══════════════════════════════════════════════════════════
# NEON DB (new registration system)
# ══════════════════════════════════════════════════════════
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()
def _gen_id(prefix): return prefix + '-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
def _tok(): return secrets.token_urlsafe(32)
def _bearer(h): return (h or "").replace("Bearer ", "")

def _session(table, id_col, eid):
    tok = _tok()
    exp = time.time() + 7 * 86400
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"INSERT INTO {table} ({id_col},token,expires_at) VALUES (%s,%s,to_timestamp(%s))", (eid, tok, exp))
    conn.commit(); cur.close(); conn.close()
    return tok

def _verify(table, token):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"SELECT * FROM {table} WHERE token=%s AND expires_at>NOW()", (token,))
    row = cur.fetchone(); cur.close(); conn.close()
    return dict(row) if row else None

def init_db():
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS hospitals (
            id SERIAL PRIMARY KEY, system_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            country TEXT, city TEXT, address TEXT, phone TEXT, registration_no TEXT,
            approval_status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS reg_doctors (
            id SERIAL PRIMARY KEY, system_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            specialty TEXT, country TEXT, city TEXT, phone TEXT,
            qualifications TEXT, bio TEXT, avatar TEXT DEFAULT '👨‍⚕️',
            license_doc_url TEXT,
            hospital_id INTEGER REFERENCES hospitals(id) ON DELETE SET NULL,
            is_online BOOLEAN DEFAULT FALSE, is_public BOOLEAN DEFAULT TRUE,
            consultation_fee NUMERIC(10,2) DEFAULT 0,
            approval_status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS patients (
            id SERIAL PRIMARY KEY, system_id TEXT UNIQUE NOT NULL,
            password_hash TEXT, name TEXT, email TEXT, phone TEXT,
            country TEXT, city TEXT, date_of_birth DATE,
            personal_doctor_id INTEGER REFERENCES reg_doctors(id) ON DELETE SET NULL,
            approval_status TEXT DEFAULT 'approved',
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS pharmacies (
            id SERIAL PRIMARY KEY, system_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            country TEXT, city TEXT, address TEXT, phone TEXT, license_no TEXT,
            license_doc_url TEXT,
            approval_status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS medicines (
            id SERIAL PRIMARY KEY, pharmacy_id INTEGER REFERENCES pharmacies(id) ON DELETE CASCADE,
            name TEXT NOT NULL, brand TEXT, category TEXT, description TEXT,
            dosage TEXT, price NUMERIC(10,2) NOT NULL, stock INTEGER DEFAULT 0,
            requires_prescription BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES patients(id) ON DELETE SET NULL,
            pharmacy_id INTEGER REFERENCES pharmacies(id) ON DELETE SET NULL,
            items JSONB NOT NULL, total NUMERIC(10,2) NOT NULL,
            status TEXT DEFAULT 'pending', delivery_address TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS patient_records (
            id SERIAL PRIMARY KEY, patient_id INTEGER REFERENCES patients(id) ON DELETE CASCADE,
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            chief_complaint TEXT, medical_history TEXT,
            temperature FLOAT, heart_rate FLOAT, spo2 FLOAT,
            systolic INTEGER, diastolic INTEGER, weight FLOAT, height FLOAT, bmi FLOAT,
            fatigue TEXT, raw_json JSONB)""")
        for t, col in [("hospital_sessions","hospital_id"),("reg_doctor_sessions","reg_doctor_id"),
                       ("patient_sessions","patient_id"),("pharmacy_sessions","pharmacy_id")]:
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {t} (
                id SERIAL PRIMARY KEY, {col} INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL, expires_at TIMESTAMPTZ NOT NULL)""")
        cur.execute("ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS username TEXT UNIQUE")
        cur.execute("ALTER TABLE reg_doctors ADD COLUMN IF NOT EXISTS username TEXT UNIQUE")
        cur.execute("ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS username TEXT UNIQUE")
        cur.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS username TEXT")
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_username_unique
                       ON patients (LOWER(username)) WHERE username IS NOT NULL""")
        conn.commit(); cur.close(); conn.close()
        print("DB init OK")
    except Exception as e:
        print(f"DB init error (non-fatal): {e}")


# ══════════════════════════════════════════════════════════
# FIREBASE FCM (original — unchanged)
# ══════════════════════════════════════════════════════════
def get_access_token():
    try:
        if not SERVICE_ACCOUNT_JSON: return None
        js = SERVICE_ACCOUNT_JSON.strip()
        if not js.startswith('{'): js = js[js.find('{'):]
        if not js.endswith('}'): js = js[:js.rfind('}')+1]
        sa = json.loads(js)
        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        print(f"Token error: {e}"); return None


def send_fcm(token, title, body, data):
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
        }}, timeout=10)
    if resp.status_code == 200: return True
    if "UNREGISTERED" in resp.text or resp.status_code == 404:
        for did, d in list(doctors.items()):
            if d.get("token") == token:
                del doctors[did]
    return False


# ══════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════
@app.on_event("startup")
def startup(): init_db()


# ══════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════
@app.get("/")
def health():
    return {"status": "online", "service": "AI Dobot Backend",
            "doctors_online": len(_online_doctors()),
            "firebase_configured": bool(SERVICE_ACCOUNT_JSON)}


# ══════════════════════════════════════════════════════════
# ORIGINAL DOCTOR ROUTES (FCM-based, in-memory)
# ══════════════════════════════════════════════════════════
class DoctorSignIn(BaseModel):
    name: str; specialty: str; avatar: str; token: str
    doctor_id: Optional[str] = None

class DoctorSignOut(BaseModel):
    doctor_id: str

@app.post("/api/doctor/signin")
def doctor_signin(data: DoctorSignIn):
    doctor_id = data.doctor_id or str(uuid.uuid4())[:8]
    doctors[doctor_id] = {
        "name": data.name.strip(), "specialty": data.specialty.strip(),
        "avatar": data.avatar, "token": data.token.strip(),
        "signed_in_at": time.time(), "last_seen": time.time(),
    }
    print(f"Doctor signed in: {data.name} id={doctor_id}")
    return {"status": "success", "doctor_id": doctor_id, "doctors_online": len(_online_doctors())}

@app.post("/api/doctor/heartbeat")
def doctor_heartbeat(data: DoctorSignOut):
    if data.doctor_id in doctors:
        doctors[data.doctor_id]["last_seen"] = time.time()
        return {"status": "ok", "doctors_online": len(_online_doctors())}
    raise HTTPException(404, "Doctor not found — please sign in again")

@app.post("/api/doctor/signout")
def doctor_signout(data: DoctorSignOut):
    if data.doctor_id in doctors:
        del doctors[data.doctor_id]
    return {"status": "success", "doctors_online": len(_online_doctors())}

@app.post("/api/doctor/register-token")
def register_token(data: dict):
    return {"status": "success", "message": "Use /api/doctor/signin instead"}

@app.get("/api/doctors/online")
def get_online_doctors():
    online = _online_doctors()
    safe = [{k: v for k, v in d.items() if k != "token"} for d in online]
    return {"status": "success", "doctors": safe, "count": len(safe)}


# ══════════════════════════════════════════════════════════
# ORIGINAL CALL ROUTES (FCM-based, in-memory)
# ══════════════════════════════════════════════════════════
class PatientCall(BaseModel):
    patient_name: str; patient_id: str; symptom: str
    doctor_id: Optional[str] = None

@app.post("/api/calls/initiate")
def initiate_call(call: PatientCall):
    online = _online_doctors()
    if not online:
        raise HTTPException(503, "No doctors online right now.")
    if not SERVICE_ACCOUNT_JSON:
        raise HTTPException(500, "Firebase not configured on server.")

    call_id = str(uuid.uuid4())[:8]
    video_call_url = f"https://meet.jit.si/ai-dobot-{call_id}"

    targets = (
        [doctors[call.doctor_id]] if call.doctor_id and call.doctor_id in doctors
        else [doctors[d["doctor_id"]] for d in online]
    )

    success = 0
    for d in targets:
        ok = send_fcm(d["token"], "🚨 New Patient Call",
                      f"{call.patient_name} · {call.symptom[:120]}",
                      {"patient_name": call.patient_name, "patient_id": call.patient_id,
                       "symptom": call.symptom, "video_call_url": video_call_url})
        if ok: success += 1

    for d in targets:
        target_id = next((did for did, doc in doctors.items() if doc is d), None)
        if target_id:
            pending_calls[call_id + "_" + target_id] = {
                "doctor_id": target_id, "patient_name": call.patient_name,
                "patient_id": call.patient_id, "symptom": call.symptom,
                "video_call_url": video_call_url, "created_at": time.time(),
            }

    return {"status": "success", "message": f"Notified {success} doctor(s)",
            "video_call_url": video_call_url, "call_id": call_id}

@app.get("/api/calls/pending/{doctor_id}")
def get_pending_calls(doctor_id: str):
    cutoff = time.time() - 120
    my_calls = [{**v, "call_id": k} for k, v in pending_calls.items()
                if v.get("doctor_id") == doctor_id and v.get("created_at", 0) > cutoff]
    return {"calls": my_calls}

@app.delete("/api/calls/pending/{call_id}")
def acknowledge_call(call_id: str):
    pending_calls.pop(call_id, None)
    return {"status": "ok"}

@app.get("/api/calls/ended/{call_id}")
def is_call_ended(call_id: str):
    return {"ended": call_id in ended_calls}

@app.post("/api/calls/end/{call_id}")
def end_call(call_id: str):
    ended_calls[call_id] = time.time()
    pending_calls.pop(call_id, None)
    cutoff = time.time() - 300
    for k in list(ended_calls.keys()):
        if ended_calls[k] < cutoff: del ended_calls[k]
    return {"status": "ended"}


# ══════════════════════════════════════════════════════════
# ADMIN PORTAL (owner only)
# ══════════════════════════════════════════════════════════
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "aidoBot-admin-2026!")

def _is_admin(authorization: str = None):
    return _bearer(authorization) == ADMIN_TOKEN

@app.post("/api/admin/login")
def admin_login(data: dict):
    if data.get("password") == ADMIN_TOKEN:
        return {"ok": True, "token": ADMIN_TOKEN}
    raise HTTPException(401, "Wrong password")

@app.get("/api/admin/pending")
def admin_pending(authorization: Optional[str] = Header(default=None)):
    if not _is_admin(authorization): raise HTTPException(401, "Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,system_id,name,email,country,city,phone,qualifications,license_doc_url,approval_status,created_at FROM reg_doctors ORDER BY approval_status='pending' DESC, created_at DESC")
    docs = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id,system_id,name,email,country,city,phone,license_no,license_doc_url,approval_status,created_at FROM pharmacies ORDER BY approval_status='pending' DESC, created_at DESC")
    pharms = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id,system_id,name,email,country,city,phone,registration_no,approval_status,created_at FROM hospitals ORDER BY created_at DESC")
    hosps = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id,system_id,name,email,country,phone,approval_status,created_at FROM patients ORDER BY created_at DESC")
    pats = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return {"doctors": docs, "pharmacies": pharms, "hospitals": hosps, "patients": pats}

@app.post("/api/admin/approve")
def admin_approve(data: dict, authorization: Optional[str] = Header(default=None)):
    if not _is_admin(authorization): raise HTTPException(401, "Unauthorized")
    entity_type = data.get("type")    # "doctor", "pharmacy", "hospital"
    entity_id   = data.get("id")
    action      = data.get("action", "approved")   # "approved" or "rejected"
    table_map = {"doctor": "reg_doctors", "pharmacy": "pharmacies", "hospital": "hospitals", "patient": "patients"}
    table = table_map.get(entity_type)
    if not table: raise HTTPException(400, "Invalid type")
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"UPDATE {table} SET approval_status=%s WHERE id=%s RETURNING name,email", (action, entity_id))
    row = cur.fetchone()
    if not row: raise HTTPException(404, "Not found")
    conn.commit(); cur.close(); conn.close()
    return {"success": True, "message": f"{row['name']} ({row['email']}) {action}"}

@app.get("/api/admin/stats")
def admin_stats(authorization: Optional[str] = Header(default=None)):
    if not _is_admin(authorization): raise HTTPException(401, "Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT approval_status, COUNT(*) FROM reg_doctors GROUP BY approval_status")
    doc_stats = {r["approval_status"]: r["count"] for r in cur.fetchall()}
    cur.execute("SELECT approval_status, COUNT(*) FROM pharmacies GROUP BY approval_status")
    pharm_stats = {r["approval_status"]: r["count"] for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) as c FROM hospitals")
    hosp_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM patients")
    pat_count = cur.fetchone()["c"]
    cur.close(); conn.close()
    return {
        "doctors": doc_stats, "pharmacies": pharm_stats,
        "hospitals": hosp_count, "patients": pat_count
    }



# ══════════════════════════════════════════════════════════
# NEW: HOSPITAL REGISTRATION (Neon DB)
# ══════════════════════════════════════════════════════════
class LoginReq(BaseModel):
    identifier: str; password: str

class RegDoctorCreate(BaseModel):
    name:str; email:str; password:str; specialty:Optional[str]=None
    username:Optional[str]=None
    country:Optional[str]=None; city:Optional[str]=None; phone:Optional[str]=None
    qualifications:Optional[str]=None; bio:Optional[str]=None
    avatar:Optional[str]="👨‍⚕️"; consultation_fee:Optional[float]=0
    license_doc_url:Optional[str]=None

@app.post("/api/reg_doctors/register")
def reg_doctor_register(d: RegDoctorCreate):
    sid = _gen_id("DOC"); conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO reg_doctors
            (system_id,name,email,username,password_hash,specialty,country,city,phone,qualifications,bio,avatar,consultation_fee,license_doc_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,system_id""",
            (sid,d.name,d.email,(d.username or "").strip() or None,_hash(d.password),d.specialty,d.country,d.city,d.phone,
             d.qualifications,d.bio,d.avatar,d.consultation_fee,d.license_doc_url))
        row = cur.fetchone(); conn.commit()
        return {"success":True,"system_id":row["system_id"],
                "message":"Registration submitted. Please wait for admin approval before signing in."}
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); raise HTTPException(400,"Email already registered")
    finally: cur.close(); conn.close()

@app.post("/api/reg_doctors/login")
def reg_doctor_login(d: LoginReq):
    ident = (d.identifier or "").strip()
    if not ident:
        raise HTTPException(400,"Identifier is required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT * FROM reg_doctors
                   WHERE password_hash=%s
                     AND (LOWER(email)=LOWER(%s) OR LOWER(COALESCE(username,''))=LOWER(%s) OR phone=%s)
                   LIMIT 1""",(_hash(d.password),ident,ident,ident))
    doc = cur.fetchone(); cur.close(); conn.close()
    if not doc: raise HTTPException(401,"Invalid credentials")
    if doc.get("approval_status","pending") == "pending":
        raise HTTPException(403,"Your registration is pending admin approval. You will be notified when approved.")
    if doc.get("approval_status") == "rejected":
        raise HTTPException(403,"Your registration was not approved. Please contact support.")
    tok = _session("reg_doctor_sessions","reg_doctor_id",doc["id"])
    r = dict(doc); r.pop("password_hash",None)
    return {"success":True,"token":tok,"doctor":r}

@app.post("/api/reg_doctors/login_by_sysid")
def reg_doctor_login_sysid(data: dict):
    name   = (data.get("name") or "").strip().lower()
    sys_id = (data.get("system_id") or "").strip().upper()
    if not name or not sys_id: raise HTTPException(400,"Name and System ID required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM reg_doctors WHERE system_id=%s",(sys_id,))
    doc = cur.fetchone(); cur.close(); conn.close()
    if not doc: raise HTTPException(404,"System ID not found. Please check your ID.")
    if doc["name"].strip().lower() != name:
        raise HTTPException(401,"Name does not match our records for this System ID.")
    if doc.get("approval_status","pending") == "pending":
        raise HTTPException(403,"Your registration is pending admin approval.")
    if doc.get("approval_status") == "rejected":
        raise HTTPException(403,"Your registration was not approved. Please contact support.")
    tok = _session("reg_doctor_sessions","reg_doctor_id",doc["id"])
    r = dict(doc); r.pop("password_hash",None)
    return {"success":True,"token":tok,"doctor":r,"specialty":doc.get("specialty","")}

@app.get("/api/reg_doctors/me")
def reg_doctor_me(authorization: Optional[str] = Header(default=None)):
    sess = _verify("reg_doctor_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,system_id,name,email,specialty,country,city,phone,qualifications,bio,avatar,consultation_fee,approval_status FROM reg_doctors WHERE id=%s",(sess["reg_doctor_id"],))
    doc = cur.fetchone(); cur.close(); conn.close()
    return dict(doc) if doc else {}


class HospReg(BaseModel):
    name:str; email:str; password:str
    username:Optional[str]=None
    country:Optional[str]=None; city:Optional[str]=None
    address:Optional[str]=None; phone:Optional[str]=None; registration_no:Optional[str]=None

@app.post("/api/hospitals/register")
def hosp_register(d: HospReg):
    sid = _gen_id("HSP"); conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO hospitals (system_id,name,email,username,password_hash,country,city,address,phone,registration_no)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,system_id""",
            (sid,d.name,d.email,(d.username or "").strip() or None,_hash(d.password),d.country,d.city,d.address,d.phone,d.registration_no))
        row = cur.fetchone(); conn.commit()
        tok = _session("hospital_sessions","hospital_id",row["id"])
        return {"success":True,"system_id":row["system_id"],"token":tok,
                "message":"Registration submitted. Please wait for admin approval before signing in."}
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); raise HTTPException(400,"Email already registered")
    finally: cur.close(); conn.close()

@app.post("/api/hospitals/login")
def hosp_login(d: LoginReq):
    ident = (d.identifier or "").strip()
    if not ident:
        raise HTTPException(400,"Identifier is required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT * FROM hospitals
                   WHERE password_hash=%s
                     AND (LOWER(email)=LOWER(%s) OR LOWER(COALESCE(username,''))=LOWER(%s) OR phone=%s)
                   LIMIT 1""",(_hash(d.password),ident,ident,ident))
    h = cur.fetchone(); cur.close(); conn.close()
    if not h: raise HTTPException(401,"Invalid credentials")
    if h.get("approval_status","approved") == "pending":
        raise HTTPException(403,"Your registration is pending admin approval. You will be notified when approved.")
    if h.get("approval_status") == "rejected":
        raise HTTPException(403,"Your registration was not approved. Please contact support.")
    tok = _session("hospital_sessions","hospital_id",h["id"])
    r = dict(h); r.pop("password_hash",None)
    return {"success":True,"token":tok,"hospital":r}

@app.post("/api/hospitals/login_by_sysid")
def hosp_login_sysid(data: dict):
    name   = (data.get("name") or "").strip().lower()
    sys_id = (data.get("system_id") or "").strip().upper()
    if not name or not sys_id: raise HTTPException(400,"Hospital name and System ID required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM hospitals WHERE system_id=%s",(sys_id,))
    h = cur.fetchone(); cur.close(); conn.close()
    if not h: raise HTTPException(404,"System ID not found.")
    if h["name"].strip().lower() != name:
        raise HTTPException(401,"Hospital name does not match our records for this System ID.")
    if h.get("approval_status","approved") == "pending":
        raise HTTPException(403,"Your hospital registration is pending admin approval.")
    if h.get("approval_status") == "rejected":
        raise HTTPException(403,"Your registration was not approved.")
    tok = _session("hospital_sessions","hospital_id",h["id"])
    r = dict(h); r.pop("password_hash",None)
    return {"success":True,"token":tok,"hospital":r}

@app.get("/api/hospitals/me")
def hosp_me(authorization: Optional[str] = Header(default=None)):
    sess = _verify("hospital_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,system_id,name,email,country,city,address,phone,registration_no FROM hospitals WHERE id=%s",(sess["hospital_id"],))
    h = cur.fetchone(); cur.close(); conn.close()
    return dict(h) if h else {}

@app.get("/api/hospitals/doctors")
def hosp_doctors(authorization: Optional[str] = Header(default=None)):
    sess = _verify("hospital_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,system_id,name,specialty,avatar,country,city,is_online FROM reg_doctors WHERE hospital_id=%s ORDER BY name",(sess["hospital_id"],))
    rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return {"doctors":rows}

@app.post("/api/hospitals/add-doctor")
def hosp_add_doctor(data: dict, authorization: Optional[str] = Header(default=None)):
    sess = _verify("hospital_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE reg_doctors SET hospital_id=%s,is_public=FALSE WHERE system_id=%s RETURNING name",(sess["hospital_id"],data.get("doctor_system_id","")))
    doc = cur.fetchone()
    if not doc: raise HTTPException(404,"Doctor not found")
    conn.commit(); cur.close(); conn.close()
    return {"success":True,"message":f"Dr. {doc['name']} added to hospital"}

@app.post("/api/hospitals/remove-doctor")
def hosp_remove_doctor(data: dict, authorization: Optional[str] = Header(default=None)):
    sess = _verify("hospital_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE reg_doctors SET hospital_id=NULL,is_public=TRUE WHERE system_id=%s AND hospital_id=%s RETURNING name",(data.get("doctor_system_id",""),sess["hospital_id"]))
    doc = cur.fetchone()
    if not doc: raise HTTPException(404,"Not found in your hospital")
    conn.commit(); cur.close(); conn.close()
    return {"success":True}


# ══════════════════════════════════════════════════════════
# NEW: PATIENT REGISTRATION (Neon DB)
# ══════════════════════════════════════════════════════════
class PatientSetup(BaseModel):
    system_id:str; password:str
    name:Optional[str]=None; email:Optional[str]=None
    username:Optional[str]=None
    phone:Optional[str]=None; country:Optional[str]=None; date_of_birth:Optional[str]=None

class PatientLogin(BaseModel):
    identifier:str; password:str

@app.post("/api/patients/create-from-robot")
def patient_create(data: dict):
    sid = _gen_id("USR"); conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO patients (system_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id,system_id",(sid,))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if row:
        return {"success":True,"system_id":row["system_id"]}
    return {"success":True,"system_id":sid}

@app.post("/api/patients/setup")
def patient_setup(d: PatientSetup):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,password_hash FROM patients WHERE system_id=%s",(d.system_id,))
    u = cur.fetchone()
    if not u: raise HTTPException(404,"System ID not found. Complete a robot Q&A session first.")
    if u["password_hash"]: raise HTTPException(400,"Account already activated. Please log in.")
    dob = None
    if d.date_of_birth:
        try:
            from datetime import datetime
            dob = datetime.strptime(d.date_of_birth,"%Y-%m-%d").date()
        except: pass
    cur.execute("UPDATE patients SET password_hash=%s,name=%s,email=%s,username=%s,phone=%s,country=%s,date_of_birth=%s WHERE id=%s",
                (_hash(d.password),d.name,d.email,(d.username or "").strip() or None,d.phone,d.country,dob,u["id"]))
    conn.commit()
    tok = _session("patient_sessions","patient_id",u["id"])
    cur.close(); conn.close()
    return {"success":True,"token":tok}

@app.post("/api/patients/login")
def patient_login(d: PatientLogin):
    ident = (d.identifier or "").strip()
    if not ident:
        raise HTTPException(400,"Identifier is required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT * FROM patients
                   WHERE password_hash=%s
                     AND (LOWER(COALESCE(email,''))=LOWER(%s)
                          OR LOWER(COALESCE(username,''))=LOWER(%s)
                          OR phone=%s
                          OR system_id=%s)
                   ORDER BY created_at DESC LIMIT 1""",
                (_hash(d.password),ident,ident,ident,ident.upper()))
    u = cur.fetchone(); cur.close(); conn.close()
    if not u: raise HTTPException(401,"Invalid identifier or password")
    if u.get("approval_status","approved") == "pending":
        raise HTTPException(403,"Your registration is pending admin approval.")
    if u.get("approval_status") == "rejected":
        raise HTTPException(403,"Your registration was not approved.")
    tok = _session("patient_sessions","patient_id",u["id"])
    r = dict(u); r.pop("password_hash",None)
    return {"success":True,"token":tok,"patient":r}

@app.post("/api/patients/login_by_sysid")
def patient_login_sysid(data: dict):
    name   = (data.get("name") or "").strip().lower()
    sys_id = (data.get("system_id") or "").strip().upper()
    if not name or not sys_id: raise HTTPException(400,"Name and System ID required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM patients WHERE system_id=%s",(sys_id,))
    u = cur.fetchone(); cur.close(); conn.close()
    if not u: raise HTTPException(404,"System ID not found. Complete a robot Q&A session first.")
    # If name is set, verify it — otherwise accept (first login after robot session)
    if u.get("name") and u["name"].strip().lower() != name:
        raise HTTPException(401,"Name does not match our records for this System ID.")
    # Update name if not set yet
    if not u.get("name"):
        conn2 = get_conn(); cur2 = conn2.cursor()
        cur2.execute("UPDATE patients SET name=%s WHERE id=%s",(data.get("name"),u["id"]))
        conn2.commit(); cur2.close(); conn2.close()
    tok = _session("patient_sessions","patient_id",u["id"])
    r = dict(u); r.pop("password_hash",None)
    return {"success":True,"token":tok,"patient":r}

@app.get("/api/patients/me")
def patient_me(authorization: Optional[str] = Header(default=None)):
    sess = _verify("patient_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,system_id,name,email,phone,country,date_of_birth FROM patients WHERE id=%s",(sess["patient_id"],))
    u = cur.fetchone(); cur.close(); conn.close()
    return dict(u) if u else {}

@app.get("/api/patients/records")
def patient_records(authorization: Optional[str] = Header(default=None)):
    sess = _verify("patient_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM patient_records WHERE patient_id=%s ORDER BY timestamp DESC",(sess["patient_id"],))
    rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return {"records":rows}

@app.post("/api/patients/set-personal-doctor")
def set_personal_doctor(data: dict, authorization: Optional[str] = Header(default=None)):
    sess = _verify("patient_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,name FROM reg_doctors WHERE system_id=%s",(data.get("doctor_system_id",""),))
    doc = cur.fetchone()
    if not doc: raise HTTPException(404,"Doctor not found")
    cur.execute("UPDATE patients SET personal_doctor_id=%s WHERE id=%s",(doc["id"],sess["patient_id"]))
    conn.commit(); cur.close(); conn.close()
    return {"success":True,"message":f"Dr. {doc['name']} set as your personal doctor"}


# ══════════════════════════════════════════════════════════
# NEW: PHARMACY + MEDICINES + SHOP (Neon DB)
# ══════════════════════════════════════════════════════════
class PharmReg(BaseModel):
    name:str; email:str; password:str
    username:Optional[str]=None
    country:Optional[str]=None; city:Optional[str]=None
    address:Optional[str]=None; phone:Optional[str]=None; license_no:Optional[str]=None
    license_doc_url:Optional[str]=None

@app.post("/api/pharmacies/register")
def pharm_register(d: PharmReg):
    sid = _gen_id("PHM"); conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO pharmacies (system_id,name,email,username,password_hash,country,city,address,phone,license_no,license_doc_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,system_id""",
            (sid,d.name,d.email,(d.username or "").strip() or None,_hash(d.password),d.country,d.city,d.address,d.phone,d.license_no,getattr(d,'license_doc_url',None)))
        row = cur.fetchone(); conn.commit()
        tok = _session("pharmacy_sessions","pharmacy_id",row["id"])
        return {"success":True,"system_id":row["system_id"],"token":tok}
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); raise HTTPException(400,"Email already registered")
    finally: cur.close(); conn.close()

@app.post("/api/pharmacies/login")
def pharm_login(d: LoginReq):
    ident = (d.identifier or "").strip()
    if not ident:
        raise HTTPException(400,"Identifier is required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT * FROM pharmacies
                   WHERE password_hash=%s
                     AND (LOWER(email)=LOWER(%s) OR LOWER(COALESCE(username,''))=LOWER(%s) OR phone=%s)
                   LIMIT 1""",(_hash(d.password),ident,ident,ident))
    p = cur.fetchone(); cur.close(); conn.close()
    if not p: raise HTTPException(401,"Invalid credentials")
    if p.get("approval_status","pending") == "pending":
        raise HTTPException(403,"Your pharmacy registration is pending admin approval.")
    if p.get("approval_status") == "rejected":
        raise HTTPException(403,"Your pharmacy registration was not approved.")
    tok = _session("pharmacy_sessions","pharmacy_id",p["id"])
    r = dict(p); r.pop("password_hash",None)
    return {"success":True,"token":tok,"pharmacy":r}

@app.post("/api/pharmacies/login_by_sysid")
def pharm_login_sysid(data: dict):
    name   = (data.get("name") or "").strip().lower()
    sys_id = (data.get("system_id") or "").strip().upper()
    if not name or not sys_id: raise HTTPException(400,"Pharmacy name and System ID required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM pharmacies WHERE system_id=%s",(sys_id,))
    p = cur.fetchone(); cur.close(); conn.close()
    if not p: raise HTTPException(404,"System ID not found.")
    if p["name"].strip().lower() != name:
        raise HTTPException(401,"Pharmacy name does not match our records for this System ID.")
    if p.get("approval_status","pending") == "pending":
        raise HTTPException(403,"Your pharmacy is pending admin approval.")
    if p.get("approval_status") == "rejected":
        raise HTTPException(403,"Your registration was not approved.")
    tok = _session("pharmacy_sessions","pharmacy_id",p["id"])
    r = dict(p); r.pop("password_hash",None)
    return {"success":True,"token":tok,"pharmacy":r}

@app.get("/api/pharmacies/me")
def pharm_me(authorization: Optional[str] = Header(default=None)):
    sess = _verify("pharmacy_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id,system_id,name,email,country,city,address,phone,license_no FROM pharmacies WHERE id=%s",(sess["pharmacy_id"],))
    p = cur.fetchone(); cur.close(); conn.close()
    return dict(p) if p else {}

class MedCreate(BaseModel):
    name:str; brand:Optional[str]=None; category:Optional[str]=None
    description:Optional[str]=None; dosage:Optional[str]=None
    price:float; stock:int=0; requires_prescription:bool=False

@app.post("/api/pharmacies/medicines")
def add_medicine(d: MedCreate, authorization: Optional[str] = Header(default=None)):
    sess = _verify("pharmacy_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO medicines (pharmacy_id,name,brand,category,description,dosage,price,stock,requires_prescription)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (sess["pharmacy_id"],d.name,d.brand,d.category,d.description,d.dosage,d.price,d.stock,d.requires_prescription))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return {"success":True,"medicine_id":row["id"]}

@app.get("/api/pharmacies/medicines")
def my_medicines(authorization: Optional[str] = Header(default=None)):
    sess = _verify("pharmacy_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM medicines WHERE pharmacy_id=%s ORDER BY name",(sess["pharmacy_id"],))
    rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return {"medicines":rows}

@app.delete("/api/pharmacies/medicines/{med_id}")
def delete_medicine(med_id: int, authorization: Optional[str] = Header(default=None)):
    sess = _verify("pharmacy_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM medicines WHERE id=%s AND pharmacy_id=%s",(med_id,sess["pharmacy_id"]))
    conn.commit(); cur.close(); conn.close()
    return {"success":True}

@app.get("/api/shop/medicines")
def shop_medicines(country: str = None, category: str = None, search: str = None, page: int = 1):
    conn = get_conn(); cur = conn.cursor()
    q = """SELECT m.*,ph.name as pharmacy_name,ph.city as pharmacy_city,ph.country as pharmacy_country
           FROM medicines m JOIN pharmacies ph ON m.pharmacy_id=ph.id WHERE m.stock>0"""
    p = []
    if country: q += " AND ph.country ILIKE %s"; p.append(f"%{country}%")
    if category: q += " AND m.category ILIKE %s"; p.append(f"%{category}%")
    if search: q += " AND (m.name ILIKE %s OR m.brand ILIKE %s)"; p.extend([f"%{search}%",f"%{search}%"])
    q += f" ORDER BY m.name LIMIT 20 OFFSET {(page-1)*20}"
    cur.execute(q,p); rows = [dict(r) for r in cur.fetchall()]; cur.close(); conn.close()
    return {"medicines":rows,"page":page}

@app.post("/api/shop/order")
def place_order(data: dict, authorization: Optional[str] = Header(default=None)):
    sess = _verify("patient_sessions",_bearer(authorization))
    patient_id = sess["patient_id"] if sess else None
    items = data.get("items",[])
    if not items: raise HTTPException(400,"No items in order")
    total = sum(i.get("price",0) * i.get("qty",1) for i in items)
    pharmacy_id = items[0].get("pharmacy_id") if items else None
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO orders (patient_id,pharmacy_id,items,total,delivery_address) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (patient_id,pharmacy_id,psycopg2.extras.Json(items),total,data.get("delivery_address","")))
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return {"success":True,"order_id":row["id"],"total":total}


# ══════════════════════════════════════════════════════════
# START SERVER
# ══════════════════════════════════════════════════════════
import uvicorn
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
