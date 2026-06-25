"""
Barem Cars AI Asistanı — Backend v3
FastAPI + Gemini proxy + SQLite + Lead scoring
Gerçek: Randevu → DB + Bildirim | WhatsApp gelen mesaj → AI yanıt | Araç CRUD | CRM
"""

import json
import re
import sqlite3
import uuid
import smtplib
import asyncio
import base64
import urllib.request
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import os
import httpx
import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://baremcars.com", "http://localhost", "http://localhost:8766",
                   "http://127.0.0.1:8766", "http://localhost:8765", "http://127.0.0.1:8765",
                   "null", "*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY and not ANTHROPIC_KEY.startswith("your-") else None

_IS_VERCEL = os.environ.get("VERCEL", "") == "1"
DB_PATH = "/tmp/veloai.db" if _IS_VERCEL else os.path.join(os.path.dirname(__file__), "veloai.db")
_notif_executor = ThreadPoolExecutor(max_workers=4)

# ── BİLDİRİM ─────────────────────────────────────────────────────────────────

SCORE_LABELS = {
    "page_open": "Siteyi açtı",
    "car_viewed": "Araç baktı",
    "price_asked": "Fiyat sordu",
    "loan_calculated": "Kredi hesapladı",
    "valuation_asked": "Araç değerlettirmek istedi",
    "phone_shared": "Telefon paylaştı",
    "appointment_booked": "Randevu aldı ✅",
    "whatsapp_clicked": "WhatsApp'a yönlendi",
    "name_shared": "İsim verdi",
    "budget_stated": "Bütçesini belirtti",
}


def _twilio_send(to: str, body: str):
    """Twilio REST API ile WhatsApp veya SMS gönder."""
    sid   = os.environ.get("TWILIO_SID", "")
    token = os.environ.get("TWILIO_TOKEN", "")
    wa_from = os.environ.get("TWILIO_WA_FROM", "")
    if not all([sid, token, wa_from]):
        return False
    try:
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        data = urllib.parse.urlencode({"From": wa_from, "To": to, "Body": body}).encode()
        req  = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=data, headers={"Authorization": f"Basic {auth}"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=12)
        return True
    except Exception as e:
        print(f"[TWILIO] Hata: {e}")
        return False


def _send_email_sync(subject: str, html: str):
    smtp_host  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port  = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user  = os.environ.get("SMTP_USER", "")
    smtp_pass  = os.environ.get("SMTP_PASS", "")
    notify_to  = os.environ.get("NOTIFY_EMAIL", "")
    if not all([smtp_user, smtp_pass, notify_to]):
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = notify_to
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as srv:
            srv.starttls()
            srv.login(smtp_user, smtp_pass)
            srv.send_message(msg)
        print(f"[EMAIL] Gönderildi → {notify_to}")
    except Exception as e:
        print(f"[EMAIL] Hata: {e}")


def _hot_lead_email_html(session: dict, events: list) -> str:
    name  = session.get("customer_name") or "Bilinmiyor"
    phone = session.get("customer_phone") or "—"
    score = session.get("lead_score", 0)
    dealer_key = os.environ.get("DEALER_KEY", "barem2024")
    panel_url  = f"http://localhost:8765/dealer?key={dealer_key}"
    rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #f0f0f0'>"
        f"{SCORE_LABELS.get(e['event_type'], e['event_type'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #f0f0f0;text-align:right;color:#7c3aed;font-weight:700'>"
        f"+{e['score_delta']}</td></tr>"
        for e in events
    )
    return f"""
    <div style="font-family:Inter,system-ui,sans-serif;max-width:480px;margin:auto">
      <div style="background:linear-gradient(135deg,#7c3aed,#06b6d4);padding:20px 28px;border-radius:14px 14px 0 0">
        <div style="color:#fff;font-size:1.1rem;font-weight:800">🔥 Sıcak Lead — Barem Cars</div>
        <div style="color:rgba(255,255,255,.75);font-size:.82rem;margin-top:4px">VeloAI Lead Bildirimi</div>
      </div>
      <div style="background:#fff;border:1px solid #e2e8f0;border-top:none;padding:24px 28px;border-radius:0 0 14px 14px">
        <table style="width:100%;margin-bottom:16px">
          <tr><td style="color:#64748b;font-size:.82rem;padding:4px 0">Müşteri</td><td style="font-weight:700">{name}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:4px 0">Telefon</td><td style="font-weight:700;color:#7c3aed">{phone}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:4px 0">Skor</td>
              <td><span style="background:#fef3c7;color:#d97706;padding:2px 10px;border-radius:100px;font-weight:800">{score} puan</span></td></tr>
        </table>
        <div style="font-size:.78rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Aktiviteler</div>
        <table style="width:100%;font-size:.82rem">{rows}</table>
        <a href="{panel_url}" style="display:block;margin-top:20px;background:#7c3aed;color:#fff;text-align:center;padding:12px;border-radius:10px;text-decoration:none;font-weight:700">
          Dealer Panelini Aç →
        </a>
      </div>
    </div>"""


def _appointment_email_html(appt: dict) -> str:
    type_labels = {"ekspertiz": "Ekspertiz", "test_surüsü": "Test Sürüşü", "fiyat_görüşmesi": "Fiyat Görüşmesi"}
    dealer_key = os.environ.get("DEALER_KEY", "barem2024")
    panel_url  = f"http://localhost:8765/dealer?key={dealer_key}#randevular"
    return f"""
    <div style="font-family:Inter,system-ui,sans-serif;max-width:480px;margin:auto">
      <div style="background:linear-gradient(135deg,#059669,#0891b2);padding:20px 28px;border-radius:14px 14px 0 0">
        <div style="color:#fff;font-size:1.1rem;font-weight:800">✅ Yeni Randevu — Barem Cars</div>
        <div style="color:rgba(255,255,255,.75);font-size:.82rem;margin-top:4px">No: {appt['id']}</div>
      </div>
      <div style="background:#fff;border:1px solid #e2e8f0;border-top:none;padding:24px 28px;border-radius:0 0 14px 14px">
        <table style="width:100%">
          <tr><td style="color:#64748b;font-size:.82rem;padding:5px 0">Müşteri</td><td style="font-weight:700">{appt['customer_name']}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:5px 0">Telefon</td><td style="font-weight:700;color:#7c3aed">{appt['customer_phone']}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:5px 0">Tür</td><td><b>{type_labels.get(appt['appointment_type'], appt['appointment_type'])}</b></td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:5px 0">Araç</td><td>{appt.get('vehicle_info') or '—'}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:5px 0">Tarih</td><td>{appt['preferred_date']}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:5px 0">Saat</td><td>{appt['preferred_time']}</td></tr>
        </table>
        <a href="{panel_url}" style="display:block;margin-top:20px;background:#059669;color:#fff;text-align:center;padding:12px;border-radius:10px;text-decoration:none;font-weight:700">
          Randevuları Gör →
        </a>
      </div>
    </div>"""


def send_hot_lead_notification(session_id: str):
    with get_db() as conn:
        s   = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        evs = conn.execute(
            "SELECT event_type, score_delta FROM lead_events WHERE session_id = ? ORDER BY id",
            (session_id,)
        ).fetchall()
    if not s:
        return
    session = dict(s)
    events  = [dict(e) for e in evs]
    html    = _hot_lead_email_html(session, events)
    name    = session.get("customer_name") or "Bilinmiyor"
    score   = session.get("lead_score", 0)
    wa_to   = os.environ.get("TWILIO_WA_TO", "")
    _notif_executor.submit(_send_email_sync,
        f"🔥 Sıcak Lead: {name} — {score} puan | Barem Cars", html)
    if wa_to:
        phone = session.get("customer_phone") or "—"
        wa_text = (
            f"🔥 *Sıcak Lead — Barem Cars*\n\n"
            f"👤 {name}\n📱 {phone}\n⭐ Skor: {score} puan\n\n"
            f"Dealer paneli → /dealer?key={os.environ.get('DEALER_KEY','barem2024')}"
        )
        _notif_executor.submit(_twilio_send, wa_to, wa_text)


# ── DATABASE ──────────────────────────────────────────────────────────────────

INITIAL_VEHICLES = [
    (1,"Peugeot","2008",2016,177214,"Benzin","Manuel","suv","B-SUV","küçük SUV",879000,"",31000,38,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321757418-1.jpg&w=500&h=350&zc=2&q=90"),
    (2,"Volkswagen","Tiguan",2011,221634,"LPG","Otomatik","suv","C-SUV","orta SUV",749000,"firsat",26000,61,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321735380-1.jpg&w=500&h=350&zc=2&q=90"),
    (3,"Cupra","Ateca",2024,67592,"Benzin","Otomatik","suv","C-SUV","orta SUV",2249000,"new",79000,124,4,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321718421-1.jpg&w=500&h=350&zc=2&q=90"),
    (4,"Tesla","Model Y",2025,23330,"Elektrik","Otomatik","suv","D-SUV","büyük SUV",2979000,"new",105000,218,7,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321387973-1.jpg&w=500&h=350&zc=2&q=90"),
    (5,"MG","HS",2024,26662,"Benzin","Otomatik","suv","C-SUV","orta SUV",1759000,"new",62000,97,3,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321553952-1.jpg&w=500&h=350&zc=2&q=90"),
    (6,"Hyundai","Getz",2009,173703,"Benzin","Manuel","hatchback","A","küçük araç",459000,"firsat",16000,29,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321581150-1.jpg&w=500&h=350&zc=2&q=90"),
    (7,"Citroen","ë-C4",2025,17265,"Elektrik","Otomatik","hatchback","C","orta hatchback",1759000,"new",62000,83,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321595841-1.jpg&w=500&h=350&zc=2&q=90"),
    (8,"Renault","Clio",2021,107639,"Benzin","Manuel","hatchback","B","kompakt",1029000,"",36000,45,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321610026-1.jpg&w=500&h=350&zc=2&q=90"),
    (9,"Volkswagen","Polo",2023,64424,"Benzin","Otomatik","hatchback","B","kompakt",1219000,"cert",43000,76,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321548205-1.jpg&w=500&h=350&zc=2&q=90"),
    (10,"Opel","Astra",2015,167178,"Dizel","Manuel","sedan","C","orta sedan",869000,"",31000,33,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321525319-1.jpg&w=500&h=350&zc=2&q=90"),
    (11,"Toyota","Corolla",2019,138500,"Benzin","Otomatik","sedan","C","orta sedan",949000,"cert",33000,67,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321480211-1.jpg&w=500&h=350&zc=2&q=90"),
    (12,"BMW","3 Serisi",2018,124200,"Dizel","Otomatik","sedan","D","büyük sedan",1459000,"",51000,112,3,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321462830-1.jpg&w=500&h=350&zc=2&q=90"),
    (13,"Mercedes-Benz","C 180",2017,156800,"Benzin","Otomatik","sedan","D","büyük sedan",1299000,"",46000,89,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321441503-1.jpg&w=500&h=350&zc=2&q=90"),
    (14,"Volkswagen","Golf",2020,98400,"Benzin","Otomatik","hatchback","C","orta hatchback",1189000,"cert",42000,94,3,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321503774-1.jpg&w=500&h=350&zc=2&q=90"),
    (15,"Ford","Focus",2018,142700,"Benzin","Manuel","hatchback","C","orta hatchback",849000,"firsat",30000,51,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321516988-1.jpg&w=500&h=350&zc=2&q=90"),
    (16,"SEAT","Leon",2019,119300,"Benzin","Otomatik","hatchback","C","orta hatchback",819000,"",29000,43,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321533614-1.jpg&w=500&h=350&zc=2&q=90"),
    (17,"Toyota","RAV4",2019,108600,"Benzin","Otomatik","suv","D-SUV","büyük SUV",1589000,"",56000,78,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321561427-1.jpg&w=500&h=350&zc=2&q=90"),
    (18,"Kia","Sportage",2020,84500,"Dizel","Otomatik","suv","C-SUV","orta SUV",1349000,"cert",48000,103,3,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321574662-1.jpg&w=500&h=350&zc=2&q=90"),
    (19,"Dacia","Duster",2022,67800,"LPG","Manuel","suv","B-SUV","küçük SUV",989000,"firsat",35000,58,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321587349-1.jpg&w=500&h=350&zc=2&q=90"),
    (20,"Fiat","Egea",2021,87300,"Benzin","Manuel","sedan","B","kompakt",849000,"",30000,39,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321540891-1.jpg&w=500&h=350&zc=2&q=90"),
    (21,"Renault","Megane",2019,131200,"Dizel","Manuel","hatchback","C","orta hatchback",879000,"firsat",31000,47,1,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321557183-1.jpg&w=500&h=350&zc=2&q=90"),
    (22,"Honda","Civic",2020,103700,"Benzin","Otomatik","sedan","C","orta sedan",1059000,"",37000,71,2,
     "https://www.baremcars.com/image.php?src=https://www.baremcars.com/images/ad-galeri/ilan-1321568945-1.jpg&w=500&h=350&zc=2&q=90"),
]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            created_at  TEXT NOT NULL,
            last_active TEXT NOT NULL,
            lead_score  INTEGER DEFAULT 0,
            is_hot      INTEGER DEFAULT 0,
            customer_name  TEXT DEFAULT '',
            customer_phone TEXT DEFAULT '',
            customer_email TEXT DEFAULT '',
            interested_cars TEXT DEFAULT '[]',
            notes       TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS lead_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            data       TEXT DEFAULT '{}',
            score_delta INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS appointments (
            id              TEXT PRIMARY KEY,
            session_id      TEXT DEFAULT '',
            customer_name   TEXT NOT NULL,
            customer_phone  TEXT NOT NULL,
            appointment_type TEXT NOT NULL,
            vehicle_info    TEXT DEFAULT '',
            preferred_date  TEXT DEFAULT '',
            preferred_time  TEXT DEFAULT '',
            branch          TEXT DEFAULT 'İstanbul Küçükçekmece',
            status          TEXT DEFAULT 'bekliyor',
            notes           TEXT DEFAULT '',
            source          TEXT DEFAULT 'web',
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vehicles (
            id              INTEGER PRIMARY KEY,
            brand           TEXT NOT NULL,
            model           TEXT NOT NULL,
            year            INTEGER,
            km              INTEGER DEFAULT 0,
            fuel            TEXT DEFAULT '',
            transmission    TEXT DEFAULT '',
            body            TEXT DEFAULT '',
            segment         TEXT DEFAULT '',
            size            TEXT DEFAULT '',
            price           INTEGER DEFAULT 0,
            tag             TEXT DEFAULT '',
            monthly_payment INTEGER DEFAULT 0,
            views           INTEGER DEFAULT 0,
            watchers        INTEGER DEFAULT 1,
            img             TEXT DEFAULT '',
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT
        );
        """)

        # Vehicles seed — sadece tablo boşsa çalışır
        count = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        if count == 0:
            now = datetime.now().isoformat()
            conn.executemany(
                """INSERT OR IGNORE INTO vehicles
                   (id,brand,model,year,km,fuel,transmission,body,segment,size,
                    price,tag,monthly_payment,views,watchers,img,is_active,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                [v + (now,) for v in INITIAL_VEHICLES]
            )

init_db()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── LEAD SCORING ──────────────────────────────────────────────────────────────

SCORE_RULES = {
    "page_open":          2,
    "car_viewed":         5,
    "price_asked":       10,
    "loan_calculated":   15,
    "valuation_asked":   20,
    "phone_shared":      25,
    "appointment_booked":40,
    "whatsapp_clicked":  20,
    "name_shared":        8,
    "budget_stated":     12,
}

HOT_THRESHOLD = 50


def add_event(session_id: str, event_type: str, data: dict = None):
    delta = SCORE_RULES.get(event_type, 0)
    now   = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO lead_events (session_id, event_type, data, score_delta, created_at) VALUES (?,?,?,?,?)",
            (session_id, event_type, json.dumps(data or {}), delta, now)
        )
        conn.execute(
            "UPDATE sessions SET lead_score = lead_score + ?, last_active = ? WHERE id = ?",
            (delta, now, session_id)
        )
        row = conn.execute("SELECT lead_score, is_hot FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row and row["lead_score"] >= HOT_THRESHOLD and not row["is_hot"]:
        with get_db() as conn:
            conn.execute("UPDATE sessions SET is_hot = 1 WHERE id = ?", (session_id,))
        _notif_executor.submit(send_hot_lead_notification, session_id)


def score_message(session_id: str, role: str, text: str, tool_calls: list = None):
    if not session_id:
        return
    lo = text.lower()
    if role == "user":
        if re.search(r"\b(fiyat|kaç|tutar|ücret|ne kadar)\b", lo):
            add_event(session_id, "price_asked")
        if re.search(r"\b(bütçe|maksimum|en fazla|kadar)\b", lo):
            add_event(session_id, "budget_stated")
        phone_m = re.search(r"(05\d{9}|\+90\s?5\d{9})", lo)
        if phone_m:
            add_event(session_id, "phone_shared")
            with get_db() as conn:
                conn.execute("UPDATE sessions SET customer_phone = ? WHERE id = ? AND customer_phone = ''",
                             (phone_m.group(), session_id))
        name_m = re.search(r"\b(benim adım|ismim|adım)\s+([a-züşğıöçÜŞĞİÖÇ][a-züşğıöçÜŞĞİÖÇ]+)", lo)
        if name_m:
            add_event(session_id, "name_shared")
            with get_db() as conn:
                conn.execute("UPDATE sessions SET customer_name = ? WHERE id = ? AND customer_name = ''",
                             (name_m.group(2), session_id))
    if tool_calls:
        for tc in tool_calls:
            if tc.get("name") == "book_appointment":
                args = tc.get("args", {})
                add_event(session_id, "appointment_booked", args)
                if args.get("name"):
                    with get_db() as conn:
                        conn.execute("UPDATE sessions SET customer_name = ? WHERE id = ? AND customer_name = ''",
                                     (args["name"], session_id))
                if args.get("phone"):
                    with get_db() as conn:
                        conn.execute("UPDATE sessions SET customer_phone = ? WHERE id = ? AND customer_phone = ''",
                                     (args["phone"], session_id))
            elif tc.get("name") == "calculate_loan":
                add_event(session_id, "loan_calculated", tc.get("args", {}))
            elif tc.get("name") == "get_vehicle_valuation":
                add_event(session_id, "valuation_asked", tc.get("args", {}))
            elif tc.get("name") == "escalate_to_whatsapp":
                add_event(session_id, "whatsapp_clicked", tc.get("args", {}))
            elif tc.get("name") == "search_inventory":
                add_event(session_id, "car_viewed", tc.get("args", {}))


# ── SESSION ───────────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    session_id: str | None = None


@app.post("/session")
def create_session(body: SessionCreate = None):
    sid    = (body.session_id if body and body.session_id else None) or str(uuid.uuid4())
    now    = datetime.now().isoformat()
    is_new = False
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO sessions (id, created_at, last_active) VALUES (?,?,?)",
                (sid, now, now)
            )
            is_new = True
    if is_new:
        add_event(sid, "page_open")
    return {"session_id": sid}


@app.get("/session/{session_id}")
def get_session(session_id: str):
    with get_db() as conn:
        row  = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Session bulunamadı")
        msgs = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        ).fetchall()
        events = conn.execute(
            "SELECT event_type, data, score_delta, created_at FROM lead_events WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        ).fetchall()
    return {
        "session": dict(row),
        "messages": [dict(m) for m in msgs],
        "events":   [dict(e) for e in events],
    }


# ── VEHICLES (Public + Admin CRUD) ────────────────────────────────────────────

class VehicleIn(BaseModel):
    brand: str
    model: str
    year: int
    km: int = 0
    fuel: str = ""
    transmission: str = ""
    body: str = ""
    segment: str = ""
    size: str = ""
    price: int = 0
    tag: str = ""
    monthly_payment: int = 0
    img: str = ""


@app.get("/vehicles")
def list_vehicles(body: str = "", fuel: str = "", max_price: int = 0, brand: str = ""):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM vehicles WHERE is_active=1 ORDER BY id ASC"
        ).fetchall()
    cars = [dict(r) for r in rows]
    if brand:
        cars = [c for c in cars if brand.lower() in c["brand"].lower()]
    if body:
        cars = [c for c in cars if body.lower() == c["body"].lower()]
    if fuel:
        cars = [c for c in cars if fuel.lower() in c["fuel"].lower()]
    if max_price:
        cars = [c for c in cars if c["price"] <= max_price]
    return {"cars": cars, "total": len(cars)}


@app.post("/vehicles")
def add_vehicle(v: VehicleIn, key: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        raise HTTPException(403, "Yetkisiz erişim")
    now = datetime.now().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO vehicles (brand,model,year,km,fuel,transmission,body,segment,size,
               price,tag,monthly_payment,img,is_active,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (v.brand, v.model, v.year, v.km, v.fuel, v.transmission, v.body,
             v.segment, v.size, v.price, v.tag, v.monthly_payment, v.img, now)
        )
        new_id = cur.lastrowid
    return {"id": new_id, "message": "Araç eklendi"}


@app.put("/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: int, v: VehicleIn, key: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        raise HTTPException(403, "Yetkisiz erişim")
    with get_db() as conn:
        conn.execute(
            """UPDATE vehicles SET brand=?,model=?,year=?,km=?,fuel=?,transmission=?,body=?,
               segment=?,size=?,price=?,tag=?,monthly_payment=?,img=? WHERE id=?""",
            (v.brand, v.model, v.year, v.km, v.fuel, v.transmission, v.body,
             v.segment, v.size, v.price, v.tag, v.monthly_payment, v.img, vehicle_id)
        )
    return {"message": "Araç güncellendi"}


@app.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, key: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        raise HTTPException(403, "Yetkisiz erişim")
    with get_db() as conn:
        conn.execute("UPDATE vehicles SET is_active=0 WHERE id=?", (vehicle_id,))
    return {"message": "Araç pasife alındı"}


# ── APPOINTMENTS ──────────────────────────────────────────────────────────────

class AppointmentIn(BaseModel):
    customer_name: str
    customer_phone: str
    appointment_type: str
    vehicle_info: str = ""
    preferred_date: str = ""
    preferred_time: str = ""
    branch: str = "İstanbul Küçükçekmece"
    session_id: str = ""


@app.post("/appointments")
def create_appointment(a: AppointmentIn):
    appt_id = f"BRM{datetime.now().strftime('%d%m%H%M%S')}"
    now     = datetime.now().isoformat()
    appt    = {
        "id": appt_id,
        "session_id": a.session_id,
        "customer_name": a.customer_name,
        "customer_phone": a.customer_phone,
        "appointment_type": a.appointment_type,
        "vehicle_info": a.vehicle_info,
        "preferred_date": a.preferred_date or "en yakın müsait gün",
        "preferred_time": a.preferred_time or "09:00–11:00",
        "branch": a.branch,
        "status": "bekliyor",
        "source": "web",
        "created_at": now,
    }
    with get_db() as conn:
        conn.execute(
            """INSERT INTO appointments
               (id,session_id,customer_name,customer_phone,appointment_type,
                vehicle_info,preferred_date,preferred_time,branch,status,source,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (appt["id"], appt["session_id"], appt["customer_name"], appt["customer_phone"],
             appt["appointment_type"], appt["vehicle_info"], appt["preferred_date"],
             appt["preferred_time"], appt["branch"], "bekliyor", "web", now)
        )
    html = _appointment_email_html(appt)
    _notif_executor.submit(
        _send_email_sync,
        f"✅ Yeni Randevu: {a.customer_name} — {appt_id} | Barem Cars",
        html
    )
    wa_to = os.environ.get("TWILIO_WA_TO", "")
    if wa_to:
        type_labels = {"ekspertiz": "Ekspertiz", "test_surüsü": "Test Sürüşü", "fiyat_görüşmesi": "Fiyat Görüşmesi"}
        wa_text = (
            f"✅ *Yeni Randevu — {appt_id}*\n\n"
            f"👤 {a.customer_name}\n"
            f"📱 {a.customer_phone}\n"
            f"🔧 {type_labels.get(a.appointment_type, a.appointment_type)}\n"
            f"🚗 {a.vehicle_info or '—'}\n"
            f"📅 {appt['preferred_date']} {appt['preferred_time']}"
        )
        _notif_executor.submit(_twilio_send, wa_to, wa_text)
    if a.session_id:
        add_event(a.session_id, "appointment_booked", {"appointment_id": appt_id})
    return {
        "success": True,
        "appointment_id": appt_id,
        "message": f"✅ Randevu alındı! No: {appt_id}",
    }


@app.get("/appointments")
def list_appointments(key: str = "", status: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        raise HTTPException(403, "Yetkisiz erişim")
    with get_db() as conn:
        q = "SELECT * FROM appointments"
        params: list = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT 200"
        rows = conn.execute(q, params).fetchall()
    return {"appointments": [dict(r) for r in rows], "total": len(rows)}


@app.patch("/appointments/{appt_id}")
def update_appointment_status(appt_id: str, status: str, key: str = "", notes: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        raise HTTPException(403, "Yetkisiz erişim")
    if status not in ("bekliyor", "onaylandi", "iptal", "tamamlandi"):
        raise HTTPException(400, "Geçersiz durum")
    with get_db() as conn:
        conn.execute(
            "UPDATE appointments SET status=?, notes=? WHERE id=?",
            (status, notes, appt_id)
        )
    return {"message": "Randevu güncellendi"}


# ── WHATSAPP GELEN MESAJ WEBHOOK ──────────────────────────────────────────────

GEMINI_SYS_WA = f"""Sen Barem Cars'ın WhatsApp satış asistanısın.
Kısa, samimi, Türkçe yanıt ver. Araç sorusunda bilgi ver, randevu öner.
Bugün: {date.today().strftime('%d %B %Y')}
Şubeler: İstanbul Küçükçekmece, Yenibosna, Ankara, Hatay İskenderun.
Tel: 0543 250 50 32 | Hafta içi 09:00-18:00"""

WA_SESSIONS: dict[str, list] = {}


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    """Twilio'dan gelen WhatsApp mesajlarını karşıla, AI yanıtı gönder."""
    form   = await request.form()
    body   = str(form.get("Body", "")).strip()
    wa_from = str(form.get("From", ""))  # whatsapp:+905XXXXXXXXX

    if not body or not wa_from:
        return PlainTextResponse('<?xml version="1.0"?><Response></Response>',
                                 media_type="text/xml")

    phone = wa_from.replace("whatsapp:", "").strip()
    history = WA_SESSIONS.get(phone, [])
    history.append({"role": "user", "parts": [{"text": body}]})

    ai_reply = "Merhaba! Şu an yoğunuz, kısa süre içinde size döneceğiz. 0543 250 50 32"
    try:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if key:
            async with httpx.AsyncClient(timeout=12.0) as hc:
                r = await hc.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
                    json={
                        "contents": history[-10:],
                        "systemInstruction": {"parts": [{"text": GEMINI_SYS_WA}]},
                        "generationConfig": {"maxOutputTokens": 300},
                    }
                )
            if r.status_code == 200:
                parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                ai_reply = " ".join(p.get("text", "") for p in parts if p.get("text")).strip()
                if ai_reply:
                    history.append({"role": "model", "parts": [{"text": ai_reply}]})
                    WA_SESSIONS[phone] = history[-20:]
    except Exception as e:
        print(f"[WA_AI] Hata: {e}")

    # Twilio TwiML yanıt
    safe = ai_reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    twiml = f'<?xml version="1.0"?><Response><Message>{safe}</Message></Response>'
    return PlainTextResponse(twiml, media_type="text/xml")


# ── GEMINI PROXY (hafızalı) ───────────────────────────────────────────────────

class GeminiRequest(BaseModel):
    contents: list
    systemInstruction: dict | None = None
    tools: list | None = None
    generationConfig: dict | None = None
    session_id: str | None = None


async def _notify_ntfy(contents):
    """Demoda gerçek kullanıcı mesajı gelince sahibe sessiz push (ziyaretçi hiçbir şey görmez)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    try:
        last = (contents or [])[-1]
        if last.get("role") != "user":
            return
        text = " ".join(
            p.get("text", "") for p in last.get("parts", [])
            if isinstance(p, dict) and p.get("text")
        )
        if not text.strip():
            return  # tool/function takip çağrısı — bildirme
        async with httpx.AsyncClient(timeout=4.0) as c:
            await c.post(
                f"https://ntfy.sh/{topic}",
                content=text.strip()[:180].encode("utf-8"),
                headers={"Title": "Barem demosu kullaniliyor",
                         "Tags": "car,bell", "Priority": "high"},
            )
    except Exception:
        pass


@app.post("/gemini")
@app.post("/api/gemini")
async def gemini_proxy(req: GeminiRequest):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=500, detail="Gemini key yapılandırılmamış")

    if _IS_VERCEL:
        url  = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        body = {"contents": req.contents}
        if req.systemInstruction: body["systemInstruction"] = req.systemInstruction
        if req.tools:             body["tools"] = req.tools
        gen_cfg = dict(req.generationConfig or {})
        gen_cfg.setdefault("thinkingConfig", {"thinkingBudget": 0})
        body["generationConfig"] = gen_cfg
        try:
            async with httpx.AsyncClient(timeout=55.0) as c:
                r = await c.post(url, json=body)
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            data = r.json()
            await _notify_ntfy(req.contents)
            return data
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Gemini timeout")

    session_id = req.session_id
    if session_id and req.contents:
        last = req.contents[-1]
        if last.get("role") == "user":
            user_text = " ".join(p.get("text", "") for p in last.get("parts", []) if isinstance(p, dict))
            now = datetime.now().isoformat()
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                    (session_id, "user", user_text, now)
                )
                conn.execute("UPDATE sessions SET last_active = ? WHERE id = ?", (now, session_id))
            score_message(session_id, "user", user_text)

    url  = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    body = {"contents": req.contents}
    if req.systemInstruction: body["systemInstruction"] = req.systemInstruction
    if req.tools:             body["tools"] = req.tools
    gen_cfg = dict(req.generationConfig or {})
    gen_cfg.setdefault("thinkingConfig", {"thinkingBudget": 0})
    body["generationConfig"] = gen_cfg

    async with httpx.AsyncClient(timeout=60.0) as hc:
        r = await hc.post(url, json=body)

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    resp = r.json()
    await _notify_ntfy(req.contents)

    if session_id:
        try:
            parts = resp.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            ai_text    = " ".join(p.get("text", "") for p in parts if p.get("text"))
            tool_calls = [{"name": p["functionCall"]["name"], "args": p["functionCall"].get("args", {})}
                          for p in parts if p.get("functionCall")]
            if ai_text:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                        (session_id, "assistant", ai_text, datetime.now().isoformat())
                    )
            if tool_calls:
                score_message(session_id, "assistant", "", tool_calls)
        except Exception:
            pass

    return resp


# ── LEADS ─────────────────────────────────────────────────────────────────────

@app.get("/leads")
def get_leads(hot_only: bool = False, limit: int = 50, key: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        raise HTTPException(403, "Yetkisiz erişim")
    with get_db() as conn:
        q = "SELECT * FROM sessions"
        if hot_only:
            q += " WHERE is_hot = 1"
        q += " ORDER BY last_active DESC LIMIT ?"
        rows = conn.execute(q, (limit,)).fetchall()
    return {"leads": [dict(r) for r in rows], "total": len(rows)}


# ── DEALER PANELİ ─────────────────────────────────────────────────────────────

@app.get("/dealer", response_class=HTMLResponse)
def dealer_panel(key: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        return HTMLResponse("<h2>Yetkisiz erişim</h2>", status_code=403)

    with get_db() as conn:
        sessions  = conn.execute("SELECT * FROM sessions ORDER BY lead_score DESC LIMIT 100").fetchall()
        total     = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        hot       = conn.execute("SELECT COUNT(*) as c FROM sessions WHERE is_hot=1").fetchone()["c"]
        appts_all = conn.execute("SELECT * FROM appointments ORDER BY created_at DESC LIMIT 200").fetchall()
        appts_cnt = conn.execute("SELECT COUNT(*) as c FROM appointments").fetchone()["c"]
        vehicles  = conn.execute("SELECT * FROM vehicles WHERE is_active=1 ORDER BY id").fetchall()
        v_count   = conn.execute("SELECT COUNT(*) as c FROM vehicles WHERE is_active=1").fetchone()["c"]

    dk = os.environ.get("DEALER_KEY", "barem2024")

    # Lead rows
    lead_rows = ""
    for s in sessions:
        score = s["lead_score"]
        badge = "#ef4444" if s["is_hot"] else ("#f59e0b" if score >= 25 else "#6b7280")
        name  = s["customer_name"] or "—"
        phone = s["customer_phone"] or "—"
        since = s["created_at"][:16].replace("T", " ")
        lead_rows += f"""
        <tr onclick="loadSession('{s['id']}')" style="cursor:pointer">
          <td>{since}</td><td style="font-weight:600">{name}</td><td>{phone}</td>
          <td style="text-align:center">
            <span style="background:{badge};color:#fff;padding:2px 10px;border-radius:100px;font-size:.72rem;font-weight:700">{score}</span>
          </td>
          <td style="text-align:center">{'<span style="color:#ef4444;font-weight:700">🔥 Sıcak</span>' if s['is_hot'] else '<span style="color:#94a3b8">—</span>'}</td>
        </tr>"""

    # Appointment rows
    status_colors = {"bekliyor": "#f59e0b", "onaylandi": "#10b981", "iptal": "#ef4444", "tamamlandi": "#6b7280"}
    status_labels = {"bekliyor": "Bekliyor", "onaylandi": "Onaylandı", "iptal": "İptal", "tamamlandi": "Tamamlandı"}
    type_labels   = {"ekspertiz": "Ekspertiz", "test_surüsü": "Test Sürüşü", "fiyat_görüşmesi": "Fiyat Görüşmesi"}
    appt_rows = ""
    for a in appts_all:
        sc = status_colors.get(a["status"], "#6b7280")
        sl = status_labels.get(a["status"], a["status"])
        tl = type_labels.get(a["appointment_type"], a["appointment_type"])
        dt = a["created_at"][:16].replace("T", " ")
        appt_rows += f"""
        <tr>
          <td>{dt}</td><td style="font-weight:600">{a['customer_name']}</td>
          <td>{a['customer_phone']}</td><td>{tl}</td>
          <td>{(a['vehicle_info'] if 'vehicle_info' in a.keys() else None) or '—'}</td>
          <td>{a['preferred_date']} {a['preferred_time']}</td>
          <td style="text-align:center">
            <span style="background:{sc};color:#fff;padding:2px 10px;border-radius:100px;font-size:.7rem;font-weight:700">{sl}</span>
          </td>
          <td style="text-align:center">
            <button onclick="updateAppt('{a['id']}','onaylandi','{dk}')" style="background:#10b981;color:#fff;border:none;padding:3px 8px;border-radius:6px;cursor:pointer;font-size:.7rem;margin-right:3px">✓</button>
            <button onclick="updateAppt('{a['id']}','iptal','{dk}')" style="background:#ef4444;color:#fff;border:none;padding:3px 8px;border-radius:6px;cursor:pointer;font-size:.7rem">✗</button>
          </td>
        </tr>"""

    # Vehicle rows
    veh_rows = ""
    for v in vehicles:
        price_fmt = f"{v['price']:,} TL".replace(",", ".")
        veh_rows += f"""
        <tr>
          <td style="font-weight:600">{v['brand']} {v['model']}</td>
          <td>{v['year']}</td><td>{v['km']:,} km</td>
          <td>{v['fuel']}</td><td>{v['transmission']}</td>
          <td style="font-weight:700;color:#7c3aed">{price_fmt}</td>
          <td style="text-align:center">
            <span style="background:{'#dcfce7;color:#16a34a' if v['tag']=='cert' else '#fef3c7;color:#d97706' if v['tag']=='firsat' else '#faf5ff;color:#7c3aed' if v['tag']=='new' else '#f1f5f9;color:#64748b'};
                  padding:2px 8px;border-radius:100px;font-size:.68rem;font-weight:700">{v['tag'] or '—'}</span>
          </td>
          <td style="text-align:center">
            <button onclick="deleteVehicle({v['id']},'{dk}')" style="background:#ef4444;color:#fff;border:none;padding:3px 8px;border-radius:6px;cursor:pointer;font-size:.7rem">Sil</button>
          </td>
        </tr>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VeloAI — Dealer Paneli</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',system-ui,sans-serif;background:#f8fafc;color:#1e293b}}
.top{{background:linear-gradient(135deg,#7c3aed,#06b6d4);padding:20px 32px;display:flex;align-items:center;gap:14px}}
.top h1{{color:#fff;font-size:1.2rem;font-weight:800}}
.stats{{display:flex;gap:16px;padding:24px 32px 8px}}
.stat{{background:#fff;border-radius:14px;padding:18px 24px;flex:1;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.stat-n{{font-size:2rem;font-weight:900;color:#7c3aed}}
.stat-l{{font-size:.74rem;color:#64748b;margin-top:3px}}
.tabs{{display:flex;gap:0;padding:20px 32px 0;border-bottom:2px solid #e2e8f0;margin:0 0 0}}
.tab{{padding:10px 20px;cursor:pointer;font-size:.84rem;font-weight:600;color:#64748b;border-bottom:2px solid transparent;margin-bottom:-2px;transition:.15s}}
.tab.active{{color:#7c3aed;border-bottom-color:#7c3aed}}
.panel{{background:#fff;margin:0 32px 32px;border-radius:0 0 14px 14px;box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden}}
.panel-h{{padding:14px 20px;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;justify-content:space-between;background:#f8fafc}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}
th{{padding:9px 12px;text-align:left;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;background:#f8fafc}}
td{{padding:9px 12px;border-bottom:1px solid #f0f0f0}}
tr:hover td{{background:#faf5ff}}
.pane{{display:none;padding:20px 32px 32px}}
.pane.active{{display:block}}
.form-row{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}}
.fg{{display:flex;flex-direction:column;gap:5px;flex:1;min-width:140px}}
.fg label{{font-size:.72rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.05em}}
.fg input,.fg select{{border:1.5px solid #e2e8f0;border-radius:8px;padding:8px 10px;font-size:.84rem;font-family:inherit}}
.fg input:focus,.fg select:focus{{outline:none;border-color:#7c3aed}}
.btn-add{{background:#7c3aed;color:#fff;border:none;padding:10px 20px;border-radius:10px;cursor:pointer;font-size:.84rem;font-weight:700;margin-top:4px}}
.detail{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99;padding:40px}}
.detail-box{{background:#fff;border-radius:16px;max-width:600px;margin:auto;overflow:hidden;max-height:80vh;display:flex;flex-direction:column}}
.detail-h{{padding:16px 20px;background:linear-gradient(135deg,#7c3aed,#06b6d4);color:#fff;display:flex;justify-content:space-between;align-items:center}}
.detail-body{{overflow-y:auto;padding:20px}}
.msg-u{{background:#f1f5f9;border-radius:10px;padding:8px 12px;margin:4px 0;font-size:.82rem}}
.msg-a{{background:#faf5ff;border:1px solid rgba(124,58,237,.15);border-radius:10px;padding:8px 12px;margin:4px 0;font-size:.82rem}}
.ev-item{{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #f0f0f0;font-size:.78rem}}
.ev-score{{background:#7c3aed;color:#fff;padding:1px 7px;border-radius:100px;font-size:.67rem;font-weight:700;margin-left:auto}}
</style>
</head>
<body>
<div class="top">
  <div style="width:34px;height:34px;background:rgba(255,255,255,.2);border-radius:9px;display:grid;place-items:center;font-weight:900;color:#fff">V</div>
  <div><h1>VeloAI Dealer Paneli</h1><span style="color:rgba(255,255,255,.7);font-size:.8rem">Barem Cars</span></div>
  <div style="margin-left:auto;color:rgba(255,255,255,.8);font-size:.8rem">{datetime.now().strftime('%d.%m.%Y %H:%M')}</div>
</div>

<div class="stats">
  <div class="stat"><div class="stat-n">{total}</div><div class="stat-l">Toplam Ziyaretçi</div></div>
  <div class="stat"><div class="stat-n" style="color:#ef4444">{hot}</div><div class="stat-l">🔥 Sıcak Lead</div></div>
  <div class="stat"><div class="stat-n" style="color:#10b981">{appts_cnt}</div><div class="stat-l">✅ Randevu</div></div>
  <div class="stat"><div class="stat-n" style="color:#0891b2">{v_count}</div><div class="stat-l">🚗 Aktif Araç</div></div>
  <div class="stat"><div class="stat-n" style="color:#f59e0b">{round(hot/total*100) if total else 0}%</div><div class="stat-l">Dönüşüm</div></div>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('leads')">Leadler ({total})</div>
  <div class="tab" onclick="showTab('randevular')">Randevular ({appts_cnt})</div>
  <div class="tab" onclick="showTab('araclar')">Araç Yönetimi ({v_count})</div>
</div>

<!-- LEADLER -->
<div id="tab-leads" class="panel" style="margin-top:0;border-radius:0 14px 14px 14px">
  <div class="panel-h"><h2 style="font-size:.95rem;font-weight:700">Ziyaretçi Listesi</h2>
    <span style="font-size:.76rem;color:#64748b">Satıra tıkla → konuşmayı gör</span></div>
  <table>
    <thead><tr><th>Tarih</th><th>İsim</th><th>Telefon</th><th style="text-align:center">Skor</th><th style="text-align:center">Durum</th></tr></thead>
    <tbody>{lead_rows}</tbody>
  </table>
</div>

<!-- RANDEVULAR -->
<div id="tab-randevular" class="panel" style="display:none;margin-top:0;border-radius:0 14px 14px 14px">
  <div class="panel-h"><h2 style="font-size:.95rem;font-weight:700">Randevular</h2></div>
  <table>
    <thead><tr><th>Tarih</th><th>Müşteri</th><th>Telefon</th><th>Tür</th><th>Araç</th><th>Zaman</th><th style="text-align:center">Durum</th><th style="text-align:center">İşlem</th></tr></thead>
    <tbody>{appt_rows if appt_rows else '<tr><td colspan="8" style="text-align:center;padding:30px;color:#94a3b8">Henüz randevu yok</td></tr>'}</tbody>
  </table>
</div>

<!-- ARAÇ YÖNETİMİ -->
<div id="tab-araclar" class="panel" style="display:none;padding:20px 32px 32px">
  <div style="background:#fff;border-radius:14px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.07)">
    <h3 style="font-size:.9rem;font-weight:700;margin-bottom:14px">Yeni Araç Ekle</h3>
    <div class="form-row">
      <div class="fg"><label>Marka</label><input id="v-brand" placeholder="Toyota"></div>
      <div class="fg"><label>Model</label><input id="v-model" placeholder="Corolla"></div>
      <div class="fg"><label>Yıl</label><input id="v-year" type="number" placeholder="2021"></div>
      <div class="fg"><label>Km</label><input id="v-km" type="number" placeholder="85000"></div>
    </div>
    <div class="form-row">
      <div class="fg"><label>Yakıt</label>
        <select id="v-fuel"><option>Benzin</option><option>Dizel</option><option>LPG</option><option>Elektrik</option><option>Hybrid</option></select></div>
      <div class="fg"><label>Vites</label>
        <select id="v-trans"><option>Manuel</option><option>Otomatik</option></select></div>
      <div class="fg"><label>Kasa</label>
        <select id="v-body"><option>sedan</option><option>hatchback</option><option>suv</option><option>stationwagon</option></select></div>
      <div class="fg"><label>Fiyat (TL)</label><input id="v-price" type="number" placeholder="950000"></div>
    </div>
    <div class="form-row">
      <div class="fg"><label>Etiket</label>
        <select id="v-tag"><option value="">—</option><option value="new">Yeni</option><option value="cert">Sertifikalı</option><option value="firsat">Fırsat</option></select></div>
      <div class="fg" style="flex:3"><label>Fotoğraf URL</label><input id="v-img" placeholder="https://..."></div>
    </div>
    <button class="btn-add" onclick="addVehicle('{dk}')">+ Araç Ekle</button>
    <span id="v-msg" style="font-size:.78rem;color:#10b981;margin-left:12px"></span>
  </div>
  <div style="background:#fff;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden">
    <div class="panel-h"><h2 style="font-size:.95rem;font-weight:700">Aktif Araçlar</h2></div>
    <table>
      <thead><tr><th>Araç</th><th>Yıl</th><th>Km</th><th>Yakıt</th><th>Vites</th><th>Fiyat</th><th style="text-align:center">Etiket</th><th style="text-align:center">İşlem</th></tr></thead>
      <tbody>{veh_rows}</tbody>
    </table>
  </div>
</div>

<!-- KONUŞMA DETAYI -->
<div class="detail" id="detail">
  <div class="detail-box">
    <div class="detail-h">
      <span id="d-title">Konuşma Detayı</span>
      <button onclick="document.getElementById('detail').style.display='none'"
        style="background:rgba(255,255,255,.2);border:none;color:#fff;width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:1.1rem">×</button>
    </div>
    <div class="detail-body" id="d-body"></div>
  </div>
</div>

<script>
function showTab(name) {{
  ['leads','randevular','araclar'].forEach(t => {{
    document.getElementById('tab-'+t).style.display = t===name ? '' : 'none';
    document.querySelectorAll('.tab')[['leads','randevular','araclar'].indexOf(t)].classList.toggle('active', t===name);
  }});
}}

async function loadSession(id) {{
  const r = await fetch('/session/' + id);
  const d = await r.json();
  const s = d.session;
  document.getElementById('d-title').textContent = (s.customer_name || 'Ziyaretçi') + ' — Skor: ' + s.lead_score;
  const icons = {{page_open:'👋',car_viewed:'🚗',price_asked:'💰',loan_calculated:'🏦',valuation_asked:'📋',phone_shared:'📱',appointment_booked:'✅',whatsapp_clicked:'💬',name_shared:'👤',budget_stated:'💵'}};
  let html = '<div style="margin-bottom:14px"><div style="font-size:.8rem;color:#64748b;margin-bottom:8px">📊 Aktiviteler</div>';
  for(const e of d.events)
    html += `<div class="ev-item">${{icons[e.event_type]||'•'}} ${{e.event_type.replace(/_/g,' ')}} <span class="ev-score">+${{e.score_delta}}</span></div>`;
  html += '</div><div style="font-size:.8rem;color:#64748b;margin-bottom:8px">💬 Konuşma</div>';
  for(const m of d.messages) {{
    const cls = m.role==='user' ? 'msg-u' : 'msg-a';
    html += `<div class="${{cls}}">${{m.role==='user'?'👤 ':'🤖 '}}${{m.content.substring(0,300)}}${{m.content.length>300?'...':''}}</div>`;
  }}
  document.getElementById('d-body').innerHTML = html;
  document.getElementById('detail').style.display = 'block';
}}

async function updateAppt(id, status, key) {{
  await fetch(`/appointments/${{id}}?status=${{status}}&key=${{key}}`, {{method:'PATCH'}});
  location.reload();
}}

async function deleteVehicle(id, key) {{
  if(!confirm('Bu aracı pasife almak istediğinize emin misiniz?')) return;
  await fetch(`/vehicles/${{id}}?key=${{key}}`, {{method:'DELETE'}});
  location.reload();
}}

async function addVehicle(key) {{
  const brand = document.getElementById('v-brand').value.trim();
  const model = document.getElementById('v-model').value.trim();
  const year  = parseInt(document.getElementById('v-year').value) || 0;
  const km    = parseInt(document.getElementById('v-km').value) || 0;
  const price = parseInt(document.getElementById('v-price').value) || 0;
  if(!brand||!model||!year||!price) {{ document.getElementById('v-msg').textContent='Zorunlu alanları doldurun'; return; }}
  const body = {{
    brand, model, year, km, price,
    fuel: document.getElementById('v-fuel').value,
    transmission: document.getElementById('v-trans').value,
    body: document.getElementById('v-body').value,
    tag: document.getElementById('v-tag').value,
    img: document.getElementById('v-img').value,
    segment:'', size:'', monthly_payment: Math.round(price/28)
  }};
  const r = await fetch(`/vehicles?key=${{key}}`, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
  const d = await r.json();
  if(d.id) {{ document.getElementById('v-msg').textContent='✅ Eklendi (ID: '+d.id+')'; setTimeout(()=>location.reload(),1000); }}
  else document.getElementById('v-msg').textContent = 'Hata: ' + JSON.stringify(d);
}}
</script>
</body></html>""")


# ── TOOL FUNCTIONS ────────────────────────────────────────────────────────────

TOOLS_CLAUDE = [
    {"name": "search_inventory", "description": "Barem Cars araç envanterinde arama yapar.",
     "input_schema": {"type": "object", "properties": {
         "brand": {"type": "string"}, "body_type": {"type": "string"},
         "fuel_type": {"type": "string"}, "max_price": {"type": "integer"},
         "max_km": {"type": "integer"}, "year_min": {"type": "integer"}}, "required": []}},
    {"name": "get_vehicle_valuation", "description": "Araç ön değerleme hesaplar.",
     "input_schema": {"type": "object", "properties": {
         "brand": {"type": "string"}, "model": {"type": "string"},
         "year": {"type": "integer"}, "mileage": {"type": "integer"},
         "condition": {"type": "string", "enum": ["mükemmel", "iyi", "orta", "hasarlı"]}},
         "required": ["brand", "model", "year", "mileage"]}},
    {"name": "book_appointment", "description": "Randevu oluşturur.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "phone": {"type": "string"},
         "vehicle_info": {"type": "string"},
         "appointment_type": {"type": "string", "enum": ["ekspertiz", "test_surüsü", "fiyat_görüşmesi"]},
         "preferred_date": {"type": "string"}, "preferred_time": {"type": "string"}},
         "required": ["name", "phone", "appointment_type"]}},
    {"name": "calculate_loan", "description": "Kredi taksit hesabı.",
     "input_schema": {"type": "object", "properties": {
         "vehicle_price": {"type": "integer"}, "down_payment": {"type": "integer"},
         "term_months": {"type": "integer"}, "annual_rate": {"type": "number"}},
         "required": ["vehicle_price", "term_months"]}},
    {"name": "get_branch_info", "description": "Şube bilgileri.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "escalate_to_whatsapp", "description": "WhatsApp'a yönlendir.",
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string"}, "conversation_summary": {"type": "string"}},
         "required": ["reason"]}},
]


def search_inventory(brand="", model="", body_type="", fuel_type="", max_price=0, max_km=0, year_min=0) -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM vehicles WHERE is_active=1").fetchall()
    cars = [dict(r) for r in rows]
    if brand:      cars = [c for c in cars if brand.lower() in c["brand"].lower()]
    if model:      cars = [c for c in cars if model.lower() in c["model"].lower()]
    if body_type:  cars = [c for c in cars if body_type.lower() == c["body"].lower()]
    if fuel_type:  cars = [c for c in cars if fuel_type.lower() in c["fuel"].lower()]
    if max_price:  cars = [c for c in cars if c["price"] <= max_price]
    if max_km:     cars = [c for c in cars if c["km"] <= max_km]
    if year_min:   cars = [c for c in cars if c["year"] >= year_min]
    if not cars:
        with get_db() as conn:
            fallback = [dict(r) for r in conn.execute("SELECT * FROM vehicles WHERE is_active=1 LIMIT 4").fetchall()]
        return {"found": 0, "message": "Bu kriterlerde stokta araç yok.", "cars": [
            {"id": c["id"], "title": f"{c['year']} {c['brand']} {c['model']}",
             "km": f"{c['km']:,} km", "fuel": c["fuel"], "price": f"{c['price']:,} TL"} for c in fallback]}
    return {"found": len(cars), "cars": [
        {"id": c["id"], "title": f"{c['year']} {c['brand']} {c['model']}",
         "km": f"{c['km']:,} km", "fuel": c["fuel"], "transmission": c["transmission"],
         "price": f"{c['price']:,} TL"} for c in cars]}


def get_vehicle_valuation(brand, model, year, mileage, fuel_type="benzin", transmission="manuel", condition="iyi") -> dict:
    brand_base = {
        "Mercedes-Benz": 4_200_000, "BMW": 3_600_000, "Audi": 3_400_000, "Tesla": 2_900_000,
        "Toyota": 1_900_000, "Honda": 2_100_000, "Volkswagen": 2_050_000, "Ford": 1_750_000,
        "Hyundai": 1_750_000, "Kia": 1_550_000, "Renault": 1_300_000, "Peugeot": 1_550_000,
        "Opel": 1_300_000, "Fiat": 1_250_000, "Dacia": 1_050_000, "SEAT": 1_550_000,
        "MG": 1_400_000, "Cupra": 2_200_000, "Citroen": 1_400_000,
    }
    base = brand_base.get(brand, 1_600_000)
    age  = datetime.now().year - year
    base *= max(0.35, 1 - age * 0.08)
    base *= max(0.55, 1 - mileage / 450_000)
    cond_mult = {"mükemmel": 1.05, "iyi": 1.0, "orta": 0.88, "hasarlı": 0.72}
    base *= cond_mult.get(condition, 1.0)
    low, high = int(base * 0.92), int(base * 1.06)
    return {
        "estimated_price_range": f"{low:,} TL – {high:,} TL",
        "estimated_price_low": low,
        "estimated_price_high": high,
        "message": f"{year} {brand} {model} için tahmini alım fiyatı: {low:,} – {high:,} TL. Kesin fiyat fiziksel ekspertiz sonrası belirlenir.",
        "next_step": "Ücretsiz ekspertiz randevusu almak ister misiniz?",
    }


def book_appointment(name, phone, appointment_type, vehicle_info="", preferred_date="", preferred_time="") -> dict:
    appt_id = f"BRM{datetime.now().strftime('%d%m%H%M%S')}"
    now     = datetime.now().isoformat()
    appt    = {
        "id": appt_id, "session_id": "",
        "customer_name": name, "customer_phone": phone,
        "appointment_type": appointment_type, "vehicle_info": vehicle_info,
        "preferred_date": preferred_date or "en yakın müsait gün",
        "preferred_time": preferred_time or "09:00–11:00",
        "branch": "İstanbul Küçükçekmece",
    }
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO appointments
                   (id,session_id,customer_name,customer_phone,appointment_type,
                    vehicle_info,preferred_date,preferred_time,branch,status,source,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (appt_id, "", name, phone, appointment_type, vehicle_info,
                 appt["preferred_date"], appt["preferred_time"],
                 "İstanbul Küçükçekmece", "bekliyor", "chat", now)
            )
        html = _appointment_email_html(appt)
        _notif_executor.submit(
            _send_email_sync,
            f"✅ Yeni Randevu (chat): {name} — {appt_id}",
            html
        )
        wa_to = os.environ.get("TWILIO_WA_TO", "")
        if wa_to:
            type_labels = {"ekspertiz": "Ekspertiz", "test_surüsü": "Test Sürüşü", "fiyat_görüşmesi": "Fiyat Görüşmesi"}
            _notif_executor.submit(_twilio_send, wa_to,
                f"✅ *Chat Randevu — {appt_id}*\n👤 {name}\n📱 {phone}\n"
                f"🔧 {type_labels.get(appointment_type, appointment_type)}\n🚗 {vehicle_info or '—'}")
    except Exception as e:
        print(f"[APPT] Kayıt hatası: {e}")
    return {
        "success": True,
        "appointment_id": appt_id,
        "name": name, "phone": phone,
        "type": appointment_type,
        "date": appt["preferred_date"],
        "time": appt["preferred_time"],
        "message": f"✅ Randevu kaydedildi! No: {appt_id} — Galeriden sizi arayacaklar.",
    }


def calculate_loan(vehicle_price, term_months, down_payment=0, annual_rate=None) -> dict:
    if annual_rate is None: annual_rate = 42.0
    rate      = annual_rate / 100 / 12
    principal = vehicle_price - down_payment
    if rate == 0:
        monthly = principal / term_months
    else:
        monthly = principal * rate / (1 - (1 + rate) ** -term_months)
    total = monthly * term_months
    return {
        "monthly_payment": f"{monthly:,.0f} TL",
        "total_payment":   f"{total:,.0f} TL",
        "loan_amount":     f"{principal:,} TL",
        "note": "Faiz tahminidir, kesin oran bankaya göre değişir.",
    }


def get_branch_info() -> dict:
    return {
        "branches": ["İstanbul Küçükçekmece", "İstanbul Yenibosna", "Ankara", "Hatay İskenderun"],
        "phone": "0 (543) 250 50 32",
        "whatsapp": "https://wa.me/905432505032",
        "hours": "Hafta içi 09:00–18:00 | Cumartesi 09:00–17:00",
    }


def escalate_to_whatsapp(reason, conversation_summary="") -> dict:
    text = f"Merhaba, web sitesinden geliyorum. Konu: {reason}."
    if conversation_summary: text += f" Özet: {conversation_summary}"
    return {
        "whatsapp_link": f"https://wa.me/905432505032?text={urllib.parse.quote(text)}",
        "message": "Sizi uzmanımıza bağlıyoruz!",
    }


TOOL_HANDLERS = {
    "search_inventory":    search_inventory,
    "get_vehicle_valuation": get_vehicle_valuation,
    "book_appointment":    book_appointment,
    "calculate_loan":      calculate_loan,
    "get_branch_info":     get_branch_info,
    "escalate_to_whatsapp": escalate_to_whatsapp,
}

SYSTEM_PROMPT = f"""Sen Barem Cars'ın satış danışmanısın. 4 şube, 20+ yıl, 50.000+ müşteri.
Araç sorusunda direkt search_inventory çağır. Jargon kullanma — 'kompakt', 'orta SUV' de.
Randevu için ad + telefon al, book_appointment çağır. Bugün: {date.today().strftime('%d %B %Y')}"""


class ChatRequest(BaseModel):
    messages: list[dict]
    session_id: str = ""


@app.post("/chat")
async def chat(req: ChatRequest):
    if not client:
        raise HTTPException(500, "Claude key yapılandırılmamış")
    messages = req.messages
    if not messages:
        raise HTTPException(400, "messages boş olamaz")

    async def event_stream():
        current_messages = list(messages)
        for _ in range(10):
            with client.messages.stream(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS_CLAUDE,
                messages=current_messages,
            ) as stream:
                response = stream.get_final_message()

            tool_uses   = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            for tb in text_blocks:
                yield f"data: {json.dumps({'type': 'text', 'content': tb.text})}\n\n"

            if not tool_uses:
                break

            current_messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tu in tool_uses:
                handler = TOOL_HANDLERS.get(tu.name)
                result  = handler(**tu.input) if handler else {"error": f"Bilinmeyen tool: {tu.name}"}
                if tu.name == "escalate_to_whatsapp":
                    yield f"data: {json.dumps({'type': 'whatsapp', 'data': result})}\n\n"
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": json.dumps(result, ensure_ascii=False)})
            current_messages.append({"role": "user", "content": tool_results})

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── UTIL ──────────────────────────────────────────────────────────────────────

@app.get("/command-center/stats")
def command_center_stats():
    with get_db() as conn:
        sessions    = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        hot_leads   = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_hot=1").fetchone()[0]
        appts       = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        appts_done  = conn.execute("SELECT COUNT(*) FROM appointments WHERE status='tamamlandi'").fetchone()[0]
        veh_active  = conn.execute("SELECT COUNT(*) FROM vehicles WHERE is_active=1").fetchone()[0]
        stock_val   = conn.execute("SELECT COALESCE(SUM(price),0) FROM vehicles WHERE is_active=1").fetchone()[0]
        msgs        = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return {
        "sessions": sessions, "hot_leads": hot_leads,
        "appointments": appts, "appointments_done": appts_done,
        "vehicles_active": veh_active, "total_stock_value": stock_val,
        "total_messages": msgs,
    }


@app.post("/command-center/query")
async def command_center_query(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()
    if not question:
        raise HTTPException(400, "Soru boş olamaz")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise HTTPException(500, "Gemini key yapılandırılmamış")
    with get_db() as conn:
        sessions   = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        hot_leads  = conn.execute("SELECT COUNT(*) FROM sessions WHERE is_hot=1").fetchone()[0]
        appts      = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        veh_active = conn.execute("SELECT COUNT(*) FROM vehicles WHERE is_active=1").fetchone()[0]
        stock_val  = conn.execute("SELECT COALESCE(SUM(price),0) FROM vehicles WHERE is_active=1").fetchone()[0]
        top_veh    = conn.execute(
            "SELECT brand, model, price, views FROM vehicles WHERE is_active=1 ORDER BY views DESC LIMIT 5"
        ).fetchall()
    ctx = (
        f"Sen bir otomotiv bayi yönetim platformu analitik asistanısın. "
        f"Mevcut işletme verileri: toplam etkileşim={sessions}, sıcak lead={hot_leads}, "
        f"randevu={appts}, aktif araç={veh_active}, stok değeri=₺{stock_val:,}. "
        f"En çok ilgi gören araçlar: "
        + ", ".join(f"{r[0]} {r[1]} (₺{r[2]:,}, {r[3]} görüntüleme)" for r in top_veh)
        + ". Soruya kısa, veri bazlı, bullet-point formatında Türkçe yanıt ver (maks 4 madde)."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": f"{ctx}\n\nSoru: {question}"}]}],
        "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.3, "thinkingConfig": {"thinkingBudget": 0}}
    }
    async with httpx.AsyncClient(timeout=30.0) as hc:
        r = await hc.post(url, json=payload)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    try:
        parts  = r.json()["candidates"][0]["content"]["parts"]
        answer = " ".join(p.get("text", "") for p in parts if p.get("text"))
    except Exception:
        answer = "Analiz tamamlanamadı."
    return {"answer": answer, "question": question}


@app.get("/health")
def health():
    with get_db() as conn:
        v_cnt = conn.execute("SELECT COUNT(*) FROM vehicles WHERE is_active=1").fetchone()[0]
        a_cnt = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    return {"status": "ok", "version": "3.0", "vehicles": v_cnt, "appointments": a_cnt}


@app.get("/config")
def config():
    return {
        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
        "email":  bool(os.environ.get("SMTP_USER")),
        "whatsapp_notify": bool(os.environ.get("TWILIO_SID")),
        "whatsapp_incoming": bool(os.environ.get("TWILIO_SID")),
    }


@app.get("/demo.html", response_class=HTMLResponse)
def serve_demo():
    demo_path = os.path.join(os.path.dirname(__file__), "demo.html")
    try:
        with open(demo_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "demo.html bulunamadı")


@app.get("/", response_class=HTMLResponse)
def serve_root():
    return serve_demo()
