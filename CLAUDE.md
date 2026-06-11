# VeloAI — Barem Cars Demo

## Proje Nedir
Türk ikinci el araba galerileri için AI destekli satış platformu.
İlk hedef müşteri: **Barem Cars** (baremcars.com) — 4 şube, İstanbul/Ankara/Hatay.
SaaS hedefi: ₺20.000/ay/galeri. İlk 2 ay tanıtım: ₺9.000/ay.

## Dosya Yapısı
```
barem_ai/
├── demo.html          ← Ana ürün (single-file SPA, tüm CSS+HTML+JS)
├── backend.py         ← FastAPI: Gemini proxy, SQLite hafıza, lead scoring, dealer paneli
├── requirements.txt   ← Python bağımlılıkları
├── .env               ← API anahtarları (git'e gitmiyor)
├── veloai.db          ← SQLite veritabanı (git'e gitmiyor)
├── render.yaml        ← Render.com deploy config
├── start.sh           ← Tek tıkla başlatma scripti
├── PITCH.md           ← Barem toplantı senaryosu
└── CLAUDE.md          ← Bu dosya
```

## Lokal Başlatma
```bash
cd /home/bba1/barem_ai
bash start.sh
# Demo  → http://localhost:8766/demo.html
# Panel → http://localhost:8765/dealer?key=barem2024
```

Manuel başlatma:
```bash
./venv/bin/uvicorn backend:app --host 0.0.0.0 --port 8765
python3 -m http.server 8766  # demo.html için
```

## Mimari

### Frontend (demo.html)
- Single-file SPA — Vercel/GitHub Pages'a tek dosya olarak deploy edilir
- **Gemini 2.5 Flash** ile AI chat (function calling)
- **BACKEND_URL**: localhost'ta `localhost:8765`, production'da `https://barem-ai.onrender.com`
- Web Speech API ile sesli giriş/çıkış (tr-TR)
- 10 gerçek Barem Cars aracı (kendi CDN'inden fotoğraflar)

### Backend (backend.py)
- **FastAPI** + uvicorn, port 8765
- `/gemini` — Gemini API proxy (key gizli, HTML'de yok)
- `/session` — UUID tabanlı müşteri session'ı oluştur/getir
- `/session/{id}` — Konuşma geçmişi + lead event'leri
- `/leads?key=` — Hot lead listesi (JSON)
- `/dealer?key=` — Dealer paneli (HTML)
- `/chat` — Claude Haiku endpoint (alternatif, opsiyonel)

### Veritabanı (SQLite — veloai.db)
Tablolar:
- `sessions` — Her ziyaretçi (UUID, skor, isim, telefon, is_hot)
- `messages` — Konuşma geçmişi (role, content, timestamp)
- `lead_events` — Scoring olayları (event_type, score_delta)

## Lead Scoring Mantığı
| Event | Puan |
|-------|------|
| page_open | +2 |
| car_viewed | +5 |
| budget_stated | +12 |
| loan_calculated | +15 |
| valuation_asked | +20 |
| phone_shared | +25 |
| whatsapp_clicked | +20 |
| appointment_booked | +40 |

**50 puan = Sıcak Lead** → Email + WhatsApp bildirimi tetiklenir (şu an kapalı, .env'e key girince açılır)

## AI Davranışı (Sistem Promptu)
- Deneyimli arabacı gibi konuşur
- Müşteri araç tarif edince ÖNCE `search_inventory` çağırır, SONRA soru sorar
- **Jargon yasak**: "D segment" değil "büyük SUV", "B segment" değil "kompakt"
- Segmentleri içsel olarak bilir ama müşteriye kullanmaz
- Tools: `search_inventory`, `get_valuation`, `calculate_loan`, `book_appointment`, `get_branch_info`, `escalate_to_whatsapp`

## Araç Kartları
CARS array'indeki her araç:
- `img`: baremcars.com CDN'den gerçek fotoğraf
- `size`: "küçük SUV" / "orta SUV" / "kompakt" vs. (AI filtreleme için)
- `seg`: B-SUV / C-SUV / D-SUV vs. (içsel kullanım)
- `views`, `watchers`: Sosyal kanıt gösterimi
- `trend`: Sparkline için 12 günlük veri

## Bildirim Sistemi (Hazır, Pasif)
Kod tamamen yazılmış, sadece .env doldurmak gerekiyor:
- **Email**: Gmail SMTP — `SMTP_USER`, `SMTP_PASS` (16 haneli uygulama şifresi)
- **WhatsApp**: Twilio — `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_WA_TO`

## Deployment

### Frontend (GitHub Pages)
Repo: `https://github.com/serdarbba/veloai-barem`
URL: `https://serdarbba.github.io/veloai-barem/demo.html`
- `main` branch'e push → otomatik deploy (`.github/workflows/deploy.yml`)

### Backend (Render.com)
1. render.com → New Web Service → GitHub repo bağla
2. `render.yaml` ayarları otomatik okunur
3. Environment Variables'a Gemini key gir
4. Deploy URL'yi `demo.html`'deki `onrender.com` satırına yaz

## .env Şablonu
```
ANTHROPIC_API_KEY=           # Opsiyonel (Claude endpoint için)
GEMINI_API_KEY=              # Zorunlu
DEALER_KEY=barem2024         # Dealer panel şifresi
NOTIFY_EMAIL=                # Bildirim gidecek email
SMTP_USER=                   # Gmail adresi
SMTP_PASS=                   # 16 haneli uygulama şifresi
TWILIO_SID=                  # Twilio Account SID
TWILIO_TOKEN=                # Twilio Auth Token
TWILIO_WA_FROM=whatsapp:+14155238886
TWILIO_WA_TO=whatsapp:+90XXXXXXXXXXX
```

## Yapılacaklar (Sonraki Sprint)
- [ ] Render.com backend deploy + URL güncelle
- [ ] Email bildirimi aktif et (Gmail uygulama şifresi)
- [ ] WhatsApp bildirimi aktif et (Twilio)
- [ ] Barem Cars toplantısı → PITCH.md senaryosunu takip et
- [ ] Toplantı sonrası: gerçek stok API entegrasyonu

## Pitch Özeti
Barem'e gösterilecek sıra:
1. Araç listesi (gerçek fotoğraflar, filtreler)
2. AI chat — "orta boy SUV bakıyorum" → anında araç gösteriyor
3. Sesli komut demo
4. Dealer paneli — lead scoring, konuşma geçmişi
5. Özellikler grid'i — yeşil/sarı/gri toggle'lar
6. Fiyat: ₺9.000 (2 ay) → ₺20.000/ay

## Teknik Notlar
- Gemini key HTML'de ASLA yok — sadece `.env`'de, backend proxy üzerinden çalışır
- SQLite WAL modu aktif (concurrent write sorununu çözer)
- `add_event()` → `with get_db()` bloğu DIŞINDA çağrılmalı (SQLite lock önlemi)
- `body_type` filtresinde `.toLowerCase()` — Gemini büyük harf döndürüyor
- `just_became_hot` flag'i: bildirim sadece bir kez gönderilir
