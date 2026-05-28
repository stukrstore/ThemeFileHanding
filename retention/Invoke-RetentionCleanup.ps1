<#
.SYNOPSIS
    지정한 폴더에서 retention 기간(일)이 지난 파일을 삭제합니다.

.DESCRIPTION
    -Path 로 주어진 폴더(또는 여러 폴더) 내부에서 LastWriteTime 이
    -RetentionDays 일보다 오래된 파일을 찾아 삭제합니다.
    -Recurse 옵션으로 하위 폴더까지, -IncludeFilter 로 확장자 패턴 지정,
    -RemoveEmptyDirs 로 비어버린 하위 폴더까지 정리할 수 있습니다.

    실수를 막기 위해 기본 동작은 -WhatIf 모드입니다(-Apply 가 없으면 삭제하지 않음).
    실행 결과는 -LogPath 에 누적 기록됩니다.

.PARAMETER Path
    대상 폴더 경로 (여러 개 지정 가능). 예: 'S:\Watcher\ARCHIVED'

.PARAMETER RetentionDays
    이 일수보다 오래된 파일을 삭제합니다. 예: 30

.PARAMETER Recurse
    하위 폴더까지 재귀적으로 탐색.

.PARAMETER IncludeFilter
    파일 이름 필터(여러 개 가능). 기본값 '*'. 예: '*.zip','*.log'

.PARAMETER RemoveEmptyDirs
    파일 삭제 후 비어 있는 하위 디렉터리도 함께 삭제.

.PARAMETER Apply
    실제 삭제를 수행. 지정하지 않으면 -WhatIf 처럼 dry-run.

.PARAMETER LogPath
    로그 파일 경로. 기본값: 스크립트 폴더\retention.log

.EXAMPLE
    # Dry-run (삭제 대상만 출력)
    .\Invoke-RetentionCleanup.ps1 -Path 'S:\Watcher\ARCHIVED' -RetentionDays 30 -Recurse

.EXAMPLE
    # 실제 삭제 + 빈 폴더 정리 + .zip 만
    .\Invoke-RetentionCleanup.ps1 `
        -Path 'S:\Watcher\ARCHIVED' `
        -RetentionDays 30 `
        -Recurse `
        -IncludeFilter '*.zip' `
        -RemoveEmptyDirs `
        -Apply
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]] $Path,

    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 36500)]
    [int] $RetentionDays,

    [switch] $Recurse,

    [string[]] $IncludeFilter = @('*'),

    [switch] $RemoveEmptyDirs,

    [switch] $Apply,

    [string] $LogPath
)

$ErrorActionPreference = 'Continue'
if (-not $LogPath) {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
    $LogPath = Join-Path $scriptDir 'retention.log'
}
$cutoff = (Get-Date).AddDays(-1 * $RetentionDays)

function Write-Log {
    param([string] $Level, [string] $Message)
    $line = "{0} | {1,-7} | {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LogPath -Value $line -Encoding utf8
    } catch {
        Write-Warning "로그 기록 실패: $($_.Exception.Message)"
    }
}

Write-Log 'INFO' ('=' * 70)
Write-Log 'INFO' "Retention cleanup 시작"
Write-Log 'INFO' ("  RetentionDays = {0} (cutoff < {1:yyyy-MM-dd HH:mm:ss})" -f $RetentionDays, $cutoff)
Write-Log 'INFO' ("  Recurse       = {0}" -f $Recurse.IsPresent)
Write-Log 'INFO' ("  IncludeFilter = {0}" -f ($IncludeFilter -join ', '))
Write-Log 'INFO' ("  RemoveEmpty   = {0}" -f $RemoveEmptyDirs.IsPresent)
Write-Log 'INFO' ("  Apply (실제 삭제) = {0}" -f $Apply.IsPresent)
Write-Log 'INFO' ('=' * 70)

$totalScanned = 0
$totalDeleted = 0
$totalBytes   = [int64] 0
$totalErrors  = 0

foreach ($p in $Path) {
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Log 'WARN' "경로 없음: $p (skip)"
        continue
    }
    Write-Log 'INFO' "🗂  대상 경로: $p"

    $gciArgs = @{
        LiteralPath = $p
        File        = $true
        Include     = $IncludeFilter
        ErrorAction = 'SilentlyContinue'
    }
    if ($Recurse) { $gciArgs['Recurse'] = $true }

    # Include 는 Recurse 또는 와일드카드 경로일 때만 동작 → 비-Recurse 시 -Filter 대체
    $files = if ($Recurse) {
        Get-ChildItem @gciArgs
    } else {
        Get-ChildItem -LiteralPath $p -File -ErrorAction SilentlyContinue | Where-Object {
            $name = $_.Name
            ($IncludeFilter | ForEach-Object { $name -like $_ }) -contains $true
        }
    }

    foreach ($f in $files) {
        $totalScanned++
        if ($f.LastWriteTime -lt $cutoff) {
            $sizeMb = [Math]::Round($f.Length / 1MB, 2)
            if ($Apply) {
                try {
                    Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop
                    $totalDeleted++
                    $totalBytes += $f.Length
                    Write-Log 'DEL'  ("{0} ({1} MB, LastWrite={2:yyyy-MM-dd})" -f $f.FullName, $sizeMb, $f.LastWriteTime)
                } catch {
                    $totalErrors++
                    Write-Log 'ERROR' ("삭제 실패: {0} ({1})" -f $f.FullName, $_.Exception.Message)
                }
            } else {
                Write-Log 'DRY'  ("[WhatIf] {0} ({1} MB, LastWrite={2:yyyy-MM-dd})" -f $f.FullName, $sizeMb, $f.LastWriteTime)
            }
        }
    }

    if ($Apply -and $RemoveEmptyDirs -and $Recurse) {
        # 깊은 폴더부터 비어 있으면 삭제
        Get-ChildItem -LiteralPath $p -Directory -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object {
                if (-not (Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue)) {
                    try {
                        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
                        Write-Log 'RMDIR' $_.FullName
                    } catch {
                        Write-Log 'ERROR' ("폴더 삭제 실패: {0} ({1})" -f $_.FullName, $_.Exception.Message)
                    }
                }
            }
    }
}

$totalMb = [Math]::Round($totalBytes / 1MB, 2)
Write-Log 'INFO' ('-' * 70)
Write-Log 'INFO' ("스캔: {0}, 삭제: {1} ({2} MB), 에러: {3}" -f $totalScanned, $totalDeleted, $totalMb, $totalErrors)
Write-Log 'INFO' "Retention cleanup 종료"
Write-Log 'INFO' ('=' * 70)

if ($totalErrors -gt 0) { exit 1 } else { exit 0 }
