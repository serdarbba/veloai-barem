"""
Vercel Serverless Function — Gemini proxy
Gemini API key'i güvenli tutar, tarayıcıdan gizler.
"""
import json
import os
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self._cors()
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            key = os.environ.get("GEMINI_API_KEY", "")
            if not key:
                self._json({"error": "Gemini key yapılandırılmamış"}, 500)
                return

            url = (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/gemini-2.5-flash:generateContent?key={key}"
            )

            # session_id backend'e gönderilmiyor (sadece Gemini'ye gidecek kısım)
            payload = {k: v for k, v in body.items() if k != "session_id"}

            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                self._json(data, 200)

        except urllib.error.HTTPError as e:
            self._json({"error": e.read().decode()}, e.code)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data, status):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Vercel loglarını temiz tut
