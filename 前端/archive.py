"""Safe, per-author/per-note archives with verified reuse and atomic writes."""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

STATE_DB = Path(__file__).resolve().parent / 'archive.sqlite3'


def archive_record(kind, folder, value=None):
    """Keep bookkeeping in the application's database, outside the user's media folders."""
    connection = sqlite3.connect(STATE_DB, timeout=20)
    try:
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS archives (kind TEXT, path TEXT, payload TEXT, PRIMARY KEY(kind,path))')
            key = str(Path(folder).resolve()).casefold()
            if value is not None:
                connection.execute('INSERT OR REPLACE INTO archives VALUES (?,?,?)',
                                   (kind, key, json.dumps(value, ensure_ascii=False)))
                return value
            row = connection.execute('SELECT payload FROM archives WHERE kind=? AND path=?', (kind, key)).fetchone()
            if row:
                return json.loads(row[0])
    finally:
        connection.close()
    legacy_name = {'author': '.xhs-author.json', 'note': '.xhs-note.json', 'metadata': '作品信息.json'}[kind]
    legacy = read_json(Path(folder) / legacy_name)
    if legacy:
        identity = legacy.get('作品ID') if kind == 'metadata' else legacy.get('id')
        if isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity):
            return archive_record(kind, folder, legacy)
    return {}


def migrate_archive(root):
    """Import recognized legacy sidecars, then remove only those exact files."""
    root = Path(root).resolve()
    counts = {'removed': 0, 'notes': 0}
    if not root.is_dir():
        return counts
    for marker in root.rglob('.xhs-author.json'):
        try:
            inside(root, marker)
            record = read_json(marker)
            if not isinstance(record.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', record['id']):
                continue
            archive_record('author', marker.parent, record)
            marker.unlink()
            counts['removed'] += 1
        except (OSError, ValueError):
            continue
    for marker in root.rglob('.xhs-note.json'):
        try:
            inside(root, marker)
            record = read_json(marker)
            if not isinstance(record.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', record['id']) or not isinstance(record.get('files'), dict):
                continue
            archive_record('note', marker.parent, record)
            metadata_file = marker.parent / '作品信息.json'
            inside(root, metadata_file)
            metadata = read_json(metadata_file)
            if metadata.get('作品ID') == record['id']:
                archive_record('metadata', marker.parent, metadata)
                metadata_file.unlink()
                counts['removed'] += 1
            marker.unlink()
            counts['removed'] += 1
            counts['notes'] += 1
        except (OSError, ValueError):
            continue
    return counts


class Cancelled(Exception):
    pass


def safe_name(value, fallback='未命名', limit=70):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or ''))
    text = re.sub(r'\s+', ' ', text).strip(' .')[:limit].rstrip(' .') or fallback
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', text):
        text = '_' + text
    return text


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def inside(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError('保存路径超出所选文件夹，请检查目录设置。')
    return path


def archive_folder(root, data, author_override=''):
    root = Path(root).resolve()
    author_id = str(data.get('作者ID') or 'unknown')
    note_id = str(data.get('作品ID') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', note_id):
        raise ValueError('作品 ID 无效，无法创建文件夹。')
    author = safe_name(author_override or data.get('作者昵称'), author_id, 55)
    author_dir = inside(root, root / author)
    existing_author = archive_record('author', author_dir).get('id')
    if author_dir.exists() and (existing_author != author_id) and any(author_dir.iterdir()):
        author_dir = inside(root, root / f'{author} [{safe_name(author_id, limit=25)}]')
        fallback_id = archive_record('author', author_dir).get('id')
        if author_dir.exists() and any(author_dir.iterdir()) and fallback_id != author_id:
            raise ValueError('作者文件夹与已有文件冲突，请更换保存目录后重试。')
    author_dir.mkdir(parents=True, exist_ok=True)
    archive_record('author', author_dir, {'id': author_id, 'name': author_override or data.get('作者昵称')})
    title = safe_name(data.get('作品标题'), note_id, 75)
    target = inside(root, author_dir / title)
    # Existing unknown folders are never adopted: they may contain the user's own files.
    old = archive_record('note', target)
    if target.exists() and (old.get('id') != note_id) and any(target.iterdir()):
        target = inside(root, author_dir / f'{title} [{note_id}]')
    if len(str(target)) > 235:
        raise ValueError('保存路径过长，请选择层级更浅的保存目录。')
    target.mkdir(parents=True, exist_ok=True)
    old = archive_record('note', target)
    if old and old.get('id') != note_id:
        raise ValueError('发现文件夹标识冲突，请更换保存目录后重试。')
    if not old:
        archive_record('note', target, {'id': note_id, 'files': {}})
    return target


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def valid_old_file(folder, record):
    if not isinstance(record, dict) or not record.get('sha256'):
        return False
    try:
        file = inside(folder, folder / record['name'])
        return file.is_file() and file.stat().st_size == record['size'] and digest(file) == record['sha256']
    except (OSError, KeyError, ValueError):
        return False


def media_extension(head, content_type, kind):
    if head.startswith(b'\xff\xd8'):
        return '.jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if head.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return '.webp'
    if len(head) > 12 and head[4:8] == b'ftyp':
        return '.heic' if b'heic' in head[:32] or b'heix' in head[:32] else '.mp4'
    if content_type.startswith('video/') and not head.lstrip().startswith(b'<'):
        return '.mp4'
    raise ValueError('服务器未返回有效图片或视频，已停止保存该文件。')


def download_note(root, data, index, reuse, cancelled, progress, author_override=''):
    folder = archive_folder(root, data, author_override)
    addresses = data.get('下载地址') or []
    if not isinstance(addresses, list):
        addresses = [addresses]
    selected = set(index) if data.get('作品类型') == '图文' and index else set(range(1, len(addresses) + 1))
    selected = sorted(n for n in selected if 1 <= n <= len(addresses))
    if not selected:
        raise ValueError(f'作品共有 {len(addresses)} 个资源，请检查指定序号。')
    manifest = archive_record('note', folder)
    records = manifest.setdefault('files', {})
    files, reused, failures = [], 0, []
    for position, number in enumerate(selected, 1):
        if cancelled():
            raise Cancelled()
        old = records.get(str(number), {})
        progress(position - 1, len(selected), f'正在处理第 {position}/{len(selected)} 个文件')
        if reuse and valid_old_file(folder, old):
            files.append({'name': str((folder / old['name']).relative_to(root)), 'size': old['size']})
            reused += 1
            continue
        url = addresses[number - 1]
        parts = urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ('https', 'http') or not parts.hostname or not (parts.hostname.endswith('.xiaohongshu.com') or parts.hostname.endswith('.xhscdn.com') or parts.hostname.endswith('.xhslink.com')):
            failures.append(f'资源 {number}：下载地址不受支持')
            continue
        tmp = folder / f'.{number:02d}.{uuid.uuid4().hex}.part'
        try:
            for attempt in range(3):
                try:
                    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36', 'Referer': 'https://www.xiaohongshu.com/'})
                    with urlopen(request, timeout=35) as response, tmp.open('wb') as output:
                        head = response.read(65536)
                        ext = media_extension(head, response.headers.get('Content-Type', ''), data.get('作品类型'))
                        size = len(head)
                        output.write(head)
                        total = int(response.headers.get('Content-Length') or 0)
                        last = time.monotonic()
                        while True:
                            if cancelled():
                                raise Cancelled()
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            size += len(chunk)
                            if time.monotonic() - last > .8:
                                detail = f'{size / 1048576:.1f} MB' + (f' / {total / 1048576:.1f} MB' if total else '')
                                progress(position - 1, len(selected), f'文件 {position}/{len(selected)} · {detail}')
                                last = time.monotonic()
                        if not size or (total and size != total):
                            raise ValueError('文件接收不完整，请重试。')
                    break
                except Cancelled:
                    raise
                except Exception:
                    tmp.unlink(missing_ok=True)
                    if attempt == 2:
                        raise
                    time.sleep(1)
            if cancelled():
                raise Cancelled()
            name = f'{number:02d}{ext}'
            dest = inside(folder, folder / name)
            # Do not overwrite an unrelated, untracked file already placed by the user.
            if dest.exists() and old.get('name') != name:
                name = f'{number:02d}_{data["作品ID"]}{ext}'
                dest = inside(folder, folder / name)
                if dest.exists() and old.get('name') != name:
                    raise ValueError('目标文件名已有未识别文件，请检查作品文件夹。')
            checksum = digest(tmp)
            tmp.replace(dest)
            records[str(number)] = {'name': name, 'size': size, 'sha256': checksum}
            archive_record('note', folder, manifest)
            files.append({'name': str(dest.relative_to(root)), 'size': size})
        except Cancelled:
            raise
        except Exception as exc:
            failures.append(f'文件 {number}：{str(exc)[:120]}')
        finally:
            tmp.unlink(missing_ok=True)
    metadata = dict(data)
    archive_record('metadata', folder, metadata)
    description = f'{data.get("作品标题", "")}\n作者：{data.get("作者昵称", "")}\n作品链接：{data.get("作品链接", "")}\n\n{data.get("作品描述", "")}'
    (folder / '作品说明.txt').write_text(description.replace('\\n', '\n'), encoding='utf-8-sig')
    progress(len(files), len(selected), '文件处理完成')
    return {'files': files, 'folder': str(folder), 'reused': reused, 'expected': len(selected), 'errors': failures}
