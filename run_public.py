#!/usr/bin/env python3
"""Launch SCORM Course Editor locally and expose it with a Cloudflare Quick Tunnel.

The public launcher enables HTTP Basic Authentication automatically and generates
an ephemeral password on every run. It does not modify the normal local launcher.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = 8765
LOCAL_URL = f"http://127.0.0.1:{PORT}"


def fail(msg: str, code: int = 1) -> None:
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    raise SystemExit(code)


def cloudflared_path() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    local_names = ["cloudflared.exe", "cloudflared"] if os.name == "nt" else ["cloudflared"]
    for name in local_names:
        p = BASE / name
        if p.exists() and p.is_file():
            return str(p)
    return None


def install_help() -> str:
    if sys.platform == "darwin":
        return "Install cloudflared first with:  brew install cloudflared"
    if os.name == "nt":
        return "Install cloudflared from Cloudflare Downloads, then reopen this launcher."
    return "Install the cloudflared package for your Linux distribution, then rerun this launcher."


def wait_for_server(user: str, password: str, proc: subprocess.Popen, timeout: float = 12.0) -> None:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            fail("The local editor could not start. Port 8765 may already be in use.")
        try:
            req = urllib.request.Request(LOCAL_URL + "/", headers={"Authorization": "Basic " + token})
            with urllib.request.urlopen(req, timeout=0.8) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    fail("The local editor did not become ready on port 8765.")


def stop(proc: subprocess.Popen | None) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> None:
    cf = cloudflared_path()
    if not cf:
        fail(install_help())

    username = "editor"
    password = secrets.token_urlsafe(12)
    env = os.environ.copy()
    env["EDITOR_USERNAME"] = username
    env["EDITOR_PASSWORD"] = password
    env["PYTHONUNBUFFERED"] = "1"

    py = sys.executable
    server = subprocess.Popen([py, str(BASE / "server.py")], cwd=BASE, env=env)
    tunnel = None
    try:
        wait_for_server(username, password, server)
        print("\nSCORM Course Editor is running locally.")
        print("Starting secure public tunnel...\n")
        print("LOGIN")
        print(f"  Username: {username}")
        print(f"  Password: {password}")
        print("\nKeep this terminal open. Press Ctrl+C to stop public access.\n")

        tunnel = subprocess.Popen(
            [cf, "tunnel", "--url", LOCAL_URL, "--no-autoupdate"],
            cwd=BASE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        shown = False
        assert tunnel.stdout is not None
        for line in tunnel.stdout:
            clean = line.rstrip()
            match = pattern.search(clean)
            if match and not shown:
                shown = True
                print("\n============================================================")
                print("PUBLIC URL")
                print("  " + match.group(0))
                print("============================================================\n")
            # Keep useful Cloudflare diagnostics visible, but reduce noise before URL.
            if " ERR " in clean or " WRN " in clean:
                print(clean)
            if server.poll() is not None:
                fail("The editor process stopped unexpectedly.")
        if tunnel.poll() not in (0, None):
            fail(f"cloudflared stopped with exit code {tunnel.returncode}")
    except KeyboardInterrupt:
        print("\nStopping public tunnel...")
    finally:
        stop(tunnel)
        stop(server)
        print("Public access stopped.")


if __name__ == "__main__":
    main()
