"""UI smoke for one-paste auto VOD pipeline (offline fixture for real public URL).

This is NOT proof the linked YouTube video itself was downloaded.
"""
from __future__ import annotations
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
URL='https://www.youtube.com/live/hOOiQagTyTA?is=-WdBBCQjg7sX0w54'
FETCH_BRIDGE = r"""
window.fetch = async function(url, options = {}) {
 const o=options||{};const body=typeof o.body==='string'?Array.from(new TextEncoder().encode(o.body)):null;
 const r=await window.qaFetch({url:String(url),method:o.method||'GET',headers:o.headers||{},bytes:body});
 return new Response(r.body,{status:r.status,headers:r.headers});
};
"""

def main():
 with tempfile.TemporaryDirectory(prefix='autoclips-auto-') as d:
  folder=Path(d);os.environ['AUTOCLIPS_DATA_ROOT']=str(folder/'data')
  for key in ('AUTOCLIPS_GOOGLE_CLIENT_ID','AUTOCLIPS_GOOGLE_CLIENT_SECRET','AUTOCLIPS_SESSION_SECRET','AUTOCLIPS_ALLOWED_EMAILS','AUTOCLIPS_PUBLIC_URL'):
   os.environ.pop(key,None)
  for mod in ('server','studio_auth'):sys.modules.pop(mod,None)
  server=importlib.import_module('server')
  source=folder/'fixture.mp4'
  subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=size=160x90:rate=12','-f','lavfi','-i','sine=frequency=600:sample_rate=16000','-t','10','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],check=True)
  server.detect_source_kind=lambda _: 'vod'
  server.download_vod=lambda _url,_folder:source
  real_which=server.shutil.which
  server.shutil.which=lambda name:'/usr/bin/'+name if name=='yt-dlp' else real_which(name)
  client=TestClient(server.app)
  routes=[]
  def bridge(_source, request):
   routes.append((request['method'],request['url']))
   response=client.request(request['method'],request['url'],headers=dict(request.get('headers') or {}),content=bytes(request['bytes']) if request.get('bytes') is not None else None)
   ct=response.headers.get('content-type','')
   body=response.text if ct.startswith('application/json') or ct.startswith('text/') else ''
   return {'status':response.status_code,'headers':{'content-type':ct},'body':body}
  html=(ROOT/'static/index.html').read_text()
  html=html.replace('<link rel="stylesheet" href="/static/app.css">','').replace('<script src="/static/app.js"></script>','').replace('<script src="/static/connections.js"></script>','')
  html=html.replace('</head>','<style>'+(ROOT/'static/app.css').read_text()+'</style></head>')
  html=html.replace('</body>','<script>'+FETCH_BRIDGE+'</script><script>'+(ROOT/'static/app.js').read_text()+'</script><script>'+(ROOT/'static/connections.js').read_text()+'</script></body>')
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
   page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,device_scale_factor=1)
   errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
   page.expose_binding('qaFetch',bridge)
   page.set_content(html)
   page.locator('#vod-url').fill(URL)
   page.locator('#vod-authorized').check()
   page.locator('#btn-vod').click()
   page.locator('.clip-card').first.wait_for(timeout=40000)
   assert any(x==('POST','/api/source/auto') for x in routes),routes
   assert '0.7.2' in page.title(),page.title()
   assert page.locator('.clip-card video').count()>0,'Preview video tag was not added'
   assert not errors,errors
   result=client.get('/api/videos').json();assert len(result['videos'])==1,result
   video_id=result['videos'][0]['id']
   manifest=client.get('/api/videos/'+video_id+'/manifest').json()
   assert manifest['clips'][0]['preview_url'],manifest
   response=client.get(manifest['clips'][0]['preview_url']);assert response.status_code==200
   assert len(response.content)>20000
   dest=Path('/mnt/data/autoclips-youtube-case/browser-mobile-url.png')
   dest.write_bytes(page.screenshot(full_page=True))
   print('PASS: User URL pasted => route /api/source/auto => VOD => automated preview MP4 => editor visible on mobile')
   print('Synthetic fixture only; NOT downloaded from Youtube. bytes=',len(response.content),'requests=',len(routes))
   browser.close()

if __name__=='__main__':main()
