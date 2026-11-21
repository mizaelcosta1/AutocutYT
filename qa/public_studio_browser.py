"""Local end-to-end QA: TLS SMTP, sign-up, login, real media and responsive templates."""
from contextlib import ExitStack
from email import message_from_bytes
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.request import urlopen

from aiosmtpd.controller import Controller
from playwright.sync_api import sync_playwright, expect

REPO = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Inbox:
    code = None
    async def handle_DATA(self, server, session, envelope):
        assert session.ssl and envelope.rcpt_tos == ['editor@example.org']
        msg = message_from_bytes(envelope.content)
        body = msg.get_payload(decode=True).decode(msg.get_content_charset())
        self.code = re.search(r'\b[0-9]{6}\b', body).group()
        return '250 accepted'


def main():
    with ExitStack() as stack:
        tmp = Path(stack.enter_context(TemporaryDirectory()))
        cert, key = tmp / 'cert.pem', tmp / 'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',
            '-keyout',str(key),'-out',str(cert)],check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        inbox = Inbox()
        smtp = Controller(inbox, hostname='127.0.0.1', port=port(), tls_context=tls, require_starttls=True)
        smtp.start()
        stack.callback(smtp.stop)
        web_port = port()
        url = f'https://127.0.0.1:{web_port}'
        root = tmp / 'studio'
        volume = root / 'expanded'
        volume.mkdir(parents=True)
        (volume / '.autoclips-volume').touch()
        env = {**os.environ, 'SSL_CERT_FILE':str(cert), 'AUTOCLIPS_PUBLIC_SIGNUP':'1',
               'AUTOCLIPS_DATA_ROOT':str(root), 'AUTOCLIPS_EXPANDED_VOLUME':str(volume),
               'AUTOCLIPS_PUBLIC_URL':url, 'AUTOCLIPS_SESSION_SECRET':'local-qa-only-' * 4,
               'AUTOCLIPS_SMTP_HOST':'127.0.0.1','AUTOCLIPS_SMTP_PORT':str(smtp.port),
               'AUTOCLIPS_SMTP_FROM':'studio@example.org'}
        proc = subprocess.Popen([sys.executable,'-m','uvicorn','server:app','--port',str(web_port),
            '--ssl-keyfile',str(key),'--ssl-certfile',str(cert)],cwd=REPO,env=env,stdout=subprocess.DEVNULL)
        def shutdown():
            proc.terminate()
            proc.wait(timeout=10)
        stack.callback(shutdown)
        for _ in range(100):
            try:
                with urlopen(url+'/health', context=ssl.create_default_context(cafile=str(cert)),timeout=1):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError('Studio failed to start')
                time.sleep(.1)
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch(executable_path=os.getenv('AUTOCLIPS_TEST_CHROMIUM') or None)
        stack.callback(browser.close)
        context = browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':940})
        failed_responses = []
        def watch(page):
            page.on('response', lambda response: failed_responses.append(response.url)
                    if response.status >= 500 else None)

        page = context.new_page()
        watch(page)
        page.goto(url)
        page.get_by_role('button',name='Criar uma conta').click()
        page.get_by_label('E-mail',exact=True).fill('editor@example.org')
        page.get_by_label('Senha',exact=True).fill('senha de teste local')
        page.get_by_role('button',name='Enviar código').click()
        expect(page.get_by_label('Código de confirmação')).to_be_visible()
        page.get_by_label('Código de confirmação').fill(inbox.code)
        page.get_by_role('button',name='Confirmar e entrar').click()
        expect(page.get_by_role('heading',name='Novo projeto')).to_be_visible()
        page.locator('.sidebar-foot a').click()
        page.get_by_label('E-mail',exact=True).fill('editor@example.org')
        page.get_by_label('Senha',exact=True).fill('senha de teste local')
        page.get_by_role('button',name='Entrar',exact=True).click()
        expect(page.get_by_role('heading',name='Novo projeto')).to_be_visible()
        fixture = tmp / 'qa.mp4'
        subprocess.run(['ffmpeg','-loglevel','error','-f','lavfi','-i','color=c=blue:s=160x90:d=3',
                        '-c:v','mpeg4',str(fixture)],check=True)
        page.locator('#video-file').set_input_files(fixture)
        page.locator('#upload-authorized').check()
        page.locator('#btn-upload').click()
        for _ in range(100):
            videos = page.request.get(url+'/api/videos').json()['videos']
            if videos:
                break
            time.sleep(.1)
        assert len(videos) == 1 and videos[0]['name'] == 'qa.mp4'
        assert list(volume.glob('studios/*/videos/*qa.mp4'))
        saved = context.storage_state()
        for device, options in [
            ('phone', pw.devices['Pixel 5']), ('tablet', pw.devices['iPad (gen 7)']),
            ('desktop', {'viewport':{'width':1440,'height':900}})]:
            with browser.new_context(**options,ignore_https_errors=True,storage_state=saved) as ctx:
                p = ctx.new_page()
                watch(p)
                p.goto(url)
                expect(p.locator('html')).to_have_attribute('data-device',device)
                assert p.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if device == 'phone':
                    p.get_by_role('button',name='Link',exact=True).click()
                    expect(p.locator('#vod-url')).to_be_visible()
                    expect(p.locator('#video-file')).to_be_hidden()
                nav = '.mobile-tabs' if device == 'phone' else '.sidebar'
                p.locator(f'{nav} [data-tab="library"]').click()
                expect(p.get_by_role('heading',name='Biblioteca',exact=True)).to_be_visible()
                p.goto(url + '/studio/' + videos[0]['id'])
                expect(p.locator('#source-player')).to_be_visible()
                expect(p.locator('html')).to_have_attribute('data-device',device)
                assert p.evaluate('document.documentElement.scrollWidth <= innerWidth')
                print(device, 'OK')
        assert not failed_responses, failed_responses
        print('Verified STARTTLS email, confirmation, logout, password login, upload and editor.')


if __name__ == '__main__':
    main()
