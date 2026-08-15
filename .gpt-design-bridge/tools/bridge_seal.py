"""Integration, post-adoption reproof, and final round seal gates."""
from __future__ import annotations
import hashlib
import difflib
import os
import re
import shutil
from pathlib import Path
from typing import Any
from bridge_adopt import _collect_source, _returned_root, _returned_sources
from bridge_artifacts import build_tree_manifest
from bridge_core import (
    BridgeError,
    atomic_write_json,
    canonical_json,
    exclusive_lock,
    kit_root,
    load_project,
    load_round,
    read_json,
    run_git,
    safe_relative_path,
    sha256_file,
    validate_iso_date,
)
from bridge_rounds import (
    apply_transition,
    browser_artifact_references,
    build_source_manifest,
    persist_round_state,
    validate_browser_proof,
    verify_proof_artifacts,
)
from bridge_surface import load_surface
from bridge_provenance import (
    post_adoption_provenance_binding,
    validate_post_adoption_provenance,
)


INTEGRATION_SCHEMA = "gpt-design-bridge/integration-evidence/v1"
SEAL_SCHEMA = "gpt-design-bridge/seal-evidence/v1"
REQUIRED_CHECKS = {"static", "unit", "integration", "build", "route-parity"}
EXACT_PARITY_CHECKS = {
    "source-parity", "dom-parity", "visual-parity", "interaction-parity"
}
CHARACTERIZED_PARITY_CHECKS = {
    "field-contract", "dom-parity", "interaction-parity"
}
SOURCE_EXCLUDED_ROOTS = {
    ".git", ".gpt-design-bridge", "node_modules", "dist", "evidence", "coverage"
}
def project_source_manifest(project: Path) -> dict[str, Any]:
    listed = run_git(
        project, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ).stdout.split("\0")
    files: dict[str, dict[str, Any]] = {}
    for value in sorted(filter(None, listed), key=str.casefold):
        if value.split("/", 1)[0].casefold() in SOURCE_EXCLUDED_ROOTS:
            continue
        relative = safe_relative_path(value, label="project source path")
        path = project / relative
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise BridgeError(f"project source contains a symbolic/non-regular file: {relative}")
        files[relative] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    if not files:
        raise BridgeError("project source fingerprint contains no files")
    return {
        "schema": "gpt-design-bridge/project-source-manifest/v1",
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files.values()),
        "tree_sha256": hashlib.sha256(canonical_json(files)).hexdigest(),
        "files": files,
    }
def _evidence_path(project: Path, value: Path, label: str) -> tuple[Path, str]:
    path = value.resolve()
    try:
        relative = path.relative_to(project).as_posix()
    except ValueError as exc:
        raise BridgeError(f"{label} must be inside the project evidence directory") from exc
    relative = safe_relative_path(relative, label=label)
    if not relative.startswith("evidence/") or not path.is_file() or path.is_symlink():
        raise BridgeError(f"{label} must be a regular file under evidence/: {relative}")
    return path, relative
def _artifact(project: Path, value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise BridgeError(f"{label} artifact must be a path string")
    path, relative = _evidence_path(project, project / value, f"{label} artifact")
    if not path.is_file():
        raise BridgeError(f"{label} artifact is missing: {relative}")
    return relative
def _designer_state(
    project: Path, record: dict[str, Any]
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    candidate = record["artifacts"]["outbound_candidates"][-1]
    surface = load_surface(project / candidate["root"] / "package" / "DESIGN-SURFACE.json")
    roots = [surface["embedded"]["source_root"], *[item["source_root"] for item in surface["direct"]]]
    extensions = {
        surface["embedded"]["source_root"]: surface["embedded"]["extensions"],
        **{item["source_root"]: item["extensions"] for item in surface["direct"]},
    }
    current = {
        path: content for root in roots
        for path, content in _collect_source(project, root, extensions[root]).items()
    }
    inspection = record["artifacts"]["return_inspections"][-1]
    mode = record["artifacts"]["adoption"].get("adoption_mode", "exact")
    if mode == "reference":
        backup = project / record["artifacts"]["adoption"]["backup_root"] / "sources"
        adopted = {
            path: content for root in roots
            for path, content in _collect_source(backup, root, extensions[root]).items()
        }
    else:
        quarantine = project / inspection["root"]
        report = read_json(quarantine / "report.json")
        returned_root = _returned_root(quarantine, report)
        _by_root, adopted = _returned_sources(returned_root, surface)
    return adopted, current
def _source_adjustments(
    before: dict[str, bytes], after: dict[str, bytes]
) -> dict[str, tuple[str | None, str | None]]:
    result: dict[str, tuple[str | None, str | None]] = {}
    for path in sorted(set(before) | set(after)):
        old = hashlib.sha256(before[path]).hexdigest() if path in before else None
        new = hashlib.sha256(after[path]).hexdigest() if path in after else None
        if old != new:
            result[path] = (old, new)
    return result


def _line_change_ranges(before: bytes | None, after: bytes | None) -> list[dict[str, Any]]:
    old = before.decode("utf-8", errors="replace").splitlines()
    new = after.decode("utf-8", errors="replace").splitlines()
    ranges: list[dict[str, Any]] = []
    for tag, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag != "equal":
            ranges.append(
                {
                    "change": tag,
                    "before_lines": [old_start + 1, old_end],
                    "after_lines": [new_start + 1, new_end],
                }
            )
    return ranges


def _decoded(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", errors="replace")


def _jsx_signatures(value: bytes | None) -> dict[str, list[str]]:
    text = _decoded(value)
    tags = [
        f"{closing or 'open'}:{name}"
        for closing, name in re.findall(r"<\s*(/?)\s*([A-Za-z][\w.$:-]*)\b", text)
    ]
    classes = [
        " ".join(next(item for item in match if item).split())
        for match in re.findall(
            r"\bclassName\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|`([^`]*)`)",
            text,
            flags=re.DOTALL,
        )
    ]
    text_nodes = [
        " ".join(item.split())
        for item in re.findall(r">([^<>{}]+)<", text, flags=re.DOTALL)
        if item.strip()
    ]
    visible_attributes = [
        f"{name}:{' '.join(value.split())}"
        for name, value in re.findall(
            r"\b(aria-label|placeholder|alt)\s*=\s*[\"']([^\"']*)[\"']",
            text,
            flags=re.IGNORECASE,
        )
    ]
    return {
        "hierarchy": tags,
        "classes": classes,
        "visible_copy": [*text_nodes, *visible_attributes],
    }


def _css_signature(value: bytes | None) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", _decoded(value), flags=re.DOTALL)
    return [
        f"{name.casefold()}:{' '.join(raw_value.split())}"
        for name, raw_value in re.findall(
            r"([A-Za-z_-][\w-]*)\s*:\s*([^;{}]+)\s*;", text
        )
    ]


def _signature_change(
    before: bytes | None,
    after: bytes | None,
    extractor: Any,
    key: str | None = None,
) -> str:
    if before is None or after is None:
        return "changed"
    old = extractor(before)
    new = extractor(after)
    if key is not None:
        old, new = old[key], new[key]
    return "changed" if old != new else "unchanged"


def _source_preservation_report(
    before: dict[str, bytes], after: dict[str, bytes], mode: str
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after), key=str.casefold):
        old, new = before.get(path), after.get(path)
        identical = old is not None and new is not None and old == new
        changed = not identical
        jsx = path.endswith((".js", ".jsx", ".ts", ".tsx"))
        css = path.endswith(".css")
        rows.append(
            {
                "path": path,
                "status": (
                    "unchanged" if identical else "added" if old is None
                    else "removed" if new is None else "modified"
                ),
                "before_sha256": hashlib.sha256(old).hexdigest() if old is not None else None,
                "after_sha256": hashlib.sha256(new).hexdigest() if new is not None else None,
                "line_changes": _line_change_ranges(old or b"", new or b"") if changed else [],
                "jsx_hierarchy_changed": (
                    _signature_change(old, new, _jsx_signatures, "hierarchy")
                    if changed and jsx else "unchanged" if jsx else "not-applicable"
                ),
                "class_names_changed": (
                    _signature_change(old, new, _jsx_signatures, "classes")
                    if changed and jsx else "unchanged" if jsx else "not-applicable"
                ),
                "visible_copy_changed": (
                    _signature_change(old, new, _jsx_signatures, "visible_copy")
                    if changed and jsx else "unchanged" if jsx else "not-applicable"
                ),
                "css_declarations_changed": (
                    _signature_change(old, new, _css_signature)
                    if changed and css else "unchanged" if css else "not-applicable"
                ),
                "approved_mechanical_adjustment": False,
            }
        )
    return {
        "schema": "gpt-design-bridge/source-preservation/v1",
        "mode": mode,
        "analysis_basis": (
            "byte hashes and complete line ranges; JSX hierarchy/classes/visible copy "
            "and CSS declarations use conservative lexical signatures"
        ),
        "byte_identical": all(item["status"] == "unchanged" for item in rows),
        "files": rows,
    }


def required_integration_checks(mode: str) -> set[str]:
    if mode == "exact":
        return REQUIRED_CHECKS | EXACT_PARITY_CHECKS
    if mode == "characterized":
        return REQUIRED_CHECKS | CHARACTERIZED_PARITY_CHECKS
    if mode == "reference":
        return set(REQUIRED_CHECKS)
    raise BridgeError(f"unknown adoption mode in integration gate: {mode!r}")
def complete_integration(
    project: Path,
    evidence_path: Path,
    provenance_id: str,
) -> dict[str, Any]:
    kit = kit_root(project)
    evidence_path, evidence_relative = _evidence_path(
        project, evidence_path, "integration evidence"
    )
    with exclusive_lock(kit / "runtime" / "mutation.lock", "adopt-integrate"):
        _config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] != "adopting":
            raise BridgeError("adopt-integrate requires an adopting round")
        record = load_round(project, active["id"])
        adoption = record["artifacts"]["adoption"]
        mode = adoption.get("adoption_mode", "exact")
        accepted_baseline_path = project / adoption["accepted_baseline"]
        if sha256_file(accepted_baseline_path) != adoption["accepted_baseline_sha256"]:
            raise BridgeError("accepted designer baseline changed after adoption")
        accepted_baseline = read_json(accepted_baseline_path)
        if accepted_baseline.get("mode") != mode:
            raise BridgeError("accepted designer baseline mode disagrees with adoption")
        if mode == "exact":
            policy_path = project / ".gpt-blackbox-lite-policy.json"
            if policy_path.is_file():
                policy = read_json(policy_path)
                missing_protection = sorted(
                    set(accepted_baseline["sealed_paths"])
                    - set(policy.get("protected_paths", []))
                )
                if missing_protection:
                    raise BridgeError(
                        "BlackBox policy no longer protects exact designer paths: "
                        + ", ".join(missing_protection)
                    )
        evidence = read_json(evidence_path)
        if set(evidence) != {
            "schema", "round_id", "adoption_stamp", "source_tree_sha256",
            "checks", "contract_additions", "designer_adjustments",
        } or (
            evidence.get("schema") != INTEGRATION_SCHEMA
            or evidence.get("round_id") != record["id"]
            or evidence.get("adoption_stamp") != adoption["adoption_stamp"]
        ):
            raise BridgeError("integration evidence is not strictly bound to this adoption")
        checks = evidence["checks"]
        required_checks = required_integration_checks(mode)
        if not isinstance(checks, list) or len(checks) != len(required_checks):
            raise BridgeError("integration evidence has the wrong check count")
        if {item.get("id") for item in checks if isinstance(item, dict)} != required_checks:
            raise BridgeError("integration evidence must cover every required check exactly once")
        for item in checks:
            if set(item) != {"id", "command", "status", "artifact", "summary"}:
                raise BridgeError(f"integration check shape is invalid: {item.get('id')!r}")
            if (
                item["status"] != "pass"
                or not isinstance(item["command"], str) or not item["command"].strip()
                or not isinstance(item["summary"], str) or not item["summary"].strip()
            ):
                raise BridgeError(f"integration check did not pass: {item['id']}")
            _artifact(project, item["artifact"], f"integration check {item['id']}")
        results = evidence["contract_additions"]
        tasks = adoption["integration_tasks"]
        if not isinstance(results, list) or len(results) != len(tasks) or {
            item.get("id") for item in results if isinstance(item, dict)
        } != {item["id"] for item in tasks}:
            raise BridgeError("integration evidence must resolve every contract addition exactly once")
        for result in results:
            if set(result) != {"id", "status", "artifact", "summary"}:
                raise BridgeError(f"contract integration result shape is invalid: {result.get('id')!r}")
            task = next(item for item in tasks if item["id"] == result["id"])
            expected = "implemented" if task["status"] == "pending" else "owner-declined"
            if result["status"] != expected:
                raise BridgeError(f"contract addition {result['id']} requires status {expected}")
            _artifact(project, result["artifact"], f"contract addition {result['id']}")
        before, current = _designer_state(project, record)
        actual_adjustments = _source_adjustments(before, current)
        preservation = _source_preservation_report(before, current, mode)
        observed_path = kit / "runtime" / f"source-preservation-{record['id']}.json"
        atomic_write_json(observed_path, preservation)
        if mode == "exact" and actual_adjustments:
            changed = ", ".join(sorted(actual_adjustments))
            raise BridgeError(
                "exact designer source changed after adoption; adapters belong outside "
                f"sealed UI files. Changed: {changed}. Full line-range report: {observed_path}"
            )
        adjustments = evidence["designer_adjustments"]
        if not isinstance(adjustments, list) or len(adjustments) != len(actual_adjustments):
            raise BridgeError("designer adjustment evidence does not match post-adoption source")
        adjustment_map = {
            item.get("path"): item for item in adjustments if isinstance(item, dict)
        }
        if set(adjustment_map) != set(actual_adjustments):
            raise BridgeError("designer adjustment evidence does not name every changed path")
        for path, hashes in actual_adjustments.items():
            item = adjustment_map[path]
            if set(item) != {"path", "reason", "before_sha256", "after_sha256"} or (
                item["before_sha256"], item["after_sha256"]
            ) != hashes or not isinstance(item["reason"], str) or not item["reason"].strip():
                raise BridgeError(f"designer adjustment evidence is invalid: {path}")
        source = project_source_manifest(project)
        if evidence["source_tree_sha256"] != source["tree_sha256"]:
            raise BridgeError("integration evidence is stale for the current project source")
        provenance = post_adoption_provenance_binding(
            project,
            record,
            provenance_id,
        )
        target = kit / "records" / "rounds" / record["id"] / "integration-evidence.json"
        preservation_target = (
            kit / "records" / "rounds" / record["id"] / "source-preservation.json"
        )
        if target.exists():
            raise BridgeError(f"integration evidence is already recorded: {target}")
        if preservation_target.exists():
            raise BridgeError(
                f"source preservation evidence is already recorded: {preservation_target}"
            )
        atomic_write_json(target, evidence)
        atomic_write_json(preservation_target, preservation)
        try:
            for task in tasks:
                result = next(item for item in results if item["id"] == task["id"])
                task["status"] = result["status"].replace("-", "_")
                task["evidence"] = result["artifact"]
            integration = {
                "evidence_sha256": sha256_file(target),
                "evidence_source": evidence_relative,
                "source_tree_sha256": source["tree_sha256"],
                "check_ids": sorted(required_checks),
                "designer_adjustment_count": len(adjustments),
                "adoption_mode": mode,
                "source_preservation_sha256": sha256_file(preservation_target),
                "source_byte_identical": preservation["byte_identical"],
                "provenance": provenance["binding"],
                "provenance_transition": provenance["transition"],
            }
            record["artifacts"]["integration"] = integration
            apply_transition(
                state, record, "proving", event="integration_completed",
                details={"evidence_sha256": integration["evidence_sha256"]},
            )
            persist_round_state(project, state, record)
            observed_path.unlink(missing_ok=True)
        except Exception:
            target.unlink(missing_ok=True)
            preservation_target.unlink(missing_ok=True)
            raise
    return integration


def record_reproof(project: Path, proof_path: Path) -> dict[str, Any]:
    kit = kit_root(project)
    proof_path, proof_relative = _evidence_path(project, proof_path, "reproof evidence")
    with exclusive_lock(kit / "runtime" / "mutation.lock", "reproof-record"):
        _config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] != "proving":
            raise BridgeError("reproof-record requires a proving round")
        record = load_round(project, active["id"])
        validate_post_adoption_provenance(project, record)
        candidates = record["artifacts"].get("reproof_candidates", [])
        if not candidates:
            raise BridgeError("reproof-record requires a post-adoption rebuilt drop")
        candidate = candidates[-1]
        integration = record["artifacts"]["integration"]
        if project_source_manifest(project)["tree_sha256"] != integration["source_tree_sha256"]:
            raise BridgeError("project source changed after integration evidence")
        _before, current = _designer_state(project, record)
        if build_source_manifest(current)["tree_sha256"] != candidate["source_tree_sha256"]:
            raise BridgeError("rebuilt drop is stale for the current designer source")
        proof = read_json(proof_path)
        if proof.get("candidate_id") != candidate["candidate_id"]:
            raise BridgeError("reproof evidence is not bound to the latest rebuild occurrence")
        validate_browser_proof(proof, candidate)
        verify_proof_artifacts(project, proof)
        candidate_root = project / candidate["root"]
        if sha256_file(candidate_root / candidate["archive_name"]) != candidate["archive_sha256"]:
            raise BridgeError("reproof package bytes changed after rebuild")
        destination = kit / "evidence" / record["id"] / f"reproof-{candidate['build_stamp']}"
        if destination.exists():
            raise BridgeError(f"reproof evidence already exists: {destination}")
        destination.mkdir(parents=True)
        try:
            atomic_write_json(destination / "browser-proof.json", proof)
            for reference in browser_artifact_references(proof):
                target = destination / "artifacts" / reference
                target.parent.mkdir(parents=True, exist_ok=True)
                with (project / reference).open("rb") as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
            reproof = {
                "candidate_id": candidate["candidate_id"],
                "build_stamp": candidate["build_stamp"],
                "package_sha256": candidate["archive_sha256"],
                "proof_sha256": sha256_file(destination / "browser-proof.json"),
                "evidence_tree_sha256": build_tree_manifest(destination)["tree_sha256"],
                "proof_source": proof_relative,
                "root": destination.relative_to(project).as_posix(),
            }
            record["artifacts"].setdefault("reproofs", []).append(reproof)
            generation = state["generation"] + 1
            state["generation"] = generation
            record["events"].append(
                {
                    "generation": generation, "event": "post_adoption_drop_reproved",
                    "build_stamp": candidate["build_stamp"],
                }
            )
            persist_round_state(project, state, record)
        except Exception:
            shutil.rmtree(destination)
            raise
    return reproof


def seal_round(project: Path, evidence_path: Path) -> dict[str, Any]:
    kit = kit_root(project)
    evidence_path, evidence_relative = _evidence_path(project, evidence_path, "seal evidence")
    with exclusive_lock(kit / "runtime" / "mutation.lock", "round-seal"):
        _config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] != "proving":
            raise BridgeError("round-seal requires a proving round")
        record = load_round(project, active["id"])
        validate_post_adoption_provenance(project, record)
        integration = record["artifacts"].get("integration")
        candidates = record["artifacts"].get("reproof_candidates", [])
        reproofs = record["artifacts"].get("reproofs", [])
        if not integration or not candidates or not reproofs:
            raise BridgeError("round-seal requires integration, rebuilt drop, and reproof evidence")
        candidate, reproof = candidates[-1], reproofs[-1]
        if (
            reproof["build_stamp"] != candidate["build_stamp"]
            or reproof["candidate_id"] != candidate["candidate_id"]
        ):
            raise BridgeError("latest rebuilt drop has no current reproof")
        source = project_source_manifest(project)
        if source["tree_sha256"] != integration["source_tree_sha256"]:
            raise BridgeError("project source changed after integration/reproof")
        _before, current = _designer_state(project, record)
        if build_source_manifest(current)["tree_sha256"] != candidate["source_tree_sha256"]:
            raise BridgeError("designer source changed after rebuilt drop")
        if any(item["status"] == "pending" for item in record["artifacts"]["adoption"]["integration_tasks"]):
            raise BridgeError("contract integration tasks remain pending")
        evidence = read_json(evidence_path)
        expected = {
            "schema": SEAL_SCHEMA, "round_id": record["id"],
            "adoption_stamp": record["artifacts"]["adoption"]["adoption_stamp"],
            "source_tree_sha256": source["tree_sha256"],
            "integration_evidence_sha256": integration["evidence_sha256"],
            "reproof_candidate_id": candidate["candidate_id"],
            "reproof_build_stamp": candidate["build_stamp"],
            "reproof_proof_sha256": reproof["proof_sha256"],
        }
        if set(evidence) != {
            *expected, "owner_date", "decision", "summary"
        } or any(evidence.get(key) != value for key, value in expected.items()):
            raise BridgeError("seal evidence is not strictly bound to current round artifacts")
        if evidence.get("decision") != "seal":
            raise BridgeError("owner did not authorize the final seal")
        validate_iso_date(evidence.get("owner_date"), "seal owner date")
        if not isinstance(evidence.get("summary"), str) or not evidence["summary"].strip():
            raise BridgeError("seal evidence requires an owner summary")
        target = kit / "records" / "rounds" / record["id"] / "seal-evidence.json"
        if target.exists():
            raise BridgeError(f"seal evidence is already recorded: {target}")
        atomic_write_json(target, evidence)
        try:
            ruling_id = f"R-{len(record['owner_rulings']) + 1:03d}"
            record["owner_rulings"].append(
                {
                    "id": ruling_id, "by": "owner", "date": evidence["owner_date"],
                    "question": "Seal this exact adopted, integrated, rebuilt, and re-proved round?",
                    "decision": f"seal: {evidence['summary']}",
                    "source_finding_id": "FINAL-SEAL",
                }
            )
            sealed = {
                "evidence_sha256": sha256_file(target), "evidence_source": evidence_relative,
                "source_tree_sha256": source["tree_sha256"],
                "package_sha256": candidate["archive_sha256"],
                "proof_sha256": reproof["proof_sha256"],
            }
            record["artifacts"]["seal"] = sealed
            apply_transition(
                state, record, "sealed", event="round_sealed",
                details={"evidence_sha256": sealed["evidence_sha256"]},
            )
            persist_round_state(project, state, record)
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return sealed
