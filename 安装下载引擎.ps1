$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand -or $pythonCommand.Source -match 'WindowsApps') {
    throw '请先安装Python 3.10+，勾选Add Python to PATH，然后重新打开此安装器。'
}
& $pythonCommand.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python版本过低，需要3.10或以上版本。' }
& $pythonCommand.Source (Join-Path $root 'scripts\install_engine.py')
if ($LASTEXITCODE -ne 0) { throw '引擎安装未完成。也可以按照README手动安装官方2.7 Windows x64压缩包。' }
