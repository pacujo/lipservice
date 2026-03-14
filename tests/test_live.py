#!/usr/bin/env python3
"""
Integration test against a real IRC server (irc.oftc.net).

Starts its own proxy instance, connects to OFTC, joins a test channel,
sends a single message, verifies state, and cleans up.

Usage:
    python3 tests/test_live.py
"""

import os
import subprocess
import sys
import time

import httpx

PORT = "8099"
BASE = f"http://127.0.0.1:{PORT}/api"


def step(msg):
    print(f"  {msg}...", end=" ", flush=True)


def ok(detail=""):
    suffix = f" ({detail})" if detail else ""
    print(f"ok{suffix}")


def main():
    print("starting proxy")
    proc = subprocess.Popen(
        [sys.executable, "-m", "lipservice"],
        env={**os.environ, "LIPSERVICE_PORT": PORT},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    try:
        run(httpx.Client(base_url=BASE, timeout=15))
        print("\nall checks passed")
    except (AssertionError, Exception) as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
    finally:
        proc.terminate()
        proc.wait()


def run(c):
    step("authenticate")
    r = c.post("/auth/token", json={"username": "admin", "password": "changeme"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    ok()

    step("create network")
    r = c.post("/networks", json={
        "name": "oftc", "host": "irc.oftc.net",
        "port": 6697, "tls": True, "nick": "pacujo-test",
    }, headers=h)
    assert r.status_code == 201, r.text
    ok()

    step("connect to irc")
    r = c.post("/networks/oftc/connect", headers=h)
    assert r.status_code == 200, r.text
    ok()

    step("wait for registration")
    nick = None
    for _ in range(30):
        time.sleep(1)
        r = c.get("/networks/oftc", headers=h)
        if r.status_code == 200 and r.json()["state"] == "connected":
            nick = r.json()["nick"]
            break
    assert nick, "IRC registration timed out (30s)"
    ok(nick)

    step("join #lipservice-test")
    r = c.post("/networks/oftc/channels", json={"name": "#lipservice-test"}, headers=h)
    assert r.status_code in (201, 409), r.text
    time.sleep(2)
    ok()

    step("verify channel list")
    r = c.get("/networks/oftc/channels", headers=h)
    assert r.status_code == 200, r.text
    names = [ch["name"] for ch in r.json()]
    assert "#lipservice-test" in names, f"channels: {names}"
    ok(", ".join(names))

    step("send message")
    r = c.post(
        "/networks/oftc/channels/%23lipservice-test/messages",
        json={"text": "lipservice integration test"}, headers=h,
    )
    assert r.status_code == 201, r.text
    ok()

    step("read messages back")
    r = c.get("/networks/oftc/channels/%23lipservice-test/messages", headers=h)
    assert r.status_code == 200, r.text
    n = len(r.json()["messages"])
    ok(f"{n} message(s)")

    step("part channel")
    r = c.delete("/networks/oftc/channels/%23lipservice-test", headers=h)
    assert r.status_code == 204, r.text
    ok()

    step("disconnect")
    r = c.post("/networks/oftc/disconnect", headers=h)
    assert r.status_code == 200, r.text
    ok()

    step("delete network")
    r = c.delete("/networks/oftc", headers=h)
    assert r.status_code == 204, r.text
    ok()

    step("check status")
    r = c.get("/status", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["networks_connected"] == 0
    ok()


if __name__ == "__main__":
    main()
