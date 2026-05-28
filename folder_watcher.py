"""
Folder Watcher → ADLS Gen2 Uploader (Managed Identity)
======================================================
Arc-connected 온프레미스 VM 또는 Azure VM 에서 동작.

동작 흐름:
  1) .env 에 지정된 로컬 폴더를 감시 (watchdog)
  2) 허용 확장자(.zip 등) 파일이 생성/이동되어 들어오면
  3) Azure Arc / Azure VM 의 System-Assigned Managed Identity 로
     ADLS Gen2 (DFS endpoint) 로 업로드
  4) 업로드 성공 시 ARCHIVED/{YYYYMMDD}/ 폴더로 원본 파일 이동

인증:
  - DefaultAzureCredential() 사용
  - Azure Arc VM      → IMDS (Hybrid) 자동 사용
  - Azure VM          → IMDS (System-Assigned MI) 자동 사용
  - 로컬 개발          → AZ CLI 로그인 fallback
"""

import os
import sys
import time
import shutil
import logging
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient
from azure.core.exceptions import ResourceExistsError, AzureError


# ----------------------------------------------------------------------------
# 로깅
# ----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("folder-watcher")

# Azure SDK / urllib3 의 잡음 제거 (INFO·DEBUG 로그 숨김)
for noisy in (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.storage",
    "urllib3",
    "msal",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ----------------------------------------------------------------------------
# 설정 로드
# ----------------------------------------------------------------------------
class Settings:
    def __init__(self) -> None:
        load_dotenv()

        self.watch_folder: Path = Path(
            os.getenv("WATCH_FOLDER", "./watch")
        ).expanduser().resolve()

        # 예: ".zip,.csv,.json"  →  {".zip", ".csv", ".json"}
        raw_ext = os.getenv("ALLOWED_EXTENSIONS", ".zip")
        self.allowed_extensions = {
            e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
            for e in raw_ext.split(",")
            if e.strip()
        }
        if not self.allowed_extensions:
            raise ValueError("ALLOWED_EXTENSIONS 가 비어있습니다. 예: '.zip,.csv'")

        # ADLS Gen2 DFS endpoint, 예: https://mskrblobv2.dfs.core.windows.net/
        self.adls_dfs_uri: str = os.getenv(
            "ADLS_DFS_URI", "https://mskrblobv2.dfs.core.windows.net/"
        ).rstrip("/") + "/"
        if "dfs.core.windows.net" not in self.adls_dfs_uri:
            raise ValueError(
                f"ADLS_DFS_URI 가 ADLS Gen2 DFS endpoint 이 아닙니다: {self.adls_dfs_uri}"
            )

        self.container: str = os.getenv("ADLS_CONTAINER", "watcher")

        # ADLS 내 업로드 대상 prefix (선택)
        self.adls_target_prefix: str = os.getenv("ADLS_TARGET_PREFIX", "").strip("/")

        # 업로드 후 이동시킬 archive 루트 폴더
        self.archive_folder: Path = Path(
            os.getenv("ARCHIVE_FOLDER", str(self.watch_folder / "ARCHIVED"))
        ).expanduser().resolve()

        # 파일 쓰기가 끝났는지 확인하기 위해 stable 검사 시 대기 시간(초)
        self.stable_check_interval: float = float(
            os.getenv("STABLE_CHECK_INTERVAL", "1.0")
        )
        self.stable_check_retries: int = int(
            os.getenv("STABLE_CHECK_RETRIES", "5")
        )

        # 디바운스: 마지막 이벤트 이후 N초 점적이면 처리
        self.debounce_seconds: float = float(
            os.getenv("DEBOUNCE_SECONDS", "2.0")
        )

        # 배타적 열기 체크 (Windows 권장)
        self.use_exclusive_lock_check: bool = (
            os.getenv("USE_EXCLUSIVE_LOCK_CHECK", "true").strip().lower()
            in ("1", "true", "yes", "on")
        )
        self.lock_check_retries: int = int(
            os.getenv("LOCK_CHECK_RETRIES", "10")
        )
        self.lock_check_interval: float = float(
            os.getenv("LOCK_CHECK_INTERVAL", "1.0")
        )

        # 폴더가 없으면 생성
        self.watch_folder.mkdir(parents=True, exist_ok=True)
        self.archive_folder.mkdir(parents=True, exist_ok=True)

    def dump(self) -> None:
        log.info("WATCH_FOLDER          = %s", self.watch_folder)
        log.info("ALLOWED_EXTENSIONS    = %s", sorted(self.allowed_extensions))
        log.info("ADLS_DFS_URI          = %s", self.adls_dfs_uri)
        log.info("ADLS_CONTAINER        = %s", self.container)
        log.info("ADLS_TARGET_PREFIX    = %s", self.adls_target_prefix or "(root)")
        log.info("ARCHIVE_FOLDER        = %s", self.archive_folder)
        log.info("DEBOUNCE_SECONDS      = %.2f", self.debounce_seconds)
        log.info("EXCLUSIVE_LOCK_CHECK  = %s", self.use_exclusive_lock_check)


# ----------------------------------------------------------------------------
# ADLS Gen2 업로더
# ----------------------------------------------------------------------------
class ADLSUploader:
    """Managed Identity (System-Assigned) 기반 ADLS Gen2 업로더."""

    def __init__(self, dfs_uri: str, container: str, target_prefix: str = "") -> None:
        self.dfs_uri = dfs_uri
        self.container = container
        self.target_prefix = target_prefix

        # DefaultAzureCredential:
        #   - Arc VM    → Hybrid IMDS (System-Assigned MI)
        #   - Azure VM  → IMDS (System-Assigned MI)
        #   - 개발자 PC → AZ CLI / VS Code 로그인
        self.credential = DefaultAzureCredential()
        self.service_client = DataLakeServiceClient(
            account_url=dfs_uri, credential=self.credential
        )

        self._ensure_filesystem()

    def _ensure_filesystem(self) -> None:
        fs_client = self.service_client.get_file_system_client(self.container)
        try:
            fs_client.create_file_system()
            log.info("ADLS filesystem '%s' 생성됨", self.container)
        except ResourceExistsError:
            log.debug("ADLS filesystem '%s' 이미 존재", self.container)

    def _remote_path(self, file_name: str) -> str:
        # yyyy/MM/dd/filename 으로 자동 partitioning
        today = datetime.now().strftime("%Y/%m/%d")
        parts = [p for p in [self.target_prefix, today, file_name] if p]
        return "/".join(parts)

    def upload(self, local_path: Path) -> str:
        remote_path = self._remote_path(local_path.name)
        fs_client = self.service_client.get_file_system_client(self.container)
        file_client = fs_client.get_file_client(remote_path)

        size = local_path.stat().st_size
        log.info("⇪ 업로드 시작: %s (%.2f MB) → %s/%s",
                 local_path.name, size / (1024 * 1024), self.container, remote_path)

        with open(local_path, "rb") as f:
            file_client.upload_data(f, overwrite=True)

        log.info("✓ 업로드 완료: %s", remote_path)
        return remote_path


# ----------------------------------------------------------------------------
# 파일 시스템 이벤트 핸들러
# ----------------------------------------------------------------------------
class WatcherHandler(FileSystemEventHandler):
    def __init__(self, settings: Settings, uploader: ADLSUploader) -> None:
        super().__init__()
        self.settings = settings
        self.uploader = uploader
        self._in_progress: set[str] = set()
        self._lock = threading.Lock()
        # debounce: path → Timer
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._debounce_lock = threading.Lock()

    # 신규 파일 생성
    def on_created(self, event):
        if event.is_directory:
            return
        self._schedule(Path(event.src_path))

    # 파일 내용 변경 (큐 업데이트·쪼개서 쓰기 등)
    def on_modified(self, event):
        if event.is_directory:
            return
        self._schedule(Path(event.src_path))

    # 외부에서 mv 로 들어오는 경우
    def on_moved(self, event):
        if event.is_directory:
            return
        # 감시 폴더 내부에서 rename 된 경우,
        # src 가 이미 처리 중이면 중복 처리 구혈
        src = Path(event.src_path).resolve()
        with self._lock:
            if str(src) in self._in_progress:
                log.debug("rename src 가 이미 처리 중이므로 스킵: %s", src)
                return
        self._schedule(Path(event.dest_path))

    # ------------------------------------------------------------------
    # 디바운스: 마지막 이벤트 이후 N초 동안 조용해야 _handle 실행
    # ------------------------------------------------------------------
    def _schedule(self, path: Path) -> None:
        # archive 폴더 이벤트는 아예 무시
        try:
            path.resolve().relative_to(self.settings.archive_folder)
            return
        except ValueError:
            pass

        ext = path.suffix.lower()
        if ext not in self.settings.allowed_extensions:
            return

        key = str(path.resolve())
        with self._debounce_lock:
            existing = self._debounce_timers.pop(key, None)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(
                self.settings.debounce_seconds,
                self._debounced_handle,
                args=(path, key),
            )
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()

    def _debounced_handle(self, path: Path, key: str) -> None:
        with self._debounce_lock:
            self._debounce_timers.pop(key, None)
        self._handle(path)

    # ------------------------------------------------------------------
    def _handle(self, path: Path) -> None:
        if not path.exists():
            log.debug("디바운스 중 사라짐: %s", path)
            return

        ext = path.suffix.lower()
        if ext not in self.settings.allowed_extensions:
            log.debug("스킵 (확장자 미허용): %s", path.name)
            return

        key = str(path.resolve())
        with self._lock:
            if key in self._in_progress:
                return
            self._in_progress.add(key)

        # 별도 스레드에서 처리 (Observer 스레드 블로킹 방지)
        threading.Thread(
            target=self._process, args=(path,), daemon=True
        ).start()

    def _process(self, path: Path) -> None:
        try:
            if not self._wait_until_stable(path):
                log.warning("파일 크기가 안정되지 않아 스킵: %s", path)
                return

            if self.settings.use_exclusive_lock_check and not self._wait_until_unlocked(path):
                log.warning("파일이 잘김을 해제하지 않아 스킵: %s", path)
                return

            # 업로드 직전 최종 존재 확인 (검사·잠금 체크 사이에 삭제될 수 있음)
            if not path.exists():
                log.warning("파일이 업로드 직전에 사라졌음: %s", path)
                return

            self.uploader.upload(path)
            self._archive(path)

        except AzureError as e:
            log.error("Azure 오류: %s | %s", path.name, e)
        except Exception as e:
            log.exception("처리 실패: %s | %s", path.name, e)
        finally:
            with self._lock:
                self._in_progress.discard(str(path.resolve()))

    def _wait_until_stable(self, path: Path) -> bool:
        """파일 쓰기가 끝났는지 size 변화로 확인. 빈 파일(0 byte) 은 거절."""
        last_size = -1
        for _ in range(self.settings.stable_check_retries):
            if not path.exists():
                return False
            size = path.stat().st_size
            if size == last_size and size > 0:
                return True
            last_size = size
            time.sleep(self.settings.stable_check_interval)
        # 마지막 한 번 더 체크 — 0 byte 는 거절
        if not path.exists():
            return False
        final_size = path.stat().st_size
        return final_size > 0 and final_size == last_size

    def _wait_until_unlocked(self, path: Path) -> bool:
        """배타적 열기 시도 — Windows 에서 쓰기 중인 파일은 PermissionError 발생."""
        for _ in range(self.settings.lock_check_retries):
            if not path.exists():
                return False
            try:
                # 'rb+' 는 공유 없이 열기 시도→ writer 가 lock 잡고 있으면 실패
                with open(path, "rb+"):
                    return True
            except (PermissionError, OSError) as e:
                log.debug("lock held: %s (%s)", path.name, e)
                time.sleep(self.settings.lock_check_interval)
        return False

    def _archive(self, path: Path) -> None:
        date_folder = datetime.now().strftime("%Y%m%d")
        target_dir = self.settings.archive_folder / date_folder
        target_dir.mkdir(parents=True, exist_ok=True)

        dest = target_dir / path.name
        # 이름 충돌 시 _1, _2 ...
        i = 1
        while dest.exists():
            dest = target_dir / f"{path.stem}_{i}{path.suffix}"
            i += 1

        shutil.move(str(path), str(dest))
        log.info("📦 ARCHIVED: %s → %s", path.name, dest)


# ----------------------------------------------------------------------------
# 시작 시 기존 파일도 처리
# ----------------------------------------------------------------------------
def process_existing(settings: Settings, handler: WatcherHandler) -> None:
    for p in settings.watch_folder.iterdir():
        if p.is_file() and p.suffix.lower() in settings.allowed_extensions:
            log.info("기존 파일 발견: %s", p.name)
            handler._handle(p)

# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main() -> int:
    settings = Settings()
    settings.dump()

    if not Path(".env").is_file():
        log.warning(".env 파일이 없어 기본값으로 시작합니다. .env.example 을 복사하여 '.env' 로 편집하세요.")

    try:
        uploader = ADLSUploader(
            dfs_uri=settings.adls_dfs_uri,
            container=settings.container,
            target_prefix=settings.adls_target_prefix,
        )
    except Exception as e:
        log.exception("ADLS 초기화 실패: %s", e)
        return 1

    handler = WatcherHandler(settings, uploader)

    # 기존 파일 우선 처리
    process_existing(settings, handler)

    observer = Observer()
    observer.schedule(handler, str(settings.watch_folder), recursive=False)
    observer.start()
    log.info("=" * 70)
    log.info("👀 폴더 감시 시작: %s", settings.watch_folder)
    log.info("   → 허용 확장자: %s", sorted(settings.allowed_extensions))
    log.info("   → 업로드 대상 : %s%s", settings.container,
             "/" + settings.adls_target_prefix if settings.adls_target_prefix else "")
    log.info("   (Ctrl+C 로 종료)")
    log.info("=" * 70)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("종료 신호 수신, 감시 중단 중...")
    finally:
        observer.stop()
        observer.join()
        log.info("종료 완료")

    return 0


if __name__ == "__main__":
    sys.exit(main())
