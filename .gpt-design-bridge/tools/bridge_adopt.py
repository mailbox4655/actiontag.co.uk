"""Transactional wholesale adoption of a verified designer surface."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from bridge_artifacts import build_tree_manifest
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
    safe_relative_path,
    sha256_file,
    validate_iso_date,
)
from bridge_return import validate_additions
from bridge_rounds import apply_transition, build_source_manifest, persist_round_state
from bridge_surface import (
    extract_embedded_sources,
    load_surface,
    map_direct_package_path,
    validate_embedded_sources,
)


DECISIONS_SCHEMA = "gpt-design-bridge/adoption-decisions/v1"
ACCEPTED_BASELINE_SCHEMA = "gpt-design-bridge/accepted-designer-baseline/v1"


def required_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for change in ("added", "modified", "removed"):
        for path in sorted(report["off_limits_changes"][change]):
            findings.append({"kind": f"off_limits_{change}", "subject": path})
    for message in sorted(report["mixed_region_findings"]):
        findings.append({"kind": "mixed_region", "subject": message})
    return [
        {"id": f"F-{index:03d}", **finding}
        for index, finding in enumerate(findings, 1)
    ]


def validate_decisions(
    value: dict[str, Any],
    round_id: str,
    report_sha256: str,
    findings: list[dict[str, str]],
    addition_ids: list[str],
) -> None:
    if set(value) != {"schema", "round_id", "report_sha256", "findings", "additions"}:
        raise BridgeError("adoption decisions have unexpected or missing fields")
    if (
        value.get("schema") != DECISIONS_SCHEMA
        or value.get("round_id") != round_id
        or value.get("report_sha256") != report_sha256
    ):
        raise BridgeError("adoption decisions are not bound to this report and round")
    finding_decisions = value.get("findings")
    addition_decisions = value.get("additions")
    if not isinstance(finding_decisions, list) or not isinstance(addition_decisions, list):
        raise BridgeError("adoption finding/addition decisions must be arrays")
    expected_findings = {item["id"] for item in findings}
    if len(finding_decisions) != len(expected_findings) or {
        item.get("id") for item in finding_decisions if isinstance(item, dict)
    } != expected_findings:
        raise BridgeError("adoption decisions do not cover every ambiguity exactly once")
    if len(addition_decisions) != len(addition_ids) or {
        item.get("id") for item in addition_decisions if isinstance(item, dict)
    } != set(addition_ids):
        raise BridgeError("adoption decisions do not cover every contract addition exactly once")
    for item in finding_decisions:
        if not isinstance(item, dict) or set(item) != {"id", "decision", "owner_date", "reason"}:
            raise BridgeError("adoption finding decision shape is invalid")
        if item["decision"] == "abort":
            raise BridgeError(f"owner aborted adoption at finding {item['id']}: {item['reason']}")
        if item["decision"] != "preserve-engineering":
            raise BridgeError(f"finding {item['id']} must preserve engineering or abort")
        validate_iso_date(item["owner_date"], f"finding {item['id']} owner date")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise BridgeError(f"finding {item['id']} requires an owner reason")
    for item in addition_decisions:
        if not isinstance(item, dict) or set(item) != {"id", "decision", "owner_date", "reason"}:
            raise BridgeError("contract addition decision shape is invalid")
        if item["decision"] not in {"implement", "owner-declined"}:
            raise BridgeError(f"contract addition {item['id']} decision is invalid")
        validate_iso_date(item["owner_date"], f"addition {item['id']} owner date")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise BridgeError(f"addition {item['id']} requires an owner reason")


def _collect_source(project: Path, root: str, extensions: list[str]) -> dict[str, bytes]:
    directory = project / root
    if not directory.is_dir() or directory.is_symlink():
        raise BridgeError(f"designer source root is missing or symbolic: {root}")
    result: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(project).as_posix()
        if path.is_symlink():
            raise BridgeError(f"designer source contains a symbolic link: {relative}")
        if path.is_dir() or path.name == ".gitkeep":
            continue
        if not path.is_file() or path.suffix not in extensions:
            raise BridgeError(f"designer source has an undeclared file type: {relative}")
        result[relative] = path.read_bytes()
    return result


def _copy_tree(source: Path, target: Path) -> str:
    target.mkdir(parents=True)
    files = [path for path in source.rglob("*") if path.is_file()]
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise BridgeError(f"cannot back up symbolic designer path: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    if not files:
        return hashlib.sha256(canonical_json({})).hexdigest()
    before = build_tree_manifest(source)["tree_sha256"]
    after = build_tree_manifest(target)["tree_sha256"]
    if before != after:
        raise BridgeError(f"pre-adoption backup verification failed: {source}")
    return before


def _write_tree(target: Path, root: str, files: dict[str, bytes]) -> None:
    target.mkdir(parents=True)
    prefix = root + "/"
    for path, content in sorted(files.items()):
        if not path.startswith(prefix):
            raise BridgeError(f"adopted source path escapes declared root {root}: {path}")
        relative = safe_relative_path(path[len(prefix) :], label="adopted source path")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)


def _returned_sources(
    returned_root: Path, surface: dict[str, Any]
) -> tuple[dict[str, dict[str, bytes]], dict[str, bytes]]:
    embedded = surface["embedded"]
    embedded_sources = extract_embedded_sources(
        (returned_root / embedded["package_path"]).read_bytes()
    )
    validate_embedded_sources(surface, embedded_sources)
    by_root: dict[str, dict[str, bytes]] = {embedded["source_root"]: embedded_sources}
    all_sources = dict(embedded_sources)
    for entry in surface["direct"]:
        mapped: dict[str, bytes] = {}
        package_root = returned_root / entry["package_root"]
        if package_root.exists():
            for path in sorted(package_root.rglob("*"), key=lambda item: item.as_posix()):
                if path.is_symlink():
                    raise BridgeError(f"returned designer surface contains a symbolic link: {path}")
                if not path.is_file():
                    continue
                package_path = path.relative_to(returned_root).as_posix()
                try:
                    source_path = map_direct_package_path(surface, package_path)
                except BridgeError:
                    continue
                if source_path:
                    mapped[source_path] = path.read_bytes()
        by_root[entry["source_root"]] = mapped
        all_sources.update(mapped)
    return by_root, all_sources


def _append_rulings(record: dict[str, Any], decisions: dict[str, Any], findings: list[dict[str, str]]) -> None:
    finding_map = {item["id"]: item for item in findings}
    for item in [*decisions["findings"], *decisions["additions"]]:
        ruling_id = f"R-{len(record['owner_rulings']) + 1:03d}"
        question = (
            f"{finding_map[item['id']]['kind']}: {finding_map[item['id']]['subject']}"
            if item["id"] in finding_map
            else f"contract addition: {item['id']}"
        )
        record["owner_rulings"].append(
            {
                "id": ruling_id, "by": "owner", "date": item["owner_date"],
                "question": question,
                "decision": f"{item['decision']}: {item['reason']}",
                "source_finding_id": item["id"],
            }
        )


def _returned_root(quarantine: Path, report: dict[str, Any]) -> Path:
    chain = report.get("wrapper_chain_descended")
    if chain is None:
        legacy = report.get("wrapper_descended")
        chain = legacy.split("/") if legacy else []
    if not isinstance(chain, list) or any(not isinstance(item, str) for item in chain):
        raise BridgeError("return inspection wrapper chain is invalid")
    return (quarantine / "tree").joinpath(*chain)


def _effective_adoption_mode(report: dict[str, Any]) -> str:
    modes = report.get("adoption_modes", {}).get("screens")
    if not isinstance(modes, list) or not modes:
        return "exact"
    values = {item.get("mode") for item in modes if isinstance(item, dict)}
    if len(values) != 1 or next(iter(values)) not in {"exact", "characterized", "reference"}:
        raise BridgeError(
            "one shared designer source surface requires one unambiguous adoption mode"
        )
    return next(iter(values))


def _protect_exact_paths(project: Path, paths: list[str]) -> tuple[Path, bytes, str] | None:
    policy_path = project / ".gpt-blackbox-lite-policy.json"
    if not policy_path.is_file():
        return None
    before = policy_path.read_bytes()
    policy = read_json(policy_path)
    protected = policy.get("protected_paths")
    if (
        policy.get("schema") != "gpt-blackbox-lite-lanes"
        or policy.get("version") != 1
        or not isinstance(protected, list)
        or any(not isinstance(item, str) for item in protected)
    ):
        raise BridgeError(
            "cannot protect accepted exact designer files: BlackBox lane policy is invalid"
        )
    policy["protected_paths"] = sorted(set(protected) | set(paths), key=str.casefold)
    atomic_write_json(policy_path, policy)
    return policy_path, before, sha256_file(policy_path)


def adopt_return(project: Path, decisions_path: Path) -> dict[str, Any]:
    kit = kit_root(project)
    with exclusive_lock(kit / "runtime" / "mutation.lock", "adopt-apply"):
        _config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] != "return_received":
            raise BridgeError("adopt-apply requires a return_received round")
        record = load_round(project, active["id"])
        inspection = record["artifacts"]["return_inspections"][-1]
        if inspection["verdict"] not in {"owner_review_required", "ready_for_adoption_review"}:
            raise BridgeError(f"latest return is not adoptable: {inspection['verdict']}")
        quarantine = project / inspection["root"]
        archive_root = project / inspection.get("archive_root", inspection["root"])
        report_path = quarantine / "report.json"
        if sha256_file(report_path) != inspection["report_sha256"]:
            raise BridgeError("return inspection report changed after intake")
        report = read_json(report_path)
        returned_root = _returned_root(quarantine, report)
        returned_manifest = build_tree_manifest(returned_root, exclude={"BASELINE-MANIFEST.json"})
        if returned_manifest["tree_sha256"] != report["returned_tree_sha256"]:
            raise BridgeError("quarantined extracted return changed after intake")
        if sha256_file(archive_root / "original.zip") != inspection["archive_sha256"]:
            raise BridgeError("quarantined return archive changed after intake")
        candidate = record["artifacts"]["outbound_candidates"][-1]
        surface = load_surface(project / candidate["root"] / "package" / "DESIGN-SURFACE.json")
        additions = read_json(returned_root / "contract-additions.json")
        validate_additions(additions, record["id"])
        findings = required_findings(report)
        decisions = read_json(decisions_path)
        validate_decisions(
            decisions, record["id"], inspection["report_sha256"], findings,
            [item["id"] for item in additions["additions"]],
        )
        roots = [surface["embedded"]["source_root"], *[item["source_root"] for item in surface["direct"]]]
        extensions = {
            surface["embedded"]["source_root"]: surface["embedded"]["extensions"],
            **{item["source_root"]: item["extensions"] for item in surface["direct"]},
        }
        current_sources = {
            path: content for root in roots
            for path, content in _collect_source(project, root, extensions[root]).items()
        }
        current_manifest = build_source_manifest(current_sources)
        if current_manifest["tree_sha256"] != candidate["source_tree_sha256"]:
            raise BridgeError("designer source drifted after outbound; refusing to discard concurrent work")
        by_root, returned_sources = _returned_sources(returned_root, surface)
        returned_source_manifest = build_source_manifest(returned_sources)
        if returned_source_manifest != report.get("return_source_baseline"):
            raise BridgeError("return source baseline no longer matches quarantined designer bytes")
        adoption_mode = _effective_adoption_mode(report)
        installed_sources = current_sources if adoption_mode == "reference" else returned_sources
        adopted_manifest = build_source_manifest(installed_sources)
        decision_hash = hashlib.sha256(canonical_json(decisions)).hexdigest()
        adoption_stamp = hashlib.sha256(
            canonical_json(
                {
                    "report": inspection["report_sha256"], "decisions": decision_hash,
                    "before": current_manifest["tree_sha256"], "after": adopted_manifest["tree_sha256"],
                    "mode": adoption_mode,
                }
            )
        ).hexdigest()[:32]
        backup = kit / "baselines" / record["id"] / f"adoption-{adoption_stamp}"
        if backup.exists():
            raise BridgeError(f"adoption backup already exists: {backup}")
        transaction = Path(tempfile.mkdtemp(prefix=f"adopt-{record['id']}.", dir=kit / "runtime"))
        swapped: list[tuple[Path, Path, Path]] = []
        created: list[Path] = []
        policy_change: tuple[Path, bytes, str] | None = None
        committed = False
        try:
            backup_stage = transaction / "backup"
            new_stage = transaction / "new"
            backup_stage.mkdir()
            new_stage.mkdir()
            backup_hashes: dict[str, str] = {}
            for index, root in enumerate(roots):
                backup_hashes[root] = _copy_tree(project / root, backup_stage / "sources" / root)
                if adoption_mode != "reference":
                    _write_tree(new_stage / str(index), root, by_root[root])
            atomic_write_json(
                backup_stage / "metadata.json",
                {
                    "schema": "gpt-design-bridge/adoption-backup/v1",
                    "round_id": record["id"], "adoption_stamp": adoption_stamp,
                    "source_tree_sha256": current_manifest["tree_sha256"],
                    "root_tree_sha256": backup_hashes,
                },
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            promote_directory(backup_stage, backup)
            atomic_write_json(
                transaction / "journal.json",
                {"operation": "wholesale-adoption", "roots": roots, "status": "swapping"},
            )
            if adoption_mode != "reference":
                for index, root in enumerate(roots):
                    current = project / root
                    old = transaction / "old" / str(index)
                    old.parent.mkdir(parents=True, exist_ok=True)
                    promote_directory(current, old)
                    try:
                        promote_directory(new_stage / str(index), current)
                    except Exception:
                        promote_directory(old, current)
                        raise
                    swapped.append((current, old, new_stage / str(index)))
            decisions_target = kit / "records" / "rounds" / record["id"] / "adoption-decisions.json"
            additions_target = kit / "contract" / "additions" / f"{record['id']}.json"
            if decisions_target.exists() or additions_target.exists():
                raise BridgeError("adoption decision/addition record already exists")
            atomic_write_json(decisions_target, decisions)
            created.append(decisions_target)
            atomic_write_json(additions_target, additions)
            created.append(additions_target)
            baseline_target = (
                kit / "contract" / "accepted-designer-baselines" / f"{record['id']}.json"
            )
            if baseline_target.exists():
                raise BridgeError(f"accepted designer baseline already exists: {baseline_target}")
            accepted_baseline = {
                "schema": ACCEPTED_BASELINE_SCHEMA,
                "round_id": record["id"],
                "mode": adoption_mode,
                "screens": report["adoption_modes"]["screens"],
                "return_report_sha256": inspection["report_sha256"],
                "returned_source_manifest": returned_source_manifest,
                "installed_source_manifest": adopted_manifest,
                "relevant_markup": report.get("relevant_markup", {}),
                "sealed_paths": (
                    sorted(returned_source_manifest["files"])
                    if adoption_mode == "exact" else []
                ),
                "adapter_boundary": (
                    "Routing, authentication, APIs, navigation, and persistence are "
                    "injected outside sealed UI files."
                ),
            }
            atomic_write_json(baseline_target, accepted_baseline)
            created.append(baseline_target)
            if adoption_mode == "exact":
                policy_change = _protect_exact_paths(
                    project, accepted_baseline["sealed_paths"]
                )
            _append_rulings(record, decisions, findings)
            tasks = [
                {
                    "id": item["id"],
                    "status": "pending" if decision["decision"] == "implement" else "owner_declined",
                    "decision": decision["decision"],
                    "reason": decision["reason"],
                }
                for item in additions["additions"]
                for decision in decisions["additions"]
                if decision["id"] == item["id"]
            ]
            adoption = {
                "adoption_stamp": adoption_stamp,
                "return_report_sha256": inspection["report_sha256"],
                "decision_sha256": decision_hash,
                "declarations_sha256": sha256_file(additions_target),
                "backup_root": backup.relative_to(project).as_posix(),
                "before_source_tree_sha256": current_manifest["tree_sha256"],
                "adopted_source_tree_sha256": adopted_manifest["tree_sha256"],
                "returned_source_tree_sha256": returned_source_manifest["tree_sha256"],
                "adoption_mode": adoption_mode,
                "accepted_baseline": baseline_target.relative_to(project).as_posix(),
                "accepted_baseline_sha256": sha256_file(baseline_target),
                "blackbox_policy_sha256": policy_change[2] if policy_change else None,
                "integration_tasks": tasks,
            }
            record["artifacts"]["adoption"] = adoption
            apply_transition(
                state, record, "adopting", event="designer_surface_adopted",
                details={"adoption_stamp": adoption_stamp},
            )
            persist_round_state(project, state, record)
            committed = True
            shutil.rmtree(transaction)
        except Exception:
            if not committed:
                for current, old, rejected in reversed(swapped):
                    if current.exists():
                        rejected.parent.mkdir(parents=True, exist_ok=True)
                        promote_directory(current, rejected)
                    if old.exists():
                        promote_directory(old, current)
                for path in created:
                    path.unlink(missing_ok=True)
                if policy_change is not None:
                    policy_path, before, _after_hash = policy_change
                    policy_path.write_bytes(before)
            if transaction.exists():
                shutil.rmtree(transaction)
            raise
    return adoption
