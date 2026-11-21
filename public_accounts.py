"""Email verification and password authentication for public studios."""
from contextlib import closing, contextmanager
from email.message import EmailMessage
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from threading import BoundedSemaphore

HASH_SLOTS = BoundedSemaphore(2)

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import studio_auth

router = APIRouter(prefix='/login/email')
PENDING = 'ac_signup'
GENERIC = 'Se o endereço puder receber um novo código, ele chegará em alguns minutos.'


def enabled():
    return os.getenv('AUTOCLIPS_PUBLIC_SIGNUP') == '1'


def public_ready():
    return enabled() and studio_auth.session_configured()


def data_root():
    return Path(os.environ.get('AUTOCLIPS_DATA_ROOT', Path.home() / 'AutoClipsStudioData')).expanduser().resolve()


@contextmanager
def transaction():
    with closing(sqlite3.connect(data_root() / 'accounts.sqlite3', timeout=10, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS accounts(email TEXT PRIMARY KEY, password TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS pending(email TEXT PRIMARY KEY, attempt TEXT, password TEXT, digest TEXT, expires INTEGER, tries INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS limits(bucket TEXT PRIMARY KEY, starts INTEGER, count INTEGER)')
        yield db
        db.commit()


def digest(value):
    return hmac.new(studio_auth.settings()['secret'].encode(), value.encode(), hashlib.sha256).hexdigest()


def limited(db, bucket, maximum, window, now):
    db.execute('DELETE FROM limits WHERE starts + 3600 <= ?', (now,))
    row = db.execute('SELECT * FROM limits WHERE bucket=?', (bucket,)).fetchone()
    if row and now - row['starts'] < window:
        if row['count'] >= maximum:
            return True
        db.execute('UPDATE limits SET count=count+1 WHERE bucket=?', (bucket,))
    else:
        db.execute('INSERT OR REPLACE INTO limits VALUES(?,?,1)', (bucket, now))
    return False


def encode_password(password, salt):
    if not HASH_SLOTS.acquire(timeout=2):
        raise HTTPException(429, "Muitas tentativas. Aguarde um instante.")
    try:
        value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**15,
                           r=8, p=1, maxmem=64*1024*1024).hex()
    finally:
        HASH_SLOTS.release()
    return salt + '$' + value


def send_code(email, code):
    msg = EmailMessage()
    msg['From'] = os.environ['AUTOCLIPS_SMTP_FROM']
    msg['To'] = email
    msg['Subject'] = 'Confirme seu e-mail no AutoClips'
    msg.set_content(f'Seu código é {code}. Ele vale por 10 minutos. Se você não iniciou o cadastro, ignore esta mensagem.')
    host = os.environ['AUTOCLIPS_SMTP_HOST']
    implicit = os.getenv('AUTOCLIPS_SMTP_TLS', 'starttls') == 'implicit'
    port = int(os.getenv('AUTOCLIPS_SMTP_PORT', '465' if implicit else '587'))
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL(host, port, timeout=10, context=context) if implicit else smtplib.SMTP(host, port, timeout=10)
    with client:
        if not implicit:
            client.starttls(context=context)
        if os.getenv('AUTOCLIPS_SMTP_USER'):
            client.login(os.environ['AUTOCLIPS_SMTP_USER'], os.environ['AUTOCLIPS_SMTP_PASSWORD'])
        client.send_message(msg)


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


class Confirmation(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(pattern=r'^[0-9]{6}$')


def gate(request, email, action):
    if not public_ready():
        raise HTTPException(503, 'O acesso público ainda está em configuração.')
    if request.headers.get('origin', studio_auth.settings()['public_url']) != studio_auth.settings()['public_url'] or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Abra esta página pelo próprio estúdio.')
    email = email.strip().casefold()
    if not re.fullmatch(r'[^\s@<>(),;:"\\]+@[^\s@<>(),;:"\\]+\.[^\s@<>(),;:"\\]+', email) or not email.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in email):
        raise HTTPException(400, 'Informe um endereço de e-mail válido.')
    now = int(time.time())
    with transaction() as db:
        ip = request.client.host if request.client else ''
        blocked = limited(db, digest(action + ':ip:' + ip), 40, 600, now)
        if not blocked:
            blocked = limited(db, digest(action + ':email:' + email), 8, 600, now)
    if blocked:
        raise HTTPException(429, 'Aguarde alguns minutos antes de tentar novamente.')
    return email, now


@router.post('/register')
def register(form: Credentials, request: Request):
    email, now = gate(request, form.email, 'register')
    if len(form.password) < 12:
        raise HTTPException(400, 'Use uma senha com pelo menos 12 caracteres.')
    if not os.getenv('AUTOCLIPS_SMTP_HOST') or not os.getenv('AUTOCLIPS_SMTP_FROM'):
        raise HTTPException(503, 'O envio de códigos ainda está em configuração.')
    attempt = secrets.token_urlsafe(24)
    code = f'{secrets.randbelow(1000000):06d}'
    password = encode_password(form.password, secrets.token_hex(16))
    with transaction() as db:
        db.execute('DELETE FROM pending WHERE expires <= ?', (now,))
        previous = db.execute('SELECT expires FROM pending WHERE email=?', (email,)).fetchone()
        if previous and previous['expires'] > now + 540:
            raise HTTPException(429, 'Aguarde um minuto para pedir outro código.')
        exists = db.execute('SELECT 1 FROM accounts WHERE email=?', (email,)).fetchone()
        if not exists:
            db.execute('INSERT OR REPLACE INTO pending VALUES(?,?,?,?,?,0)',
                       (email, digest(attempt), password, digest(attempt + code), now+600))
    if not exists:
        try:
            send_code(email, code)
        except (OSError, smtplib.SMTPException, KeyError, ValueError):
            with transaction() as db:
                db.execute('DELETE FROM pending WHERE email=? AND attempt=?', (email, digest(attempt)))
            raise HTTPException(503, 'O serviço de e-mail está indisponível. Tente novamente.') from None
    response = JSONResponse({'message': GENERIC}, status_code=202)
    response.set_cookie(PENDING, attempt, max_age=600, secure=True, httponly=True, samesite='strict', path='/login/email')
    return studio_auth._auth_headers(response)


@router.post('/confirm')
def confirm(form: Confirmation, request: Request):
    email, now = gate(request, form.email, 'confirm')
    attempt = request.cookies.get(PENDING, '')[:128]
    accepted = False
    with transaction() as db:
        row = db.execute('SELECT * FROM pending WHERE email=?', (email,)).fetchone()
        if row and row['expires'] > now and row['tries'] < 5 and hmac.compare_digest(row['attempt'], digest(attempt)):
            if hmac.compare_digest(row['digest'], digest(attempt + form.code)):
                db.execute('INSERT INTO accounts VALUES(?,?)', (email, row['password']))
                db.execute('DELETE FROM pending WHERE email=?', (email,))
                accepted = True
            else:
                db.execute('UPDATE pending SET tries=tries+1 WHERE email=?', (email,))
    if not accepted:
        raise HTTPException(400, 'Código inválido ou expirado. Use o navegador onde iniciou o cadastro.')
    response = studio_auth.open_session(JSONResponse({'authenticated': True}), email)
    response.delete_cookie(PENDING, path='/login/email', secure=True, httponly=True, samesite='strict')
    return response


@router.post('/signin')
def signin(form: Credentials, request: Request):
    email, _ = gate(request, form.email, 'signin')
    with transaction() as db:
        row = db.execute('SELECT password FROM accounts WHERE email=?', (email,)).fetchone()
    saved = row['password'] if row else '00'*16 + '$' + '00'*64
    candidate = encode_password(form.password, saved.split('$')[0])
    if not hmac.compare_digest(candidate, saved):
        raise HTTPException(401, 'E-mail ou senha incorretos.')
    return studio_auth.open_session(JSONResponse({'authenticated': True}), email)
