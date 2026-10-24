from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


class Client:
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.sequence = 0

    def call(self, method, params=None):
        self.sequence += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.sequence,
                                            "method": method, "params": params or {}}) + "\n")
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 60)[0]:
            raise RuntimeError("Amethyst did not respond within 60 seconds.")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("The Amethyst launch backend stopped unexpectedly.")
        response = json.loads(line)
        if response.get("id") != self.sequence:
            raise RuntimeError("Unexpected Amethyst launch response.")
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        return response["result"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()


def run(entry_id, command):
    client = Client(command)
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        client.call("system.handshake")
        job = client.call("launchers.entry.run.start", {
            "provider_id": "amethyst.application_launcher/applications", "launcher_id": entry_id})["job"]
        while job["state"] not in {"succeeded", "failed", "cancelled"}:
            if stopping:
                client.call("jobs.cancel", {"job_id": job["id"]})
            time.sleep(0.15)
            job = client.call("jobs.get", {"job_id": job["id"]})["job"]
        if job["state"] != "succeeded":
            raise RuntimeError((job.get("error") or {}).get("message", "Application launch cancelled."))
        launch = job["result"]["launch"]
        stop_sent = False
        while launch["active"]:
            if stopping and not stop_sent:
                client.call("launch.stop", {"launch_id": launch["id"], "confirm": True})
                stop_sent = True
            time.sleep(0.3)
            launch = client.call("launch.get", {"launch_id": launch["id"]})["launch"]
        if launch["state"] == "failed":
            raise RuntimeError((launch.get("error") or {}).get("message", "Application launch failed."))
    finally:
        client.close()


def main():
    root = Path(__file__).resolve().parent
    log_dir = root / "shortcuts"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "last-launch.log").open("w", buffering=1) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        try:
            if len(sys.argv) < 4 or sys.argv[2] != "--":
                raise ValueError("Expected an application ID followed by -- and the headless command.")
            run(sys.argv[1], sys.argv[3:])
        except Exception as exc:
            print(f"Amethyst Steam shortcut: {exc}", file=sys.stderr, flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
