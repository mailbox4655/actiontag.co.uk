"""Progress-aware child process execution with no implicit completion deadline."""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge_core import BridgeError, kit_root


@dataclass(frozen=True)
class ProcessResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    log_path: Path
    duration_seconds: float
    termination_reason: str | None


def _positive_optional(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise BridgeError(f"{label} must be a positive number of seconds or omitted")
    return float(value)


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _read_pipe(
    name: str, source: Any, messages: queue.Queue[tuple[str, bytes | None]]
) -> None:
    try:
        while True:
            chunk = os.read(source.fileno(), 65_536)
            if not chunk:
                break
            messages.put((name, chunk))
    finally:
        messages.put((name, None))


def run_process(
    command: list[str],
    project: Path,
    label: str,
    *,
    timeout: float | None = None,
    stall_timeout: float | None = None,
    heartbeat: float = 5.0,
) -> ProcessResult:
    """Run a command to completion, surfacing progress without a hidden timeout."""
    timeout = _positive_optional(timeout, "process timeout")
    stall_timeout = _positive_optional(stall_timeout, "process stall timeout")
    heartbeat_value = _positive_optional(heartbeat, "process heartbeat")
    assert heartbeat_value is not None
    if not command:
        raise BridgeError(f"{label} has no command")
    safe_label = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-") or "process"
    logs = kit_root(project) / "runtime" / "process-logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{safe_label}-{time.time_ns()}-{os.getpid()}.log"
    started = time.monotonic()
    last_output = started
    next_heartbeat = started + heartbeat_value
    first_progress = started + 1.0
    first_reported = False
    output_bytes = 0
    termination_reason: str | None = None
    messages: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    readers: list[threading.Thread] = []
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise BridgeError(f"{label} could not start: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    for name, source in (("stdout", process.stdout), ("stderr", process.stderr)):
        reader = threading.Thread(
            target=_read_pipe,
            args=(name, source, messages),
            name=f"gdb-{safe_label}-{name}",
            daemon=True,
        )
        reader.start()
        readers.append(reader)
    closed: set[str] = set()
    try:
        with log_path.open("xb") as log:
            while True:
                now = time.monotonic()
                try:
                    stream, chunk = messages.get(timeout=0.2)
                except queue.Empty:
                    stream, chunk = "", b""
                if chunk is None:
                    closed.add(stream)
                elif chunk:
                    last_output = time.monotonic()
                    output_bytes += len(chunk)
                    (stdout_chunks if stream == "stdout" else stderr_chunks).append(chunk)
                    log.write(f"[{stream}] ".encode("ascii") + chunk)
                    log.flush()
                    if stream == "stdout":
                        sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                        sys.stdout.flush()
                    else:
                        sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                        sys.stderr.flush()
                now = time.monotonic()
                elapsed = now - started
                quiet = now - last_output
                if not first_reported and now >= first_progress and process.poll() is None:
                    first_reported = True
                    print(
                        f"[GPT Design Bridge] {label} is still running ({elapsed:.1f}s); "
                        f"durable log: {log_path}",
                        file=sys.stderr,
                        flush=True,
                    )
                if now >= next_heartbeat and process.poll() is None:
                    print(
                        f"[GPT Design Bridge] {label}: {elapsed:.1f}s elapsed, "
                        f"{output_bytes} output byte(s), last output {quiet:.1f}s ago",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_heartbeat = now + heartbeat_value
                if termination_reason is None and timeout is not None and elapsed >= timeout:
                    termination_reason = "explicit-deadline"
                    _terminate_tree(process)
                elif (
                    termination_reason is None
                    and stall_timeout is not None
                    and quiet >= stall_timeout
                ):
                    termination_reason = "explicit-stall-timeout"
                    _terminate_tree(process)
                if len(closed) == 2 and process.poll() is not None and messages.empty():
                    break
        returncode = process.wait()
    except KeyboardInterrupt:
        termination_reason = "interrupted"
        _terminate_tree(process)
        process.wait()
        raise
    finally:
        if process.poll() is None:
            _terminate_tree(process)
        for reader in readers:
            reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    result = ProcessResult(
        args=list(command),
        returncode=returncode,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        log_path=log_path,
        duration_seconds=time.monotonic() - started,
        termination_reason=termination_reason,
    )
    if termination_reason:
        raise BridgeError(
            f"{label} stopped by {termination_reason} after {result.duration_seconds:.1f}s; "
            f"partial output is preserved at {log_path}"
        )
    if returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise BridgeError(
            f"{label} failed with exit {returncode}: {detail}; full log: {log_path}"
        )
    return result
