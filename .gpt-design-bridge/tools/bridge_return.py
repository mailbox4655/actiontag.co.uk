"""Quarantine and fact-based inspection of a complete designer return."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from bridge_artifacts import (
    ArchiveLimitError,
    ArchiveLimits,
    build_tree_manifest,
    classify_manifests,
    extract_archive,
    scan_secrets,
    validate_manifest,
)
from bridge_core import (
    BridgeError,
    atomic_write_json,
    canonical_json,
    exclusive_lock,
    kit_root,
    load_project,
    load_round,
    promote_directory,
    read_json,
    sha256_file,
    validate_iso_date,
)
from bridge_rounds import apply_transition, persist_round_state
from bridge_rounds import adoption_screens, build_source_manifest
from bridge_surface import (
    assert_outside_region_unchanged,
    assert_plumbing_unchanged,
    extract_embedded_sources,
    is_designer_package_path,
    load_surface,
    map_direct_package_path,
    validate_embedded_sources,
)


REPORT_SCHEMA = "gpt-design-bridge/return-inspection/v2"
ADDITIONS_SCHEMA = "gpt-design-bridge/contract-additions/v1"
RETURNED_DATA_ESCROW = "travelling-data-export.sqlite3"
RETURN_ONLY_FILES = {RETURNED_DATA_ESCROW}
RETURN_ROOT_MARKERS = (
    "index.html",
    "BASELINE-MANIFEST.json",
    "RETURN-NOTE.md",
    "contract-additions.json",
)
ADDITION_KEYS = {
    "id", "kind", "entity_or_path", "shape", "surfaces", "behavior",
    "rules", "specimen", "directive",
}


class ReturnSafetyError(BridgeError):
    """The original was quarantined, but safety prevents lifecycle advancement."""


def _copy_new(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)


def validate_additions(value: dict[str, Any], round_id: str) -> int:
    if set(value) != {"schema", "round_id", "additions"}:
        raise BridgeError("contract additions root has unexpected or missing fields")
    if value.get("schema") != ADDITIONS_SCHEMA or value.get("round_id") != round_id:
        raise BridgeError("contract additions schema or round ID does not match")
    additions = value.get("additions")
    if not isinstance(additions, list):
        raise BridgeError("contract additions.additions must be an array")
    seen: set[str] = set()
    for item in additions:
        if not isinstance(item, dict) or set(item) != ADDITION_KEYS:
            raise BridgeError("contract addition has unexpected or missing fields")
        addition_id = item.get("id")
        if not isinstance(addition_id, str) or not re.fullmatch(r"CA-[1-9][0-9]*", addition_id):
            raise BridgeError(f"contract addition ID is invalid: {addition_id!r}")
        if addition_id in seen:
            raise BridgeError(f"contract addition ID is duplicated: {addition_id}")
        seen.add(addition_id)
        if item.get("kind") not in {
            "field", "entity", "route", "asset", "interaction", "copy-rule"
        }:
            raise BridgeError(f"contract addition kind is invalid: {addition_id}")
        for key in ("entity_or_path", "shape", "behavior"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise BridgeError(f"contract addition {addition_id}.{key} must be non-empty")
        if not isinstance(item.get("rules"), str):
            raise BridgeError(f"contract addition {addition_id}.rules must be a string")
        surfaces = item.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces or any(
            not isinstance(surface, str) or not surface.strip() for surface in surfaces
        ):
            raise BridgeError(f"contract addition {addition_id}.surfaces is invalid")
        specimen = item.get("specimen")
        if not isinstance(specimen, dict) or set(specimen) != {"used", "example", "marking"}:
            raise BridgeError(f"contract addition {addition_id}.specimen is invalid")
        if specimen["used"] is True:
            if specimen["marking"] != "SPECIMEN" or specimen["example"] is None:
                raise BridgeError(f"contract addition {addition_id} has an unmarked specimen")
        elif specimen["used"] is False:
            if specimen["marking"] != "not-applicable" or specimen["example"] is not None:
                raise BridgeError(f"contract addition {addition_id} has false specimen authority")
        else:
            raise BridgeError(f"contract addition {addition_id}.specimen.used must be boolean")
        directive = item.get("directive")
        if not isinstance(directive, dict) or set(directive) != {"by", "date", "note"}:
            raise BridgeError(f"contract addition {addition_id}.directive is invalid")
        if directive["by"] not in {"owner", "designer"}:
            raise BridgeError(f"contract addition {addition_id}.directive.by is invalid")
        validate_iso_date(directive["date"], f"contract addition {addition_id} directive date")
        if not isinstance(directive["note"], str) or not directive["note"].strip():
            raise BridgeError(f"contract addition {addition_id}.directive.note must be non-empty")
    return len(additions)


def _return_note(root: Path) -> tuple[str, list[str]]:
    path = root / "RETURN-NOTE.md"
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return "", [f"RETURN-NOTE.md is missing or not UTF-8: {exc}"]
    placeholders = sorted(set(re.findall(r"<<[A-Z0-9_-]+>>", text)))
    if placeholders:
        findings.append(f"RETURN-NOTE.md still contains {len(placeholders)} placeholder(s)")
    required = ("# Designer return note", "## Identity", "## Scope", "## Changes", "## Return completeness")
    missing = [heading for heading in required if heading not in text]
    if missing:
        findings.append("RETURN-NOTE.md is missing required headings: " + ", ".join(missing))
    return text, findings


def _classify_paths(
    changes: dict[str, list[str]], surface: dict[str, Any]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    designer = {key: [] for key in ("added", "modified", "removed")}
    off_limits = {key: [] for key in ("added", "modified", "removed")}
    index_path = surface["embedded"]["package_path"]
    exact = {"RETURN-NOTE.md", "contract-additions.json", *RETURN_ONLY_FILES}
    for kind in designer:
        for path in changes[kind]:
            allowed = path in exact or (path == index_path and kind == "modified")
            if not allowed and is_designer_package_path(surface, path):
                try:
                    allowed = map_direct_package_path(surface, path) is not None
                except BridgeError:
                    allowed = False
            (designer if allowed else off_limits)[kind].append(path)
    return designer, off_limits


def _persist_inspection(
    project: Path,
    state: dict[str, Any],
    record: dict[str, Any],
    inspection: dict[str, Any],
    *,
    advance: bool,
) -> None:
    record["artifacts"].setdefault("return_inspections", []).append(inspection)
    if advance:
        apply_transition(
            state,
            record,
            "return_received",
            event="return_inspected",
            details={"archive_sha256": inspection["archive_sha256"], "verdict": inspection["verdict"]},
        )
    else:
        generation = state["generation"] + 1
        state["generation"] = generation
        record["events"].append(
            {
                "generation": generation,
                "event": "return_inspection_recorded",
                "archive_sha256": inspection["archive_sha256"],
                "verdict": inspection["verdict"],
            }
        )
    persist_round_state(project, state, record)


def _validate_limit_override(limits: ArchiveLimits) -> None:
    defaults = ArchiveLimits()
    selected = limits.as_dict()
    baseline = defaults.as_dict()
    lowered = [name for name, value in selected.items() if value < baseline[name]]
    raised = [name for name, value in selected.items() if value > baseline[name]]
    if lowered or not raised:
        raise BridgeError(
            "archive limit override must retain every safety default and explicitly "
            f"raise at least one; lowered={lowered}, raised={raised}"
        )


def _designer_source_manifest(
    returned_root: Path,
    surface: dict[str, Any],
    embedded_sources: dict[str, bytes],
) -> dict[str, Any]:
    sources = dict(embedded_sources)
    for entry in surface["direct"]:
        package_root = returned_root / entry["package_root"]
        if not package_root.exists():
            continue
        for path in sorted(package_root.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file() or path.is_symlink():
                continue
            package_path = path.relative_to(returned_root).as_posix()
            source_path = map_direct_package_path(surface, package_path)
            if source_path:
                sources[source_path] = path.read_bytes()
    return build_source_manifest(sources)


def _attempt_destination(
    destination: Path, limits: ArchiveLimits, retry: bool
) -> Path:
    if not retry:
        return destination
    digest = hashlib.sha256(canonical_json(limits.as_dict())).hexdigest()[:16]
    return destination / "attempts" / f"limits-{digest}"


def inspect_return(
    project: Path,
    archive: Path,
    *,
    limits: ArchiveLimits | None = None,
) -> dict[str, Any]:
    kit = kit_root(project)
    selected_limits = limits or ArchiveLimits()
    if not selected_limits.is_default:
        _validate_limit_override(selected_limits)
    archive = archive.resolve()
    if not archive.is_file():
        raise BridgeError(f"designer return archive is missing: {archive}")
    archive_hash = sha256_file(archive)
    with exclusive_lock(kit / "runtime" / "mutation.lock", "return-inspect"):
        _config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] != "awaiting_return":
            raise BridgeError("return-inspect requires an awaiting_return round")
        record = load_round(project, active["id"])
        candidate = record["artifacts"]["outbound_candidates"][-1]
        baseline_root = project / candidate["root"] / "package"
        if sha256_file(project / candidate["root"] / candidate["archive_name"]) != candidate["archive_sha256"]:
            raise BridgeError("recorded outbound package has changed; return comparison is unsafe")
        destination = kit / "returns" / "quarantine" / record["id"] / archive_hash
        retry = destination.exists()
        if retry:
            if selected_limits.is_default:
                raise BridgeError(
                    f"this return archive is already quarantined: {destination}; "
                    "a retry is permitted only with an explicit raised archive-limit override"
                )
            prior = [
                item for item in record["artifacts"].get("return_inspections", [])
                if item["archive_sha256"] == archive_hash
            ]
            if not prior or prior[-1]["verdict"] != "blocked_limits":
                raise BridgeError(
                    "archive-limit override is allowed only after this exact quarantined "
                    "archive was blocked by a measured default limit"
                )
            original = destination / "original.zip"
            if not original.is_file() or sha256_file(original) != archive_hash:
                raise BridgeError("quarantined original is missing or changed; override is unsafe")
        attempt_destination = _attempt_destination(destination, selected_limits, retry)
        if attempt_destination.exists():
            raise BridgeError(f"this archive/limit inspection attempt already exists: {attempt_destination}")
        staging = Path(tempfile.mkdtemp(prefix=f"return-{record['id']}.", dir=kit / "runtime"))
        destination_created = False
        try:
            extraction_archive = destination / "original.zip" if retry else staging / "original.zip"
            if not retry:
                _copy_new(archive, extraction_archive)
                if sha256_file(extraction_archive) != archive_hash:
                    raise BridgeError("designer return changed while it was being quarantined")
            archive_bytes = extraction_archive.stat().st_size
            try:
                extracted = extract_archive(
                    extraction_archive,
                    staging / "tree",
                    limits=selected_limits,
                    root_markers=RETURN_ROOT_MARKERS,
                )
            except ArchiveLimitError as exc:
                report = {
                    "schema": REPORT_SCHEMA,
                    "round_id": record["id"],
                    "archive_sha256": archive_hash,
                    "archive_bytes": archive_bytes,
                    "archive_limits": selected_limits.as_dict(),
                    "archive_limit_override": not selected_limits.is_default,
                    "verdict": "blocked_limits",
                    "limit_finding": {
                        "field": exc.field,
                        "observed": exc.observed,
                        "limit": exc.limit,
                        "subject": exc.subject,
                    },
                    "security_findings": [],
                    "advanced": False,
                }
                atomic_write_json(staging / "report.json", report)
                attempt_destination.parent.mkdir(parents=True, exist_ok=True)
                promote_directory(staging, attempt_destination)
                destination_created = True
                inspection = {
                    "archive_sha256": archive_hash,
                    "verdict": report["verdict"],
                    "root": attempt_destination.relative_to(project).as_posix(),
                    "archive_root": destination.relative_to(project).as_posix(),
                    "report_sha256": sha256_file(attempt_destination / "report.json"),
                    "archive_limits": selected_limits.as_dict(),
                }
                _persist_inspection(project, state, record, inspection, advance=False)
                raise ReturnSafetyError(
                    "return archive was quarantined and exceeded a measured safety "
                    f"default: {exc}"
                )
            except BridgeError as exc:
                report = {
                    "schema": REPORT_SCHEMA, "round_id": record["id"],
                    "archive_sha256": archive_hash, "verdict": "blocked_safety",
                    "archive_limits": selected_limits.as_dict(),
                    "archive_limit_override": not selected_limits.is_default,
                    "security_findings": [str(exc)], "advanced": False,
                }
                atomic_write_json(staging / "report.json", report)
                attempt_destination.parent.mkdir(parents=True, exist_ok=True)
                promote_directory(staging, attempt_destination)
                destination_created = True
                inspection = {
                    "archive_sha256": archive_hash, "verdict": report["verdict"],
                    "root": attempt_destination.relative_to(project).as_posix(),
                    "archive_root": destination.relative_to(project).as_posix(),
                    "report_sha256": sha256_file(attempt_destination / "report.json"),
                    "archive_limits": selected_limits.as_dict(),
                }
                _persist_inspection(project, state, record, inspection, advance=False)
                raise ReturnSafetyError(f"return archive was quarantined but is unsafe: {exc}")
            returned_root = extracted.content_root
            baseline = read_json(baseline_root / "BASELINE-MANIFEST.json")
            validate_manifest(baseline, "outbound baseline manifest")
            returned = build_tree_manifest(returned_root, exclude={"BASELINE-MANIFEST.json"})
            escrow_entry = returned["files"].get(RETURNED_DATA_ESCROW)
            secrets = scan_secrets(returned_root, exclude=RETURN_ONLY_FILES)
            changes = classify_manifests(baseline, returned)
            surface = load_surface(baseline_root / "DESIGN-SURFACE.json")
            designer, off_limits = _classify_paths(changes, surface)
            documentation: list[str] = []
            mixed_findings: list[str] = []
            embedded_sources: dict[str, bytes] = {}
            index_path = surface["embedded"]["package_path"]
            if not (returned_root / index_path).is_file():
                documentation.append(f"returned package is missing mixed designer entrypoint: {index_path}")
            else:
                baseline_index = (baseline_root / index_path).read_bytes()
                returned_index = (returned_root / index_path).read_bytes()
                try:
                    embedded_sources = extract_embedded_sources(returned_index)
                    validate_embedded_sources(surface, embedded_sources)
                except BridgeError as exc:
                    documentation.append(str(exc))
                for check in (assert_outside_region_unchanged, assert_plumbing_unchanged):
                    try:
                        check(baseline_index, returned_index)
                    except BridgeError as exc:
                        mixed_findings.append(str(exc))
            manifest_path = returned_root / "BASELINE-MANIFEST.json"
            if not manifest_path.is_file() or sha256_file(manifest_path) != candidate["baseline_manifest_sha256"]:
                documentation.append("returned BASELINE-MANIFEST.json is missing or changed")
                kind = "modified" if manifest_path.is_file() else "removed"
                off_limits[kind].append("BASELINE-MANIFEST.json")
            note, note_findings = _return_note(returned_root)
            documentation.extend(note_findings)
            if escrow_entry is None:
                documentation.append(
                    f"returned package is missing required data escrow: {RETURNED_DATA_ESCROW}"
                )
            elif escrow_entry["bytes"] == 0:
                documentation.append(
                    f"required returned data escrow is empty: {RETURNED_DATA_ESCROW}"
                )
            declared_count = 0
            additions_value: dict[str, Any] | None = None
            try:
                additions_value = read_json(returned_root / "contract-additions.json")
                declared_count = validate_additions(additions_value, record["id"])
            except BridgeError as exc:
                additions_value = None
                documentation.append(str(exc))
            application_changes = {
                path
                for kind in ("added", "modified", "removed")
                for path in changes[kind]
                if path not in {"RETURN-NOTE.md", "contract-additions.json", *RETURN_ONLY_FILES}
            }
            unaccounted = sorted(path for path in application_changes if path not in note)
            if unaccounted:
                documentation.append("RETURN-NOTE.md does not name changed path(s): " + ", ".join(unaccounted))
            missing_ids = [
                item["id"] for item in (additions_value or {"additions": []})["additions"]
                if item["id"] not in note
            ]
            if missing_ids:
                documentation.append("RETURN-NOTE.md does not name addition ID(s): " + ", ".join(missing_ids))
            source_baseline = (
                _designer_source_manifest(returned_root, surface, embedded_sources)
                if embedded_sources else None
            )
            adoption_modes = read_json(baseline_root / "ADOPTION-MODES.json")
            expected_modes = adoption_screens(record)
            if (
                adoption_modes.get("schema") != "gpt-design-bridge/adoption-modes/v1"
                or adoption_modes.get("screens") != expected_modes
            ):
                raise BridgeError("outbound adoption-mode declaration is invalid or stale")
            security = [
                f"{item['path']}:{item['line']} ({item['rule']})" for item in secrets
            ]
            if security:
                verdict = "blocked_safety"
            elif documentation:
                verdict = "needs_resubmission"
            elif any(off_limits.values()) or mixed_findings:
                verdict = "owner_review_required"
            else:
                verdict = "ready_for_adoption_review"
            report = {
                "schema": REPORT_SCHEMA, "round_id": record["id"],
                "build_stamp": candidate["build_stamp"], "archive_sha256": archive_hash,
                "archive_bytes": archive_bytes,
                "archive_limits": selected_limits.as_dict(),
                "archive_limit_override": not selected_limits.is_default,
                "wrapper_descended": extracted.wrapper,
                "wrapper_chain_descended": list(extracted.wrapper_chain),
                "returned_tree_sha256": returned["tree_sha256"], "changes": changes,
                "designer_changes": designer, "off_limits_changes": off_limits,
                "mixed_region_findings": mixed_findings, "documentation_findings": documentation,
                "security_findings": security, "contract_addition_count": declared_count,
                "adoption_modes": adoption_modes,
                "return_source_baseline": source_baseline,
                "relevant_markup": {
                    index_path: {
                        "bytes": (returned_root / index_path).stat().st_size,
                        "sha256": sha256_file(returned_root / index_path),
                    }
                } if (returned_root / index_path).is_file() else {},
                "returned_data_escrow": {
                    "path": RETURNED_DATA_ESCROW,
                    "present": escrow_entry is not None,
                    "bytes": escrow_entry["bytes"] if escrow_entry is not None else None,
                    "sha256": escrow_entry["sha256"] if escrow_entry is not None else None,
                    "content_inspected": False,
                    "automatic_action": "none",
                    "owner_instruction_required": True,
                },
                "verdict": verdict, "advanced": verdict in {
                    "owner_review_required", "ready_for_adoption_review"
                },
            }
            atomic_write_json(staging / "report.json", report)
            attempt_destination.parent.mkdir(parents=True, exist_ok=True)
            promote_directory(staging, attempt_destination)
            destination_created = True
            inspection = {
                "archive_sha256": archive_hash, "verdict": verdict,
                "root": attempt_destination.relative_to(project).as_posix(),
                "archive_root": destination.relative_to(project).as_posix(),
                "report_sha256": sha256_file(attempt_destination / "report.json"),
                "archive_limits": selected_limits.as_dict(),
            }
            _persist_inspection(project, state, record, inspection, advance=report["advanced"])
            if security:
                raise ReturnSafetyError(
                    "return was quarantined with high-confidence secret finding(s): "
                    + ", ".join(security)
                )
            return report
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
