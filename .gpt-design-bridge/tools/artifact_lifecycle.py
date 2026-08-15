#!/usr/bin/env python3
"""Report, preview, and safely prune explicitly registered reproducible artifacts."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


POLICY_SCHEMA = "gpt-artifact-lifecycle-policy"
MARKER_SCHEMA = "gpt-reproducible-artifact"
REPORT_SCHEMA = "gpt-artifact-lifecycle-report"
PREVIEW_SCHEMA = "gpt-artifact-lifecycle-preview"
RECEIPT_SCHEMA = "gpt-artifact-lifecycle-receipt"
MARKER_NAME = ".gpt-artifact.json"
ARTIFACT_CLASSES = (
    "generated-preview",
    "reproducible-build",
    "staging",
    "temporary-extraction",
)
ARTIFACT_STATES = ("current", "previous", "obsolete", "temporary")
DEFAULT_PROTECTED_PATTERNS = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.key",
    "**/*.key",
    "*.pem",
    "**/*.pem",
    "*.pfx",
    "**/*.pfx",
    "*.db",
    "**/*.db",
    "*.sqlite",
    "**/*.sqlite",
    "*.sqlite3",
    "**/*.sqlite3",
    "travelling-data-export*",
    "**/travelling-data-export*",
    "RETURN-NOTE.md",
    "**/RETURN-NOTE.md",
    "contract-additions.json",
    "**/contract-additions.json",
    "proof.json",
    "**/proof.json",
    "proof.partial.json",
    "**/proof.partial.json",
)


class LifecycleError(RuntimeError):
    """A fail-loud artifact lifecycle refusal."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git returned no diagnostic"
        raise LifecycleError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return completed


def resolve_repo(value: str | None) -> Path:
    candidate = Path(value or os.getcwd()).resolve()
    completed = run_git(candidate, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode != 0:
        raise LifecycleError(f"{candidate} is not inside a Git repository")
    return Path(completed.stdout.strip()).resolve()


def normalize_repo_relative(value: str, label: str) -> str:
    text = value.strip().replace("\\", "/")
    if not text:
        raise LifecycleError(f"{label} cannot be empty")
    if text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        raise LifecycleError(f"{label} must be repository-relative: {value}")
    if any(part in {"", ".", ".."} for part in text.split("/")):
        raise LifecycleError(f"{label} must be a normalized child path: {value}")
    if any(character in text for character in "*?["):
        raise LifecycleError(f"{label} cannot contain a wildcard: {value}")
    return text


def descendant(root: Path, candidate: Path, label: str, *, allow_equal: bool = False) -> Path:
    root = root.resolve()
    candidate = candidate.resolve()
    if candidate == root and allow_equal:
        return candidate
    if not candidate.is_relative_to(root):
        raise LifecycleError(f"{label} escapes {root}: {candidate}")
    if candidate == root:
        raise LifecycleError(f"{label} may not be the repository or registered root itself: {candidate}")
    return candidate


def is_reparse_or_link(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def scan_tree(candidate: Path) -> dict[str, Any]:
    """Hash a directory without following links, junctions, or reparse points."""
    if is_reparse_or_link(candidate):
        raise LifecycleError(f"candidate root is a link or reparse point: {candidate}")
    if not candidate.is_dir():
        raise LifecycleError(f"candidate is not a directory: {candidate}")
    digest = hashlib.sha256()
    total_bytes = 0
    files: list[str] = []
    directories: list[str] = []

    def visit(directory: Path) -> None:
        nonlocal total_bytes
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        for entry in entries:
            entry_path = Path(entry.path)
            relative = entry_path.relative_to(candidate).as_posix()
            if entry.is_symlink() or is_reparse_or_link(entry_path):
                raise LifecycleError(
                    f"candidate contains a link or reparse point at {relative}: {entry_path}"
                )
            if entry.is_dir(follow_symlinks=False):
                directories.append(relative)
                digest.update(b"D\0" + relative.encode("utf-8", errors="surrogateescape") + b"\0")
                visit(entry_path)
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                total_bytes += size
                files.append(relative)
                digest.update(b"F\0" + relative.encode("utf-8", errors="surrogateescape") + b"\0")
                digest.update(str(size).encode("ascii") + b"\0")
                with entry_path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            else:
                raise LifecycleError(f"candidate contains an unsupported filesystem entry: {entry_path}")

    visit(candidate)
    return {
        "tree_sha256": digest.hexdigest(),
        "bytes": total_bytes,
        "file_count": len(files),
        "directory_count": len(directories),
        "files": files,
    }


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"{label} must be a JSON object: {path}")
    return value


def load_policy(repo: Path, value: str) -> tuple[Path, dict[str, Any], str, bool, bool]:
    relative = normalize_repo_relative(value, "policy path")
    policy_path = descendant(repo, repo / relative, "policy path")
    raw = policy_path.read_bytes() if policy_path.exists() else None
    if raw is None:
        raise LifecycleError(f"artifact lifecycle policy does not exist: {policy_path}")
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot parse artifact lifecycle policy {policy_path}: {exc}") from exc
    if not isinstance(policy, dict) or (policy.get("schema"), policy.get("version")) != (
        POLICY_SCHEMA,
        1,
    ):
        raise LifecycleError(f"{policy_path} must be a {POLICY_SCHEMA} version 1 object")
    roots = policy.get("roots")
    if not isinstance(roots, list) or not roots:
        raise LifecycleError(f"{policy_path}: roots must be a non-empty array")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    normalized_roots: list[dict[str, Any]] = []
    for index, root in enumerate(roots):
        label = f"roots[{index}]"
        if not isinstance(root, dict):
            raise LifecycleError(f"{policy_path}: {label} must be an object")
        root_id = root.get("id")
        if not isinstance(root_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", root_id):
            raise LifecycleError(f"{policy_path}: {label}.id must be a stable 1-64 character ID")
        if root_id in seen_ids:
            raise LifecycleError(f"{policy_path}: duplicate root id {root_id}")
        seen_ids.add(root_id)
        root_path = normalize_repo_relative(str(root.get("path", "")), f"{label}.path")
        if root_path in seen_paths:
            raise LifecycleError(f"{policy_path}: duplicate root path {root_path}")
        seen_paths.add(root_path)
        allowed_classes = root.get("allowed_classes")
        if (
            not isinstance(allowed_classes, list)
            or not allowed_classes
            or not all(item in ARTIFACT_CLASSES for item in allowed_classes)
        ):
            raise LifecycleError(
                f"{policy_path}: {label}.allowed_classes must be a non-empty subset of {ARTIFACT_CLASSES}"
            )
        require_remote = root.get("require_remote")
        if not isinstance(require_remote, bool):
            raise LifecycleError(f"{policy_path}: {label}.require_remote must be boolean")
        remote_refs = root.get("remote_refs", [])
        if (
            not isinstance(remote_refs, list)
            or not all(isinstance(item, str) and item.strip() for item in remote_refs)
            or (require_remote and not remote_refs)
        ):
            raise LifecycleError(
                f"{policy_path}: {label}.remote_refs must be a non-empty string array when remote proof is required"
            )
        rebuild_files = root.get("rebuild_files", [])
        if not isinstance(rebuild_files, list) or not all(isinstance(item, str) for item in rebuild_files):
            raise LifecycleError(f"{policy_path}: {label}.rebuild_files must be a string array")
        rebuild_files = [
            normalize_repo_relative(item, f"{label}.rebuild_files") for item in rebuild_files
        ]
        rebuild_command = root.get("rebuild_command")
        if not isinstance(rebuild_command, str) or not rebuild_command.strip():
            raise LifecycleError(f"{policy_path}: {label}.rebuild_command must be a non-empty string")
        keep = root.get("keep_newest_obsolete", 0)
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
            raise LifecycleError(f"{policy_path}: {label}.keep_newest_obsolete must be >= 0")
        normalized_roots.append(
            {
                "id": root_id,
                "path": root_path,
                "allowed_classes": list(dict.fromkeys(allowed_classes)),
                "require_remote": require_remote,
                "remote_refs": list(dict.fromkeys(item.strip() for item in remote_refs)),
                "rebuild_files": list(dict.fromkeys(rebuild_files)),
                "rebuild_command": rebuild_command.strip(),
                "keep_newest_obsolete": keep,
            }
        )
    patterns = policy.get("protected_patterns", [])
    if not isinstance(patterns, list) or not all(isinstance(item, str) and item.strip() for item in patterns):
        raise LifecycleError(f"{policy_path}: protected_patterns must be a string array")
    normalized = {
        "schema": POLICY_SCHEMA,
        "version": 1,
        "roots": normalized_roots,
        "protected_patterns": list(dict.fromkeys([*DEFAULT_PROTECTED_PATTERNS, *patterns])),
    }
    tracked = run_git(repo, "ls-files", "--error-unmatch", "--", relative, check=False).returncode == 0
    committed = tracked and run_git(
        repo, "diff", "--quiet", "HEAD", "--", relative, check=False
    ).returncode == 0
    return policy_path, normalized, sha256_bytes(raw), tracked, committed


def read_marker(candidate: Path) -> tuple[dict[str, Any] | None, list[str]]:
    marker_path = candidate / MARKER_NAME
    if not marker_path.exists():
        return None, [f"missing required marker {MARKER_NAME}"]
    try:
        marker = read_json(marker_path, "artifact marker")
    except LifecycleError as exc:
        return None, [str(exc)]
    reasons: list[str] = []
    if (marker.get("schema"), marker.get("version")) != (MARKER_SCHEMA, 1):
        reasons.append(f"marker must be {MARKER_SCHEMA} version 1")
    artifact_class = marker.get("class")
    if artifact_class not in ARTIFACT_CLASSES:
        reasons.append(f"marker class must be one of {ARTIFACT_CLASSES}")
    state_value = marker.get("state")
    if state_value not in ARTIFACT_STATES:
        reasons.append(f"marker state must be one of {ARTIFACT_STATES}")
    source_commit = marker.get("source_commit")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", source_commit):
        reasons.append("marker source_commit must be a Git commit ID")
    created_at = marker.get("created_at")
    if not isinstance(created_at, str):
        reasons.append("marker created_at must be an ISO-8601 string")
    else:
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            reasons.append("marker created_at is not valid ISO-8601")
    return marker, reasons


def protected_matches(files: Iterable[str], patterns: Iterable[str]) -> list[str]:
    lowered_patterns = [pattern.replace("\\", "/").casefold() for pattern in patterns]
    matches: list[str] = []
    for value in files:
        lowered = value.casefold()
        name = Path(value).name.casefold()
        if any(fnmatch.fnmatchcase(lowered, pattern) or fnmatch.fnmatchcase(name, pattern) for pattern in lowered_patterns):
            matches.append(value)
    return matches


def resolve_commit(repo: Path, value: str) -> str | None:
    completed = run_git(repo, "rev-parse", "--verify", f"{value}^{{commit}}", check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def commit_contains_rebuild_files(repo: Path, commit: str, files: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for value in files:
        if run_git(repo, "cat-file", "-e", f"{commit}:{value}", check=False).returncode != 0:
            missing.append(value)
    return missing


def remote_proof(repo: Path, commit: str, refs: Iterable[str]) -> tuple[bool, list[str]]:
    containing: list[str] = []
    for ref in refs:
        if run_git(repo, "rev-parse", "--verify", ref, check=False).returncode != 0:
            continue
        if run_git(repo, "merge-base", "--is-ancestor", commit, ref, check=False).returncode == 0:
            containing.append(ref)
    return bool(containing), containing


def candidate_report(
    repo: Path,
    root_config: dict[str, Any],
    candidate: Path,
    patterns: list[str],
) -> dict[str, Any]:
    relative = candidate.relative_to(repo).as_posix()
    result: dict[str, Any] = {
        "root_id": root_config["id"],
        "path": relative,
        "name": candidate.name,
        "decision": "retain",
        "reasons": [],
        "bytes": 0,
        "file_count": 0,
        "directory_count": 0,
        "tree_sha256": None,
    }
    try:
        snapshot = scan_tree(candidate)
    except (OSError, LifecycleError) as exc:
        result["reasons"].append(f"filesystem safety check failed: {exc}")
        return result
    result.update({key: snapshot[key] for key in ("tree_sha256", "bytes", "file_count", "directory_count")})
    marker, marker_reasons = read_marker(candidate)
    result["reasons"].extend(marker_reasons)
    if marker is None or marker_reasons:
        return result
    result["marker"] = marker
    if marker["class"] not in root_config["allowed_classes"]:
        result["reasons"].append(
            f"artifact class {marker['class']} is not allowed for registered root {root_config['id']}"
        )
    if marker["state"] in {"current", "previous"}:
        result["reasons"].append(f"artifact state {marker['state']} is retained for deployment rollback")
    sensitive = protected_matches(snapshot["files"], patterns)
    if sensitive:
        result["protected_files"] = sensitive
        result["reasons"].append(
            f"candidate contains {len(sensitive)} protected data, secret, designer, or evidence file(s)"
        )
    commit = resolve_commit(repo, marker["source_commit"])
    if commit is None:
        result["reasons"].append(
            f"source commit {marker['source_commit']} does not exist locally, so reconstruction is unproved"
        )
    else:
        result["resolved_source_commit"] = commit
        missing_rebuild = commit_contains_rebuild_files(repo, commit, root_config["rebuild_files"])
        if missing_rebuild:
            result["reasons"].append(
                "source commit lacks required rebuild file(s): " + ", ".join(missing_rebuild)
            )
        if root_config["require_remote"]:
            verified, containing = remote_proof(repo, commit, root_config["remote_refs"])
            result["remote_containing_refs"] = containing
            if not verified:
                result["reasons"].append(
                    f"source commit {commit} is not contained by any configured remote ref: "
                    + ", ".join(root_config["remote_refs"])
                )
    if not result["reasons"]:
        result["decision"] = "delete"
        result["reasons"].append(
            "marked obsolete or temporary reproducible artifact has verified local and remote reconstruction"
        )
    return result


def build_report(repo: Path, policy_value: str) -> dict[str, Any]:
    policy_path, policy, policy_sha, policy_tracked, policy_committed = load_policy(repo, policy_value)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "version": 1,
        "generated_at": utc_now(),
        "repo_root": str(repo),
        "head": run_git(repo, "rev-parse", "HEAD").stdout.strip(),
        "policy_path": policy_path.relative_to(repo).as_posix(),
        "policy_sha256": policy_sha,
        "policy_tracked": policy_tracked,
        "policy_committed": policy_committed,
        "roots": [],
        "candidates": [],
        "delete_bytes": 0,
        "retained_bytes": 0,
    }
    for root_config in policy["roots"]:
        root_path = descendant(repo, repo / root_config["path"], f"registered root {root_config['id']}")
        root_result = {
            "id": root_config["id"],
            "path": root_config["path"],
            "exists": root_path.exists(),
            "candidate_count": 0,
            "diagnostics": [],
        }
        report["roots"].append(root_result)
        if not root_path.exists():
            root_result["diagnostics"].append(f"registered root does not exist: {root_path}")
            continue
        try:
            if is_reparse_or_link(root_path):
                raise LifecycleError(f"registered root is a link or reparse point: {root_path}")
            if not root_path.is_dir():
                raise LifecycleError(f"registered root is not a directory: {root_path}")
        except (OSError, LifecycleError) as exc:
            root_result["diagnostics"].append(str(exc))
            continue
        candidates: list[dict[str, Any]] = []
        for child in sorted(root_path.iterdir(), key=lambda item: item.name.casefold()):
            if not child.is_dir() or child.is_symlink():
                candidates.append(
                    {
                        "root_id": root_config["id"],
                        "path": child.relative_to(repo).as_posix(),
                        "name": child.name,
                        "decision": "retain",
                        "reasons": ["registered roots prune marked direct child directories only"],
                        "bytes": child.stat().st_size if child.is_file() else 0,
                        "file_count": 1 if child.is_file() else 0,
                        "directory_count": 0,
                        "tree_sha256": None,
                    }
                )
                continue
            candidates.append(candidate_report(repo, root_config, child, policy["protected_patterns"]))

        eligible_obsolete = [
            item
            for item in candidates
            if item["decision"] == "delete" and item.get("marker", {}).get("state") == "obsolete"
        ]
        eligible_obsolete.sort(
            key=lambda item: (item["marker"]["created_at"], item["name"]), reverse=True
        )
        for item in eligible_obsolete[: root_config["keep_newest_obsolete"]]:
            item["decision"] = "retain"
            item["reasons"] = [
                f"retained by keep_newest_obsolete={root_config['keep_newest_obsolete']} policy"
            ]
        root_result["candidate_count"] = len(candidates)
        report["candidates"].extend(candidates)

    if not policy_tracked or not policy_committed:
        for candidate in report["candidates"]:
            if candidate["decision"] == "delete":
                candidate["decision"] = "retain"
                candidate["reasons"] = [
                    f"lifecycle policy {report['policy_path']} is not tracked and unchanged from HEAD; prune authority is unsealed"
                ]
    for candidate in report["candidates"]:
        field = "delete_bytes" if candidate["decision"] == "delete" else "retained_bytes"
        report[field] += candidate.get("bytes", 0)
    return report


def preview_value(report: dict[str, Any]) -> dict[str, Any]:
    deletions = [
        {
            "root_id": item["root_id"],
            "path": item["path"],
            "tree_sha256": item["tree_sha256"],
            "bytes": item["bytes"],
            "file_count": item["file_count"],
            "directory_count": item["directory_count"],
            "source_commit": item["resolved_source_commit"],
            "class": item["marker"]["class"],
            "state": item["marker"]["state"],
        }
        for item in report["candidates"]
        if item["decision"] == "delete"
    ]
    preview = {
        "schema": PREVIEW_SCHEMA,
        "version": 1,
        "generated_at": utc_now(),
        "repo_root": report["repo_root"],
        "head_at_preview": report["head"],
        "policy_path": report["policy_path"],
        "policy_sha256": report["policy_sha256"],
        "delete_bytes": sum(item["bytes"] for item in deletions),
        "deletions": deletions,
        "retained": [
            {"path": item["path"], "bytes": item["bytes"], "reasons": item["reasons"]}
            for item in report["candidates"]
            if item["decision"] == "retain"
        ],
    }
    preview["preview_sha256"] = sha256_bytes(canonical_json(preview))
    return preview


def verify_preview_hash(preview: dict[str, Any]) -> str:
    supplied = preview.get("preview_sha256")
    unsigned = dict(preview)
    unsigned.pop("preview_sha256", None)
    actual = sha256_bytes(canonical_json(unsigned))
    if not isinstance(supplied, str) or supplied != actual:
        raise LifecycleError(
            f"preview hash mismatch: recorded {supplied!r}, calculated {actual}; refuse prune"
        )
    return actual


def prune(repo: Path, preview_path: Path, confirmation: str, receipt_path: Path) -> dict[str, Any]:
    preview = read_json(preview_path, "prune preview")
    if (preview.get("schema"), preview.get("version")) != (PREVIEW_SCHEMA, 1):
        raise LifecycleError(f"{preview_path} must be a {PREVIEW_SCHEMA} version 1 document")
    preview_hash = verify_preview_hash(preview)
    if confirmation != preview_hash:
        raise LifecycleError(
            f"--preview-sha256 does not match {preview_path}: expected {preview_hash}, received {confirmation}"
        )
    if Path(preview.get("repo_root", "")).resolve() != repo:
        raise LifecycleError(
            f"preview belongs to {preview.get('repo_root')}, not requested repository {repo}"
        )
    current = build_report(repo, preview["policy_path"])
    if not current["policy_tracked"] or not current["policy_committed"]:
        raise LifecycleError(
            f"policy {current['policy_path']} is not tracked and unchanged from HEAD; prune is unauthorized"
        )
    if current["policy_sha256"] != preview["policy_sha256"]:
        raise LifecycleError(
            f"policy changed after preview: expected {preview['policy_sha256']}, received {current['policy_sha256']}"
        )
    current_by_path = {item["path"]: item for item in current["candidates"]}
    exact: list[dict[str, Any]] = []
    for planned in preview.get("deletions", []):
        item = current_by_path.get(planned.get("path"))
        if item is None:
            raise LifecycleError(f"preview candidate no longer exists in the registered report: {planned.get('path')}")
        if item["decision"] != "delete":
            raise LifecycleError(
                f"preview candidate is no longer eligible: {item['path']}; " + "; ".join(item["reasons"])
            )
        for field in ("root_id", "tree_sha256", "bytes", "file_count", "directory_count"):
            if item.get(field) != planned.get(field):
                raise LifecycleError(
                    f"preview candidate changed at {item['path']}: {field} expected {planned.get(field)!r}, received {item.get(field)!r}"
                )
        exact.append(item)

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "version": 1,
        "status": "running",
        "started_at": utc_now(),
        "repo_root": str(repo),
        "policy_path": current["policy_path"],
        "policy_sha256": current["policy_sha256"],
        "preview_path": str(preview_path),
        "preview_sha256": preview_hash,
        "planned_bytes": sum(item["bytes"] for item in exact),
        "removed": [],
    }
    try:
        for item in exact:
            target = descendant(repo, repo / item["path"], "prune candidate")
            scan = scan_tree(target)
            if scan["tree_sha256"] != item["tree_sha256"]:
                raise LifecycleError(
                    f"candidate changed immediately before deletion: {target}; expected {item['tree_sha256']}, received {scan['tree_sha256']}"
                )
            shutil.rmtree(target)
            if target.exists():
                raise LifecycleError(f"candidate still exists after removal attempt: {target}")
            receipt["removed"].append(
                {
                    "path": item["path"],
                    "tree_sha256": item["tree_sha256"],
                    "bytes": item["bytes"],
                    "removed_at": utc_now(),
                }
            )
        receipt["status"] = "pass"
    except Exception as exc:
        receipt["status"] = "fail"
        receipt["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        receipt["completed_at"] = utc_now()
        receipt["removed_bytes"] = sum(item["bytes"] for item in receipt["removed"])
        receipt["remaining_planned_bytes"] = receipt["planned_bytes"] - receipt["removed_bytes"]
        atomic_write_json(receipt_path, receipt)
    return receipt


def template() -> dict[str, Any]:
    return {
        "policy": {
            "schema": POLICY_SCHEMA,
            "version": 1,
            "roots": [
                {
                    "id": "builds",
                    "path": ".artifacts/builds",
                    "allowed_classes": ["reproducible-build", "staging"],
                    "require_remote": True,
                    "remote_refs": ["refs/remotes/origin/main"],
                    "rebuild_files": ["package.json", "package-lock.json"],
                    "rebuild_command": "npm ci && npm run build",
                    "keep_newest_obsolete": 0,
                }
            ],
            "protected_patterns": ["designer-rounds/**", "evidence/**"],
        },
        "candidate_marker": {
            "schema": MARKER_SCHEMA,
            "version": 1,
            "class": "reproducible-build",
            "state": "obsolete",
            "source_commit": "<full Git commit ID>",
            "created_at": "<ISO-8601 timestamp>",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="repository path; defaults to the current Git repository")
    parser.add_argument(
        "--policy",
        default=".gpt-artifact-lifecycle.json",
        help="tracked repository-relative policy path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report", help="classify registered direct-child artifacts")
    report.add_argument("--output", help="optional report JSON path")

    preview = subparsers.add_parser("preview", help="write an exact no-change prune preview")
    preview.add_argument("--output", required=True, help="preview JSON path")

    prune_parser = subparsers.add_parser("prune", help="apply one unchanged preview")
    prune_parser.add_argument("--preview", required=True)
    prune_parser.add_argument("--preview-sha256", required=True)
    prune_parser.add_argument("--receipt", required=True)
    prune_parser.add_argument("--apply", action="store_true")

    subparsers.add_parser("template", help="print example policy and candidate marker JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "template":
        print(json.dumps(template(), indent=2))
        return 0
    repo = resolve_repo(args.repo)
    if args.command == "report":
        value = build_report(repo, args.policy)
        if args.output:
            atomic_write_json(Path(args.output), value)
        print(json.dumps(value, indent=2))
        return 0
    if args.command == "preview":
        report = build_report(repo, args.policy)
        value = preview_value(report)
        output = Path(args.output).resolve()
        for item in value["deletions"]:
            target = (repo / item["path"]).resolve()
            if output == target or output.is_relative_to(target):
                raise LifecycleError(f"preview output cannot be inside a deletion candidate: {output}")
        atomic_write_json(output, value)
        print(json.dumps(value, indent=2))
        return 0
    if args.command == "prune":
        if not args.apply:
            raise LifecycleError("prune requires --apply plus the exact --preview-sha256")
        value = prune(
            repo,
            Path(args.preview).resolve(),
            args.preview_sha256,
            Path(args.receipt).resolve(),
        )
        print(json.dumps(value, indent=2))
        return 0
    raise LifecycleError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LifecycleError as exc:
        print(f"GPT Artifact Lifecycle ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
