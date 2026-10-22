"""Isolated HTTP tests for remote boundary and collection orchestration.

The fixture marks loopback test connections as a remote client. No LAN listener
or browser is started. Collection results are fixtures, not a fresh site crawl.
"""
import importlib
import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

WEB = Path(__file__).resolve().parents[1] / '前端'
sys.path.insert(0, str(WEB))
w = importlib.import_module('workbench')
ext = importlib.import_module('external_api')
checks = []


def check(label, value):
    assert value, label
    checks.append(label)
    print('PASS ' + label)


with tempfile.TemporaryDirectory(prefix='xhs-api-test-') as temporary:
    target = Path(temporary)
    shutil.copy2(WEB / 'api-docs.html', target / 'api-docs.html')
    w.WEB, w.ROOT = target, target
    w.api_json = lambda *args, **kwargs: {'info': {'title': 'XHS-Downloader'}}
    w.runtime_paths = lambda: ('fixture-node', 'fixture-playwright')
    w.persist('settings', {'id': 'destination', 'folder': str(target / 'downloads')})
    def fake_collect(c):
        try:
            notes = [{'id': 'a'*24, 'url': 'https://www.xiaohongshu.com/explore/' + 'a'*24, 'title': 'fixture', 'author': 'fixture', 'kind': '图文'}]
            w.update(c, 'collection', notes=notes, author='fixture', status='complete', complete=True, message='fixture completed')
        finally:
            w.ACTIVE_SCAN.release()
    w.collect = fake_collect
    class RemoteHandler(w.Handler):
        def setup(self):
            super().setup()
            self.client_address = ('192.0.2.10', self.client_address[1])
    server = ThreadingHTTPServer(('127.0.0.1', 0), RemoteHandler)
    w.PORT = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{w.PORT}'
    key = ext.key(w)
    def call(path, body=None, auth=True, extra=None):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + key
        headers.update(extra or {})
        payload = None if body is None else json.dumps(body).encode()
        try:
            with urlopen(Request(base + path, data=payload, headers=headers), timeout=5) as result:
                content = result.read()
                return result.status, json.loads(content) if 'json' in result.headers.get('Content-Type', '') else content
        except HTTPError as error:
            return error.code, json.loads(error.read())
    try:
        check('remote_workbench_denied', call('/')[0] == 403)
        code, html = call('/api-docs', auth=False)
        check('remote_docs_no_admin_token_or_api_key', code == 200 and w.TOKEN.encode() not in html and key.encode() not in html and b"const csrf='';" in html)
        check('remote_key_read_denied_even_with_admin_token', call('/api/admin/api-key', {}, extra={'X-XHS-Token': w.TOKEN})[0] == 403)
        check('remote_global_folder_change_denied', call('/api/folder', {'folder': str(target)}, extra={'X-XHS-Token': w.TOKEN})[0] == 403)
        check('remote_legacy_files_denied', call('/files/' + 'a'*32 + '/0')[0] == 403)
        check('remote_v1_requires_key', call('/api/v1/tasks', auth=False)[0] == 401)
        check('remote_v1_accepts_bearer', call('/api/v1/tasks')[0] == 200)
        code, result = call('/api/v1/collections', {'url': 'https://www.xiaohongshu.com/user/profile/' + 'b'*24})
        check('collection_creation_contract_202', code == 202 and result['status_url'].endswith(result['id']))
        identity = result['id']
        for _ in range(50):
            _, result = call('/api/v1/collections/' + identity)
            if result['status'] == 'complete':
                break
            time.sleep(.02)
        check('collection_fixture_completion_visible', result['status'] == 'complete' and result['note_count'] == 1)
        code, downloads = call(f'/api/v1/collections/{identity}/download', {})
        check('collection_download_all_creates_queued_tasks', code == 202 and downloads['count'] == 1 and w.JOBS[downloads['ids'][0]]['status'] == 'queued')
        check('collection_active_download_conflict', call(f'/api/v1/collections/{identity}/download', {})[0] == 409)
        task_id = downloads['ids'][0]
        code, result = call(f'/api/v1/tasks/{task_id}/cancel', {})
        check('queued_cancellation', code == 200 and result['status'] == 'cancelled')
        code, retried = call(f'/api/v1/tasks/{task_id}/retry', {})
        check('retry_creates_new_identity', code == 202 and retried['id'] != task_id)
        check('retry_active_task_conflict', call(f'/api/v1/tasks/{retried["id"]}/retry', {})[0] == 409)
        call(f'/api/v1/tasks/{retried["id"]}/cancel', {})
        c = w.COLLECTIONS[identity]
        c['notes'] = [{'id': str(i), 'url': 'https://www.xiaohongshu.com/explore/' + 'a'*24, 'title': 'fixture'} for i in range(1001)]
        count = len(w.JOBS)
        check('oversized_collection_rejected_without_partial_enqueue', call(f'/api/v1/collections/{identity}/download', {})[0] == 429 and len(w.JOBS) == count)
        c['status'] = 'scanning'
        code, result = call(f'/api/v1/collections/{identity}/cancel', {})
        check('collection_cancellation_preserves_notes', code == 200 and result['status'] == 'cancelled' and result['note_count'] == 1001)
        w.ACTIVE_SCAN.acquire()
        check('concurrent_collection_conflict', call('/api/v1/collections', {'url': 'https://www.xiaohongshu.com/user/profile/' + 'c'*24})[0] == 409)
        w.ACTIVE_SCAN.release()
        check('stored_key_survives_reload', key == ext.key(w))
    finally:
        server.shutdown()
        server.server_close()

print(f'{len(checks)} isolated HTTP checks passed.')
