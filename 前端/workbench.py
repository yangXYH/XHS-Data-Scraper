"""Local download workbench: persisted jobs, selected destination and author collections."""
import json
import argparse
import ipaddress
import shutil
import sys
import external_api
import mimetypes
import os
import queue
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request, urlopen
from archive import Cancelled, archive_folder, download_note, inside, migrate_archive, read_json, safe_name

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
PORT = 5557
API = 'http://127.0.0.1:5556'
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
PENDING = queue.Queue()
JOBS, COLLECTIONS, PROCESSES = {}, {}, {}
ACTIVE_SCAN = threading.Lock()
PICKER = threading.Lock()
ACTIVE = {'queued', 'running', 'cancelling'}
SCANNING = {'starting', 'scanning', 'login', 'attention'}
LINKS = re.compile(r'https?://(?:www\.)?(?:(?:xiaohongshu|rednote)\.com|xhslink\.com)/[^\s<>"\u201c\u201d\u3000]+', re.I)
MEDIA = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.mp4', '.mov', '.gif', '.avif', '.txt', '.json'}


@contextmanager
def database():
    connection = sqlite3.connect(WEB / 'workbench.sqlite3', timeout=20)
    connection.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, payload TEXT, PRIMARY KEY(kind,id))')
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def persist(kind, record):
    with database() as db:
        db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (kind, record['id'], json.dumps(record, ensure_ascii=False)))


def update(record, kind='job', **values):
    with LOCK:
        record.update(values)
        persist(kind, record)


def default_dir():
    settings = read_json(ROOT / '_internal/Volume/settings.json')
    folder = Path(settings.get('work_path') or '.')
    if not folder.is_absolute():
        folder = ROOT / folder
    return (folder / (settings.get('folder_name') or '爬取资源')).resolve()


def download_dir():
    with database() as db:
        row = db.execute('SELECT payload FROM records WHERE kind=? AND id=?', ('settings', 'destination')).fetchone()
    return Path(json.loads(row[0])['folder']) if row else default_dir()


def validate_folder(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('请选择保存文件夹，或输入完整的文件夹路径。')
    folder = Path(os.path.expandvars(raw.strip().strip('"'))).expanduser()
    if not folder.is_absolute():
        raise ValueError('请输入完整路径，例如 D:\\小红书作品。')
    folder = folder.resolve()
    if len(str(folder)) > 140:
        raise ValueError('保存路径过长，请选择层级更浅的文件夹。')
    folder.mkdir(parents=True, exist_ok=True)
    probe = folder / ('.xhs-write-' + uuid.uuid4().hex)
    try:
        with probe.open('xb') as file:
            file.write(b'ok')
    finally:
        probe.unlink(missing_ok=True)
    return folder


def parse_links(text):
    return list(dict.fromkeys(match.group().rstrip('，。；、！？!;)]}）】') for match in LINKS.finditer(text)))


def is_profile(link):
    return bool(re.fullmatch(r'/user/profile/[a-f0-9]{24}/?', urlsplit(link).path, re.I))


def api_json(path, body=None, timeout=5):
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    with urlopen(Request(API + path, data=payload, headers={'Content-Type': 'application/json; charset=utf-8'}), timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8-sig'))


def chinese_api_schema():
    return external_api.schema()


def ensure_api():
    try:
        return api_json('/openapi.json').get('info', {}).get('title') == 'XHS-Downloader'
    except (OSError, ValueError):
        pass
    logdir = WEB / 'logs'
    logdir.mkdir(exist_ok=True)
    with (logdir / 'api.log').open('ab') as log:
        subprocess.Popen([str(ROOT / 'main.exe'), 'api'], cwd=ROOT, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(20):
        time.sleep(1)
        try:
            if api_json('/openapi.json', timeout=1).get('info', {}).get('title') == 'XHS-Downloader':
                return True
        except (OSError, ValueError):
            continue
    return False


def make_job(url, mode='download', index=None, reuse=True, collection=None, folder=None, title=''):
    job = {'id': uuid.uuid4().hex, 'url': url, 'mode': mode, 'index': index or [], 'reuse': reuse,
           'root': str(folder or download_dir()), 'collection_id': collection['id'] if collection else '',
           'author_override': collection.get('author', '') if collection else '', 'title': title,
           'status': 'queued', 'message': '等待处理', 'files': [], 'addresses': [],
           'current': 0, 'total': 0, 'created': time.time()}
    with LOCK:
        JOBS[job['id']] = job
        persist('job', job)
        PENDING.put(job)
    return job


def worker():
    while True:
        job = PENDING.get()
        with LOCK:
            if job['status'] == 'cancelled':
                PENDING.task_done()
                continue
            update(job, status='running', message='正在解析作品信息…')
        try:
            data = api_json('/xhs/detail', {'url': job['url'], 'download': False, 'skip': False}, timeout=90).get('data')
            if not data:
                update(job, error_code='NOTE_UNAVAILABLE')
                raise ValueError('未取得作品数据，请确认链接完整、作品可访问后重试。')
            addresses = data.get('下载地址') or []
            if not isinstance(addresses, list):
                addresses = [addresses]
            if not addresses:
                update(job, error_code='NO_RESOURCES')
                raise ValueError('未获取到资源地址，请检查作品是否可访问。')
            if job['status'] in ('cancelled', 'cancelling'):
                raise Cancelled()
            update(job, title=data.get('作品标题') or '未命名作品', author=data.get('作者昵称') or '',
                   kind=data.get('作品类型') or '', description=data.get('作品描述') or '', addresses=addresses)
            if job['mode'] == 'parse':
                update(job, status='done', message=f'已解析 {len(addresses)} 个资源地址', finished=time.time())
                continue
            root = Path(job['root'])
            folder = archive_folder(root, data, job.get('author_override', ''))
            update(job, folder=str(folder), message='正在保存作品文件…')
            def progress(current, total, message):
                if job['status'] not in ('cancelled', 'cancelling'):
                    update(job, current=current, total=total, message=message)
            result = download_note(root, data, job['index'], job['reuse'],
                                   lambda: job['status'] in ('cancelled', 'cancelling'), progress,
                                   job.get('author_override', ''))
            if job['status'] in ('cancelled', 'cancelling'):
                raise Cancelled()
            count = len(result['files'])
            if count == result['expected']:
                message = f'已保存 {count} 个文件' if not result['reused'] else f'作品已就绪 · {count} 个文件，其中 {result["reused"]} 个已存在'
                status = 'done'
            else:
                status = 'warning' if count else 'failed'
                message = f'已保存 {count}/{result["expected"]} 个文件；' + '；'.join(result['errors'])[:220]
            update(job, status=status, message=message, files=result['files'], folder=result['folder'],
                   errors=result['errors'], error_code='DOWNLOAD_INCOMPLETE' if status != 'done' else '', finished=time.time())
        except Cancelled:
            update(job, status='cancelled', message='已停止；已保存的文件会保留，重试时自动复用。', finished=time.time())
        except HTTPError as exc:
            update(job, status='failed', message=f'下载服务返回错误（{exc.code}），请稍后重试。', error_code='UPSTREAM_ERROR', finished=time.time())
        except (URLError, TimeoutError, ConnectionError):
            update(job, status='failed', message='网络连接失败或超时，请检查服务和网络后重试。', error_code='NETWORK_ERROR', finished=time.time())
        except Exception as exc:
            update(job, status='failed', message=str(exc)[:350], finished=time.time())
        finally:
            PENDING.task_done()


def runtime_paths():
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    nodes = [os.environ.get('XHS_NODE_EXECUTABLE'), shutil.which('node'), str(bundled / 'bin/node.exe')]
    modules = [os.environ.get('XHS_PLAYWRIGHT_MODULE'), str(WEB / 'node_modules/playwright'), str(bundled / 'node_modules/playwright')]
    node = next((Path(p) for p in nodes if p and Path(p).is_file()), None)
    module = next((Path(p) for p in modules if p and (Path(p) / 'package.json').is_file()), None)
    if not node or not module:
        raise ValueError('作者采集需要 Node.js 与 Playwright；请参阅前端/使用说明.txt 安装运行环境。')
    return node, module


def collect(collection):
    process = None
    try:
        node, module = runtime_paths()
        environment = dict(os.environ, XHS_PW_MODULE=str(module))
        process = subprocess.Popen([str(node), str(WEB / 'collector.js')], cwd=WEB, env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        with LOCK:
            PROCESSES[collection['id']] = process
        process.stdin.write(json.dumps({'url': collection['url']}, ensure_ascii=False) + '\n')
        process.stdin.flush()
        notes = {note['id']: note for note in collection.get('notes', [])}
        for line in process.stdout:
            if collection['status'] == 'cancelled':
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'notes':
                for note in event.get('notes', []):
                    if not re.fullmatch(r'[a-f0-9]{24}', str(note.get('id', '')), re.I):
                        continue
                    if urlsplit(note.get('url', '')).hostname != 'www.xiaohongshu.com':
                        continue
                    notes[note['id']] = note
                update(collection, 'collection', notes=list(notes.values()), author=event.get('author') or collection.get('author', ''),
                       status='scanning', message=f'已发现 {len(notes)} 篇，正在加载更多…')
            elif event.get('type') == 'status':
                update(collection, 'collection', status=event['status'], message=event['message'], author=event.get('author') or collection.get('author', ''))
            elif event.get('type') == 'complete':
                update(collection, 'collection', status='complete' if event['complete'] else 'partial', complete=bool(event['complete']),
                       message=event['message'], author=event.get('author') or collection.get('author', ''), finished=time.time())
            elif event.get('type') == 'error':
                update(collection, 'collection', status='partial' if notes else 'failed', complete=False, message=event['message'])
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
        if collection['status'] in SCANNING:
            update(collection, 'collection', status='partial' if notes else 'failed', complete=False,
                   message='采集窗口已退出；已获取的结果已保留，可重新扫描。')
    except Exception as exc:
        update(collection, 'collection', status='failed', message=str(exc)[:350], complete=False)
    finally:
        if process and process.poll() is None:
            try:
                process.stdin.write('{"action":"cancel"}\n')
                process.stdin.flush()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
        with LOCK:
            PROCESSES.pop(collection['id'], None)
        ACTIVE_SCAN.release()


def public_collection(collection):
    data = {key: value for key, value in collection.items() if key != 'notes'}
    data['note_count'] = len(collection.get('notes', []))
    data['preview'] = collection.get('notes', [])[:12]
    jobs = [job for job in JOBS.values() if job.get('collection_id') == collection['id']]
    data['downloads'] = {'total': len(jobs), 'done': sum(job['status'] == 'done' for job in jobs),
                         'active': sum(job['status'] in ACTIVE for job in jobs),
                         'failed': sum(job['status'] in ('failed', 'warning') for job in jobs)}
    return data


def picker():
    if not PICKER.acquire(blocking=False):
        raise ValueError('文件夹选择窗口已经打开，请先完成选择。')
    try:
        script = r'''Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择小红书作品保存位置'
$dialog.SelectedPath = $env:XHS_INITIAL_FOLDER
$dialog.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
try { if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::OutputEncoding = [Text.Encoding]::UTF8; [Console]::Write($dialog.SelectedPath) } }
finally { $dialog.Dispose(); $owner.Dispose() }
'''
        result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-Command', script],
                                env=dict(os.environ, XHS_INITIAL_FOLDER=str(download_dir())),
                                capture_output=True, encoding='utf-8', timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ValueError('系统文件夹选择器无法打开，请直接粘贴路径。')
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise ValueError('选择目录超时，请重新选择或手动输入路径。')
    finally:
        PICKER.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, data, content_type='application/json; charset=utf-8'):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        elif isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        try:
            address = urlsplit('http://' + self.headers.get('Host', ''))
            if address.port != PORT or address.username or address.password:
                return False
            return address.hostname == 'localhost' or ipaddress.ip_address(address.hostname).version == 4
        except (ValueError, TypeError):
            return False

    def do_PUT(self):
        self.api_method()

    do_DELETE = do_PATCH = do_PUT

    def api_method(self):
        if not self.valid_host():
            return self.send(403, {'error': '不支持的访问地址'})
        if not external_api.handle(self, sys.modules[__name__]):
            self.send(405, {'error': '不支持的请求方法'})

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': '不支持的访问地址'})
        path = urlsplit(self.path).path
        if external_api.handle(self, sys.modules[__name__]):
            return
        if path == '/api-docs':
            page = (WEB / 'api-docs.html').read_text(encoding='utf-8')
            return self.send(200, page.replace('__CSRF_TOKEN__', TOKEN if external_api.local(self) else ''), 'text/html; charset=utf-8')
        if path in ('/vendor/swagger-ui.css', '/vendor/swagger-ui-bundle.js'):
            file = WEB / path[1:]
            return self.send(200, file.read_bytes(), 'text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8')
        if not external_api.local(self):
            return self.send(403, {'error': '工作台仅限本机操作。远程调用请使用 /api-docs 和 /api/v1。'})
        if path in ('/', '/docs'):
            return self.send(200, (WEB / 'index.html').read_text(encoding='utf-8').replace('__CSRF_TOKEN__', TOKEN), 'text/html; charset=utf-8')
        if path == '/api-docs':
            return self.send(200, (WEB / 'api-docs.html').read_text(encoding='utf-8').replace('__CSRF_TOKEN__', TOKEN), 'text/html; charset=utf-8')
        if path == '/api/openapi.json':
            try:
                return self.send(200, chinese_api_schema())
            except (OSError, ValueError):
                return self.send(503, {'error': '下载服务暂未连接，请稍后刷新。'})
        if path in ('/app.js', '/style.css', '/ui-v2.css'):
            return self.send(200, (WEB / path[1:]).read_bytes(), 'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
        if path == '/api/status':
            try:
                info = api_json('/openapi.json', timeout=2).get('info', {})
            except (OSError, ValueError):
                info = {}
            return self.send(200, {'online': info.get('title') == 'XHS-Downloader', 'version': info.get('version', ''), 'folder': str(download_dir()), 'bind_host': self.server.server_address[0], 'api_version': '1.0.0'})
        if path in ('/api/state', '/api/tasks'):
            with LOCK:
                jobs = list(JOBS.values())
                if path == '/api/tasks':
                    return self.send(200, jobs)
                counts = {'all': len(jobs), 'active': sum(job['status'] in ACTIVE for job in jobs),
                          'done': sum(job['status'] == 'done' for job in jobs),
                          'failed': sum(job['status'] in ('failed', 'warning', 'cancelled') for job in jobs)}
                collections = [public_collection(item) for item in list(COLLECTIONS.values())[-5:]]
                return self.send(200, {'jobs': jobs[-300:], 'counts': counts, 'collections': collections, 'folder': str(download_dir())})
        if path.startswith('/files/'):
            match = re.fullmatch(r'/files/([a-f0-9]{32})/(\d+)', path)
            if match:
                with LOCK:
                    job = JOBS.get(match[1])
                    files = job.get('files', []) if job else []
                index = int(match[2])
                if job and index < len(files):
                    try:
                        file = inside(job['root'], Path(job['root']) / files[index]['name'])
                        if file.suffix.lower() in MEDIA and file.is_file():
                            return self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                    except (OSError, ValueError):
                        pass
            return self.send(404, {'error': '文件不存在或已被移动'})
        return self.send(404, {'error': '页面不存在'})

    def do_POST(self):
        if not self.valid_host():
            return self.send(403, {'error': '不支持的访问地址'})
        if external_api.handle(self, sys.modules[__name__]):
            return
        if not external_api.local(self) or self.headers.get('X-XHS-Token') != TOKEN:
            return self.send(403, {'error': '页面已更新，请刷新后重试。'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 <= length <= 131072:
                return self.send(413, {'error': '提交内容过长，请分批处理。'})
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('请求内容格式不正确')
            path = urlsplit(self.path).path
            if path == '/api/xhs/detail':
                return self.send(410, {'error': {'code': 'API_REPLACED', 'message': '此旧接口已停用；请使用 /api/v1/tasks。说明见 /api-docs。'}})
            if path == '/api/folder/pick':
                return self.send(200, {'folder': picker()})
            if path == '/api/folder':
                folder = validate_folder(data.get('folder'))
                with LOCK:
                    persist('settings', {'id': 'destination', 'folder': str(folder)})
                return self.send(200, {'folder': str(folder)})
            if path == '/api/open-folder':
                folder = download_dir()
                with LOCK:
                    job = JOBS.get(data.get('job_id'))
                if job:
                    folder = inside(job['root'], Path(job.get('folder') or job['root']))
                folder.mkdir(parents=True, exist_ok=True)
                os.startfile(str(folder))
                return self.send(200, {'ok': True})
            if path == '/api/tasks/clear':
                with LOCK, database() as db:
                    for key, job in list(JOBS.items()):
                        if job['status'] not in ACTIVE:
                            del JOBS[key]
                            db.execute('DELETE FROM records WHERE kind=? AND id=?', ('job', key))
                return self.send(200, {'ok': True})
            if path == '/api/tasks/cancel':
                count = 0
                with LOCK:
                    targets = [job for job in JOBS.values() if job['id'] == data.get('id') or (data.get('collection_id') and job.get('collection_id') == data['collection_id'])]
                    for job in targets:
                        if job['status'] in ACTIVE:
                            update(job, status='cancelling' if job['status'] == 'running' else 'cancelled', message='正在停止，已保存文件会保留。')
                            count += 1
                return self.send(200, {'count': count})
            if path == '/api/tasks/retry':
                with LOCK:
                    old = JOBS.get(data.get('id'))
                    if not old or old['status'] in ACTIVE:
                        raise ValueError('任务不存在，或仍在处理中。')
                    collection = COLLECTIONS.get(old.get('collection_id'))
                    job = make_job(old['url'], old['mode'], old['index'], True, collection, Path(old['root']), old.get('title', ''))
                return self.send(202, {'ids': [job['id']]})
            if path == '/api/collections':
                links = parse_links(str(data.get('text', '')))
                if len(links) != 1 or not is_profile(links[0]):
                    raise ValueError('请输入一条完整的作者主页链接，形如 /user/profile/作者ID。')
                runtime_paths()
                if not ACTIVE_SCAN.acquire(blocking=False):
                    raise ValueError('已有作者正在采集中，请等待完成或停止当前采集。')
                collection = {'id': uuid.uuid4().hex, 'url': links[0], 'author': '', 'status': 'starting',
                              'message': '正在启动采集窗口…', 'notes': [], 'complete': False, 'created': time.time()}
                with LOCK:
                    COLLECTIONS[collection['id']] = collection
                    persist('collection', collection)
                threading.Thread(target=collect, args=(collection,), daemon=True).start()
                return self.send(202, {'id': collection['id']})
            if path == '/api/collections/cancel':
                with LOCK:
                    collection = COLLECTIONS.get(data.get('id'))
                    if not collection:
                        raise ValueError('作品集不存在')
                    process = PROCESSES.get(collection['id'])
                    update(collection, 'collection', status='cancelled', message='已停止采集，当前结果已保留。', complete=False)
                if process and process.poll() is None:
                    try:
                        process.stdin.write('{"action":"cancel"}\n')
                        process.stdin.flush()
                    except OSError:
                        pass
                return self.send(200, {'ok': True})
            if path == '/api/collections/download':
                with LOCK:
                    collection = COLLECTIONS.get(data.get('id'))
                    if not collection or not collection.get('notes'):
                        raise ValueError('还没有取得作品链接，请先获取作品集。')
                    if collection['status'] in SCANNING:
                        raise ValueError('请等待采集完成，或先停止采集再下载当前结果。')
                    if any(job['status'] in ACTIVE and job.get('collection_id') == collection['id'] for job in JOBS.values()):
                        raise ValueError('这个作品集正在下载，请在任务列表查看进度。')
                    selected = data.get('ids')
                    if selected is not None and (not isinstance(selected, list) or not all(isinstance(item, str) for item in selected)):
                        raise ValueError('作品选择格式不正确')
                    notes = [note for note in collection['notes'] if selected is None or note['id'] in selected]
                    if not notes:
                        raise ValueError('请至少选择一篇作品')
                    root = download_dir()
                    ids = [make_job(note['url'], collection=collection, folder=root, title=note['title'])['id'] for note in notes]
                return self.send(202, {'ids': ids})
            if path == '/api/tasks':
                text = data.get('text')
                if not isinstance(text, str):
                    raise ValueError('请粘贴作品链接')
                links = parse_links(text)
                if not links:
                    raise ValueError('没有识别到小红书链接，请粘贴完整分享文案。')
                if len(links) > 50:
                    raise ValueError('笔记批量输入每次最多 50 条；整位作者请使用“作者作品集”。')
                if any(is_profile(link) for link in links):
                    raise ValueError('这是作者主页，请切换到“作者作品集”获取全部作品。')
                mode = data.get('mode', 'download')
                if mode not in ('download', 'parse'):
                    raise ValueError('处理方式不正确')
                index = data.get('index') or []
                if not isinstance(index, list) or any(type(i) is not int or not 1 <= i <= 100 for i in index):
                    raise ValueError('图片序号请输入 1–100 的整数。')
                root = download_dir()
                ids = [make_job(link, mode, index, bool(data.get('reuse', True)), folder=root)['id'] for link in links]
                return self.send(202, {'ids': ids})
            return self.send(404, {'error': '接口不存在'})
        except (ValueError, TypeError, KeyError) as exc:
            return self.send(400, {'error': str(exc)})
        except PermissionError:
            return self.send(400, {'error': '无法写入该目录，请选择一个有写入权限的文件夹。'})
        except OSError as exc:
            return self.send(500, {'error': f'系统操作失败：{str(exc)[:180]}'})


def restore():
    with database() as db:
        rows = list(db.execute('SELECT kind,payload FROM records WHERE kind IN (?,?) ORDER BY rowid', ('job', 'collection')))
    for kind, payload in rows:
        record = json.loads(payload)
        if kind == 'job':
            JOBS[record['id']] = record
            if record['status'] in ACTIVE:
                update(record, status='queued', message='已恢复任务，等待继续。')
                PENDING.put(record)
        else:
            COLLECTIONS[record['id']] = record
            if record['status'] in SCANNING:
                update(record, 'collection', status='partial' if record.get('notes') else 'failed', complete=False,
                       message='上次采集已中断，已保留结果，可重新扫描。')
    legacy = WEB / 'legacy-tasks.json'
    if not rows and legacy.is_file():
        for job in read_json(legacy, []) or []:
            job.update(root=str(default_dir()), reuse=True, current=0, total=0, collection_id='')
            JOBS[job['id']] = job
            persist('job', job)


def main():
    global PORT
    parser = argparse.ArgumentParser(description='小红书下载工作台与开放接口')
    parser.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5557)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port 必须在1024–65535之间')
    PORT = args.port
    server = ThreadingHTTPServer((args.host, PORT), Handler)
    external_api.key(sys.modules[__name__])
    restore()
    roots = {str(default_dir()), str(download_dir())}
    roots.update(job['root'] for job in JOBS.values() if job.get('root'))
    for root in roots:
        result = migrate_archive(root)
        if result['removed']:
            print(f'已将 {result["notes"]} 篇作品的管理记录迁入数据库，清理 {result["removed"]} 个 JSON 文件。', flush=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=ensure_api, daemon=True).start()
    print(f'XHS 下载工作台：http://127.0.0.1:{PORT}', flush=True)
    server.serve_forever()
