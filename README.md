# 小红书作品下载工作台

面向 Windows 的中文下载工作台，支持单篇笔记、作者作品集、按作者和作品标题归档，以及供其他程序调用的 API。

本项目提供工作台、采集编排和开放接口，笔记解析依赖 [XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) 官方引擎。仓库不包含登录资料、API密钥、用户数据库、下载作品或原版二进制运行库。

## 功能

- 自选保存目录，按「作者名 / 作品标题」整理图片与视频。
- 输入作者主页，采集当前账号可见的作品，支持全部下载或选篇下载。
- 每篇作品一个文件夹，附带作品说明；管理数据保存在程序内部数据库。
- 任务进度、取消、失败重试、完整文件复用和重启恢复。
- 带持久 API Key 的 `/api/v1` 接口、交互文档和 Python 客户端。
- 默认仅本机运行，可由管理员显式开启局域网共享。

## 安装与启动

需要 Windows 10/11 x64、Python 3.10+。安装 Python 时请勾选 **Add Python to PATH**。

1. 克隆或下载本仓库并解压到普通文件夹，建议避免过深的路径。
2. 双击 **安装下载引擎.bat**。安装器下载固定的上游2.7版本，核对官方SHA256，并将 `main.exe` 和 `_internal` 安装到仓库根目录；已有引擎时不会覆盖。
3. 双击 **打开下载页面.bat**，打开 [本机工作台](http://127.0.0.1:5557/)。
4. 在工作台选择保存目录，粘贴笔记链接或分享文案开始下载。

也可以手动从 [上游2.7版本](https://github.com/JoeanAmier/XHS-Downloader/releases/tag/2.7) 下载 `XHS-Downloader_V2.7_Windows_X64.zip`，把压缩包中的 `main.exe` 和 `_internal` 放到本项目根目录。

```text
XHS-Data-Scraper/
├─ main.exe                  # 安装器获取，不纳入Git
├─ _internal/                # 引擎运行库，不纳入Git
├─ 前端/                     # 工作台与API源码
├─ 接口示例/                 # Python客户端、说明和OpenAPI规范
└─ 打开下载页面.bat
```

首次运行会自动创建独立的本地数据库和 API Key，不需要复制其他人的配置。

## 作者作品集

作者采集还需要 **Node.js 20+** 和 **Microsoft Edge**。在 PowerShell 中安装浏览器依赖：

```powershell
cd .\前端
npm install
```

然后在工作台切换“作者作品集”，粘贴作者主页并获取作品。首次打开采集窗口时，由本机操作员扫码登录或完成验证。登录状态只保存在这台电脑的 `前端/browser-profile`。

采集只覆盖当前账号可见的作品。界面显示部分结果时，不代表作者全部历史作品；私密、已删除或不可访问的作品无法保证获取。

## API接入

启动后打开 [接口文档](http://127.0.0.1:5557/api-docs)，点击“显示并填入本机 API Key”，然后展开接口并使用 **Try it out** 测试。

所有业务接口使用：

```http
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

创建任务返回202；查询任务到 `done` 才表示全部成功。任务、作者采集、作品列表、取消重试和文件下载均有文档。

完整说明：[接口调用说明](接口示例/接口调用说明.md) · [Python客户端](接口示例/xhs_client.py) · [OpenAPI规范](接口示例/openapi.json)

```powershell
$env:XHS_API_KEY = '管理员提供的密钥'
$env:XHS_BASE_URL = 'http://127.0.0.1:5557'
python .\接口示例\xhs_client.py health
python .\接口示例\xhs_client.py --out 'D:\下载作品' download '完整笔记链接'
```

## 局域网共享

管理员双击 **打开局域网共享.bat**，将新接口切换为监听 `0.0.0.0:5557`。正在处理任务时会拒绝切换，避免中断下载。

其他电脑使用 `http://服务器局域网IP:5557/api/v1` 与管理员提供的 API Key。按需仅对可信局域网放行 TCP5557。原始引擎的5556/5558端口不受新密钥保护，不应向调用者开放。脚本不会自动改变防火墙。

关闭共享使用 **关闭局域网共享.bat**。系统文件夹操作和工作台管理始终限于本机。默认未开放浏览器跨域 CORS；网页应用可通过自己的后端调用。公网部署需要额外配置 HTTPS 与网络访问控制。

## 本地验证

```powershell
python tests/test_api.py
python tests/test_installer.py
```

隔离HTTP测试覆盖鉴权、模拟远程访问边界、作者任务编排、取消重试、队列上限等，不需要Cookie，不启动真实采集，不写入用户下载目录。

开发环境另已验证真实笔记解析、媒体下载、已有195篇作者作品分页、选篇下载、客户端文件一致性和进程重启后密钥有效性，共59项检查。本轮未在第二台实体电脑验收；作者登录与网站可访问性需要使用者在自己的环境验证。

## 第三方组件

- 笔记解析引擎：[JoeanAmier/XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader)，GPL-3.0；本仓库不重新分发其二进制。
- 文档页面内置 Swagger UI 5.17.14，保留其 Apache-2.0 LICENSE 与 NOTICE。
- 作者采集使用 Playwright，通过npm安装。

参见 [第三方说明](THIRD_PARTY_NOTICES.md)。请仅下载自己拥有权利或已获授权的作品。
