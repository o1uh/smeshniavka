import os
import sys
import time
from pathlib import Path


def main():
    heartbeat_path = Path("data/heartbeat")
    if not heartbeat_path.exists():
        print("Healthcheck failed: data/heartbeat does not exist", file=sys.stderr)
        sys.exit(1)

    mtime = heartbeat_path.stat().st_mtime
    age = time.time() - mtime
    if age > 120.0:
        print(f"Healthcheck failed: heartbeat is stale ({age:.1f}s > 120s)", file=sys.stderr)
        sys.exit(1)

    print(f"Healthcheck OK: heartbeat age is {age:.1f}s")
    sys.exit(0)


if __name__ == "__main__":
    main()
