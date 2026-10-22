"""小红书下载服务 API v1 客户端。仅使用 Python 标准库，Python 3.10+。"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# Windows redirected consoles may use GBK; titles can contain emoji.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(errors='backslashreplace')


class Client:
    def __init__(self, base_url, api_key):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path.rstrip('/') not in ('', '/api/v1') or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('base 应为 http://服务器IP:5557 或 http://服务器IP:5557/api/v1')
        self.origin = f'{parsed.scheme}://{parsed.netloc}'
        self.base = self.origin + '/api/v1'
        self.api_key = api_key

    def request(self, path, data=None, idempotency_key=None):
        headers = {'Authorization': 'Bearer ' + self.api_key}
        payload = None
        if data is not None:
            payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
            headers['Idempotency-Key'] = idempotency_key or uuid.uuid4().hex
        try:
            with urlopen(Request(self.base + path, data=payload, headers=headers), timeout=45) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            try:
                value = json.loads(error.read().decode('utf-8'))['error']
                message = f'{value["code"]}: {value["message"]}'
            except (ValueError, KeyError, TypeError):
                message = '请求失败'
            raise RuntimeError(f'HTTP {error.code} {message}') from None

    def wait(self, identity, kind='tasks', timeout=900):
        deadline, previous = time.monotonic() + timeout, ''
        while time.monotonic() < deadline:
            result = self.request(f'/{kind}/{identity}')
            marker = f'{result["status"]} · {result["message"]}'
            if marker != previous:
                print(marker, flush=True)
                if kind == 'collections' and result['status'] in ('login', 'attention'):
                    print('需要服务器操作员在采集窗口扫码登录或处理验证。', flush=True)
                previous = marker
            if kind == 'tasks' and result['status'] in ('done', 'warning', 'failed', 'cancelled'):
                return result
            if kind == 'collections':
                if result['status'] in ('complete', 'partial', 'failed', 'cancelled'):
                    return result
            time.sleep(2)
        raise TimeoutError(f'等待超时；任务仍保留在服务中，ID={identity}。可继续查询或调用 cancel。')

    def save_files(self, task, destination):
        def safe(value):
            value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', value or '').strip(' .')[:65]
            if not value or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
                value = '_' + (value or '未命名')
            return value
        folder = Path(destination).resolve() / safe(task.get('author')) / safe(task.get('title'))
        folder.mkdir(parents=True, exist_ok=True)
        for item in task['files']:
            address = item['download_url']
            if not address.startswith('/api/v1/tasks/') or urlsplit(address).netloc:
                raise RuntimeError('服务器返回了不支持的文件地址')
            name = safe(item['name'].replace('\\', '/').rsplit('/', 1)[-1])
            file = folder / name
            temporary = folder / (name + '.' + uuid.uuid4().hex + '.part')
            try:
                with urlopen(Request(self.origin + address, headers={'Authorization': 'Bearer ' + self.api_key}), timeout=120) as response, temporary.open('xb') as output:
                    while chunk := response.read(256 * 1024):
                        output.write(chunk)
                if temporary.stat().st_size != item['size']:
                    raise RuntimeError('文件大小校验失败，请重试文件下载')
                temporary.replace(file)
            finally:
                temporary.unlink(missing_ok=True)
            print('已保存：' + str(file))
        return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default=os.environ.get('XHS_BASE_URL', 'http://127.0.0.1:5557'))
    parser.add_argument('--out', default='小红书下载', help='调用者电脑的文件保存目录')
    parser.add_argument('--index', type=int, nargs='*', default=[], help='资源序号，从1开始；省略则全部')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('action', choices=('health', 'parse', 'download', 'collect', 'collection', 'task'))
    parser.add_argument('value', nargs='?', help='笔记URL、作者主页URL或任务/作品集ID')
    args = parser.parse_args()
    api_key = os.environ.get('XHS_API_KEY')
    if not api_key:
        parser.error('请先设置环境变量 XHS_API_KEY（从服务器管理员获取）。')
    if args.action != 'health' and not args.value:
        parser.error('此操作需要链接或ID。')
    client = Client(args.base, api_key)
    if args.action == 'health':
        result = client.request('/health')
    elif args.action in ('parse', 'download'):
        accepted = client.request('/tasks', {'url': args.value, 'mode': 'parse' if args.action == 'parse' else 'download', 'index': args.index})
        print('任务ID：' + accepted['id'])
        result = client.wait(accepted['id'], timeout=args.timeout)
        if result['status'] != 'done':
            raise RuntimeError(f'任务未全部成功：{result["status"]} {result["message"]}；ID={accepted["id"]}')
        if args.action == 'download':
            client.save_files(result, args.out)
    elif args.action == 'task':
        result = client.request('/tasks/' + args.value)
    else:
        identity = args.value
        if args.action == 'collect':
            identity = client.request('/collections', {'url': args.value})['id']
            print('作品集ID：' + identity)
            collection = client.wait(identity, 'collections', args.timeout)
            if collection['status'] in ('failed', 'cancelled'):
                raise RuntimeError(f'作者采集未完成：{collection["message"]}；ID={identity}')
        result = client.request('/collections/' + identity)
        result['notes'] = []
        offset = 0
        while True:
            page = client.request(f'/collections/{identity}/notes?offset={offset}&limit=200')
            result['notes'].extend(page['items'])
            offset += len(page['items'])
            if offset >= page['total'] or not page['items']:
                break
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print('错误：' + str(error), file=sys.stderr)
        sys.exit(1)
