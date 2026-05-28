"""
folder_watcher.py 통합 동작 테스트
- ADLSUploader 는 mock 으로 교체 (Azure 호출 없이)
- WATCH_FOLDER 에 파일을 실제로 쓰면서 watchdog 동작 확인
- debounce / stable / lock 가드, archive 이동, 0-byte 거절, 확장자 필터 검증
"""
import os
import sys
import time
import shutil
import threading
from pathlib import Path

# 테스트용 폴더
WATCH = Path(r"S:\temp\watcher-test\watch")
ARCHIVE = Path(r"S:\temp\watcher-test\archive")
if WATCH.exists():
    shutil.rmtree(WATCH, ignore_errors=True)
if ARCHIVE.exists():
    shutil.rmtree(ARCHIVE, ignore_errors=True)
WATCH.mkdir(parents=True, exist_ok=True)
ARCHIVE.mkdir(parents=True, exist_ok=True)

os.environ["WATCH_FOLDER"] = str(WATCH)
os.environ["ARCHIVE_FOLDER"] = str(ARCHIVE)
os.environ["ALLOWED_EXTENSIONS"] = ".zip"
os.environ["ADLS_DFS_URI"] = "https://mskrblobv2.dfs.core.windows.net/"
os.environ["ADLS_CONTAINER"] = "watcher"
os.environ["DEBOUNCE_SECONDS"] = "1.5"
os.environ["STABLE_CHECK_INTERVAL"] = "0.3"
os.environ["STABLE_CHECK_RETRIES"] = "4"
os.environ["USE_EXCLUSIVE_LOCK_CHECK"] = "true"
os.environ["LOCK_CHECK_INTERVAL"] = "0.3"
os.environ["LOCK_CHECK_RETRIES"] = "6"

sys.path.insert(0, str(Path(__file__).parent))

import folder_watcher as fw


# ----------------------------------------------------------------------------
# Mock uploader
# ----------------------------------------------------------------------------
class MockUploader:
    def __init__(self):
        self.uploaded: list[tuple[str, int]] = []
        self.fail_next = False

    def upload(self, local_path: Path) -> str:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated upload failure")
        size = local_path.stat().st_size
        self.uploaded.append((local_path.name, size))
        print(f"  [MOCK] upload OK: {local_path.name} ({size} bytes)")
        return f"mock/{local_path.name}"


# ----------------------------------------------------------------------------
# Boot watcher
# ----------------------------------------------------------------------------
settings = fw.Settings()
uploader = MockUploader()
handler = fw.WatcherHandler(settings, uploader)  # type: ignore[arg-type]

from watchdog.observers import Observer
observer = Observer()
observer.schedule(handler, str(WATCH), recursive=False)
observer.start()

results = {"pass": 0, "fail": 0}


def assert_eq(name, actual, expected):
    if actual == expected:
        print(f"✓ {name}")
        results["pass"] += 1
    else:
        print(f"✗ {name}  expected={expected!r}  actual={actual!r}")
        results["fail"] += 1


def wait_for(predicate, timeout=15.0, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ----------------------------------------------------------------------------
# Test 1: 정상 .zip 업로드 + archive 이동
# ----------------------------------------------------------------------------
print("\n[TEST 1] normal .zip upload + archive")
f1 = WATCH / "sample1.zip"
with open(f1, "wb") as fh:
    fh.write(b"X" * (256 * 1024))   # 256KB

ok = wait_for(lambda: any(n == "sample1.zip" for n, _ in uploader.uploaded), timeout=20)
assert_eq("uploaded sample1.zip", ok, True)
assert_eq("original removed from watch", f1.exists(), False)

# archive 확인
from datetime import datetime
today = datetime.now().strftime("%Y%m%d")
archived = ARCHIVE / today / "sample1.zip"
ok2 = wait_for(lambda: archived.exists(), timeout=5)
assert_eq("archive file exists", ok2, True)


# ----------------------------------------------------------------------------
# Test 2: 확장자 미허용 (.txt) 은 무시
# ----------------------------------------------------------------------------
print("\n[TEST 2] disallowed extension is ignored")
before = len(uploader.uploaded)
f2 = WATCH / "ignore.txt"
f2.write_bytes(b"hello")
time.sleep(3)  # debounce + stable 시간
assert_eq("no upload for .txt", len(uploader.uploaded), before)
assert_eq(".txt still present", f2.exists(), True)


# ----------------------------------------------------------------------------
# Test 3: 0 byte 파일은 stable 체크에서 거절
# ----------------------------------------------------------------------------
print("\n[TEST 3] empty (0-byte) file rejected")
before = len(uploader.uploaded)
f3 = WATCH / "empty.zip"
f3.touch()
time.sleep(5)
assert_eq("no upload for 0-byte zip", len(uploader.uploaded), before)
assert_eq("0-byte file still present (not archived)", f3.exists(), True)


# ----------------------------------------------------------------------------
# Test 4: 슬로우 라이트 (3초간 쓰는 파일) - debounce 가 묶고 stable 통과 후 1회만 업로드
# ----------------------------------------------------------------------------
print("\n[TEST 4] slow write - debounce should coalesce, single upload")
before = len(uploader.uploaded)
f4 = WATCH / "slow.zip"

def slow_write():
    with open(f4, "wb") as fh:
        for i in range(6):
            fh.write(b"Y" * (64 * 1024))
            fh.flush()
            os.fsync(fh.fileno())
            time.sleep(0.5)

t = threading.Thread(target=slow_write, daemon=True)
t.start()
t.join()

ok = wait_for(lambda: any(n == "slow.zip" for n, _ in uploader.uploaded), timeout=25)
assert_eq("slow.zip uploaded exactly once", [n for n, _ in uploader.uploaded].count("slow.zip"), 1)
slow_size = next((sz for n, sz in uploader.uploaded if n == "slow.zip"), -1)
assert_eq("slow.zip uploaded with full size (6*64KB)", slow_size, 6 * 64 * 1024)


# ----------------------------------------------------------------------------
# Test 5: rename into watcher (.tmp → .zip) → on_moved 처리
# ----------------------------------------------------------------------------
print("\n[TEST 5] rename .tmp → .zip handled via on_moved")
tmp = WATCH / "renaming.tmp"
final = WATCH / "renaming.zip"
tmp.write_bytes(b"Z" * 50_000)
time.sleep(2)   # .tmp 는 무시 - debounce 도 안 잡힘
tmp.rename(final)

ok = wait_for(lambda: any(n == "renaming.zip" for n, _ in uploader.uploaded), timeout=20)
assert_eq("renaming.zip uploaded", ok, True)


# ----------------------------------------------------------------------------
# Shutdown
# ----------------------------------------------------------------------------
observer.stop()
observer.join()

print("\n=========================================")
print(f"PASSED: {results['pass']}   FAILED: {results['fail']}")
print(f"Uploaded files: {uploader.uploaded}")
print("=========================================")
sys.exit(0 if results["fail"] == 0 else 1)
