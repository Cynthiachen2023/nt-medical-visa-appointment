[CmdletBinding()]
param(
    [string]$PythonCommand = "python",
    [switch]$SkipDependencyInstall,
    [switch]$SkipTests,
    [switch]$Clean
)

<#
.SYNOPSIS
    构建包含固定版本 Chrome for Testing 的 Windows 应用目录。

.DESCRIPTION
    脚本只在仓库的 .build 目录中生成和清理文件。它会验证浏览器压缩包的
    文件大小与 SHA-256，运行测试，再调用 PyInstaller onedir。Inno Setup
    安装程序由后续独立步骤生成，不属于本脚本职责。
#>

$RepoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$BuildRoot = Join-Path $RepoRoot ".build"
$DownloadsRoot = Join-Path $BuildRoot "downloads"
$BrowserRoot = Join-Path $BuildRoot "browser"
$DistRoot = Join-Path $BuildRoot "dist"
$PyInstallerRoot = Join-Path $BuildRoot "pyinstaller"
$SpecRoot = Join-Path $BuildRoot "spec"
$TestTempRoot = Join-Path $BuildRoot "pytest-temp"
$VenvRoot = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$ManifestPath = Join-Path $PSScriptRoot "browser-manifest.json"
$EntryPoint = Join-Path $PSScriptRoot "launcher.py"
$ApplicationName = "BupaAppointmentMonitor"


function Assert-GeneratedPath {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $Candidate = [System.IO.Path]::GetFullPath($Path)
    $AllowedPrefix = [System.IO.Path]::GetFullPath($BuildRoot).TrimEnd("\") + "\"
    if (-not $Candidate.StartsWith($AllowedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝操作 .build 以外的路径：$Candidate"
    }
}


function Remove-GeneratedDirectory {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    Assert-GeneratedPath -Path $Path
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    }
}


function Invoke-Python {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    & $VenvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令执行失败，退出码：$LASTEXITCODE"
    }
}


function Test-Artifact {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [object]$Artifact
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $File = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($File.Length -ne [int64]$Artifact.size_bytes) {
        return $false
    }
    $ActualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    return $ActualHash.Equals(
        [string]$Artifact.sha256,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}


function Get-VerifiedArtifact {
    param(
        [Parameter(Mandatory)]
        [object]$Artifact
    )

    $ArchivePath = Join-Path $DownloadsRoot ([string]$Artifact.archive_name)
    if (Test-Artifact -Path $ArchivePath -Artifact $Artifact) {
        Write-Host "使用已验证缓存：$($Artifact.archive_name)" -ForegroundColor Green
        return $ArchivePath
    }

    if (Test-Path -LiteralPath $ArchivePath) {
        Assert-GeneratedPath -Path $ArchivePath
        Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction Stop
    }

    Write-Host "下载：$($Artifact.url)" -ForegroundColor Cyan
    Invoke-WebRequest `
        -Uri ([string]$Artifact.url) `
        -OutFile $ArchivePath `
        -UseBasicParsing `
        -ErrorAction Stop

    if (-not (Test-Artifact -Path $ArchivePath -Artifact $Artifact)) {
        Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
        throw "下载文件校验失败：$($Artifact.archive_name)"
    }
    Write-Host "校验通过：$($Artifact.archive_name)" -ForegroundColor Green
    return $ArchivePath
}


if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "找不到浏览器清单：$ManifestPath"
}
if (-not (Test-Path -LiteralPath $EntryPoint -PathType Leaf)) {
    throw "找不到应用入口：$EntryPoint"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($Manifest.schema_version -ne 1) {
    throw "不支持浏览器清单版本：$($Manifest.schema_version)"
}
if ($Manifest.platform -ne "win64") {
    throw "当前构建只支持 win64 浏览器，清单平台为：$($Manifest.platform)"
}

New-Item -ItemType Directory -Path $BuildRoot -Force -ErrorAction Stop | Out-Null
New-Item -ItemType Directory -Path $DownloadsRoot -Force -ErrorAction Stop | Out-Null

if ($Clean) {
    Write-Host "清理旧的构建输出…" -ForegroundColor Cyan
    Remove-GeneratedDirectory -Path $BrowserRoot
    Remove-GeneratedDirectory -Path $DistRoot
    Remove-GeneratedDirectory -Path $PyInstallerRoot
    Remove-GeneratedDirectory -Path $SpecRoot
    Remove-GeneratedDirectory -Path $TestTempRoot
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Host "创建项目虚拟环境…" -ForegroundColor Cyan
    & $PythonCommand -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "创建虚拟环境失败，退出码：$LASTEXITCODE"
    }
}

Push-Location -LiteralPath $RepoRoot
try {
    if (-not $SkipDependencyInstall) {
        Write-Host "安装项目和构建依赖…" -ForegroundColor Cyan
        Invoke-Python -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-e", ".[dev]")
    }

    if (-not $SkipTests) {
        Write-Host "运行自动化测试…" -ForegroundColor Cyan
        # 使用仓库内的临时目录，避免不同 Windows 账户的系统临时目录权限冲突。
        Remove-GeneratedDirectory -Path $TestTempRoot
        Invoke-Python -Arguments @(
            "-m",
            "pytest",
            "tests",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            $TestTempRoot
        )
    }

    $ChromeArchive = Get-VerifiedArtifact -Artifact $Manifest.artifacts.chrome
    $DriverArchive = Get-VerifiedArtifact -Artifact $Manifest.artifacts.chromedriver

    Remove-GeneratedDirectory -Path $BrowserRoot
    New-Item -ItemType Directory -Path $BrowserRoot -Force -ErrorAction Stop | Out-Null
    Write-Host "解压固定版本浏览器和驱动…" -ForegroundColor Cyan
    Expand-Archive -LiteralPath $ChromeArchive -DestinationPath $BrowserRoot -Force
    Expand-Archive -LiteralPath $DriverArchive -DestinationPath $BrowserRoot -Force

    $ChromeExecutable = Join-Path $BrowserRoot ([string]$Manifest.artifacts.chrome.executable)
    $DriverExecutable = Join-Path $BrowserRoot ([string]$Manifest.artifacts.chromedriver.executable)
    if (-not (Test-Path -LiteralPath $ChromeExecutable -PathType Leaf)) {
        throw "解压后找不到 Chrome：$ChromeExecutable"
    }
    if (-not (Test-Path -LiteralPath $DriverExecutable -PathType Leaf)) {
        throw "解压后找不到 ChromeDriver：$DriverExecutable"
    }

    Remove-GeneratedDirectory -Path $DistRoot
    Remove-GeneratedDirectory -Path $PyInstallerRoot
    Remove-GeneratedDirectory -Path $SpecRoot
    New-Item -ItemType Directory -Path $DistRoot -Force -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $PyInstallerRoot -Force -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $SpecRoot -Force -ErrorAction Stop | Out-Null

    Write-Host "运行 PyInstaller onedir 构建…" -ForegroundColor Cyan
    Invoke-Python -Arguments @(
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        $ApplicationName,
        "--paths",
        (Join-Path $RepoRoot "src"),
        "--distpath",
        $DistRoot,
        "--workpath",
        $PyInstallerRoot,
        "--specpath",
        $SpecRoot,
        "--collect-all",
        "tzdata",
        "--collect-all",
        "keyring",
        # Selenium 通过延迟导入加载浏览器实现，PyInstaller 无法只靠静态分析发现。
        "--collect-submodules",
        "selenium.webdriver",
        "--hidden-import",
        "keyring.backends.Windows",
        $EntryPoint
    )

    $ApplicationRoot = Join-Path $DistRoot $ApplicationName
    $ApplicationExecutable = Join-Path $ApplicationRoot "$ApplicationName.exe"
    if (-not (Test-Path -LiteralPath $ApplicationExecutable -PathType Leaf)) {
        throw "PyInstaller 没有生成预期程序：$ApplicationExecutable"
    }

    $BundledBrowserRoot = Join-Path $ApplicationRoot "browser"
    New-Item -ItemType Directory -Path $BundledBrowserRoot -Force -ErrorAction Stop | Out-Null
    Copy-Item `
        -Path (Join-Path $BrowserRoot "*") `
        -Destination $BundledBrowserRoot `
        -Recurse `
        -Force `
        -ErrorAction Stop

    $BundledChrome = Join-Path $BundledBrowserRoot ([string]$Manifest.artifacts.chrome.executable)
    $BundledDriver = Join-Path $BundledBrowserRoot ([string]$Manifest.artifacts.chromedriver.executable)
    if (-not (Test-Path -LiteralPath $BundledChrome -PathType Leaf)) {
        throw "应用目录缺少内置 Chrome：$BundledChrome"
    }
    if (-not (Test-Path -LiteralPath $BundledDriver -PathType Leaf)) {
        throw "应用目录缺少内置 ChromeDriver：$BundledDriver"
    }

    $ExecutableHash = (Get-FileHash -LiteralPath $ApplicationExecutable -Algorithm SHA256).Hash
    Write-Host ""
    Write-Host "构建成功" -ForegroundColor Green
    Write-Host "应用目录：$ApplicationRoot"
    Write-Host "可执行文件：$ApplicationExecutable"
    Write-Host "EXE SHA-256：$ExecutableHash"
    Write-Host "浏览器版本：$($Manifest.version)"
}
finally {
    Pop-Location
}
