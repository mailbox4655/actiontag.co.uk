#!/usr/bin/env python3
"""Same-host health recovery and transition alerts for ActionTag."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import time
import urllib.error
import urllib.request

POSTMARK_URL = "https://api.postmarkapp.com/email"


def read_environment(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def require(values: dict[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def check_health(url: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ActionTag health watcher"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(64_000))
        healthy = response.status == 200 and payload.get("status") == "healthy"
        return healthy, json.dumps(payload, sort_keys=True)
    except (OSError, ValueError, urllib.error.URLError) as error:
        return False, f"{type(error).__name__}: {error}"


def send_transition(values: dict[str, str], subject: str, text: str) -> str:
    payload = {
        "From": f"{require(values, 'POSTMARK_FROM_NAME')} <{require(values, 'POSTMARK_FROM_EMAIL')}>",
        "To": require(values, "CONTACT_TO"),
        "Cc": require(values, "CONTACT_CC"),
        "Subject": subject,
        "TextBody": text,
        "MessageStream": require(values, "POSTMARK_MESSAGE_STREAM"),
        "Tag": "actiontag-health",
    }
    request = urllib.request.Request(
        POSTMARK_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": require(values, "POSTMARK_SERVER_TOKEN"),
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read(64_000))
    message_id = result.get("MessageID")
    if response.status != 200 or not message_id:
        raise RuntimeError("Postmark health transition response was incomplete")
    return str(message_id)


def atomic_state(path: pathlib.Path, payload: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=pathlib.Path, required=True)
    parser.add_argument("--state", type=pathlib.Path, required=True)
    args = parser.parse_args()

    values = read_environment(args.env)
    health_url = require(values, "ACTIONTAG_LOCAL_HEALTH_URL")
    previous = None
    if args.state.is_file():
        previous = json.loads(args.state.read_text(encoding="utf-8")).get("status")

    healthy, detail = check_health(health_url)
    restarted = False
    if not healthy:
        subprocess.run(["systemctl", "restart", "actiontag.service"], check=True)
        restarted = True
        time.sleep(5)
        healthy, detail = check_health(health_url)

    status = "healthy" if healthy else "unhealthy"
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    if previous != status and (previous is not None or not healthy):
        label = "RECOVERED" if healthy else "UNHEALTHY"
        message_id = send_transition(
            values,
            f"ActionTag health {label}",
            f"Status: {status}\nChecked: {timestamp}\nRestarted: {restarted}\nDetail: {detail}\n",
        )
        print(f"transition={status} postmark={message_id}")
    else:
        print(f"status={status} restarted={str(restarted).lower()} detail={detail}")
    atomic_state(args.state, {
        "status": status,
        "checked_at": timestamp,
        "release": require(values, "ACTIONTAG_RELEASE"),
        "detail": detail,
    })
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
