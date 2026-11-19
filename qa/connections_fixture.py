"""Local browser QA with a real app and a fake upstream, never real credentials.

Run: python qa/connections_fixture.py /absolute/path/openrouter-public-models.json
The supplied JSON is a GET /models snapshot. Every upstream API call is mocked.
This file is never imported by the production server.
"""
from pathlib import Path
import json
import os
import sys
import tempfile

if __name__ != "__main__":
    raise RuntimeError("Execute apenas como ambiente local de QA")
if os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("AUTOCLIPS_PUBLIC_URL"):
    raise RuntimeError("Não execute esta fixture no ambiente de produção")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AUTOCLIPS_DATA_ROOT"] = tempfile.mkdtemp(prefix="autoclips-connections-qa-")
snapshot = json.loads(Path(sys.argv[1]).read_text())

import httpx
import uvicorn
from fastapi.responses import HTMLResponse
import integrations
import server


def upstream(request):
    key = request.headers.get("authorization", "")
    if request.url.path.endswith("/key"):
        if key != "Bearer qa-key-no-real-account":
            return httpx.Response(401, json={"error": {"message": "fixture invalid key"}})
        return httpx.Response(200, json={"data": {"free_model_daily_requests": {"used": 0, "limit": 50, "remaining": 50}}})
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json=snapshot)
    if request.url.path.endswith("/chat/completions"):
        body = json.loads(request.content)
        assert body["model"] == "openrouter/free"
        assert body["provider"]["max_price"]["prompt"] == 0
        assert body["provider"]["max_price"]["completion"] == 0
        return httpx.Response(200, json={"model": "qa/resposta-simulada", "choices": [{"message": {"content": "OK"}}]})
    return httpx.Response(404)


real_client = httpx.Client


def mock_client(*args, **kwargs):
    return real_client(*args, **kwargs, transport=httpx.MockTransport(upstream))


httpx.Client = mock_client

badge = '<div style="padding:10px 18px;background:#24334b;color:#e2ecff;font:13px system-ui;text-align:center">QA local · catálogo público real · chave fictícia · teste de IA simulado</div>'
html = (ROOT / "static/index.html").read_text().replace('<body>', '<body>' + badge)
for route in list(server.app.router.routes):
    if getattr(route, "path", None) == "/":
        server.app.router.routes.remove(route)


@server.app.get("/", response_class=HTMLResponse)
def qa_home():
    return html


@server.app.get("/qa/mobile", response_class=HTMLResponse)
def qa_mobile():
    return '''<!doctype html><html lang="pt-BR"><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>QA AutoClips · celular 390 px</title></head>
<body style="margin:0;background:#191c28;display:grid;place-items:center;min-height:100vh"><main><p style="color:#d7dcef;font:14px system-ui">Validação responsiva · viewport 390 × 844 px</p><iframe title="AutoClips mobile" src="/#connections" style="width:390px;height:844px;border:1px solid #50566b;border-radius:12px"></iframe></main></body></html>'''


uvicorn.run(server.app, host="127.0.0.1", port=8765, access_log=False)
