"""Persisted designer-round lifecycle for GPT Design Bridge."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from bridge_core import (
    ACTIVE_ROUND_STATUSES,
    BridgeError,
    OPENABLE_PHASES,
    ROUND_SCHEMA,
    atomic_write_json,
    canonical_json,
    exclusive_lock,
    kit_root,
    load_project,
    load_round,
    non_empty,
    promote_directory,
    read_json,
    records_root,
    round_ids,
    round_record_path,
    round_root,
    safe_relative_path,
    sha256_file,
    validate_iso_date,
    validate_round,
    validate_state,
)
PROOF_SCHEMA = "gpt-design-bridge/browser-proof/v1"
BRIEF_DATA_SCHEMA = "gpt-design-bridge/drop-brief-data/v1"
FRESH_INSTANCE_FIELDS = {
    "SUPERSEDED_BASELINE_OR_NONE": "This courier is the complete current baseline.",
    "REPLACEMENT_INSTRUCTION_AND_REASON_OR_NOT_APPLICABLE": (
        "Use only the files and instructions included in this courier."
    ),
}
PRIOR_EXPERIENCE = re.compile(
    r"(?i)(?:"
    r"\bsupersed\w*\b|"
    r"\b(?:previous|prior|earlier)\s+"
    r"(?:drop|courier|package|designer|round|baseline|experience)\b|"
    r"\bas\s+(?:before|previously|last\s+time)\b"
    r")"
)
ROUND_REFERENCE = re.compile(r"(?i)\bround[-\s]+0*([0-9]+)\b")
DEFERRAL_CORRECTION_SCHEMA = "gpt-design-bridge/deferral-correction/v1"
GENERATED_PACKAGE_FILES = {
    "ADOPTION-MODES.json",
    "BASELINE-MANIFEST.json",
    "DESIGNER-CONSTITUTION.md",
    "OFF-LIMITS.json",
    "OFF-LIMITS.md",
    "OWNER-RULES-DESIGN.md",
    "PRESERVATION-BASELINE.json",
    "RETURN-NOTE.md",
    "RETURNING-THIS-DROP.md",
    "contract-additions.json",
    "contract-additions.schema.json",
}
ADOPTION_MODES = {"exact", "characterized", "reference"}
NEXT_STATUS = {
    "outbound_open": "awaiting_return",
    "awaiting_return": "return_received",
    "return_received": "adopting",
    "adopting": "proving",
    "proving": "sealed",
}
def _unique_paths(values: list[str], label: str) -> list[str]:
    if not values:
        raise BridgeError(f"at least one {label} is required")
    normalized = [safe_relative_path(value, label=label) for value in values]
    if len(set(normalized)) != len(normalized):
        raise BridgeError(f"{label} contains a duplicate path")
    return normalized
def next_round_id(project_root: Path) -> str:
    ids = round_ids(project_root)
    number = max((int(item) for item in ids), default=0) + 1
    return f"{number:03d}"
def _assert_paths_exist(project_root: Path, values: list[str], label: str) -> None:
    for relative in values:
        candidate = (project_root / relative).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as exc:
            raise BridgeError(f"{label} resolves outside the project: {relative}") from exc
        if not candidate.exists():
            raise BridgeError(f"{label} does not exist: {relative}")
def _persist_pair(project_root: Path, state: dict[str, Any], record: dict[str, Any]) -> None:
    state_path = kit_root(project_root) / "state.json"
    record_path = round_record_path(project_root, record["id"])
    validate_state(state, state_path)
    validate_round(record, record_path)
    previous_record = record_path.read_bytes()
    try:
        atomic_write_json(record_path, record)
        atomic_write_json(state_path, state)
    except Exception:
        atomic_write_json(record_path, json.loads(previous_record))
        raise


def persist_round_state(
    project_root: Path, state: dict[str, Any], record: dict[str, Any]
) -> None:
    """Validate and atomically persist one active round plus lifecycle state."""
    _persist_pair(project_root, state, record)


def operational_template(path: Path, sample_heading: str, title: str) -> str:
    text = path.read_text(encoding="utf-8")
    text = text.split(f"\n---\n\n# {sample_heading}", 1)[0]
    first_heading = text.index("\n## ")
    return f"# {title}\n{text[first_heading:]}"


def _validate_browser_context(context: Any, label: str, *, normal: bool) -> None:
    if (
        not isinstance(context, dict)
        or not isinstance(context.get("summary"), str)
        or not context["summary"].strip()
        or not isinstance(context.get("artifact"), str)
        or not context["artifact"].strip()
    ):
        raise BridgeError(f"browser proof context is invalid: {label}")
    if normal and (
        context.get("rendered") is not True
        or type(context.get("root_children")) is not int
        or context["root_children"] < 1
        or type(context.get("text_chars")) is not int
        or context["text_chars"] < 1
        or context.get("console_errors") != []
        or context.get("failed_requests") != []
    ):
        raise BridgeError(f"browser proof context did not pass: {label}")


def validate_browser_proof(proof: dict[str, Any], candidate: dict[str, Any]) -> None:
    if (
        proof.get("schema") != PROOF_SCHEMA
        or proof.get("build_stamp") != candidate["build_stamp"]
        or proof.get("package_sha256") != candidate["archive_sha256"]
    ):
        raise BridgeError("browser proof is not bound to the current package")
    browser = proof.get("browser")
    if (
        not isinstance(browser, dict)
        or browser.get("name") not in {"Google Chrome", "Chromium"}
        or browser.get("visible") is not True
        or browser.get("independent") is not True
        or not isinstance(browser.get("version"), str) or not browser["version"].strip()
        or not isinstance(browser.get("profile"), str) or not browser["profile"].strip()
    ):
        raise BridgeError("browser proof did not use visible independent Chrome/Chromium")
    contexts = proof.get("contexts")
    required_contexts = {
        "file",
        "deep_http",
        "broken_mount",
        "home_without_backend",
    }
    if not isinstance(contexts, dict) or not required_contexts.issubset(contexts):
        missing = sorted(required_contexts - set(contexts or {}))
        raise BridgeError(
            "browser proof must contain every required context; missing=" + ", ".join(missing)
        )
    for label, context in contexts.items():
        if label not in required_contexts:
            _validate_browser_context(context, label, normal=False)
    _validate_browser_context(contexts["file"], "file", normal=True)
    _validate_browser_context(contexts["deep_http"], "deep_http", normal=True)
    if not str(contexts["file"].get("url", "")).startswith("file:"):
        raise BridgeError("file browser proof URL is not file://")
    if not str(contexts["deep_http"].get("url", "")).startswith(("http://", "https://")):
        raise BridgeError("deep browser proof URL is not HTTP")
    if contexts["broken_mount"].get("visible_error") is not True:
        raise BridgeError("broken-mount proof lacks a visible error")
    home = contexts["home_without_backend"]
    if home.get("request_attempted") is not True or home.get("visible_error") is not True:
        raise BridgeError("home-without-backend proof did not fail loudly")
    interactions = proof.get("interactions")
    if not isinstance(interactions, list) or not interactions:
        raise BridgeError("browser proof must exercise at least one interaction")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str) or not item["id"].strip()
        or item.get("status") != "pass"
        or item.get("input") not in {"click", "keyboard"}
        or not isinstance(item.get("summary"), str) or not item["summary"].strip()
        or not isinstance(item.get("artifact"), str) or not item["artifact"].strip()
        for item in interactions
    ):
        raise BridgeError("browser interaction proof contains an invalid result")
    parity = proof.get("parity")
    if (
        not isinstance(parity, dict)
        or set(parity) != {"path", "sha256"}
        or not isinstance(parity.get("path"), str)
        or not parity["path"].startswith(".gpt-design-bridge/evidence/")
        or not re.fullmatch(r"[0-9a-f]{64}", parity.get("sha256", ""))
    ):
        raise BridgeError("browser proof must bind one exact production-to-travelling parity report")


def browser_artifact_references(proof: dict[str, Any]) -> list[str]:
    references = [item["artifact"] for item in proof["contexts"].values()]
    references.extend(item["artifact"] for item in proof["interactions"])
    return list(dict.fromkeys(references))


def verify_proof_artifacts(project: Path, proof: dict[str, Any]) -> None:
    references = browser_artifact_references(proof)
    for reference in references:
        if not isinstance(reference, str) or not reference.startswith("evidence/"):
            raise BridgeError(
                "browser proof artifact must be a regular file under evidence/: "
                f"{reference!r}"
            )
        relative = safe_relative_path(reference, label="browser proof artifact")
        path = (project / relative).resolve()
        try:
            path.relative_to(project)
        except ValueError as exc:
            raise BridgeError(f"browser proof artifact escapes the project: {relative}") from exc
        if not path.is_file() or path.is_symlink():
            raise BridgeError(
                f"browser proof artifact must be a regular file under evidence/: {relative}"
            )


def _validate_fresh_instance_brief(
    fields: dict[str, str], generated: dict[str, str]
) -> None:
    for name, expected in FRESH_INSTANCE_FIELDS.items():
        if fields.get(name) != expected:
            raise BridgeError(
                "drop brief for a fresh designer instance requires the standalone "
                f"value for {name}"
            )
    current_round = generated.get("ROUND_ID", "")
    if not current_round.isdigit():
        raise BridgeError("generated ROUND_ID must be numeric before brief rendering")
    current_number = int(current_round)
    for name, value in {**fields, **generated}.items():
        if PRIOR_EXPERIENCE.search(value):
            raise BridgeError(
                "drop brief for a fresh designer instance must not depend on prior "
                f"history: {name}"
            )
        references = {int(match) for match in ROUND_REFERENCE.findall(value)}
        if references - {current_number}:
            raise BridgeError(
                "drop brief for a fresh designer instance references another round: "
                f"{name}"
            )


def render_brief(kit: Path, data_path: Path, generated: dict[str, str]) -> str:
    data = read_json(data_path)
    if set(data) != {"schema", "fields"} or data.get("schema") != BRIEF_DATA_SCHEMA:
        raise BridgeError("drop brief data must use the strict v1 schema")
    fields = data["fields"]
    if not isinstance(fields, dict) or any(
        not isinstance(value, str) or not value for value in fields.values()
    ):
        raise BridgeError("every drop brief field value must be a non-empty string")
    _validate_fresh_instance_brief(fields, generated)
    template = operational_template(
        kit / "templates" / "DROP-BRIEF.template.md",
        "Worked sample — Statecraft request 003 (real reference round)",
        "Designer drop brief",
    )
    names = set(re.findall(r"<<([A-Z0-9_-]+)>>", template))
    overlap = names & set(generated) & set(fields)
    if overlap:
        raise BridgeError("brief data tries to override generated fields: " + ", ".join(sorted(overlap)))
    values = {**fields, **generated}
    missing, extra = names - set(values), set(fields) - names
    if missing or extra:
        raise BridgeError(
            f"brief fields mismatch; missing={sorted(missing)}, unexpected={sorted(extra)}"
        )
    for name in sorted(names):
        template = template.replace(f"<<{name}>>", values[name])
    if re.search(r"<<[A-Z0-9_-]+>>", template):
        raise BridgeError("filled drop brief still contains a placeholder")
    return template


def build_source_manifest(sources: dict[str, bytes]) -> dict[str, Any]:
    files = {
        path: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in sorted(sources.items())
    }
    return {
        "schema": "gpt-design-bridge/source-manifest/v1",
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files.values()),
        "tree_sha256": hashlib.sha256(canonical_json(files)).hexdigest(),
        "files": files,
    }


def scope_covers(root: str, surfaces: list[str]) -> bool:
    return any(root == item or root.startswith(item + "/") for item in surfaces)


def adoption_screens(record: dict[str, Any]) -> list[dict[str, str]]:
    """Return a complete per-entrypoint mode declaration, including legacy records."""
    declared = record["scope"].get("screens")
    if declared is None:
        return [
            {"id": entrypoint, "entrypoint": entrypoint, "mode": "exact"}
            for entrypoint in record["scope"]["entrypoints"]
        ]
    return [dict(item) for item in declared]


def open_round(
    project_root: Path,
    *,
    goal: str,
    owner_request: str,
    designer_surface: list[str],
    entrypoints: list[str],
    route_prefixes: list[str],
    provenance_id: str,
    adoption_mode: str = "exact",
) -> dict[str, Any]:
    root = kit_root(project_root)
    with exclusive_lock(root / "runtime" / "mutation.lock", "round-open"):
        _config, state = load_project(project_root)
        if state["active_round"] is not None:
            raise BridgeError(f"round {state['active_round']['id']} is already active")
        if state["phase"] not in OPENABLE_PHASES:
            raise BridgeError(f"cannot open a round from lifecycle phase {state['phase']!r}")
        surfaces = _unique_paths(designer_surface, "designer surface path")
        entries = _unique_paths(entrypoints, "package entrypoint")
        if adoption_mode not in ADOPTION_MODES:
            raise BridgeError(
                "adoption mode must be exact, characterized, or reference"
            )
        _assert_paths_exist(project_root, surfaces, "designer surface path")
        _assert_paths_exist(project_root, entries, "package entrypoint")
        from bridge_provenance import round_provenance_binding

        provenance = round_provenance_binding(
            project_root,
            provenance_id,
            surfaces,
            entries,
        )
        prefixes = list(dict.fromkeys(route_prefixes))
        if not prefixes:
            raise BridgeError("at least one route prefix is required")
        generation = state["generation"] + 1
        round_id = next_round_id(project_root)
        record = {
            "schema": ROUND_SCHEMA,
            "id": round_id,
            "status": "outbound_open",
            "opened_from_phase": state["phase"],
            "goal": non_empty(goal, "goal"),
            "owner_request": non_empty(owner_request, "owner request"),
            "scope": {
                "designer_surface": surfaces,
                "entrypoints": entries,
                "route_prefixes": prefixes,
                "screens": [
                    {"id": entrypoint, "entrypoint": entrypoint, "mode": adoption_mode}
                    for entrypoint in entries
                ],
            },
            "provenance": provenance,
            "owner_rulings": [],
            "events": [{"generation": generation, "event": "round_opened"}],
            "artifacts": {},
        }
        validate_round(record, round_record_path(project_root, round_id))
        destination = round_root(project_root, round_id)
        staging = root / "runtime" / f"round-{round_id}.create-{os.getpid()}"
        if destination.exists() or staging.exists():
            raise BridgeError(f"round path collision for {round_id}")
        try:
            staging.mkdir()
            atomic_write_json(staging / "round.json", record)
            declarations = {
                "schema": "gpt-design-bridge/contract-additions/v1",
                "round_id": round_id,
                "additions": [],
            }
            atomic_write_json(staging / "contract-additions.json", declarations)
            promote_directory(staging, destination)
            state["generation"] = generation
            state["phase"] = "outbound_open"
            state["active_round"] = {"id": round_id, "status": "outbound_open"}
            atomic_write_json(root / "state.json", state)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            if destination.exists():
                shutil.rmtree(destination)
            raise
    return record
def apply_transition(
    state: dict[str, Any],
    record: dict[str, Any],
    target: str,
    *,
    event: str,
    details: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
) -> None:
    current = record["status"]
    expected = NEXT_STATUS.get(current)
    if target != expected:
        raise BridgeError(
            f"illegal round transition {current!r} -> {target!r}; expected {expected!r}"
        )
    active = state.get("active_round")
    if not isinstance(active, dict) or active.get("id") != record["id"]:
        raise BridgeError(f"round {record['id']} is not the active round")
    if target == "sealed":
        open_debt = [
            item["id"] for item in state["deferred_obligations"] if item["status"] == "open"
        ]
        if open_debt:
            raise BridgeError(
                "cannot seal with open deferred obligations: " + ", ".join(open_debt)
            )
    generation = state["generation"] + 1
    event_record: dict[str, Any] = {"generation": generation, "event": non_empty(event, "event")}
    if details:
        event_record["details"] = details
    record["status"] = target
    record["events"].append(event_record)
    if artifacts:
        collisions = set(record["artifacts"]).intersection(artifacts)
        if collisions:
            raise BridgeError(f"artifact keys already recorded: {', '.join(sorted(collisions))}")
        record["artifacts"].update(artifacts)
    state["generation"] = generation
    if target == "sealed":
        state["phase"] = "sealed"
        state["active_round"] = None
        state["sealed_rounds"].append(record["id"])
    else:
        state["phase"] = target
        state["active_round"] = {"id": record["id"], "status": target}
def add_owner_ruling(
    project_root: Path, *, question: str, decision: str, ruling_date: str
) -> dict[str, Any]:
    root = kit_root(project_root)
    with exclusive_lock(root / "runtime" / "mutation.lock", "owner-ruling"):
        _config, state = load_project(project_root)
        active = state["active_round"]
        if active is None:
            raise BridgeError("an owner ruling requires an active designer round")
        record = load_round(project_root, active["id"])
        ruling_id = f"R-{len(record['owner_rulings']) + 1:03d}"
        ruling = {
            "id": ruling_id,
            "by": "owner",
            "date": validate_iso_date(ruling_date, "ruling date"),
            "question": non_empty(question, "ruling question"),
            "decision": non_empty(decision, "ruling decision"),
        }
        generation = state["generation"] + 1
        record["owner_rulings"].append(ruling)
        record["events"].append(
            {"generation": generation, "event": "owner_ruling_recorded", "ruling_id": ruling_id}
        )
        state["generation"] = generation
        _persist_pair(project_root, state, record)
    return ruling


def add_capability_ruling(
    project_root: Path,
    *,
    capability_id: str,
    change: str,
    replacement_id: str | None,
    question: str,
    decision: str,
    ruling_date: str,
    user_approved: bool,
) -> dict[str, Any]:
    """Record one prompt-scoped owner decision for an exact parity difference."""
    if user_approved is not True:
        raise BridgeError(
            "capability rulings require --user-approved after the owner approves this exact change"
        )
    if change not in {"remove", "replace", "change"}:
        raise BridgeError("capability change must be remove, replace, or change")
    capability = non_empty(capability_id, "capability ID")
    if change == "replace":
        replacement = non_empty(replacement_id, "replacement capability ID")
    elif replacement_id is not None:
        raise BridgeError("replacement ID is valid only for a replace ruling")
    else:
        replacement = None
    root = kit_root(project_root)
    with exclusive_lock(root / "runtime" / "mutation.lock", "capability-ruling"):
        _config, state = load_project(project_root)
        active = state["active_round"]
        if active is None:
            raise BridgeError("a capability ruling requires an active designer round")
        record = load_round(project_root, active["id"])
        existing = {
            item.get("capability_change", {}).get("capability_id")
            for item in record["owner_rulings"]
        }
        if capability in existing:
            raise BridgeError(f"capability already has an owner ruling: {capability}")
        ruling_id = f"R-{len(record['owner_rulings']) + 1:03d}"
        ruling = {
            "id": ruling_id,
            "by": "owner",
            "date": validate_iso_date(ruling_date, "ruling date"),
            "question": non_empty(question, "ruling question"),
            "decision": non_empty(decision, "ruling decision"),
            "capability_change": {
                "capability_id": capability,
                "change": change,
                "replacement_id": replacement,
            },
        }
        generation = state["generation"] + 1
        record["owner_rulings"].append(ruling)
        record["events"].append(
            {
                "generation": generation,
                "event": "owner_capability_change_approved",
                "ruling_id": ruling_id,
                "capability_id": capability,
            }
        )
        state["generation"] = generation
        _persist_pair(project_root, state, record)
    return ruling
def add_deferral(
    project_root: Path, *, obligation: str, reason: str, discharge_gate: str
) -> dict[str, Any]:
    root = kit_root(project_root)
    with exclusive_lock(root / "runtime" / "mutation.lock", "defer"):
        _config, state = load_project(project_root)
        existing = state["deferred_obligations"]
        numbers = [
            int(item["id"].split("-", 1)[1])
            for item in existing
            if re.fullmatch(r"D-[0-9]{3,}", item.get("id", ""))
        ]
        deferral_id = f"D-{max(numbers, default=0) + 1:03d}"
        generation = state["generation"] + 1
        active = state["active_round"]
        item = {
            "id": deferral_id,
            "status": "open",
            "round_id": active["id"] if active else None,
            "obligation": non_empty(obligation, "obligation"),
            "reason": non_empty(reason, "deferral reason"),
            "discharge_gate": non_empty(discharge_gate, "discharge gate"),
            "created_generation": generation,
        }
        existing.append(item)
        state["generation"] = generation
        if active:
            record = load_round(project_root, active["id"])
            record["events"].append(
                {"generation": generation, "event": "obligation_deferred", "deferral_id": deferral_id}
            )
            _persist_pair(project_root, state, record)
        else:
            validate_state(state, root / "state.json")
            atomic_write_json(root / "state.json", state)
    return item


def correct_deferral(
    project_root: Path,
    *,
    deferral_id: str,
    obligation: str,
    reason: str,
    discharge_gate: str,
    correction_reason: str,
    audit_path: Path,
) -> dict[str, Any]:
    root = kit_root(project_root)
    with exclusive_lock(root / "runtime" / "mutation.lock", "deferral-correct"):
        _config, state = load_project(project_root)
        match = next(
            (item for item in state["deferred_obligations"] if item["id"] == deferral_id),
            None,
        )
        if match is None:
            raise BridgeError(f"unknown deferred obligation: {deferral_id}")
        if match["status"] != "open":
            raise BridgeError(f"only an open deferred obligation can be corrected: {deferral_id}")
        target = (
            audit_path.resolve()
            if audit_path.is_absolute()
            else (project_root / audit_path).resolve()
        )
        runtime = (root / "runtime").resolve()
        try:
            target.relative_to(runtime)
        except ValueError as exc:
            raise BridgeError("deferral correction audit must be under bridge runtime/") from exc
        relative = target.relative_to(project_root.resolve()).as_posix()
        if target.exists() or target.is_symlink():
            raise BridgeError(f"deferral correction audit already exists: {relative}")
        if not target.parent.is_dir() or target.parent.is_symlink():
            raise BridgeError(f"deferral correction audit parent is unavailable: {relative}")
        before = {
            "obligation": match["obligation"],
            "reason": match["reason"],
            "discharge_gate": match["discharge_gate"],
        }
        after = {
            "obligation": non_empty(obligation, "corrected obligation"),
            "reason": non_empty(reason, "corrected deferral reason"),
            "discharge_gate": non_empty(discharge_gate, "corrected discharge gate"),
        }
        if before == after:
            raise BridgeError("deferral correction must change at least one governed field")
        generation = state["generation"] + 1
        active = state["active_round"]
        audit = {
            "schema": DEFERRAL_CORRECTION_SCHEMA,
            "deferral_id": deferral_id,
            "generation": generation,
            "round_id": active["id"] if active else None,
            "correction_reason": non_empty(correction_reason, "correction reason"),
            "before": before,
            "after": after,
        }
        atomic_write_json(target, audit)
        audit_sha256 = sha256_file(target)
        try:
            match.update(after)
            state["generation"] = generation
            if active:
                record = load_round(project_root, active["id"])
                record["events"].append(
                    {
                        "generation": generation,
                        "event": "obligation_corrected",
                        "deferral_id": deferral_id,
                        "audit": relative,
                        "audit_sha256": audit_sha256,
                    }
                )
                _persist_pair(project_root, state, record)
            else:
                validate_state(state, root / "state.json")
                atomic_write_json(root / "state.json", state)
        except Exception:
            match.update(before)
            target.unlink(missing_ok=True)
            raise
    return {
        "deferral": match,
        "audit": relative,
        "audit_sha256": audit_sha256,
    }


def discharge_deferral(project_root: Path, *, deferral_id: str, evidence: str) -> dict[str, Any]:
    root = kit_root(project_root)
    with exclusive_lock(root / "runtime" / "mutation.lock", "discharge"):
        _config, state = load_project(project_root)
        match = next(
            (item for item in state["deferred_obligations"] if item["id"] == deferral_id),
            None,
        )
        if match is None:
            raise BridgeError(f"unknown deferred obligation: {deferral_id}")
        if match["status"] != "open":
            raise BridgeError(f"deferred obligation is already discharged: {deferral_id}")
        generation = state["generation"] + 1
        match["status"] = "discharged"
        match["evidence"] = non_empty(evidence, "discharge evidence")
        match["discharged_generation"] = generation
        active = state["active_round"]
        match["discharged_in_round"] = active["id"] if active else None
        state["generation"] = generation
        if active:
            record = load_round(project_root, active["id"])
            record["events"].append(
                {"generation": generation, "event": "obligation_discharged", "deferral_id": deferral_id}
            )
            _persist_pair(project_root, state, record)
        else:
            validate_state(state, root / "state.json")
            atomic_write_json(root / "state.json", state)
    return match
def abandon_round(project_root: Path, *, reason: str) -> dict[str, Any]:
    root = kit_root(project_root)
    with exclusive_lock(root / "runtime" / "mutation.lock", "round-abandon"):
        _config, state = load_project(project_root)
        active = state["active_round"]
        if active is None:
            raise BridgeError("there is no active designer round to abandon")
        if active["status"] in {"adopting", "proving"}:
            raise BridgeError(
                f"cannot abandon round {active['id']} after adoption began; complete or explicitly recover it"
            )
        record = load_round(project_root, active["id"])
        generation = state["generation"] + 1
        record["status"] = "abandoned"
        record["events"].append(
            {
                "generation": generation,
                "event": "round_abandoned",
                "reason": non_empty(reason, "abandon reason"),
            }
        )
        state["generation"] = generation
        state["phase"] = record["opened_from_phase"]
        state["active_round"] = None
        _persist_pair(project_root, state, record)
    return record
