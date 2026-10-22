"""Install the pinned upstream Windows x64 engine without overwriting local data."""
import argparse
import hashlib
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

URL = 'https://github.com/JoeanAmier/XHS-Downloader/releases/download/2.7/XHS-Downloader_V2.7_Windows_X64.zip'
SHA256 = 'ff5e7b6355895d5d18232f5db69d5b08c7237720c2c8c57b1d9b5bde8fa40c99'


def install(root, archive_path=None):
    root = Path(root).resolve()
    if (root / 'main.exe').exists() or (root / '_internal').exists():
        raise ValueError('目标目录已有 main.exe 或 _internal，未覆盖。已有完整引擎时请直接启动工作台。')
    with tempfile.TemporaryDirectory(prefix='.engine-install-', dir=root) as temporary:
        temporary = Path(temporary)
        if archive_path:
            archive_path = Path(archive_path).resolve()
        else:
            archive_path = temporary / 'engine.zip'
            print('正在下载官方 XHS-Downloader 2.7 Windows x64（约41MB）…', flush=True)
            deadline, received, last_report = time.monotonic() + 180, 0, 0
            with urlopen(Request(URL, headers={'User-Agent': 'XHS-Data-Scraper-Installer/1.0'}), timeout=30) as response, archive_path.open('xb') as output:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError('官方压缩包下载超过3分钟，请按README手动下载后安装。')
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    if received - last_report >= 4 * 1024 * 1024:
                        print(f'已下载 {received // (1024 * 1024)} MB', flush=True)
                        last_report = received
        digest = hashlib.sha256()
        with archive_path.open('rb') as source:
            while chunk := source.read(256 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != SHA256:
            raise ValueError('压缩包SHA256与官方固定版本不一致，已中止安装。')
        print('SHA256校验通过，正在解压…', flush=True)
        stage = temporary / 'stage'
        stage.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            candidates = [PurePosixPath(i.filename) for i in archive.infolist() if PurePosixPath(i.filename).name == 'main.exe']
            if len(candidates) != 1:
                raise ValueError('压缩包结构不符合预期：应仅包含一个main.exe。')
            prefix = candidates[0].parent
            for member in archive.infolist():
                name = PurePosixPath(member.filename)
                if name.is_absolute() or '..' in name.parts or '\\' in member.filename:
                    raise ValueError('压缩包中存在不安全路径，已中止。')
                try:
                    relative = name.relative_to(prefix)
                except ValueError:
                    continue
                if not relative.parts or relative.parts[0] not in ('main.exe', '_internal'):
                    continue
                target = stage.joinpath(*relative.parts).resolve()
                if not target.is_relative_to(stage.resolve()):
                    raise ValueError('解压路径超出目标目录。')
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)
        if not (stage / 'main.exe').is_file() or not (stage / '_internal').is_dir():
            raise ValueError('压缩包缺少main.exe或_internal运行库。')
        # Do not replace an existing user's engine or settings.
        if (root / 'main.exe').exists() or (root / '_internal').exists():
            raise ValueError('安装期间目标目录被修改，未覆盖。')
        shutil.move(str(stage / '_internal'), str(root / '_internal'))
        shutil.move(str(stage / 'main.exe'), str(root / 'main.exe'))
        print('引擎已安装。请双击“打开下载页面.bat”。', flush=True)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='可选：已下载的官方2.7 Windows x64 zip；仍会校验SHA256')
    args = parser.parse_args()
    try:
        install(Path(__file__).resolve().parents[1], args.archive)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print('安装失败：' + str(error), file=sys.stderr)
        sys.exit(1)
