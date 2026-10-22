param([switch]$Share, [switch]$LocalOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$page = 'http://127.0.0.1:5557/'
$serverFile = Join-Path $root '前端\server.py'
$running = $false
$status = $null
try {
    $status = Invoke-RestMethod -Uri ($page + 'api/status') -TimeoutSec 3
    $running = $null -ne $status.folder
} catch {}
if ($Share -and $LocalOnly) { throw 'Share 和 LocalOnly 不能同时使用。' }
$needsRestart = $running -and (($Share -and $status.bind_host -ne '0.0.0.0') -or ($LocalOnly -and $status.bind_host -eq '0.0.0.0'))
if ($needsRestart) {
    $state = Invoke-RestMethod -Uri ($page + 'api/state') -TimeoutSec 3
    $scanning = @($state.collections | Where-Object { $_.status -in @('starting','scanning','login','attention') }).Count
    if ($state.counts.active -or $scanning) { throw '正在处理任务。请先等待完成或在工作台停止，再切换共享模式。' }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 5557 -ErrorAction SilentlyContinue)
    $processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($processIds.Count -ne 1) { throw '无法确认工作台进程，请手动关闭后重新启动。' }
    $current = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $processIds[0])
    if (-not $current -or $current.CommandLine.IndexOf($serverFile, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or [IO.Path]::GetFileName($current.ExecutablePath) -ne 'python.exe') {
        throw '端口5557不是本程序的 Python 服务，未执行关闭。'
    }
    Stop-Process -Id $current.ProcessId
    $running = $false
    Start-Sleep -Milliseconds 500
}
if (-not $running) {
    $python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand -or $pythonCommand.Source -match 'WindowsApps') { throw '请安装 Python 3.10 或以上版本，并勾选 Add Python to PATH。' }
        $python = $pythonCommand.Source
    }
    & $python -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
    if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.10 或以上版本。' }
    $logs = Join-Path $root '前端\logs'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $bindAddress = '127.0.0.1'
    if ($Share) { $bindAddress = '0.0.0.0' }
    Start-Process -FilePath $python -ArgumentList ('"' + $serverFile + '" --host ' + $bindAddress) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'web.stdout.log') -RedirectStandardError (Join-Path $logs 'web.stderr.log') | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            $status = Invoke-RestMethod -Uri ($page + 'api/status') -TimeoutSec 2
            $running = $status.api_version -eq '1.0.0'
            if ($running) { break }
        } catch {}
    }
    if (-not $running) { throw '启动失败，请查看前端\logs日志。' }
}
if ($Share) {
    Write-Host '已启用局域网接口。仅向可信调用方提供 API Key。'
    Write-Host '请仅按需放行入站端口5557；原始解析端口5556和5558不应向其他电脑开放。'
    Write-Host '局域网IP地址：'
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object InterfaceAlias,IPAddress | Format-Table
    Start-Process ($page + 'api-docs')
} else {
    Start-Process $page
}
