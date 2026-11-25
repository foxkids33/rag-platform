"""Poll RAG for jobs only while the corporate Confluence endpoint is reachable."""

import http.client
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import time

HOST = os.environ["RAG_CONFLUENCE_HOST"]
DNS_DEVICE = Path("/sys/class/net/cscotun0")
VERIFY = ssl.create_default_context()


def reachable():
    if not DNS_DEVICE.exists():
        return False
    client = http.client.HTTPSConnection(HOST, timeout=5, context=VERIFY)
    try:
        client.request("GET", "/rest/api/space?limit=1", headers={"Accept": "application/json"})
        return client.getresponse().status in {200, 401, 403}
    except (OSError, http.client.HTTPException):
        return False
    finally:
        client.close()


def main():
    child = None
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            if child is not None and child.poll() is not None:
                return child.returncode or 1
            if reachable():
                if child is None and not stopping:
                    child = subprocess.Popen([sys.executable, "-m", "confluence_connector",
                                              "--env-file", "/data/worker.env"])
            elif child is not None:
                child.terminate()
                child.wait(timeout=8)
                child = None
            for _ in range(12):
                if stopping:
                    break
                time.sleep(1)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
