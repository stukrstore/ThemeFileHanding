# SDK Sample — ADLS Gen2 단발 업로드 (Windows, Managed Identity)

상위 [Folder Watcher](../README.md) 와 동일한 인증·업로드 로직을 **단발 CLI** 형태로 분리한
최소 예제입니다. CI/CD 단계, 수동 보정, 스케줄러에서 호출하는 용도로 사용할 수 있습니다.

- 인증: `DefaultAzureCredential`
  - Azure VM            → IMDS (System-Assigned MI)
  - Azure Arc Windows VM → himds IMDS (System-Assigned MI)
  - 개발자 PC           → `az login` 자격
- 업로드 경로: `{ADLS_TARGET_PREFIX}/yyyy/MM/dd/<파일명>`
- 대상 컨테이너가 없으면 자동 생성 시도 (계정 스코프 권한 필요)

> RBAC, Managed Identity 활성화 등 사전 준비는 [상위 README](../README.md) 의 2 장을 참고하세요.

---

## 1. 파일

| 파일 | 설명 |
|---|---|
| [`upload_to_adls.py`](./upload_to_adls.py) | ADLS Gen2 업로드 스크립트 (CLI) |
| [`requirements.txt`](./requirements.txt) | 의존 패키지 |
| [`.env.example`](./.env.example) | `.env` 템플릿 |

---

## 2. 설치 & 실행 (PowerShell)

```powershell
cd S:\GitRepos\Python\FolderWatcher\sdk
pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env

# 단일 파일
python upload_to_adls.py "S:\data\sample.zip"

# 여러 파일
python upload_to_adls.py "S:\data\a.zip" "S:\data\b.csv"
```

---

## 3. 환경 변수 (`.env`)

| 변수 | 설명 | 예시 |
|---|---|---|
| `ADLS_DFS_URI` | ADLS Gen2 DFS endpoint | `https://mskrblobv2.dfs.core.windows.net/` |
| `ADLS_CONTAINER` | 컨테이너(filesystem) 이름 | `watcher` |
| `ADLS_TARGET_PREFIX` | (선택) 컨테이너 내부 prefix | `inbox` (또는 비움) |

---

## 4. 코드 요약

```python
from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient

cred = DefaultAzureCredential()
svc  = DataLakeServiceClient(account_url=ADLS_DFS_URI, credential=cred)
fs   = svc.get_file_system_client(ADLS_CONTAINER)
file = fs.get_file_client(f"{prefix}/yyyy/MM/dd/{name}")

with open(local_path, "rb") as f:
    file.upload_data(f, overwrite=True)
```

핵심:
- `DataLakeServiceClient` 는 **DFS endpoint** (`*.dfs.core.windows.net`) 를 받아야 합니다 (Blob endpoint 아님).
- 컨테이너가 없으면 `create_file_system()` 으로 생성을 시도하고, 이미 있으면 `ResourceExistsError` 를 무시합니다.
- `upload_data(overwrite=True)` 는 큰 파일을 자동으로 청크 업로드합니다 (스트림 입력).

---

## 5. 종료 코드

| 코드 | 의미 |
|---|---|
| `0` | 모든 파일 업로드 성공 |
| `1` | 1개 이상 실패 |
| `2` | 인자 누락 (usage) |

---

## 6. 트러블슈팅

| 증상 | 해결 |
|---|---|
| `ADLS_DFS_URI 가 DFS endpoint 가 아닙니다` | `*.dfs.core.windows.net` URI 인지 확인 |
| `AuthorizationFailure` / 403 | MI 에 `Storage Blob Data Contributor` 권한 부여 (상위 README 2.3 절) |
| `DefaultAzureCredential failed` | VM MI 미할당 또는 개발자 PC 에서 `az login` 안 됨 |
| 업로드는 되지만 컨테이너 자동 생성 실패 | 컨테이너를 미리 생성하거나 계정 스코프 권한 부여 |
