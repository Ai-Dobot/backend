"""
backend.py — AI Dobot Telemedicine Backend
==========================================
Deploy this single file to Render.

ENVIRONMENT VARIABLES:
  FIREBASE_SERVICE_ACCOUNT  → paste full Firebase service account JSON
  FIREBASE_PROJECT_ID       → ai-dobot
  DATABASE_URL              → Neon PostgreSQL connection string
  CLOUDINARY_CLOUD_NAME     → Cloudinary cloud name (medicine images)
  CLOUDINARY_API_KEY        → Cloudinary API key
  CLOUDINARY_API_SECRET     → Cloudinary API secret

HOW IT CONNECTS:
  Doctor app (GitHub Pages) ──sign in──► POST /api/doctor/signin
  Patient robot ─────────────────────► GET  /api/doctors/online
  Patient robot ──────call──────────► POST /api/calls/initiate  ← fires FCM to doctor
  Hospital/Patient/Pharmacy portals ► /api/hospitals /api/patients /api/pharmacies
"""

from fastapi import FastAPI, HTTPException, Header, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
import requests, os, json, uuid, time, re
# Auto-install deps if not present (Render / minimal requirements.txt)
import subprocess, sys
try:
    import multipart  # noqa: F401 — from package python-multipart (FastAPI UploadFile)
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'python-multipart', '-q'])
try:
    import psycopg2
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'psycopg2-binary', '-q'])
import psycopg2, psycopg2.extras, hashlib, secrets, string, random
try:
    import cloudinary
    import cloudinary.uploader
except ImportError:
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'cloudinary', '-q'])
    import cloudinary
    import cloudinary.uploader
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
pending_hospital_calls: Dict[str, dict] = {}
ended_calls:   Dict[str, float] = {}


def _online_doctors():
    cutoff = time.time() - 90
    return [
        {**d, "doctor_id": did, "available": True}
        for did, d in doctors.items()
        if d.get("last_seen", 0) > cutoff
    ]

def _norm_name(v: str) -> str:
    v = (v or "").strip().lower()
    v = re.sub(r"^dr\.?\s+", "", v)
    return re.sub(r"\s+", " ", v)

def _norm_phone(v: str) -> str:
    return re.sub(r"\D+", "", (v or ""))

def _personal_name_match(saved_norm: str, doc_name: str) -> bool:
    """
    For Personal Doctor (name + phone): avoid substring false positives
    (e.g. 'hiv kumar verma' must not match 'shiv kumar verma').
    Requires exact normalized full name OR every saved word to appear as a whole token in the doctor name.
    """
    s = (saved_norm or "").strip()
    d = _norm_name(doc_name)
    if not s:
        return True
    if s == d:
        return True
    st = set(s.split())
    dt = set(d.split())
    if not st:
        return False
    return st <= dt

def _hydrate_online_doctor(doc: dict) -> dict:
    # If live in-memory presence lacks profile metadata, enrich from DB by name.
    if (doc.get("country") and doc.get("phone")) or not doc.get("name"):
        return doc
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT rd.country, rd.city, rd.phone, rd.hospital_id,
                   h.name AS hospital_name, h.registration_no AS hospital_registration_no
            FROM reg_doctors rd
            LEFT JOIN hospitals h ON rd.hospital_id = h.id
            WHERE LOWER(rd.name)=LOWER(%s)
            ORDER BY CASE
              WHEN rd.approval_status='approved' THEN 0
              WHEN rd.approval_status='pending' THEN 1
              ELSE 2
            END, rd.created_at DESC
            LIMIT 1
        """, (doc.get("name",""),))
        row = cur.fetchone()
        cur.close(); conn.close()
        if not row:
            return doc
        d = dict(doc)
        if not d.get("country"): d["country"] = row.get("country") or ""
        if not d.get("city"): d["city"] = row.get("city") or ""
        if not d.get("phone"): d["phone"] = row.get("phone") or ""
        if not d.get("hospital_name"): d["hospital_name"] = row.get("hospital_name") or ""
        if not d.get("hospital_registration_no"): d["hospital_registration_no"] = row.get("hospital_registration_no") or ""
        d["is_private"] = row.get("hospital_id") is None
        return d
    except Exception:
        return doc


# ══════════════════════════════════════════════════════════
# NEON DB (new registration system)
# ══════════════════════════════════════════════════════════
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()
def _gen_id(prefix): return prefix + '-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
def _tok(): return secrets.token_urlsafe(32)
def _bearer(h): return (h or "").replace("Bearer ", "")
def _unique_msg(err):
    msg = (str(err) or "").lower()
    if "username" in msg:
        return "Username already taken"
    if "system_id" in msg:
        return "Account code conflict, please retry"
    return "Duplicate value found for a unique field"

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

def _cloudinary_upload_bytes(data: bytes, folder: str = "medicines") -> str:
    cn = os.getenv("CLOUDINARY_CLOUD_NAME")
    ak = os.getenv("CLOUDINARY_API_KEY")
    sec = os.getenv("CLOUDINARY_API_SECRET")
    if not cn or not ak or not sec:
        raise HTTPException(
            503,
            "Image upload not configured. Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET on Render.",
        )
    cloudinary.config(cloud_name=cn, api_key=ak, api_secret=sec)
    try:
        res = cloudinary.uploader.upload(data, folder=folder, resource_type="image")
    except Exception as e:
        raise HTTPException(500, f"Cloudinary upload failed: {e!s}")
    url = (res.get("secure_url") or res.get("url") or "").strip()
    if not url:
        raise HTTPException(500, "Cloudinary returned no image URL")
    return url


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
        cur.execute("""
            DO $$
            DECLARE c RECORD;
            BEGIN
                FOR c IN
                    SELECT tc.table_name, tc.constraint_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.constraint_column_usage ccu
                      ON tc.constraint_name = ccu.constraint_name
                     AND tc.table_schema = ccu.table_schema
                    WHERE tc.table_schema = 'public'
                      AND tc.constraint_type = 'UNIQUE'
                      AND ccu.column_name = 'email'
                      AND tc.table_name IN ('hospitals','reg_doctors','pharmacies')
                LOOP
                    EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', c.table_name, c.constraint_name);
                END LOOP;
            END $$;
        """)
        # Pharmacy / shop / medicine image (migrations for existing DBs)
        cur.execute("ALTER TABLE medicines ADD COLUMN IF NOT EXISTS image_url TEXT")
        cur.execute("ALTER TABLE medicines ADD COLUMN IF NOT EXISTS diagnosis TEXT")
        cur.execute("ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS region TEXT")
        cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_email TEXT")
        cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name TEXT")
        cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_phone TEXT")
        cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status TEXT DEFAULT 'pending'")
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
def seed_demo_boxing_pharmacy():
    """
    Demo pharmacy + ~20 sample medicines (external image URLs) for testing.
    Disable: set env SEED_BOXING_DEMO=0
    Sign in: email boxing.demo@sample-delete.local  password SampleBoxing2024!
    """
    if os.getenv("SEED_BOXING_DEMO", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    DEMO_EMAIL = "boxing.demo@sample-delete.local"
    DEMO_SYSTEM = "PHM-BOXINGDEMO"
    SAMPLE_MEDS = [
        {"name": "Paracetamol 500mg", "brand": "Boxing Relief", "category": "Pain Relief", "description": "Pain and fever relief.", "dosage": "1–2 tablets every 4–6h", "diagnosis": "Headache, mild pain, fever", "price": 4.99, "stock": 200, "rx": False, "img": "https://images.unsplash.com/photo-1584308666744-24d5c474e870?w=400&q=80&auto=format&fit=crop"},
        {"name": "Ibuprofen 200mg", "brand": "Boxing Relief", "category": "Pain Relief", "description": "Anti-inflammatory pain relief.", "dosage": "1 tablet every 6–8h with food", "diagnosis": "Inflammation, muscle pain", "price": 6.49, "stock": 150, "rx": False, "img": "https://images.unsplash.com/photo-1587854692152-cbe660dbde88?w=400&q=80&auto=format&fit=crop"},
        {"name": "Vitamin D3 1000 IU", "brand": "SunBox", "category": "Vitamins", "description": "Daily vitamin D support.", "dosage": "1 softgel daily", "diagnosis": "Vitamin D deficiency support", "price": 12.99, "stock": 120, "rx": False, "img": "https://images.unsplash.com/photo-1550572017-edd951aa8f45?w=400&q=80&auto=format&fit=crop"},
        {"name": "Multivitamin Adults", "brand": "VitalBox", "category": "Vitamins", "description": "Broad-spectrum daily vitamins.", "dosage": "1 tablet daily with meal", "diagnosis": "General wellness", "price": 18.5, "stock": 90, "rx": False, "img": "https://images.unsplash.com/photo-1471864195440-6b9b8d524493?w=400&q=80&auto=format&fit=crop"},
        {"name": "Cetirizine 10mg", "brand": "AllerBox", "category": "Other", "description": "24h allergy relief.", "dosage": "1 tablet daily", "diagnosis": "Allergic rhinitis, hives", "price": 9.25, "stock": 180, "rx": False, "img": "https://images.unsplash.com/photo-1584308666744-24d5c474e870?w=400&q=80&auto=format&fit=crop"},
        {"name": "Loratadine 10mg", "brand": "AllerBox", "category": "Other", "description": "Non-drowsy antihistamine.", "dosage": "1 tablet daily", "diagnosis": "Seasonal allergies", "price": 8.75, "stock": 160, "rx": False, "img": "https://images.unsplash.com/photo-1631549916766-7429e6950018?w=400&q=80&auto=format&fit=crop"},
        {"name": "Omeprazole 20mg", "brand": "AcidBox", "category": "Other", "description": "Reduces stomach acid.", "dosage": "1 capsule before breakfast", "diagnosis": "Acid reflux, GERD (OTC use)", "price": 14.0, "stock": 100, "rx": False, "img": "https://images.unsplash.com/photo-1587854692152-cbe660dbde88?w=400&q=80&auto=format&fit=crop"},
        {"name": "Saline Nasal Spray", "brand": "ClearBox", "category": "Cold & Flu", "description": "Sterile saline rinse.", "dosage": "2–6 sprays per nostril", "diagnosis": "Dry nose, congestion rinse", "price": 5.5, "stock": 220, "rx": False, "img": "https://images.unsplash.com/photo-1583947215259-38e31be8751f?w=400&q=80&auto=format&fit=crop"},
        {"name": "Hydrocortisone Cream 1%", "brand": "SkinBox", "category": "Skin", "description": "Mild steroid cream for irritation.", "dosage": "Thin layer 2–3x daily", "diagnosis": "Eczema, insect bites", "price": 7.99, "stock": 85, "rx": False, "img": "https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=400&q=80&auto=format&fit=crop"},
        {"name": "Aspirin 81mg Low Dose", "brand": "CardioBox", "category": "Heart", "description": "Low-dose aspirin.", "dosage": "As directed by physician", "diagnosis": "Cardiovascular prevention (Rx guidance)", "price": 6.0, "stock": 300, "rx": False, "img": "https://images.unsplash.com/photo-1584308666744-24d5c474e870?w=400&q=80&auto=format&fit=crop"},
        {"name": "Amoxicillin 500mg", "brand": "PharmaBox", "category": "Antibiotics", "description": "Antibiotic — prescription only.", "dosage": "Per prescription", "diagnosis": "Bacterial infections", "price": 22.0, "stock": 60, "rx": True, "img": "https://images.unsplash.com/photo-1471864195440-6b9b8d524493?w=400&q=80&auto=format&fit=crop"},
        {"name": "Azithromycin 250mg", "brand": "PharmaBox", "category": "Antibiotics", "description": "Macrolide antibiotic.", "dosage": "Per prescription", "diagnosis": "Respiratory bacterial infection", "price": 28.5, "stock": 45, "rx": True, "img": "https://images.unsplash.com/photo-1587854692152-cbe660dbde88?w=400&q=80&auto=format&fit=crop"},
        {"name": "Metformin 500mg", "brand": "DiabetBox", "category": "Diabetes", "description": "Blood sugar management.", "dosage": "Per prescription", "diagnosis": "Type 2 diabetes", "price": 15.75, "stock": 110, "rx": True, "img": "https://images.unsplash.com/photo-1579684385127-1ef15d508118?w=400&q=80&auto=format&fit=crop"},
        {"name": "Atorvastatin 20mg", "brand": "LipidBox", "category": "Heart", "description": "Cholesterol management.", "dosage": "Per prescription", "diagnosis": "High cholesterol", "price": 19.99, "stock": 70, "rx": True, "img": "https://images.unsplash.com/photo-1550572017-edd951aa8f45?w=400&q=80&auto=format&fit=crop"},
        {"name": "Electrolyte Powder", "brand": "HydroBox", "category": "Vitamins", "description": "Rehydration mix.", "dosage": "1 sachet in 200ml water", "diagnosis": "Dehydration, sports recovery", "price": 11.25, "stock": 140, "rx": False, "img": "https://images.unsplash.com/photo-1514996937319-344454492b37?w=400&q=80&auto=format&fit=crop"},
        {"name": "Zinc Lozenges", "brand": "ImmunoBox", "category": "Cold & Flu", "description": "Immune support lozenges.", "dosage": "1 lozenge every 2h when needed", "diagnosis": "Cold symptom support", "price": 8.99, "stock": 95, "rx": False, "img": "https://images.unsplash.com/photo-1584308666744-24d5c474e870?w=400&q=80&auto=format&fit=crop"},
        {"name": "Antiseptic Solution 500ml", "brand": "FirstBox", "category": "Other", "description": "Wound cleansing.", "dosage": "Apply to clean wound", "diagnosis": "Minor cuts and grazes", "price": 6.75, "stock": 75, "rx": False, "img": "https://images.unsplash.com/photo-1583947215259-38e31be8751f?w=400&q=80&auto=format&fit=crop"},
        {"name": "Digital Thermometer", "brand": "TempBox", "category": "Other", "description": "Fast oral/axillary reading.", "dosage": "N/A", "diagnosis": "Fever monitoring", "price": 24.99, "stock": 40, "rx": False, "img": "https://images.unsplash.com/photo-1584036561566-baf8f0f1d1cc?w=400&q=80&auto=format&fit=crop"},
        {"name": "Hand Sanitizer 500ml", "brand": "CleanBox", "category": "Skin", "description": "70% alcohol gel.", "dosage": "Rub hands until dry", "diagnosis": "Hygiene", "price": 7.25, "stock": 250, "rx": False, "img": "https://images.unsplash.com/photo-1584483766114-3cea5b5bab87?w=400&q=80&auto=format&fit=crop"},
        {"name": "Probiotic Capsules", "brand": "GutBox", "category": "Vitamins", "description": "Digestive flora support.", "dosage": "1 capsule daily", "diagnosis": "Digestive wellness", "price": 21.5, "stock": 65, "rx": False, "img": "https://images.unsplash.com/photo-1550572017-edd951aa8f45?w=400&q=80&auto=format&fit=crop"},
    ]
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM pharmacies WHERE email=%s OR system_id=%s", (DEMO_EMAIL, DEMO_SYSTEM))
        row = cur.fetchone()
        if row:
            pid = row["id"]
        else:
            cur.execute(
                """INSERT INTO pharmacies (system_id,name,email,username,password_hash,country,region,city,address,phone,license_no,license_doc_url,approval_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'approved') RETURNING id""",
                (
                    DEMO_SYSTEM,
                    "Boxing Pharmacy",
                    DEMO_EMAIL,
                    None,
                    _hash("SampleBoxing2024!"),
                    "United States",
                    "California",
                    "Los Angeles",
                    "123 Demo Street (sample — delete later)",
                    "+1-555-010-BOX",
                    "DEMO-LIC-BOXING",
                    "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
                ),
            )
            pid = cur.fetchone()["id"]
        cur.execute("SELECT COUNT(*) AS c FROM medicines WHERE pharmacy_id=%s", (pid,))
        have = int(cur.fetchone()["c"] or 0)
        need = max(0, 20 - have)
        if need == 0:
            conn.commit()
            return
        start = have
        for i in range(need):
            m = SAMPLE_MEDS[start + i]
            cur.execute(
                """INSERT INTO medicines (pharmacy_id,name,brand,category,description,dosage,diagnosis,price,stock,requires_prescription,image_url)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    pid,
                    m["name"],
                    m["brand"],
                    m["category"],
                    m["description"],
                    m["dosage"],
                    m["diagnosis"],
                    m["price"],
                    m["stock"],
                    m["rx"],
                    m["img"],
                ),
            )
        conn.commit()
        print(f"Demo Boxing Pharmacy seeded: pharmacy_id={pid}, added {need} sample medicines.")
    except Exception as e:
        conn.rollback()
        print(f"Demo seed skipped: {e}")
    finally:
        cur.close()
        conn.close()


@app.on_event("startup")
def startup():
    init_db()
    try:
        seed_demo_boxing_pharmacy()
    except Exception as e:
        print(f"startup seed (non-fatal): {e}")


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
    country: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None
    hospital_name: Optional[str] = None
    hospital_registration_no: Optional[str] = None
    is_private: Optional[bool] = True

class DoctorSignOut(BaseModel):
    doctor_id: str

@app.post("/api/doctor/signin")
def doctor_signin(data: DoctorSignIn):
    doctor_id = data.doctor_id or str(uuid.uuid4())[:8]
    doctors[doctor_id] = {
        "name": data.name.strip(), "specialty": data.specialty.strip(),
        "avatar": data.avatar, "token": data.token.strip(),
        "country": (data.country or "").strip(),
        "city": (data.city or "").strip(),
        "phone": (data.phone or "").strip(),
        "hospital_name": (data.hospital_name or "").strip(),
        "hospital_registration_no": (data.hospital_registration_no or "").strip(),
        "is_private": bool(data.is_private if data.is_private is not None else True),
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
def get_online_doctors(
    country: Optional[str] = None,
    city: Optional[str] = None,
    doctor_name: Optional[str] = None,
    doctor_phone: Optional[str] = None,
    hospital_name: Optional[str] = None,
    hospital_reg_no: Optional[str] = None,
    only_private: Optional[bool] = None
):
    online = [_hydrate_online_doctor(d) for d in _online_doctors()]
    c = (country or "").strip().lower()
    ci = (city or "").strip().lower()
    dn = _norm_name(doctor_name or "")
    dp = _norm_phone(doctor_phone or "")
    hn = (hospital_name or "").strip().lower()
    hr = (hospital_reg_no or "").strip().lower()

    if c:
        online = [d for d in online if (d.get("country") or "").strip().lower() == c]
    if ci:
        online = [d for d in online if (d.get("city") or "").strip().lower() == ci]
    # Personal mode: both name + phone → strict whole-token name match (not substring)
    if dn and dp:
        online = [
            d for d in online
            if _personal_name_match(dn, d.get("name") or "")
            and _norm_phone(d.get("phone") or "").endswith(dp)
        ]
    else:
        if dn:
            online = [d for d in online if dn in _norm_name(d.get("name") or "")]
        if dp:
            online = [d for d in online if dp and dp in _norm_phone(d.get("phone") or "")]
    if hn:
        online = [d for d in online if hn in (d.get("hospital_name") or "").strip().lower()]
    if hr:
        online = [d for d in online if (d.get("hospital_registration_no") or "").strip().lower() == hr]
    if only_private is True:
        online = [d for d in online if bool(d.get("is_private", True))]

    safe = [{k: v for k, v in d.items() if k != "token"} for d in online]
    return {"status": "success", "doctors": safe, "count": len(safe)}


# ══════════════════════════════════════════════════════════
# ORIGINAL CALL ROUTES (FCM-based, in-memory)
# ══════════════════════════════════════════════════════════
class PatientCall(BaseModel):
    patient_name: str; patient_id: str; symptom: str
    doctor_id: Optional[str] = None
    doctor_name: Optional[str] = None
    doctor_phone: Optional[str] = None
    hospital_name: Optional[str] = None
    hospital_reg_no: Optional[str] = None

def _find_hospital_row(hosp_name: str, hosp_reg: str):
    """
    Match hospitals row from robot settings. Exact name+reg first, then registration-only,
    then fuzzy name — avoids 404 when spacing/casing differs from DB.
    """
    hn = (hosp_name or "").strip()
    hr = (hosp_reg or "").strip()
    if not hn or not hr:
        return None
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT id, name, registration_no FROM hospitals
               WHERE lower(trim(name)) = lower(trim(%s))
                 AND lower(trim(coalesce(registration_no,''))) = lower(trim(%s))
               LIMIT 1""",
            (hn, hr),
        )
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            """SELECT id, name, registration_no FROM hospitals
               WHERE lower(trim(coalesce(registration_no,''))) = lower(trim(%s))
               LIMIT 1""",
            (hr,),
        )
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            """SELECT id, name, registration_no FROM hospitals
               WHERE lower(trim(name)) LIKE %s
               LIMIT 8""",
            (f"%{hn.lower()}%",),
        )
        rows = cur.fetchall() or []
        if len(rows) == 1:
            return rows[0]
        hr_l = hr.lower()
        for r in rows:
            reg = (r.get("registration_no") or "").strip().lower()
            if reg == hr_l or hr_l in reg or reg in hr_l:
                return r
        if rows:
            return rows[0]
        return None
    finally:
        cur.close()
        conn.close()


@app.post("/api/calls/initiate")
def initiate_call(call: PatientCall):
    hosp_name = (call.hospital_name or "").strip()
    hosp_reg = (call.hospital_reg_no or "").strip()
    if hosp_name and hosp_reg:
        h = _find_hospital_row(hosp_name, hosp_reg)
        if not h:
            raise HTTPException(
                404,
                "Hospital not found — check name and registration number match the portal, or ask admin to register this hospital.",
            )
        call_id = str(uuid.uuid4())[:8]
        video_call_url = f"https://meet.jit.si/ai-dobot-{call_id}"
        pending_hospital_calls[call_id + "_H" + str(h["id"])] = {
            "call_id": call_id,
            "hospital_id": h["id"],
            "hospital_name": h["name"],
            "patient_name": call.patient_name,
            "patient_id": call.patient_id,
            "symptom": call.symptom,
            "video_call_url": video_call_url,
            "created_at": time.time(),
        }
        return {
            "status": "success",
            "message": f"Hospital {h['name']} notified",
            "video_call_url": video_call_url,
            "call_id": call_id
        }

    online = [_hydrate_online_doctor(d) for d in _online_doctors()]
    if not online:
        raise HTTPException(503, "No doctors online right now.")
    if not SERVICE_ACCOUNT_JSON:
        raise HTTPException(500, "Firebase not configured on server.")

    call_id = str(uuid.uuid4())[:8]
    video_call_url = f"https://meet.jit.si/ai-dobot-{call_id}"

    if call.doctor_id and call.doctor_id in doctors:
        targets = [doctors[call.doctor_id]]
    elif call.doctor_name or call.doctor_phone:
        n = _norm_name(call.doctor_name or "")
        p = _norm_phone(call.doctor_phone or "")
        targets = []
        for d in online:
            name_ok = (not n) or (n in _norm_name(d.get("name") or ""))
            phone_ok = (not p) or (p in _norm_phone(d.get("phone") or ""))
            if name_ok and phone_ok:
                targets.append(doctors[d["doctor_id"]])
        if not targets and n:
            # fallback: if phone mismatched, allow name-only match
            for d in online:
                if n in _norm_name(d.get("name") or ""):
                    targets.append(doctors[d["doctor_id"]])
    else:
        targets = [doctors[d["doctor_id"]] for d in online]

    if not targets:
        raise HTTPException(404, "No matching online doctor found.")

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
    for k in list(pending_hospital_calls.keys()):
        if pending_hospital_calls[k].get("call_id") == call_id:
            pending_hospital_calls.pop(k, None)
    cutoff = time.time() - 300
    for k in list(ended_calls.keys()):
        if ended_calls[k] < cutoff: del ended_calls[k]
    return {"status": "ended"}

@app.get("/api/hospitals/calls/pending")
def hospital_pending_calls(authorization: Optional[str] = Header(default=None)):
    sess = _verify("hospital_sessions", _bearer(authorization))
    if not sess:
        raise HTTPException(401, "Unauthorized")
    hid = sess["hospital_id"]
    cutoff = time.time() - 300
    calls = [{**v, "id": k} for k, v in pending_hospital_calls.items()
             if v.get("hospital_id") == hid and v.get("created_at", 0) > cutoff]
    calls.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"calls": calls}

@app.delete("/api/hospitals/calls/pending/{pending_id}")
def hospital_ack_call(pending_id: str, authorization: Optional[str] = Header(default=None)):
    sess = _verify("hospital_sessions", _bearer(authorization))
    if not sess:
        raise HTTPException(401, "Unauthorized")
    call = pending_hospital_calls.get(pending_id)
    if call and call.get("hospital_id") == sess["hospital_id"]:
        pending_hospital_calls.pop(pending_id, None)
        return {"status": "ok"}
    raise HTTPException(404, "Call not found")


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
    cur.execute("SELECT id,system_id,name,email,country,region,city,phone,license_no,license_doc_url,approval_status,created_at FROM pharmacies ORDER BY approval_status='pending' DESC, created_at DESC")
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


@app.get("/api/admin/pharmacies/{pharmacy_id}/medicines")
def admin_pharmacy_medicines(pharmacy_id: int, authorization: Optional[str] = Header(default=None)):
    if not _is_admin(authorization):
        raise HTTPException(401, "Unauthorized")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, system_id, name FROM pharmacies WHERE id=%s", (pharmacy_id,))
    ph = cur.fetchone()
    if not ph:
        cur.close()
        conn.close()
        raise HTTPException(404, "Pharmacy not found")
    cur.execute(
        """SELECT id, name, brand, category, description, dosage, diagnosis, price, stock,
                  requires_prescription, image_url, created_at
           FROM medicines WHERE pharmacy_id=%s ORDER BY name""",
        (pharmacy_id,),
    )
    meds = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"pharmacy": dict(ph), "medicines": meds, "count": len(meds)}


@app.delete("/api/admin/medicines/{med_id}")
def admin_delete_medicine(med_id: int, authorization: Optional[str] = Header(default=None)):
    if not _is_admin(authorization):
        raise HTTPException(401, "Unauthorized")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM medicines WHERE id=%s RETURNING id", (med_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(404, "Medicine not found")
    conn.commit()
    cur.close()
    conn.close()
    return {"success": True, "deleted_id": med_id}


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
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback(); raise HTTPException(400, _unique_msg(e))
    finally: cur.close(); conn.close()

@app.post("/api/reg_doctors/login")
def reg_doctor_login(d: LoginReq):
    ident = (d.identifier or "").strip()
    if not ident:
        raise HTTPException(400,"Identifier is required")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT rd.*, h.name AS hospital_name, h.registration_no AS hospital_registration_no
                   FROM reg_doctors rd
                   LEFT JOIN hospitals h ON rd.hospital_id = h.id
                   WHERE rd.password_hash=%s
                     AND (LOWER(rd.email)=LOWER(%s) OR LOWER(COALESCE(rd.username,''))=LOWER(%s) OR rd.phone=%s)
                   ORDER BY CASE
                     WHEN rd.approval_status='approved' THEN 0
                     WHEN rd.approval_status='pending'  THEN 1
                     ELSE 2
                   END, rd.created_at DESC
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
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback(); raise HTTPException(400, _unique_msg(e))
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
                   ORDER BY CASE
                     WHEN approval_status='approved' THEN 0
                     WHEN approval_status='pending'  THEN 1
                     ELSE 2
                   END, created_at DESC
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
                   ORDER BY CASE
                     WHEN approval_status='approved' THEN 0
                     WHEN approval_status='pending'  THEN 1
                     ELSE 2
                   END, created_at DESC LIMIT 1""",
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
    country:Optional[str]=None
    region:Optional[str]=None
    city:Optional[str]=None
    address:Optional[str]=None; phone:Optional[str]=None; license_no:Optional[str]=None
    license_doc_url:Optional[str]=None

@app.post("/api/pharmacies/register")
def pharm_register(d: PharmReg):
    sid = _gen_id("PHM"); conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO pharmacies (system_id,name,email,username,password_hash,country,region,city,address,phone,license_no,license_doc_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,system_id""",
            (sid,d.name,d.email,(d.username or "").strip() or None,_hash(d.password),d.country,d.region,d.city,d.address,d.phone,d.license_no,getattr(d,'license_doc_url',None)))
        row = cur.fetchone(); conn.commit()
        tok = _session("pharmacy_sessions","pharmacy_id",row["id"])
        return {"success":True,"system_id":row["system_id"],"token":tok}
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback(); raise HTTPException(400, _unique_msg(e))
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
                   ORDER BY CASE
                     WHEN approval_status='approved' THEN 0
                     WHEN approval_status='pending'  THEN 1
                     ELSE 2
                   END, created_at DESC
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
    cur.execute("SELECT id,system_id,name,email,country,region,city,address,phone,license_no FROM pharmacies WHERE id=%s",(sess["pharmacy_id"],))
    p = cur.fetchone(); cur.close(); conn.close()
    return dict(p) if p else {}


class PharmLocationPatch(BaseModel):
    country: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None


@app.patch("/api/pharmacies/location")
def pharm_patch_location(d: PharmLocationPatch, authorization: Optional[str] = Header(default=None)):
    sess = _verify("pharmacy_sessions", _bearer(authorization))
    if not sess:
        raise HTTPException(401, "Unauthorized")
    conn = get_conn()
    cur = conn.cursor()
    sets, vals = [], []
    if d.country is not None:
        sets.append("country=%s")
        vals.append(d.country.strip() or None)
    if d.region is not None:
        sets.append("region=%s")
        vals.append(d.region.strip() or None)
    if d.city is not None:
        sets.append("city=%s")
        vals.append(d.city.strip() or None)
    if not sets:
        cur.close()
        conn.close()
        raise HTTPException(400, "No fields to update")
    vals.append(sess["pharmacy_id"])
    cur.execute(f"UPDATE pharmacies SET {', '.join(sets)} WHERE id=%s", vals)
    conn.commit()
    cur.close()
    conn.close()
    return {"success": True}


@app.post("/api/pharmacies/medicine-image")
async def upload_medicine_image(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(default=None),
):
    sess = _verify("pharmacy_sessions", _bearer(authorization))
    if not sess:
        raise HTTPException(401, "Unauthorized")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 8 MB)")
    ct = (file.content_type or "").lower()
    if ct and not ct.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    url = _cloudinary_upload_bytes(raw, folder="medicines")
    return {"success": True, "url": url}


class MedCreate(BaseModel):
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    dosage: Optional[str] = None
    diagnosis: Optional[str] = None
    price: float
    stock: int = 0
    requires_prescription: bool = False
    image_url: Optional[str] = None

@app.post("/api/pharmacies/medicines")
def add_medicine(d: MedCreate, authorization: Optional[str] = Header(default=None)):
    sess = _verify("pharmacy_sessions",_bearer(authorization))
    if not sess: raise HTTPException(401,"Unauthorized")
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """INSERT INTO medicines (pharmacy_id,name,brand,category,description,dosage,diagnosis,price,stock,requires_prescription,image_url)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            sess["pharmacy_id"],
            d.name,
            d.brand,
            d.category,
            d.description,
            d.dosage,
            d.diagnosis,
            d.price,
            d.stock,
            d.requires_prescription,
            (d.image_url or "").strip() or None,
        ),
    )
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


class MedUpdate(BaseModel):
    name: Optional[str] = None
    brand: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    dosage: Optional[str] = None
    diagnosis: Optional[str] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    requires_prescription: Optional[bool] = None
    image_url: Optional[str] = None


@app.put("/api/pharmacies/medicines/{med_id}")
def update_medicine(med_id: int, d: MedUpdate, authorization: Optional[str] = Header(default=None)):
    sess = _verify("pharmacy_sessions", _bearer(authorization))
    if not sess:
        raise HTTPException(401, "Unauthorized")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM medicines WHERE id=%s AND pharmacy_id=%s",
        (med_id, sess["pharmacy_id"]),
    )
    if not cur.fetchone():
        cur.close()
        conn.close()
        raise HTTPException(404, "Medicine not found")
    try:
        data = d.model_dump(exclude_unset=True)
    except AttributeError:
        data = d.dict(exclude_unset=True)
    if not data:
        cur.close()
        conn.close()
        raise HTTPException(400, "No fields to update")
    if "image_url" in data and data["image_url"] is not None:
        data["image_url"] = (data["image_url"] or "").strip() or None
    cols, vals = [], []
    for k, v in data.items():
        cols.append(f"{k}=%s")
        vals.append(v)
    vals.append(med_id)
    vals.append(sess["pharmacy_id"])
    cur.execute(
        f"UPDATE medicines SET {', '.join(cols)} WHERE id=%s AND pharmacy_id=%s",
        vals,
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"success": True, "medicine_id": med_id}

def _countries_from_restcountries() -> list:
    """Smaller JSON (~200KB) — reliable from Render."""
    r = requests.get(
        "https://restcountries.com/v3.1/all?fields=name",
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Dobot-Backend/1.0)"},
    )
    r.raise_for_status()
    data = r.json()
    names = set()
    for c in data:
        n = (c or {}).get("name") or {}
        common = n.get("common")
        if common:
            names.add(common)
    return sorted(names)


def _countries_from_countriesnow() -> list:
    """Large payload (~1MB) — can time out on some hosts; used as 2nd try."""
    r = requests.get(
        "https://countriesnow.space/api/v0.1/countries",
        timeout=120,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Dobot-Backend/1.0)"},
    )
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j.get("msg") or "countriesnow returned error")
    return sorted({x.get("country") for x in (j.get("data") or []) if x.get("country")})


def _countries_static_fallback() -> list:
    """Always works if outbound APIs fail (same names as typical shop filters)."""
    raw = (
        "Afghanistan,Albania,Algeria,Andorra,Angola,Antigua and Barbuda,Argentina,Armenia,Australia,Austria,Azerbaijan,"
        "Bahamas,Bahrain,Bangladesh,Barbados,Belarus,Belgium,Belize,Benin,Bhutan,Bolivia,Bosnia and Herzegovina,Botswana,Brazil,Brunei,Bulgaria,Burkina Faso,Burundi,"
        "Cambodia,Cameroon,Canada,Cape Verde,Central African Republic,Chad,Chile,China,Colombia,Comoros,Congo,Costa Rica,Croatia,Cuba,Cyprus,Czechia,"
        "Democratic Republic of the Congo,Denmark,Djibouti,Dominica,Dominican Republic,Ecuador,Egypt,El Salvador,Equatorial Guinea,Eritrea,Estonia,Eswatini,Ethiopia,"
        "Fiji,Finland,France,Gabon,Gambia,Georgia,Germany,Ghana,Greece,Grenada,Guatemala,Guinea,Guinea-Bissau,Guyana,Haiti,Honduras,Hungary,"
        "Iceland,India,Indonesia,Iran,Iraq,Ireland,Israel,Italy,Jamaica,Japan,Jordan,Kazakhstan,Kenya,Kiribati,Kuwait,Kyrgyzstan,"
        "Laos,Latvia,Lebanon,Lesotho,Liberia,Libya,Liechtenstein,Lithuania,Luxembourg,Madagascar,Malawi,Malaysia,Maldives,Mali,Malta,Marshall Islands,Mauritania,Mauritius,Mexico,Micronesia,Moldova,Monaco,Mongolia,Montenegro,Morocco,Mozambique,Myanmar,"
        "Namibia,Nauru,Nepal,Netherlands,New Zealand,Nicaragua,Niger,Nigeria,North Korea,North Macedonia,Norway,Oman,Pakistan,Palau,Palestine,Panama,Papua New Guinea,Paraguay,Peru,Philippines,Poland,Portugal,Qatar,Romania,Russia,Rwanda,"
        "Saint Kitts and Nevis,Saint Lucia,Saint Vincent and the Grenadines,Samoa,San Marino,Sao Tome and Principe,Saudi Arabia,Senegal,Serbia,Seychelles,Sierra Leone,Singapore,Slovakia,Slovenia,Solomon Islands,Somalia,South Africa,South Korea,South Sudan,Spain,Sri Lanka,Sudan,Suriname,Sweden,Switzerland,Syria,"
        "Taiwan,Tajikistan,Tanzania,Thailand,Timor-Leste,Togo,Tonga,Trinidad and Tobago,Tunisia,Turkey,Turkmenistan,Tuvalu,Uganda,Ukraine,United Arab Emirates,United Kingdom,United States,Uruguay,Uzbekistan,Vanuatu,Vatican City,Venezuela,Vietnam,Yemen,Zambia,Zimbabwe"
    )
    return sorted({x.strip() for x in raw.split(",") if x.strip()})


@app.get("/api/shop/locations/countries")
def shop_location_countries():
    """Country list for shop / pharmacy registration (browser calls this; no CORS to third parties)."""
    sources = (
        ("restcountries", _countries_from_restcountries),
        ("countriesnow", _countries_from_countriesnow),
    )
    last_err = None
    for _label, fn in sources:
        try:
            names = fn()
            if names:
                return {"countries": names, "source": _label}
        except Exception as e:
            last_err = e
            continue
    static = _countries_static_fallback()
    return {"countries": static, "source": "static", "warning": str(last_err) if last_err else None}


@app.post("/api/shop/locations/states")
def shop_location_states(data: dict):
    """States/regions for a country (fallback: Nationwide)."""
    country = (data.get("country") or "").strip()
    if not country:
        raise HTTPException(400, "country is required")
    try:
        r = requests.post(
            "https://countriesnow.space/api/v0.1/countries/states",
            json={"country": country},
            timeout=25,
        )
        r.raise_for_status()
        j = r.json()
        states = [s.get("name") for s in (j.get("data", {}).get("states") or []) if s.get("name")]
        if not states:
            return {"states": ["Nationwide"]}
        return {"states": sorted(set(states))}
    except Exception:
        return {"states": ["Nationwide"]}


@app.get("/api/shop/medicines")
def shop_medicines(
    country: str = None,
    region: str = None,
    category: str = None,
    search: str = None,
    page: int = 1,
):
    conn = get_conn()
    cur = conn.cursor()
    q = """SELECT m.*,ph.name as pharmacy_name,ph.city as pharmacy_city,ph.country as pharmacy_country,
                  ph.region as pharmacy_region
           FROM medicines m JOIN pharmacies ph ON m.pharmacy_id=ph.id
           WHERE m.stock>0 AND ph.approval_status='approved'"""
    p = []
    if country and country.strip():
        q += " AND LOWER(TRIM(ph.country)) = LOWER(TRIM(%s))"
        p.append(country.strip())
    if region and region.strip():
        rl = region.strip().lower()
        if rl not in ("all", "nationwide", "*", "any"):
            q += " AND LOWER(TRIM(COALESCE(ph.region,''))) = LOWER(TRIM(%s))"
            p.append(region.strip())
    if category and category.strip():
        q += " AND m.category ILIKE %s"
        p.append(f"%{category.strip()}%")
    if search and search.strip():
        q += " AND (m.name ILIKE %s OR m.brand ILIKE %s OR COALESCE(m.diagnosis,'') ILIKE %s)"
        s = f"%{search.strip()}%"
        p.extend([s, s, s])
    q += f" ORDER BY m.name LIMIT 24 OFFSET {(page-1)*24}"
    cur.execute(q, p)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"medicines": rows, "page": page}

@app.post("/api/shop/order")
def place_order(data: dict, authorization: Optional[str] = Header(default=None)):
    tok = _bearer(authorization)
    sess = _verify("patient_sessions", tok) if tok else None
    patient_id = sess["patient_id"] if sess else None
    items = data.get("items",[])
    if not items: raise HTTPException(400,"No items in order")
    total = sum(float(i.get("price",0) or 0) * int(i.get("qty",1) or 1) for i in items)
    pharmacy_id = items[0].get("pharmacy_id") if items else None
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """INSERT INTO orders (patient_id,pharmacy_id,items,total,delivery_address,
           customer_email,customer_name,customer_phone,payment_status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            patient_id,
            pharmacy_id,
            psycopg2.extras.Json(items),
            total,
            (data.get("delivery_address") or "").strip(),
            (data.get("customer_email") or "").strip() or None,
            (data.get("customer_name") or "").strip() or None,
            (data.get("customer_phone") or "").strip() or None,
            "pending_payment",
        ),
    )
    row = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return {
        "success": True,
        "order_id": row["id"],
        "total": total,
        "payment_status": "pending_payment",
        "message": "Order saved. Online payment can be connected later — status is pending_payment.",
    }


# ══════════════════════════════════════════════════════════
# START SERVER
# ══════════════════════════════════════════════════════════
import uvicorn
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
