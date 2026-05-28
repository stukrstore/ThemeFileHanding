# Retention Cleanup — Windows Task Scheduler 용 PowerShell 스크립트

지정한 폴더에서 **retention 기간(일)** 이 지난 파일을 정리하는 스크립트입니다.
Windows **작업 스케줄러(Task Scheduler)** 에 등록해 정기 실행하도록 설계되었습니다.

상위 [Folder Watcher](../README.md) 의 `ARCHIVED/` 폴더 정리에 그대로 사용할 수 있습니다.

---

## 1. 파일

| 파일 | 설명 |
|---|---|
| [`Invoke-RetentionCleanup.ps1`](./Invoke-RetentionCleanup.ps1) | retention 삭제 본체 |
| `retention.log` | 실행 시 자동 생성되는 로그 (스크립트 폴더 기본) |

---

## 2. 사용법 (PowerShell)

```powershell
# Dry-run (실제 삭제 안 함, 대상만 표시)
.\Invoke-RetentionCleanup.ps1 `
    -Path 'S:\Watcher\ARCHIVED' `
    -RetentionDays 30 `
    -Recurse

# 실제 삭제 + 빈 폴더 정리 + .zip 만
.\Invoke-RetentionCleanup.ps1 `
    -Path 'S:\Watcher\ARCHIVED' `
    -RetentionDays 30 `
    -Recurse `
    -IncludeFilter '*.zip' `
    -RemoveEmptyDirs `
    -Apply

# 여러 경로 동시 정리
.\Invoke-RetentionCleanup.ps1 `
    -Path 'S:\Watcher\ARCHIVED','D:\Logs' `
    -RetentionDays 14 `
    -Recurse `
    -Apply
```

> **안전장치**: `-Apply` 를 주지 않으면 항상 dry-run 입니다.
> 운영 등록 전에 반드시 dry-run 으로 대상 목록을 확인하세요.

### 파라미터

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `-Path` | ✅ | 대상 폴더 (1개 이상). 예: `'S:\Watcher\ARCHIVED'` |
| `-RetentionDays` | ✅ | 이 일수보다 오래된 파일 삭제 (0 ~ 36500) |
| `-Recurse` |  | 하위 폴더까지 재귀 |
| `-IncludeFilter` |  | 파일명 패턴(여러 개). 기본 `*`. 예: `'*.zip','*.log'` |
| `-RemoveEmptyDirs` |  | 삭제 후 빈 하위 폴더 정리 (`-Recurse` 와 함께) |
| `-Apply` |  | **실제 삭제 수행** (없으면 dry-run) |
| `-LogPath` |  | 로그 파일 경로. 기본 `<스크립트폴더>\retention.log` |

### 종료 코드

| 코드 | 의미 |
|---|---|
| `0` | 정상 종료 (에러 없음, dry-run 포함) |
| `1` | 1개 이상 삭제 실패 발생 |

---

## 3. Windows 작업 스케줄러 등록

### 3.1 GUI (taskschd.msc)

1. `Win + R` → `taskschd.msc` 실행
2. **작업 만들기(Create Task)** 클릭
3. **일반(General)** 탭
   - 이름: `FolderWatcher Retention Cleanup`
   - **사용자가 로그온하지 않았을 때도 실행** 선택
   - **가장 높은 수준의 권한으로 실행** 체크
4. **트리거(Triggers)** → **새로 만들기**
   - 매일 / 오전 03:00 / 1일마다 (예시)
5. **동작(Actions)** → **새로 만들기**
   - 동작: 프로그램 시작
   - 프로그램/스크립트:
     ```
     powershell.exe
     ```
   - 인수 추가:
     ```
     -NoProfile -ExecutionPolicy Bypass -File "S:\GitRepos\Python\FolderWatcher\retention\Invoke-RetentionCleanup.ps1" -Path "S:\Watcher\ARCHIVED" -RetentionDays 30 -Recurse -RemoveEmptyDirs -Apply
     ```
   - 시작 위치(선택):
     ```
     S:\GitRepos\Python\FolderWatcher\retention
     ```
6. **조건(Conditions)** 탭에서 *AC 전원 필요* 가 부담스러우면 체크 해제
7. **설정(Settings)** 탭: *작업이 이미 실행 중인 경우 새 인스턴스를 시작하지 않음* 권장

### 3.2 PowerShell 로 일괄 등록 (권장)

관리자 PowerShell 에서 실행:

```powershell
$TaskName = 'FolderWatcher Retention Cleanup'
$Script   = 'S:\GitRepos\Python\FolderWatcher\retention\Invoke-RetentionCleanup.ps1'
$Args     = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" " +
            "-Path `"S:\Watcher\ARCHIVED`" " +
            "-RetentionDays 30 " +
            "-Recurse -RemoveEmptyDirs -Apply"

$Action    = New-ScheduledTaskAction `
                -Execute 'powershell.exe' `
                -Argument $Args `
                -WorkingDirectory (Split-Path $Script)

$Trigger   = New-ScheduledTaskTrigger -Daily -At 3:00am

$Principal = New-ScheduledTaskPrincipal `
                -UserId 'SYSTEM' `
                -LogonType ServiceAccount `
                -RunLevel Highest

$Settings  = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Principal $Principal `
    -Settings  $Settings `
    -Description 'Delete files older than retention days from FolderWatcher ARCHIVED.'
```

> **`SYSTEM`** 계정으로 실행하면 로컬 파일 시스템 접근 권한이 자동으로 보장됩니다.
> 특정 사용자로 실행해야 하는 경우 `-UserId 'DOMAIN\user' -LogonType Password` 로 바꾸고
> `Register-ScheduledTask -User -Password` 옵션을 추가하세요.

### 3.3 작업 확인 / 수동 실행 / 삭제

```powershell
# 등록된 작업 정보
Get-ScheduledTask -TaskName 'FolderWatcher Retention Cleanup' | Format-List *

# 즉시 1회 실행 (테스트)
Start-ScheduledTask -TaskName 'FolderWatcher Retention Cleanup'

# 마지막 실행 결과
Get-ScheduledTaskInfo -TaskName 'FolderWatcher Retention Cleanup'

# 작업 삭제
Unregister-ScheduledTask -TaskName 'FolderWatcher Retention Cleanup' -Confirm:$false
```

---

## 4. 로그 예시

```
2026-05-28 03:00:00 | INFO    | ======================================================================
2026-05-28 03:00:00 | INFO    | Retention cleanup 시작
2026-05-28 03:00:00 | INFO    |   RetentionDays = 30 (cutoff < 2026-04-28 03:00:00)
2026-05-28 03:00:00 | INFO    |   Recurse       = True
2026-05-28 03:00:00 | INFO    |   IncludeFilter = *.zip
2026-05-28 03:00:00 | INFO    |   RemoveEmpty   = True
2026-05-28 03:00:00 | INFO    |   Apply (실제 삭제) = True
2026-05-28 03:00:00 | INFO    | ======================================================================
2026-05-28 03:00:00 | INFO    | 🗂  대상 경로: S:\Watcher\ARCHIVED
2026-05-28 03:00:01 | DEL     | S:\Watcher\ARCHIVED\20260420\a.zip (12.34 MB, LastWrite=2026-04-20)
2026-05-28 03:00:01 | RMDIR   | S:\Watcher\ARCHIVED\20260420
2026-05-28 03:00:01 | INFO    | ----------------------------------------------------------------------
2026-05-28 03:00:01 | INFO    | 스캔: 421, 삭제: 87 (1024.55 MB), 에러: 0
2026-05-28 03:00:01 | INFO    | Retention cleanup 종료
```

---

## 5. 트러블슈팅

| 증상 | 해결 |
|---|---|
| `이 스크립트를 실행할 수 없습니다` (ExecutionPolicy) | 작업 인수에 `-ExecutionPolicy Bypass` 가 포함되어 있는지 확인 |
| 권한 거부로 삭제 실패 | 작업을 `SYSTEM` 또는 해당 폴더 권한 있는 계정으로 실행 |
| Dry-run 처럼만 동작 | `-Apply` 스위치 누락 |
| 빈 폴더가 안 지워짐 | `-Recurse` 와 `-RemoveEmptyDirs` 가 모두 있어야 함 |
| 한글 로그 깨짐 | `powershell.exe` 대신 `pwsh.exe`(PowerShell 7+) 로 실행하거나 콘솔 코드페이지 `chcp 65001` |
