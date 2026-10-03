import os
import time
import threading
from pathlib import Path

HF_TOKEN = os.environ.get("HF_TOKEN")
BUCKET_REPO = os.environ.get("BUCKET_REPO", "your-username/sig-infinity-backup")
REPO_TYPE = "dataset"

LOCAL_DATA_DIR = Path("/app")
SESSION_FILE = LOCAL_DATA_DIR / "session.json"
DB_FILE = LOCAL_DATA_DIR / "sig_infinity.db"


def restore_from_bucket():
    print("[Sync] Attempting to restore data from bucket...")
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=BUCKET_REPO,
            repo_type=REPO_TYPE,
            local_dir="/app",
            token=HF_TOKEN,
            local_dir_use_symlinks=False,
        )
        print("[Sync] Successfully restored data.")
    except Exception as e:
        print(f"[Sync] Could not restore data (normal on first run): {e}")


def sync_to_bucket():
    try:
        from huggingface_hub import HfApi
        api = HfApi()
        for file_path in [SESSION_FILE, DB_FILE]:
            if file_path.exists():
                print(f"[Sync] Uploading {file_path.name}...")
                api.upload_file(
                    path_or_fileobj=str(file_path),
                    path_in_repo=file_path.name,
                    repo_id=BUCKET_REPO,
                    repo_type=REPO_TYPE,
                    token=HF_TOKEN,
                )
    except Exception as e:
        print(f"[Sync] Error during sync: {e}")


def main_loop():
    LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    restore_from_bucket()
    while True:
        time.sleep(300)
        print("[Sync] Running periodic sync...")
        sync_to_bucket()


if __name__ == "__main__":
    sync_thread = threading.Thread(target=main_loop, daemon=True)
    sync_thread.start()
    print("[Sync] Daemon started in background.")
    while True:
        time.sleep(60)