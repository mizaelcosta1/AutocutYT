"""Google OpenID Connect login for the AutoClips web studio.

Normal sign-in requests only openid/email/profile. A separate, explicit Drive
connection uses the same registered callback, with a session-bound purpose.
OAuth application credentials stay in server variables and the encrypted vault.
"""
from __future__ import annotations

import base64
import hashlib
from html import escape
import os
import re
import secrets
from pathlib import Path
import public_accounts
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
DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
DRIVE_CONNECT_HANDLER = None
router = APIRouter()


def settings():
    return {
        'client_id': os.getenv('AUTOCLIPS_GOOGLE_CLIENT_ID', '').strip(),
        'client_secret': os.getenv('AUTOCLIPS_GOOGLE_CLIENT_SECRET', '').strip(),
        'secret': os.getenv('AUTOCLIPS_SESSION_SECRET', ''),
        'public_url': os.getenv('AUTOCLIPS_PUBLIC_URL', '').strip().rstrip('/'),
        'allowed': {e.strip().casefold() for e in os.getenv('AUTOCLIPS_ALLOWED_EMAILS', '').split(',') if e.strip()},
    }


def session_configured():
    cfg = settings()
    parsed = urlsplit(cfg['public_url'])
    return bool(len(cfg['secret']) >= 32 and parsed.scheme == 'https' and parsed.netloc
                and not parsed.username and not parsed.password and parsed.path in {'', '/'}
                and not parsed.query and not parsed.fragment)


def google_configured():
    cfg = settings()
    return bool(session_configured() and cfg['client_id'] and cfg['client_secret']
                and (cfg['allowed'] or public_accounts.enabled()))


def configured():
    return google_configured() or public_accounts.public_ready()


def open_session(response, email):
    response.set_cookie(SESSION_COOKIE, serializer('studio-session').dumps({'email': email}),
                        max_age=COOKIE_MAX_AGE, httponly=True, secure=True, samesite='lax', path='/')
    return _auth_headers(response)


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
    if email and (public_accounts.public_ready() or email in settings()['allowed']):
        return {'email': email}
    return None


def storage_owner(email):
    """Stable private namespace; never use email addresses in file names."""
    return hashlib.sha256(str(email).strip().casefold().encode()).hexdigest()


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


def safe_next(value):
    """Only return to known studio pages, never an external or action URL."""
    if not isinstance(value, str) or len(value) > 512 or any(ord(ch) < 32 for ch in value):
        return '/'
    try:
        target = urlsplit(value)
    except ValueError:
        return '/'
    if (target.scheme or target.netloc or target.query or '\\' in value or '%' in value
            or not (target.path == '/' or re.fullmatch(r'/studio/[A-Za-z0-9_-]{1,100}', target.path))
            or target.fragment not in {'', 'connections', 'library', 'editor'}):
        return '/'
    return target.path + ('#' + target.fragment if target.fragment else '')


def login_url(next_path='/'):
    target = safe_next(next_path)
    return '/login' + ('?' + urlencode({'next': target}) if target != '/' else '')


def _auth_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def login_html(configured_ok, next_path='/', error_message='', connection_error=False):
    target = safe_next(next_path)
    google_url = '/login/google' + ('?' + urlencode({'next': target}) if target != '/' else '')
    content = '''<div class="brand-mark" aria-hidden="true">A<span>✂</span></div><div class="eyebrow">SEU ESTÚDIO DE CORTES · v0.8.2</div><h1>AutoClips AI Studio</h1><p class="intro">Da live ao clipe: combine facecam e gameplay, personalize as legendas e revise antes de exportar.</p>'''
    if error_message:
        content += '<div class="auth-message" role="alert"><h2>Não foi possível concluir</h2><p>' + escape(error_message) + '</p></div>'
    if configured_ok:
        if connection_error:
            content += '<a class="button return-button" href="/#connections">Voltar às conexões</a>'
        else:
            content += '<p class="login-note">Use sua conta Google autorizada para acessar seus projetos.</p>'
            # Local SVG keeps the official multicolour mark visible without a
            # third-party script or an image request before authentication.
            content += '<a class="button google-button" href="' + escape(google_url, quote=True) + '''"><svg width="20" height="20" viewBox="0 0 48 48" aria-hidden="true" focusable="false"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6C44.4 38.02 46.98 31.87 46.98 24.55z"/><path fill="#FBBC05" d="M10.53 28.59A14.41 14.41 0 0 1 9.75 24c0-1.59.27-3.13.76-4.59l-7.98-6.19A23.87 23.87 0 0 0 0 24c0 3.87.93 7.53 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.91-5.8l-7.73-6C30.03 37.65 27.26 38.5 24 38.5c-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg><span>Entrar com Google</span></a>'''
            content += '<p class="privacy-note">A autenticação acontece na página oficial do Google.</p>'
            content += '<div class="connection-note"><b>Quer enviar cortes ao YouTube?</b><p>Depois de entrar, abra Conexões e autorize o canal. Você escolhe quando enviar cada vídeo.</p></div>'
    else:
        content += ('<h2>Acesso em configuração</h2><p>O acesso com Google está sendo preparado. '
                    'Tente novamente mais tarde ou avise o responsável pelo estúdio.</p>')
    return '''<!doctype html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Entrar | AutoClips AI Studio</title><style>
*{box-sizing:border-box}body{background:#08121d;color:#eff7fc;font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:100svh;margin:0;padding:24px;background-image:radial-gradient(ellipse at 15% 5%,#42d5e714,transparent 50%),radial-gradient(ellipse at 90% 90%,#fb62720b,transparent 50%)}
main{width:min(480px,100%);padding:clamp(24px,5vw,40px);border:1px solid #2b4255;border-radius:22px;background:#102030;box-shadow:0 24px 80px #0003}.brand-mark{font-size:38px;color:#42d5e7;font-weight:850;letter-spacing:-3px;margin-bottom:24px}.brand-mark span{font-size:32px;color:#fb6272;margin-left:4px}.eyebrow{font-size:11px;letter-spacing:.14em;color:#a9bbca;font-weight:700}h1{font-size:clamp(25px,5vw,30px);letter-spacing:-.035em;line-height:1.2;margin:12px 0}h2{font-size:18px}p{color:#a9bbca;line-height:1.65}.intro{margin-bottom:24px}.login-note{font-size:14px}.button{display:flex;align-items:center;justify-content:center;gap:12px;border:1px solid #747775;border-radius:12px;padding:12px;text-align:center;text-decoration:none;font-weight:500;margin-top:16px;min-height:48px}.google-button{background:#fff;color:#1f1f1f;font-family:Arial,sans-serif;font-size:14px}.google-button svg{flex:0 0 20px}.google-button:hover{background:#f2f2f2}.return-button{background:#42d5e7;color:#06222c;border-color:#42d5e7}.button:focus-visible{outline:3px solid #fb6272;outline-offset:4px}.privacy-note{font-size:12px;text-align:center}.connection-note{border-top:1px solid #2b4255;margin-top:24px;padding-top:20px;font-size:13px}.connection-note b{color:#dceef5}.connection-note p{margin-bottom:0}.auth-message{border:1px solid #fb627288;border-radius:12px;padding:16px;background:#fb62720b;margin:20px 0}.auth-message h2{color:#ff9ba6;margin:0 0 8px}.auth-message p{color:#e3c8ce;margin:0;font-size:14px}
</style></head><body><main>''' + content + '</main></body></html>'


@router.get('/login')
def login(request: Request):
    next_path = safe_next(request.query_params.get('next', '/'))
    if read_session(request):
        return _auth_headers(RedirectResponse(next_path, status_code=303))
    ready = configured()
    if public_accounts.public_ready():
        page = (Path(__file__).parent / 'static' / 'access.html').read_text()
        page = page.replace('{{google_url}}', escape('/login/google?' + urlencode({'next': next_path}), quote=True))
        page = page.replace('{{google}}', '' if google_configured() else 'hidden').replace('{{next}}', escape(next_path, quote=True))
        return _auth_headers(HTMLResponse(page))
    return _auth_headers(HTMLResponse(login_html(ready, next_path), status_code=200 if ready else 503))


@router.get('/login/google')
def login_google(request: Request):
    next_path = safe_next(request.query_params.get('next', '/'))
    if not google_configured():
        return _auth_headers(RedirectResponse(login_url(next_path), status_code=303))
    return _start_google(next_path=next_path)


@router.get('/auth/storage/drive/start')
def connect_google_drive(request: Request):
    session = read_session(request)
    if not google_configured() or not session or not DRIVE_CONNECT_HANDLER:
        raise HTTPException(401, 'Entre com Google antes de conectar o Drive.')
    return _start_google(purpose='drive', email=session['email'])


def _start_google(*, purpose='login', email=None, next_path='/'):
    cfg = settings()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    pending = {'state': state, 'nonce': nonce, 'verifier': verifier, 'purpose': purpose}
    if purpose == 'drive':
        pending['email'] = email
    else:
        pending['next'] = safe_next(next_path)
    payload = serializer('google-oauth').dumps(pending)
    parameters = {
        'client_id': cfg['client_id'], 'redirect_uri': cfg['public_url'] + '/login/google/callback',
        'response_type': 'code', 'scope': 'openid email profile', 'state': state,
        'nonce': nonce, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'access_type': 'online', 'prompt': 'select_account',
    }
    if purpose == 'drive':
        parameters.update(scope='openid email profile ' + DRIVE_SCOPE,
                          access_type='offline', prompt='consent', login_hint=email)
    query = urlencode(parameters)
    response = RedirectResponse(GOOGLE_AUTH + '?' + query, status_code=302)
    response.set_cookie(OAUTH_COOKIE, payload, max_age=OAUTH_MAX_AGE, httponly=True, secure=True, samesite='lax', path='/login/google')
    return _auth_headers(response)


def _login_error(request, status, detail, pending=None):
    """Give people a retry screen while preserving API errors and HTTP status."""
    if 'text/html' in request.headers.get('accept', ''):
        friendly = {
            'Estado OAuth inválido ou expirado': 'Esta tentativa de entrada expirou. Clique em Entrar com Google para tentar novamente.',
            'A autorização Google foi cancelada': 'A entrada foi cancelada. Quando quiser, clique em Entrar com Google para tentar novamente.',
            'Falha na autenticação Google': 'Não conseguimos confirmar a entrada com o Google. Tente novamente em alguns instantes.',
            'Nonce OAuth inválido': 'Não foi possível validar esta tentativa de entrada. Clique em Entrar com Google para começar novamente.',
            'O Google não confirmou o e-mail': 'Use uma conta com o endereço de e-mail confirmado no Google.',
            'Esta conta Google não tem acesso ao AutoClips': 'Esta conta ainda não tem acesso ao estúdio. Escolha sua conta autorizada ou fale com o responsável pelo AutoClips.',
        }.get(detail, detail)
        response = HTMLResponse(login_html(configured(), (pending or {}).get('next', '/'), friendly,
                                           connection_error=(pending or {}).get('purpose') == 'drive'), status_code=status)
    else:
        response = JSONResponse({'detail': detail}, status_code=status)
    response.delete_cookie(OAUTH_COOKIE, path='/login/google', secure=True, httponly=True, samesite='lax')
    return _auth_headers(response)


@router.get('/login/google/callback')
def login_google_callback(request: Request, code: str = '', state: str = '', error: str = ''):
    pending = read_oauth_cookie(request.cookies.get(OAUTH_COOKIE))
    if (not pending or not state or len(state) > 256 or not state.isascii()
            or not secrets.compare_digest(pending.get('state', ''), state)):
        return _login_error(request, 400, 'Estado OAuth inválido ou expirado', pending)
    if error:
        if pending.get('purpose') == 'drive':
            return _storage_redirect(False)
        return _login_error(request, 400, 'A autorização Google foi cancelada', pending)
    if not code:
        return _login_error(request, 400, 'Estado OAuth inválido ou expirado', pending)
    try:
        token = exchange_code(code, pending['verifier'])
        identity = verify_identity(token['id_token'], settings()['client_id'])
    except (httpx.HTTPError, KeyError, jwt.PyJWTError, ValueError):
        return _login_error(request, 502, 'Falha na autenticação Google', pending)
    nonce = str(identity.get('nonce', ''))
    if not nonce.isascii() or not secrets.compare_digest(nonce, pending['nonce']):
        return _login_error(request, 403, 'Nonce OAuth inválido', pending)
    if identity.get('email_verified') not in {True, 'true'}:
        return _login_error(request, 403, 'O Google não confirmou o e-mail', pending)
    email = str(identity.get('email', '')).strip().casefold()
    if not email or (not public_accounts.public_ready() and email not in settings()['allowed']):
        return _login_error(request, 403, 'Esta conta Google não tem acesso ao AutoClips', pending)
    if pending.get('purpose') == 'drive':
        session = read_session(request)
        if (not session or session['email'] != pending.get('email') or email != pending.get('email')):
            return _login_error(request, 403, 'Conecte o Drive da mesma conta usada para entrar no estúdio.', pending)
        if DRIVE_SCOPE not in str(token.get('scope', '')).split() or not DRIVE_CONNECT_HANDLER:
            return _storage_redirect(False)
        try:
            DRIVE_CONNECT_HANDLER(storage_owner(email), token, identity)
        except (ValueError, httpx.HTTPError):
            # Provider messages can contain credentials. The connection panel
            # explains setup/reconnect without reflecting tokens into a URL.
            return _storage_redirect(False)
        return _storage_redirect(True)
    response = RedirectResponse(safe_next(pending.get('next', '/')), status_code=303)
    response.set_cookie(SESSION_COOKIE, serializer('studio-session').dumps({'email': email}), max_age=COOKIE_MAX_AGE,
                        httponly=True, secure=True, samesite='lax', path='/')
    response.delete_cookie(OAUTH_COOKIE, path='/login/google', secure=True, httponly=True, samesite='lax')
    return _auth_headers(response)


def _storage_redirect(connected):
    response = RedirectResponse('/?storage=' + ('connected' if connected else 'error') + '#connections', status_code=303)
    response.delete_cookie(OAUTH_COOKIE, path='/login/google', secure=True, httponly=True, samesite='lax')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
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
