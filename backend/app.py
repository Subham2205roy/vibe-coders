# backend/app.py - SmartTransit Backend API
import os, math, json, hashlib, secrets, uuid
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
import urllib.request
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import joblib

# ── SQLite + SQLAlchemy ──────────────────────────────────────────────
from sqlalchemy import create_engine, Column, String, Float, Integer, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# Use an absolute path so the app always hits the same SQLite file, no matter
# where Uvicorn is launched from (root vs backend folder).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, 'smarttransit.db')
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"
print(f"DEBUG: Using database at: {DATABASE_PATH}")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ── Environment Config ──────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "your-google-client-id-here")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
JWT_SECRET = os.getenv("JWT_SECRET", "smarttransit-dev-secret-key-2026")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

ORS_KEY = os.getenv("ORS_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_KEY", "")

import base64, hmac

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64d(s: str) -> bytes:
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)

def create_token(payload: dict) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload["exp"] = (datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS)).isoformat()
    body = _b64(json.dumps(payload).encode())
    sig = _b64(hmac.new(JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"

def decode_token(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("bad token")
        header, body, sig = parts
        expected_sig = _b64(hmac.new(JWT_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected_sig):
            raise ValueError("invalid signature")
        payload = json.loads(_b64d(body))
        if datetime.fromisoformat(payload["exp"]) < datetime.utcnow():
            raise ValueError("expired")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return f"{salt}:{h.hex()}"

def verify_password(password: str, stored: str) -> bool:
    salt, h = stored.split(":")
    return hmac.compare_digest(h, hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex())

# ── Database Models ──────────────────────────────────────────────────
class UserDB(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)  # "passenger" or "driver"
    phone = Column(String, default="")
    employee_id = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

class TicketDB(Base):
    __tablename__ = "tickets"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)
    route_id = Column(String, nullable=False)
    route_name = Column(String, default="")
    from_stop = Column(String, nullable=False)
    to_stop = Column(String, nullable=False)
    fare = Column(Float, default=0.0)
    status = Column(String, default="active")  # active, used, expired
    booked_at = Column(DateTime, default=datetime.utcnow)
    qr_data = Column(String, default="")

class SavedRouteDB(Base):
    __tablename__ = "saved_routes"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False, index=True)
    name = Column(String, default="")
    from_place = Column(String, nullable=False)
    to_place = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class LiveBusDB(Base):
    __tablename__ = "live_buses"
    bus_reg = Column(String, primary_key=True)
    driver_id = Column(String, nullable=False)
    driver_name = Column(String, default="")
    route_id = Column(String, default="")
    latitude = Column(Float, default=0.0)
    longitude = Column(Float, default=0.0)
    speed = Column(Float, default=0.0)
    passenger_count = Column(Integer, default=0)
    crowd_level = Column(String, default="Low")  # Low, Medium, High
    status = Column(String, default="running")    # running, delayed, breakdown
    delay_reason = Column(String, default="")
    trip_started_at = Column(DateTime, default=datetime.utcnow)
    last_update = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ── Dependency ──────────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(token: str = Query(None, alias="token"), authorization: str = None):
    t = token
    if not t and authorization and authorization.startswith("Bearer "):
        t = authorization[7:]
    if not t:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return decode_token(t)

# ── Load ML model ───────────────────────────────────────────────────
model_path = os.path.join(os.path.dirname(__file__), "model.pkl")
try:
    eta_model = joblib.load(model_path)
except Exception:
    eta_model = None

# ── Pydantic Schemas ────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "passenger"
    phone: str = ""
    employee_id: str = ""

class LoginRequest(BaseModel):
    email: str
    password: str

class LocationUpdate(BaseModel):
    bus_reg: str
    latitude: float
    longitude: float
    speed: float = 0.0
    route_id: str = ""

class PassengerCountUpdate(BaseModel):
    bus_reg: str
    passenger_count: int

class StatusUpdate(BaseModel):
    bus_reg: str
    status: str  # running, delayed, breakdown
    delay_reason: str = ""

class TicketBookRequest(BaseModel):
    route_id: str
    route_name: str = ""
    from_stop: str
    to_stop: str
    fare: float = 0.0

class SaveRouteRequest(BaseModel):
    name: str = ""
    from_place: str
    to_place: str

class TripStartRequest(BaseModel):
    bus_reg: str
    route_id: str = ""

# ── Real Kolkata Bus Data ───────────────────────────────────────────
BUS_ROUTES = [
    {"id": "S12", "name": "S12", "from": "Howrah Station", "to": "New Town", "via": "Salt Lake", "fare_min": 10, "fare_max": 35, "frequency_min": 10},
    {"id": "AC1", "name": "AC1", "from": "Jadavpur", "to": "Airport", "via": "EM Bypass", "fare_min": 25, "fare_max": 55, "frequency_min": 15},
    {"id": "45A", "name": "45A", "from": "Garia Station", "to": "Esplanade", "via": "Rashbehari", "fare_min": 8, "fare_max": 25, "frequency_min": 8},
    {"id": "DN9", "name": "DN9", "from": "Dunlop", "to": "Babughat", "via": "Shyambazar", "fare_min": 10, "fare_max": 30, "frequency_min": 12},
    {"id": "S9", "name": "S9", "from": "Howrah Station", "to": "Salt Lake Sector V", "via": "Ultadanga", "fare_min": 10, "fare_max": 30, "frequency_min": 10},
    {"id": "AC2", "name": "AC2", "from": "Garia", "to": "Karunamoyee", "via": "Science City", "fare_min": 25, "fare_max": 50, "frequency_min": 20},
    {"id": "230", "name": "230", "from": "Howrah Station", "to": "Joka", "via": "Behala", "fare_min": 8, "fare_max": 28, "frequency_min": 10},
    {"id": "12B", "name": "12B/1", "from": "Tollygunge", "to": "Howrah Station", "via": "Kalighat", "fare_min": 8, "fare_max": 22, "frequency_min": 7},
    {"id": "201", "name": "201", "from": "Barasat", "to": "Babughat", "via": "Dum Dum", "fare_min": 12, "fare_max": 35, "frequency_min": 15},
    {"id": "AC9", "name": "AC9", "from": "Airport", "to": "Esplanade", "via": "VIP Road", "fare_min": 30, "fare_max": 60, "frequency_min": 20},
    {"id": "S32", "name": "S32", "from": "Sealdah", "to": "Dankuni", "via": "Shyambazar", "fare_min": 10, "fare_max": 32, "frequency_min": 12},
    {"id": "46A", "name": "46A", "from": "Ruby Hospital", "to": "Howrah Station", "via": "Park Circus", "fare_min": 8, "fare_max": 25, "frequency_min": 8},
    {"id": "S5", "name": "S5", "from": "Barrackpore", "to": "Esplanade", "via": "Baranagar", "fare_min": 12, "fare_max": 30, "frequency_min": 12},
    {"id": "DN20", "name": "DN20", "from": "Dunlop", "to": "Garia", "via": "EM Bypass", "fare_min": 12, "fare_max": 35, "frequency_min": 15},
    {"id": "AC47", "name": "AC47", "from": "Howrah Station", "to": "Garia", "via": "Moulali", "fare_min": 25, "fare_max": 50, "frequency_min": 15},
    {"id": "78", "name": "78", "from": "Esplanade", "to": "Barasat", "via": "Dum Dum", "fare_min": 10, "fare_max": 30, "frequency_min": 10},
    {"id": "S11", "name": "S11", "from": "Howrah", "to": "Karunamoyee", "via": "Sealdah", "fare_min": 10, "fare_max": 28, "frequency_min": 12},
    {"id": "215", "name": "215", "from": "Sealdah", "to": "Airport", "via": "VIP Road", "fare_min": 10, "fare_max": 30, "frequency_min": 15},
    {"id": "L230", "name": "L230", "from": "Garia", "to": "Babughat", "via": "Tollygunge", "fare_min": 8, "fare_max": 25, "frequency_min": 8},
    {"id": "AC3", "name": "AC3", "from": "New Town", "to": "Esplanade", "via": "Salt Lake", "fare_min": 30, "fare_max": 55, "frequency_min": 20},
    {"id": "S41", "name": "S41", "from": "Baruipur", "to": "Howrah", "via": "Garia", "fare_min": 12, "fare_max": 35, "frequency_min": 15},
    {"id": "E32", "name": "E32", "from": "Barrackpore", "to": "Garia", "via": "Ultadanga", "fare_min": 12, "fare_max": 35, "frequency_min": 12},
    {"id": "MM12", "name": "MM12", "from": "Behala", "to": "BBD Bagh", "via": "Diamond Harbour Rd", "fare_min": 8, "fare_max": 22, "frequency_min": 10},
    {"id": "AC20", "name": "AC20", "from": "Howrah", "to": "Airport", "via": "EM Bypass", "fare_min": 30, "fare_max": 65, "frequency_min": 25},
    {"id": "S6", "name": "S6", "from": "Sealdah", "to": "Salt Lake", "via": "CIT Road", "fare_min": 8, "fare_max": 20, "frequency_min": 8},
    {"id": "34", "name": "34", "from": "Esplanade", "to": "Jadavpur", "via": "Gariahat", "fare_min": 8, "fare_max": 20, "frequency_min": 7},
    {"id": "203", "name": "203", "from": "Sealdah", "to": "Sonarpur", "via": "Garia", "fare_min": 10, "fare_max": 28, "frequency_min": 12},
    {"id": "AC36", "name": "AC36", "from": "Barasat", "to": "Tollygunge", "via": "Jessore Road", "fare_min": 25, "fare_max": 50, "frequency_min": 20},
    {"id": "S15", "name": "S15", "from": "Howrah", "to": "Nicco Park", "via": "Salt Lake", "fare_min": 10, "fare_max": 30, "frequency_min": 12},
    {"id": "DN44", "name": "DN44", "from": "Dunlop", "to": "Jadavpur", "via": "Shyambazar", "fare_min": 10, "fare_max": 28, "frequency_min": 12},
]

BUS_STOPS = [
    {"id": "hw", "name": "Howrah Station", "lat": 22.5839, "lng": 88.3430, "routes": ["S12","230","12B","S11","AC20","S15","S41","AC47"]},
    {"id": "es", "name": "Esplanade", "lat": 22.5554, "lng": 88.3520, "routes": ["45A","AC9","S5","78","AC3","34"]},
    {"id": "sd", "name": "Sealdah Station", "lat": 22.5684, "lng": 88.3733, "routes": ["S32","S6","203","215"]},
    {"id": "nt", "name": "New Town City Centre", "lat": 22.5941, "lng": 88.4842, "routes": ["S12","AC3"]},
    {"id": "sl5", "name": "Salt Lake Sector V", "lat": 22.5726, "lng": 88.4319, "routes": ["S9","S6","S15"]},
    {"id": "ga", "name": "Garia Station", "lat": 22.4590, "lng": 88.3852, "routes": ["45A","AC2","DN20","L230","203","S41"]},
    {"id": "jp", "name": "Jadavpur", "lat": 22.4987, "lng": 88.3692, "routes": ["AC1","34","DN44"]},
    {"id": "ap", "name": "Airport (NSCBI)", "lat": 22.6520, "lng": 88.4463, "routes": ["AC1","AC9","215","AC20"]},
    {"id": "dl", "name": "Dunlop", "lat": 22.6460, "lng": 88.3784, "routes": ["DN9","DN20","DN44"]},
    {"id": "bb", "name": "Babughat", "lat": 22.5636, "lng": 88.3400, "routes": ["DN9","201","L230"]},
    {"id": "br", "name": "Barasat", "lat": 22.7235, "lng": 88.4802, "routes": ["201","78","AC36"]},
    {"id": "tg", "name": "Tollygunge", "lat": 22.4985, "lng": 88.3470, "routes": ["12B","AC36"]},
    {"id": "km", "name": "Karunamoyee", "lat": 22.5772, "lng": 88.4041, "routes": ["AC2","S11"]},
    {"id": "rb", "name": "Ruby Hospital", "lat": 22.5145, "lng": 88.3988, "routes": ["46A","DN20"]},
    {"id": "sh", "name": "Shyambazar", "lat": 22.5956, "lng": 88.3716, "routes": ["DN9","S32","DN44"]},
    {"id": "pc", "name": "Park Circus", "lat": 22.5393, "lng": 88.3695, "routes": ["46A"]},
    {"id": "sc", "name": "Science City", "lat": 22.5403, "lng": 88.3960, "routes": ["AC2"]},
    {"id": "bh", "name": "Behala", "lat": 22.4889, "lng": 88.3100, "routes": ["230","MM12"]},
    {"id": "jk", "name": "Joka", "lat": 22.4487, "lng": 88.3095, "routes": ["230"]},
    {"id": "bp", "name": "Barrackpore", "lat": 22.7584, "lng": 88.3685, "routes": ["S5","E32"]},
    {"id": "dd", "name": "Dum Dum", "lat": 22.6224, "lng": 88.4218, "routes": ["201","78"]},
    {"id": "ul", "name": "Ultadanga", "lat": 22.5813, "lng": 88.3900, "routes": ["S9","E32"]},
    {"id": "gh", "name": "Gariahat", "lat": 22.5183, "lng": 88.3667, "routes": ["34"]},
    {"id": "kg", "name": "Kalighat", "lat": 22.5240, "lng": 88.3440, "routes": ["12B"]},
    {"id": "ml", "name": "Moulali", "lat": 22.5568, "lng": 88.3590, "routes": ["AC47"]},
    {"id": "sl1", "name": "Salt Lake Sector I", "lat": 22.5800, "lng": 88.4100, "routes": ["S12","S6","AC3"]},
    {"id": "bbd", "name": "BBD Bagh", "lat": 22.5728, "lng": 88.3488, "routes": ["MM12"]},
    {"id": "dk", "name": "Dankuni", "lat": 22.6784, "lng": 88.2931, "routes": ["S32"]},
    {"id": "rh", "name": "Rashbehari", "lat": 22.5175, "lng": 88.3544, "routes": ["45A"]},
    {"id": "vip", "name": "VIP Road", "lat": 22.6093, "lng": 88.4260, "routes": ["AC9","215"]},
    {"id": "by", "name": "Baruipur", "lat": 22.3581, "lng": 88.4305, "routes": ["S41"]},
    {"id": "dh", "name": "Diamond Harbour Road", "lat": 22.4830, "lng": 88.3200, "routes": ["MM12"]},
    {"id": "js", "name": "Jessore Road", "lat": 22.6300, "lng": 88.4000, "routes": ["AC36","78"]},
    {"id": "np", "name": "Nicco Park", "lat": 22.5756, "lng": 88.4150, "routes": ["S15"]},
    {"id": "sr", "name": "Sonarpur", "lat": 22.4350, "lng": 88.4150, "routes": ["203"]},
    {"id": "ct", "name": "CIT Road", "lat": 22.5650, "lng": 88.3850, "routes": ["S6"]},
    {"id": "bn", "name": "Baranagar", "lat": 22.6388, "lng": 88.3788, "routes": ["S5"]},
    {"id": "eb", "name": "EM Bypass Connector", "lat": 22.5316, "lng": 88.3978, "routes": ["AC1","DN20","AC20"]},
    {"id": "ht", "name": "Hatibagan", "lat": 22.5880, "lng": 88.3630, "routes": ["DN9"]},
    {"id": "np2", "name": "New Alipore", "lat": 22.5070, "lng": 88.3330, "routes": ["230"]},
    {"id": "pb", "name": "Prince Anwar Shah Road", "lat": 22.5050, "lng": 88.3780, "routes": ["AC2","DN20"]},
    {"id": "lk", "name": "Lake Town", "lat": 22.5942, "lng": 88.3920, "routes": ["E32","215"]},
    {"id": "cg", "name": "College Street", "lat": 22.5730, "lng": 88.3630, "routes": ["DN9","S32"]},
    {"id": "wr", "name": "Wireless Crossing", "lat": 22.5260, "lng": 88.3910, "routes": ["46A","AC2"]},
    {"id": "nb", "name": "Nabanna", "lat": 22.5588, "lng": 88.3168, "routes": ["230"]},
    {"id": "sg", "name": "Salt Lake Gate", "lat": 22.5760, "lng": 88.4050, "routes": ["S12","S9","S6","S15"]},
    {"id": "sb", "name": "Santoshpur", "lat": 22.4770, "lng": 88.3930, "routes": ["203","E32"]},
    {"id": "kr", "name": "Kona Expressway", "lat": 22.5760, "lng": 88.3100, "routes": ["AC20"]},
    {"id": "tb", "name": "Taratala", "lat": 22.5050, "lng": 88.3180, "routes": ["230","MM12"]},
    {"id": "gl", "name": "Golf Green", "lat": 22.4970, "lng": 88.3920, "routes": ["DN20","AC2"]},
    # ── 30 new stops ──
    {"id": "blr", "name": "Belur Math", "lat": 22.6308, "lng": 88.3527, "routes": ["S5","S15"]},
    {"id": "dks", "name": "Dakshineswar", "lat": 22.6548, "lng": 88.3576, "routes": ["DN9","S5"]},
    {"id": "svb", "name": "Sovabazar", "lat": 22.5881, "lng": 88.3598, "routes": ["DN9","S32"]},
    {"id": "bhw", "name": "Bhawanipur", "lat": 22.5267, "lng": 88.3450, "routes": ["12B","34"]},
    {"id": "hz", "name": "Hazra", "lat": 22.5237, "lng": 88.3510, "routes": ["45A","34"]},
    {"id": "ld", "name": "Lansdowne", "lat": 22.5150, "lng": 88.3480, "routes": ["12B","AC36"]},
    {"id": "blg", "name": "Ballygunge", "lat": 22.5268, "lng": 88.3640, "routes": ["34","AC1"]},
    {"id": "ksb", "name": "Kasba", "lat": 22.5100, "lng": 88.3870, "routes": ["AC2","203"]},
    {"id": "rp", "name": "Regent Park", "lat": 22.5190, "lng": 88.3850, "routes": ["46A","AC2"]},
    {"id": "mk", "name": "Maniktala", "lat": 22.5870, "lng": 88.3800, "routes": ["E32","DN44"]},
    {"id": "blc", "name": "Belgachia", "lat": 22.6020, "lng": 88.3700, "routes": ["DN9","201"]},
    {"id": "csp", "name": "Cossipore", "lat": 22.6100, "lng": 88.3680, "routes": ["S5","DN44"]},
    {"id": "sdp", "name": "Sodepur", "lat": 22.6950, "lng": 88.3920, "routes": ["S5","201"]},
    {"id": "nht", "name": "Naihati", "lat": 22.8930, "lng": 88.4220, "routes": ["E32"]},
    {"id": "chn", "name": "Chandannagar", "lat": 22.8672, "lng": 88.3630, "routes": ["S32"]},
    {"id": "bly", "name": "Bally", "lat": 22.6500, "lng": 88.3400, "routes": ["12B","S15"]},
    {"id": "llh", "name": "Liluah", "lat": 22.6250, "lng": 88.3340, "routes": ["AC20","S11"]},
    {"id": "belg", "name": "Belgharia", "lat": 22.6650, "lng": 88.3880, "routes": ["201","78"]},
    {"id": "mdg", "name": "Madhyamgram", "lat": 22.6800, "lng": 88.4500, "routes": ["78","AC36"]},
    {"id": "rjh", "name": "Rajarhat", "lat": 22.5990, "lng": 88.4750, "routes": ["AC3","S12"]},
    {"id": "cnp", "name": "Chinar Park", "lat": 22.6020, "lng": 88.4550, "routes": ["AC3","S9"]},
    {"id": "bgti", "name": "Baguiati", "lat": 22.6120, "lng": 88.4200, "routes": ["215","AC9"]},
    {"id": "plb", "name": "Phoolbagan", "lat": 22.5720, "lng": 88.3850, "routes": ["S6","AC47"]},
    {"id": "ent", "name": "Entally", "lat": 22.5630, "lng": 88.3700, "routes": ["AC47","46A"]},
    {"id": "bwb", "name": "Bow Barracks", "lat": 22.5580, "lng": 88.3610, "routes": ["45A","AC47"]},
    {"id": "dmt", "name": "Dharmatala", "lat": 22.5550, "lng": 88.3530, "routes": ["45A","78","AC9"]},
    {"id": "mdn", "name": "Maidan", "lat": 22.5500, "lng": 88.3455, "routes": ["34","230","12B"]},
    {"id": "ndn", "name": "Nandan", "lat": 22.5280, "lng": 88.3500, "routes": ["34","AC36"]},
    {"id": "exc", "name": "Exide Crossing", "lat": 22.5320, "lng": 88.3530, "routes": ["45A","AC1"]},
    {"id": "rbs", "name": "Rabindra Sadan", "lat": 22.5310, "lng": 88.3470, "routes": ["12B","34","AC36"]},
]

# ── App Init ─────────────────────────────────────────────────────────
app = FastAPI(title="SmartTransit API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Root ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"message": "SmartTransit API v2.0 🚍", "status": "running"}

@app.get("/config")
def get_config():
    """Provides public API keys to the frontend."""
    return {
        "google_client_id": GOOGLE_CLIENT_ID,
        "ors_key": ORS_KEY,
        "gemini_key": GEMINI_KEY
    }

# ── Auth ─────────────────────────────────────────────────────────────
@app.post("/auth/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    print(f"DEBUG: Registering user {req.email}")
    try:
        existing = db.query(UserDB).filter(UserDB.email == req.email).first()
        if existing:
            print(f"DEBUG: Email {req.email} already exists.")
            raise HTTPException(status_code=400, detail="Email already registered")
        user = UserDB(
            name=req.name,
            email=req.email,
            password_hash=hash_password(req.password),
            role=req.role,
            phone=req.phone,
            employee_id=req.employee_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"DEBUG: User {req.email} registered successfully with ID {user.id}")
        token = create_token({"user_id": user.id, "email": user.email, "role": user.role, "name": user.name})
        return {"token": token, "user": {"id": user.id, "name": user.name, "email": user.email, "role": user.role}}
    except Exception as e:
        print(f"DEBUG: Registration ERROR: {str(e)}")
        db.rollback()
        raise e

@app.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(UserDB).filter(UserDB.email == req.email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token({"user_id": user.id, "email": user.email, "role": user.role, "name": user.name})
    return {"token": token, "user": {"id": user.id, "name": user.name, "email": user.email, "role": user.role}}

class GoogleAuthRequest(BaseModel):
    credential: str
    role: str = "passenger"

@app.post("/auth/google")
def google_auth(req: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Verify Google ID token and login/register the user."""
    try:
        # Verify token with Google
        url = f"https://oauth2.googleapis.com/tokeninfo?id_token={req.credential}"
        with urllib.request.urlopen(url) as resp:
            info = json.loads(resp.read().decode())
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    # Verify the token audience matches our client ID
    if info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Token not intended for this app")

    email = info.get("email")
    name = info.get("name", email.split("@")[0])
    if not email:
        raise HTTPException(status_code=400, detail="No email in Google token")

    # Check if user exists
    user = db.query(UserDB).filter(UserDB.email == email).first()
    if not user:
        # Auto-register with Google OAuth marker
        user = UserDB(
            name=name,
            email=email,
            password_hash="GOOGLE_OAUTH",
            role=req.role,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    token = create_token({"user_id": user.id, "email": user.email, "role": user.role, "name": user.name})
    return {"token": token, "user": {"id": user.id, "name": user.name, "email": user.email, "role": user.role}}

# ── Bus Routes ───────────────────────────────────────────────────────
@app.get("/routes")
def get_routes():
    return {"routes": BUS_ROUTES}

# NOTE: find-route and search MUST come before {route_id} wildcard

def _match_stop(query: str):
    """Fuzzy match a stop by name: supports partial, word-boundary, and alias matching."""
    q = query.lower().strip()
    # Exact match first
    for s in BUS_STOPS:
        if s["name"].lower() == q or s["id"] == q:
            return s
    # Common aliases for Kolkata places
    aliases = {
        "howrah": "howrah station", "sealdah": "sealdah station",
        "newtown": "new town city centre", "new town": "new town city centre",
        "airport": "airport (nscbi)", "garia": "garia station",
        "salt lake": "salt lake sector v", "sector v": "salt lake sector v",
        "sector 5": "salt lake sector v", "sector i": "salt lake sector i",
        "sector 1": "salt lake sector i", "dum dum": "dum dum",
        "tollygunge": "tollygunge", "tolly": "tollygunge",
        "karunamoyee": "karunamoyee", "ruby": "ruby hospital",
        "esplanade": "esplanade", "jadavpur": "jadavpur",
        "barasat": "barasat", "behala": "behala", "joka": "joka",
        "bbdbagh": "bbd bagh", "bbd bagh": "bbd bagh",
        "gariahat": "gariahat", "park circus": "park circus",
        "science city": "science city", "dunlop": "dunlop", 
        "nicco park": "nicco park", "babughat": "babughat",
        "shyambazar": "shyambazar", "barrackpore": "barrackpore",
        "belur": "belur math", "belur math": "belur math",
        "dakshineswar": "dakshineswar", "sovabazar": "sovabazar",
        "bhawanipur": "bhawanipur", "hazra": "hazra",
        "lansdowne": "lansdowne", "ballygunge": "ballygunge",
        "kasba": "kasba", "regent park": "regent park",
        "maniktala": "maniktala", "belgachia": "belgachia",
        "cossipore": "cossipore", "sodepur": "sodepur",
        "naihati": "naihati", "chandannagar": "chandannagar",
        "bally": "bally", "liluah": "liluah",
        "belgharia": "belgharia", "madhyamgram": "madhyamgram",
        "rajarhat": "rajarhat", "chinar park": "chinar park",
        "baguiati": "baguiati", "phoolbagan": "phoolbagan",
        "entally": "entally", "dharmatala": "dharmatala",
        "maidan": "maidan", "nandan": "nandan",
        "exide": "exide crossing", "rabindra sadan": "rabindra sadan",
    }
    if q in aliases:
        alias_name = aliases[q]
        for s in BUS_STOPS:
            if s["name"].lower() == alias_name:
                return s
    # Partial match: check if query is contained in stop name
    matches = []
    for s in BUS_STOPS:
        name_lower = s["name"].lower()
        if q in name_lower:
            matches.append(s)
    if len(matches) == 1:
        return matches[0]
    if matches:
        matches.sort(key=lambda s: len(s["name"]))
        return matches[0]
    # Word-start match
    for s in BUS_STOPS:
        words = s["name"].lower().split()
        if any(w.startswith(q) for w in words):
            return s
    return None

@app.get("/routes/find-route")
def find_route_between(from_stop: str = Query(...), to_stop: str = Query(...)):
    from_s = _match_stop(from_stop)
    to_s = _match_stop(to_stop)
    if not from_s or not to_s:
        missing = []
        if not from_s:
            missing.append(from_stop)
        if not to_s:
            missing.append(to_stop)
        raise HTTPException(status_code=404, detail=f"Stop(s) not found: {', '.join(missing)}. Try using full stop names like 'Howrah Station', 'New Town', 'Esplanade', etc.")
    
    direct_routes = set(from_s.get("routes", [])) & set(to_s.get("routes", []))
    results = []
    for rid in direct_routes:
        route = next((r for r in BUS_ROUTES if r["id"] == rid), None)
        if route:
            dist = math.sqrt((from_s["lat"]-to_s["lat"])**2 + (from_s["lng"]-to_s["lng"])**2) * 111
            est_time = max(5, int(dist * 3.5))
            results.append({
                "type": "direct",
                "route": route,
                "from_stop": from_s["name"],
                "to_stop": to_s["name"],
                "estimated_time_min": est_time,
                "estimated_fare": route.get("fare_min", 8),
                "transfers": 0
            })
    
    # If no direct route, find transfer routes (fastest first)
    if not results:
        transfer_results = []
        seen_transfers = set()
        for mid_stop in BUS_STOPS:
            if mid_stop["id"] == from_s["id"] or mid_stop["id"] == to_s["id"]:
                continue
            from_common = set(from_s.get("routes", [])) & set(mid_stop.get("routes", []))
            to_common = set(mid_stop.get("routes", [])) & set(to_s.get("routes", []))
            if from_common and to_common:
                r1 = next((r for r in BUS_ROUTES if r["id"] == list(from_common)[0]), None)
                r2 = next((r for r in BUS_ROUTES if r["id"] == list(to_common)[0]), None)
                if r1 and r2:
                    key = f"{r1['id']}-{mid_stop['id']}-{r2['id']}"
                    if key in seen_transfers:
                        continue
                    seen_transfers.add(key)
                    d1 = math.sqrt((from_s["lat"]-mid_stop["lat"])**2 + (from_s["lng"]-mid_stop["lng"])**2) * 111
                    d2 = math.sqrt((mid_stop["lat"]-to_s["lat"])**2 + (mid_stop["lng"]-to_s["lng"])**2) * 111
                    est_time = max(10, int((d1 + d2) * 3.5) + 5)
                    transfer_results.append({
                        "type": "transfer",
                        "leg1_route": r1,
                        "leg2_route": r2,
                        "from_stop": from_s["name"],
                        "transfer_stop": mid_stop["name"],
                        "to_stop": to_s["name"],
                        "estimated_time_min": est_time,
                        "estimated_fare": r1.get("fare_min",8) + r2.get("fare_min",8),
                        "transfers": 1
                    })
        transfer_results.sort(key=lambda x: x["estimated_time_min"])
        results = transfer_results[:5]
    
    results.sort(key=lambda x: (x["transfers"], x["estimated_time_min"]))
    return {
        "results": results[:5],
        "from": from_s["name"],
        "to": to_s["name"],
        "from_coords": {"lat": from_s["lat"], "lng": from_s["lng"]},
        "to_coords": {"lat": to_s["lat"], "lng": to_s["lng"]}
    }

@app.get("/routes/search/{query}")
def search_routes(query: str):
    q = query.lower()
    results = [r for r in BUS_ROUTES if q in r["name"].lower() or q in r["from"].lower() or q in r["to"].lower() or q in r.get("via", "").lower()]
    return {"routes": results}

@app.get("/routes/{route_id}")
def get_route(route_id: str):
    route = next((r for r in BUS_ROUTES if r["id"] == route_id), None)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    stops = [s for s in BUS_STOPS if route_id in s.get("routes", [])]
    return {"route": route, "stops": stops}

# ── Bus Stops ────────────────────────────────────────────────────────
@app.get("/stops")
def get_stops():
    return {"stops": BUS_STOPS}

@app.get("/stops/nearest")
def get_nearest_stops(lat: float = Query(...), lng: float = Query(...), limit: int = Query(5)):
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    stops_with_dist = []
    for s in BUS_STOPS:
        dist = haversine(lat, lng, s["lat"], s["lng"])
        stops_with_dist.append({**s, "distance_km": round(dist, 2), "distance_m": round(dist * 1000)})
    stops_with_dist.sort(key=lambda x: x["distance_km"])
    return {"stops": stops_with_dist[:limit], "user_location": {"lat": lat, "lng": lng}}

# ── _match_stop helper (used by find-route above) ───────────────────

# ── ETA Prediction ──────────────────────────────────────────────────
@app.get("/predict-eta")
def predict_eta(
    distance_remaining: float = Query(...),
    avg_speed: float = Query(25),
    traffic_index: float = Query(0.5),
    hour_of_day: int = Query(None)
):
    if hour_of_day is None:
        hour_of_day = datetime.now().hour
    if eta_model:
        features = [[distance_remaining, avg_speed, traffic_index, hour_of_day]]
        eta = eta_model.predict(features)
        return {"eta_minutes": round(float(eta[0]), 2), "source": "ml_model"}
    else:
        base_eta = (distance_remaining / max(avg_speed, 1)) * 60
        traffic_multiplier = 1 + (traffic_index * 0.8)
        if 8 <= hour_of_day <= 10 or 17 <= hour_of_day <= 20:
            traffic_multiplier += 0.3
        return {"eta_minutes": round(base_eta * traffic_multiplier, 2), "source": "formula"}

# ── Smart ETA (MVP) ─────────────────────────────────────────────────
@app.get("/smart-eta")
def smart_eta(
    user_lat: float = Query(...),
    user_lng: float = Query(...),
    destination: str = Query(...),
    db: Session = Depends(get_db),
):
    """Core MVP endpoint: user GPS + destination → nearest stop + best bus + ETA"""
    import math

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    # 1. Find nearest bus stops to user (sorted by distance)
    stops_by_dist = []
    for s in BUS_STOPS:
        dist = haversine(user_lat, user_lng, s["lat"], s["lng"])
        stops_by_dist.append({**s, "distance_km": round(dist, 2)})
    stops_by_dist.sort(key=lambda x: x["distance_km"])

    # 2. Fuzzy-match destination to a bus stop
    dest_lower = destination.strip().lower()
    aliases = {
        "howrah": "howrah station", "sealdah": "sealdah station",
        "airport": "airport (nscbi)", "newtown": "new town city centre",
        "new town": "new town city centre", "salt lake": "salt lake sector v",
        "sector v": "salt lake sector v", "garia": "garia station",
        "behala": "behala", "joka": "joka", "esplanade": "esplanade",
        "jadavpur": "jadavpur", "dunlop": "dunlop", "tollygunge": "tollygunge",
        "barasat": "barasat", "bbd bagh": "bbd bagh", "bbdbagh": "bbd bagh",
        "karunamoyee": "karunamoyee", "science city": "science city",
        "ruby": "ruby hospital", "park circus": "park circus",
        "gariahat": "gariahat", "shyambazar": "shyambazar",
        "nicco park": "nicco park", "babughat": "babughat",
        "barrackpore": "barrackpore", "ultadanga": "ultadanga",
        "dum dum": "dum dum", "moulali": "moulali",
        "belur": "belur math", "belur math": "belur math",
        "dakshineswar": "dakshineswar", "sovabazar": "sovabazar",
        "bhawanipur": "bhawanipur", "hazra": "hazra",
        "lansdowne": "lansdowne", "ballygunge": "ballygunge",
        "kasba": "kasba", "regent park": "regent park",
        "maniktala": "maniktala", "belgachia": "belgachia",
        "cossipore": "cossipore", "sodepur": "sodepur",
        "naihati": "naihati", "chandannagar": "chandannagar",
        "bally": "bally", "liluah": "liluah",
        "belgharia": "belgharia", "madhyamgram": "madhyamgram",
        "rajarhat": "rajarhat", "chinar park": "chinar park",
        "baguiati": "baguiati", "phoolbagan": "phoolbagan",
        "entally": "entally", "dharmatala": "dharmatala",
        "maidan": "maidan", "nandan": "nandan",
        "exide": "exide crossing", "rabindra sadan": "rabindra sadan",
    }
    resolved = aliases.get(dest_lower, dest_lower)
    dest_stop = None
    for s in BUS_STOPS:
        if resolved == s["name"].lower() or dest_lower in s["name"].lower() or s["name"].lower().startswith(dest_lower):
            dest_stop = s
            break
    if not dest_stop:
        # Try partial match
        for s in BUS_STOPS:
            if any(word in s["name"].lower() for word in dest_lower.split()):
                dest_stop = s
                break
    if not dest_stop:
        raise HTTPException(status_code=404, detail=f"Could not find destination '{destination}' in our bus stop database")

    # 3. Find a bus route that connects a nearby stop to the destination
    best_result = None
    for nearby in stops_by_dist[:8]:  # Check 8 nearest stops
        common_routes = set(nearby["routes"]) & set(dest_stop["routes"])
        if common_routes and nearby["id"] != dest_stop["id"]:
            route_id = list(common_routes)[0]
            route = next((r for r in BUS_ROUTES if r["id"] == route_id), None)
            if route:
                # Calculate distance between the two stops
                stop_distance = haversine(nearby["lat"], nearby["lng"], dest_stop["lat"], dest_stop["lng"])
                best_result = {
                    "pickup_stop": nearby,
                    "route": route,
                    "stop_distance_km": round(stop_distance, 2),
                }
                break

    if not best_result:
        # Fallback: find a transfer route
        for nearby in stops_by_dist[:5]:
            for mid_stop in BUS_STOPS:
                common1 = set(nearby["routes"]) & set(mid_stop["routes"])
                common2 = set(mid_stop["routes"]) & set(dest_stop["routes"])
                if common1 and common2 and nearby["id"] != mid_stop["id"] and mid_stop["id"] != dest_stop["id"]:
                    r1 = next((r for r in BUS_ROUTES if r["id"] == list(common1)[0]), None)
                    r2 = next((r for r in BUS_ROUTES if r["id"] == list(common2)[0]), None)
                    if r1 and r2:
                        d1 = haversine(nearby["lat"], nearby["lng"], mid_stop["lat"], mid_stop["lng"])
                        d2 = haversine(mid_stop["lat"], mid_stop["lng"], dest_stop["lat"], dest_stop["lng"])
                        best_result = {
                            "pickup_stop": nearby,
                            "route": r1,
                            "transfer_stop": mid_stop,
                            "transfer_route": r2,
                            "stop_distance_km": round(d1 + d2, 2),
                            "is_transfer": True,
                        }
                        break
            if best_result:
                break

    if not best_result:
        raise HTTPException(status_code=404, detail=f"No bus route found from your location to {destination}")

    # 4. Auto-calculate traffic index from live bus speeds
    hour_of_day = datetime.now().hour
    all_live = db.query(LiveBusDB).all()
    if all_live and any(b.speed > 0 for b in all_live):
        # Calculate from real bus speeds: fast = low traffic, slow = high traffic
        avg_live_speed = sum(b.speed for b in all_live if b.speed > 0) / max(len([b for b in all_live if b.speed > 0]), 1)
        # Map speed to traffic index: 40+ km/h = 0.1, 5 km/h = 1.0
        traffic_index = round(max(0.1, min(1.0, 1.0 - (avg_live_speed - 5) / 35)), 2)
        traffic_source = "live_speed"
    else:
        # No live buses — use time-of-day heuristic
        if 8 <= hour_of_day <= 10 or 17 <= hour_of_day <= 20:
            traffic_index = 0.8  # Rush hour
        elif 11 <= hour_of_day <= 16:
            traffic_index = 0.5  # Moderate
        elif 21 <= hour_of_day or hour_of_day <= 5:
            traffic_index = 0.2  # Night — light traffic
        else:
            traffic_index = 0.4  # Early morning
        traffic_source = "time_of_day"

    # 5. Calculate ETA
    pickup = best_result["pickup_stop"]
    walk_distance_km = pickup["distance_km"]
    walk_time_min = round(walk_distance_km / 0.08, 1)  # ~5 km/h walking = 0.083 km/min

    stop_distance = best_result["stop_distance_km"]
    avg_speed = 22  # Average bus speed in Kolkata (km/h)

    # Use ML or formula for bus travel ETA
    if eta_model:
        features = [[stop_distance, avg_speed, traffic_index, hour_of_day]]
        bus_travel_min = round(float(eta_model.predict(features)[0]), 1)
        eta_source = "ml_model"
    else:
        base_eta = (stop_distance / max(avg_speed, 1)) * 60
        traffic_multiplier = 1 + (traffic_index * 0.8)
        if 8 <= hour_of_day <= 10 or 17 <= hour_of_day <= 20:
            traffic_multiplier += 0.3
        bus_travel_min = round(base_eta * traffic_multiplier, 1)
        eta_source = "formula"

    # Estimated wait time at bus stop (based on route frequency)
    route = best_result["route"]
    wait_time_min = round(route["frequency_min"] / 2, 1)  # Average wait = half the frequency

    total_eta_min = round(walk_time_min + wait_time_min + bus_travel_min, 1)

    # 5. Check for live buses on this route
    live_bus = None
    live_buses = db.query(LiveBusDB).filter(LiveBusDB.route_id == route["id"]).all()
    if live_buses:
        # Find the closest live bus
        closest = min(live_buses, key=lambda b: haversine(b.latitude, b.longitude, pickup["lat"], pickup["lng"]))
        bus_to_stop_dist = haversine(closest.latitude, closest.longitude, pickup["lat"], pickup["lng"])
        if eta_model:
            live_eta_features = [[bus_to_stop_dist, max(closest.speed, 15), traffic_index, hour_of_day]]
            live_eta = round(float(eta_model.predict(live_eta_features)[0]), 1)
        else:
            live_eta = round((bus_to_stop_dist / max(closest.speed if closest.speed > 0 else 15, 1)) * 60 * (1 + traffic_index * 0.8), 1)
        live_bus = {
            "bus_reg": closest.bus_reg,
            "distance_km": round(bus_to_stop_dist, 2),
            "speed": closest.speed,
            "crowd_level": closest.crowd_level,
            "status": closest.status,
            "live_eta_min": live_eta,
        }
        # Update wait time with actual live bus ETA
        wait_time_min = live_eta
        total_eta_min = round(walk_time_min + live_eta + bus_travel_min, 1)

    # Build response
    response = {
        "user_location": {"lat": user_lat, "lng": user_lng},
        "pickup_stop": {
            "name": pickup["name"], "lat": pickup["lat"], "lng": pickup["lng"],
            "distance_km": pickup["distance_km"],
            "walk_time_min": walk_time_min,
        },
        "destination_stop": {
            "name": dest_stop["name"], "lat": dest_stop["lat"], "lng": dest_stop["lng"],
        },
        "bus_route": {
            "id": route["id"], "name": route["name"],
            "from": route["from"], "to": route["to"], "via": route.get("via", ""),
            "fare_range": f"₹{route['fare_min']}–₹{route['fare_max']}",
            "frequency_min": route["frequency_min"],
        },
        "eta": {
            "walk_time_min": walk_time_min,
            "wait_time_min": wait_time_min,
            "bus_travel_min": bus_travel_min,
            "total_min": total_eta_min,
            "source": eta_source,
        },
        "distance_km": best_result["stop_distance_km"],
        "traffic_index": traffic_index,
        "traffic_source": traffic_source,
        "hour_of_day": hour_of_day,
        "live_bus": live_bus,
    }

    # Add transfer info if applicable
    if best_result.get("is_transfer"):
        ts = best_result["transfer_stop"]
        tr = best_result["transfer_route"]
        response["transfer"] = {
            "stop": {"name": ts["name"], "lat": ts["lat"], "lng": ts["lng"]},
            "route": {"id": tr["id"], "name": tr["name"], "from": tr["from"], "to": tr["to"]},
        }
        response["bus_route"]["transfer_note"] = f"Change at {ts['name']} to {tr['name']}"

    return response


# ── Live Bus Tracking ────────────────────────────────────────────────
@app.post("/bus/update-location")
def update_bus_location(req: LocationUpdate, db: Session = Depends(get_db)):
    bus = db.query(LiveBusDB).filter(LiveBusDB.bus_reg == req.bus_reg).first()
    if bus:
        bus.latitude = req.latitude
        bus.longitude = req.longitude
        bus.speed = req.speed
        bus.route_id = req.route_id or bus.route_id
        bus.last_update = datetime.utcnow()
    else:
        bus = LiveBusDB(
            bus_reg=req.bus_reg,
            driver_id="",
            latitude=req.latitude,
            longitude=req.longitude,
            speed=req.speed,
            route_id=req.route_id,
        )
        db.add(bus)
    db.commit()
    return {"status": "ok", "bus_reg": req.bus_reg}

@app.post("/bus/update-passengers")
def update_passengers(req: PassengerCountUpdate, db: Session = Depends(get_db)):
    bus = db.query(LiveBusDB).filter(LiveBusDB.bus_reg == req.bus_reg).first()
    if not bus:
        raise HTTPException(status_code=404, detail="Bus not found")
    bus.passenger_count = req.passenger_count
    if req.passenger_count <= 20:
        bus.crowd_level = "Low"
    elif req.passenger_count <= 40:
        bus.crowd_level = "Medium"
    else:
        bus.crowd_level = "High"
    bus.last_update = datetime.utcnow()
    db.commit()
    return {"status": "ok", "crowd_level": bus.crowd_level, "passenger_count": bus.passenger_count}

@app.post("/bus/update-status")
def update_bus_status(req: StatusUpdate, db: Session = Depends(get_db)):
    bus = db.query(LiveBusDB).filter(LiveBusDB.bus_reg == req.bus_reg).first()
    if not bus:
        raise HTTPException(status_code=404, detail="Bus not found")
    bus.status = req.status
    bus.delay_reason = req.delay_reason
    bus.last_update = datetime.utcnow()
    db.commit()
    return {"status": "ok", "bus_status": bus.status}

@app.post("/bus/start-trip")
def start_trip(req: TripStartRequest, db: Session = Depends(get_db)):
    existing = db.query(LiveBusDB).filter(LiveBusDB.bus_reg == req.bus_reg).first()
    if existing:
        existing.status = "running"
        existing.route_id = req.route_id
        existing.passenger_count = 0
        existing.crowd_level = "Low"
        existing.delay_reason = ""
        existing.trip_started_at = datetime.utcnow()
        existing.last_update = datetime.utcnow()
    else:
        bus = LiveBusDB(
            bus_reg=req.bus_reg,
            driver_id="",
            route_id=req.route_id,
            trip_started_at=datetime.utcnow(),
        )
        db.add(bus)
    db.commit()
    return {"status": "trip_started", "bus_reg": req.bus_reg}

@app.post("/bus/end-trip")
def end_trip(bus_reg: str = Query(...), db: Session = Depends(get_db)):
    bus = db.query(LiveBusDB).filter(LiveBusDB.bus_reg == bus_reg).first()
    if bus:
        db.delete(bus)
        db.commit()
    return {"status": "trip_ended", "bus_reg": bus_reg}

@app.get("/bus/live")
def get_live_buses(db: Session = Depends(get_db)):
    buses = db.query(LiveBusDB).all()
    return {"buses": [
        {
            "bus_reg": b.bus_reg, "route_id": b.route_id, "latitude": b.latitude,
            "longitude": b.longitude, "speed": b.speed, "passenger_count": b.passenger_count,
            "crowd_level": b.crowd_level, "status": b.status, "delay_reason": b.delay_reason,
            "route_name": next((r["name"] for r in BUS_ROUTES if r["id"] == b.route_id), b.route_id),
            "route_info": next((f"{r['from']} → {r['to']}" for r in BUS_ROUTES if r["id"] == b.route_id), ""),
            "last_update": b.last_update.isoformat() if b.last_update else None,
        } for b in buses
    ]}

@app.get("/bus/{bus_reg}")
def get_bus(bus_reg: str, db: Session = Depends(get_db)):
    bus = db.query(LiveBusDB).filter(LiveBusDB.bus_reg == bus_reg.upper()).first()
    if not bus:
        raise HTTPException(status_code=404, detail="Bus not found or trip ended")
    return {
        "bus_reg": bus.bus_reg, "route_id": bus.route_id, "latitude": bus.latitude,
        "longitude": bus.longitude, "speed": bus.speed, "passenger_count": bus.passenger_count,
        "crowd_level": bus.crowd_level, "status": bus.status, "delay_reason": bus.delay_reason,
    }

# ── Crowd Levels ─────────────────────────────────────────────────────
@app.get("/crowd-levels")
def get_crowd_levels(db: Session = Depends(get_db)):
    buses = db.query(LiveBusDB).all()
    levels = []
    for b in buses:
        route = next((r for r in BUS_ROUTES if r["id"] == b.route_id), None)
        levels.append({
            "bus_reg": b.bus_reg,
            "route_id": b.route_id,
            "route_name": route["name"] if route else b.route_id,
            "route_info": f"{route['from']} → {route['to']}" if route else "",
            "passenger_count": b.passenger_count,
            "crowd_level": b.crowd_level,
            "status": b.status,
        })
    return {"crowd_data": levels}

# ── Tickets ──────────────────────────────────────────────────────────
@app.post("/tickets")
def book_ticket(req: TicketBookRequest, token: str = Query(...), db: Session = Depends(get_db)):
    user = decode_token(token)
    qr = f"ST-{uuid.uuid4().hex[:8].upper()}-{req.route_id}-{datetime.now().strftime('%Y%m%d%H%M')}"
    ticket = TicketDB(
        user_id=user["user_id"],
        route_id=req.route_id,
        route_name=req.route_name,
        from_stop=req.from_stop,
        to_stop=req.to_stop,
        fare=req.fare,
        qr_data=qr,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return {
        "ticket": {
            "id": ticket.id, "route_id": ticket.route_id, "route_name": ticket.route_name,
            "from_stop": ticket.from_stop, "to_stop": ticket.to_stop, "fare": ticket.fare,
            "status": ticket.status, "qr_data": ticket.qr_data,
            "booked_at": ticket.booked_at.isoformat(),
        }
    }

@app.get("/tickets")
def get_tickets(token: str = Query(...), db: Session = Depends(get_db)):
    user = decode_token(token)
    tickets = db.query(TicketDB).filter(TicketDB.user_id == user["user_id"]).order_by(TicketDB.booked_at.desc()).all()
    return {"tickets": [
        {
            "id": t.id, "route_id": t.route_id, "route_name": t.route_name,
            "from_stop": t.from_stop, "to_stop": t.to_stop, "fare": t.fare,
            "status": t.status, "qr_data": t.qr_data, "booked_at": t.booked_at.isoformat(),
        } for t in tickets
    ]}

# ── Saved Routes ─────────────────────────────────────────────────────
@app.post("/saved-routes")
def save_route(req: SaveRouteRequest, token: str = Query(...), db: Session = Depends(get_db)):
    user = decode_token(token)
    sr = SavedRouteDB(user_id=user["user_id"], name=req.name, from_place=req.from_place, to_place=req.to_place)
    db.add(sr)
    db.commit()
    db.refresh(sr)
    return {"saved_route": {"id": sr.id, "name": sr.name, "from_place": sr.from_place, "to_place": sr.to_place}}

@app.get("/saved-routes")
def get_saved_routes(token: str = Query(...), db: Session = Depends(get_db)):
    user = decode_token(token)
    routes = db.query(SavedRouteDB).filter(SavedRouteDB.user_id == user["user_id"]).order_by(SavedRouteDB.created_at.desc()).all()
    return {"saved_routes": [
        {"id": r.id, "name": r.name, "from_place": r.from_place, "to_place": r.to_place, "created_at": r.created_at.isoformat()} for r in routes
    ]}

@app.delete("/saved-routes/{route_id}")
def delete_saved_route(route_id: str, token: str = Query(...), db: Session = Depends(get_db)):
    user = decode_token(token)
    sr = db.query(SavedRouteDB).filter(SavedRouteDB.id == route_id, SavedRouteDB.user_id == user["user_id"]).first()
    if not sr:
        raise HTTPException(status_code=404, detail="Saved route not found")
    db.delete(sr)
    db.commit()
    return {"status": "deleted"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=True)
