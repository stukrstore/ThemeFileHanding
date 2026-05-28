"""
ADLS Gen2 SDK Upload Sample (Windows, Managed Identity)
=======================================================

지정한 로컬 파일을 Azure Data Lake Storage Gen2 로 업로드하는 최소 예제입니다.
인증은 `DefaultAzureCredential` 로 자동 선택됩니다:

- Azure VM            → IMDS (System-Assigned Managed Identity)
- Azure Arc Windows VM → himds IMDS (System-Assigned Managed Identity)
- 개발자 PC           → `az login` 자격

업로드 경로 규칙:
    {ADLS_TARGET_PREFIX}/yyyy/MM/dd/<파일명>

사용 예 (PowerShell):
    workon Watcher
    pip install -r requirements.txt
    Copy-Item .env.example .env
    notepad .env
    python upload_to_adls.py "S:\\data\\sample.zip"
    python upload_to_adls.py "S:\\data\\a.zip" "S:\\data\\b.csv"
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# 로깅
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("adls-upload")
# Azure SDK INFO 로그 잡음 제거
for noisy in ("azure", "urllib3", "msal"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------
load_dotenv()

ADLS_DFS_URI = os.getenv(
    "ADLS_DFS_URI", "https://mskrblobv2.dfs.core.windows.net/"
).strip()
ADLS_CONTAINER = os.getenv("ADLS_CONTAINER", "watcher").strip()
ADLS_TARGET_PREFIX = os.getenv("ADLS_TARGET_PREFIX", "").strip().strip("/")

if "dfs.core.windows.net" not in ADLS_DFS_URI:
    raise ValueError(
        f"ADLS_DFS_URI 가 DFS endpoint 가 아닙니다: {ADLS_DFS_URI!r} "
        "(예: https://<account>.dfs.core.windows.net/)"
    )


# ----------------------------------------------------------------------------
# Uploader
# ----------------------------------------------------------------------------
class ADLSUploader:
    def __init__(self, dfs_uri: str, container: str, target_prefix: str = "") -> None:
        self.container = container
        self.target_prefix = target_prefix.strip("/")
        self.credential = DefaultAzureCredential()
        self.service_client = DataLakeServiceClient(
            account_url=dfs_uri, credential=self.credential
        )
        self._ensure_filesystem()

    def _ensure_filesystem(self) -> None:
        fs = self.service_client.get_file_system_client(self.container)
        try:
            fs.create_file_system()
            log.info("컨테이너 생성: %s", self.container)
        except ResourceExistsError:
            pass  # 이미 존재

    def _remote_path(self, filename: str) -> str:
        now = datetime.now()
        date_path = now.strftime("%Y/%m/%d")
        parts = [p for p in (self.target_prefix, date_path, filename) if p]
        return "/".join(parts)

    def upload(self, local_path: Path) -> str:
        if not local_path.is_file():
            raise FileNotFoundError(local_path)

        remote = self._remote_path(local_path.name)
        size_mb = local_path.stat().st_size / 1024 / 1024
        log.info("⇪ 업로드: %s (%.2f MB) → %s/%s",
                 local_path.name, size_mb, self.container, remote)

        fs = self.service_client.get_file_system_client(self.container)
        file_client = fs.get_file_client(remote)
        with local_path.open("rb") as f:
            file_client.upload_data(f, overwrite=True)

        log.info("✓ 업로드 완료: %s/%s", self.container, remote)
        return remote


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("Usage: python upload_to_adls.py <file1> [file2 ...]")
        return 2

    uploader = ADLSUploader(
        dfs_uri=ADLS_DFS_URI,
        container=ADLS_CONTAINER,
        target_prefix=ADLS_TARGET_PREFIX,
    )

    rc = 0
    for arg in argv[1:]:
        try:
            uploader.upload(Path(arg))
        except Exception as e:
            log.exception("업로드 실패: %s (%s)", arg, e)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
