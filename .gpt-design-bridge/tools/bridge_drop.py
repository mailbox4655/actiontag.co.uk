"""Outbound package, proof, and courier operations."""
from __future__ import annotations
import hashlib
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from bridge_artifacts import build_tree_manifest, create_deterministic_zip, scan_secrets
from bridge_core import (
    BridgeError,
    atomic_write_json,
    canonical_json,
    exclusive_lock,
    kit_root,
    load_project,
    load_round,
    non_empty,
    promote_directory,
    read_json,
    safe_relative_path,
    sha256_file,
)
from bridge_rounds import (
    BRIEF_DATA_SCHEMA,
    GENERATED_PACKAGE_FILES,
    PROOF_SCHEMA,
    adoption_screens,
    apply_transition,
    browser_artifact_references,
    build_source_manifest,
    operational_template as _operational_template,
    persist_round_state,
    render_brief as _render_brief,
    scope_covers,
    validate_browser_proof,
    verify_proof_artifacts,
)
from bridge_surface import (
    extract_embedded_sources,
    is_designer_package_path,
    load_surface,
    map_direct_package_path,
    validate_embedded_sources,
)
from bridge_travelling_assets import validate_travelling_asset_pack
from bridge_provenance import (
    public_preservation_baseline,
    validate_parity_reference,
    validate_post_adoption_provenance,
    validate_round_provenance,
)


WINDOWS_PROMOTION_ATTEMPTS = int(
    os.environ.get("GDB_WINDOWS_PROMOTION_ATTEMPTS", "121")
)
WINDOWS_PROMOTION_DELAY_SECONDS = float(
    os.environ.get("GDB_WINDOWS_PROMOTION_DELAY_SECONDS", "0.25")
)
WINDOWS_STAGING_ATTEMPTS = int(
    os.environ.get("GDB_WINDOWS_STAGING_ATTEMPTS", "256")
)


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
def _copy_new(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)


def _promote_directory(
    source: Path,
    destination: Path,
    *,
    replace=None,
    sleep=time.sleep,
    windows: bool = os.name == "nt",
) -> None:
    promote_directory(
        source,
        destination,
        replace=replace,
        sleep=sleep,
        windows=windows,
        attempts=WINDOWS_PROMOTION_ATTEMPTS,
        delay_seconds=WINDOWS_PROMOTION_DELAY_SECONDS,
    )


def _create_staging_directory(
    parent: Path,
    prefix: str,
    *,
    windows: bool = os.name == "nt",
    mkdtemp=tempfile.mkdtemp,
    token_factory=None,
    create_directory=None,
) -> Path:
    if not parent.is_dir() or parent.is_symlink():
        raise BridgeError(f"staging parent is missing or symbolic: {parent}")
    if not windows:
        return Path(mkdtemp(prefix=prefix, dir=parent))
    token = token_factory or (lambda: uuid.uuid4().hex)
    create = create_directory or (lambda path: path.mkdir())
    for _attempt in range(WINDOWS_STAGING_ATTEMPTS):
        candidate = parent / f"{prefix}{token()}"
        try:
            create(candidate)
        except FileExistsError:
            continue
        if not candidate.is_dir() or candidate.is_symlink():
            raise BridgeError(f"staging creator did not produce a real directory: {candidate}")
        return candidate
    raise BridgeError("could not create a unique Windows staging directory")


def _collect_source(project: Path, root: str, extensions: list[str]) -> dict[str, bytes]:
    directory = project / root
    if not directory.is_dir() or directory.is_symlink():
        raise BridgeError(f"declared source root is missing or symbolic: {root}")
    result: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(project).as_posix()
        if path.is_symlink():
            raise BridgeError(f"designer source contains a symbolic link: {relative}")
        if path.is_dir():
            continue
        if path.name == ".gitkeep":
            continue
        if not path.is_file() or path.suffix not in extensions:
            raise BridgeError(f"designer source has an undeclared file type: {relative}")
        result[relative] = path.read_bytes()
    return result
def verify_prepared_stage(
    project: Path,
    stage: Path,
    record: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if provenance is None:
        validate_round_provenance(project, record)
    resolved = stage.resolve()
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise BridgeError(f"prepared stage must be inside the project: {resolved}") from exc
    reserved = {part.casefold() for part in resolved.parts}
    if not resolved.is_dir() or {".gpt-design-bridge", ".git"} & reserved:
        raise BridgeError(f"prepared stage is missing or reserved: {resolved}")
    for name in GENERATED_PACKAGE_FILES:
        if (resolved / name).exists():
            raise BridgeError(f"prepared stage collides with generated package file: {name}")
    surface = load_surface(resolved / "DESIGN-SURFACE.json")
    for entrypoint in record["scope"]["entrypoints"]:
        if not (resolved / safe_relative_path(entrypoint, label="package entrypoint")).is_file():
            raise BridgeError(f"prepared stage is missing package entrypoint: {entrypoint}")
    sources: dict[str, bytes] = {}
    embedded = surface["embedded"]
    if not scope_covers(embedded["source_root"], record["scope"]["designer_surface"]):
        raise BridgeError(f"embedded source root is outside round scope: {embedded['source_root']}")
    expected_embedded = _collect_source(project, embedded["source_root"], embedded["extensions"])
    returned_embedded = extract_embedded_sources((resolved / embedded["package_path"]).read_bytes())
    validate_embedded_sources(surface, returned_embedded)
    if returned_embedded != expected_embedded:
        raise BridgeError("prepared embedded designer sources do not exactly match project source")
    sources.update(expected_embedded)
    for entry in surface["direct"]:
        if not scope_covers(entry["source_root"], record["scope"]["designer_surface"]):
            raise BridgeError(f"direct source root is outside round scope: {entry['source_root']}")
        expected = _collect_source(project, entry["source_root"], entry["extensions"])
        package_root = resolved / entry["package_root"]
        actual: dict[str, bytes] = {}
        if package_root.exists():
            files = [item for item in package_root.rglob("*") if item.is_file()]
            if files:
                manifest = build_tree_manifest(package_root)
                for relative in manifest["files"]:
                    package_path = f"{entry['package_root']}/{relative}"
                    source_path = map_direct_package_path(surface, package_path)
                    actual[source_path] = (resolved / package_path).read_bytes()
        if actual != expected:
            raise BridgeError(f"prepared direct surface does not match source: {entry['package_root']}")
        sources.update(expected)
    validate_travelling_asset_pack(resolved)
    secrets = scan_secrets(resolved)
    if secrets:
        summary = ", ".join(f"{item['path']}:{item['line']} ({item['rule']})" for item in secrets)
        raise BridgeError(f"prepared stage contains high-confidence secret material: {summary}")
    return surface, build_source_manifest(sources)
def _copy_package_docs(
    project: Path,
    kit: Path,
    package: Path,
    record: dict[str, Any],
    surface: dict[str, Any],
    provenance: dict[str, Any],
) -> None:
    round_id = record["id"]
    contract = kit / "contract"
    templates = kit / "templates"
    _copy_new(contract / "CONSTITUTION.md", package / "DESIGNER-CONSTITUTION.md")
    _copy_new(contract / "OWNER-RULES-DESIGN.md", package / "OWNER-RULES-DESIGN.md")
    _write_new(
        package / "PRESERVATION-BASELINE.json",
        canonical_json(public_preservation_baseline(project, record, provenance)),
    )
    return_note = _operational_template(
        templates / "RETURN-NOTE.template.md",
        "Worked sample — Statecraft accepted round 001",
        "Designer return note",
    )
    _write_new(package / "RETURN-NOTE.md", return_note.encode("utf-8"))
    _copy_new(templates / "contract-additions.schema.json", package / "contract-additions.schema.json")
    accepted = kit / "contract" / "additions" / f"{round_id}.json"
    declarations = read_json(
        accepted if accepted.is_file() else kit / "records" / "rounds" / round_id / "contract-additions.json"
    )
    _write_new(package / "contract-additions.json", canonical_json(declarations))
    returning = (
        "# Returning this drop\n\n"
        "Follow this procedure in order. It is a byte-integrity requirement, not a "
        "formatting preference.\n\n"
        "1. Start from a fresh extraction of the original sealed outbound package. "
        "Do not start from an earlier return, a designer-application export, or a "
        "folder previously described as cleaned.\n"
        "2. Overlay only the intended designer-owned changes, the filled "
        "`RETURN-NOTE.md`, `contract-additions.json`, and the final "
        "`travelling-data-export.sqlite3`.\n"
        "3. Preserve every undeclared and off-limits file byte-for-byte from the "
        "original package. Treat supposedly untouched files as opaque bytes. Do not "
        "allow design, export, optimization, formatting, sanitization, or archive "
        "tooling to parse and reserialize files under `assets/pickers/**`.\n"
        "4. Before archiving, compare every supposedly unchanged path with its "
        "SHA-256 entry in `BASELINE-MANIFEST.json`. Restore every mismatch from the "
        "original sealed package. Do not claim a path was untouched unless its hash "
        "matches.\n"
        "5. If the packaging process cannot preserve untouched bytes or verify the "
        "manifest hashes, stop and report that exact limitation. Do not return an "
        "unverified or rewritten archive.\n"
        "6. Return the complete effective package tree, not a selection of changed "
        "files. Put that root directly in the archive or beneath any harmless chain "
        "of wrapper directories. Engineering discovers exactly one effective root by "
        "its required marker files and rejects ambiguous sibling package roots. The "
        "effective root contains `index.html`, "
        "`BASELINE-MANIFEST.json`, `RETURN-NOTE.md`, and the escrow database.\n\n"
        "Read `ADOPTION-MODES.json` and copy its mode into `RETURN-NOTE.md`; do not "
        "change it. `exact` means your returned JSX, CSS, assets, markup hierarchy, "
        "classes, and visible copy become sealed executable interface source rather "
        "than inspiration for engineering to recreate or translate.\n\n"
        "Fill `RETURN-NOTE.md` and make it name every actual changed path. After the "
        "final local-data change, use **Export design data** and place the downloaded "
        "file at the package root as exactly `travelling-data-export.sqlite3`. If "
        "local data changes again, export again and replace that file before "
        "archiving. The database is mandatory owner-only escrow: engineering "
        "preserves it and reports its identity but must not open, query, compare, "
        "import, merge, overwrite, convert, or derive anything from it unless the "
        "owner later gives a separate prompt-scoped instruction that names the "
        "database and requested operation. Do not add credentials or production "
        "data. Engineering will verify the remaining return facts before adoption.\n"
    )
    _write_new(package / "RETURNING-THIS-DROP.md", returning.encode("utf-8"))
    adoption_modes = {
        "schema": "gpt-design-bridge/adoption-modes/v1",
        "default_mode": "exact",
        "screens": adoption_screens(record),
        "meaning": {
            "exact": (
                "Returned JSX, CSS, assets, markup hierarchy, classes, and visible "
                "copy are executable source and are sealed by default."
            ),
            "characterized": (
                "Behavior and fields are fixed; visual treatment may change only "
                "through the recorded engineering integration gate."
            ),
            "reference": "Informational only; the returned source is not auto-installed.",
        },
    }
    _write_new(package / "ADOPTION-MODES.json", canonical_json(adoption_modes))
    current = build_tree_manifest(package)["files"]
    engineering = sorted(
        {
            *(
                path
                for path in current
                if not is_designer_package_path(surface, path)
                and path not in {"RETURN-NOTE.md", "contract-additions.json"}
            ),
            "BASELINE-MANIFEST.json",
            "OFF-LIMITS.json",
            "OFF-LIMITS.md",
        }
    )
    off_limits = {
        "schema": "gpt-design-bridge/off-limits/v2",
        "mixed_region": {
            surface["embedded"]["package_path"]: "only the marked designer block region is editable"
        },
        "editable_roots": [entry["package_root"] for entry in surface["direct"]],
        "editable_files": ["RETURN-NOTE.md", "contract-additions.json"],
        "required_return_artifacts": [
            {
                "path": "travelling-data-export.sqlite3",
                "handling": "owner-only-escrow-no-automatic-data-operation",
            }
        ],
        "engineering_owned_files": engineering,
    }
    _write_new(package / "OFF-LIMITS.json", canonical_json(off_limits))
    prose = "# Off limits\n\n" + "\n".join(f"- `{path}`" for path in engineering) + "\n"
    _write_new(package / "OFF-LIMITS.md", prose.encode("utf-8"))
def build_drop_candidate(project: Path, stage: Path) -> dict[str, Any]:
    kit = kit_root(project)
    with exclusive_lock(kit / "runtime" / "mutation.lock", "drop-build"):
        _config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] not in {"outbound_open", "awaiting_return", "proving"}:
            raise BridgeError("drop-build requires an outbound_open, awaiting_return, or proving round")
        record = load_round(project, active["id"])
        provenance = (
            validate_post_adoption_provenance(project, record)
            if active["status"] == "proving"
            else validate_round_provenance(project, record)
        )
        surface, source_manifest = verify_prepared_stage(
            project,
            stage,
            record,
            provenance,
        )
        runtime = kit / "runtime"
        staging = _create_staging_directory(runtime, f"drop-{record['id']}.")
        destination: Path | None = None
        destination_created = False
        try:
            package = staging / "package"
            package.mkdir()
            stage_manifest = build_tree_manifest(stage)
            for relative in stage_manifest["files"]:
                _copy_new(stage / relative, package / relative)
            _copy_package_docs(project, kit, package, record, surface, provenance)
            packaged_secrets = scan_secrets(package)
            if packaged_secrets:
                raise BridgeError("generated package contains high-confidence secret material")
            baseline = build_tree_manifest(package, exclude={"BASELINE-MANIFEST.json"})
            atomic_write_json(package / "BASELINE-MANIFEST.json", baseline)
            package_manifest = build_tree_manifest(package)
            build_stamp = hashlib.sha256(
                canonical_json(
                    {
                        "round_id": record["id"],
                        "package_tree": package_manifest["tree_sha256"],
                        "source_tree": source_manifest["tree_sha256"],
                        "provenance_verification": provenance["verification_sha256"],
                        "capability_manifest": provenance["capability_manifest_sha256"],
                    }
                )
            ).hexdigest()[:32]
            package_folder = f"{_config['project']['slug']}-round-{record['id']}"
            archive_name = f"{package_folder}-app.zip"
            first, second = staging / "package-a.zip", staging / "package-b.zip"
            create_deterministic_zip(package, first, prefix=package_folder)
            create_deterministic_zip(package, second, prefix=package_folder)
            if first.read_bytes() != second.read_bytes():
                raise BridgeError("deterministic double-build produced different package bytes")
            destination = kit / "outbound" / record["id"] / build_stamp
            purpose = "post-adoption-reproof" if active["status"] == "proving" else "outbound"
            sequence_key = "reproof_candidates" if purpose == "post-adoption-reproof" else "outbound_candidates"
            candidate_prefix = "RP" if purpose == "post-adoption-reproof" else "OUT"
            candidate = {
                "purpose": purpose,
                "candidate_id": f"{candidate_prefix}-{len(record['artifacts'].get(sequence_key, [])) + 1:03d}",
                "build_stamp": build_stamp,
                "package_folder": package_folder,
                "archive_name": archive_name,
                "archive_sha256": sha256_file(first),
                "archive_bytes": first.stat().st_size,
                "package_file_count": package_manifest["file_count"],
                "package_tree_sha256": package_manifest["tree_sha256"],
                "baseline_manifest_sha256": sha256_file(package / "BASELINE-MANIFEST.json"),
                "source_tree_sha256": source_manifest["tree_sha256"],
                "provenance_id": provenance["id"],
                "provenance_verification_sha256": provenance["verification_sha256"],
                "capability_manifest_sha256": provenance["capability_manifest_sha256"],
                "root": (destination.relative_to(project)).as_posix(),
            }
            if destination.exists():
                existing_archive = destination / archive_name
                if purpose != "post-adoption-reproof" or (
                    not existing_archive.is_file()
                    or sha256_file(existing_archive) != candidate["archive_sha256"]
                ):
                    raise BridgeError(f"outbound candidate already exists: {destination}")
                candidate["reused_identical_artifact"] = True
                shutil.rmtree(staging)
            else:
                candidate["reused_identical_artifact"] = False
                first.rename(staging / archive_name)
                second.unlink()
                atomic_write_json(staging / "outbound.json", candidate)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _promote_directory(staging, destination)
                destination_created = True
            generation = state["generation"] + 1
            if active["status"] == "proving":
                event = "post_adoption_drop_rebuilt"
                record["artifacts"].setdefault(sequence_key, []).append(candidate)
            elif active["status"] == "awaiting_return":
                record["status"] = state["phase"] = "outbound_open"
                state["active_round"]["status"] = "outbound_open"
                event = "outbound_correction_candidate_built"
                record["artifacts"].setdefault(sequence_key, []).append(candidate)
            else:
                event = "outbound_candidate_built"
                record["artifacts"].setdefault(sequence_key, []).append(candidate)
            record["events"].append(
                {"generation": generation, "event": event, "build_stamp": build_stamp}
            )
            state["generation"] = generation
            persist_round_state(project, state, record)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            if destination_created and destination and destination.exists():
                shutil.rmtree(destination)
            raise
    return candidate
def release_drop(
    project: Path,
    proof_path: Path,
    brief_data_path: Path,
    *,
    correction_reason: str | None = None,
) -> dict[str, Any]:
    kit = kit_root(project)
    with exclusive_lock(kit / "runtime" / "mutation.lock", "drop-release"):
        config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] not in {"outbound_open", "awaiting_return"}:
            raise BridgeError("drop-release requires an outbound_open or awaiting_return round")
        correcting = active["status"] == "awaiting_return"
        if correcting:
            reason = non_empty(correction_reason, "--correction-reason")
        elif correction_reason is not None:
            raise BridgeError(
                "--correction-reason is valid only for an immutable awaiting_return "
                "courier correction"
            )
        else:
            reason = None
        record = load_round(project, active["id"])
        validate_round_provenance(project, record)
        candidates = record["artifacts"].get("outbound_candidates", [])
        if not candidates:
            raise BridgeError("drop-release requires a built outbound candidate")
        candidate = candidates[-1]
        releases = record["artifacts"].get("outbound_releases", [])
        prior_release = releases[-1] if releases else None
        if correcting:
            if prior_release is None:
                raise BridgeError("courier correction requires an existing outbound release")
            if prior_release["package_sha256"] != candidate["archive_sha256"]:
                raise BridgeError(
                    "courier correction package differs from the released package; "
                    "rebuild and re-prove instead"
                )
        proof = read_json(proof_path)
        validate_browser_proof(proof, candidate)
        verify_proof_artifacts(project, proof)
        parity = validate_parity_reference(project, record, candidate, proof["parity"])
        candidate_root = project / candidate["root"]
        if sha256_file(candidate_root / candidate["archive_name"]) != candidate["archive_sha256"]:
            raise BridgeError("outbound package bytes changed after candidate build")
        staging = _create_staging_directory(kit / "runtime", f"release-{record['id']}.")
        release_number = len(releases) + 1
        destination = candidate_root / (
            "release" if release_number == 1 else f"release-{release_number:03d}"
        )
        destination_created = False
        try:
            courier = staging / "courier"
            courier.mkdir()
            _copy_new(candidate_root / candidate["archive_name"], courier / candidate["archive_name"])
            summaries = proof["contexts"]
            modes = adoption_screens(record)
            adoption_mode_summary = (
                modes[0]["mode"]
                if len({item["mode"] for item in modes}) == 1
                else "; ".join(
                    f"{item['entrypoint']}={item['mode']}" for item in modes
                )
            )
            generated = {
                "PROJECT_NAME": config["project"]["name"],
                "ROUND_ID": record["id"],
                "PROTOCOL_VERSION": "gpt-design-bridge/v1",
                "BASELINE_TREE": f"SOURCE-TREE-SHA256:{candidate['source_tree_sha256']}",
                "ARCHIVE_NAME": candidate["archive_name"],
                "ARCHIVE_SHA256": candidate["archive_sha256"],
                "ARCHIVE_BYTES": str(candidate["archive_bytes"]),
                "PACKAGE_FILE_COUNT": str(candidate["package_file_count"]),
                "MANIFEST_SHA256": candidate["baseline_manifest_sha256"],
                "BUILD_STAMP": candidate["build_stamp"],
                "ADOPTION_MODE": adoption_mode_summary,
                "PACKAGE_FOLDER": candidate["package_folder"],
                "ARCHIVE_HASH_A": candidate["archive_sha256"],
                "ARCHIVE_HASH_B": candidate["archive_sha256"],
                "FILE_EVIDENCE": summaries["file"]["summary"],
                "SUBPATH_EVIDENCE": summaries["deep_http"]["summary"],
                "BROKEN_MOUNT_EVIDENCE": summaries["broken_mount"]["summary"],
                "HOME_FAILURE_EVIDENCE": summaries["home_without_backend"]["summary"],
                "INTERACTION_EVIDENCE": "; ".join(item["summary"] for item in proof["interactions"]),
                "PROVENANCE_EVIDENCE": (
                    f"{record['provenance']['id']} verification "
                    f"{record['provenance']['verification_sha256']} on Git tree "
                    f"{record['provenance']['guarded_tree']}"
                ),
                "CAPABILITY_PARITY_EVIDENCE": (
                    f"Capability parity {parity['report_sha256']}; production "
                    f"{parity['production_manifest_sha256']}; travelling "
                    f"{parity['travelling_manifest']['sha256']}"
                ),
                "APPROVED_DIFFERENCES": (
                    "; ".join(
                        f"{item['capability_id']}: {item['change']}"
                        for item in parity["approved_differences"]
                    )
                    or "None."
                ),
                "PRESERVATION_DEBT": (
                    "; ".join(
                        item["id"] for item in state["deferred_obligations"]
                        if item["status"] == "open"
                    )
                    or "None."
                ),
            }
            brief = _render_brief(kit, brief_data_path, generated)
            _write_new(courier / "DROP-BRIEF.md", brief.encode("utf-8"))
            _copy_new(kit / "contract" / "CONSTITUTION.md", courier / "CONSTITUTION.md")
            _copy_new(
                kit / "contract" / "OWNER-RULES-DESIGN.md",
                courier / "OWNER-RULES-DESIGN.md",
            )
            _write_new(courier / "BROWSER-PROOF.json", canonical_json(proof))
            _copy_new(project / proof["parity"]["path"], courier / "CAPABILITY-PARITY.json")
            for reference in browser_artifact_references(proof):
                _copy_new(project / reference, courier / "browser-evidence" / reference)
            _write_new(
                courier / "PACKAGE-SHA256.txt",
                f"{candidate['archive_sha256']}  {candidate['archive_name']}\n".encode(),
            )
            start = (
                f"# Start here\n\nRead `DROP-BRIEF.md`, `OWNER-RULES-DESIGN.md`, and "
                f"`CONSTITUTION.md`, then extract `{candidate['archive_name']}`. Open the "
                "extracted `index.html`; no build or production access is required. After "
                "the final local-data change, export **Export design data** to "
                "`travelling-data-export.sqlite3` at the extracted package root. Return "
                "the complete extracted folder, including that mandatory owner-only "
                "database escrow.\n"
            )
            _write_new(courier / "START-HERE.md", start.encode("utf-8"))
            courier_name = f"{candidate['package_folder']}-courier.zip"
            first, second = staging / "courier-a.zip", staging / "courier-b.zip"
            create_deterministic_zip(courier, first, prefix=f"{candidate['package_folder']}-courier")
            create_deterministic_zip(courier, second, prefix=f"{candidate['package_folder']}-courier")
            if first.read_bytes() != second.read_bytes():
                raise BridgeError("deterministic double-build produced different courier bytes")
            first.rename(staging / courier_name)
            second.unlink()
            release = {
                "release_id": f"REL-{release_number:03d}",
                "correction": correcting,
                "build_stamp": candidate["build_stamp"],
                "package_sha256": candidate["archive_sha256"],
                "proof_sha256": hashlib.sha256(canonical_json(proof)).hexdigest(),
                "parity_sha256": proof["parity"]["sha256"],
                "provenance_verification_sha256": record["provenance"]["verification_sha256"],
                "evidence_tree_sha256": build_tree_manifest(courier / "browser-evidence")["tree_sha256"],
                "courier_name": courier_name,
                "courier_sha256": sha256_file(staging / courier_name),
                "courier_bytes": (staging / courier_name).stat().st_size,
                "root": (destination.relative_to(project)).as_posix(),
            }
            if prior_release is not None:
                release["supersedes_courier_sha256"] = prior_release["courier_sha256"]
            if correcting and release["courier_sha256"] == prior_release["courier_sha256"]:
                raise BridgeError("courier correction produced bytes identical to the prior release")
            atomic_write_json(staging / "release.json", release)
            if destination.exists():
                raise BridgeError(f"outbound release already exists: {destination}")
            _promote_directory(staging, destination)
            destination_created = True
            record["artifacts"].setdefault("outbound_releases", []).append(release)
            if correcting:
                generation = state["generation"] + 1
                record["events"].append(
                    {
                        "generation": generation,
                        "event": "outbound_release_corrected",
                        "details": {
                            "build_stamp": candidate["build_stamp"],
                            "courier_sha256": release["courier_sha256"],
                            "supersedes_courier_sha256": prior_release["courier_sha256"],
                            "reason": reason,
                        },
                    }
                )
                state["generation"] = generation
            else:
                apply_transition(
                    state,
                    record,
                    "awaiting_return",
                    event="outbound_released",
                    details={
                        "build_stamp": candidate["build_stamp"],
                        "courier_sha256": release["courier_sha256"],
                    },
                )
            persist_round_state(project, state, record)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            if destination_created and destination.exists():
                shutil.rmtree(destination)
            raise
    return release
