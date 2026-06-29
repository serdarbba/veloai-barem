/**
 * VeloAI — Gemini proxy (Cloudflare Worker, ÜCRETSİZ).
 * Key tarayıcıya İNMEZ: yalnızca Worker secret'ında (GEMINI_API_KEY).
 * GitHub Pages frontend (demo.html) buraya POST atar; biz Gemini'yi çağırırız.
 *
 * Deploy:
 *   wrangler login
 *   wrangler secret put GEMINI_API_KEY   # AIzaSy... key'i yapıştır
 *   wrangler deploy
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",            // istersen sadece kendi domainine kısıtla
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (request.method !== "POST") return json({ error: "Method Not Allowed" }, 405);

    const key = (env.GEMINI_API_KEY || "").trim();
    if (!key) return json({ error: "Gemini key yapılandırılmamış (wrangler secret put GEMINI_API_KEY)" }, 500);

    let req;
    try { req = await request.json(); }
    catch (e) { return json({ error: "Geçersiz JSON" }, 400); }

    const body = { contents: req.contents || [] };
    if (req.systemInstruction) body.systemInstruction = req.systemInstruction;
    if (req.tools)             body.tools = req.tools;
    const gen = Object.assign({}, req.generationConfig || {});
    if (gen.thinkingConfig === undefined) gen.thinkingConfig = { thinkingBudget: 0 };
    body.generationConfig = gen;

    const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${key}`;
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const text = await r.text();   // Gemini yanıtını aynen geçir
      return new Response(text, {
        status: r.status,
        headers: { ...CORS, "Content-Type": "application/json" },
      });
    } catch (e) {
      return json({ error: "Gemini'ye ulaşılamadı: " + e.message }, 502);
    }
  },
};
