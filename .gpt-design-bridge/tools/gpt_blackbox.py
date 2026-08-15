#!/usr/bin/env python3
"""GPT Blackbox Lite: preservation-first Git diff and evidence gate."""

from __future__ import annotations

import argparse
import copy
import difflib
import fnmatch
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
SUPERSESSION_SCHEMA = "gpt-blackbox-lite-supersession"
ROLES = (
    "senior-engineer",
    "senior-it-project-manager",
    "top-industry-it-guru",
)
STAGES = ("plan", "implementation", "audit")
REVIEW_STAGES = (*STAGES, "settle")
VERDICTS = ("pass", "revise", "block")
LANES = ("patch", "standard", "surface", "full")
CHEAP_LANES = frozenset(("patch", "standard", "surface"))
LANE_POLICY_FILENAME = ".gpt-blackbox-lite-policy.json"
LANE_POLICY_SCHEMA = "gpt-blackbox-lite-lanes"
FAST_LANE_SCHEMA = "gpt-blackbox-lite/fast-lane/v1"
FAST_LANE_DIAGNOSTIC_SCHEMA = "gpt-blackbox-lite/fast-lane-diagnostics/v1"
FAST_LANE_MANIFEST_FILENAME = ".gpt-blackbox-lite-fast-lane.json"
FAST_LANE_BROWSER_POLICIES = ("owner-live", "closure-proof")
LANE_REQUIRED_ROLES = {
    "patch": {"plan": ROLES[:1], "implementation": ROLES[:1], "audit": ()},
    "standard": {"plan": ROLES[:2], "implementation": ROLES[:2], "audit": ROLES[:1]},
    "surface": {"plan": ROLES[:2], "implementation": ROLES[:2], "audit": ROLES[:1]},
    "full": {stage: ROLES for stage in STAGES},
}
RISK_DOMAINS = (
    "architecture",
    "data",
    "delivery",
    "deployment",
    "designer",
    "preservation",
    "security",
    "ui",
)
HIGH_RISK_DOMAINS = frozenset(("data", "deployment", "designer", "security"))
TASK_RISK_DOMAINS = {
    "foundation": ("architecture",),
    "dependency-change": ("architecture",),
    "security": ("security",),
    "auth": ("security",),
    "schema-change": ("data",),
    "migration": ("data",),
    "deploy": ("deployment",),
    "release": ("deployment",),
    "designer-round": ("designer",),
    "designer-adoption": ("designer",),
}
ARTIFACT_CLASSES = (
    "archive",
    "asset",
    "database",
    "documentation",
    "generated",
    "source",
    "vendor",
)
TYPED_BINARY_CLASSES = frozenset(("archive", "asset", "database", "generated", "vendor"))
DEFAULT_THRESHOLDS = {
    "max_file_changed_lines": 350,
    "max_total_changed_lines": 900,
    "max_deletion_ratio": 0.35,
    "max_churn_ratio": 0.65,
    "min_similarity": 0.45,
    "min_rewrite_lines": 40,
}

ANCHOR_PATTERNS = (
    ("function", re.compile(r"(?m)^\s*(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b")),
    ("class", re.compile(r"(?m)^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)\b")),
    ("interface", re.compile(r"(?m)^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)\b")),
    ("type", re.compile(r"(?m)^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\b")),
    ("enum", re.compile(r"(?m)^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)\b")),
    ("export", re.compile(r"(?m)^\s*export\s+(?:declare\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b")),
    ("function", re.compile(r"(?m)^\s*(?:async\s+)?def\s+([A-Za-z_][\w]*)\s*\(")),
    ("class", re.compile(r"(?m)^\s*class\s+([A-Za-z_][\w]*)\b")),
)


class BlackboxError(RuntimeError):
    """A user-correctable harness error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
    text: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=merged_env,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr if text else decode(completed.stderr)
        raise BlackboxError(f"git {' '.join(args)} failed: {stderr.strip()}")
    return completed


def resolve_repo(value: str | None) -> Path:
    candidate = Path(value or os.getcwd()).resolve()
    completed = run_git(candidate, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode != 0:
        raise BlackboxError(f"{candidate} is not inside a Git repository")
    return Path(completed.stdout.strip()).resolve()


def absolute_git_dir(repo: Path) -> Path:
    return Path(run_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve()


def validate_task_id(task_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
        raise BlackboxError(
            "task ID must be 1-64 characters using only letters, digits, hyphens, or underscores"
        )
    return task_id


def state_dir(repo: Path, task_id: str) -> Path:
    return absolute_git_dir(repo) / "gpt-blackbox-lite" / validate_task_id(task_id)


def contract_path(repo: Path, task_id: str) -> Path:
    return state_dir(repo, task_id) / "contract.json"


def normalize_pattern(value: str) -> str:
    pattern = value.strip().replace("\\", "/")
    if not pattern:
        raise BlackboxError("path patterns cannot be empty")
    if pattern == "**":
        raise BlackboxError("repository-wide '**' is intentionally rejected; use narrow paths")
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:/", pattern):
        raise BlackboxError(f"path patterns must be repository-relative: {value}")
    if any(part == ".." for part in pattern.split("/")):
        raise BlackboxError(f"path patterns cannot escape the repository: {value}")
    if pattern.startswith("./"):
        pattern = pattern[2:]
    if pattern.endswith("/"):
        pattern += "**"
    return pattern


def normalize_anchor(value: str) -> str:
    anchor = value.strip()
    if not anchor:
        raise BlackboxError("protected anchors cannot be empty")
    if "::" in anchor:
        path, symbol = anchor.split("::", 1)
        path = normalize_pattern(path)
        if any(char in path for char in "*?["):
            raise BlackboxError("file-specific anchors require an exact file path")
        if not symbol.strip():
            raise BlackboxError("file-specific anchors require a non-empty symbol")
        return f"{path}::{symbol.strip()}"
    return anchor


def normalize_artifact_class(value: str) -> dict[str, str]:
    pattern, separator, artifact_class = value.rpartition("=")
    pattern = normalize_pattern(pattern)
    artifact_class = artifact_class.strip().lower()
    if not separator or artifact_class not in ARTIFACT_CLASSES:
        raise BlackboxError(
            "artifact classes must use 'path-pattern="
            + "|".join(ARTIFACT_CLASSES)
            + "'"
        )
    return {"pattern": pattern, "class": artifact_class}


def artifact_class_for(contract: dict[str, Any], path: str) -> str | None:
    selected = None
    for entry in contract.get("artifact_classes", []):
        if path_matches(path, (entry["pattern"],)):
            selected = entry["class"]
    if selected:
        return selected
    if Path(path).suffix.lower() in {".md", ".txt", ".rst", ".adoc"}:
        return "documentation"
    return None


def build_review_requirements(
    lane: str,
    task_kind: str,
    risk_domains: Iterable[str],
) -> dict[str, list[str]]:
    domains = set(risk_domains)
    domains.update(TASK_RISK_DOMAINS.get(task_kind, ()))
    required: dict[str, set[str]] = {stage: set() for stage in STAGES}
    required["plan"].add(ROLES[0])
    required["implementation"].add(ROLES[0])
    if lane != "patch":
        required["audit"].add(ROLES[0])
    if domains.intersection(HIGH_RISK_DOMAINS):
        for stage in STAGES:
            required[stage].update(ROLES)
    else:
        if "architecture" in domains:
            for stage in STAGES:
                required[stage].add(ROLES[2])
        if domains.intersection({"delivery", "preservation"}):
            required["plan"].add(ROLES[1])
            required["audit"].add(ROLES[1])
    result = {
        stage: [role for role in ROLES if role in required[stage]]
        for stage in STAGES
    }
    result["settle"] = list(result["audit"] or [ROLES[0]])
    return result


def artifact_risk_domains(entries: Iterable[dict[str, str]]) -> set[str]:
    domains: set[str] = set()
    for entry in entries:
        artifact_class = entry["class"]
        if artifact_class == "database":
            domains.add("data")
        elif artifact_class == "archive":
            domains.add("security")
        elif artifact_class in {"generated", "vendor"}:
            domains.add("architecture")
    return domains


def validate_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BlackboxError(f"lane policy field '{field}' must be a positive integer")
    return value
def load_lane_policy(repo: Path) -> dict[str, Any] | None:
    path = repo / LANE_POLICY_FILENAME
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        policy = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlackboxError(f"cannot read lane policy {path}: {exc}") from exc
    if not isinstance(policy, dict) or (
        policy.get("schema"), policy.get("version")
    ) != (LANE_POLICY_SCHEMA, 1):
        raise BlackboxError(f"lane policy {path} must be a '{LANE_POLICY_SCHEMA}' v1 object")
    if policy.get("settle_enabled") is not True:
        raise BlackboxError(f"{path}: settle_enabled must be true; depth may not be skipped")
    normalized: dict[str, Any] = {"sha256": hashlib.sha256(raw).hexdigest(), "path": path.name}
    for field in ("protected_paths", "interface_paths", "surface_support_paths"):
        values = policy.get(field)
        required = field != "surface_support_paths"
        if not isinstance(values, list) or (required and not values) or not all(
            isinstance(value, str) for value in values
        ):
            raise BlackboxError(f"lane policy '{field}' must be a{' non-empty' if required else ''} string array")
        normalized[field] = list(dict.fromkeys(normalize_pattern(value) for value in values))
    task_kinds = policy.get("force_full_task_kinds")
    if not isinstance(task_kinds, list) or not task_kinds or not all(
        isinstance(value, str) and value.strip() for value in task_kinds
    ):
        raise BlackboxError("lane policy 'force_full_task_kinds' must be a non-empty string array")
    normalized["force_full_task_kinds"] = list(dict.fromkeys(x.strip().lower() for x in task_kinds))
    thresholds = policy.get("thresholds")
    if not isinstance(thresholds, dict):
        raise BlackboxError("lane policy 'thresholds' must be an object")
    normalized["thresholds"] = {}
    for lane in CHEAP_LANES:
        profile = thresholds.get(lane)
        if not isinstance(profile, dict):
            raise BlackboxError(f"lane policy is missing thresholds.{lane}")
        normalized["thresholds"][lane] = {
            field: validate_positive_int(profile.get(field), f"thresholds.{lane}.{field}")
            for field in ("max_changed_lines", "max_files")
        }
    return normalized
def patterns_overlap(left: str, right: str) -> bool:
    return path_matches(left, (right,)) or path_matches(right, (left,))
def select_lane(
    args: argparse.Namespace,
    policy: dict[str, Any] | None,
    allowed_paths: list[str],
) -> dict[str, Any]:
    task_kind = args.task_kind.strip().lower()
    declared = {
        "max_changed_lines": args.max_lines,
        "max_files": args.max_files,
        "interface_change": bool(args.interface_change),
    }
    selected, source = "full", "safe-default"
    reason = "no lane policy is present; full remains the inert default"
    candidate = args.lane
    complete = args.max_lines is not None and args.max_files is not None
    if policy:
        protected = [p for p in allowed_paths if any(patterns_overlap(p, g) for g in policy["protected_paths"])]
        if task_kind in policy["force_full_task_kinds"]:
            source, reason = "guard", f"task kind '{task_kind}' is force-full"
        elif protected:
            source, reason = "guard", "declared path intersects protected path: " + ", ".join(protected)
        else:
            source = "owner-pick" if candidate else "classifier"
            if candidate is None and complete:
                candidates = ("surface",) if args.interface_change else ("patch", "standard")
                candidate = next(
                    (lane for lane in candidates if args.max_lines <= policy["thresholds"][lane]["max_changed_lines"]
                     and args.max_files <= policy["thresholds"][lane]["max_files"]),
                    "full",
                )
            elif candidate is None:
                candidate = "full"
            reason = f"{source} selected '{candidate}'"
            if candidate in CHEAP_LANES:
                limits = policy["thresholds"][candidate]
                within = complete and args.max_lines <= limits["max_changed_lines"] and args.max_files <= limits["max_files"]
                if within and (not args.interface_change or candidate == "surface"):
                    selected = candidate
                else:
                    source, reason = "guard", f"declared envelope cannot use '{candidate}'"
    limits = policy["thresholds"].get(selected) if policy and selected in CHEAP_LANES else None
    return {
        "selected": selected,
        "requested": args.lane,
        "source": source,
        "reason": reason,
        "task_kind": task_kind,
        "declared": declared,
        "limits": limits,
        "policy_path": policy["path"] if policy else None,
        "policy_sha256": policy["sha256"] if policy else None,
        "protected_paths": policy["protected_paths"] if policy else [],
        "interface_paths": policy["interface_paths"] if policy else [],
        "surface_support_paths": policy["surface_support_paths"] if policy else [],
    }


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise BlackboxError(f"missing harness state: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BlackboxError(f"cannot read harness state {path}: {exc}") from exc


def load_contract(repo: Path, task_id: str) -> dict[str, Any]:
    contract = read_json(contract_path(repo, task_id))
    recorded_root = Path(contract.get("repo_root", "")).resolve()
    if recorded_root != repo.resolve():
        raise BlackboxError(
            f"task belongs to {recorded_root}, not the active repository {repo.resolve()}"
        )
    return contract


def task_baseline_ref_oid(repo: Path, task_id: str) -> str:
    """Return the immutable baseline tree sealed by this repository's task ref."""
    reference = task_ref(task_id)
    completed = run_git(repo, "rev-parse", "--verify", reference, check=False)
    oid = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", oid):
        raise BlackboxError(
            f"task '{task_id}' has no valid immutable baseline ref {reference}"
        )
    object_type = run_git(repo, "cat-file", "-t", oid, check=False)
    if object_type.returncode != 0 or object_type.stdout.strip() != "tree":
        raise BlackboxError(
            f"task '{task_id}' baseline ref {reference} does not identify a Git tree"
        )
    return oid


def load_retired_contract_for_supersession(repo: Path, task_id: str) -> dict[str, Any]:
    """Load an old contract after a repo rename without weakening normal path identity."""
    contract = read_json(contract_path(repo, task_id))
    if contract.get("task_id") != task_id:
        raise BlackboxError(
            f"retired task state at {contract_path(repo, task_id)} names another task"
        )
    baseline = str(contract.get("baseline_tree", ""))
    sealed_baseline = task_baseline_ref_oid(repo, task_id)
    if baseline != sealed_baseline:
        raise BlackboxError(
            f"retired task '{task_id}' contract baseline {baseline or '<missing>'} does not "
            f"match immutable task ref {task_ref(task_id)} at {sealed_baseline}"
        )
    return contract


def save_contract(repo: Path, task_id: str, contract: dict[str, Any]) -> None:
    atomic_write_json(contract_path(repo, task_id), contract)


def fast_lane_root(repo: Path) -> Path:
    return absolute_git_dir(repo) / "gpt-blackbox-lite" / "fast-lane"


def fast_lane_session_path(repo: Path, session_id: str) -> Path:
    if not re.fullmatch(r"\d{8}-\d{3}", session_id):
        raise BlackboxError(f"invalid Fast Lane session ID: {session_id!r}")
    return fast_lane_root(repo) / "sessions" / f"{session_id}.json"


def fast_lane_base_ref(session_id: str) -> str:
    if not re.fullmatch(r"\d{8}-\d{3}", session_id):
        raise BlackboxError(f"invalid Fast Lane session ID: {session_id!r}")
    return f"refs/gpt-blackbox-lite/fast-lane/{session_id}/base"


def validate_fast_lane_state(repo: Path, state: Any, source: Path | None = None) -> dict[str, Any]:
    location = str(source or fast_lane_root(repo))
    if not isinstance(state, dict) or state.get("schema") != FAST_LANE_SCHEMA:
        raise BlackboxError(f"Fast Lane state has an unsupported schema: {location}")
    session_id = str(state.get("session_id", ""))
    fast_lane_session_path(repo, session_id)
    if Path(str(state.get("repo_root", ""))).resolve() != repo.resolve():
        raise BlackboxError(
            f"Fast Lane session {session_id} belongs to {state.get('repo_root')!r}, "
            f"not {str(repo)!r}"
        )
    if state.get("status") not in {"active", "closing", "sealed"}:
        raise BlackboxError(
            f"Fast Lane session {session_id} has invalid status {state.get('status')!r}"
        )
    return state


def load_fast_lane(repo: Path, required: bool = True) -> dict[str, Any] | None:
    pointer_path = fast_lane_root(repo) / "current.json"
    if not pointer_path.exists():
        if required:
            raise BlackboxError("no Fast Lane session has been started")
        return None
    pointer = read_json(pointer_path)
    if not isinstance(pointer, dict) or not isinstance(pointer.get("session_id"), str):
        raise BlackboxError(f"Fast Lane pointer is malformed: {pointer_path}")
    path = fast_lane_session_path(repo, pointer["session_id"])
    return validate_fast_lane_state(repo, read_json(path), path)


def save_fast_lane(repo: Path, state: dict[str, Any]) -> None:
    validate_fast_lane_state(repo, state)
    atomic_write_json(fast_lane_session_path(repo, state["session_id"]), state)
    atomic_write_json(
        fast_lane_root(repo) / "current.json",
        {"schema": FAST_LANE_SCHEMA, "session_id": state["session_id"]},
    )


def require_clean(repo: Path, context: str) -> None:
    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status:
        paths = [line[3:] if len(line) > 3 else line for line in status.splitlines()]
        preview = ", ".join(paths[:8])
        suffix = " ..." if len(paths) > 8 else ""
        raise BlackboxError(
            f"{context} requires a clean working tree; observed {len(paths)} changed path(s): "
            f"{preview}{suffix}"
        )


def fast_repo_snapshot(repo: Path) -> dict[str, Any]:
    status = run_git(
        repo,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        text=False,
    ).stdout
    index = run_git(repo, "write-tree", check=False)
    return {
        "head": head_oid(repo),
        "worktree_tree": make_worktree_tree(repo),
        "index_tree": index.stdout.strip() if index.returncode == 0 else None,
        "index_error": index.stderr.strip() if index.returncode != 0 else None,
        "status_sha256": hashlib.sha256(status).hexdigest(),
    }


def fast_lane_today(timezone_name: str) -> str:
    candidates = [timezone_name]
    if timezone_name == "Europe/Kiev":
        candidates.append("Europe/Kyiv")
    elif timezone_name == "Europe/Kyiv":
        candidates.append("Europe/Kiev")
    for candidate in candidates:
        try:
            return datetime.now(ZoneInfo(candidate)).strftime("%d%m%Y")
        except ZoneInfoNotFoundError:
            continue
    raise BlackboxError(
        f"timezone data for {timezone_name!r} is unavailable; install system tzdata "
        "before starting Fast Lane so the owner date can be verified"
    )


SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:password|passwd|secret|api[-_]?key|token|access[-_]?token|auth[-_]?token|private[-_]?key)"
    r"\s*[:=]\s*([^\s,;]+)"
)
SECRET_FLAG = re.compile(
    r"(?i)^--?(?:password|passwd|secret|api[-_]?key|token|access[-_]?token|auth[-_]?token)$"
)
SECRET_INLINE_FLAG = re.compile(
    r"(?i)--?(?:password|passwd|secret|api[-_]?key|token|access[-_]?token|auth[-_]?token)"
    r"(?:=|\s+)([^\s,;]+)"
)
SECRET_PREFIX = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{16,}\.)"
)


def is_secret_reference(value: str) -> bool:
    candidate = value.strip()
    return bool(
        re.fullmatch(r"%[A-Za-z_][A-Za-z0-9_]*%", candidate)
        or re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", candidate)
        or re.fullmatch(r"\$env:[A-Za-z_][A-Za-z0-9_]*", candidate, re.IGNORECASE)
        or re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", candidate)
        or re.fullmatch(r"process\.env\.[A-Za-z_][A-Za-z0-9_]*", candidate)
        or re.fullmatch(r"os\.(?:environ\[[^\]]+\]|getenv\([^\)]+\))", candidate)
        or re.fullmatch(r"Environment\.GetEnvironmentVariable\([^\)]+\)", candidate)
        or candidate.lower().startswith(("env:", "environment:", "ignored-local:"))
    )


def secret_material_reason(values: Iterable[str]) -> str | None:
    parts = [str(value) for value in values]
    joined = "\n".join(parts)
    if SECRET_PREFIX.search(joined):
        return "a credential-like token or private-key marker is present"
    if re.search(r"(?i)https?://[^\s/:]+:[^\s/@]+@", joined):
        return "a URL contains inline credentials"
    for match in SECRET_INLINE_FLAG.finditer(joined):
        if not is_secret_reference(match.group(1)):
            return "a credential flag is followed by an inline value"
    for match in SECRET_ASSIGNMENT.finditer(joined):
        if not is_secret_reference(match.group(1)):
            return "a password, secret, key, or token appears to have an inline value"
    for index, part in enumerate(parts[:-1]):
        if SECRET_FLAG.fullmatch(part.strip()) and not is_secret_reference(parts[index + 1]):
            return f"{part!r} is followed by an inline value instead of a safe environment reference"
    return None


def require_no_secret_material(values: Iterable[str], context: str) -> None:
    reason = secret_material_reason(values)
    if reason:
        raise BlackboxError(
            f"{context} cannot enter Fast Lane commits, state, or logs because {reason}; "
            "refer to an ignored environment variable by name and never provide its value"
        )


def fast_worktree_secret_failure(repo: Path) -> str | None:
    changed_paths = set(
        run_git(repo, "diff", "--name-only", "--", ".").stdout.splitlines()
    )
    changed_paths.update(
        run_git(repo, "diff", "--cached", "--name-only", "--", ".").stdout.splitlines()
    )
    untracked_raw = run_git(
        repo, "ls-files", "--others", "--exclude-standard", "-z", text=False
    ).stdout
    untracked = [decode(item) for item in untracked_raw.split(b"\0") if item]
    changed_paths.update(untracked)
    for path in sorted(changed_paths):
        normalized = path.replace("\\", "/")
        name = Path(normalized).name.casefold()
        if (
            (name.startswith(".env") and name not in {".env.example", ".env.sample", ".env.template"})
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
            or name in {"id_rsa", "id_ed25519", "credentials.json"}
        ):
            return f"changed path {normalized!r} is credential-bearing by convention"
    diffs = [
        run_git(repo, "diff", "--no-ext-diff", "--unified=0", "--", ".").stdout,
        run_git(
            repo, "diff", "--cached", "--no-ext-diff", "--unified=0", "--", "."
        ).stdout,
    ]
    reason = secret_material_reason(diffs)
    if reason:
        return f"a tracked diff contains credential-like material: {reason}"
    for relative in untracked:
        path = repo / relative
        try:
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                continue
            data = path.read_bytes()
        except OSError as exc:
            return f"cannot inspect untracked path {relative!r} for credentials: {exc}"
        if is_binary(data):
            continue
        reason = secret_material_reason((decode_text(data),))
        if reason:
            return f"untracked path {relative!r} contains credential-like material: {reason}"
    return None


def fast_message_json(body: str, field: str) -> Any:
    prefix = f"{field}: "
    line = next((item for item in body.splitlines() if item.startswith(prefix)), None)
    if line is None:
        raise BlackboxError(f"Fast Lane checkpoint is missing {field}")
    try:
        return json.loads(line[len(prefix) :])
    except json.JSONDecodeError as exc:
        raise BlackboxError(f"Fast Lane checkpoint has malformed {field}: {exc}") from exc


FAST_LANE_JSON_MESSAGE_FIELDS = (
    "Fast-Lane-Session-JSON",
    "Fast-Lane-Sequence-JSON",
    "Owner-Instruction-JSON",
    "Checkpoint-Summary-JSON",
    "External-Record-JSON",
)


def fast_message_has_field(body: str, field: str) -> bool:
    prefix = f"{field}: "
    return any(item.startswith(prefix) for item in body.splitlines())


def fast_lane_uses_legacy_checkpoint_metadata(state: dict[str, Any]) -> bool:
    entries = state.get("commits")
    return bool(entries) and isinstance(entries, list) and all(
        isinstance(entry, dict) and "summary" not in entry for entry in entries
    )


def validate_legacy_fast_checkpoint(
    state: dict[str, Any],
    entry: dict[str, Any],
    expected_sequence: int,
    prior: str,
    parents: list[str],
    subject: str,
    body: str,
) -> None:
    """Validate the exact checkpoint format emitted before JSON recovery trailers."""
    prefix = f"fast lane {state['date']} commit {expected_sequence:03d}: "
    summary = subject[len(prefix) :] if subject.startswith(prefix) else ""
    if (
        entry.get("sequence") != expected_sequence
        or entry.get("parent") != prior
        or parents != [prior]
        or entry.get("subject") != subject
        or not summary
        or summary != " ".join(summary.split())
    ):
        raise BlackboxError(
            f"Fast Lane checkpoint {expected_sequence:03d} legacy metadata or parent is inconsistent"
        )
    if any(fast_message_has_field(body, field) for field in FAST_LANE_JSON_MESSAGE_FIELDS):
        raise BlackboxError(
            f"Fast Lane checkpoint {expected_sequence:03d} mixes legacy and JSON metadata"
        )
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n")
    instruction = str(entry.get("owner_instruction", "")).replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    owner_block = (
        f"Owner-Instruction: {instruction}\n\n"
        "Validation: deferred until Fast Lane closure"
    )
    if not instruction or normalized_body.count(owner_block) != 1:
        raise BlackboxError(
            f"Fast Lane checkpoint {expected_sequence:03d} has stale legacy owner instruction"
        )
    required_lines = {
        "Fast-Lane-Session: ": f"Fast-Lane-Session: {state['session_id']}",
        "Fast-Lane-Sequence: ": f"Fast-Lane-Sequence: {expected_sequence:03d}",
    }
    body_lines = normalized_body.splitlines()
    if body_lines.count("Validation: deferred until Fast Lane closure") != 1:
        raise BlackboxError(
            f"Fast Lane checkpoint {expected_sequence:03d} has stale legacy validation metadata"
        )
    for prefix, expected_line in required_lines.items():
        observed_lines = [line for line in body_lines if line.startswith(prefix)]
        if observed_lines != [expected_line]:
            raise BlackboxError(
                f"Fast Lane checkpoint {expected_sequence:03d} has stale legacy trailer {expected_line!r}"
            )
    external = entry.get("external")
    external_lines = [
        line
        for line in body_lines
        if line.startswith(("External-Command: ", "External-Result: ", "External-Rollback: "))
    ]
    if external is None:
        if external_lines:
            raise BlackboxError(
                f"Fast Lane checkpoint {expected_sequence:03d} has an unrecorded legacy external action"
            )
        return
    if not isinstance(external, dict):
        raise BlackboxError(
            f"Fast Lane checkpoint {expected_sequence:03d} legacy external record is malformed"
        )
    commands = external.get("commands")
    result = external.get("result")
    rollback = external.get("rollback")
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(item, str) and item for item in commands)
        or not isinstance(result, str)
        or not result
        or not isinstance(rollback, str)
        or not rollback
    ):
        raise BlackboxError(
            f"Fast Lane checkpoint {expected_sequence:03d} legacy external record is incomplete"
        )
    expected_external_lines = [
        *(f"External-Command: {item}" for item in commands),
        f"External-Result: {result}",
        f"External-Rollback: {rollback}",
    ]
    if external_lines != expected_external_lines:
        raise BlackboxError(
            f"Fast Lane checkpoint {expected_sequence:03d} has stale legacy external metadata"
        )


def fast_commit_metadata(repo: Path, oid: str) -> tuple[list[str], str, str]:
    completed = run_git(repo, "show", "-s", "--format=%P%x00%s%x00%B", oid)
    parts = completed.stdout.split("\x00", 2)
    if len(parts) != 3:
        raise BlackboxError(f"cannot parse Fast Lane checkpoint commit {oid}")
    parents = parts[0].strip().split()
    return parents, parts[1].strip(), parts[2]


def archive_fast_evidence(state: dict[str, Any], invalidating_oid: str) -> None:
    prior_diagnosis = state.pop("diagnosis", None)
    if prior_diagnosis:
        prior_diagnosis.update(
            {"invalidated_by": invalidating_oid, "invalidated_at": utc_now()}
        )
        state.setdefault("diagnosis_attempts", []).append(prior_diagnosis)
    prior_closure = state.pop("closure", None)
    if prior_closure:
        prior_closure.update(
            {"invalidated_by": invalidating_oid, "invalidated_at": utc_now()}
        )
        state.setdefault("closure_attempts", []).append(prior_closure)
    state.pop("seal", None)


def recover_fast_checkpoint(repo: Path, state: dict[str, Any]) -> bool:
    """Recover the one commit/state split possible after Git committed successfully."""
    current = head_oid(repo)
    expected_parent = state.get("checkpoint_head")
    if current == expected_parent or not current or not expected_parent:
        return False
    parents, subject, body = fast_commit_metadata(repo, current)
    if parents != [expected_parent]:
        return False
    expected_sequence = len(state.get("commits", [])) + 1
    try:
        session_id = fast_message_json(body, "Fast-Lane-Session-JSON")
        sequence = fast_message_json(body, "Fast-Lane-Sequence-JSON")
        instruction = fast_message_json(body, "Owner-Instruction-JSON")
        summary = fast_message_json(body, "Checkpoint-Summary-JSON")
        external = fast_message_json(body, "External-Record-JSON")
    except BlackboxError:
        return False
    if (
        session_id != state["session_id"]
        or sequence != expected_sequence
        or subject
        != f"fast lane {state['date']} commit {expected_sequence:03d}: {' '.join(str(summary).split())}"
    ):
        return False
    entry = {
        "sequence": expected_sequence,
        "oid": current,
        "parent": expected_parent,
        "subject": subject,
        "summary": summary,
        "owner_instruction": instruction,
        "files": name_status(repo, expected_parent, current),
        "external": external,
        "created_at": utc_now(),
        "recovered_after_state_write_failure": True,
    }
    state.setdefault("commits", []).append(entry)
    state["checkpoint_head"] = current
    archive_fast_evidence(state, current)
    state["status"] = "active"
    save_fast_lane(repo, state)
    return True


def validate_fast_range(
    repo: Path,
    state: dict[str, Any],
    require_current_head: bool = True,
    allow_legacy_sealed: bool = False,
) -> list[str]:
    validate_fast_lane_state(repo, state)
    session_id = state["session_id"]
    base = str(state.get("base_commit", ""))
    head = str(state.get("checkpoint_head", ""))
    if not re.fullmatch(r"[0-9a-f]{40,64}", base) or not re.fullmatch(
        r"[0-9a-f]{40,64}", head
    ):
        raise BlackboxError(f"Fast Lane session {session_id} has an invalid base or head OID")
    sealed_base = run_git(
        repo, "rev-parse", "--verify", fast_lane_base_ref(session_id), check=False
    )
    if sealed_base.returncode != 0 or sealed_base.stdout.strip() != base:
        raise BlackboxError(
            f"Fast Lane session {session_id} base ref is missing or does not match {base}"
        )
    if require_current_head and head_oid(repo) != head:
        raise BlackboxError(
            f"HEAD moved outside Fast Lane bookkeeping: expected {head}, observed {head_oid(repo)}"
        )
    ancestor = run_git(repo, "merge-base", "--is-ancestor", base, head, check=False)
    if ancestor.returncode != 0:
        raise BlackboxError(f"Fast Lane head {head} is not descended from base {base}")
    actual = run_git(repo, "rev-list", "--reverse", f"{base}..{head}").stdout.splitlines()
    entries = state.get("commits", [])
    if not isinstance(entries, list):
        raise BlackboxError(f"Fast Lane session {session_id} commit ledger is malformed")
    recorded = [str(item.get("oid", "")) for item in entries if isinstance(item, dict)]
    if len(recorded) != len(entries) or actual != recorded:
        raise BlackboxError(
            f"Fast Lane range {base}..{head} is not the exact recorded checkpoint sequence"
        )
    prior = base
    observed_formats: set[str] = set()
    for expected_sequence, entry in enumerate(entries, 1):
        oid = entry["oid"]
        parents, subject, body = fast_commit_metadata(repo, oid)
        legacy = "summary" not in entry and not any(
            fast_message_has_field(body, field) for field in FAST_LANE_JSON_MESSAGE_FIELDS
        )
        observed_formats.add("legacy" if legacy else "json")
        if len(observed_formats) != 1:
            raise BlackboxError(
                f"Fast Lane session {session_id} mixes legacy and JSON checkpoint metadata"
            )
        if legacy:
            if not allow_legacy_sealed or state.get("status") != "sealed":
                raise BlackboxError(
                    f"Fast Lane checkpoint {expected_sequence:03d} uses legacy metadata outside an integrity-bound sealed session"
                )
            validate_legacy_fast_checkpoint(
                state,
                entry,
                expected_sequence,
                prior,
                parents,
                subject,
                body,
            )
        else:
            expected_subject = (
                f"fast lane {state['date']} commit {expected_sequence:03d}: "
                f"{' '.join(str(entry.get('summary', '')).split())}"
            )
            if (
                entry.get("sequence") != expected_sequence
                or entry.get("parent") != prior
                or parents != [prior]
                or entry.get("subject") != expected_subject
                or subject != expected_subject
            ):
                raise BlackboxError(
                    f"Fast Lane checkpoint {expected_sequence:03d} metadata or parent is inconsistent"
                )
        if "Validation: deferred until Fast Lane closure" not in body:
            raise BlackboxError(
                f"Fast Lane checkpoint {expected_sequence:03d} lost its deferred-validation marker"
            )
        if not legacy:
            expected_fields = {
                "Fast-Lane-Session-JSON": session_id,
                "Fast-Lane-Sequence-JSON": expected_sequence,
                "Owner-Instruction-JSON": entry.get("owner_instruction"),
                "Checkpoint-Summary-JSON": entry.get("summary"),
                "External-Record-JSON": entry.get("external"),
            }
            for field, expected in expected_fields.items():
                if fast_message_json(body, field) != expected:
                    raise BlackboxError(
                        f"Fast Lane checkpoint {expected_sequence:03d} has stale {field}"
                    )
        if name_status(repo, prior, oid) != entry.get("files"):
            raise BlackboxError(
                f"Fast Lane checkpoint {expected_sequence:03d} changed-path ledger is stale"
            )
        prior = oid
    return recorded


def fast_mode_blockers(repo: Path) -> list[str]:
    blockers: list[str] = []
    for path in sorted((fast_lane_root(repo) / "sessions").glob("*.json")):
        try:
            state = validate_fast_lane_state(repo, read_json(path), path)
        except BlackboxError as exc:
            blockers.append(str(exc))
            continue
        if state.get("status") in {"active", "closing"}:
            blockers.append(
                f"Fast Lane session {state['session_id']} is {state['status']}"
            )
    return blockers


def normal_task_blockers(repo: Path) -> list[str]:
    blockers: list[str] = []
    queue = load_settle_queue(repo)
    state_root = absolute_git_dir(repo) / "gpt-blackbox-lite"
    for contract_file in sorted(state_root.glob("*/contract.json")):
        contract = read_json(contract_file)
        task_id = str(contract.get("task_id", contract_file.parent.name))
        report_file = contract_file.with_name("final-gate.json")
        report = read_json(report_file, {})
        valid_supersession, supersession_failures_found = has_valid_supersession(
            repo, task_id
        )
        if valid_supersession:
            continue
        if supersession_path(repo, task_id).exists():
            blockers.extend(
                f"normal task {task_id} has invalid supersession audit: {failure}"
                for failure in supersession_failures_found
            )
            continue
        if (
            report.get("verdict") != "pass"
            or report.get("contract_version") != contract.get("version")
        ):
            retirement = legacy_retirement_failure(
                repo, contract_file, contract, report_file, report
            )
            if retirement == "":
                continue
            if retirement is None:
                blockers.append(f"normal task {task_id} has no current passing final gate")
            else:
                blockers.append(
                    f"normal task {task_id} has invalid legacy retirement record: {retirement}"
                )
            continue
        if contract.get("lane", {}).get("selected") in CHEAP_LANES:
            item = queue.get("items", {}).get(task_id)
            if not item or item.get("status") != "confirmed":
                blockers.append(f"normal task {task_id} has unsettled deferred depth")
    return blockers


def head_oid(repo: Path) -> str | None:
    completed = run_git(repo, "rev-parse", "--verify", "HEAD", check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def make_worktree_tree(repo: Path) -> str:
    root = absolute_git_dir(repo) / "gpt-blackbox-lite" / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    index_path = root / f"index-{os.getpid()}-{uuid.uuid4().hex}"
    environment = {"GIT_INDEX_FILE": str(index_path)}
    try:
        if head_oid(repo):
            run_git(repo, "read-tree", "HEAD", env=environment)
        else:
            run_git(repo, "read-tree", "--empty", env=environment)
        run_git(repo, "add", "-A", "--", ".", env=environment)
        return run_git(repo, "write-tree", env=environment).stdout.strip()
    finally:
        try:
            index_path.unlink()
        except FileNotFoundError:
            pass


def task_ref(task_id: str) -> str:
    return f"refs/gpt-blackbox-lite/{validate_task_id(task_id)}/baseline"


def supersession_ref(task_id: str) -> str:
    return f"refs/gpt-blackbox-lite/{validate_task_id(task_id)}/supersession"


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatchcase(normalized, pattern):
            return True
    return False


def tree_path_exists(repo: Path, tree: str, path: str) -> bool:
    return run_git(repo, "cat-file", "-e", f"{tree}:{path}", check=False).returncode == 0


def tree_blob(repo: Path, tree: str, path: str) -> bytes:
    completed = run_git(repo, "show", f"{tree}:{path}", check=False, text=False)
    return completed.stdout if completed.returncode == 0 else b""


def is_binary(data: bytes) -> bool:
    return b"\x00" in data


def decode_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def contains_anchor(data: bytes, anchor: str) -> bool:
    text = decode_text(data)
    if re.fullmatch(r"[A-Za-z_$][\w$]*", anchor):
        return re.search(rf"(?<![\w$]){re.escape(anchor)}(?![\w$])", text) is not None
    return anchor in text


def tree_contains_anchor(
    repo: Path,
    tree: str,
    anchor: str,
    *,
    whole_word: bool = False,
) -> bool:
    args = ["grep", "-q", "-F"]
    if whole_word:
        args.append("-w")
    args.extend(["-e", anchor, tree, "--"])
    return run_git(repo, *args, check=False).returncode == 0


def validate_protected_anchors(repo: Path, tree: str, anchors: Iterable[str]) -> None:
    missing: list[str] = []
    for anchor in anchors:
        if "::" in anchor:
            path, symbol = anchor.split("::", 1)
            exists = tree_path_exists(repo, tree, path) and contains_anchor(
                tree_blob(repo, tree, path), symbol
            )
        else:
            exists = tree_contains_anchor(repo, tree, anchor)
        if not exists:
            missing.append(anchor)
    if missing:
        raise BlackboxError(
            "protected anchors must exist in the baseline; missing: " + ", ".join(missing)
        )


def name_status(repo: Path, baseline: str, current: str) -> list[dict[str, str]]:
    completed = run_git(
        repo,
        "diff",
        "--no-renames",
        "--name-status",
        "-z",
        baseline,
        current,
        "--",
        text=False,
    )
    tokens = completed.stdout.split(b"\0")
    changes: list[dict[str, str]] = []
    index = 0
    while index + 1 < len(tokens):
        if not tokens[index]:
            break
        status = decode(tokens[index])
        path = decode(tokens[index + 1]).replace("\\", "/")
        changes.append({"status": status[:1], "path": path})
        index += 2
    return changes


def numstat(repo: Path, baseline: str, current: str) -> dict[str, tuple[int | None, int | None]]:
    completed = run_git(
        repo,
        "diff",
        "--no-renames",
        "--numstat",
        "-z",
        baseline,
        current,
        "--",
        text=False,
    )
    result: dict[str, tuple[int | None, int | None]] = {}
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        parts = raw.split(b"\t", 2)
        if len(parts) != 3:
            continue
        add_raw, delete_raw, path_raw = parts
        additions = None if add_raw == b"-" else int(add_raw)
        deletions = None if delete_raw == b"-" else int(delete_raw)
        result[decode(path_raw).replace("\\", "/")] = (additions, deletions)
    return result


def extract_anchors(text: str) -> set[tuple[str, str]]:
    anchors: set[tuple[str, str]] = set()
    for kind, pattern in ANCHOR_PATTERNS:
        for match in pattern.finditer(text):
            anchors.add((kind, match.group(1)))
    return anchors


def violation(code: str, path: str | None, detail: str) -> dict[str, str]:
    item = {"code": code, "detail": detail}
    if path is not None:
        item["path"] = path
    return item
def lane_assessment(
    repo: Path,
    contract: dict[str, Any],
    current_tree: str,
    files: list[dict[str, Any]],
    total_changed: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    lane = contract.get("lane", {"selected": "full"})
    if lane.get("selected") not in CHEAP_LANES:
        return [], []
    blockers: list[dict[str, str]] = []
    escalations: list[dict[str, str]] = []
    policy_path = lane["policy_path"]
    policy = tree_blob(repo, current_tree, policy_path)
    if not policy or hashlib.sha256(policy).hexdigest() != lane["policy_sha256"]:
        blockers.append(
            violation("lane-policy-drift", policy_path, "cheap-lane policy bytes are absent or changed")
        )
    declared = lane["declared"]
    limits = lane["limits"]
    ceilings = {
        "file": (len(files), min(declared["max_files"], limits["max_files"])),
        "line": (total_changed, min(declared["max_changed_lines"], limits["max_changed_lines"])),
    }
    for kind, (actual, ceiling) in ceilings.items():
        if actual > ceiling:
            escalations.append(violation(f"lane-{kind}-envelope-observed", None, f"{actual} exceeds the initial {ceiling} estimate"))
    changed_paths = [item["path"] for item in files]
    for path in changed_paths:
        if path_matches(path, lane["protected_paths"]):
            escalations.append(violation("lane-protected-path", path, "path requires full rigor"))
    interface_paths = [p for p in changed_paths if path_matches(p, lane["interface_paths"])]
    if lane["selected"] in ("patch", "standard"):
        for path in interface_paths:
            escalations.append(violation("lane-interface-observed", path, "interface diff requires surface or full rigor"))
    if lane["selected"] == "surface":
        if not interface_paths:
            escalations.append(violation("surface-lane-without-surface", None, "actual diff is not confined to the declared surface"))
        surface_paths = (*lane["interface_paths"], *lane["surface_support_paths"])
        for path in changed_paths:
            if not path_matches(path, surface_paths):
                escalations.append(violation("surface-lane-non-surface-path", path, "path requires full rigor"))
    return blockers, escalations


def analyze_diff(
    repo: Path,
    contract: dict[str, Any],
    current_tree: str | None = None,
) -> dict[str, Any]:
    baseline = contract["baseline_tree"]
    current = current_tree or make_worktree_tree(repo)
    changes = name_status(repo, baseline, current)
    stats = numstat(repo, baseline, current)
    thresholds = contract["thresholds"]
    allowed_paths = contract["allowed_paths"]
    owner_boundaries = contract.get("owner_boundaries", [])
    owner_boundary_exceptions = contract.get("owner_boundary_exceptions", [])
    allow_delete = contract.get("allow_delete", [])
    allow_rewrite = contract.get("allow_rewrite", [])
    violations: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    files: list[dict[str, Any]] = []
    total_changed = 0

    for change in changes:
        path = change["path"]
        status = change["status"]
        additions, deletions = stats.get(path, (0, 0))
        rewrite_authorized = path_matches(path, allow_rewrite)
        delete_authorized = path_matches(path, allow_delete)
        artifact_class = artifact_class_for(contract, path)

        if (
            path_matches(path, owner_boundaries)
            and not path_matches(path, owner_boundary_exceptions)
        ):
            violations.append(
                violation(
                    "owner-boundary-changed",
                    path,
                    "the owner marked this path off-limits to task-local discovery and edits",
                )
            )

        if not path_matches(path, allowed_paths):
            violations.append(
                violation(
                    "outside-allowlist",
                    path,
                    "changed path is not in the preservation contract",
                )
            )

        if status == "D" and not delete_authorized:
            violations.append(
                violation(
                    "tracked-file-deleted",
                    path,
                    "tracked file deletion requires explicit user authorization",
                )
            )

        old_data = tree_blob(repo, baseline, path) if status != "A" else b""
        new_data = tree_blob(repo, current, path) if status != "D" else b""
        binary = is_binary(old_data) or is_binary(new_data) or additions is None or deletions is None

        file_result: dict[str, Any] = {
            "path": path,
            "status": status,
            "binary": binary,
            "additions": additions,
            "deletions": deletions,
            "artifact_class": artifact_class,
        }

        if binary:
            if artifact_class in TYPED_BINARY_CLASSES:
                observations.append(
                    violation(
                        "typed-binary-change-observed",
                        path,
                        f"{artifact_class} binary requires current-tree artifact-integrity evidence",
                    )
                )
            elif not rewrite_authorized:
                violations.append(
                    violation(
                        "binary-change-uninspectable",
                        path,
                        "binary replacement requires explicit rewrite authorization",
                    )
                )
            files.append(file_result)
            continue

        additions = int(additions or 0)
        deletions = int(deletions or 0)
        changed_lines = additions + deletions
        total_changed += changed_lines
        old_text = decode_text(old_data)
        new_text = decode_text(new_data)
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        old_count = len(old_lines)
        new_count = len(new_lines)
        deletion_ratio = deletions / max(old_count, 1)
        churn_ratio = changed_lines / max(old_count + new_count, 1)
        if max(old_count, new_count) <= 20_000:
            similarity = difflib.SequenceMatcher(
                None, old_lines, new_lines, autojunk=False
            ).ratio()
        else:
            similarity = max(0.0, 1.0 - churn_ratio)

        file_result.update(
            {
                "old_lines": old_count,
                "new_lines": new_count,
                "changed_lines": changed_lines,
                "deletion_ratio": round(deletion_ratio, 4),
                "churn_ratio": round(churn_ratio, 4),
                "similarity": round(similarity, 4),
            }
        )

        if changed_lines > thresholds["max_file_changed_lines"] and not rewrite_authorized:
            observations.append(
                violation(
                    "large-file-change-observed",
                    path,
                    f"{changed_lines} changed lines exceeds the former {thresholds['max_file_changed_lines']} line review tripwire",
                )
            )

        if (
            status not in ("A", "D")
            and old_count >= thresholds["min_rewrite_lines"]
            and deletion_ratio > thresholds["max_deletion_ratio"]
            and not rewrite_authorized
        ):
            observations.append(
                violation(
                    "high-deletion-observed",
                    path,
                    f"{deletion_ratio:.1%} of baseline lines were deleted",
                )
            )

        if (
            status not in ("A", "D")
            and old_count >= thresholds["min_rewrite_lines"]
            and churn_ratio > thresholds["max_churn_ratio"]
            and similarity < thresholds["min_similarity"]
            and not rewrite_authorized
        ):
            observations.append(
                violation(
                    "high-churn-observed",
                    path,
                    f"high churn ({churn_ratio:.1%}) and low baseline similarity ({similarity:.1%}) require preservation review",
                )
            )

        if status != "D" or not delete_authorized:
            old_anchors = extract_anchors(old_text)
            new_anchors = extract_anchors(new_text)
            for kind, symbol in sorted(old_anchors - new_anchors):
                if not tree_contains_anchor(repo, current, symbol, whole_word=True):
                    observations.append(
                        violation(
                            "auto-detected-anchor-removed",
                            path,
                            f"{kind} '{symbol}' disappeared; require a move, rename, or retirement explanation",
                        )
                    )

        files.append(file_result)

    lane_blockers, lane_escalations = lane_assessment(
        repo, contract, current, files, total_changed
    )
    violations.extend(lane_blockers)

    if total_changed > thresholds["max_total_changed_lines"]:
        observations.append(
            violation(
                "large-task-change-observed",
                None,
                f"{total_changed} changed lines exceeds the former {thresholds['max_total_changed_lines']} line review tripwire",
            )
        )

    for protected in contract.get("protected_anchors", []):
        if "::" in protected:
            path, symbol = protected.split("::", 1)
            existed = tree_path_exists(repo, baseline, path) and contains_anchor(
                tree_blob(repo, baseline, path), symbol
            )
            remains = tree_path_exists(repo, current, path) and contains_anchor(
                tree_blob(repo, current, path), symbol
            )
        else:
            path = None
            symbol = protected
            existed = tree_contains_anchor(repo, baseline, symbol)
            remains = tree_contains_anchor(repo, current, symbol)
        if existed and not remains:
            violations.append(
                violation(
                    "explicit-protected-anchor-removed",
                    path,
                    f"protected anchor '{symbol}' disappeared",
                )
            )

    def unique_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
        unique: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            key = (item["code"], item.get("path", ""), item["detail"])
            if key not in seen:
                unique.append(item)
                seen.add(key)
        return unique

    unique = unique_items(violations)
    observed = unique_items(observations)
    if contract.get("lane", {}).get("selected") in CHEAP_LANES:
        risk_codes = {
            "high-deletion-observed",
            "high-churn-observed",
            "auto-detected-anchor-removed",
        }
        lane_escalations.extend(item for item in observed if item["code"] in risk_codes)
    escalations = unique_items(lane_escalations)

    return {
        "schema": SCHEMA_VERSION,
        "task_id": contract["task_id"],
        "contract_version": contract["version"],
        "baseline_tree": baseline,
        "current_tree": current,
        "generated_at": utc_now(),
        "changed_files": files,
        "changed_file_count": len(files),
        "total_changed_lines": total_changed,
        "violations": unique,
        "observations": observed,
        "lane_escalations": escalations,
        "verdict": "pass" if not unique else "block",
    }


def ensure_lane_promotion(
    repo: Path,
    task_id: str,
    contract: dict[str, Any],
    current_tree: str | None = None,
) -> dict[str, Any]:
    if contract.get("lane", {}).get("selected") not in CHEAP_LANES:
        return contract
    current = current_tree or make_worktree_tree(repo)
    if current == contract["baseline_tree"]:
        return contract
    analysis = analyze_diff(repo, contract, current)
    reasons = analysis.get("lane_escalations", [])
    if not reasons:
        return contract
    before = contract["lane"]["selected"]
    contract["lane"].update(
        {
            "selected": "full",
            "source": "automatic-promotion",
            "reason": "; ".join(item["detail"] for item in reasons),
        }
    )
    contract.setdefault("lane_promotions", []).append(
        {
            "from": before,
            "to": "full",
            "tree": current,
            "reasons": reasons,
            "created_at": utc_now(),
        }
    )
    promotion_domains = {"delivery", "preservation"}
    if any(
        item["code"] in {"high-churn-observed", "auto-detected-anchor-removed"}
        for item in reasons
    ):
        promotion_domains.add("architecture")
    contract["risk_domains"] = sorted(
        set(contract.get("risk_domains", ())).union(promotion_domains)
    )
    contract["review_requirements"] = build_review_requirements(
        "full",
        contract.get("lane", {}).get("task_kind", "code-change"),
        contract["risk_domains"],
    )
    save_contract(repo, task_id, contract)
    print(
        f"GPT Blackbox Lite LANE PROMOTION: {before} -> full; "
        "the current worktree is preserved and deeper reviews are now required"
    )
    for item in reasons:
        location = f" [{item['path']}]" if item.get("path") else ""
        print(f"  NOTE {item['code']}{location}: {item['detail']}")
    return contract


def reviews_path(repo: Path, task_id: str) -> Path:
    return state_dir(repo, task_id) / "reviews.json"


def evidence_path(repo: Path, task_id: str) -> Path:
    return state_dir(repo, task_id) / "evidence.json"


def supersession_path(repo: Path, task_id: str) -> Path:
    return state_dir(repo, task_id) / "supersession.json"


def legacy_retirement_path(repo: Path, task_id: str) -> Path:
    """Locate a retirement record written by pre-supersession Lite releases."""
    return state_dir(repo, task_id) / "retirement.json"


def sha256_path(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BlackboxError(f"cannot hash harness state {path}: {exc}") from exc


def retirement_obligations(contract: dict[str, Any]) -> dict[str, str]:
    """Return the stable, mechanically enumerable duties a retirement audit must address."""
    obligations = {
        "goal": f"Original task goal: {contract.get('goal', '').strip()}",
        "final-gate": "Explain why the successor's passing full gate retires the missing or failed gate.",
    }
    for pattern in contract.get("allowed_paths", []):
        obligations[f"allowed-path:{pattern}"] = f"Original allowed scope: {pattern}"
    for anchor in contract.get("protected_anchors", []):
        obligations[f"protected-anchor:{anchor}"] = f"Original protected anchor: {anchor}"
    for boundary in contract.get("owner_boundaries", []):
        obligations[f"owner-boundary:{boundary}"] = f"Original owner boundary: {boundary}"
    for exception in contract.get("owner_boundary_exceptions", []):
        obligations[f"owner-boundary-exception:{exception}"] = (
            f"Explicit owner-approved exact exception to an owner boundary: {exception}"
        )
    for entry in contract.get("artifact_classes", []):
        key = f"artifact-class:{entry['pattern']}={entry['class']}"
        obligations[key] = f"Original typed artifact contract: {entry['pattern']}={entry['class']}"
    required = list(contract.get("required_checks", []))
    if contract.get("require_browser") and "browser" not in required:
        required.append("browser")
    for check_id in required:
        obligations[f"required-check:{check_id}"] = f"Original required check: {check_id}"
    if contract.get("lane", {}).get("selected") in CHEAP_LANES:
        obligations["deferred-settle"] = "Original cheap-lane full-depth settle obligation."
    return dict(sorted(obligations.items()))


def parse_coverage(values: list[str] | None) -> dict[str, str]:
    coverage: dict[str, str] = {}
    for raw in values or []:
        obligation_id, separator, resolution = raw.partition("=")
        obligation_id = obligation_id.strip()
        resolution = resolution.strip()
        if not separator or not obligation_id:
            raise BlackboxError(
                "each --coverage value must be 'obligation-id=concrete resolution evidence'"
            )
        if obligation_id in coverage:
            raise BlackboxError(f"duplicate --coverage obligation '{obligation_id}'")
        if not resolution:
            raise BlackboxError(f"coverage for '{obligation_id}' requires a concrete resolution")
        coverage[obligation_id] = resolution
    return coverage


def supersession_failures(repo: Path, task_id: str) -> list[str]:
    path = supersession_path(repo, task_id)
    if not path.exists():
        return ["no supersession audit exists"]
    try:
        record = read_json(path)
        old_contract_file = contract_path(repo, task_id)
        old_contract = load_retired_contract_for_supersession(repo, task_id)
        successor_id = validate_task_id(str(record.get("successor_task", "")))
        successor_contract_file = contract_path(repo, successor_id)
        successor_gate_file = state_dir(repo, successor_id) / "final-gate.json"
        successor_contract = load_contract(repo, successor_id)
        successor_gate = read_json(successor_gate_file)
    except BlackboxError as exc:
        return [str(exc)]

    failures: list[str] = []
    sealed_blob = run_git(
        repo, "rev-parse", "--verify", supersession_ref(task_id), check=False
    )
    current_blob = run_git(repo, "hash-object", str(path), check=False)
    if (
        sealed_blob.returncode != 0
        or current_blob.returncode != 0
        or sealed_blob.stdout.strip() != current_blob.stdout.strip()
    ):
        failures.append("supersession audit bytes do not match the immutable Git-ref seal")
    record_version = record.get("version")
    if record.get("schema") != SUPERSESSION_SCHEMA or record_version not in (1, 2):
        failures.append("supersession audit schema/version is invalid")
    if record.get("retired_task") != task_id:
        failures.append("supersession audit names another retired task")
    if record.get("owner_approved") is not True:
        failures.append("supersession audit is not owner approved")
    if successor_id == task_id:
        failures.append("a task cannot supersede itself")
    if successor_contract.get("created_at", "") < old_contract.get("created_at", ""):
        failures.append("successor task was not created after the retired task")
    if successor_contract.get("lane", {}).get("selected") != "full":
        failures.append("successor task must use the full lane")
    if successor_gate.get("verdict") != "pass":
        failures.append("successor final gate does not pass")
    if successor_gate.get("completion") != "final":
        failures.append("successor gate is not a final completion")
    if successor_gate.get("contract_version") != successor_contract.get("version"):
        failures.append("successor gate belongs to another contract version")
    successor_tree = successor_gate.get("current_tree")
    if not isinstance(successor_tree, str) or not re.fullmatch(r"[0-9a-f]{40,64}", successor_tree):
        failures.append("successor gate does not name a valid Git tree")
    else:
        try:
            analysis = analyze_diff(repo, successor_contract, successor_tree)
            if analysis["violations"]:
                failures.append("successor tree no longer passes deterministic diff analysis")
            approval = successor_contract.get("plan_approved")
            if not approval or approval.get("contract_version") != successor_contract.get("version"):
                failures.append("successor contract lacks current preflight approval")
            for stage in STAGES:
                expected_tree = (
                    ensure_plan_review_tree(
                        repo, successor_id, successor_contract, successor_tree
                    )
                    if stage == "plan"
                    else successor_tree
                )
                failures.extend(
                    f"successor {failure}"
                    for failure in review_failures(
                        repo, successor_id, successor_contract, stage, expected_tree
                    )
                )
            failures.extend(
                f"successor {failure}"
                for failure in evidence_failures(
                    repo, successor_id, successor_contract, successor_tree
                )
            )
        except BlackboxError as exc:
            failures.append(f"cannot revalidate successor gate: {exc}")

    bindings = record.get("bindings")
    if not isinstance(bindings, dict):
        failures.append("supersession audit bindings are missing")
    else:
        try:
            expected_bindings = {
                "retired_contract_sha256": sha256_path(old_contract_file),
                "successor_contract_sha256": sha256_path(successor_contract_file),
                "successor_gate_sha256": sha256_path(successor_gate_file),
                "successor_reviews_sha256": sha256_path(reviews_path(repo, successor_id)),
                "successor_evidence_sha256": sha256_path(evidence_path(repo, successor_id)),
                "successor_tree": successor_gate.get("current_tree"),
            }
            if record_version == 2:
                expected_bindings.update(
                    {
                        "retired_task_ref": task_baseline_ref_oid(repo, task_id),
                        "successor_task_ref": task_baseline_ref_oid(repo, successor_id),
                    }
                )
        except BlackboxError as exc:
            failures.append(str(exc))
            expected_bindings = {}
        for name, expected in expected_bindings.items():
            if bindings.get(name) != expected:
                failures.append(f"supersession binding '{name}' is stale or altered")

    expected_obligations = retirement_obligations(old_contract)
    entries = record.get("coverage")
    if not isinstance(entries, list):
        failures.append("supersession coverage must be an array")
    else:
        actual: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                failures.append("supersession coverage contains a malformed entry")
                continue
            if entry["id"] in actual:
                failures.append(f"supersession coverage duplicates '{entry['id']}'")
            actual[entry["id"]] = entry
        missing = sorted(set(expected_obligations) - set(actual))
        unexpected = sorted(set(actual) - set(expected_obligations))
        if missing:
            failures.append("supersession coverage is missing: " + ", ".join(missing))
        if unexpected:
            failures.append("supersession coverage is unexpected: " + ", ".join(unexpected))
        for obligation_id, description in expected_obligations.items():
            entry = actual.get(obligation_id, {})
            if entry.get("description") != description:
                failures.append(f"supersession description for '{obligation_id}' is stale")
            if not str(entry.get("resolution", "")).strip():
                failures.append(f"supersession resolution for '{obligation_id}' is absent")
    return failures


def has_valid_supersession(repo: Path, task_id: str) -> tuple[bool, list[str]]:
    failures = supersession_failures(repo, task_id)
    return not failures, failures


def legacy_retirement_failure(
    repo: Path,
    contract_file: Path,
    contract: dict[str, Any],
    report_file: Path,
    report: dict[str, Any],
) -> str | None:
    """Validate, but never create or rewrite, an older retirement.json seal."""
    task_id = str(contract.get("task_id", contract_file.parent.name))
    record_file = legacy_retirement_path(repo, task_id)
    if not record_file.exists():
        return None
    try:
        record = read_json(record_file)
    except BlackboxError as exc:
        return str(exc)

    required = {
        "schema": SCHEMA_VERSION,
        "task_id": task_id,
        "contract_version": contract.get("version"),
        "failed_gate_verdict": "block",
        "user_approved": True,
    }
    for key, expected in required.items():
        if record.get(key) != expected:
            return f"retirement field {key!r} does not match {expected!r}"
    if len(str(record.get("reason", "")).strip()) < 20:
        return "retirement reason is missing or too short"
    if (
        report.get("verdict") != "block"
        or report.get("contract_version") != contract.get("version")
    ):
        return "retirement is not bound to a failed gate for the current contract"
    if record.get("contract_sha256") != sha256_path(contract_file):
        return "retired contract bytes changed"
    if record.get("failed_gate_sha256") != sha256_path(report_file):
        return "retired failed-gate bytes changed"

    try:
        authorizing_task = validate_task_id(str(record.get("authorizing_task", "")))
    except BlackboxError as exc:
        return str(exc)
    if authorizing_task == task_id:
        return "a failed task cannot authorize its own retirement"
    try:
        authorizing_contract = load_contract(repo, authorizing_task)
        authorizing_gate_file = state_dir(repo, authorizing_task) / "final-gate.json"
        authorizing_gate = read_json(authorizing_gate_file, {})
    except BlackboxError as exc:
        return str(exc)
    if (
        authorizing_gate.get("verdict") != "pass"
        or authorizing_gate.get("contract_version") != authorizing_contract.get("version")
    ):
        return f"authorizing task '{authorizing_task}' has no passing current-contract gate"
    if record.get("authorizing_gate_sha256") != sha256_path(authorizing_gate_file):
        return "authorizing gate bytes changed"
    return ""

def settle_queue_path(repo: Path) -> Path:
    return absolute_git_dir(repo) / "gpt-blackbox-lite" / "settle-queue.json"
def load_settle_queue(repo: Path) -> dict[str, Any]:
    return read_json(settle_queue_path(repo), {"schema": SCHEMA_VERSION, "items": {}})

def save_settle_queue(repo: Path, queue: dict[str, Any]) -> None:
    atomic_write_json(settle_queue_path(repo), queue)

def settle_item(repo: Path, task_id: str) -> dict[str, Any]:
    item = load_settle_queue(repo).get("items", {}).get(task_id)
    if item is None:
        raise BlackboxError(f"task '{task_id}' has no provisional settle obligation")
    return item

def queue_provisional(repo: Path, contract: dict[str, Any], tree: str, report: Path) -> dict[str, Any]:
    queue = load_settle_queue(repo)
    items = queue.setdefault("items", {})
    prior = items.get(contract["task_id"])
    if prior and (prior.get("tree"), prior.get("contract_version"), prior.get("status")) == (
        tree, contract["version"], "confirmed"
    ):
        return prior
    item = {
        "task_id": contract["task_id"],
        "lane": contract["lane"]["selected"],
        "contract_version": contract["version"],
        "baseline_tree": contract["baseline_tree"],
        "tree": tree,
        "status": "provisional",
        "gate_report": str(report),
        "queued_at": utc_now(),
        "settled_at": None,
    }
    items[contract["task_id"]] = item
    save_settle_queue(repo, queue)
    return item

def required_roles(contract: dict[str, Any], stage: str) -> tuple[str, ...]:
    explicit = contract.get("review_requirements", {}).get(stage)
    if isinstance(explicit, list):
        return tuple(role for role in ROLES if role in explicit)
    if stage == "settle": return ROLES
    selected = contract.get("lane", {}).get("selected", "full")
    return tuple(LANE_REQUIRED_ROLES.get(selected, LANE_REQUIRED_ROLES["full"])[stage])


def ensure_plan_review_tree(
    repo: Path,
    task_id: str,
    contract: dict[str, Any],
    current_tree: str,
) -> str:
    """Bind plan review to the baseline or an approved amendment's current tree."""
    amendments = contract.get("amendments", [])
    latest = amendments[-1] if amendments else None
    if not isinstance(latest, dict) or latest.get("to_version") != contract["version"]:
        return contract["baseline_tree"]
    review_tree = latest.get("review_tree")
    if isinstance(review_tree, str) and re.fullmatch(r"[0-9a-f]{40,64}", review_tree):
        return review_tree
    raise BlackboxError(
        "current amendment has no immutable review_tree; it predates the safe "
        "retrospective-amendment contract and cannot bind itself to later edits"
    )

def latest_review(
    reviews: dict[str, Any],
    stage: str,
    role: str,
    contract_version: int,
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in reviews.get("items", [])
        if item.get("stage") == stage
        and item.get("role") == role
        and item.get("contract_version") == contract_version
    ]
    return candidates[-1] if candidates else None


def review_failures(
    repo: Path,
    task_id: str,
    contract: dict[str, Any],
    stage: str,
    expected_tree: str,
) -> list[str]:
    reviews = read_json(reviews_path(repo, task_id), {"schema": SCHEMA_VERSION, "items": []})
    failures: list[str] = []
    for role in required_roles(contract, stage):
        item = latest_review(reviews, stage, role, contract["version"])
        if item is None:
            failures.append(f"missing {stage} verdict from {role}")
            continue
        if item.get("verdict") != "pass":
            failures.append(f"{stage} verdict from {role} is {item.get('verdict')}")
        if item.get("tree") != expected_tree:
            failures.append(f"{stage} verdict from {role} is stale")
    return failures


def evidence_failures(
    repo: Path,
    task_id: str,
    contract: dict[str, Any],
    current_tree: str,
) -> list[str]:
    evidence = read_json(evidence_path(repo, task_id), {"schema": SCHEMA_VERSION, "checks": {}})
    failures: list[str] = []
    required = list(contract.get("required_checks", []))
    if contract.get("require_browser") and "browser" not in required:
        required.append("browser")
    for check_id in required:
        item = evidence.get("checks", {}).get(check_id)
        if item is None:
            failures.append(f"missing required check '{check_id}'")
            continue
        if item.get("status") != "pass":
            failures.append(f"required check '{check_id}' is {item.get('status')}")
        if item.get("tree") != current_tree:
            failures.append(f"required check '{check_id}' is stale")
        if item.get("contract_version") != contract["version"]:
            failures.append(f"required check '{check_id}' belongs to an older contract")
        if item.get("kind") == "browser" and not item.get("artifacts"):
            failures.append("browser evidence has no artifact or durable observation reference")
    return failures


def print_analysis(analysis: dict[str, Any]) -> None:
    print(
        f"GPT Blackbox Lite {analysis['verdict'].upper()}: "
        f"{analysis['changed_file_count']} file(s), "
        f"{analysis['total_changed_lines']} changed line(s), "
        f"{len(analysis['violations'])} violation(s), "
        f"{len(analysis.get('observations', []))} risk observation(s)"
    )
    for file_info in analysis["changed_files"]:
        additions = file_info.get("additions")
        deletions = file_info.get("deletions")
        print(
            f"  {file_info['status']} {file_info['path']} "
            f"(+{additions if additions is not None else '-'} "
            f"-{deletions if deletions is not None else '-'})"
        )
    for item in analysis["violations"]:
        location = f" [{item['path']}]" if item.get("path") else ""
        print(f"  BLOCK {item['code']}{location}: {item['detail']}")
    for item in analysis.get("observations", []):
        location = f" [{item['path']}]" if item.get("path") else ""
        print(f"  NOTE {item['code']}{location}: {item['detail']}")


def command_start(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    fast_conflicts = fast_mode_blockers(repo)
    if fast_conflicts:
        raise BlackboxError(
            "normal tasks cannot overlap an active, closing, or malformed Fast Lane lifecycle: "
            + "; ".join(fast_conflicts)
        )
    task_id = validate_task_id(args.task)
    if args.max_lines is not None and args.max_lines <= 0:
        raise BlackboxError("--max-lines must be a positive integer")
    if args.max_files is not None and args.max_files <= 0:
        raise BlackboxError("--max-files must be a positive integer")
    directory = state_dir(repo, task_id)
    if directory.exists():
        raise BlackboxError(
            f"task '{task_id}' already exists at {directory}; choose another task ID"
        )
    allowed = [normalize_pattern(value) for value in args.allow]
    owner_boundaries = [normalize_pattern(value) for value in (args.exclude or [])]
    protected = [normalize_anchor(value) for value in (args.protect or [])]
    artifact_classes = [
        normalize_artifact_class(value) for value in (args.artifact_class or [])
    ]
    risk_domains = sorted(
        set(value.strip().lower() for value in (args.risk_domain or []))
        .union(TASK_RISK_DOMAINS.get(args.task_kind.strip().lower(), ()))
        .union(artifact_risk_domains(artifact_classes))
    )
    policy = load_lane_policy(repo)
    lane = select_lane(args, policy, allowed)
    required = list(dict.fromkeys(args.require or []))
    require_browser = bool(args.require_browser or lane["selected"] == "surface")
    if require_browser and "browser" not in required:
        required.append("browser")
    if (
        any(entry["class"] in TYPED_BINARY_CLASSES for entry in artifact_classes)
        and "artifact-integrity" not in required
    ):
        required.append("artifact-integrity")
    for check_id in required:
        validate_task_id(check_id)

    directory.mkdir(parents=True)
    baseline = make_worktree_tree(repo)
    if lane["policy_path"]: lane["policy_sha256"] = hashlib.sha256(tree_blob(repo, baseline, lane["policy_path"])).hexdigest()
    validate_protected_anchors(repo, baseline, protected)
    run_git(repo, "update-ref", task_ref(task_id), baseline)
    contract = {
        "schema": SCHEMA_VERSION,
        "version": 1,
        "task_id": task_id,
        "goal": args.goal.strip(),
        "repo_root": str(repo),
        "baseline_head": head_oid(repo),
        "baseline_tree": baseline,
        "baseline_status": run_git(repo, "status", "--porcelain=v1").stdout.splitlines(),
        "allowed_paths": list(dict.fromkeys(allowed)),
        "owner_boundaries": list(dict.fromkeys(owner_boundaries)),
        "owner_boundary_exceptions": [],
        "protected_anchors": list(dict.fromkeys(protected)),
        "artifact_classes": artifact_classes,
        "risk_domains": risk_domains,
        "allow_delete": [],
        "allow_rewrite": [],
        "required_checks": required,
        "require_browser": require_browser,
        "lane": lane,
        "review_requirements": build_review_requirements(
            lane["selected"], lane["task_kind"], risk_domains
        ),
        "thresholds": DEFAULT_THRESHOLDS.copy(),
        "created_at": utc_now(),
        "amendments": [],
    }
    save_contract(repo, task_id, contract)
    atomic_write_json(reviews_path(repo, task_id), {"schema": SCHEMA_VERSION, "items": []})
    atomic_write_json(evidence_path(repo, task_id), {"schema": SCHEMA_VERSION, "checks": {}})
    print(f"GPT Blackbox Lite baseline created for '{task_id}'")
    print(f"  repository: {repo}")
    print(f"  baseline tree: {baseline}")
    print(
        f"  lane: {lane['selected']} "
        f"({lane['source']}: {lane['reason']})"
    )
    print(f"  state: {directory}")
    roles = ", ".join(required_roles(contract, "plan"))
    print(f"  next: record plan verdicts from {roles}, then run preflight")
    return 0


def command_amend(args: argparse.Namespace) -> int:
    if not args.user_approved:
        raise BlackboxError("contract amendments require --user-approved after explicit user approval")
    if not args.reason.strip():
        raise BlackboxError("amendment reason cannot be empty")
    if not any((args.allow, args.allow_delete, args.allow_rewrite, args.protect, args.artifact_class)):
        raise BlackboxError("amend must add at least one path or protected anchor")

    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    current_tree = make_worktree_tree(repo)
    prior_approval = contract.get("plan_approved")
    retrospective = current_tree != contract["baseline_tree"]
    if retrospective and (
        not isinstance(prior_approval, dict)
        or prior_approval.get("contract_version") != contract["version"]
    ):
        raise BlackboxError(
            "a changed tree may be amended without reversal only when the previous "
            "contract already passed preflight; preserve the work and repair the "
            "missing plan authority before expanding scope"
        )
    additions = {
        "allowed_paths": [normalize_pattern(value) for value in (args.allow or [])],
        "allow_delete": [normalize_pattern(value) for value in (args.allow_delete or [])],
        "allow_rewrite": [normalize_pattern(value) for value in (args.allow_rewrite or [])],
        "protected_anchors": [normalize_anchor(value) for value in (args.protect or [])],
        "artifact_classes": [
            normalize_artifact_class(value) for value in (args.artifact_class or [])
        ],
    }
    additions["owner_boundary_exceptions"] = [
        path
        for path in additions["allowed_paths"]
        if not any(character in path for character in "*?[")
        and path_matches(path, contract.get("owner_boundaries", []))
    ]
    validate_protected_anchors(
        repo,
        contract["baseline_tree"],
        additions["protected_anchors"],
    )
    for key, values in additions.items():
        if key == "artifact_classes":
            combined = [*contract.get(key, []), *values]
            deduplicated: list[dict[str, str]] = []
            seen_classes: set[tuple[str, str]] = set()
            for entry in combined:
                identity = (entry["pattern"], entry["class"])
                if identity not in seen_classes:
                    deduplicated.append(entry)
                    seen_classes.add(identity)
            contract[key] = deduplicated
        else:
            contract[key] = list(dict.fromkeys([*contract.get(key, []), *values]))
    if (
        any(entry["class"] in TYPED_BINARY_CLASSES for entry in additions["artifact_classes"])
        and "artifact-integrity" not in contract["required_checks"]
    ):
        contract["required_checks"].append("artifact-integrity")
    contract["risk_domains"] = sorted(
        set(contract.get("risk_domains", ())).union(
            artifact_risk_domains(additions["artifact_classes"])
        )
    )
    contract["review_requirements"] = build_review_requirements(
        contract.get("lane", {}).get("selected", "full"),
        contract.get("lane", {}).get("task_kind", "code-change"),
        contract["risk_domains"],
    )
    old_version = contract["version"]
    contract["version"] = old_version + 1
    contract.pop("plan_approved", None)
    contract.setdefault("amendments", []).append(
        {
            "from_version": old_version,
            "to_version": contract["version"],
            "reason": args.reason.strip(),
            "user_approved": True,
            "retrospective": retrospective,
            "review_tree": current_tree if retrospective else contract["baseline_tree"],
            "prior_plan_approval": prior_approval,
            "additions": additions,
            "created_at": utc_now(),
        }
    )
    save_contract(repo, args.task, contract)
    print(
        f"Contract amended to version {contract['version']}; "
        "all prior approvals and evidence are now stale"
    )
    return 0


def command_discover(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    discovered = normalize_pattern(args.path)
    if any(character in discovered for character in "*?["):
        raise BlackboxError("discovered dependencies must name one exact repository-relative path")
    if not args.reason.strip():
        raise BlackboxError("dependency discovery reason cannot be empty")
    if path_matches(discovered, contract.get("owner_boundaries", [])):
        raise BlackboxError(f"discovered path crosses an owner boundary: {discovered}")
    lane = contract.get("lane", {})
    if path_matches(discovered, lane.get("protected_paths", [])):
        raise BlackboxError(
            f"discovered path is policy-protected and needs an owner-approved amendment: {discovered}"
        )
    if path_matches(discovered, contract.get("allowed_paths", [])):
        raise BlackboxError(f"path is already inside the planned task scope: {discovered}")
    contract["allowed_paths"].append(discovered)
    contract.setdefault("discoveries", []).append(
        {
            "path": discovered,
            "reason": args.reason.strip(),
            "tree_when_discovered": make_worktree_tree(repo),
            "created_at": utc_now(),
            "authority": "same-owner-goal-no-delete-no-rewrite",
        }
    )
    save_contract(repo, args.task, contract)
    print(f"GPT Blackbox Lite DISCOVERY RECORDED: {discovered}")
    print("  current worktree is preserved; deletion and rewrite remain unauthorized")
    return 0


def command_review(args: argparse.Namespace) -> int:
    if not args.summary.strip():
        raise BlackboxError("review summary cannot be empty")
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    if args.stage == "settle":
        if contract.get("lane", {}).get("selected") not in CHEAP_LANES:
            raise BlackboxError("only cheap-lane tasks carry deferred settle reviews")
        item = settle_item(repo, args.task)
        current = item["tree"]
    else:
        current = make_worktree_tree(repo)
        contract = ensure_lane_promotion(repo, args.task, contract, current)

    plan_tree = ensure_plan_review_tree(repo, args.task, contract, current)

    if (
        args.stage == "plan"
        and current != plan_tree
        and not contract.get("lane_promotions")
    ):
        raise BlackboxError(
            "working tree changed after the plan review boundary; preserve the work, "
            "inspect the new exact tree, and obtain any required owner amendment"
        )
    if args.stage not in ("plan", "settle"):
        approval = contract.get("plan_approved")
        if not approval or approval.get("contract_version") != contract["version"]:
            raise BlackboxError("current contract has not passed preflight")

    if args.stage == "implementation" and args.verdict == "pass":
        analysis = analyze_diff(repo, contract, current)
        if analysis["violations"]:
            raise BlackboxError("passing implementation cannot overrule: " + ", ".join(x["code"] for x in analysis["violations"]))

    if args.stage in ("audit", "settle") and args.verdict == "pass":
        analysis = analyze_diff(repo, contract, current)
        if analysis["violations"]:
            raise BlackboxError(
                f"{args.stage} cannot pass while diff violations remain"
            )
        missing = evidence_failures(repo, args.task, contract, current)
        if missing:
            raise BlackboxError(
                f"{args.stage} cannot pass before evidence passes: " + "; ".join(missing)
            )

    path = reviews_path(repo, args.task)
    reviews = read_json(path, {"schema": SCHEMA_VERSION, "items": []})
    reviews.setdefault("items", []).append(
        {
            "stage": args.stage,
            "role": args.role,
            "verdict": args.verdict,
            "summary": args.summary.strip(),
            "tree": plan_tree if args.stage == "plan" else current,
            "contract_version": contract["version"],
            "created_at": utc_now(),
        }
    )
    atomic_write_json(path, reviews)
    if args.stage == "settle" and args.verdict != "pass":
        queue = load_settle_queue(repo)
        queue["items"][args.task]["status"] = "finding-raised"
        queue["items"][args.task]["finding_at"] = utc_now()
        save_settle_queue(repo, queue)
    print(f"Recorded {args.stage} verdict: {args.role} -> {args.verdict}")
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    current = make_worktree_tree(repo)
    plan_tree = ensure_plan_review_tree(repo, args.task, contract, current)
    failures: list[str] = []
    if current != plan_tree:
        failures.append("working tree changed after the immutable plan review boundary")
    failures.extend(
        review_failures(
            repo,
            args.task,
            contract,
            "plan",
            plan_tree,
        )
    )
    if failures:
        print("GPT Blackbox Lite PREFLIGHT BLOCKED")
        for failure in failures:
            print(f"  BLOCK {failure}")
        return 2
    contract["plan_approved"] = {
        "contract_version": contract["version"],
        "tree": plan_tree,
        "kind": (
            "retrospective-amendment"
            if plan_tree != contract["baseline_tree"]
            else "initial-baseline"
        ),
        "approved_at": utc_now(),
    }
    save_contract(repo, args.task, contract)
    print(
        "GPT Blackbox Lite PREFLIGHT PASS: "
        + (
            "owner-approved amendment reconciled on the preserved worktree"
            if plan_tree != contract["baseline_tree"]
            else "editing may begin"
        )
    )
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    current = make_worktree_tree(repo)
    contract = ensure_lane_promotion(repo, args.task, contract, current)
    analysis = analyze_diff(repo, contract, current)
    atomic_write_json(state_dir(repo, args.task) / "latest-inspection.json", analysis)
    print_analysis(analysis)
    return 0 if analysis["verdict"] == "pass" else 2


def require_preflight(contract: dict[str, Any]) -> None:
    approval = contract.get("plan_approved")
    if not approval or approval.get("contract_version") != contract["version"]:
        raise BlackboxError("current contract has not passed preflight")


def positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop only the check process tree started by this harness invocation."""
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


def stream_pipe(
    source: Any,
    messages: queue.Queue[bytes | None],
) -> None:
    try:
        while True:
            # os.read returns the bytes currently available from the pipe;
            # BufferedReader.read1 can wait for its requested buffer on Windows.
            chunk = os.read(source.fileno(), 65536)
            if not chunk:
                break
            messages.put(chunk)
    finally:
        messages.put(None)


def write_live_output(log_file: Any, chunk: bytes) -> None:
    log_file.write(chunk)
    log_file.flush()
    binary_stdout = getattr(sys.stdout, "buffer", None)
    if binary_stdout is not None:
        binary_stdout.write(chunk)
        binary_stdout.flush()
    else:
        sys.stdout.write(decode(chunk))
        sys.stdout.flush()


def command_run(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    contract = ensure_lane_promotion(repo, args.task, contract)
    require_preflight(contract)
    check_id = validate_task_id(args.check)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise BlackboxError("run requires a command after '--'")
    before = make_worktree_tree(repo)
    log_dir = state_dir(repo, args.task) / "evidence"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{check_id}.log"
    started = utc_now()
    started_monotonic = time.monotonic()
    last_output = started_monotonic
    next_heartbeat = started_monotonic + args.heartbeat
    heartbeat_count = 0
    output_bytes = 0
    termination_reason: str | None = None
    return_code: int | None = None
    process: subprocess.Popen[bytes] | None = None
    messages: queue.Queue[bytes | None] = queue.Queue()
    reader: threading.Thread | None = None
    launch_error: str | None = None

    print(
        f"GPT Blackbox Lite running check '{check_id}' with no implicit deadline"
        + (f"; explicit deadline {args.timeout:g}s" if args.timeout else "")
        + (f"; stall policy {args.stall_timeout:g}s" if args.stall_timeout else ""),
        flush=True,
    )
    try:
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        if process.stdout is None:
            raise BlackboxError("check process did not expose its output pipe")
        reader = threading.Thread(
            target=stream_pipe,
            args=(process.stdout, messages),
            name=f"blackbox-check-{check_id}",
            daemon=True,
        )
        reader.start()
        stream_closed = False
        with log_path.open("wb") as log_file:
            while True:
                now = time.monotonic()
                wait_for = min(0.25, max(0.01, next_heartbeat - now))
                try:
                    item = messages.get(timeout=wait_for)
                except queue.Empty:
                    item = b""
                if item is None:
                    stream_closed = True
                elif item:
                    write_live_output(log_file, item)
                    output_bytes += len(item)
                    last_output = time.monotonic()

                now = time.monotonic()
                elapsed = now - started_monotonic
                quiet_for = now - last_output
                if now >= next_heartbeat and process.poll() is None:
                    heartbeat_count += 1
                    print(
                        f"[GPT Blackbox Lite] '{check_id}' still running: "
                        f"{elapsed:.1f}s elapsed, {output_bytes} output byte(s), "
                        f"last child output {quiet_for:.1f}s ago",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_heartbeat = now + args.heartbeat

                if termination_reason is None and args.timeout is not None and elapsed >= args.timeout:
                    termination_reason = "deadline-exceeded"
                    print(
                        f"[GPT Blackbox Lite] explicit {args.timeout:g}s deadline exceeded; "
                        "preserving partial output and stopping this check process tree",
                        file=sys.stderr,
                        flush=True,
                    )
                    terminate_process_tree(process)
                elif (
                    termination_reason is None
                    and args.stall_timeout is not None
                    and quiet_for >= args.stall_timeout
                ):
                    termination_reason = "stall-timeout"
                    print(
                        f"[GPT Blackbox Lite] no child output for {args.stall_timeout:g}s; "
                        "the explicitly selected stall policy fired and partial output is preserved",
                        file=sys.stderr,
                        flush=True,
                    )
                    terminate_process_tree(process)

                if stream_closed and process.poll() is not None and messages.empty():
                    break
            return_code = process.wait()
    except KeyboardInterrupt:
        termination_reason = "interrupted"
        if process is not None:
            terminate_process_tree(process)
            return_code = process.wait()
        print(
            "[GPT Blackbox Lite] check interrupted; partial output remains recorded",
            file=sys.stderr,
            flush=True,
        )
    except OSError as exc:
        termination_reason = "launch-error"
        launch_error = f"{type(exc).__name__}: {exc}"
        log_path.write_text(f"Unable to start check {check_id}: {launch_error}\n", encoding="utf-8")
        print(f"GPT Blackbox Lite could not start check '{check_id}': {launch_error}", file=sys.stderr)
    finally:
        if process is not None and process.poll() is None:
            terminate_process_tree(process)
        if reader is not None:
            reader.join(timeout=5)

    duration_seconds = time.monotonic() - started_monotonic
    after = make_worktree_tree(repo)
    mutated = before != after
    status = "pass" if return_code == 0 and termination_reason is None and not mutated else "fail"
    if status == "pass":
        summary = "command passed"
    elif termination_reason == "deadline-exceeded":
        summary = "command reached its explicit deadline; partial output was preserved"
    elif termination_reason == "stall-timeout":
        summary = "command reached its explicitly selected stall timeout; partial output was preserved"
    elif termination_reason == "interrupted":
        summary = "command was interrupted; partial output was preserved"
    elif termination_reason == "launch-error":
        summary = f"command could not start: {launch_error}"
    elif mutated:
        summary = "command modified the tracked working tree"
    else:
        summary = f"command exited with return code {return_code}"
    evidence = read_json(
        evidence_path(repo, args.task),
        {"schema": SCHEMA_VERSION, "checks": {}},
    )
    evidence.setdefault("checks", {})[check_id] = {
        "kind": "command",
        "status": status,
        "command": command,
        "return_code": return_code,
        "timed_out": termination_reason == "deadline-exceeded",
        "stalled": termination_reason == "stall-timeout",
        "interrupted": termination_reason == "interrupted",
        "termination_reason": termination_reason,
        "deadline_seconds": args.timeout,
        "stall_timeout_seconds": args.stall_timeout,
        "heartbeat_seconds": args.heartbeat,
        "heartbeat_count": heartbeat_count,
        "duration_seconds": round(duration_seconds, 3),
        "output_bytes": output_bytes,
        "mutated_tracked_tree": mutated,
        "tree": after,
        "tree_before": before,
        "contract_version": contract["version"],
        "started_at": started,
        "created_at": utc_now(),
        "artifacts": [str(log_path)],
        "summary": summary,
    }
    atomic_write_json(evidence_path(repo, args.task), evidence)
    print(f"Recorded check '{check_id}': {status} (log: {log_path})")
    return 0 if status == "pass" else 2


def command_record(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    contract = ensure_lane_promotion(repo, args.task, contract)
    require_preflight(contract)
    check_id = validate_task_id(args.check)
    if not args.summary.strip():
        raise BlackboxError("evidence summary cannot be empty")
    artifacts = [item.strip() for item in (args.artifact or []) if item.strip()]
    if args.kind == "browser" and args.status == "pass" and not artifacts:
        raise BlackboxError("passing browser evidence requires at least one artifact or observation reference")
    current = make_worktree_tree(repo)
    evidence = read_json(
        evidence_path(repo, args.task),
        {"schema": SCHEMA_VERSION, "checks": {}},
    )
    evidence.setdefault("checks", {})[check_id] = {
        "kind": args.kind,
        "status": args.status,
        "summary": args.summary.strip(),
        "artifacts": artifacts,
        "tree": current,
        "contract_version": contract["version"],
        "created_at": utc_now(),
    }
    atomic_write_json(evidence_path(repo, args.task), evidence)
    print(f"Recorded {args.kind} evidence '{check_id}': {args.status}")
    return 0 if args.status == "pass" else 2


def validate_evidence_summary(value: str, context: str) -> str:
    summary = value.strip()
    if not summary:
        raise BlackboxError(f"{context} cannot be empty")
    if summary.casefold().rstrip(".! ") in {
        "looks good",
        "tested",
        "done",
        "ok",
        "pass",
        "passed",
    }:
        raise BlackboxError(
            f"{context} must name the evidence examined and the concrete observation; "
            "generic approval is not evidence"
        )
    return summary


def load_fast_diagnostic_manifest(repo: Path, value: str | None) -> dict[str, Any]:
    requested = Path(value or FAST_LANE_MANIFEST_FILENAME)
    path = requested.resolve() if requested.is_absolute() else (repo / requested).resolve()
    try:
        relative = path.relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise BlackboxError(
            "Fast Lane diagnostic manifest must stay inside the repository"
        ) from exc
    tracked = run_git(repo, "ls-files", "--error-unmatch", "--", relative, check=False)
    if tracked.returncode != 0:
        raise BlackboxError(
            f"Fast Lane diagnostic manifest must be tracked by Git: {relative}"
        )
    current_head = head_oid(repo)
    if not current_head or not tree_path_exists(repo, current_head, relative):
        raise BlackboxError(
            f"Fast Lane diagnostic manifest is not present at the current Git head: {relative}"
        )
    raw = tree_blob(repo, current_head, relative)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlackboxError(
            f"cannot read Fast Lane diagnostic manifest {relative}: {exc}"
        ) from exc
    if not isinstance(manifest, dict) or (
        manifest.get("schema"), manifest.get("version")
    ) != (FAST_LANE_DIAGNOSTIC_SCHEMA, 1):
        raise BlackboxError(
            f"Fast Lane diagnostic manifest {relative} must be "
            f"'{FAST_LANE_DIAGNOSTIC_SCHEMA}' v1"
        )
    browser_policy = manifest.get("browser_policy", "owner-live")
    browser_policy_explicit = "browser_policy" in manifest
    if browser_policy not in FAST_LANE_BROWSER_POLICIES:
        raise BlackboxError(
            f"Fast Lane manifest browser_policy must be one of: "
            f"{', '.join(FAST_LANE_BROWSER_POLICIES)}"
        )
    raw_checks = manifest.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise BlackboxError(
            "Fast Lane diagnostic manifest requires a non-empty checks array"
        )
    checks: list[dict[str, Any]] = []
    seen = {"range-diff"}
    for index, item in enumerate(raw_checks, 1):
        if not isinstance(item, dict):
            raise BlackboxError(f"Fast Lane diagnostic check {index} must be an object")
        unexpected = sorted(set(item) - {"id", "command", "timeout", "when_changed"})
        if unexpected:
            raise BlackboxError(
                f"Fast Lane diagnostic check {index} has unsupported fields: "
                + ", ".join(unexpected)
            )
        check_id = validate_task_id(str(item.get("id", "")))
        if check_id in seen:
            raise BlackboxError(
                f"duplicate or reserved Fast Lane diagnostic check: {check_id}"
            )
        seen.add(check_id)
        command = item.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            raise BlackboxError(
                f"Fast Lane diagnostic check '{check_id}' requires a non-empty string command array"
            )
        require_no_secret_material(command, f"Fast Lane diagnostic check '{check_id}'")
        timeout = item.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise BlackboxError(
                f"Fast Lane diagnostic check '{check_id}' requires a positive timeout"
            )
        raw_when_changed = item.get("when_changed", [])
        if not isinstance(raw_when_changed, list) or any(
            not isinstance(pattern, str) for pattern in raw_when_changed
        ):
            raise BlackboxError(
                f"Fast Lane diagnostic check '{check_id}' has an invalid when_changed list"
            )
        checks.append(
            {
                "id": check_id,
                "command": list(command),
                "timeout": float(timeout),
                "when_changed": [normalize_pattern(pattern) for pattern in raw_when_changed],
            }
        )
    browser_checks = [item for item in checks if item["id"] == "browser"]
    if browser_policy == "owner-live" and browser_checks:
        raise BlackboxError(
            "browser_policy 'owner-live' cannot claim a Fast Lane browser check; "
            "browser proof remains a separate release-boundary obligation"
        )
    if browser_policy == "closure-proof" and not browser_checks:
        raise BlackboxError(
            "browser_policy 'closure-proof' requires a diagnostic check with id 'browser'"
        )
    return {
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "browser_policy": browser_policy,
        "browser_policy_explicit": browser_policy_explicit,
        "checks": checks,
    }


def fast_diagnosis_binding(diagnosis: dict[str, Any]) -> str:
    bound = {
        "attempt": diagnosis.get("attempt"),
        "audit_head": diagnosis.get("audit_head"),
        "commit_oids": diagnosis.get("commit_oids"),
        "manifest": diagnosis.get("manifest"),
        "required_checks": diagnosis.get("required_checks"),
        "skipped_checks": diagnosis.get("skipped_checks"),
        "checks": diagnosis.get("checks"),
    }
    encoded = json.dumps(bound, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fast_diagnostic_check_failures(diagnosis: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for check_id in diagnosis.get("required_checks", []):
        item = diagnosis.get("checks", {}).get(check_id)
        if not item or item.get("status") != "pass":
            failures.append(f"diagnostic check '{check_id}' has not passed")
            continue
        if item.get("head") != diagnosis.get("audit_head"):
            failures.append(f"diagnostic check '{check_id}' is stale")
        if item.get("commit_oids") != diagnosis.get("commit_oids"):
            failures.append(f"diagnostic check '{check_id}' names another checkpoint range")
        if item.get("snapshot_before") != diagnosis.get("initial_snapshot"):
            failures.append(f"diagnostic check '{check_id}' did not start on the frozen tree")
        if item.get("snapshot_after") != diagnosis.get("initial_snapshot"):
            failures.append(f"diagnostic check '{check_id}' mutated repository state")
        if item.get("secret_detected"):
            failures.append(f"diagnostic check '{check_id}' emitted credential-like material")
    return failures


def fast_diagnosis_failures(diagnosis: dict[str, Any]) -> list[str]:
    failures = fast_diagnostic_check_failures(diagnosis)
    binding = fast_diagnosis_binding(diagnosis)
    for role in ROLES:
        review = diagnosis.get("reviews", {}).get(role)
        if not review or review.get("verdict") != "pass":
            failures.append(f"diagnostic review from {role} has not passed")
            continue
        if review.get("head") != diagnosis.get("audit_head"):
            failures.append(f"diagnostic review from {role} is stale")
        if review.get("commit_oids") != diagnosis.get("commit_oids"):
            failures.append(f"diagnostic review from {role} names another checkpoint range")
        if review.get("diagnosis_sha256") != binding:
            failures.append(f"diagnostic review from {role} is not bound to current checks")
    return failures


def require_fast_diagnosis(
    repo: Path,
    state: dict[str, Any],
    *,
    require_clean_tree: bool,
) -> dict[str, Any]:
    if require_clean_tree:
        require_clean(repo, "Fast Lane diagnosis promotion")
    commits = validate_fast_range(repo, state)
    diagnosis = state.get("diagnosis")
    if not isinstance(diagnosis, dict):
        raise BlackboxError("run fast-diagnose before reviewing or finishing Fast Lane")
    if not diagnosis.get("completed_at"):
        raise BlackboxError("Fast Lane diagnosis did not complete; rerun fast-diagnose")
    if (diagnosis.get("audit_head"), diagnosis.get("commit_oids")) != (
        state["checkpoint_head"],
        commits,
    ):
        raise BlackboxError("Fast Lane diagnosis is stale; rerun fast-diagnose")
    current = load_fast_diagnostic_manifest(
        repo, diagnosis.get("manifest", {}).get("path")
    )
    if current["sha256"] != diagnosis.get("manifest", {}).get("sha256"):
        raise BlackboxError("Fast Lane diagnostic manifest changed after diagnosis")
    return diagnosis


def run_fast_diagnostic_command(
    repo: Path,
    state: dict[str, Any],
    diagnosis: dict[str, Any],
    check_id: str,
    command: list[str],
    timeout: float,
    *,
    applicability: dict[str, Any],
) -> dict[str, Any]:
    before = fast_repo_snapshot(repo)
    log_path = fast_lane_root(repo) / "evidence" / (
        f"{state['session_id']}-diagnosis-{diagnosis['attempt']}-{check_id}.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "GPT_BLACKBOX_FAST_BASE": state["base_commit"],
            "GPT_BLACKBOX_FAST_HEAD": diagnosis["audit_head"],
            "GPT_BLACKBOX_FAST_SESSION": state["session_id"],
        }
    )
    started_at = utc_now()
    started = time.monotonic()
    next_heartbeat = started + 10.0
    output_bytes = 0
    termination_reason: str | None = None
    return_code: int | None = None
    launch_error: str | None = None
    process: subprocess.Popen[bytes] | None = None
    messages: queue.Queue[bytes | None] = queue.Queue()
    reader: threading.Thread | None = None
    print(
        f"[Fast Lane] running '{check_id}' with explicit {timeout:g}s deadline",
        flush=True,
    )
    try:
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
            env=environment,
        )
        if process.stdout is None:
            raise BlackboxError(f"Fast Lane check '{check_id}' exposed no output pipe")
        reader = threading.Thread(
            target=stream_pipe,
            args=(process.stdout, messages),
            name=f"fast-lane-check-{check_id}",
            daemon=True,
        )
        reader.start()
        stream_closed = False
        with log_path.open("wb") as log_file:
            while True:
                now = time.monotonic()
                try:
                    item = messages.get(timeout=0.25)
                except queue.Empty:
                    item = b""
                if item is None:
                    stream_closed = True
                elif item:
                    write_live_output(log_file, item)
                    output_bytes += len(item)
                now = time.monotonic()
                elapsed = now - started
                if now >= next_heartbeat and process.poll() is None:
                    print(
                        f"[Fast Lane] '{check_id}' still running: {elapsed:.1f}s elapsed, "
                        f"{output_bytes} output byte(s)",
                        file=sys.stderr,
                        flush=True,
                    )
                    next_heartbeat = now + 10.0
                if termination_reason is None and elapsed >= timeout:
                    termination_reason = "deadline-exceeded"
                    print(
                        f"[Fast Lane] '{check_id}' reached its manifest deadline; "
                        "partial output is retained and only its process tree will stop",
                        file=sys.stderr,
                        flush=True,
                    )
                    terminate_process_tree(process)
                if stream_closed and process.poll() is not None and messages.empty():
                    break
            return_code = process.wait()
    except KeyboardInterrupt:
        termination_reason = "interrupted"
        if process is not None:
            terminate_process_tree(process)
            return_code = process.wait()
    except (OSError, BlackboxError) as exc:
        termination_reason = "launch-error"
        launch_error = f"{type(exc).__name__}: {exc}"
        log_path.write_text(
            f"Unable to start Fast Lane check {check_id}: {launch_error}\n",
            encoding="utf-8",
        )
    finally:
        if process is not None and process.poll() is None:
            terminate_process_tree(process)
        if reader is not None:
            reader.join(timeout=5)
    duration = round(time.monotonic() - started, 3)
    after = fast_repo_snapshot(repo)
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = ""
    warnings = [line for line in output.splitlines() if line.startswith("WARN ")]
    secret_reason = secret_material_reason((output,))
    passed = (
        return_code == 0
        and termination_reason is None
        and before == diagnosis["initial_snapshot"]
        and after == diagnosis["initial_snapshot"]
        and head_oid(repo) == diagnosis["audit_head"]
        and secret_reason is None
    )
    return {
        "kind": "command",
        "status": "pass" if passed else "fail",
        "command": command,
        "return_code": return_code,
        "termination_reason": termination_reason,
        "launch_error": launch_error,
        "deadline_seconds": timeout,
        "duration_seconds": duration,
        "output_bytes": output_bytes,
        "warnings": warnings,
        "secret_detected": secret_reason is not None,
        "secret_reason": secret_reason,
        "snapshot_before": before,
        "snapshot_after": after,
        "head": head_oid(repo),
        "commit_oids": diagnosis["commit_oids"],
        "applicability": applicability,
        "artifacts": [str(log_path)],
        "started_at": started_at,
        "created_at": utc_now(),
    }


def command_fast_start(args: argparse.Namespace) -> int:
    instruction = args.instruction
    if not args.user_approved or not instruction.strip():
        raise BlackboxError(
            "Fast Lane requires the owner's explicit start instruction and --user-approved"
        )
    require_no_secret_material((instruction,), "Fast Lane start instruction")
    repo = resolve_repo(args.repo)
    mode_conflicts = fast_mode_blockers(repo)
    task_conflicts = normal_task_blockers(repo)
    if mode_conflicts or task_conflicts:
        raise BlackboxError(
            "Fast Lane cannot overlap another unfinished harness lifecycle: "
            + "; ".join([*mode_conflicts, *task_conflicts])
        )
    require_clean(repo, "Fast Lane start")
    base = head_oid(repo)
    if not base:
        raise BlackboxError("Fast Lane requires an existing Git commit")
    try:
        parsed = datetime.strptime(args.date, "%d%m%Y")
    except ValueError as exc:
        raise BlackboxError("--date must use DDMMYYYY and name a real calendar date") from exc
    if parsed.strftime("%d%m%Y") != args.date:
        raise BlackboxError("--date must preserve leading zeroes in DDMMYYYY form")
    today = fast_lane_today(args.timezone)
    if args.date != today:
        raise BlackboxError(
            f"Fast Lane date must be today's {args.timezone} date: expected {today}, "
            f"observed {args.date}"
        )
    existing_numbers: list[int] = []
    for path in (fast_lane_root(repo) / "sessions").glob(f"{args.date}-*.json"):
        if re.fullmatch(rf"{re.escape(args.date)}-(\d{{3}})", path.stem):
            existing_numbers.append(int(path.stem.rsplit("-", 1)[-1]))
    refs = run_git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        f"refs/gpt-blackbox-lite/fast-lane/{args.date}-*/base",
        check=False,
    ).stdout.splitlines()
    for reference in refs:
        match = re.search(rf"{re.escape(args.date)}-(\d{{3}})/base$", reference)
        if match:
            existing_numbers.append(int(match.group(1)))
    sequence = max(existing_numbers or [0]) + 1
    if sequence > 999:
        raise BlackboxError(f"Fast Lane session space is exhausted for {args.date}")
    session_id = f"{args.date}-{sequence:03d}"
    state = {
        "schema": FAST_LANE_SCHEMA,
        "session_id": session_id,
        "date": args.date,
        "timezone": args.timezone,
        "repo_root": str(repo),
        "status": "active",
        "owner_start_instruction": instruction,
        "owner_approved": True,
        "base_commit": base,
        "checkpoint_head": base,
        "commits": [],
        "diagnosis_attempts": [],
        "closure_attempts": [],
        "seal": None,
        "started_at": utc_now(),
    }
    run_git(repo, "update-ref", fast_lane_base_ref(session_id), base)
    try:
        save_fast_lane(repo, state)
    except Exception:
        run_git(repo, "update-ref", "-d", fast_lane_base_ref(session_id), check=False)
        raise
    print(f"GPT Blackbox Lite FAST LANE STARTED: {session_id}")
    print(f"  base: {base}")
    print("  next checkpoint: 001; validation is explicitly deferred until closure")
    return 0


def command_fast_status(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    state = load_fast_lane(repo, False)
    if not state:
        print("GPT Blackbox Lite FAST LANE: inactive")
        return 0
    recovered = recover_fast_checkpoint(repo, state)
    if recovered:
        state = load_fast_lane(repo)
        assert state is not None
    payload = copy.deepcopy(state)
    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
    payload["runtime"] = {
        "head": head_oid(repo),
        "head_matches_checkpoint": head_oid(repo) == state.get("checkpoint_head"),
        "working_tree_clean": not bool(status),
        "dirty_paths": [
            line[3:] if len(line) > 3 else line for line in status.splitlines()
        ],
        "next_sequence": len(state.get("commits", [])) + 1,
        "recovered_checkpoint": recovered,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_fast_commit(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    state = load_fast_lane(repo)
    assert state is not None
    recovered = recover_fast_checkpoint(repo, state)
    if recovered:
        state = load_fast_lane(repo)
        assert state is not None
        raise BlackboxError(
            f"recovered checkpoint {len(state['commits']):03d} after a prior state-write "
            "interruption; inspect fast-status before recording another instruction"
        )
    if state["status"] not in {"active", "closing"}:
        raise BlackboxError(f"Fast Lane session {state['session_id']} is already sealed")
    if head_oid(repo) != state["checkpoint_head"]:
        raise BlackboxError(
            f"HEAD moved after the last Fast Lane checkpoint: expected "
            f"{state['checkpoint_head']}, observed {head_oid(repo)}"
        )
    instruction = args.instruction
    summary = args.summary
    if not instruction.strip() or not summary.strip():
        raise BlackboxError("Fast Lane checkpoint instruction and summary cannot be empty")
    subject_summary = " ".join(summary.split())
    external_commands = list(args.external_command or [])
    external_fields = (
        bool(external_commands),
        bool(args.external_result and args.external_result.strip()),
        bool(args.external_rollback and args.external_rollback.strip()),
    )
    if any(external_fields) and not all(external_fields):
        raise BlackboxError(
            "external actions require command, result, and rollback/forward-repair bookkeeping"
        )
    if external_commands and not args.external_user_approved:
        raise BlackboxError(
            "external Fast Lane actions require --external-user-approved after the owner's "
            "prompt explicitly authorizes that exact live mutation"
        )
    if args.external_user_approved and not external_commands:
        raise BlackboxError("--external-user-approved is valid only with a complete external record")
    external = (
        {
            "commands": external_commands,
            "result": args.external_result,
            "rollback": args.external_rollback,
            "owner_approved": True,
        }
        if external_commands
        else None
    )
    secret_inputs = [instruction, summary, *external_commands]
    if args.external_result:
        secret_inputs.append(args.external_result)
    if args.external_rollback:
        secret_inputs.append(args.external_rollback)
    require_no_secret_material(secret_inputs, "Fast Lane checkpoint")
    dirty = run_git(
        repo, "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout.splitlines()
    if not dirty and external is None:
        raise BlackboxError(
            "Fast Lane checkpoint has no repository change or owner-approved external record"
        )
    secret_failure = fast_worktree_secret_failure(repo)
    if secret_failure:
        raise BlackboxError(
            "Fast Lane checkpoint refuses to stage the current tree because "
            f"{secret_failure}; remove the value from project history and use an ignored "
            "environment reference"
        )
    sequence = len(state["commits"]) + 1
    subject = f"fast lane {state['date']} commit {sequence:03d}: {subject_summary}"
    body = [
        f"Owner-Instruction: {instruction}",
        "",
        "Validation: deferred until Fast Lane closure",
        f"Fast-Lane-Session: {state['session_id']}",
        f"Fast-Lane-Sequence: {sequence:03d}",
    ]
    if external:
        body.extend(
            [
                *(f"External-Command: {item}" for item in external_commands),
                f"External-Result: {args.external_result}",
                f"External-Rollback: {args.external_rollback}",
            ]
        )
    body.extend(
        [
            "",
            "Fast-Lane-Session-JSON: "
            + json.dumps(state["session_id"], ensure_ascii=False),
            "Fast-Lane-Sequence-JSON: " + json.dumps(sequence),
            "Owner-Instruction-JSON: " + json.dumps(instruction, ensure_ascii=False),
            "Checkpoint-Summary-JSON: " + json.dumps(summary, ensure_ascii=False),
            "External-Record-JSON: " + json.dumps(external, ensure_ascii=False),
        ]
    )
    prior = state["checkpoint_head"]
    run_git(repo, "add", "-A", "--", ".")
    commit_args = ["-c", "commit.gpgsign=false", "commit"]
    if not dirty:
        commit_args.append("--allow-empty")
    run_git(repo, *commit_args, "-m", subject, "-m", "\n".join(body))
    oid = head_oid(repo)
    if oid is None:
        raise BlackboxError("Git reported a Fast Lane commit but HEAD cannot be resolved")
    require_clean(repo, "completed Fast Lane checkpoint")
    entry = {
        "sequence": sequence,
        "oid": oid,
        "parent": prior,
        "subject": subject,
        "summary": summary,
        "owner_instruction": instruction,
        "files": name_status(repo, prior, oid),
        "external": external,
        "created_at": utc_now(),
    }
    state["commits"].append(entry)
    state["checkpoint_head"] = oid
    archive_fast_evidence(state, oid)
    state["status"] = "active"
    save_fast_lane(repo, state)
    validate_fast_range(repo, state)
    print(f"GPT Blackbox Lite FAST LANE CHECKPOINT {sequence:03d}: {oid}")
    print(f"  {subject}")
    print("  validation remains deferred; use fast-status before the next instruction")
    return 0


def command_fast_diagnose(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    state = load_fast_lane(repo)
    assert state is not None
    if state["status"] != "active":
        raise BlackboxError(
            f"Fast Lane diagnosis requires active status, not {state['status']}"
        )
    require_clean(repo, "Fast Lane diagnosis")
    commits = validate_fast_range(repo, state)
    if not commits:
        raise BlackboxError("Fast Lane cannot diagnose without a recorded checkpoint")
    manifest = load_fast_diagnostic_manifest(repo, args.manifest)
    current_manifest_blob = tree_blob(repo, state["checkpoint_head"], manifest["path"])
    if hashlib.sha256(current_manifest_blob).hexdigest() != manifest["sha256"]:
        raise BlackboxError(
            f"Fast Lane manifest {manifest['path']} does not match the exact checkpoint head"
        )
    changed_paths = [
        item["path"]
        for item in name_status(repo, state["base_commit"], state["checkpoint_head"])
    ]
    applicable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in manifest["checks"]:
        matched = sorted(
            path
            for path in changed_paths
            if not item["when_changed"] or path_matches(path, item["when_changed"])
        )
        if item["when_changed"] and not matched:
            skipped.append(
                {
                    "id": item["id"],
                    "status": "skipped-not-applicable",
                    "predicate": {"when_changed": item["when_changed"]},
                    "matched_paths": [],
                }
            )
        else:
            selected = copy.deepcopy(item)
            selected["matched_paths"] = matched
            applicable.append(selected)
    prior = state.pop("diagnosis", None)
    if prior:
        prior.update(
            {"superseded_at": utc_now(), "superseded_reason": "diagnosis rerun"}
        )
        state.setdefault("diagnosis_attempts", []).append(prior)
    attempts = [
        int(item.get("attempt", 0))
        for item in state.get("diagnosis_attempts", [])
        if isinstance(item, dict)
    ]
    diagnosis = {
        "attempt": max(attempts or [0]) + 1,
        "audit_head": state["checkpoint_head"],
        "commit_oids": commits,
        "manifest": {
            "path": manifest["path"],
            "sha256": manifest["sha256"],
            "browser_policy": manifest["browser_policy"],
            "browser_policy_explicit": manifest["browser_policy_explicit"],
        },
        "changed_paths": changed_paths,
        "required_checks": ["range-diff", *(item["id"] for item in applicable)],
        "skipped_checks": skipped,
        "checks": {},
        "reviews": {},
        "initial_snapshot": fast_repo_snapshot(repo),
        "started_at": utc_now(),
    }
    state["diagnosis"] = diagnosis
    save_fast_lane(repo, state)
    commands = [
        {
            "id": "range-diff",
            "command": [
                "git",
                "diff",
                "--check",
                f"{state['base_commit']}..{state['checkpoint_head']}",
            ],
            "timeout": 120.0,
            "when_changed": [],
            "matched_paths": changed_paths,
        },
        *applicable,
    ]
    print(f"GPT Blackbox Lite FAST LANE DIAGNOSIS: {state['session_id']}")
    print(f"  exact range: {state['base_commit']}..{state['checkpoint_head']}")
    print(
        f"  manifest: {manifest['path']} sha256={manifest['sha256']} "
        f"browser_policy={manifest['browser_policy']}"
    )
    if not manifest["browser_policy_explicit"]:
        print(
            "  WARN browser_policy is absent; legacy v1 default 'owner-live' is recorded. "
            "Add the field explicitly on the next governed project change."
        )
    for item in skipped:
        print(
            f"  {item['id']}: skipped-not-applicable "
            f"(when_changed={item['predicate']['when_changed']}, matched_paths=[])"
        )
    for item in commands:
        applicability = {
            "when_changed": item.get("when_changed", []),
            "matched_paths": item.get("matched_paths", []),
        }
        result = run_fast_diagnostic_command(
            repo,
            state,
            diagnosis,
            item["id"],
            item["command"],
            float(item["timeout"]),
            applicability=applicability,
        )
        diagnosis["checks"][item["id"]] = result
        save_fast_lane(repo, state)
        warning_suffix = (
            f", warnings={len(result['warnings'])}" if result["warnings"] else ""
        )
        print(
            f"  {item['id']}: {result['status']} "
            f"({result['duration_seconds']:.3f}s{warning_suffix})"
        )
        if result["status"] != "pass":
            print(f"    log: {result['artifacts'][0]}")
            if result["termination_reason"]:
                print(f"    cause: {result['termination_reason']}")
            if result["snapshot_after"] != diagnosis["initial_snapshot"]:
                print("    cause: repository state changed during diagnostics")
            if result["secret_detected"]:
                print("    cause: credential-like output was detected; treat the log as sensitive")
    diagnosis["final_snapshot"] = fast_repo_snapshot(repo)
    diagnosis["completed_at"] = utc_now()
    diagnosis["check_failures"] = fast_diagnostic_check_failures(diagnosis)
    save_fast_lane(repo, state)
    if diagnosis["check_failures"]:
        print(
            f"  collected {len(diagnosis['check_failures'])} failure(s); all three roles "
            "must record their findings before the next corrective checkpoint"
        )
        return 2
    print("  all applicable checks passed; record all three diagnostic reviews")
    return 0


def command_fast_diagnostic_review(args: argparse.Namespace) -> int:
    summary = validate_evidence_summary(
        args.summary, "Fast Lane diagnostic review summary"
    )
    repo = resolve_repo(args.repo)
    state = load_fast_lane(repo)
    assert state is not None
    if state["status"] != "active":
        raise BlackboxError("Fast Lane diagnostic reviews require active status")
    diagnosis = require_fast_diagnosis(repo, state, require_clean_tree=False)
    index = ROLES.index(args.role)
    if index and ROLES[index - 1] not in diagnosis["reviews"]:
        raise BlackboxError(
            f"{ROLES[index - 1]} must record a diagnostic verdict before {args.role}"
        )
    diagnosis["reviews"][args.role] = {
        "verdict": args.verdict,
        "summary": summary,
        "head": diagnosis["audit_head"],
        "commit_oids": diagnosis["commit_oids"],
        "diagnosis_sha256": fast_diagnosis_binding(diagnosis),
        "created_at": utc_now(),
    }
    save_fast_lane(repo, state)
    print(f"Recorded Fast Lane diagnostic verdict: {args.role} -> {args.verdict}")
    return 0 if args.verdict == "pass" else 2


def command_fast_finish(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    state = load_fast_lane(repo)
    assert state is not None
    if state["status"] != "active":
        raise BlackboxError(
            f"Fast Lane finish requires active status, not {state['status']}"
        )
    require_clean(repo, "Fast Lane finish")
    commits = validate_fast_range(repo, state)
    if not commits:
        raise BlackboxError("Fast Lane cannot close without a recorded checkpoint")
    diagnosis = require_fast_diagnosis(repo, state, require_clean_tree=True)
    failures = fast_diagnosis_failures(diagnosis)
    if failures:
        raise BlackboxError(
            "Fast Lane diagnosis is not promotable: " + "; ".join(failures)
        )
    state["status"] = "closing"
    state["closure"] = {
        "attempt": len(state.get("closure_attempts", [])) + 1,
        "audit_head": state["checkpoint_head"],
        "commit_oids": commits,
        "manifest": copy.deepcopy(diagnosis["manifest"]),
        "required_checks": list(diagnosis["required_checks"]),
        "skipped_checks": copy.deepcopy(diagnosis["skipped_checks"]),
        "checks": copy.deepcopy(diagnosis["checks"]),
        "reviews": copy.deepcopy(diagnosis["reviews"]),
        "promoted_from_diagnosis": {
            "attempt": diagnosis["attempt"],
            "diagnosis_sha256": fast_diagnosis_binding(diagnosis),
        },
        "started_at": utc_now(),
    }
    save_fast_lane(repo, state)
    print(f"GPT Blackbox Lite FAST LANE CLOSURE READY: {state['session_id']}")
    print(f"  exact range: {state['base_commit']}..{state['checkpoint_head']}")
    print(
        f"  promoted {len(diagnosis['checks'])} exact-head checks and "
        f"{len(diagnosis['reviews'])} reviews without rerunning them"
    )
    print("  next: run fast-gate")
    return 0


def require_fast_closing(repo: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") != "closing" or not isinstance(state.get("closure"), dict):
        raise BlackboxError("run fast-finish before fast-gate")
    require_clean(repo, "Fast Lane gate")
    commits = validate_fast_range(repo, state)
    closure = state["closure"]
    if (closure.get("audit_head"), closure.get("commit_oids")) != (
        state["checkpoint_head"],
        commits,
    ):
        raise BlackboxError("Fast Lane closure is stale; diagnose and finish again")
    return closure


def fast_closure_failures(repo: Path, state: dict[str, Any], closure: dict[str, Any]) -> list[str]:
    diagnosis_like = {
        "attempt": closure.get("promoted_from_diagnosis", {}).get("attempt"),
        "audit_head": closure.get("audit_head"),
        "commit_oids": closure.get("commit_oids"),
        "manifest": closure.get("manifest"),
        "required_checks": closure.get("required_checks"),
        "skipped_checks": closure.get("skipped_checks"),
        "checks": closure.get("checks"),
        "reviews": closure.get("reviews"),
        "initial_snapshot": next(
            (
                item.get("snapshot_before")
                for item in closure.get("checks", {}).values()
                if isinstance(item, dict)
            ),
            None,
        ),
    }
    failures = fast_diagnostic_check_failures(diagnosis_like)
    expected_binding = closure.get("promoted_from_diagnosis", {}).get(
        "diagnosis_sha256"
    )
    for role in ROLES:
        review = closure.get("reviews", {}).get(role)
        if not review or review.get("verdict") != "pass":
            failures.append(f"missing passing Fast Lane review from {role}")
            continue
        if review.get("head") != closure.get("audit_head"):
            failures.append(f"Fast Lane review from {role} is stale")
        if review.get("commit_oids") != closure.get("commit_oids"):
            failures.append(f"Fast Lane review from {role} names another range")
        if review.get("diagnosis_sha256") != expected_binding:
            failures.append(f"Fast Lane review from {role} has another diagnosis binding")
    current_manifest = load_fast_diagnostic_manifest(
        repo, closure.get("manifest", {}).get("path")
    )
    if current_manifest["sha256"] != closure.get("manifest", {}).get("sha256"):
        failures.append("Fast Lane diagnostic manifest changed after closure promotion")
    if fast_repo_snapshot(repo) != diagnosis_like["initial_snapshot"]:
        failures.append("repository snapshot changed after Fast Lane diagnosis")
    return failures


def fast_gate_report_path(repo: Path, session_id: str) -> Path:
    return fast_lane_root(repo) / "evidence" / f"{session_id}-final-gate.json"


def complete_fast_seal_from_existing_tag(
    repo: Path,
    state: dict[str, Any],
    closure: dict[str, Any],
) -> bool:
    tag = f"fast-lane-{state['session_id']}-sealed"
    tag_ref = f"refs/tags/{tag}"
    if run_git(repo, "rev-parse", "--verify", tag_ref, check=False).returncode != 0:
        return False
    report_path = fast_gate_report_path(repo, state["session_id"])
    if not report_path.is_file():
        raise BlackboxError(
            f"Fast Lane seal tag {tag} exists but gate report is missing: {report_path}"
        )
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    contents = run_git(
        repo, "for-each-ref", "--format=%(contents)", tag_ref, check=False
    ).stdout
    peeled = run_git(repo, "rev-parse", "--verify", f"{tag_ref}^{{}}", check=False)
    if (
        peeled.returncode != 0
        or peeled.stdout.strip() != closure["audit_head"]
        or f"Gate-SHA256: {digest}" not in contents
    ):
        raise BlackboxError(
            f"existing Fast Lane tag {tag} does not bind the current head and report"
        )
    state["status"] = "sealed"
    state["sealed_at"] = utc_now()
    state["seal"] = {
        "tag": tag,
        "head": closure["audit_head"],
        "report": str(report_path),
        "sha256": digest,
    }
    closure["completed_at"] = utc_now()
    save_fast_lane(repo, state)
    return True


def command_fast_gate(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    state = load_fast_lane(repo)
    assert state is not None
    closure = require_fast_closing(repo, state)
    if complete_fast_seal_from_existing_tag(repo, state, closure):
        print("GPT Blackbox Lite FAST LANE GATE: PASS (recovered existing seal)")
        print(f"  tag: {state['seal']['tag']}")
        return 0
    failures = fast_closure_failures(repo, state, closure)
    report = {
        "schema": FAST_LANE_SCHEMA,
        "session_id": state["session_id"],
        "base_commit": state["base_commit"],
        "head": closure["audit_head"],
        "checkpoint_oids": closure["commit_oids"],
        "checkpoints": copy.deepcopy(state["commits"]),
        "manifest": copy.deepcopy(closure["manifest"]),
        "skipped_checks": copy.deepcopy(closure["skipped_checks"]),
        "checks": copy.deepcopy(closure["checks"]),
        "reviews": copy.deepcopy(closure["reviews"]),
        "failures": failures,
        "verdict": "pass" if not failures else "block",
        "generated_at": utc_now(),
    }
    report_path = fast_gate_report_path(repo, state["session_id"])
    atomic_write_json(report_path, report)
    if failures:
        print("GPT Blackbox Lite FAST LANE GATE: BLOCKED")
        for failure in failures:
            print(f"  BLOCK {failure}")
        return 2
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    tag = f"fast-lane-{state['session_id']}-sealed"
    message = (
        f"Fast Lane {state['session_id']} sealed\n\n"
        f"Base: {state['base_commit']}\n"
        f"Head: {closure['audit_head']}\n"
        f"Gate-SHA256: {digest}"
    )
    run_git(
        repo,
        "-c",
        "tag.gpgSign=false",
        "tag",
        "-a",
        tag,
        "-m",
        message,
        closure["audit_head"],
    )
    if not complete_fast_seal_from_existing_tag(repo, state, closure):
        raise BlackboxError(f"Fast Lane tag {tag} was not created")
    print("GPT Blackbox Lite FAST LANE GATE: PASS")
    print(f"  sealed range: {state['base_commit']}..{closure['audit_head']}")
    print(f"  report sha256: {digest}")
    print(f"  tag: {tag}")
    return 0


def command_gate(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    current = make_worktree_tree(repo)
    contract = ensure_lane_promotion(repo, args.task, contract, current)
    analysis = analyze_diff(repo, contract, current)
    failures: list[str] = [
        f"{item['code']}{' [' + item['path'] + ']' if item.get('path') else ''}: {item['detail']}"
        for item in analysis["violations"]
    ]
    approval = contract.get("plan_approved")
    if not approval or approval.get("contract_version") != contract["version"]:
        failures.append("current contract version has not passed preflight")
    plan_tree = ensure_plan_review_tree(repo, args.task, contract, current)
    for stage in STAGES:
        expected = plan_tree if stage == "plan" else current
        failures.extend(review_failures(repo, args.task, contract, stage, expected))
    failures.extend(evidence_failures(repo, args.task, contract, current))
    report = {
        "schema": SCHEMA_VERSION,
        "task_id": args.task,
        "contract_version": contract["version"],
        "current_tree": current,
        "generated_at": utc_now(),
        "analysis": analysis,
        "failures": failures,
        "verdict": "pass" if not failures else "block",
        "completion": "provisional" if not failures and contract.get("lane", {}).get("selected") in CHEAP_LANES else "final",
    }
    report_path = state_dir(repo, args.task) / "final-gate.json"
    atomic_write_json(report_path, report)
    print_analysis(analysis)
    if failures:
        print("GPT Blackbox Lite FINAL GATE: BLOCKED")
        for failure in failures:
            print(f"  BLOCK {failure}")
        return 2
    if contract.get("lane", {}).get("selected") in CHEAP_LANES:
        item = queue_provisional(repo, contract, current, report_path)
        if item["status"] == "confirmed":
            print("GPT Blackbox Lite SETTLED GATE: PASS")
        else:
            print("GPT Blackbox Lite PROVISIONAL GATE: PASS")
            print("  release blocked until three settle reviews and settle-gate pass")
        print(f"  lane: {contract['lane']['selected']}")
    else:
        print("GPT Blackbox Lite FINAL GATE: PASS")
    print(f"  tree: {current}")
    print(f"  report: {report_path}")
    return 0

def command_settle_gate(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    item = settle_item(repo, args.task)
    tree = item["tree"]
    analysis = analyze_diff(repo, contract, tree)
    failures = [f"{x['code']}{' [' + x['path'] + ']' if x.get('path') else ''}: {x['detail']}" for x in analysis["violations"]]
    gate_report = read_json(Path(item["gate_report"]))
    if gate_report.get("verdict") != "pass" or gate_report.get("current_tree") != tree:
        failures.append("provisional gate report is absent, failed, or bound to another tree")
    failures.extend(review_failures(repo, args.task, contract, "settle", tree))
    failures.extend(evidence_failures(repo, args.task, contract, tree))
    report = {"schema": SCHEMA_VERSION, "task_id": args.task, "contract_version": contract["version"],
              "tree": tree, "generated_at": utc_now(), "analysis": analysis, "failures": failures,
              "verdict": "pass" if not failures else "block"}
    report_path = state_dir(repo, args.task) / "settle-gate.json"
    atomic_write_json(report_path, report)
    if failures:
        print("GPT Blackbox Lite SETTLE GATE: BLOCKED")
        for failure in failures:
            print(f"  BLOCK {failure}")
        return 2
    queue = load_settle_queue(repo)
    queue["items"][args.task].update(
        {"status": "confirmed", "settled_at": utc_now(), "settle_report": str(report_path)}
    )
    save_settle_queue(repo, queue)
    print("GPT Blackbox Lite SETTLE GATE: PASS")
    print(f"  tree: {tree}")
    print(f"  report: {report_path}")
    return 0


def command_supersede(args: argparse.Namespace) -> int:
    if not args.user_approved:
        raise BlackboxError(
            "task supersession requires --user-approved after explicit owner approval"
        )
    if not args.reason.strip():
        raise BlackboxError("supersession reason cannot be empty")
    repo = resolve_repo(args.repo)
    task_id = validate_task_id(args.task)
    successor_id = validate_task_id(args.by)
    if task_id == successor_id:
        raise BlackboxError("a task cannot supersede itself")
    path = supersession_path(repo, task_id)
    if path.exists() or run_git(repo, "rev-parse", "--verify", supersession_ref(task_id), check=False).returncode == 0:
        raise BlackboxError(f"immutable supersession audit already exists: {path}")

    old_contract = load_retired_contract_for_supersession(repo, task_id)
    successor_contract = load_contract(repo, successor_id)
    old_gate = read_json(state_dir(repo, task_id) / "final-gate.json", {})
    if (
        old_gate.get("verdict") == "pass"
        and old_gate.get("contract_version") == old_contract.get("version")
    ):
        raise BlackboxError("a currently passing task does not need supersession")
    if successor_contract.get("created_at", "") < old_contract.get("created_at", ""):
        raise BlackboxError("successor task must have been created after the retired task")
    successor_gate_path = state_dir(repo, successor_id) / "final-gate.json"
    successor_gate = read_json(successor_gate_path)
    if successor_contract.get("lane", {}).get("selected") != "full":
        raise BlackboxError("successor task must use the full lane")
    if (
        successor_gate.get("verdict") != "pass"
        or successor_gate.get("completion") != "final"
        or successor_gate.get("contract_version") != successor_contract.get("version")
    ):
        raise BlackboxError("successor task needs a current passing full final gate")

    expected = retirement_obligations(old_contract)
    supplied = parse_coverage(args.coverage)
    missing = sorted(set(expected) - set(supplied))
    unexpected = sorted(set(supplied) - set(expected))
    if missing or unexpected:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if unexpected:
            detail.append("unexpected " + ", ".join(unexpected))
        raise BlackboxError("coverage must address the exact retirement obligations: " + "; ".join(detail))

    record = {
        "schema": SUPERSESSION_SCHEMA,
        "version": 2,
        "retired_task": task_id,
        "successor_task": successor_id,
        "owner_approved": True,
        "reason": args.reason.strip(),
        "created_at": utc_now(),
        "bindings": {
            "retired_contract_sha256": sha256_path(contract_path(repo, task_id)),
            "successor_contract_sha256": sha256_path(contract_path(repo, successor_id)),
            "successor_gate_sha256": sha256_path(successor_gate_path),
            "successor_reviews_sha256": sha256_path(reviews_path(repo, successor_id)),
            "successor_evidence_sha256": sha256_path(evidence_path(repo, successor_id)),
            "retired_task_ref": task_baseline_ref_oid(repo, task_id),
            "successor_task_ref": task_baseline_ref_oid(repo, successor_id),
            "successor_tree": successor_gate["current_tree"],
        },
        "coverage": [
            {"id": obligation_id, "description": description, "resolution": supplied[obligation_id]}
            for obligation_id, description in expected.items()
        ],
    }
    atomic_write_json(path, record)
    audit_blob = run_git(repo, "hash-object", "-w", str(path)).stdout.strip()
    run_git(repo, "update-ref", supersession_ref(task_id), audit_blob)
    valid, failures = has_valid_supersession(repo, task_id)
    if not valid:
        run_git(repo, "update-ref", "-d", supersession_ref(task_id), check=False)
        path.unlink(missing_ok=True)
        raise BlackboxError("supersession audit failed self-validation: " + "; ".join(failures))
    print(f"GPT Blackbox Lite SUPERSESSION PASS: {task_id} -> {successor_id}")
    print(f"  audit: {path}")
    return 0


def legacy_fast_closure_failures(
    state: dict[str, Any],
    closure: dict[str, Any],
    commits: list[str],
) -> list[str]:
    failures: list[str] = []
    audit_head = state.get("checkpoint_head")
    required_checks = closure.get("required_checks")
    checks = closure.get("checks")
    reviews = closure.get("reviews")
    if (
        not isinstance(required_checks, list)
        or not required_checks
        or not all(isinstance(check_id, str) and check_id for check_id in required_checks)
        or len(required_checks) != len(set(required_checks))
    ):
        failures.append("legacy closure required-check ledger is absent or malformed")
        required_checks = []
    if not isinstance(checks, dict):
        failures.append("legacy closure check evidence is absent or malformed")
        checks = {}
    for check_id in required_checks:
        check = checks.get(check_id)
        if not isinstance(check, dict) or check.get("status") != "pass":
            failures.append(f"legacy closure check {check_id!r} is not passing")
            continue
        if check.get("head") != audit_head:
            failures.append(f"legacy closure check {check_id!r} is bound to another head")
    if not isinstance(reviews, dict):
        failures.append("legacy closure reviews are absent or malformed")
        reviews = {}
    for role in ROLES:
        review = reviews.get(role)
        if not isinstance(review, dict) or review.get("verdict") != "pass":
            failures.append(f"legacy closure lacks a passing review from {role}")
            continue
        if review.get("head") != audit_head or review.get("commit_oids") != commits:
            failures.append(f"legacy closure review from {role} names another range")
    for current_only in ("manifest", "skipped_checks", "promoted_from_diagnosis"):
        if current_only in closure:
            failures.append(
                f"legacy closure unexpectedly contains current-format field {current_only!r}"
            )
    return failures


def fast_session_failures(repo: Path, session_file: Path) -> list[str]:
    failures: list[str] = []
    try:
        state = validate_fast_lane_state(repo, read_json(session_file), session_file)
        session_id = state["session_id"]
        if session_file.stem != session_id:
            failures.append(
                f"state filename {session_file.name} does not match session {session_id}"
            )
        legacy_metadata = fast_lane_uses_legacy_checkpoint_metadata(state)
        commits = validate_fast_range(
            repo,
            state,
            require_current_head=False,
            allow_legacy_sealed=legacy_metadata,
        )
    except BlackboxError as exc:
        return [str(exc)]
    if state.get("status") != "sealed":
        failures.append(f"status={state.get('status', 'invalid')}")
        return failures
    closure = state.get("closure")
    seal = state.get("seal")
    if not isinstance(closure, dict):
        failures.append("sealed session has no closure record")
        closure = {}
    if not isinstance(seal, dict):
        failures.append("sealed session has no seal record")
        seal = {}
    if (closure.get("audit_head"), closure.get("commit_oids")) != (
        state.get("checkpoint_head"),
        commits,
    ):
        failures.append("closure is not bound to the exact recorded head and checkpoints")
    if legacy_metadata:
        failures.extend(legacy_fast_closure_failures(state, closure, commits))
    report_path = fast_gate_report_path(repo, session_id)
    try:
        expected_root = (fast_lane_root(repo) / "evidence").resolve()
        report_path.resolve().relative_to(expected_root)
    except ValueError:
        failures.append("gate report path escapes Fast Lane evidence storage")
    if seal.get("report") != str(report_path):
        failures.append("seal names another gate report path")
    if not report_path.is_file():
        failures.append(f"gate report is missing: {report_path}")
        report: dict[str, Any] = {}
        digest = None
    else:
        digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        try:
            report = read_json(report_path)
        except BlackboxError as exc:
            failures.append(str(exc))
            report = {}
    if digest is not None and seal.get("sha256") != digest:
        failures.append("gate report SHA-256 does not match the seal")
    if legacy_metadata:
        expected_report_fields = {
            "schema": FAST_LANE_SCHEMA,
            "session_id": session_id,
            "base_commit": state.get("base_commit"),
            "head": state.get("checkpoint_head"),
            "commit_oids": commits,
            "checks": closure.get("checks"),
            "reviews": closure.get("reviews"),
            "verdict": "pass",
        }
        for current_only in ("checkpoint_oids", "checkpoints", "manifest"):
            if current_only in report:
                failures.append(
                    f"legacy gate report unexpectedly contains current-format field {current_only!r}"
                )
    else:
        expected_report_fields = {
            "schema": FAST_LANE_SCHEMA,
            "session_id": session_id,
            "base_commit": state.get("base_commit"),
            "head": state.get("checkpoint_head"),
            "checkpoint_oids": commits,
            "checkpoints": state.get("commits"),
            "manifest": closure.get("manifest"),
            "checks": closure.get("checks"),
            "reviews": closure.get("reviews"),
            "verdict": "pass",
        }
    for field, expected in expected_report_fields.items():
        if report.get(field) != expected:
            failures.append(f"gate report field '{field}' is absent, stale, or altered")
    if report.get("failures") != []:
        failures.append("gate report retains unresolved failures")
    tag = f"fast-lane-{session_id}-sealed"
    if seal.get("tag") != tag or seal.get("head") != state.get("checkpoint_head"):
        failures.append("seal names another tag or head")
    tag_ref = f"refs/tags/{tag}"
    object_type = run_git(repo, "cat-file", "-t", tag_ref, check=False)
    peeled = run_git(repo, "rev-parse", "--verify", f"{tag_ref}^{{}}", check=False)
    contents = run_git(
        repo, "for-each-ref", "--format=%(contents)", tag_ref, check=False
    )
    if object_type.returncode != 0 or object_type.stdout.strip() != "tag":
        failures.append("seal tag is missing or is not annotated")
    if peeled.returncode != 0 or peeled.stdout.strip() != state.get("checkpoint_head"):
        failures.append("seal tag is missing or targets another commit")
    annotation = contents.stdout
    required_annotation = (
        f"Base: {state.get('base_commit')}",
        f"Head: {state.get('checkpoint_head')}",
        f"Gate-SHA256: {digest}",
    )
    for marker in required_annotation:
        if marker not in annotation:
            failures.append(f"seal annotation is missing {marker!r}")
    return list(dict.fromkeys(failures))

def command_release_gate(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    queue = load_settle_queue(repo)
    superseded: set[str] = set()
    invalid_supersession_tasks: set[str] = set()
    invalid_supersessions: list[str] = []
    state_root = absolute_git_dir(repo) / "gpt-blackbox-lite"
    for audit_file in state_root.glob("*/supersession.json"):
        task_id = audit_file.parent.name
        valid, failures = has_valid_supersession(repo, task_id)
        if valid:
            superseded.add(task_id)
        else:
            invalid_supersession_tasks.add(task_id)
            invalid_supersessions.extend(
                f"{task_id}: invalid supersession audit: {failure}" for failure in failures
            )
    unresolved = [
        x for x in queue.get("items", {}).values()
        if x.get("status") != "confirmed" and x.get("task_id") not in superseded
    ]
    unfinished: list[str] = []
    legacy_retired: list[str] = []
    for contract_file in state_root.glob("*/contract.json"):
        contract = read_json(contract_file)
        report_file = contract_file.with_name("final-gate.json")
        report = read_json(report_file, {})
        task_id = contract["task_id"]
        if task_id in superseded:
            continue
        if task_id in invalid_supersession_tasks:
            continue
        if report.get("verdict") != "pass" or report.get("contract_version") != contract["version"]:
            retirement = legacy_retirement_failure(
                repo, contract_file, contract, report_file, report
            )
            if retirement == "":
                legacy_retired.append(task_id)
            elif retirement is None:
                unfinished.append(f"{task_id}: current contract has no passing final gate")
            else:
                unfinished.append(
                    f"{task_id}: invalid legacy retirement record: {retirement}"
                )
        elif contract.get("lane", {}).get("selected") in CHEAP_LANES and task_id not in queue.get("items", {}):
            unfinished.append(f"{task_id}: cheap gate has no settle queue entry")
    fast_failures: list[str] = []
    fast_sessions = sorted((fast_lane_root(repo) / "sessions").glob("*.json"))
    for session_file in fast_sessions:
        for failure in fast_session_failures(repo, session_file):
            fast_failures.append(f"Fast Lane {session_file.stem}: {failure}")
    if unfinished or unresolved or invalid_supersessions or fast_failures:
        print("GPT Blackbox Lite RELEASE GATE: BLOCKED")
        for failure in sorted(unfinished):
            print(f"  BLOCK {failure}")
        for failure in sorted(invalid_supersessions):
            print(f"  BLOCK {failure}")
        for failure in sorted(fast_failures):
            print(f"  BLOCK {failure}")
        for item in sorted(unresolved, key=lambda value: value["task_id"]):
            print(f"  BLOCK {item['task_id']}: lane={item['lane']} status={item['status']} tree={item['tree']}")
        return 2
    print(
        "GPT Blackbox Lite RELEASE GATE: PASS "
        f"({len(queue.get('items', {}))} settled, {len(superseded)} superseded, "
        f"{len(legacy_retired)} legacy retired, "
        f"{len(fast_sessions)} Fast Lane sealed)"
    )
    for task_id in sorted(legacy_retired):
        print(
            f"  LEGACY RETIRED {task_id}: preserved failed gate plus "
            "owner-approved hash-bound retirement"
        )
    return 0

def command_status(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    contract = load_contract(repo, args.task)
    reviews = read_json(reviews_path(repo, args.task), {"items": []})
    evidence = read_json(evidence_path(repo, args.task), {"checks": {}})
    queue_item = load_settle_queue(repo).get("items", {}).get(args.task)
    supersession = read_json(supersession_path(repo, args.task), None) if supersession_path(repo, args.task).exists() else None
    print(json.dumps({"contract": contract, "reviews": reviews, "evidence": evidence,
                      "settle": queue_item, "supersession": supersession}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preservation-first Git task gates plus owner-operated Fast Lane "
            "checkpoint and sealed closure"
        )
    )
    parser.add_argument("--repo", help="repository path (defaults to current directory)")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    start = subparsers.add_parser("start", help="capture the exact pre-edit baseline")
    start.add_argument("--task", required=True)
    start.add_argument("--goal", required=True)
    start.add_argument("--allow", action="append", required=True)
    start.add_argument(
        "--exclude",
        action="append",
        help="owner-controlled path boundary that task-local discovery may never cross",
    )
    start.add_argument("--protect", action="append")
    start.add_argument(
        "--artifact-class",
        action="append",
        help="repeat as path-pattern=artifact-class for typed non-source evidence",
    )
    start.add_argument("--risk-domain", action="append", choices=RISK_DOMAINS)
    start.add_argument("--require", action="append")
    start.add_argument("--require-browser", action="store_true")
    start.add_argument("--lane", choices=LANES)
    start.add_argument("--task-kind", default="code-change")
    start.add_argument("--max-lines", type=int)
    start.add_argument("--max-files", type=int)
    start.add_argument("--interface-change", action="store_true")
    start.set_defaults(handler=command_start)

    amend = subparsers.add_parser("amend", help="expand the contract after user approval")
    amend.add_argument("--task", required=True)
    amend.add_argument("--allow", action="append")
    amend.add_argument("--allow-delete", action="append")
    amend.add_argument("--allow-rewrite", action="append")
    amend.add_argument("--protect", action="append")
    amend.add_argument("--artifact-class", action="append")
    amend.add_argument("--reason", required=True)
    amend.add_argument("--user-approved", action="store_true")
    amend.set_defaults(handler=command_amend)

    discover = subparsers.add_parser(
        "discover",
        help="add one exact in-goal dependency without undoing work or granting delete/rewrite authority",
    )
    discover.add_argument("--task", required=True)
    discover.add_argument("--path", required=True)
    discover.add_argument("--reason", required=True)
    discover.set_defaults(handler=command_discover)

    review = subparsers.add_parser("review", help="record one tree-bound reasoning verdict")
    review.add_argument("--task", required=True)
    review.add_argument("--stage", choices=REVIEW_STAGES, required=True)
    review.add_argument("--role", choices=ROLES, required=True)
    review.add_argument("--verdict", choices=VERDICTS, required=True)
    review.add_argument("--summary", required=True)
    review.set_defaults(handler=command_review)

    preflight = subparsers.add_parser("preflight", help="require all plan verdicts before editing")
    preflight.add_argument("--task", required=True)
    preflight.set_defaults(handler=command_preflight)

    inspect = subparsers.add_parser("inspect", help="analyze the actual diff against the baseline")
    inspect.add_argument("--task", required=True)
    inspect.set_defaults(handler=command_inspect)

    run = subparsers.add_parser("run", help="run and record a deterministic tree-bound check")
    run.add_argument("--task", required=True)
    run.add_argument("--check", required=True)
    run.add_argument(
        "--timeout",
        type=positive_seconds,
        default=None,
        help="optional explicit deadline in seconds; healthy commands have no implicit deadline",
    )
    run.add_argument(
        "--stall-timeout",
        type=positive_seconds,
        default=None,
        help="optional explicit no-output deadline; disabled unless the caller selects it",
    )
    run.add_argument(
        "--heartbeat",
        type=positive_seconds,
        default=10.0,
        help="progress-report interval in seconds; does not stop the command (default: 10)",
    )
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(handler=command_run)

    record = subparsers.add_parser("record", help="record browser or manual evidence")
    record.add_argument("--task", required=True)
    record.add_argument("--check", required=True)
    record.add_argument("--kind", choices=("browser", "manual", "other"), required=True)
    record.add_argument("--status", choices=("pass", "fail", "block"), required=True)
    record.add_argument("--summary", required=True)
    record.add_argument("--artifact", action="append")
    record.set_defaults(handler=command_record)

    gate = subparsers.add_parser("gate", help="issue the final mechanical pass/block verdict")
    gate.add_argument("--task", required=True)
    gate.set_defaults(handler=command_gate)

    settle_gate = subparsers.add_parser(
        "settle-gate",
        help="confirm one provisional cheap-lane tree after full-depth settle reviews",
    )
    settle_gate.add_argument("--task", required=True)
    settle_gate.set_defaults(handler=command_settle_gate)

    supersede = subparsers.add_parser(
        "supersede",
        help="retire one unfinished task through an owner-approved, hash-bound successor audit",
    )
    supersede.add_argument("--task", required=True, help="unfinished task to retire")
    supersede.add_argument("--by", required=True, help="later full-lane task with a passing final gate")
    supersede.add_argument("--reason", required=True)
    supersede.add_argument(
        "--coverage",
        action="append",
        help="repeat as obligation-id=concrete resolution evidence",
    )
    supersede.add_argument("--user-approved", action="store_true")
    supersede.set_defaults(handler=command_supersede)

    release_gate = subparsers.add_parser(
        "release-gate",
        help="block release while any cheap-lane settle obligation is unresolved",
    )
    release_gate.set_defaults(handler=command_release_gate)

    status = subparsers.add_parser("status", help="print contract, reviews, and evidence")
    status.add_argument("--task", required=True)
    status.set_defaults(handler=command_status)

    fast_start = subparsers.add_parser(
        "fast-start", help="start an explicitly owner-authorized Fast Lane session"
    )
    fast_start.add_argument("--date", required=True, help="current DDMMYYYY date")
    fast_start.add_argument("--timezone", default="Europe/Kiev")
    fast_start.add_argument("--instruction", required=True)
    fast_start.add_argument("--user-approved", action="store_true")
    fast_start.set_defaults(handler=command_fast_start)

    fast_status = subparsers.add_parser(
        "fast-status", help="print current Fast Lane state and runtime consistency"
    )
    fast_status.set_defaults(handler=command_fast_status)

    fast_commit = subparsers.add_parser(
        "fast-commit", help="record one owner instruction as an immutable checkpoint"
    )
    fast_commit.add_argument("--instruction", required=True)
    fast_commit.add_argument("--summary", required=True)
    fast_commit.add_argument("--external-command", action="append")
    fast_commit.add_argument("--external-result")
    fast_commit.add_argument("--external-rollback")
    fast_commit.add_argument(
        "--external-user-approved",
        action="store_true",
        help="attest that the current owner prompt authorizes the exact external mutation",
    )
    fast_commit.set_defaults(handler=command_fast_commit)

    fast_diagnose = subparsers.add_parser(
        "fast-diagnose",
        help="run every applicable tracked diagnostic check without fail-fast",
    )
    fast_diagnose.add_argument("--manifest", default=FAST_LANE_MANIFEST_FILENAME)
    fast_diagnose.set_defaults(handler=command_fast_diagnose)

    fast_review = subparsers.add_parser(
        "fast-diagnostic-review",
        help="record one sequential exact-diagnosis reviewer verdict",
    )
    fast_review.add_argument("--role", choices=ROLES, required=True)
    fast_review.add_argument("--verdict", choices=VERDICTS, required=True)
    fast_review.add_argument("--summary", required=True)
    fast_review.set_defaults(handler=command_fast_diagnostic_review)

    fast_finish = subparsers.add_parser(
        "fast-finish", help="promote passing diagnosis into exact-head closure"
    )
    fast_finish.set_defaults(handler=command_fast_finish)

    fast_gate = subparsers.add_parser(
        "fast-gate", help="hash, tag, and seal the exact Fast Lane range"
    )
    fast_gate.set_defaults(handler=command_fast_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BlackboxError as exc:
        print(f"GPT Blackbox Lite ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("GPT Blackbox Lite interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
