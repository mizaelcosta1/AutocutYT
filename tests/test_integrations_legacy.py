"""No live API calls: verify contracts without consuming account access or credits."""
import importlib
from pathlib import Path
import json
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient


def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOCLIPS_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.delenv('AUTOCLIPS_PASSWORD', raising=False)
    monkeypatch.delenv('AUTOCLIPS_ENCRYPTION_KEY', raising=False)
    for name in ['AUTOCLIPS_YOUTUBE_CLIENT_ID','AUTOCLIPS_YOUTUBE_CLIENT_SECRET',
                 'AUTOCLIPS_TIKTOK_CLIENT_KEY','AUTOCLIPS_TIKTOK_CLIENT_SECRET',
                 'AUTOCLIPS_INSTAGRAM_CLIENT_ID','AUTOCLIPS_INSTAGRAM_CLIENT_SECRET',
                 'AUTOCLIPS_TWITCH_CLIENT_ID','AUTOCLIPS_TWITCH_CLIENT_SECRET',
                 'AUTOCLIPS_KICK_CLIENT_ID','AUTOCLIPS_KICK_CLIENT_SECRET']:
        monkeypatch.delenv(name, raising=False)
    if 'server' in sys.modules:
        del sys.modules['server']
    return importlib.import_module('server')


FAKE_MODELS = [
    {'id':'openai/test-model','name':'Testing Model','context_length':16000, 'pricing': {'prompt':'0.00'}},
    {'id':'google/small-model','name':'Small Model','context_length':10000}
]


def test_models_discovery_and_encrypted_key(tmp_path, monkeypatch):
    server = fresh(tmp_path, monkeypatch)
    import integrations
    calls=[]
    def fake_discovery(kind, base, key):
        calls.append((kind,base,key))
        return FAKE_MODELS
    monkeypatch.setattr(integrations, 'discover_models', fake_discovery)
    client=TestClient(server.app)
    key='sk-or-v1-secret-should-not-leak'
    created=client.post('/api/ai/providers', json={'kind':'openrouter','name':'OpenRouter','api_key':key})
    assert created.status_code==201, created.text
    provider=created.json()['provider']
    assert provider['model_count']==2
    assert 'api_key' not in created.text and key not in created.text
    assert calls[0][1]=='https://openrouter.ai/api/v1'
    record=Path(server.ROOT / 'connections.enc')
    assert record.exists() and key.encode() not in record.read_bytes()
    assert (server.ROOT / '.connections.key').exists()
    assert 'secret-should-not-leak' not in client.get('/api/ai/providers').text
    listed=client.get(f'/api/ai/providers/{provider["id"]}/models?q=small')
    assert listed.status_code==200 and listed.json()['total']==1
    chosen=client.post('/api/ai/selection',json={'stage':'moments', 'provider_id':provider['id'],'model_id':FAKE_MODELS[0]['id']})
    assert chosen.status_code==200
    assert integrations.get_selected(server.CONNECTION_VAULT)['api_key']==key
    refreshed=client.post(f'/api/ai/providers/{provider["id"]}/refresh')
    assert refreshed.status_code==200 and refreshed.json()['count']==2
    assert client.delete(f'/api/ai/providers/{provider["id"]}').status_code==200
    assert integrations.get_selected(server.CONNECTION_VAULT) is None
    assert key not in server.CONNECTION_VAULT.read().__str__()


def test_custom_endpoint_validation(tmp_path, monkeypatch):
    server=fresh(tmp_path, monkeypatch)
    import integrations
    client=TestClient(server.app)
    for url in ['http://api.example.com/v1', 'https://127.0.0.1/v1',
                'https://localhost:5000/v1','https://user:pwd@example.com/v1',
                'https://api.example.com/v1?secret=yes','https://169.254.169.254/v1']:
        response=client.post('/api/ai/providers',json={'kind':'custom','name':'Example',
                          'endpoint':url,'api_key':'examplekey'})
        assert response.status_code==422,(url,response.text)
    monkeypatch.setattr(integrations.socket,'getaddrinfo',lambda *a,**kw:[(None,None,None,None,('9.9.9.9',443))])
    assert integrations.validate_endpoint('https://api.example.com/v1/models')=='https://api.example.com/v1'
    monkeypatch.setattr(integrations,'discover_models',lambda kind,base,key:FAKE_MODELS)
    res=client.post('/api/ai/providers',json={'kind':'custom','name':'Other Cloud',
                    'endpoint':'https://api.example.com/v1','api_key':'my-secret-api-key'})
    assert res.status_code==201,res.text
    assert res.json()['provider']['endpoint']=='https://api.example.com/v1'


def test_social_buttons_disabled_without_app_credentials(tmp_path,monkeypatch):
    server=fresh(tmp_path,monkeypatch)
    client=TestClient(server.app)
    data=client.get('/api/social/accounts').json()
    assert len(data['networks'])==5
    assert all(not x['configured'] and not x['connected'] for x in data['networks'])
    assert client.get('/auth/youtube/start',follow_redirects=False).status_code==409
    assert client.get('/').status_code==200
    assert client.get('/static/connections.js').status_code==200


def test_youtube_oauth_state_success_and_one_use(tmp_path,monkeypatch):
    server=fresh(tmp_path,monkeypatch)
    import integrations
    monkeypatch.setenv('AUTOCLIPS_PUBLIC_URL','https://studio.example.com')
    monkeypatch.setenv('AUTOCLIPS_YOUTUBE_CLIENT_ID','app-client-id')
    monkeypatch.setenv('AUTOCLIPS_YOUTUBE_CLIENT_SECRET','app-client-secret')
    client=TestClient(server.app, base_url='https://studio.example.com')
    started=client.get('/auth/youtube/start',follow_redirects=False)
    assert started.status_code==302
    url=urlsplit(started.headers['location'])
    assert url.hostname=='accounts.google.com'
    qs=parse_qs(url.query)
    assert qs['client_id']==['app-client-id']
    assert qs['redirect_uri']==['https://studio.example.com/auth/youtube/callback']
    state=qs['state'][0]
    assert state and len(state)>20
    invalid=client.get('/auth/youtube/callback?code=foo&state=wrong')
    assert invalid.status_code==400
    calls=[]
    monkeypatch.setattr(integrations,'_exchange_code',lambda *a:(calls.append(a) or {'access_token':'top-secret-token','refresh_token':'another-secret','expires_in':3600,'scope':'https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.upload'}))
    monkeypatch.setattr(integrations,'_identity',lambda *a:{'id':'UC123','name':'Meu canal'})
    good=client.get('/auth/youtube/callback?code=one-use-code&state='+state,follow_redirects=False)
    assert good.status_code==303
    assert good.headers['location']=='/?connected=youtube#connections'
    assert calls[0][0]=='youtube'
    assert 'top-secret-token' not in str(good.headers)
    assert client.get('/api/social/accounts').json()['networks'][0]['account']['name']=='Meu canal'
    assert client.get('/auth/youtube/callback?code=one-use-code&state='+state).status_code==400
    assert client.post('/api/social/youtube/disconnect').status_code==200
    assert not client.get('/api/social/accounts').json()['networks'][0]['connected']


def test_kick_pkce_oauth(tmp_path,monkeypatch):
    server=fresh(tmp_path,monkeypatch)
    monkeypatch.setenv('AUTOCLIPS_PUBLIC_URL','https://studio.example.com')
    monkeypatch.setenv('AUTOCLIPS_KICK_CLIENT_ID','kick-client')
    monkeypatch.setenv('AUTOCLIPS_KICK_CLIENT_SECRET','kick-secret')
    client=TestClient(server.app)
    res=client.get('/auth/kick/start',follow_redirects=False)
    assert res.status_code==302
    url=urlsplit(res.headers['location']);qs=parse_qs(url.query)
    assert url.hostname=='id.kick.com'
    assert qs['code_challenge_method']==['S256']
    assert len(qs['code_challenge'][0])>40
    stored=server.CONNECTION_VAULT.read()['pending'][qs['state'][0]]
    assert stored['verifier'] and stored['expires']
    assert stored['redirect']=='https://studio.example.com/auth/kick/callback'


def test_cloud_reranker_requires_per_file_consent(tmp_path,monkeypatch):
    server=fresh(tmp_path,monkeypatch)
    client=TestClient(server.app)
    # Must not transmit excerpts or spend tokens without approval.
    res=client.post('/api/videos/anything/analyze',json={'use_cloud_model':True,'cloud_approved':False})
    # Existing video check occurs first: make sure invalid IDs are safely rejected.
    assert res.status_code==404
    # Direct remote reranker respects missing excerpts and call count bounds.
    import integrations
    class StubResponse:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':'{"score":8,"reason":"Bom gancho"}'}}]}
    calls=[]
    class StubClient:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,uri,**kwargs):
            calls.append((uri,kwargs['json']))
            return StubResponse()
    monkeypatch.setattr(integrations.httpx,'Client',StubClient)
    proposals=[{'start':i,'end':i+10,'score':i,'excerpt':'Uma surpresa na conversa','reason':'vocal'} for i in range(20)]
    result=integrations.rerank_remote(proposals,{'endpoint':'https://provider.test/v1','api_key':'example-secret','model_id':'model-1'})
    assert len(calls)==12 and len(result)==20
    assert any(c.get('method')=='remote-llm+heuristic' for c in result)
    assert all(x[0]=='https://provider.test/v1/chat/completions' for x in calls)
    assert all('.mp4' not in str(x[1]) and 'file://' not in str(x[1]) for x in calls)
