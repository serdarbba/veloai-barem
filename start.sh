#!/bin/bash
cd "$(dirname "$0")"

# Eski processleri kapat
kill $(lsof -ti:8765) 2>/dev/null
kill $(lsof -ti:8766) 2>/dev/null
sleep 1

# Backend başlat
./venv/bin/uvicorn backend:app --host 0.0.0.0 --port 8765 &
BACKEND_PID=$!

# Statik dosya sunucusu (demo.html için)
python3 -m http.server 8766 &
STATIC_PID=$!

sleep 2

# Tarayıcıda aç
xdg-open http://localhost:8766/demo.html 2>/dev/null || \
  open http://localhost:8766/demo.html 2>/dev/null || \
  echo "Tarayıcıda şu adresi aç: http://localhost:8766/demo.html"

echo ""
echo "✅ VeloAI çalışıyor"
echo "   Demo   → http://localhost:8766/demo.html"
echo "   Panel  → http://localhost:8765/dealer?key=barem2024"
echo ""
echo "Durdurmak için: Ctrl+C"

wait
