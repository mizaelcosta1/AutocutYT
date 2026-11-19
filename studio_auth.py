"""Google OpenID Connect login for the AutoClips web studio.

This is not YouTube upload authorization: its requested scopes are only
openid/email/profile. OAuth application credentials live in Railway Variables,
never in browser forms or committed configuration files.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = 'ac_studio_session'
OAUTH_COOKIE = 'ac_google_oauth'
COOKIE_MAX_AGE = 12 * 60 * 60
OAUTH_MAX_AGE = 10 * 60
GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'
GOOGLE_JWKS = 'https://www.googleapis.com/oauth2/v3/certs'
router = APIRouter()


def settings():
    return {
        'client_id': os.getenv('AUTOCLIPS_GOOGLE_CLIENT_ID', '').strip(),
        'client_secret': os.getenv('AUTOCLIPS_GOOGLE_CLIENT_SECRET', '').strip(),
        'secret': os.getenv('AUTOCLIPS_SESSION_SECRET', ''),
        'public_url': os.getenv('AUTOCLIPS_PUBLIC_URL', '').strip().rstrip('/'),
        'allowed': {e.strip().casefold() for e in os.getenv('AUTOCLIPS_ALLOWED_EMAILS', '').split(',') if e.strip()},
    }


def configured():
    cfg = settings()
    parsed = urlsplit(cfg['public_url'])
    return bool(cfg['client_id'] and cfg['client_secret'] and len(cfg['secret']) >= 32
                and cfg['allowed'] and parsed.scheme == 'https' and parsed.netloc
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)


def serializer(salt):
    return URLSafeTimedSerializer(settings()['secret'], salt='autoclips-' + salt)


def read_oauth_cookie(raw):
    try:
        return serializer('google-oauth').loads(raw, max_age=OAUTH_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError):
        return None


def read_session(request):
    if not configured():
        return None
    try:
        session = serializer('studio-session').loads(request.cookies.get(SESSION_COOKIE), max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError):
        return None
    email = str(session.get('email', '')).strip().casefold()
    if email in settings()['allowed']:
        return {'email': email}
    return None


def exchange_code(code, verifier):
    cfg = settings()
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        response = client.post(GOOGLE_TOKEN, data={
            'grant_type': 'authorization_code', 'code': code,
            'client_id': cfg['client_id'], 'client_secret': cfg['client_secret'],
            'redirect_uri': cfg['public_url'] + '/login/google/callback',
            'code_verifier': verifier,
        })
        response.raise_for_status()
        return response.json()


def verify_identity(raw, client_id):
    signing_key = jwt.PyJWKClient(GOOGLE_JWKS).get_signing_key_from_jwt(raw)
    return jwt.decode(raw, signing_key.key, algorithms=['RS256'], audience=client_id,
                      issuer=['accounts.google.com', 'https://accounts.google.com'], options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})


def login_html(configured_ok):
    content = '''<h1>AutoClips AI Studio</h1><p>Edite vídeos e conecte seu canal com segurança.</p>'''
    if configured_ok:
        content += '<a class="button" href="/login/google">Entrar com Google</a>'
    else:
        content += ('<h2>Configuração necessária</h2><p>O administrador precisa configurar no Railway '
                    '<code>AUTOCLIPS_GOOGLE_CLIENT_ID</code>, <code>AUTOCLIPS_GOOGLE_CLIENT_SECRET</code>, '
                    '<code>AUTOCLIPS_SESSION_SECRET</code>, <code>AUTOCLIPS_ALLOWED_EMAILS</code> e '
                    '<code>AUTOCLIPS_PUBLIC_URL</code>. Depois, cadastre o URI de retorno no Google Cloud.</p>')
    return '''<!doctype html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Entrar | AutoClips AI Studio</title><style>body{background:#0d1020;color:#f7f7ff;font:16px system-ui;display:grid;place-items:center;min-height:100vh;margin:0}main{width:min(460px,calc(100vw - 52px));padding:32px;border:1px solid #3a3f57;border-radius:20px;background:#171b2d}h1{font-size:28px}p{color:#b9c0d4;line-height:1.5}.button{display:block;background:#fafafa;border-radius:12px;padding:16px;text-align:center;color:#181818;text-decoration:none;font-weight:700;margin-top:26px}code{overflow-wrap:anywhere;color:#8fcaff}</style></head><body><main>''' + content + '</main></body></html>'


@router.get('/login')
def login(request: Request):
    if read_session(request):
        return RedirectResponse('/', status_code=303)
    return HTMLResponse(login_html(configured()), status_code=200 if configured() else 503)


@router.get('/login/google')
def login_google(request: Request):
    if not configured():
        return RedirectResponse('/login', status_code=303)
    cfg = settings()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    payload = serializer('google-oauth').dumps({'state': state, 'nonce': nonce, 'verifier': verifier})
    query = urlencode({
        'client_id': cfg['client_id'], 'redirect_uri': cfg['public_url'] + '/login/google/callback',
        'response_type': 'code', 'scope': 'openid email profile', 'state': state,
        'nonce': nonce, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'access_type': 'online', 'prompt': 'select_account',
    })
    response = RedirectResponse(GOOGLE_AUTH + '?' + query, status_code=302)
    response.set_cookie(OAUTH_COOKIE, payload, max_age=OAUTH_MAX_AGE, httponly=True, secure=True, samesite='lax', path='/login/google')
    return response


@router.get('/login/google/callback')
def login_google_callback(request: Request, code: str = '', state: str = '', error: str = ''):
    pending = read_oauth_cookie(request.cookies.get(OAUTH_COOKIE))
    if error:
        raise HTTPException(400, 'A autorização Google foi cancelada')
    if not pending or not code or not state or not secrets.compare_digest(pending['state'], state):
        raise HTTPException(400, 'Estado OAuth inválido ou expirado')
    try:
        token = exchange_code(code, pending['verifier'])
        identity = verify_identity(token['id_token'], settings()['client_id'])
    except (httpx.HTTPError, KeyError, jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(502, 'Falha na autenticação Google') from exc
    if not secrets.compare_digest(str(identity.get('nonce', '')), pending['nonce']):
        raise HTTPException(403, 'Nonce OAuth inválido')
    if identity.get('email_verified') not in {True, 'true'}:
        raise HTTPException(403, 'O Google não confirmou o e-mail')
    email = str(identity.get('email', '')).strip().casefold()
    if email not in settings()['allowed']:
        raise HTTPException(403, 'Esta conta Google não tem acesso ao AutoClips')
    response = RedirectResponse('/', status_code=303)
    response.set_cookie(SESSION_COOKIE, serializer('studio-session').dumps({'email': email}), max_age=COOKIE_MAX_AGE,
                        httponly=True, secure=True, samesite='lax', path='/')
    response.delete_cookie(OAUTH_COOKIE, path='/login/google', secure=True, httponly=True, samesite='lax')
    return response


@router.get('/logout')
def logout():
    response = RedirectResponse('/login', status_code=303)
    response.delete_cookie(SESSION_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
    return response


@router.get('/api/auth/session')
def current_session(request: Request):
    active = read_session(request)
    return {'authenticated': bool(active), 'email': active['email'] if active else None}
