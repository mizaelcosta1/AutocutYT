"""Private data roots and a shared delivery worker for public studios."""
from collections import OrderedDict
from contextvars import ContextVar
import os
from pathlib import Path
import re
import threading
from weakref import WeakValueDictionary

from integrations import Vault
from storage_delivery import StorageDelivery
from trend_suggestions import SuggestionsService

active = ContextVar('account_workspace', default=None)


class Workspace:
    def __init__(self, root, load_tasks):
        self.ROOT = root
        self.UPLOADS = root / 'upload_sessions'
        self.MEDIA = root / 'videos'
        self.RECORDINGS = root / 'recordings'
        self.DOWNLOADS = root / 'downloads'
        for folder in (self.UPLOADS, self.MEDIA, self.RECORDINGS, self.DOWNLOADS):
            folder.mkdir(parents=True, exist_ok=True)
        self.INDEX = root / 'catalog.json'
        self.TASKS_FILE = root / 'tasks.json'
        self.TASKS = load_tasks(self.TASKS_FILE)
        self.LIVE = {}
        self.PENDING_PUBLISH = {}
        self.PROCESSING_VIDEOS = {}
        self.RENDERING = set()
        self.CONNECTION_VAULT = Vault(root)
        self.STORAGE_DELIVERY = StorageDelivery(root, self.CONNECTION_VAULT)
        self.STORAGE_DELIVERY.archiver.cleanup_interrupted_restores()
        self.suggestions = SuggestionsService(root, self.CONNECTION_VAULT)


class Accounts:
    def __init__(self, root, load_tasks):
        self.root = root
        self.load_tasks = load_tasks
        self.lock = threading.Lock()
        self.cache = WeakValueDictionary()
        self.recent = OrderedDict()
        self.pending = set()
        self.stop_event = threading.Event()
        self.worker = None

    def volume(self):
        setting = os.getenv('AUTOCLIPS_EXPANDED_VOLUME')
        if not setting:
            return None
        volume = Path(setting).resolve()
        if volume == self.root or not volume.is_dir() or not (volume / '.autoclips-volume').is_file():
            raise OSError('Volume de edição indisponível.')
        return volume

    def locate(self, owner):
        # Keep the location of existing studios when enabling another volume.
        entry = self.root / 'account-spaces' / owner
        entry.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = entry / 'location'
        if marker.exists():
            root = Path(marker.read_text())
            if not root.is_dir() or (root != entry / 'studio' and not (root.parent.parent / '.autoclips-volume').is_file()):
                raise OSError('Volume de edição indisponível.')
            return root
        volume = self.volume()
        root = volume / 'studios' / owner if volume else entry / 'studio'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = entry / 'location.tmp'
        tmp.write_text(str(root))
        tmp.replace(marker)
        return root

    def get(self, owner):
        if not re.fullmatch('[0-9a-f]{64}', owner):
            raise ValueError('Conta inválida.')
        with self.lock:
            root = self.locate(owner)
            ws = self.cache.get(owner)
            if ws is None:
                ws = Workspace(root, self.load_tasks)
                self.cache[owner] = ws
            self.recent[owner] = ws
            self.recent.move_to_end(owner)
            if len(self.recent) > 32:
                self.recent.popitem(last=False)
            return ws

    def mark_pending(self, owner):
        with self.lock:
            self.pending.add(owner)

    def start(self, storage_service):
        registry = self.root / 'account-spaces'
        if registry.is_dir():
            for entry in registry.iterdir():
                if (entry / 'location').is_file():
                    self.mark_pending(entry.name)
        self.stop_event.clear()
        def run():
            while not self.stop_event.is_set():
                with self.lock:
                    owners = self.pending.copy()
                for owner in owners:
                    if self.stop_event.is_set():
                        break
                    try:
                        ws = self.get(owner)
                        token = active.set(ws)
                        try:
                            service = storage_service()
                            with self.lock, service.lock:
                                outstanding = any(j['status'] in {'running', 'queued', 'retry_wait'} or
                                    (j['status'] == 'succeeded' and j.get('archive_requested') and not j.get('archived') and not j.get('archive_error'))
                                    for j in service.jobs.values())
                                if not outstanding:
                                    self.pending.discard(owner)
                            if outstanding:
                                service.run_once()
                        finally:
                            active.reset(token)
                    except (OSError, RuntimeError, ValueError):
                        continue
                self.stop_event.wait(2)
        self.worker = threading.Thread(target=run, name='account-deliveries', daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=3)
