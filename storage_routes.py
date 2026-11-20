"""Authenticated API boundaries for private export delivery."""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool

import studio_auth
from storage_delivery import DeliveryError


class DeliverySettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    auto_download: StrictBool | None = None
    auto_upload: StrictBool | None = None
    provider: Literal['drive', 'github'] | None = None
    qualities: list[Literal['preview', 'final']] | None = Field(default=None, min_length=1, max_length=2)


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['clip', 'timeline']
    clip_id: str | None = Field(default=None, pattern=r'^clip_[0-9a-z_]+$', max_length=100)
    quality: Literal['preview', 'final']
    render_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    provider: Literal['drive', 'github']


def _owner(request: Request):
    session = studio_auth.read_session(request)
    if session:
        return studio_auth.storage_owner(session['email'])
    if not studio_auth.configured() and request.client and request.client.host in {'127.0.0.1', '::1', 'testclient', 'localhost'}:
        return 'local'
    raise HTTPException(401, 'Faça login com Google.')


def _same_origin(request: Request, response: Response):
    response.headers['Cache-Control'] = 'private, no-store'
    if request.method not in {'POST', 'PATCH', 'DELETE', 'PUT'}:
        return
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Abra este recurso pelo próprio estúdio.')
    origin = request.headers.get('origin')
    if origin:
        expected = studio_auth.settings()['public_url'] if studio_auth.configured() else str(request.base_url)
        expected = urlsplit(expected)
        if origin.rstrip('/') != f'{expected.scheme}://{expected.netloc}':
            raise HTTPException(403, 'Origem da solicitação inválida.')


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DeliveryError as exc:
        raise HTTPException(422, str(exc)) from None


def build_storage_router(service, enqueue, validate_video):
    """Dependencies are callables so tests and app reloads share no stale vault."""
    router = APIRouter(dependencies=[Depends(_same_origin)])

    @router.get('/api/storage/settings')
    def settings(owner=Depends(_owner)):
        return _call(service().public_settings, owner)

    @router.patch('/api/storage/settings')
    def update_settings(patch: DeliverySettings, owner=Depends(_owner)):
        values = patch.model_dump(exclude_unset=True)
        if any(value is None for value in values.values()):
            raise HTTPException(422, 'Escolha valores válidos para a automação.')
        return _call(service().configure, owner, values)

    @router.post('/api/storage/github')
    async def connect_github(request: Request, owner=Depends(_owner)):
        # Do not let a validation error echo an API token back to the browser.
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(422, 'Dados da conexão inválidos.')
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(422, 'Dados da conexão inválidos.') from None
        if (not isinstance(payload, dict) or set(payload) != {'token', 'repository'}
                or not isinstance(payload.get('token'), str) or not 1 <= len(payload['token'].strip()) <= 4096
                or not isinstance(payload.get('repository'), str)
                or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', payload['repository'].strip())):
            raise HTTPException(422, 'Informe o token e um repositório privado no formato proprietário/repositório.')
        return await run_in_threadpool(_call, service().connect_github, owner,
                                       payload['token'].strip(), payload['repository'].strip())

    @router.post('/api/storage/{provider}/disconnect')
    def disconnect(provider: Literal['drive', 'github'], owner=Depends(_owner)):
        return _call(service().disconnect, owner, provider)

    @router.get('/api/storage/jobs')
    def all_jobs(owner=Depends(_owner)):
        return {'jobs': _call(service().list_jobs, owner)}

    @router.get('/api/videos/{video_id}/deliveries')
    def video_jobs(video_id: str, owner=Depends(_owner)):
        validate_video(video_id)
        return {'jobs': _call(service().list_jobs, owner, video_id=video_id)}

    @router.post('/api/videos/{video_id}/deliveries')
    def send(video_id: str, req: DeliveryRequest, owner=Depends(_owner)):
        if (req.kind == 'clip') != bool(req.clip_id):
            raise HTTPException(422, 'Selecione o corte ou a montagem a enviar.')
        return {'job': _call(enqueue, owner, video_id, req)}

    @router.post('/api/storage/jobs/{job_id}/retry')
    def retry(job_id: str, owner=Depends(_owner)):
        return {'job': _call(service().retry, owner, job_id)}

    @router.post('/api/storage/jobs/{job_id}/cancel')
    def cancel(job_id: str, owner=Depends(_owner)):
        return {'job': _call(service().cancel, owner, job_id)}

    return router
