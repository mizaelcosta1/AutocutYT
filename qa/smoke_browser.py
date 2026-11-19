"""Offline Chromium use-case smoke test with real FastAPI/FFmpeg backend.

This test uses Playwright browser controls, but intercepts Fetch calls into
FastAPI TestClient: sandbox policy blocks even loopback Chromium navigation.
No external accounts, social posts, or paid AI jobs are used.
Run: python qa/smoke_browser.py
"""
from __future__ import annotations
import base64
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Async network binding is called from Chromium to local TestClient.
FETCH_BRIDGE = r"""
window.fetch = async function(url, options = {}) {
  const opts = options || {};
  let bodyBytes = null;
  if (typeof opts.body === 'string') bodyBytes = Array.from(new TextEncoder().encode(opts.body));
  else if (opts.body != null && typeof opts.body.arrayBuffer === 'function') bodyBytes = Array.from(new Uint8Array(await opts.body.arrayBuffer()));
  const result = await window.qaFetch({url:String(url), method:opts.method || 'GET', headers:opts.headers || {}, bytes:bodyBytes});
  return new Response(result.body, {status:result.status, headers:result.headers});
};
"""


def main():
    with tempfile.TemporaryDirectory(prefix='autoclips-browser-') as d:
        data=Path(d)
        os.environ['AUTOCLIPS_DATA_ROOT']=str(data/'data')
        for key in ('AUTOCLIPS_GOOGLE_CLIENT_ID','AUTOCLIPS_GOOGLE_CLIENT_SECRET',
                    'AUTOCLIPS_SESSION_SECRET','AUTOCLIPS_ALLOWED_EMAILS','AUTOCLIPS_PUBLIC_URL'):
            os.environ.pop(key,None)
        os.environ['AUTOCLIPS_COMFY_WORKFLOWS_JSON']=json.dumps([{
            'title':'Frame original 9:16', 'workflow_id':'fe5b7c97-11fa-448b-bb4e-12edd5bb72e0'
        }])
        for key in ('server','studio_auth'):
            sys.modules.pop(key,None)
        server=importlib.import_module('server')
        client=TestClient(server.app)
        synthetic = data/'smoke.mp4'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
          '-f','lavfi','-i','testsrc2=size=320x180:rate=15',
          '-f','lavfi','-i','sine=frequency=440:sample_rate=16000',
          '-t','3','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(synthetic)],check=True)
        routes=[]
        def bridge(_source, req):
            url=req['url']
            routes.append((req['method'],url))
            headers=dict(req.get('headers') or {})
            body=bytes(req['bytes']) if req.get('bytes') is not None else None
            response=client.request(req['method'], url, headers=headers, content=body)
            content_type=response.headers.get('content-type','')
            # Browser app scripts ask for JSON; binary media src requests don't
            # go through fetch, so never return binary bytes here.
            if content_type.startswith('application/json') or 'text/' in content_type:
                body=response.text
            else:
                body=''
            return {'status':response.status_code, 'headers':{'content-type':content_type}, 'body':body}
        html=(ROOT/'static/index.html').read_text(encoding='utf-8')
        html=html.replace('<link rel="stylesheet" href="/static/app.css">','')
        html=html.replace('<script src="/static/app.js"></script>','')
        html=html.replace('<script src="/static/connections.js"></script>','')
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,device_scale_factor=1)
            errors=[]
            page.on('pageerror',lambda err:errors.append(str(err)))
            page.expose_binding('qaFetch',bridge)
            html = html.replace('</head>', '<style>' + (ROOT/'static/app.css').read_text(encoding='utf-8') + '</style></head>')
            html = html.replace('</body>', '<script>' + FETCH_BRIDGE + '</script><script>' + (ROOT/'static/app.js').read_text(encoding='utf-8') + '</script><script>' + (ROOT/'static/connections.js').read_text(encoding='utf-8') + '</script></body>')
            page.set_content(html)
            assert 'AutoClips AI Studio v0.7.1' in page.title()
            page.locator('.mobile-tabs [data-tab="connections"]').click()
            link=page.locator('#comfy-workflows .comfy-launch'); link.wait_for(timeout=10000)
            assert link.get_attribute('target')=='_blank'
            assert link.get_attribute('href')=='https://cloud.comfy.org/#fe5b7c97-11fa-448b-bb4e-12edd5bb72e0'
            page.locator('.mobile-tabs [data-tab="sources"]').click()
            page.set_input_files('#video-file',str(synthetic))
            assert page.locator('#btn-upload').is_disabled()
            page.locator('#upload-authorized').check()
            assert page.locator('#btn-upload').is_enabled()
            page.locator('#btn-upload').click()
            page.locator('#editor-body').wait_for(state='visible',timeout=40000)
            page.locator('#manual-title').fill('Clipe criado no navegador')
            page.locator('#clip-end').fill('2.0')
            page.locator('#btn-manual').click()
            page.locator('.clip-card').wait_for(timeout=10000)
            assert 'Clipe criado no navegador' in page.locator('.clip-card .c-title').input_value()
            page.locator('.clip-card .c-gen-meta').wait_for(timeout=10000)
            assert 'Sugerir título (sem IA)' in page.locator('.clip-card .c-gen-meta').inner_text()
            page.locator('.clip-card .c-gen-meta').click()
            page.wait_for_timeout(300)
            assert page.locator('.clip-card .c-title').input_value()
            # An invalid unsaved interval must never trigger an export job.
            page.locator('.clip-card .c-start').fill('9999')
            routes_before=len(routes)
            page.locator('.clip-card button').filter(has_text='Gerar Previa').first.click()
            page.wait_for_timeout(800)
            after=routes[routes_before:]
            assert not any('/render/preview' in path and method=='POST' for method,path in after), after
            assert 'fora dos limites' in page.locator('#notice').inner_text()
            # Correct the interval, then check a real clip preview render.
            page.locator('.clip-card .c-start').fill('0')
            page.locator('.clip-card button').filter(has_text='Gerar Previa').first.click()
            page.locator('.clip-card video').first.wait_for(timeout=45000)
            assert page.locator('.clip-card .c-yt-approve').count()==0
            final=page.locator('.clip-card .c-render-final')
            final.wait_for(timeout=15000)
            assert final.is_disabled(), 'Final must demand explicit preview approval'
            page.locator('.clip-card .c-preview-approve').check()
            assert final.is_enabled()
            final.click()
            page.locator('.clip-card a[download$="_final_1080p.mp4"]').wait_for(timeout=65000)
            assert page.locator('.clip-card a[download$="_final_1080p.mp4"]').get_attribute('href').endswith('/final')
            assert any('/render/final' in path and method == 'POST' for method,path in routes)
            assert not errors, errors
            screenshot_path = Path(os.getenv('AUTOCLIPS_QA_SCREENSHOT', str(data/'smoke_mobile.png')))
            screenshot_path.write_bytes(page.screenshot(full_page=True))
            print('BROWSER_PASS mobile tabs, Comfy real link, permission gate, upload FFmpeg, manual clip, heuristic title, failed save stops render, preview, explicit final consent and real 1080p render')
            print('browser_api_requests=',len(routes))
            browser.close()

if __name__=='__main__': main()
