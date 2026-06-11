"""
Barem Cars AI Asistanı — Backend v2
FastAPI + Gemini proxy + SQLite hafıza + Lead scoring
"""

import json
import re
import sqlite3
import uuid
import smtplib
import asyncio
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
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://baremcars.com", "http://localhost", "http://localhost:8766",
                   "http://127.0.0.1:8766", "http://localhost:8765", "http://127.0.0.1:8765",
                   "null", "*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY and not ANTHROPIC_KEY.startswith("your-") else None

# Vercel read-only filesystem → /tmp kullan; lokalde proje dizini
_IS_VERCEL = os.environ.get("VERCEL", "") == "1"
DB_PATH = "/tmp/veloai.db" if _IS_VERCEL else os.path.join(os.path.dirname(__file__), "veloai.db")
_notif_executor = ThreadPoolExecutor(max_workers=2)

# ── BİLDİRİM SİSTEMİ ─────────────────────────────────────────────────────────

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

def _send_email_sync(session: dict, events: list):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    notify_to = os.environ.get("NOTIFY_EMAIL", "")

    if not all([smtp_user, smtp_pass, notify_to]):
        return

    name  = session.get("customer_name") or "Bilinmiyor"
    phone = session.get("customer_phone") or "—"
    score = session.get("lead_score", 0)
    sid   = session.get("id", "")
    dealer_key = os.environ.get("DEALER_KEY", "barem2024")
    panel_url = f"http://localhost:8765/dealer?key={dealer_key}"

    event_rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #f0f0f0'>{SCORE_LABELS.get(e['event_type'], e['event_type'])}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #f0f0f0;text-align:right;color:#7c3aed;font-weight:700'>+{e['score_delta']}</td></tr>"
        for e in events
    )

    html = f"""
    <div style="font-family:Inter,system-ui,sans-serif;max-width:480px;margin:auto">
      <div style="background:linear-gradient(135deg,#7c3aed,#06b6d4);padding:20px 28px;border-radius:14px 14px 0 0">
        <div style="color:#fff;font-size:1.1rem;font-weight:800">🔥 Sıcak Lead — Barem Cars</div>
        <div style="color:rgba(255,255,255,.75);font-size:.82rem;margin-top:4px">VeloAI Lead Bildirimi</div>
      </div>
      <div style="background:#fff;border:1px solid #e2e8f0;border-top:none;padding:24px 28px;border-radius:0 0 14px 14px">
        <table style="width:100%;margin-bottom:16px">
          <tr><td style="color:#64748b;font-size:.82rem;padding:4px 0">Müşteri</td><td style="font-weight:700">{name}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:4px 0">Telefon</td><td style="font-weight:700;color:#7c3aed">{phone}</td></tr>
          <tr><td style="color:#64748b;font-size:.82rem;padding:4px 0">Lead Skoru</td><td><span style="background:#fef3c7;color:#d97706;padding:2px 10px;border-radius:100px;font-weight:800;font-size:.9rem">{score} puan</span></td></tr>
        </table>
        <div style="font-size:.78rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Aktiviteler</div>
        <table style="width:100%;font-size:.82rem">{event_rows}</table>
        <a href="{panel_url}" style="display:block;margin-top:20px;background:#7c3aed;color:#fff;text-align:center;padding:12px;border-radius:10px;text-decoration:none;font-weight:700">
          Dealer Panelini Aç →
        </a>
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔥 Sıcak Lead: {name} — {score} puan | Barem Cars"
    msg["From"]    = smtp_user
    msg["To"]      = notify_to
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[NOTIFY] Email gönderildi → {notify_to} (session={sid})")
    except Exception as e:
        print(f"[NOTIFY] Email hatası: {e}")


def _send_whatsapp_sync(session: dict):
    twilio_sid   = os.environ.get("TWILIO_SID", "")
    twilio_token = os.environ.get("TWILIO_TOKEN", "")
    wa_from      = os.environ.get("TWILIO_WA_FROM", "")  # whatsapp:+14155238886
    wa_to        = os.environ.get("TWILIO_WA_TO", "")    # whatsapp:+905XXXXXXXXX

    if not all([twilio_sid, twilio_token, wa_from, wa_to]):
        return

    name  = session.get("customer_name") or "Bilinmiyor"
    phone = session.get("customer_phone") or "—"
    score = session.get("lead_score", 0)

    text = (
        f"🔥 *Sıcak Lead — Barem Cars*\n\n"
        f"👤 Müşteri: {name}\n"
        f"📱 Telefon: {phone}\n"
        f"⭐ Skor: {score} puan\n\n"
        f"Dealer paneline gir → /dealer?key=barem2024"
    )

    try:
        import base64
        auth = base64.b64encode(f"{twilio_sid}:{twilio_token}".encode()).decode()
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"From": wa_from, "To": wa_to, "Body": text}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json",
            data=data,
            headers={"Authorization": f"Basic {auth}"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[NOTIFY] WhatsApp gönderildi → {wa_to}")
    except Exception as e:
        print(f"[NOTIFY] WhatsApp hatası: {e}")


def send_hot_lead_notification(session_id: str):
    with get_db() as conn:
        s = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        evs = conn.execute(
            "SELECT event_type, score_delta FROM lead_events WHERE session_id = ? ORDER BY id",
            (session_id,)
        ).fetchall()
    if not s:
        return
    session = dict(s)
    events  = [dict(e) for e in evs]
    _notif_executor.submit(_send_email_sync, session, events)
    _notif_executor.submit(_send_whatsapp_sync, session)

# ── DATABASE ──────────────────────────────────────────────────────────────────

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
        """)

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
    "page_open":          2,   # Siteyi açtı
    "car_viewed":         5,   # Araç baktı
    "price_asked":       10,   # Fiyat sordu
    "loan_calculated":   15,   # Kredi hesapladı
    "valuation_asked":   20,   # Aracını değerlettirmek istedi
    "phone_shared":      25,   # Telefon verdi
    "appointment_booked":40,   # Randevu aldı
    "whatsapp_clicked":  20,   # WhatsApp'a yönlendi
    "name_shared":       10,   # İsim verdi
    "budget_stated":     12,   # Bütçe belirtti
}

HOT_LEAD_THRESHOLD = 50

def add_event(session_id: str, event_type: str, data: dict = None):
    delta = SCORE_RULES.get(event_type, 0)
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO lead_events (session_id, event_type, data, score_delta, created_at) VALUES (?,?,?,?,?)",
            (session_id, event_type, json.dumps(data or {}), delta, now)
        )
        # Skoru güncelle
        conn.execute(
            "UPDATE sessions SET lead_score = lead_score + ?, last_active = ? WHERE id = ?",
            (delta, now, session_id)
        )
        # Hot lead kontrolü — sadece ilk kez eşiği geçtiğinde bildir
        row = conn.execute("SELECT lead_score, is_hot FROM sessions WHERE id = ?", (session_id,)).fetchone()
        just_became_hot = row and row["lead_score"] >= HOT_LEAD_THRESHOLD and not row["is_hot"]
        if just_became_hot:
            conn.execute("UPDATE sessions SET is_hot = 1 WHERE id = ?", (session_id,))
    if just_became_hot:
        send_hot_lead_notification(session_id)

def score_message(session_id: str, role: str, text: str, tool_calls: list = None):
    """Mesaj içeriğine bakarak lead event'leri tetikle."""
    if role == "user":
        t = text.lower()
        if any(w in t for w in ["kredi", "taksit", "aylık"]):
            add_event(session_id, "loan_calculated")
        if any(w in t for w in ["satmak", "değer", "ne kadar alır", "tramer"]):
            add_event(session_id, "valuation_asked")
        if any(w in t for w in ["bütçem", "bütçe", "elimde", "param"]):
            add_event(session_id, "budget_stated")
        # Telefon numarası paylaştı mı?
        if re.search(r"05\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", text):
            add_event(session_id, "phone_shared", {"phone": re.search(r"05[\d\s\-]{10,12}", text).group()})
            # Telefonu session'a kaydet
            phone = re.search(r"05[\d\s\-]{10,12}", text).group().replace(" ", "").replace("-", "")
            with get_db() as conn:
                conn.execute("UPDATE sessions SET customer_phone = ? WHERE id = ? AND customer_phone = ''",
                             (phone, session_id))

    if tool_calls:
        for tc in tool_calls:
            if tc.get("name") == "book_appointment":
                add_event(session_id, "appointment_booked", tc.get("args", {}))
                # İsim ve telefonu kaydet
                args = tc.get("args", {})
                with get_db() as conn:
                    if args.get("name"):
                        conn.execute("UPDATE sessions SET customer_name = ? WHERE id = ?",
                                     (args["name"], session_id))
                    if args.get("phone"):
                        conn.execute("UPDATE sessions SET customer_phone = ? WHERE id = ?",
                                     (args["phone"], session_id))
            elif tc.get("name") == "search_inventory":
                add_event(session_id, "car_viewed", tc.get("args", {}))
            elif tc.get("name") == "calculate_loan":
                add_event(session_id, "loan_calculated", tc.get("args", {}))
            elif tc.get("name") == "escalate_to_whatsapp":
                add_event(session_id, "whatsapp_clicked", tc.get("args", {}))
            elif tc.get("name") == "get_vehicle_valuation":
                add_event(session_id, "valuation_asked", tc.get("args", {}))

# ── SESSION ENDPOINTS ─────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    session_id: str = ""

@app.post("/session")
def create_session(body: SessionCreate = None):
    sid = (body.session_id if body and body.session_id else None) or str(uuid.uuid4())
    now = datetime.now().isoformat()
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
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
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
        "events": [dict(e) for e in events],
    }

# ── GEMINI PROXY (hafızalı) ───────────────────────────────────────────────────

class GeminiRequest(BaseModel):
    contents: list
    systemInstruction: dict | None = None
    tools: list | None = None
    generationConfig: dict | None = None
    session_id: str | None = None

@app.post("/gemini")
async def gemini_proxy(req: GeminiRequest):
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="Gemini key not configured")
    # Vercel'de session işlemlerini atla (ephemeral /tmp DB, SQLite unreliable)
    if _IS_VERCEL:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
        body = {"contents": req.contents}
        if req.systemInstruction: body["systemInstruction"] = req.systemInstruction
        if req.tools: body["tools"] = req.tools
        if req.generationConfig: body["generationConfig"] = req.generationConfig
        try:
            async with httpx.AsyncClient(timeout=55.0) as c:
                r = await c.post(url, json=body)
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail=r.text)
            return r.json()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Gemini timeout")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Son kullanıcı mesajını kaydet
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    body = {"contents": req.contents}
    if req.systemInstruction:
        body["systemInstruction"] = req.systemInstruction
    if req.tools:
        body["tools"] = req.tools
    if req.generationConfig:
        body["generationConfig"] = req.generationConfig

    async with httpx.AsyncClient(timeout=60.0) as client_http:
        r = await client_http.post(url, json=body)

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    resp = r.json()

    # AI yanıtını ve tool call'ları kaydet + puanla
    if session_id:
        try:
            candidates = resp.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                ai_text = " ".join(p.get("text", "") for p in parts if p.get("text"))
                tool_calls = [{"name": p["functionCall"]["name"], "args": p["functionCall"].get("args", {})}
                              for p in parts if p.get("functionCall")]
                if ai_text:
                    now = datetime.now().isoformat()
                    with get_db() as conn:
                        conn.execute(
                            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
                            (session_id, "assistant", ai_text, now)
                        )
                if tool_calls:
                    score_message(session_id, "assistant", "", tool_calls)
        except Exception:
            pass

    return resp

# ── LEADS ENDPOINT ────────────────────────────────────────────────────────────

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

@app.get("/dealer", response_class=HTMLResponse)
def dealer_panel(key: str = ""):
    if key != os.environ.get("DEALER_KEY", "barem2024"):
        return HTMLResponse("<h2>Yetkisiz erişim</h2>", status_code=403)

    with get_db() as conn:
        sessions = conn.execute(
            "SELECT * FROM sessions ORDER BY lead_score DESC LIMIT 100"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        hot   = conn.execute("SELECT COUNT(*) as c FROM sessions WHERE is_hot=1").fetchone()["c"]
        appts = conn.execute(
            "SELECT COUNT(*) as c FROM lead_events WHERE event_type='appointment_booked'"
        ).fetchone()["c"]

    rows_html = ""
    for s in sessions:
        score = s["lead_score"]
        badge_color = "#ef4444" if s["is_hot"] else ("#f59e0b" if score >= 25 else "#6b7280")
        name  = s["customer_name"] or "—"
        phone = s["customer_phone"] or "—"
        since = s["created_at"][:16].replace("T", " ")
        rows_html += f"""
        <tr onclick="loadSession('{s['id']}')" style="cursor:pointer">
          <td style="padding:10px 14px;border-bottom:1px solid #f0f0f0;font-size:.78rem;color:#64748b">{since}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #f0f0f0;font-weight:600">{name}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #f0f0f0;color:#64748b">{phone}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #f0f0f0;text-align:center">
            <span style="background:{badge_color};color:#fff;padding:2px 10px;border-radius:100px;font-size:.72rem;font-weight:700">{score}</span>
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #f0f0f0;text-align:center">
            {'<span style="color:#ef4444;font-weight:700">🔥 Sıcak</span>' if s['is_hot'] else '<span style="color:#94a3b8">—</span>'}
          </td>
        </tr>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VeloAI — Dealer Paneli</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Inter',system-ui,sans-serif;background:#f8fafc;color:#1e293b}}
  .top{{background:linear-gradient(135deg,#7c3aed,#06b6d4);padding:20px 32px;display:flex;align-items:center;gap:14px}}
  .top h1{{color:#fff;font-size:1.2rem;font-weight:800}}
  .top span{{color:rgba(255,255,255,.7);font-size:.8rem}}
  .stats{{display:flex;gap:16px;padding:24px 32px}}
  .stat{{background:#fff;border-radius:14px;padding:18px 24px;flex:1;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
  .stat-n{{font-size:2rem;font-weight:900;color:#7c3aed}}
  .stat-l{{font-size:.74rem;color:#64748b;margin-top:3px}}
  .panel{{background:#fff;margin:0 32px 32px;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,.07);overflow:hidden}}
  .panel-h{{padding:16px 20px;border-bottom:1px solid #f0f0f0;display:flex;align-items:center;justify-content:space-between}}
  .panel-h h2{{font-size:.95rem;font-weight:700}}
  table{{width:100%;border-collapse:collapse}}
  th{{padding:10px 14px;text-align:left;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;background:#f8fafc}}
  tr:hover{{background:#faf5ff}}
  .detail{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:99;padding:40px}}
  .detail-box{{background:#fff;border-radius:16px;max-width:600px;margin:auto;overflow:hidden;max-height:80vh;display:flex;flex-direction:column}}
  .detail-h{{padding:16px 20px;background:linear-gradient(135deg,#7c3aed,#06b6d4);color:#fff;display:flex;justify-content:space-between;align-items:center}}
  .detail-body{{overflow-y:auto;padding:20px}}
  .msg-u{{background:#f1f5f9;border-radius:10px;padding:8px 12px;margin:4px 0;font-size:.82rem}}
  .msg-a{{background:#faf5ff;border:1px solid rgba(124,58,237,.15);border-radius:10px;padding:8px 12px;margin:4px 0;font-size:.82rem}}
  .ev-item{{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #f0f0f0;font-size:.78rem}}
  .ev-score{{background:#7c3aed;color:#fff;padding:1px 7px;border-radius:100px;font-size:.67rem;font-weight:700;margin-left:auto}}
  .features{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;padding:0 32px 28px}}
  .feat{{background:#fff;border-radius:14px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.07);display:flex;gap:14px;align-items:flex-start;position:relative;overflow:hidden}}
  .feat-ico{{width:40px;height:40px;border-radius:11px;display:grid;place-items:center;font-size:1.15rem;flex-shrink:0}}
  .feat-title{{font-size:.86rem;font-weight:700;margin-bottom:3px}}
  .feat-desc{{font-size:.74rem;color:#64748b;line-height:1.5}}
  .feat-badge{{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:100px;font-size:.66rem;font-weight:700;margin-top:7px}}
  .badge-on{{background:#dcfce7;color:#16a34a}}
  .badge-ready{{background:#fef9c3;color:#a16207}}
  .badge-soon{{background:#f1f5f9;color:#64748b}}
  .toggle{{width:36px;height:20px;border-radius:100px;position:relative;flex-shrink:0;margin-left:auto;margin-top:2px}}
  .toggle-on{{background:#7c3aed}}
  .toggle-off{{background:#e2e8f0}}
  .toggle::after{{content:'';position:absolute;width:14px;height:14px;border-radius:50%;background:#fff;top:3px;transition:.2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}}
  .toggle-on::after{{right:3px}}
  .toggle-off::after{{left:3px}}
  .feat-stripe{{position:absolute;top:0;left:0;right:0;height:3px}}
</style>
</head>
<body>
<div class="top">
  <div style="width:34px;height:34px;background:rgba(255,255,255,.2);border-radius:9px;display:grid;place-items:center;font-size:.9rem;font-weight:900;color:#fff">V</div>
  <div>
    <h1>VeloAI Dealer Paneli</h1>
    <span>Barem Cars — Müşteri Zekası</span>
  </div>
  <div style="margin-left:auto;color:rgba(255,255,255,.8);font-size:.8rem">{datetime.now().strftime('%d.%m.%Y %H:%M')}</div>
</div>

<div class="stats">
  <div class="stat"><div class="stat-n">{total}</div><div class="stat-l">Toplam Ziyaretçi</div></div>
  <div class="stat"><div class="stat-n" style="color:#ef4444">{hot}</div><div class="stat-l">🔥 Sıcak Lead</div></div>
  <div class="stat"><div class="stat-n" style="color:#10b981">{appts}</div><div class="stat-l">✅ Randevu</div></div>
  <div class="stat"><div class="stat-n" style="color:#f59e0b">{round(hot/total*100) if total else 0}%</div><div class="stat-l">Dönüşüm Oranı</div></div>
</div>

<div style="padding:0 32px 10px;font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8">Sistem Özellikleri</div>
<div class="features">

  <div class="feat">
    <div class="feat-stripe" style="background:linear-gradient(90deg,#7c3aed,#06b6d4)"></div>
    <div class="feat-ico" style="background:#faf5ff;color:#7c3aed">🤖</div>
    <div style="flex:1">
      <div class="feat-title">AI Satış Asistanı</div>
      <div class="feat-desc">7/24 çalışan, araç bilen, randevu alan asistan</div>
      <span class="feat-badge badge-on">✓ Aktif</span>
    </div>
    <div class="toggle toggle-on"></div>
  </div>

  <div class="feat">
    <div class="feat-stripe" style="background:linear-gradient(90deg,#7c3aed,#06b6d4)"></div>
    <div class="feat-ico" style="background:#faf5ff;color:#7c3aed">🧠</div>
    <div style="flex:1">
      <div class="feat-title">Müşteri Hafızası</div>
      <div class="feat-desc">Her ziyaretçiyi tanır, önceki ilgisini hatırlar</div>
      <span class="feat-badge badge-on">✓ Aktif</span>
    </div>
    <div class="toggle toggle-on"></div>
  </div>

  <div class="feat">
    <div class="feat-stripe" style="background:linear-gradient(90deg,#7c3aed,#06b6d4)"></div>
    <div class="feat-ico" style="background:#faf5ff;color:#7c3aed">🎯</div>
    <div style="flex:1">
      <div class="feat-title">Lead Scoring</div>
      <div class="feat-desc">Ciddi alıcıyı otomatik tespit — bütçe, kredi, telefon sinyalleri</div>
      <span class="feat-badge badge-on">✓ Aktif</span>
    </div>
    <div class="toggle toggle-on"></div>
  </div>

  <div class="feat">
    <div class="feat-stripe" style="background:#fef9c3"></div>
    <div class="feat-ico" style="background:#fefce8;color:#ca8a04">📧</div>
    <div style="flex:1">
      <div class="feat-title">Email Bildirimi</div>
      <div class="feat-desc">Sıcak lead oluşunca satıcıya anında mail — müşteri kaçmaz</div>
      <span class="feat-badge badge-ready">⚡ Hazır — Aktif Edilebilir</span>
    </div>
    <div class="toggle toggle-off"></div>
  </div>

  <div class="feat">
    <div class="feat-stripe" style="background:#fef9c3"></div>
    <div class="feat-ico" style="background:#fefce8;color:#ca8a04">💬</div>
    <div style="flex:1">
      <div class="feat-title">WhatsApp Bildirimi</div>
      <div class="feat-desc">Sıcak lead oluşunca satıcının telefonuna WhatsApp mesajı</div>
      <span class="feat-badge badge-ready">⚡ Hazır — Aktif Edilebilir</span>
    </div>
    <div class="toggle toggle-off"></div>
  </div>

  <div class="feat">
    <div class="feat-stripe" style="background:#f1f5f9"></div>
    <div class="feat-ico" style="background:#f8fafc;color:#94a3b8">📉</div>
    <div style="flex:1">
      <div class="feat-title">Fiyat Takip Alarmı</div>
      <div class="feat-desc">Müşteri ilgilendiği araç fiyatı düşünce otomatik bildirim alır</div>
      <span class="feat-badge badge-soon">Yakında</span>
    </div>
    <div class="toggle toggle-off"></div>
  </div>

  <div class="feat">
    <div class="feat-stripe" style="background:#f1f5f9"></div>
    <div class="feat-ico" style="background:#f8fafc;color:#94a3b8">🔄</div>
    <div style="flex:1">
      <div class="feat-title">Canlı Stok Entegrasyonu</div>
      <div class="feat-desc">Stok sistemine bağlanır, araç satılınca AI otomatik güncellenir</div>
      <span class="feat-badge badge-soon">Yakında</span>
    </div>
    <div class="toggle toggle-off"></div>
  </div>

  <div class="feat">
    <div class="feat-stripe" style="background:#f1f5f9"></div>
    <div class="feat-ico" style="background:#f8fafc;color:#94a3b8">📸</div>
    <div style="flex:1">
      <div class="feat-title">Görsel Değerleme</div>
      <div class="feat-desc">Müşteri araç fotoğrafı gönderir, AI hasar ve değer tahmini yapar</div>
      <span class="feat-badge badge-soon">Yakında</span>
    </div>
    <div class="toggle toggle-off"></div>
  </div>

</div>

<div class="panel">
  <div class="panel-h">
    <h2>Ziyaretçi Listesi</h2>
    <span style="font-size:.76rem;color:#64748b">Satıra tıkla → konuşmayı gör</span>
  </div>
  <table>
    <thead><tr>
      <th>Tarih</th><th>İsim</th><th>Telefon</th><th style="text-align:center">Lead Skoru</th><th style="text-align:center">Durum</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<div class="detail" id="detail">
  <div class="detail-box">
    <div class="detail-h">
      <span id="d-title">Konuşma Detayı</span>
      <button onclick="document.getElementById('detail').style.display='none'" style="background:rgba(255,255,255,.2);border:none;color:#fff;width:28px;height:28px;border-radius:50%;cursor:pointer;font-size:1.1rem">×</button>
    </div>
    <div class="detail-body" id="d-body"></div>
  </div>
</div>

<script>
async function loadSession(id) {{
  const r = await fetch('/session/' + id);
  const d = await r.json();
  const s = d.session;
  document.getElementById('d-title').textContent = (s.customer_name || 'Ziyaretçi') + ' — Skor: ' + s.lead_score;

  let html = '<div style="margin-bottom:14px">';
  html += '<div style="font-size:.8rem;color:#64748b;margin-bottom:8px">📊 Aktiviteler</div>';
  for(const e of d.events) {{
    const icons = {{page_open:'👋',car_viewed:'🚗',price_asked:'💰',loan_calculated:'🏦',valuation_asked:'📋',phone_shared:'📱',appointment_booked:'✅',whatsapp_clicked:'💬',name_shared:'👤',budget_stated:'💵'}};
    html += `<div class="ev-item">${{icons[e.event_type]||'•'}} ${{e.event_type.replace(/_/g,' ')}} <span class="ev-score">+${{e.score_delta}}</span></div>`;
  }}
  html += '</div><div style="font-size:.8rem;color:#64748b;margin-bottom:8px">💬 Konuşma</div>';

  for(const m of d.messages) {{
    const cls = m.role === 'user' ? 'msg-u' : 'msg-a';
    const pfx = m.role === 'user' ? '👤 ' : '🤖 ';
    html += `<div class="${{cls}}">${{pfx}}${{m.content.substring(0,300)}}${{m.content.length>300?'...':''}}</div>`;
  }}

  document.getElementById('d-body').innerHTML = html;
  document.getElementById('detail').style.display = 'block';
}}
</script>
</body>
</html>""")

# ── CLAUDE /chat ENDPOINT (mevcut) ────────────────────────────────────────────

TOOLS_CLAUDE = [
    {
        "name": "search_inventory",
        "description": "Barem Cars araç envanterinde arama yapar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "body_type": {"type": "string", "enum": ["sedan", "suv", "hatchback", "coupe", "pickup", ""]},
                "fuel_type": {"type": "string", "enum": ["benzin", "dizel", "elektrik", "hybrid", ""]},
                "max_price": {"type": "integer"},
                "max_km": {"type": "integer"},
                "year_min": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "get_vehicle_valuation",
        "description": "Araç ön değerleme hesaplar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "model": {"type": "string"},
                "year": {"type": "integer"},
                "mileage": {"type": "integer"},
                "condition": {"type": "string", "enum": ["mükemmel", "iyi", "orta", "hasarlı"]},
            },
            "required": ["brand", "model", "year", "mileage"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Randevu oluşturur.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "phone": {"type": "string"},
                "vehicle_info": {"type": "string"},
                "appointment_type": {"type": "string", "enum": ["ekspertiz", "test_surüsü", "fiyat_görüşmesi"]},
                "preferred_date": {"type": "string"},
                "preferred_time": {"type": "string"},
            },
            "required": ["name", "phone", "appointment_type"],
        },
    },
    {
        "name": "calculate_loan",
        "description": "Kredi taksit hesabı.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle_price": {"type": "integer"},
                "down_payment": {"type": "integer"},
                "term_months": {"type": "integer"},
                "annual_rate": {"type": "number"},
            },
            "required": ["vehicle_price", "term_months"],
        },
    },
    {
        "name": "get_branch_info",
        "description": "Şube bilgileri.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "escalate_to_whatsapp",
        "description": "WhatsApp'a yönlendir.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "conversation_summary": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]

def search_inventory(brand="", model="", body_type="", fuel_type="", max_price=0, max_km=0, year_min=0) -> dict:
    demo_cars = [
        {"id":1,"brand":"Peugeot","model":"2008","year":2016,"km":177214,"fuel":"Benzin","transmission":"Manuel","body":"suv","price":879000},
        {"id":2,"brand":"Volkswagen","model":"Tiguan","year":2011,"km":221634,"fuel":"LPG","transmission":"Otomatik","body":"suv","price":749000},
        {"id":3,"brand":"Cupra","model":"Ateca","year":2024,"km":67592,"fuel":"Benzin","transmission":"Otomatik","body":"suv","price":2249000},
        {"id":4,"brand":"Tesla","model":"Model Y","year":2025,"km":23330,"fuel":"Elektrik","transmission":"Otomatik","body":"suv","price":2979000},
        {"id":5,"brand":"MG","model":"HS","year":2024,"km":26662,"fuel":"Benzin","transmission":"Otomatik","body":"suv","price":1759000},
        {"id":6,"brand":"Hyundai","model":"Getz","year":2009,"km":173703,"fuel":"Benzin","transmission":"Manuel","body":"hatchback","price":459000},
        {"id":7,"brand":"Citroen","model":"ë-C4","year":2025,"km":17265,"fuel":"Elektrik","transmission":"Otomatik","body":"hatchback","price":1759000},
        {"id":8,"brand":"Renault","model":"Clio","year":2021,"km":107639,"fuel":"Benzin","transmission":"Manuel","body":"hatchback","price":1029000},
        {"id":9,"brand":"Volkswagen","model":"Polo","year":2023,"km":64424,"fuel":"Benzin","transmission":"Otomatik","body":"hatchback","price":1219000},
        {"id":10,"brand":"Opel","model":"Astra","year":2015,"km":167178,"fuel":"Dizel","transmission":"Manuel","body":"sedan","price":869000},
    ]
    results = [c for c in demo_cars if
               (not brand or brand.lower() in c["brand"].lower()) and
               (not model or model.lower() in c["model"].lower()) and
               (not body_type or body_type.lower() == c["body"].lower()) and
               (not fuel_type or fuel_type.lower() in c["fuel"].lower()) and
               (not max_price or c["price"] <= max_price) and
               (not max_km or c["km"] <= max_km) and
               (not year_min or c["year"] >= year_min)]
    if not results:
        return {"found": 0, "message": "Bu kriterlerde stokta araç yok. Bütün stok gösteriliyor.", "cars": [
            {"id":c["id"],"title":f"{c['year']} {c['brand']} {c['model']}","km":f"{c['km']:,} km","fuel":c["fuel"],"price":f"{c['price']:,} TL"} for c in demo_cars[:4]
        ]}
    return {"found": len(results), "cars": [
        {"id":c["id"],"title":f"{c['year']} {c['brand']} {c['model']}","km":f"{c['km']:,} km","fuel":c["fuel"],"transmission":c["transmission"],"price":f"{c['price']:,} TL"} for c in results
    ]}

def get_vehicle_valuation(brand, model, year, mileage, fuel_type="benzin", transmission="manuel", condition="iyi") -> dict:
    base = 600_000
    age = datetime.now().year - year
    base *= max(0.5, 1 - age * 0.07)
    base *= max(0.6, 1 - mileage / 500_000)
    condition_mult = {"mükemmel": 1.05, "iyi": 1.0, "orta": 0.90, "hasarlı": 0.75}
    base *= condition_mult.get(condition, 1.0)
    low, high = int(base * 0.92), int(base * 1.05)
    return {
        "estimated_price_range": f"{low:,} TL – {high:,} TL",
        "estimated_price_low": low,
        "estimated_price_high": high,
        "message": f"{year} {brand} {model} için tahmini alım fiyatı: {low:,} – {high:,} TL. Kesin fiyat için ücretsiz ekspertiz randevusu alın.",
        "next_step": "Randevu için adınızı ve telefon numaranızı paylaşın.",
    }

def book_appointment(name, phone, appointment_type, vehicle_info="", preferred_date="", preferred_time="") -> dict:
    if not preferred_date: preferred_date = "en yakın müsait gün"
    if not preferred_time: preferred_time = "09:00-11:00"
    appointment_id = f"BRM{datetime.now().strftime('%d%m%H%M')}"
    return {
        "success": True,
        "appointment_id": appointment_id,
        "name": name,
        "phone": phone,
        "type": appointment_type,
        "vehicle_info": vehicle_info,
        "date": preferred_date,
        "time": preferred_time,
        "message": f"✅ Randevu alındı! No: {appointment_id} — {phone} numarasına SMS gönderilecek.",
    }

def calculate_loan(vehicle_price, term_months, down_payment=0, annual_rate=None) -> dict:
    if annual_rate is None: annual_rate = 42.0
    monthly_rate = annual_rate / 100 / 12
    principal = vehicle_price - down_payment
    if monthly_rate == 0:
        monthly_payment = principal / term_months
    else:
        monthly_payment = principal * monthly_rate / (1 - (1 + monthly_rate) ** -term_months)
    total = monthly_payment * term_months
    return {
        "monthly_payment": f"{monthly_payment:,.0f} TL",
        "total_payment": f"{total:,.0f} TL",
        "loan_amount": f"{principal:,} TL",
        "note": "Faiz tahminidir, kesin oran bankaya göre değişir.",
    }

def get_branch_info() -> dict:
    return {
        "branches": ["İstanbul Küçükçekmece", "İstanbul Yenibosna", "Ankara", "Hatay İskenderun"],
        "phone": "0 (553) 744 22 50",
        "whatsapp": "https://wa.me/905432505032",
        "hours": "Hafta içi 09:00–18:00 | Cumartesi 09:00–17:00",
    }

def escalate_to_whatsapp(reason, conversation_summary="") -> dict:
    import urllib.parse
    text = f"Merhaba, web sitesinden geliyorum. Konu: {reason}."
    if conversation_summary: text += f" Özet: {conversation_summary}"
    return {
        "whatsapp_link": f"https://wa.me/905432505032?text={urllib.parse.quote(text)}",
        "message": "Sizi uzmanımıza bağlıyoruz!",
    }

TOOL_HANDLERS = {
    "search_inventory": search_inventory,
    "get_vehicle_valuation": get_vehicle_valuation,
    "book_appointment": book_appointment,
    "calculate_loan": calculate_loan,
    "get_branch_info": get_branch_info,
    "escalate_to_whatsapp": escalate_to_whatsapp,
}

SYSTEM_PROMPT = f"""Sen Barem Cars'ın satış danışmanısın. 4 şube, 20+ yıl, 50.000+ müşteri.
Araç sorusunda direkt search_inventory çağır. Jargon kullanma — 'kompakt', 'orta SUV' de.
Randevu için ad + telefon al. Bugün: {date.today().strftime('%d %B %Y')}"""

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

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            for tb in text_blocks:
                yield f"data: {json.dumps({'type': 'text', 'content': tb.text})}\n\n"

            if not tool_uses:
                break

            current_messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tu in tool_uses:
                handler = TOOL_HANDLERS.get(tu.name)
                result = handler(**tu.input) if handler else {"error": f"Bilinmeyen tool: {tu.name}"}
                if tu.name == "escalate_to_whatsapp":
                    yield f"data: {json.dumps({'type': 'whatsapp', 'data': result})}\n\n"
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result, ensure_ascii=False)})
            current_messages.append({"role": "user", "content": tool_results})

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0"}

@app.get("/config")
def config():
    return {"status": "ok"}

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
