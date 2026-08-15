"""Deterministic and hostile-input-safe artifact primitives."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from bridge_core import (
    BridgeError,
    canonical_json,
    promote_directory,
    safe_relative_path,
    sha256_file,
)


MANIFEST_SCHEMA = "gpt-design-bridge/tree-manifest/v1"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_ARCHIVE_FILES = 20_000
MAX_ARCHIVE_FILE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000.0
TEXT_SECRET_RULES = (
    (
        "credential-assignment",
        re.compile(
            rb"(?im)^[ \t]*(?:POSTMARK_SERVER_TOKEN|CLOUDFLARE_API_TOKEN|"
            rb"DATABASE_URL|JWT_SECRET|SESSION_SECRET|API_KEY|PRIVATE_KEY)"
            rb"[ \t]*=[ \t]*[\"']?[^ \t\r\n\"']{12,}"
        ),
    ),
    (
        "authorization-bearer",
        re.compile(rb"(?im)^[ \t]*Authorization[ \t]*:[ \t]*Bearer[ \t]+[A-Za-z0-9._~+/-]{16,}"),
    ),
)
PRIVATE_KEY_PATTERN = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
SCAN_BLOCK_BYTES = 1024 * 1024
SCAN_OVERLAP_BYTES = 4096


@dataclass(frozen=True)
class ArchiveInspection:
    files: tuple[str, ...]
    wrapper_chain: tuple[str, ...]
    total_bytes: int
    limits: dict[str, int | float]

    @property
    def wrapper(self) -> str | None:
        """Backward-compatible joined wrapper path for older report readers."""
        return "/".join(self.wrapper_chain) if self.wrapper_chain else None


@dataclass(frozen=True)
class ExtractionResult:
    extraction_root: Path
    content_root: Path
    wrapper_chain: tuple[str, ...]
    files: tuple[str, ...]
    limits: dict[str, int | float]

    @property
    def wrapper(self) -> str | None:
        return "/".join(self.wrapper_chain) if self.wrapper_chain else None


@dataclass(frozen=True)
class ArchiveLimits:
    """Explicit ZIP intake limits. Defaults are safety guards, not package-size law."""

    max_members: int = MAX_ARCHIVE_FILES
    max_file_bytes: int = MAX_ARCHIVE_FILE_BYTES
    max_total_bytes: int = MAX_ARCHIVE_TOTAL_BYTES
    max_compression_ratio: float = MAX_COMPRESSION_RATIO

    def __post_init__(self) -> None:
        values = {
            "max_members": self.max_members,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_compression_ratio": self.max_compression_ratio,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise BridgeError(f"archive limit {name} must be a positive number")
        if not all(isinstance(values[name], int) for name in (
            "max_members", "max_file_bytes", "max_total_bytes"
        )):
            raise BridgeError("archive member and byte limits must be integers")

    def as_dict(self) -> dict[str, int | float]:
        return {
            "max_members": self.max_members,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_compression_ratio": self.max_compression_ratio,
        }

    @property
    def is_default(self) -> bool:
        return self == ArchiveLimits()


class ArchiveLimitError(BridgeError):
    """A safe default was exceeded; the original archive remains eligible for review."""

    def __init__(
        self,
        field: str,
        observed: int | float | str,
        limit: int | float,
        subject: str,
    ):
        self.field = field
        self.observed = observed
        self.limit = limit
        self.subject = subject
        super().__init__(
            f"archive safety default {field} was exceeded for {subject}: "
            f"observed={observed}, limit={limit}; quarantine the original and use an "
            "explicit round-bound limit override only after reviewing this measurement"
        )


def _source_files(root: Path, exclude: set[str] | None = None) -> list[tuple[str, Path]]:
    resolved = root.resolve()
    if not resolved.is_dir() or root.is_symlink():
        raise BridgeError(f"artifact source must be a real directory: {root}")
    excluded = exclude or set()
    result: list[tuple[str, Path]] = []
    for path in sorted(resolved.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(resolved).as_posix()
        if path.is_symlink():
            raise BridgeError(f"artifact source contains a symbolic link: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BridgeError(f"artifact source contains a non-regular file: {relative}")
        normalized = safe_relative_path(relative, label="artifact path")
        if normalized not in excluded:
            result.append((normalized, path))
    if not result:
        raise BridgeError(f"artifact source contains no files: {root}")
    return result


def build_tree_manifest(root: Path, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    excluded = {safe_relative_path(item, label="manifest exclusion") for item in exclude}
    files: dict[str, dict[str, Any]] = {}
    total = 0
    for relative, path in _source_files(root, excluded):
        size = path.stat().st_size
        total += size
        files[relative] = {"bytes": size, "sha256": sha256_file(path)}
    tree_hash = hashlib.sha256(canonical_json(files)).hexdigest()
    return {
        "schema": MANIFEST_SCHEMA,
        "file_count": len(files),
        "total_bytes": total,
        "tree_sha256": tree_hash,
        "files": files,
    }


def validate_manifest(manifest: dict[str, Any], label: str = "manifest") -> None:
    if manifest.get("schema") != MANIFEST_SCHEMA or not isinstance(manifest.get("files"), dict):
        raise BridgeError(f"{label} has an unsupported schema")
    files = manifest["files"]
    if manifest.get("file_count") != len(files):
        raise BridgeError(f"{label} file_count does not match files")
    total = 0
    canonical: dict[str, Any] = {}
    for relative, entry in sorted(files.items()):
        if safe_relative_path(relative, label=f"{label} path") != relative:
            raise BridgeError(f"{label} path is not normalized: {relative!r}")
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("bytes"), int)
            or entry["bytes"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))
        ):
            raise BridgeError(f"{label} entry is invalid: {relative}")
        total += entry["bytes"]
        canonical[relative] = entry
    expected_tree = hashlib.sha256(canonical_json(canonical)).hexdigest()
    if manifest.get("total_bytes") != total or manifest.get("tree_sha256") != expected_tree:
        raise BridgeError(f"{label} aggregate values do not match its file records")


def classify_manifests(baseline: dict[str, Any], returned: dict[str, Any]) -> dict[str, list[str]]:
    validate_manifest(baseline, "baseline manifest")
    validate_manifest(returned, "returned manifest")
    before, after = baseline["files"], returned["files"]
    before_paths, after_paths = set(before), set(after)
    shared = before_paths & after_paths
    return {
        "added": sorted(after_paths - before_paths),
        "modified": sorted(path for path in shared if before[path] != after[path]),
        "removed": sorted(before_paths - after_paths),
        "unchanged": sorted(path for path in shared if before[path] == after[path]),
    }


def scan_secrets(
    root: Path, *, exclude: Iterable[str] = ()
) -> list[dict[str, Any]]:
    excluded = {
        safe_relative_path(item, label="secret-scan exclusion") for item in exclude
    }
    findings: list[dict[str, Any]] = []
    for relative, path in _source_files(root, excluded):
        rules = [("private-key", PRIVATE_KEY_PATTERN), *TEXT_SECRET_RULES]
        unseen = {name: pattern for name, pattern in rules}
        tail = b""
        newlines_read = 0
        with path.open("rb") as stream:
            while unseen and (block := stream.read(SCAN_BLOCK_BYTES)):
                content = tail + block
                line_base = newlines_read - tail.count(b"\n")
                for rule, pattern in list(unseen.items()):
                    match = pattern.search(content)
                    if match:
                        line = line_base + content.count(b"\n", 0, match.start()) + 1
                        findings.append({"path": relative, "line": line, "rule": rule})
                        del unseen[rule]
                newlines_read += block.count(b"\n")
                tail = content[-SCAN_OVERLAP_BYTES:]
    return findings


def create_deterministic_zip(source: Path, destination: Path, *, prefix: str | None = None) -> Path:
    if destination.exists():
        raise BridgeError(f"refusing to overwrite archive: {destination}")
    prefix_value = safe_relative_path(prefix, label="archive prefix") if prefix else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as raw:
            with zipfile.ZipFile(
                raw, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                for relative, path in _source_files(source):
                    name = f"{prefix_value}/{relative}" if prefix_value else relative
                    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.flag_bits |= 0x800
                    archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _member_path(info: zipfile.ZipInfo) -> tuple[str, bool]:
    if "\x00" in info.filename:
        raise BridgeError("archive member contains NUL")
    is_directory = info.is_dir() or info.filename.endswith("/")
    candidate = info.filename[:-1] if is_directory else info.filename
    normalized = safe_relative_path(candidate, label="archive member")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    expected_type = stat.S_IFDIR if is_directory else stat.S_IFREG
    if file_type not in (0, expected_type):
        raise BridgeError(f"archive member is not a regular file/directory: {normalized}")
    if info.flag_bits & 0x1:
        raise BridgeError(f"encrypted archive member is not supported: {normalized}")
    return normalized, is_directory


def _unique_content_root(
    files: list[str], root_markers: tuple[str, ...]
) -> tuple[str, ...]:
    if root_markers:
        file_set = set(files)
        prefixes: set[tuple[str, ...]] = {()}
        for relative in files:
            parts = relative.split("/")
            prefixes.update(tuple(parts[:depth]) for depth in range(1, len(parts)))
        candidates = [
            prefix
            for prefix in sorted(prefixes, key=lambda item: (len(item), item))
            if all("/".join((*prefix, marker)) in file_set for marker in root_markers)
        ]
        if len(candidates) != 1:
            rendered = ["/".join(item) or "." for item in candidates]
            raise BridgeError(
                "archive package root is ambiguous or missing: expected exactly one "
                f"directory containing {list(root_markers)!r}; found {rendered!r}"
            )
        return candidates[0]

    parts = [item.split("/") for item in files]
    chain: list[str] = []
    depth = 0
    while all(len(item) > depth + 1 for item in parts):
        values = {item[depth] for item in parts}
        if len(values) != 1:
            break
        chain.append(next(iter(values)))
        depth += 1
    return tuple(chain)


def inspect_archive(
    path: Path,
    *,
    limits: ArchiveLimits | None = None,
    root_markers: Iterable[str] = (),
) -> ArchiveInspection:
    if not path.is_file():
        raise BridgeError(f"return archive is missing: {path}")
    selected_limits = limits or ArchiveLimits()
    markers = tuple(
        safe_relative_path(marker, label="archive root marker") for marker in root_markers
    )
    seen: set[str] = set()
    files: list[str] = []
    total = 0
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = len(archive.infolist())
            if members > selected_limits.max_members:
                raise ArchiveLimitError(
                    "max_members", members, selected_limits.max_members, path.name
                )
            for info in archive.infolist():
                normalized, is_directory = _member_path(info)
                collision_key = normalized.casefold()
                if collision_key in seen:
                    raise BridgeError(f"archive contains a duplicate/case-colliding path: {normalized}")
                seen.add(collision_key)
                if is_directory:
                    continue
                if info.file_size > selected_limits.max_file_bytes:
                    raise ArchiveLimitError(
                        "max_file_bytes", info.file_size,
                        selected_limits.max_file_bytes, normalized,
                    )
                total += info.file_size
                if total > selected_limits.max_total_bytes:
                    raise ArchiveLimitError(
                        "max_total_bytes", total,
                        selected_limits.max_total_bytes, path.name,
                    )
                if info.file_size and (
                    not info.compress_size
                    or info.file_size / info.compress_size
                    > selected_limits.max_compression_ratio
                ):
                    ratio: float | str = (
                        "infinite (compressed size is 0 bytes)"
                        if not info.compress_size
                        else info.file_size / info.compress_size
                    )
                    raise ArchiveLimitError(
                        "max_compression_ratio", ratio,
                        selected_limits.max_compression_ratio, normalized,
                    )
                files.append(normalized)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise BridgeError(f"cannot read a valid ZIP archive {path}: {exc}") from exc
    if not files:
        raise BridgeError("archive contains no files")
    wrapper_chain = _unique_content_root(files, markers)
    return ArchiveInspection(
        tuple(sorted(files)), wrapper_chain, total, selected_limits.as_dict()
    )


def extract_archive(
    path: Path,
    destination: Path,
    *,
    limits: ArchiveLimits | None = None,
    root_markers: Iterable[str] = (),
) -> ExtractionResult:
    inspection = inspect_archive(path, limits=limits, root_markers=root_markers)
    if destination.exists():
        raise BridgeError(f"refusing to overwrite quarantine extraction: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        with zipfile.ZipFile(path, "r") as archive:
            for info in archive.infolist():
                relative, is_directory = _member_path(info)
                target = staging / relative
                target.resolve().relative_to(staging.resolve())
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
        promote_directory(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    content = destination.joinpath(*inspection.wrapper_chain)
    return ExtractionResult(
        destination,
        content,
        inspection.wrapper_chain,
        inspection.files,
        inspection.limits,
    )
