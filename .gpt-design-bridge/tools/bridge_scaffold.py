"""Transactional application-foundation scaffolding."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge_core import (
    BridgeError,
    atomic_write_json,
    check_project,
    exclusive_lock,
    kit_root,
    load_project,
    read_json,
    run_git,
    safe_relative_path,
    sha256_file,
)


FOUNDATION_SCHEMA = "gpt-design-bridge/app-foundation/v2"
SCAFFOLD_RECORD_SCHEMA = "gpt-design-bridge/app-scaffold/v2"
PICKER_BUNDLE_SCHEMA = "gpt-design-bridge/icon-picker-template/v1"
BOOTSTRAP_CERTIFICATE_SCHEMA = "gpt-design-bridge/bootstrap-certificate/v1"
BOOTSTRAP_CERTIFICATE_RELATIVE = (
    ".gpt-design-bridge/baselines/bootstrap-certificate.json"
)
TEMPLATE_SUFFIX = ".gdb-template"
TOKEN_PATTERN = re.compile(r"@@[A-Z0-9_]+@@")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _strict_foundation_manifest(path: Path) -> dict[str, Any]:
    manifest = read_json(path)
    expected = {
        "schema",
        "common_root",
        "database_roots",
        "template_suffix",
        "bundle_manifests",
    }
    if set(manifest) != expected or manifest.get("schema") != FOUNDATION_SCHEMA:
        raise BridgeError(f"unsupported or non-strict app foundation manifest: {path}")
    if manifest.get("template_suffix") != TEMPLATE_SUFFIX:
        raise BridgeError(f"app foundation template suffix must be {TEMPLATE_SUFFIX!r}")
    common = safe_relative_path(manifest.get("common_root", ""), label="common_root")
    variants = manifest.get("database_roots")
    if not isinstance(variants, dict) or set(variants) != {"sqlite", "postgresql"}:
        raise BridgeError("app foundation must declare exactly sqlite and postgresql roots")
    normalized = {
        name: safe_relative_path(value, label=f"database_roots.{name}")
        for name, value in variants.items()
    }
    if len({common.casefold(), *(value.casefold() for value in normalized.values())}) != 3:
        raise BridgeError("app foundation common and database roots must be distinct")
    bundles = manifest.get("bundle_manifests")
    if not isinstance(bundles, list) or not bundles:
        raise BridgeError("app foundation must declare at least one bundle manifest")
    normalized_bundles = [
        safe_relative_path(value, label="bundle_manifests")
        for value in bundles
    ]
    if (
        len(normalized_bundles) != len(set(value.casefold() for value in normalized_bundles))
        or normalized_bundles != sorted(normalized_bundles, key=str.casefold)
    ):
        raise BridgeError("app foundation bundle manifests must be unique and sorted")
    return {
        **manifest,
        "common_root": common,
        "database_roots": normalized,
        "bundle_manifests": normalized_bundles,
    }


def _source_files(root: Path, relative_root: str) -> list[Path]:
    source_root = root / relative_root
    if not source_root.is_dir() or source_root.is_symlink():
        raise BridgeError(f"app foundation source root is missing or symbolic: {relative_root}")
    entries = sorted(source_root.rglob("*"), key=lambda item: item.as_posix().casefold())
    for entry in entries:
        if entry.is_symlink():
            raise BridgeError(f"app foundation may not contain symbolic links: {entry}")
        if not entry.is_dir() and not entry.is_file():
            raise BridgeError(f"app foundation contains a non-regular entry: {entry}")
    return [entry for entry in entries if entry.is_file()]


def _destination_path(source: Path, source_root: Path) -> str:
    relative = source.relative_to(source_root).as_posix()
    if relative.endswith(TEMPLATE_SUFFIX):
        relative = relative[: -len(TEMPLATE_SUFFIX)]
    return safe_relative_path(relative, label="scaffold destination")


def _render_template(source: Path, replacements: dict[str, str]) -> bytes:
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BridgeError(f"cannot read UTF-8 app template {source}: {exc}") from exc
    unknown = sorted(set(TOKEN_PATTERN.findall(text)) - set(replacements))
    if unknown:
        raise BridgeError(
            f"app template has unknown token(s) in {source}: {', '.join(unknown)}"
        )
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text.encode("utf-8")


def _selected_files(
    foundation_root: Path,
    manifest: dict[str, Any],
    database: str,
) -> list[tuple[str, Path]]:
    roots = [manifest["common_root"], manifest["database_roots"][database]]
    selected: list[tuple[str, Path]] = []
    destinations: dict[str, str] = {}
    for relative_root in roots:
        source_root = foundation_root / relative_root
        for source in _source_files(foundation_root, relative_root):
            destination = _destination_path(source, source_root)
            folded = destination.casefold()
            if folded in destinations:
                raise BridgeError(
                    "app foundation contains a case-insensitive destination collision: "
                    f"{destinations[folded]} and {destination}"
                )
            destinations[folded] = destination
            selected.append((destination, source))
    if not selected:
        raise BridgeError("selected app foundation contains no files")
    return sorted(selected, key=lambda item: item[0].casefold())


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _inventory_digest(rows: list[tuple[str, bytes]]) -> str:
    content = "".join(
        f"{_hash_bytes(payload)}  {name}\n" for name, payload in rows
    ).encode("utf-8")
    return _hash_bytes(content)


def _exact_record(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BridgeError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BridgeError(f"{label} must be a positive integer")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise BridgeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _bundle_files(
    foundation_root: Path,
    relative_manifest: str,
) -> tuple[list[tuple[str, bytes]], dict[str, Any]]:
    manifest_path = foundation_root / relative_manifest
    manifest = read_json(manifest_path)
    _exact_record(
        manifest,
        {
            "schema",
            "source",
            "archive",
            "source_snapshot",
            "scaffold",
            "libraries",
            "required_files",
        },
        f"bundle manifest {relative_manifest}",
    )
    if manifest.get("schema") != PICKER_BUNDLE_SCHEMA:
        raise BridgeError(f"unsupported bundle schema in {relative_manifest}")
    _exact_record(manifest.get("source"), {"label", "relative_path"}, "bundle source")
    archive = _exact_record(
        manifest.get("archive"), {"path", "bytes", "sha256"}, "bundle archive"
    )
    archive_name = safe_relative_path(archive.get("path", ""), label="bundle archive path")
    if "/" in archive_name:
        raise BridgeError("bundle archive path must be beside its manifest")
    archive_path = manifest_path.parent / archive_name
    if not archive_path.is_file() or archive_path.is_symlink():
        raise BridgeError(f"bundle archive is missing or symbolic: {archive_path}")
    archive_content = archive_path.read_bytes()
    if len(archive_content) != _positive_integer(archive.get("bytes"), "bundle archive bytes"):
        raise BridgeError(f"bundle archive byte count drifted: {archive_path}")
    if _hash_bytes(archive_content) != _digest(archive.get("sha256"), "bundle archive sha256"):
        raise BridgeError(f"bundle archive hash drifted: {archive_path}")

    snapshot = _exact_record(
        manifest.get("source_snapshot"),
        {"file_count", "total_bytes", "manifest_sha256"},
        "bundle source_snapshot",
    )
    scaffold = _exact_record(
        manifest.get("scaffold"),
        {"destination", "excluded", "file_count", "total_bytes", "manifest_sha256"},
        "bundle scaffold",
    )
    destination = safe_relative_path(
        scaffold.get("destination", ""), label="bundle scaffold destination"
    )
    excluded_value = scaffold.get("excluded")
    if not isinstance(excluded_value, list):
        raise BridgeError("bundle scaffold.excluded must be an array")
    excluded = [
        safe_relative_path(value, label="bundle scaffold.excluded")
        for value in excluded_value
    ]
    if excluded != sorted(set(excluded), key=str.casefold):
        raise BridgeError("bundle scaffold.excluded must be unique and sorted")

    try:
        with zipfile.ZipFile(io.BytesIO(archive_content), "r") as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if names != sorted(names):
                raise BridgeError("bundle ZIP entries must be sorted")
            if len(names) != len(set(name.casefold() for name in names)):
                raise BridgeError("bundle ZIP contains duplicate or case-colliding paths")
            rows: list[tuple[str, bytes]] = []
            for info in infos:
                name = safe_relative_path(info.filename, label="bundle ZIP entry")
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir() or (mode and not stat.S_ISREG(mode)):
                    raise BridgeError(f"bundle ZIP entry is not a regular file: {name}")
                if info.flag_bits & 0x1:
                    raise BridgeError(f"bundle ZIP entry is encrypted: {name}")
                payload = bundle.read(info)
                if len(payload) != info.file_size:
                    raise BridgeError(f"bundle ZIP entry size drifted: {name}")
                rows.append((name, payload))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise BridgeError(f"cannot inspect bundle archive {archive_path}: {exc}") from exc

    expected_source = (
        _positive_integer(snapshot.get("file_count"), "bundle source file_count"),
        _positive_integer(snapshot.get("total_bytes"), "bundle source total_bytes"),
        _digest(snapshot.get("manifest_sha256"), "bundle source manifest_sha256"),
    )
    actual_source = (
        len(rows),
        sum(len(payload) for _name, payload in rows),
        _inventory_digest(rows),
    )
    if actual_source != expected_source:
        raise BridgeError(
            f"bundle source inventory drifted: expected={expected_source}, actual={actual_source}"
        )
    names_set = {name for name, _payload in rows}
    required_value = manifest.get("required_files")
    if not isinstance(required_value, list):
        raise BridgeError("bundle required_files must be an array")
    required = [
        safe_relative_path(value, label="bundle required_files")
        for value in required_value
    ]
    if required != sorted(set(required), key=str.casefold):
        raise BridgeError("bundle required_files must be unique and sorted")
    missing = sorted(set(required) - names_set)
    if missing:
        raise BridgeError(f"bundle required file(s) missing: {', '.join(missing)}")
    missing_exclusions = sorted(set(excluded) - names_set)
    if missing_exclusions:
        raise BridgeError(
            f"bundle excluded file(s) missing: {', '.join(missing_exclusions)}"
        )

    selected = [row for row in rows if row[0] not in set(excluded)]
    expected_selected = (
        _positive_integer(scaffold.get("file_count"), "bundle scaffold file_count"),
        _positive_integer(scaffold.get("total_bytes"), "bundle scaffold total_bytes"),
        _digest(scaffold.get("manifest_sha256"), "bundle scaffold manifest_sha256"),
    )
    actual_selected = (
        len(selected),
        sum(len(payload) for _name, payload in selected),
        _inventory_digest(selected),
    )
    if actual_selected != expected_selected:
        raise BridgeError(
            "bundle scaffold inventory drifted: "
            f"expected={expected_selected}, actual={actual_selected}"
        )

    libraries = _exact_record(
        manifest.get("libraries"), {"lucide", "twemoji"}, "bundle libraries"
    )
    lucide = _exact_record(
        libraries.get("lucide"),
        {"asset_root", "file_count", "license", "package", "version"},
        "bundle libraries.lucide",
    )
    twemoji = _exact_record(
        libraries.get("twemoji"),
        {"asset_root", "file_count", "graphics_license", "package", "version"},
        "bundle libraries.twemoji",
    )
    if lucide != {
        "asset_root": "assets/lucide",
        "file_count": 1952,
        "license": "ISC",
        "package": "lucide-static",
        "version": "1.14.0",
    }:
        raise BridgeError("bundle Lucide package contract drifted")
    if twemoji != {
        "asset_root": "assets/twemoji",
        "file_count": 3720,
        "graphics_license": "CC-BY-4.0",
        "package": "@twemoji/svg",
        "version": "15.0.0",
    }:
        raise BridgeError("bundle Twemoji package contract drifted")
    lucide_count = sum(
        name.startswith("assets/lucide/") and name.endswith(".svg")
        for name, _payload in rows
    )
    twemoji_count = sum(
        name.startswith("assets/twemoji/") and name.endswith(".svg")
        for name, _payload in rows
    )
    if lucide_count != lucide.get("file_count") or twemoji_count != twemoji.get("file_count"):
        raise BridgeError("bundle Lucide or Twemoji asset count drifted")

    files = [
        (
            safe_relative_path(f"{destination}/{name}", label="bundle destination"),
            payload,
        )
        for name, payload in selected
    ]
    record = {
        "manifest": relative_manifest,
        "archive_sha256": archive["sha256"],
        "source_file_count": snapshot["file_count"],
        "scaffold_file_count": scaffold["file_count"],
    }
    return files, record


def _remove_created(created_files: list[Path], created_directories: set[Path]) -> None:
    for path in reversed(created_files):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for directory in sorted(created_directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _application_foundation_record(project: Path) -> dict[str, Any]:
    path = kit_root(project) / "baselines" / "application-foundation.json"
    record = read_json(path)
    expected = {
        "schema",
        "foundation_schema",
        "database",
        "bundles",
        "file_count",
        "files",
    }
    if set(record) != expected or record.get("schema") != SCAFFOLD_RECORD_SCHEMA:
        raise BridgeError(f"application foundation record is not strict: {path}")
    files = record.get("files")
    if not isinstance(files, dict) or record.get("file_count") != len(files):
        raise BridgeError(f"application foundation file count is stale: {path}")
    for relative, entry in files.items():
        safe_relative_path(relative, label="application foundation path")
        _exact_record(entry, {"bytes", "sha256"}, f"application file {relative}")
        size = entry.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BridgeError(
                f"application file byte count must be a non-negative integer: {relative}"
            )
        _digest(entry.get("sha256"), f"application file sha256: {relative}")
    return record


def _verify_application_foundation(project: Path, record: dict[str, Any]) -> None:
    findings: list[str] = []
    for relative, expected in sorted(record["files"].items()):
        path = project / relative
        if not path.is_file() or path.is_symlink():
            findings.append(f"missing or non-regular scaffold file: {relative}")
            continue
        actual_size = path.stat().st_size
        if actual_size != expected["bytes"]:
            findings.append(
                f"scaffold byte-count mismatch: {relative}: "
                f"expected {expected['bytes']}, found {actual_size}"
            )
        actual_hash = sha256_file(path)
        if actual_hash != expected["sha256"]:
            findings.append(
                f"scaffold hash mismatch: {relative}: "
                f"expected {expected['sha256']}, found {actual_hash}"
            )
    if findings:
        detail = "\n  - ".join(findings)
        raise BridgeError(
            "bootstrap certification requires the exact untouched application "
            f"foundation:\n  - {detail}"
        )


def _blackbox_task_names(project: Path) -> list[str]:
    git_dir_result = run_git(project, "rev-parse", "--git-dir")
    raw = Path(git_dir_result.stdout.strip())
    git_dir = raw if raw.is_absolute() else project / raw
    harness = git_dir.resolve() / "gpt-blackbox-lite"
    if not harness.exists():
        return []
    if not harness.is_dir() or harness.is_symlink():
        raise BridgeError(f"BlackBox state root is not a regular directory: {harness}")
    return sorted(
        entry.name
        for entry in harness.iterdir()
        if entry.name != ".tmp"
    )


def _git_inventory_files(project: Path) -> list[tuple[str, Path]]:
    completed = run_git(
        project,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    names = [name for name in completed.stdout.split("\0") if name]
    rows: list[tuple[str, Path]] = []
    for name in sorted(names, key=str.casefold):
        if name == BOOTSTRAP_CERTIFICATE_RELATIVE:
            continue
        if (
            "\\" in name
            or name.startswith("/")
            or re.match(r"^[A-Za-z]:", name)
            or any(part in ("", ".", "..") for part in name.split("/"))
            or any(ord(character) < 32 for character in name)
        ):
            raise BridgeError(f"Git returned an unsafe bootstrap path: {name!r}")
        path = project / name
        if not path.is_file() or path.is_symlink():
            raise BridgeError(f"bootstrap inventory path is not a regular file: {name}")
        lowered = Path(name).name.lower()
        if (
            (lowered == ".env" or lowered.startswith(".env."))
            and not lowered.endswith(".example")
        ):
            raise BridgeError(
                "credential-bearing environment files must be Git-ignored before "
                f"bootstrap certification: {name}"
            )
        rows.append((name, path))
    return rows


def _inventory_summary(rows: list[tuple[str, Path]]) -> dict[str, Any]:
    manifest = "".join(
        f"{sha256_file(path)}  {path.stat().st_size}  {name}\n"
        for name, path in rows
    ).encode("utf-8")
    return {
        "file_count": len(rows),
        "total_bytes": sum(path.stat().st_size for _name, path in rows),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "excluded": [
            ".git/",
            "Git-standard ignored files",
            BOOTSTRAP_CERTIFICATE_RELATIVE,
        ],
    }


def _supplemental_manifest(
    rows: list[tuple[str, Path]],
    application_paths: set[str],
) -> dict[str, dict[str, Any]]:
    return {
        name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for name, path in rows
        if name not in application_paths
        and not name.startswith(".gpt-design-bridge/")
    }


def certify_bootstrap(project: Path) -> dict[str, Any]:
    """Certify the official large seed before any ordinary BlackBox task starts."""
    kit = kit_root(project)
    certificate_path = project / BOOTSTRAP_CERTIFICATE_RELATIVE
    with exclusive_lock(kit / "runtime" / "mutation.lock", "bootstrap-certify"):
        if certificate_path.exists() or certificate_path.is_symlink():
            raise BridgeError(
                f"bootstrap certificate already exists and is immutable: {certificate_path}"
            )
        config, state = load_project(project)
        if (
            state["phase"] != "building"
            or state["active_round"] is not None
            or state.get("application_foundation") is None
        ):
            raise BridgeError(
                "bootstrap-certify requires a newly scaffolded building project "
                "with no active designer round"
            )
        tasks = _blackbox_task_names(project)
        if tasks:
            raise BridgeError(
                "bootstrap-certify must run before the first BlackBox task; "
                f"found: {', '.join(tasks)}"
            )
        project_errors = [
            finding
            for finding in check_project(project)
            if "lifecycle mutation lock is still present" not in finding
        ]
        if project_errors:
            raise BridgeError(
                "project-local Design Bridge check failed before certification:\n  - "
                + "\n  - ".join(project_errors)
            )
        record = _application_foundation_record(project)
        _verify_application_foundation(project, record)
        rows = _git_inventory_files(project)
        application_paths = set(record["files"])
        certificate = {
            "schema": BOOTSTRAP_CERTIFICATE_SCHEMA,
            "certified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project_slug": config["project"]["slug"],
            "state_generation": state["generation"],
            "blackbox_task_count": 0,
            "application_foundation": {
                "record_sha256": sha256_file(
                    kit / "baselines" / "application-foundation.json"
                ),
                "file_count": record["file_count"],
            },
            "git_inventory": _inventory_summary(rows),
            "supplemental_files": _supplemental_manifest(rows, application_paths),
        }
        atomic_write_json(certificate_path, certificate)
    return certificate


def verify_bootstrap(project: Path) -> dict[str, Any]:
    """Verify an unchanged certified seed immediately before the first task."""
    certificate_path = project / BOOTSTRAP_CERTIFICATE_RELATIVE
    certificate = read_json(certificate_path)
    expected = {
        "schema",
        "certified_at",
        "project_slug",
        "state_generation",
        "blackbox_task_count",
        "application_foundation",
        "git_inventory",
        "supplemental_files",
    }
    if (
        set(certificate) != expected
        or certificate.get("schema") != BOOTSTRAP_CERTIFICATE_SCHEMA
        or certificate.get("blackbox_task_count") != 0
    ):
        raise BridgeError(f"bootstrap certificate is not strict: {certificate_path}")
    config, state = load_project(project)
    if (
        certificate.get("project_slug") != config["project"]["slug"]
        or certificate.get("state_generation") != state["generation"]
    ):
        raise BridgeError("bootstrap certificate does not match current project state")
    tasks = _blackbox_task_names(project)
    if tasks:
        raise BridgeError(
            "bootstrap-check is a pre-first-task gate; BlackBox task state already exists: "
            + ", ".join(tasks)
        )
    project_errors = check_project(project)
    if project_errors:
        raise BridgeError(
            "project-local Design Bridge check failed:\n  - "
            + "\n  - ".join(project_errors)
        )
    record = _application_foundation_record(project)
    _verify_application_foundation(project, record)
    expected_foundation = certificate.get("application_foundation")
    actual_foundation = {
        "record_sha256": sha256_file(
            kit_root(project) / "baselines" / "application-foundation.json"
        ),
        "file_count": record["file_count"],
    }
    if expected_foundation != actual_foundation:
        raise BridgeError(
            "application foundation record differs from the bootstrap certificate"
        )
    rows = _git_inventory_files(project)
    actual_inventory = _inventory_summary(rows)
    if certificate.get("git_inventory") != actual_inventory:
        raise BridgeError(
            "current non-ignored Git inventory differs from the certified seed: "
            f"expected {certificate.get('git_inventory')}, found {actual_inventory}"
        )
    actual_supplemental = _supplemental_manifest(rows, set(record["files"]))
    if certificate.get("supplemental_files") != actual_supplemental:
        raise BridgeError(
            "project-local skills, prompt, or other supplemental seed files differ "
            "from the bootstrap certificate"
        )
    return certificate


def scaffold_application(project: Path) -> dict[str, Any]:
    kit = kit_root(project)
    with exclusive_lock(kit / "runtime" / "mutation.lock", "app-scaffold"):
        config, state = load_project(project)
        if state["phase"] != "initialized" or state["active_round"] is not None:
            raise BridgeError("app-scaffold requires an initialized project with no active round")
        record_path = kit / "baselines" / "application-foundation.json"
        if record_path.exists():
            raise BridgeError(f"application foundation record already exists: {record_path}")
        foundation_root = kit / "foundation" / "app-starter"
        manifest = _strict_foundation_manifest(foundation_root / "FOUNDATION.json")
        database = config["database"]["engine"]
        selected = _selected_files(foundation_root, manifest, database)
        bundled: list[tuple[str, bytes]] = []
        bundle_records: list[dict[str, Any]] = []
        for relative_manifest in manifest["bundle_manifests"]:
            files, bundle_record = _bundle_files(foundation_root, relative_manifest)
            bundled.extend(files)
            bundle_records.append(bundle_record)
        hostname = config["deployment"]["hostname"]
        if hostname is not None and not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            hostname.lower(),
        ):
            raise BridgeError(f"deployment hostname is not a valid DNS hostname: {hostname!r}")
        deployment_hostname = hostname.lower() if hostname else "SPECIMEN_SET_DEPLOYMENT_HOSTNAME"
        replacements = {
            "@@PROJECT_NAME_JSON@@": json.dumps(
                config["project"]["name"], ensure_ascii=False
            ),
            "@@PROJECT_SLUG@@": config["project"]["slug"],
            "@@PROJECT_SLUG_JSON@@": json.dumps(config["project"]["slug"]),
            "@@PROJECT_NAME_MARKDOWN@@": config["project"]["name"]
            .replace("\r", " ")
            .replace("\n", " "),
            "@@DATABASE_ENGINE@@": database,
            "@@DATABASE_ENGINE_JSON@@": json.dumps(database),
            "@@APPLICATION_PORT@@": str(config["deployment"]["application_port"]),
            "@@HOSTNAME_JSON@@": json.dumps(
                config["deployment"]["hostname"], ensure_ascii=False
            ),
            "@@DEPLOYMENT_HOSTNAME@@": deployment_hostname,
        }
        planned: list[tuple[str, Path, bytes]] = []
        for destination, source in selected:
            try:
                content = (
                    _render_template(source, replacements)
                    if source.name.endswith(TEMPLATE_SUFFIX)
                    else source.read_bytes()
                )
            except OSError as exc:
                raise BridgeError(f"cannot read app foundation file {source}: {exc}") from exc
            planned.append((destination, project / destination, content))
        planned.extend(
            (destination, project / destination, content)
            for destination, content in bundled
        )
        destinations: dict[str, str] = {}
        for destination, target, _content in planned:
            folded = destination.casefold()
            if folded in destinations:
                raise BridgeError(
                    "app foundation contains a case-insensitive destination collision: "
                    f"{destinations[folded]} and {destination}"
                )
            destinations[folded] = destination
            if target.exists() or target.is_symlink():
                raise BridgeError(f"app-scaffold refuses to overwrite existing path: {destination}")
        planned.sort(key=lambda item: item[0].casefold())
        created_files: list[Path] = []
        created_directories: set[Path] = set()
        try:
            file_manifest: dict[str, dict[str, Any]] = {}
            for relative, target, content in planned:
                missing: list[Path] = []
                cursor = target.parent
                while cursor != project and not cursor.exists():
                    missing.append(cursor)
                    cursor = cursor.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                created_directories.update(missing)
                with target.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                created_files.append(target)
                file_manifest[relative] = {
                    "bytes": len(content),
                    "sha256": _hash_bytes(content),
                }
            record = {
                "schema": SCAFFOLD_RECORD_SCHEMA,
                "foundation_schema": FOUNDATION_SCHEMA,
                "database": database,
                "bundles": bundle_records,
                "file_count": len(file_manifest),
                "files": file_manifest,
            }
            with record_path.open("xb") as stream:
                encoded = (
                    json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
                ).encode("utf-8")
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            created_files.append(record_path)
            state["phase"] = "building"
            state["generation"] += 1
            state["application_foundation"] = {
                "schema": SCAFFOLD_RECORD_SCHEMA,
                "database": database,
                "bundle_count": len(bundle_records),
                "file_count": len(file_manifest),
            }
            atomic_write_json(kit / "state.json", state)
        except Exception:
            _remove_created(created_files, created_directories)
            raise
    return record
