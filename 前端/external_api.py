"""Versioned, authenticated API. All downloads share the workbench queue."""
import hashlib
import ipaddress
import json
import mimetypes
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

PREFIX = '/api/v1'
LIMIT = 1000


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def local(handler):
    return ipaddress.ip_address(handler.client_address[0]).is_loopback


def key(w):
    with w.LOCK, w.database() as db:
        row = db.execute('SELECT payload FROM records WHERE kind=? AND id=?', ('settings', 'api-key')).fetchone()
        if row:
            return json.loads(row[0])['key']
        value = 'xhs_' + secrets.token_urlsafe(36)
        db.execute('INSERT INTO records VALUES (?,?,?)', ('settings', 'api-key', json.dumps({'id': 'api-key', 'key': value})))
        return value


def fail(h, error):
    h.send(error.status, {'error': {'code': error.code, 'message': error.message}, 'request_id': uuid.uuid4().hex})


def body(h):
    if h.headers.get('Transfer-Encoding'):
        raise ApiError(400, 'INVALID_REQUEST', '不支持 Transfer-Encoding，请发送 Content-Length。')
    if h.headers.get_content_type() != 'application/json':
        raise ApiError(415, 'UNSUPPORTED_MEDIA_TYPE', '请求必须使用 application/json。')
    try:
        size = int(h.headers.get('Content-Length', '0'))
        if not 0 < size <= 131072:
            raise ApiError(413 if size > 131072 else 422, 'INVALID_BODY_SIZE', 'JSON 请求体应为 1–131072 字节。')
        h.connection.settimeout(15)
        value = json.loads(h.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise ApiError(422, 'INVALID_JSON', '请求体必须是合法的 JSON 对象。')


def fields(data, allowed):
    if set(data) - set(allowed):
        raise ApiError(422, 'UNKNOWN_FIELD', '未知字段：' + ', '.join(sorted(set(data) - set(allowed))))


def links(w, raw, profile=False):
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        raise ApiError(422, 'INVALID_URL', '请提供一条完整链接（url）。')
    parsed = w.parse_links(raw.strip())
    if len(parsed) != 1 or parsed[0] != raw.strip():
        raise ApiError(422, 'INVALID_URL', 'url 必须是单个完整的小红书链接。')
    address = urlsplit(parsed[0])
    if address.username or address.password or address.port not in (None, 80, 443):
        raise ApiError(422, 'INVALID_URL', '不支持该链接的主机或端口。')
    if w.is_profile(parsed[0]) != profile:
        raise ApiError(422, 'WRONG_URL_TYPE', '作者主页请使用 /collections；笔记请使用 /tasks。')
    return parsed[0]


def options(data):
    mode, index, reuse = data.get('mode', 'download'), data.get('index', []), data.get('reuse', True)
    if mode not in ('download', 'parse') or type(reuse) is not bool:
        raise ApiError(422, 'INVALID_OPTIONS', 'mode 应为 download 或 parse；reuse 应为布尔值。')
    if not isinstance(index, list) or len(index) > 100 or any(type(i) is not int or not 1 <= i <= 100 for i in index):
        raise ApiError(422, 'INVALID_INDEX', 'index 应为 1–100 的整数数组，空数组表示全部资源。')
    return mode, list(dict.fromkeys(index)), reuse


def capacity(w, count):
    if sum(j['status'] in w.ACTIVE for j in w.JOBS.values()) + count > LIMIT:
        raise ApiError(429, 'QUEUE_FULL', '任务队列已满，请等待处理后重试。')


def task(w, job):
    result = {k: job.get(k) for k in ('id', 'url', 'mode', 'status', 'message', 'title', 'author', 'kind', 'description', 'current', 'total', 'created', 'finished', 'collection_id')}
    result['resource_urls'] = list(job.get('addresses', []))
    result['error'] = {'code': job.get('error_code', 'TASK_FAILED'), 'message': job['message']} if job['status'] in ('failed', 'warning') else None
    result['files'] = [{'index': i, 'name': f['name'], 'size': f['size'], 'download_url': f'{PREFIX}/tasks/{job["id"]}/files/{i}'} for i, f in enumerate(job.get('files', []))]
    return result


def collection(w, value):
    result = w.public_collection(value)
    result.pop('preview', None)
    result['notes_url'] = f'{PREFIX}/collections/{value["id"]}/notes'
    return result


def page(h, values):
    query = parse_qs(urlsplit(h.path).query)
    try:
        offset, limit = int(query.get('offset', ['0'])[0]), int(query.get('limit', ['50'])[0])
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError()
    except ValueError:
        raise ApiError(422, 'INVALID_PAGINATION', 'offset ≥ 0，limit 范围为 1–200。')
    return {'items': values[offset:offset+limit], 'total': len(values), 'offset': offset, 'limit': limit}


def find(store, identity):
    if identity not in store:
        raise ApiError(404, 'NOT_FOUND', '指定任务或作品集不存在。')
    return store[identity]


def cancel(w, job):
    if job['status'] in w.ACTIVE:
        w.update(job, status='cancelling' if job['status'] in ('running', 'cancelling') else 'cancelled', message='正在停止，已保存文件会保留。')


def dispatch(h, w, path, method):
    parts = path[len(PREFIX):].strip('/').split('/')
    if method == 'GET':
        with w.LOCK:
            if parts == ['health']:
                try:
                    online = w.api_json('/openapi.json', timeout=2).get('info', {}).get('title') == 'XHS-Downloader'
                except (OSError, ValueError):
                    online = False
                return 200 if online else 503, {'api_version': '1.0.0', 'backend_online': online, 'active_tasks': sum(j['status'] in w.ACTIVE for j in w.JOBS.values()), 'storage_layout': '作者名/作品标题/文件', 'collection_runtime_ready': runtime_ready(w)}
            if parts == ['tasks']:
                return 200, page(h, [task(w, j) for j in reversed(list(w.JOBS.values()))])
            if len(parts) == 2 and parts[0] == 'tasks':
                return 200, task(w, find(w.JOBS, parts[1]))
            if len(parts) == 4 and parts[0] == 'tasks' and parts[2] == 'files':
                job = find(w.JOBS, parts[1])
                if not parts[3].isdigit() or len(parts[3]) > 6 or int(parts[3]) >= len(job.get('files', [])):
                    raise ApiError(404, 'FILE_NOT_FOUND', '资源文件不存在。')
                item = job['files'][int(parts[3])]
                file = w.inside(job['root'], Path(job['root']) / item['name'])
                if not file.is_file() or file.suffix.lower() not in w.MEDIA:
                    raise ApiError(404, 'FILE_NOT_FOUND', '资源文件已移动或删除。')
                return 'file', file
            if parts == ['collections']:
                return 200, page(h, [collection(w, c) for c in reversed(list(w.COLLECTIONS.values()))])
            if len(parts) in (2, 3) and parts[0] == 'collections':
                c = find(w.COLLECTIONS, parts[1])
                if len(parts) == 2:
                    return 200, collection(w, c)
                if parts[2] == 'notes':
                    return 200, page(h, list(c.get('notes', [])))
        raise ApiError(404, 'NOT_FOUND', '接口不存在。')
    data = body(h)
    # Persist successful submissions so a client retry with the same key does not duplicate work.
    idem = h.headers.get('Idempotency-Key')
    if idem and (not re.fullmatch(r'[\w.-]{1,128}', idem, re.ASCII)):
        raise ApiError(422, 'INVALID_IDEMPOTENCY_KEY', 'Idempotency-Key 仅允许 1–128 位英文、数字、下划线、点、连字符。')
    fingerprint = hashlib.sha256((path + json.dumps(data, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    with w.LOCK:
        if idem:
            with w.database() as db:
                row = db.execute('SELECT payload FROM records WHERE kind=? AND id=?', ('idempotency', idem)).fetchone()
            if row:
                record = json.loads(row[0])
                if record['fingerprint'] != fingerprint:
                    raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '此幂等键已用于不同请求，请更换键。')
                return record['status'], record['response']
        status, result = mutate(w, parts, data)
        if idem:
            w.persist('idempotency', {'id': idem, 'fingerprint': fingerprint, 'status': status, 'response': result})
        return status, result


def runtime_ready(w):
    try:
        w.runtime_paths()
        return True
    except ValueError:
        return False


def mutate(w, parts, data):
    if parts == ['tasks']:
        fields(data, ('url', 'mode', 'index', 'reuse'))
        url = links(w, data.get('url'))
        mode, index, reuse = options(data)
        capacity(w, 1)
        job = w.make_job(url, mode, index, reuse)
        return 202, {'id': job['id'], 'status': 'queued', 'status_url': f'{PREFIX}/tasks/{job["id"]}'}
    if len(parts) == 3 and parts[0] == 'tasks':
        fields(data, ())
        job = find(w.JOBS, parts[1])
        if parts[2] == 'cancel':
            cancel(w, job)
            return 200, task(w, job)
        if parts[2] == 'retry':
            if job['status'] in w.ACTIVE:
                raise ApiError(409, 'TASK_ACTIVE', '任务仍在处理中。')
            capacity(w, 1)
            new = w.make_job(job['url'], job['mode'], job['index'], True, w.COLLECTIONS.get(job.get('collection_id')), Path(job['root']), job.get('title', ''))
            return 202, {'id': new['id'], 'status': 'queued', 'status_url': f'{PREFIX}/tasks/{new["id"]}'}
    if parts == ['collections']:
        fields(data, ('url',))
        url = links(w, data.get('url'), True)
        if not runtime_ready(w):
            raise ApiError(503, 'COLLECTOR_UNAVAILABLE', '服务器尚未安装 Node.js / Playwright 作者采集环境。')
        if not w.ACTIVE_SCAN.acquire(blocking=False):
            raise ApiError(409, 'COLLECTOR_BUSY', '已有作者正在采集，请稍后重试。')
        c = {'id': uuid.uuid4().hex, 'url': url, 'author': '', 'status': 'starting', 'message': '正在启动采集窗口…', 'notes': [], 'complete': False, 'created': time.time()}
        try:
            w.COLLECTIONS[c['id']] = c
            w.persist('collection', c)
            threading.Thread(target=w.collect, args=(c,), daemon=True).start()
        except Exception:
            w.ACTIVE_SCAN.release()
            raise
        return 202, {'id': c['id'], 'status': 'starting', 'status_url': f'{PREFIX}/collections/{c["id"]}'}
    if len(parts) == 3 and parts[0] == 'collections':
        c = find(w.COLLECTIONS, parts[1])
        if parts[2] == 'cancel':
            fields(data, ())
            if c['status'] in w.SCANNING:
                w.update(c, 'collection', status='cancelled', complete=False, message='已停止采集，结果已保留。')
                process = w.PROCESSES.get(c['id'])
                if process and process.poll() is None:
                    try:
                        process.stdin.write('{"action":"cancel"}\n')
                        process.stdin.flush()
                    except OSError:
                        pass
            return 200, collection(w, c)
        if parts[2] == 'download':
            fields(data, ('note_ids',))
            if c['status'] in w.SCANNING:
                raise ApiError(409, 'COLLECTION_ACTIVE', '请先等待采集结束或停止采集。')
            if any(j['status'] in w.ACTIVE and j.get('collection_id') == c['id'] for j in w.JOBS.values()):
                raise ApiError(409, 'COLLECTION_DOWNLOADING', '这个作品集正在下载。')
            selected = data.get('note_ids')
            if selected is not None and (not isinstance(selected, list) or not selected or not all(isinstance(i, str) for i in selected)):
                raise ApiError(422, 'INVALID_NOTE_IDS', 'note_ids 应为非空的笔记 ID 数组；省略时下载当前结果中的全部笔记。')
            known = {n['id'] for n in c.get('notes', [])}
            if selected and set(selected) - known:
                raise ApiError(422, 'UNKNOWN_NOTE_ID', 'note_ids 中有不属于此作品集的笔记。')
            notes = [n for n in c.get('notes', []) if selected is None or n['id'] in selected]
            if not notes:
                raise ApiError(409, 'COLLECTION_EMPTY', '当前作品集没有可下载的笔记。')
            capacity(w, len(notes))
            root = w.download_dir()
            ids = [w.make_job(n['url'], collection=c, folder=root, title=n['title'])['id'] for n in notes]
            return 202, {'ids': ids, 'count': len(ids), 'collection_complete': bool(c.get('complete'))}
    raise ApiError(404, 'NOT_FOUND', '接口不存在。')


def stream(h, file):
    try:
        source = file.open('rb')
    except OSError:
        raise ApiError(404, 'FILE_NOT_FOUND', '文件已移动或删除。')
    with source:
        import os
        size = os.fstat(source.fileno()).st_size
        h.send_response(200)
        h.send_header('Content-Type', mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        h.send_header('Content-Length', str(size))
        h.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(file.name))
        h.send_header('Cache-Control', 'no-store')
        h.send_header('X-Content-Type-Options', 'nosniff')
        h.end_headers()
        try:
            while chunk := source.read(1024 * 256):
                h.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def handle(h, w):
    path, method = urlsplit(h.path).path, h.command
    if path == '/api/admin/api-key':
        try:
            if not local(h) or method != 'POST' or not secrets.compare_digest(h.headers.get('X-XHS-Token', '').encode('utf-8'), w.TOKEN.encode('utf-8')):
                raise ApiError(403, 'LOCAL_ADMIN_ONLY', '仅本机工作台管理员可读取密钥。')
            fields(body(h), ())
            h.send(200, {'api_key': key(w)})
        except ApiError as exc:
            fail(h, exc)
        return True
    if path in ('/api/openapi.json', '/api/v1/openapi.json'):
        if method == 'GET':
            h.send(200, schema())
        else:
            fail(h, ApiError(405, 'METHOD_NOT_ALLOWED', '请使用 GET。'))
        return True
    if not (path == PREFIX or path.startswith(PREFIX + '/')):
        return False
    try:
        authorization = h.headers.get('Authorization', '')
        if not secrets.compare_digest(authorization.encode('utf-8'), ('Bearer ' + key(w)).encode('utf-8')):
            h.send_response(401)
            h.send_header('WWW-Authenticate', 'Bearer')
            data = json.dumps({'error': {'code': 'UNAUTHORIZED', 'message': '请提供 Authorization: Bearer <API_KEY>。'}}).encode()
            h.send_header('Content-Type', 'application/json; charset=utf-8')
            h.send_header('Content-Length', str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return True
        if method not in ('GET', 'POST'):
            raise ApiError(405, 'METHOD_NOT_ALLOWED', '此接口仅支持 GET / POST。')
        status, value = dispatch(h, w, path, method)
        if status == 'file':
            stream(h, value)
        else:
            h.send(status, value)
    except ApiError as exc:
        fail(h, exc)
    except (OSError, TimeoutError):
        fail(h, ApiError(503, 'SERVICE_UNAVAILABLE', '服务暂时不可用，请稍后重试。'))
    except (ValueError, TypeError, KeyError):
        fail(h, ApiError(422, 'INVALID_REQUEST', '请求参数格式不正确。'))
    return True


def schema():
    ref = lambda name: {'$ref': '#/components/schemas/' + name}
    string = {'type': 'string'}
    ident = {'name': 'id', 'in': 'path', 'required': True, 'schema': string, 'description': '任务或作品集 ID'}
    pagination = [{'name': n, 'in': 'query', 'schema': {'type': 'integer', 'default': d, 'minimum': minimum, **({'maximum': 200} if n == 'limit' else {})}} for n, d, minimum in [('offset', 0, 0), ('limit', 50, 1)]]
    idem = {'name': 'Idempotency-Key', 'in': 'header', 'schema': {'type': 'string', 'maxLength': 128}, 'description': '推荐每次提交生成唯一值；相同键与请求重复提交返回原响应，内容不同返回 409。'}
    paths = {}
    def add(path, method, title, tag, request=None, response=None, status='200', parameters=None, description=''):
        op = {'operationId': method + '_' + re.sub(r'[^a-z0-9]+', '_', path).strip('_'), 'summary': title, 'tags': [tag], 'description': description, 'responses': {status: {'description': '已接收，需轮询状态' if status == '202' else '成功', 'content': {'application/json': {'schema': response or {'type': 'object'}}}}, **{str(c): {'description': msg, 'content': {'application/json': {'schema': ref('Error')}}} for c, msg in [(401, '密钥缺失或错误'), (404, '资源不存在'), (409, '当前状态冲突'), (422, '参数错误'), (429, '任务队列已满'), (503, '服务未就绪')]}}}
        if request:
            op['requestBody'] = {'required': True, 'content': {'application/json': {'schema': request}}}
        if parameters:
            op['parameters'] = parameters
        paths.setdefault(path, {})[method] = op
    obj = lambda properties, required=(): {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}
    add('/health', 'get', '检查解析服务与采集环境', '服务')
    add('/tasks', 'get', '分页查询任务', '任务', response=ref('TaskPage'), parameters=pagination)
    add('/tasks', 'post', '创建下载或解析任务', '任务', ref('TaskInput'), ref('Accepted'), '202', [idem], '下载保存到管理员选择的目录，结构为 作者名/作品标题/文件。mode=parse 只返回资源地址。202 仅表示接收成功；请查询任务直到终态。')
    add('/tasks/{id}', 'get', '查询任务状态、结果和文件地址', '任务', response=ref('Task'), parameters=[ident])
    add('/tasks/{id}/cancel', 'post', '停止任务（保留已保存文件）', '任务', obj({}), ref('Task'), parameters=[ident])
    add('/tasks/{id}/retry', 'post', '重试任务并复用完整文件', '任务', obj({}), ref('Accepted'), '202', [ident, idem])
    add('/tasks/{id}/files/{index}', 'get', '下载服务器已保存的文件', '任务', parameters=[ident, {'name': 'index', 'in': 'path', 'required': True, 'schema': {'type': 'integer', 'minimum': 0}}], description='使用任务返回的 download_url；仍需 Bearer 密钥。index 从 0 开始，与创建任务的 index（从1开始）不同。文件以流方式返回。')
    paths['/tasks/{id}/files/{index}']['get']['responses']['200']['content'] = {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}
    add('/collections', 'get', '分页查询已采集的作者作品集', '作者作品集', response=ref('CollectionPage'), parameters=pagination)
    add('/collections', 'post', '从作者主页采集作品', '作者作品集', obj({'url': {**string, 'description': '完整作者主页 URL，含分享链接原有参数。'}}, ['url']), ref('Accepted'), '202', [idem], '服务器会打开 Edge 采集窗口。首次使用需服务器操作员扫码登录；login/attention 状态需操作员处理。只能保证取得当前账号可见的作品；complete=false 时并非全部。每次仅一个采集任务。')
    add('/collections/{id}', 'get', '查询采集状态及可见作品数量', '作者作品集', response=ref('Collection'), parameters=[ident])
    add('/collections/{id}/notes', 'get', '分页获取作品列表', '作者作品集', response=ref('NotePage'), parameters=[ident, *pagination])
    add('/collections/{id}/cancel', 'post', '停止采集，保留已有结果', '作者作品集', obj({}), ref('Collection'), parameters=[ident])
    add('/collections/{id}/download', 'post', '下载全部已采集作品或指定作品', '作者作品集', obj({'note_ids': {'type': 'array', 'minItems': 1, 'items': string, 'description': '省略表示当前作品集中的全部；完整性请查看 collection_complete。'}}), obj({'ids': {'type': 'array', 'items': string}, 'count': {'type': 'integer'}, 'collection_complete': {'type': 'boolean'}}), '202', [ident, idem], '为每篇作品创建独立任务，按作者名/作品标题归档。轮询返回的每个任务 ID。活动任务上限1000，超过返回429；可使用 note_ids 分批下载。')
    schemas = {
        'Error': obj({'error': obj({'code': string, 'message': string}, ['code', 'message']), 'request_id': string}, ['error']),
        'TaskInput': obj({'url': {**string, 'description': '单篇笔记完整URL（含原有 xsec_token），也支持 xhslink.com 短链。'}, 'mode': {'type': 'string', 'enum': ['download', 'parse'], 'default': 'download'}, 'index': {'type': 'array', 'maxItems': 100, 'items': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'default': [], 'description': '要保存的资源序号，从1开始；空数组表示全部。'}, 'reuse': {'type': 'boolean', 'default': True}}, ['url']),
        'Accepted': obj({'id': string, 'status': string, 'status_url': string}, ['id', 'status', 'status_url']),
        'Task': obj({'id': string, 'url': string, 'mode': string, 'status': {'type': 'string', 'enum': ['queued', 'running', 'cancelling', 'done', 'warning', 'failed', 'cancelled']}, 'message': string, 'title': {'type': ['string', 'null']}, 'author': {'type': ['string', 'null']}, 'kind': {'type': ['string', 'null']}, 'description': {'type': ['string', 'null']}, 'collection_id': string, 'current': {'type': 'integer'}, 'total': {'type': 'integer'}, 'created': {'type': 'number'}, 'finished': {'type': ['number', 'null']}, 'resource_urls': {'type': 'array', 'items': string}, 'files': {'type': 'array', 'items': ref('File')}, 'error': {'anyOf': [obj({'code': string, 'message': string}), {'type': 'null'}]}}, ['id', 'status', 'files', 'resource_urls']),
        'File': obj({'index': {'type': 'integer'}, 'name': string, 'size': {'type': 'integer'}, 'download_url': string}, ['index', 'name', 'size', 'download_url']),
        'Collection': {'type': 'object', 'properties': {'id': string, 'url': string, 'author': string, 'status': {'type': 'string', 'enum': ['starting', 'scanning', 'login', 'attention', 'complete', 'partial', 'failed', 'cancelled']}, 'message': string, 'note_count': {'type': 'integer'}, 'complete': {'type': 'boolean'}, 'notes_url': string, 'downloads': {'type': 'object'}, 'created': {'type': 'number'}, 'finished': {'type': 'number'}}},
        'Note': obj({'id': string, 'url': string, 'title': string, 'author': string, 'kind': string}),
    }
    for name, entry in [('TaskPage', 'Task'), ('CollectionPage', 'Collection'), ('NotePage', 'Note')]:
        schemas[name] = obj({'items': {'type': 'array', 'items': ref(entry)}, 'total': {'type': 'integer'}, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['items', 'total', 'offset', 'limit'])
    return {'openapi': '3.1.0', 'info': {'title': '小红书下载服务 · 开放接口', 'version': '1.0.0', 'description': '可供脚本和其他应用调用的真实接口。先获取管理员提供的 API Key，点击 Authorize 输入密钥（不含 Bearer 前缀）。创建任务后轮询状态；done 成功，warning 部分成功，failed 失败，cancelled 已停止。文件保存到服务器选定目录，调用者通过文件接口获取到自己的电脑。'}, 'servers': [{'url': PREFIX, 'description': '当前服务器'}], 'security': [{'BearerAuth': []}], 'components': {'securitySchemes': {'BearerAuth': {'type': 'http', 'scheme': 'bearer', 'description': '由服务器管理员提供的持久 API Key，不是浏览器页面令牌。'}}, 'schemas': schemas}, 'paths': paths}
