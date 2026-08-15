#!/usr/bin/env python3
"""Project-local command for GPT Design Bridge."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from bridge_core import (
    BridgeError,
    KIT_DIRNAME,
    KIT_VERSION,
    PROJECT_SCHEMA,
    STATE_SCHEMA,
    canonical_json,
    check_project,
    copy_assets,
    copy_tools,
    exclusive_lock,
    kit_root,
    load_project,
    promote_directory,
    resolve_git_root,
    slugify,
    validate_project,
    validate_slug,
)
from bridge_rounds import (
    abandon_round,
    add_capability_ruling,
    add_deferral,
    add_owner_ruling,
    correct_deferral,
    discharge_deferral,
    open_round,
)
from bridge_drop import build_drop_candidate, release_drop
from bridge_return import inspect_return
from bridge_artifacts import ArchiveLimits
from bridge_adopt import adopt_return
from bridge_seal import complete_integration, record_reproof, seal_round
from bridge_scaffold import certify_bootstrap, scaffold_application, verify_bootstrap
from bridge_stage import prepare_stage
from bridge_provenance import (
    capture_provenance,
    declaration_template,
    parity_bindings_template,
    verify_provenance,
    write_parity_report,
)


DIRECTORIES = (
    "contract",
    "templates",
    "records/rounds",
    "records/provenance",
    "baselines",
    "outbound",
    "returns/inbox",
    "returns/quarantine",
    "evidence",
    "runtime",
)


def positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def initial_project(
    args: argparse.Namespace,
    tool_manifest: dict[str, Any],
    foundation_manifest: dict[str, Any],
) -> dict[str, Any]:
    slug = validate_slug(args.slug) if args.slug else slugify(args.name)
    database: dict[str, Any] = {"engine": args.database}
    if args.database == "sqlite":
        database.update(
            {
                "journal_mode": "DELETE",
                "network_filesystem": False,
                "wal_policy": "disabled-until-runtime-version-and-concurrency-proof",
                "postgresql_promotion_gate": [
                    "database accessed from multiple hosts",
                    "simultaneous writers cannot queue",
                    "multiple application replicas planned",
                ],
            }
        )
    origin = "brownfield" if (resolve_git_root(Path(args.target)) / ".harness-separation" / "contract.json").is_file() else "fresh"
    return {
        "schema": PROJECT_SCHEMA,
        "kit_version": KIT_VERSION,
        "project": {"name": args.name, "slug": slug, "origin": origin},
        "designer": {
            "authority": "external",
            "mode": "travelling-drop",
            "designer_can_run_build": False,
        },
        "database": database,
        "deployment": {
            "runtime": "systemd",
            "reverse_proxy": "host-selected-caddy-or-nginx",
            "containerization": "prohibited",
            "hosting": "hetzner-vps",
            "dns": "cloudflare",
            "system_mail": "postmark",
            "hostname": args.hostname,
            "application_port": args.port,
        },
        "browser_proof": {
            "surface": "visible-independent-chrome-or-chromium",
            "in_app_browser_allowed": False,
        },
        "tool_manifest": tool_manifest,
        "foundation_manifest": foundation_manifest,
    }


def initial_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "generation": 1,
        "phase": "initialized",
        "active_round": None,
        "sealed_rounds": [],
        "deferred_obligations": [],
    }


def command_init(args: argparse.Namespace) -> int:
    project_root = resolve_git_root(Path(args.target))
    final_root = kit_root(project_root)
    if final_root.exists():
        raise BridgeError(f"refusing to overwrite existing project kit: {final_root}")
    init_lock = project_root / f"{KIT_DIRNAME}.init.lock"
    with exclusive_lock(init_lock, "init"):
        staging = project_root / f"{KIT_DIRNAME}.init-{os.getpid()}"
        if staging.exists():
            raise BridgeError(f"staging path already exists: {staging}")
        try:
            staging.mkdir()
            for relative in DIRECTORIES:
                directory = staging / relative
                directory.mkdir(parents=True)
                if relative != "runtime":
                    write_exclusive(directory / ".gitkeep", b"")
            write_exclusive(
                staging / ".gitignore",
                b"runtime/*\n!runtime/.gitkeep\noutbound/*\n!outbound/.gitkeep\n"
                b"returns/inbox/*\n!returns/inbox/.gitkeep\n"
                b"returns/quarantine/*\n!returns/quarantine/.gitkeep\n"
                b"baselines/*/adoption-*/\n"
                b"records/provenance/*\n!records/provenance/.gitkeep\n"
                b"evidence/*\n!evidence/.gitkeep\n",
            )
            write_exclusive(staging / "runtime" / ".gitkeep", b"")
            tool_manifest = copy_tools(Path(__file__).resolve().parent, staging)
            asset_root = Path(__file__).resolve().parent.parent / "assets" / "project-kit"
            foundation_manifest = copy_assets(asset_root, staging)
            project = initial_project(args, tool_manifest, foundation_manifest)
            validate_project(project, staging / "project.json")
            write_exclusive(staging / "project.json", canonical_json(project))
            write_exclusive(staging / "state.json", canonical_json(initial_state()))
            promote_directory(staging, final_root)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
    print(f"GPT Design Bridge initialized: {project_root}")
    print(f"  project: {project['project']['name']} ({project['project']['slug']})")
    print(f"  database: {project['database']['engine']}")
    print("  deployment: native systemd + host-selected Caddy/Nginx; Docker prohibited")
    print(f"  next: python {KIT_DIRNAME}/tools/gpt_design_bridge.py status")
    return 0


def resolve_project_argument(value: str) -> Path:
    return resolve_git_root(Path(value))


def require_blackbox_release_gate(root: Path, boundary: str) -> None:
    harness = kit_root(root) / "tools" / "gpt_blackbox.py"
    if not harness.is_file():
        raise BridgeError(
            f"{boundary}: project-local BlackBox release gate is missing: {harness}"
        )
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(harness),
            "--repo",
            str(root),
            "release-gate",
        ],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        raise BridgeError(
            f"{boundary}: BlackBox release gate blocked this lifecycle mutation "
            f"(exit {completed.returncode}); settle or seal every reported task first"
        )


def status_payload(root: Path) -> dict[str, Any]:
    config, state = load_project(root)
    provenance_root = kit_root(root) / "records" / "provenance"
    provenance = sorted(
        item.name for item in provenance_root.iterdir()
        if item.is_dir() and item.name.startswith("P-")
    ) if provenance_root.is_dir() else []
    return {
        "project": config["project"],
        "database": config["database"]["engine"],
        "deployment": config["deployment"],
        "phase": state["phase"],
        "generation": state["generation"],
        "active_round": state["active_round"],
        "sealed_round_count": len(state["sealed_rounds"]),
        "deferred_obligation_count": len(state["deferred_obligations"]),
        "provenance_count": len(provenance),
        "latest_provenance": provenance[-1] if provenance else None,
    }


def command_status(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    payload = status_payload(root)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(f"project: {payload['project']['name']} ({payload['project']['slug']})")
        print(f"phase: {payload['phase']} · generation {payload['generation']}")
        print(f"database: {payload['database']}")
        print(
            f"rounds: active={payload['active_round']!r}, "
            f"sealed={payload['sealed_round_count']}"
        )
        print(f"deferred obligations: {payload['deferred_obligation_count']}")
        print(
            f"production provenance: count={payload['provenance_count']}, "
            f"latest={payload['latest_provenance']!r}"
        )
    return 0


def command_check(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    errors = check_project(root)
    if errors:
        print(f"GPT Design Bridge CHECK FAILED: {len(errors)} finding(s)", file=sys.stderr)
        for finding in errors:
            print(f"  - {finding}", file=sys.stderr)
        return 2
    config, state = load_project(root)
    print("GPT Design Bridge CHECK PASS")
    print(f"  project: {config['project']['slug']}")
    print(f"  phase: {state['phase']}")
    print(f"  tools: {len(config['tool_manifest'])} hash-verified")
    print(f"  foundation assets: {len(config['foundation_manifest'])} hash-verified")
    return 0


def command_app_scaffold(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    if (root / ".harness-separation" / "contract.json").is_file():
        raise BridgeError(
            "app-scaffold is for fresh projects only; brownfield projects preserve their existing stack"
        )
    record = scaffold_application(root)
    print(f"application foundation created: {root}")
    print(f"  database: {record['database']}")
    print(f"  files: {record['file_count']}")
    print("  lifecycle: building")
    return 0


def command_bootstrap_certify(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    certificate = certify_bootstrap(root)
    print(f"bootstrap seed certified: {root}")
    print(f"  files: {certificate['git_inventory']['file_count']}")
    print(f"  bytes: {certificate['git_inventory']['total_bytes']}")
    print(f"  SHA-256: {certificate['git_inventory']['manifest_sha256']}")
    print("  next: run bootstrap-check immediately before the first BlackBox task")
    return 0


def command_bootstrap_check(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    certificate = verify_bootstrap(root)
    print("GPT Design Bridge BOOTSTRAP CHECK PASS")
    print(f"  project: {certificate['project_slug']}")
    print(f"  files: {certificate['git_inventory']['file_count']}")
    print(f"  SHA-256: {certificate['git_inventory']['manifest_sha256']}")
    print("  normal BlackBox task limits begin with the next code change")
    return 0


def command_round_open(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    require_blackbox_release_gate(root, "round-open")
    record = open_round(
        root,
        goal=args.goal,
        owner_request=args.owner_request,
        designer_surface=args.surface,
        entrypoints=args.entrypoint,
        route_prefixes=args.route_prefix or ["/api"],
        provenance_id=args.provenance,
        adoption_mode=args.adoption_mode,
    )
    print(f"designer round opened: {record['id']}")
    print(f"  status: {record['status']}")
    print(f"  adoption mode: {record['scope']['screens'][0]['mode']}")
    print(f"  record: {kit_root(root) / 'records' / 'rounds' / record['id']}")
    print("  next: build and prove the outbound drop before marking it shipped")
    return 0


def parse_proof_paths(values: list[str] | None) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values or []:
        if "=" not in value:
            raise BridgeError("proof must be supplied as STATE=path/to/proof.json")
        state, raw_path = value.split("=", 1)
        state = state.strip().lower()
        if not state or not raw_path.strip() or state in result:
            raise BridgeError(f"invalid or duplicate proof mapping: {value!r}")
        result[state] = Path(raw_path)
    return result


def command_provenance_template(args: argparse.Namespace) -> int:
    print(json.dumps(declaration_template(args.task), indent=2, ensure_ascii=False))
    return 0


def command_provenance_capture(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    result = capture_provenance(root, Path(args.declaration), parse_proof_paths(args.proof))
    print(f"production provenance captured: {result['id']}")
    print(f"  baseline tree: {result['baseline_tree']}")
    print(f"  build graph: {result['build_graph_sha256']}")
    print(f"  capability manifest: {result['capability_manifest_sha256']}")
    print("  next: implement within the same BlackBox task, pass its final gate, then provenance-verify")
    return 0


def command_provenance_verify(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    result = verify_provenance(
        root,
        args.id,
        parse_proof_paths(args.proof),
        decisions_path=Path(args.decisions) if args.decisions else None,
        user_approved_capability_changes=args.user_approved_capability_changes,
        user_approved_compatibility_change=args.user_approved_compatibility_change,
    )
    print(f"production provenance verified: {result['id']}")
    print(f"  guarded tree: {result['guarded_tree']}")
    print(f"  capability manifest: {result['capability_manifest_sha256']}")
    print("  next: round-open --provenance " + result["id"])
    return 0


def command_capability_ruling(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    ruling = add_capability_ruling(
        root,
        capability_id=args.capability,
        change=args.change,
        replacement_id=args.replacement,
        question=args.question,
        decision=args.decision,
        ruling_date=args.date,
        user_approved=args.user_approved,
    )
    print(f"owner capability ruling recorded: {ruling['id']}")
    print(f"  capability: {ruling['capability_change']['capability_id']}")
    return 0


def command_parity_check(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    _config, state = load_project(root)
    active = state.get("active_round")
    if not active:
        raise BridgeError("parity-check requires an active designer round")
    from bridge_core import load_round

    record = load_round(root, active["id"])
    candidates = record["artifacts"].get("outbound_candidates", [])
    if not candidates:
        raise BridgeError("parity-check requires a built outbound candidate")
    result = write_parity_report(
        root,
        Path(args.package).resolve(),
        parse_proof_paths(args.proof),
        Path(args.bindings),
        candidates[-1],
        Path(args.output),
    )
    print("production-to-travelling capability parity PASS")
    print(f"  report: {result['path']}")
    print(f"  SHA-256: {result['sha256']}")
    return 0


def command_parity_template(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    _config, state = load_project(root)
    active = state.get("active_round")
    if not active:
        raise BridgeError("parity-template requires an active designer round")
    from bridge_core import load_round

    record = load_round(root, active["id"])
    print(json.dumps(parity_bindings_template(root, record), indent=2, ensure_ascii=False))
    return 0


def command_round_abandon(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    record = abandon_round(root, reason=args.reason)
    print(f"designer round abandoned: {record['id']}")
    print(f"  restored lifecycle phase: {record['opened_from_phase']}")
    return 0


def command_owner_ruling(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    ruling = add_owner_ruling(
        root,
        question=args.question,
        decision=args.decision,
        ruling_date=args.date,
    )
    print(f"owner ruling recorded: {ruling['id']}")
    return 0


def command_defer(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    item = add_deferral(
        root,
        obligation=args.obligation,
        reason=args.reason,
        discharge_gate=args.gate,
    )
    print(f"deferred obligation recorded: {item['id']}")
    print(f"  discharge gate: {item['discharge_gate']}")
    return 0


def command_deferral_correct(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    result = correct_deferral(
        root,
        deferral_id=args.id,
        obligation=args.obligation,
        reason=args.reason,
        discharge_gate=args.gate,
        correction_reason=args.correction_reason,
        audit_path=Path(args.audit),
    )
    print(f"deferred obligation corrected: {result['deferral']['id']}")
    print(f"  audit: {result['audit']}")
    print(f"  audit SHA-256: {result['audit_sha256']}")
    return 0


def command_stage_prepare(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    require_blackbox_release_gate(root, "stage-prepare")
    stage = prepare_stage(
        root,
        output=args.output,
        replace=args.replace,
        node_override=args.node,
        process_timeout=args.process_timeout,
        process_stall_timeout=args.process_stall_timeout,
        process_heartbeat=args.process_heartbeat,
    )
    print(f"travelling designer stage prepared: {stage['stage_stamp']}")
    print(f"  stage: {root / stage['root']}")
    print(f"  files: {stage['file_count']}")
    print(f"  tree SHA-256: {stage['tree_sha256']}")
    print("  next: prove this exact stage, then build the outbound drop")
    return 0


def command_discharge(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    item = discharge_deferral(root, deferral_id=args.id, evidence=args.evidence)
    print(f"deferred obligation discharged: {item['id']}")
    return 0


def command_drop_build(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    candidate = build_drop_candidate(root, Path(args.stage))
    print(f"{candidate['purpose']} candidate built: {candidate['build_stamp']}")
    print(f"  package: {root / candidate['root'] / candidate['archive_name']}")
    print(f"  SHA-256: {candidate['archive_sha256']}")
    if candidate["purpose"] == "outbound":
        print("  state remains outbound_open until exact-package browser proof passes")
    else:
        print("  state remains proving until reproof evidence and the owner seal pass")
    return 0


def command_drop_release(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    require_blackbox_release_gate(root, "drop-release")
    release = release_drop(
        root,
        Path(args.proof),
        Path(args.brief_data),
        correction_reason=args.correction_reason,
    )
    label = "outbound courier correction sealed" if release["correction"] else "outbound release sealed"
    print(f"{label}: {release['build_stamp']}")
    print(f"  courier: {root / release['root'] / release['courier_name']}")
    print(f"  SHA-256: {release['courier_sha256']}")
    print("  lifecycle: awaiting_return")
    return 0


def command_return_inspect(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    limits = ArchiveLimits(
        max_members=args.archive_max_members,
        max_file_bytes=args.archive_max_file_bytes,
        max_total_bytes=args.archive_max_total_bytes,
        max_compression_ratio=args.archive_max_compression_ratio,
    )
    report = inspect_return(root, Path(args.archive), limits=limits)
    print(f"designer return inspected: {report['archive_sha256']}")
    print(f"  verdict: {report['verdict']}")
    print(f"  lifecycle advanced: {str(report['advanced']).lower()}")
    print(
        "  archive limits: "
        + json.dumps(report["archive_limits"], sort_keys=True, separators=(",", ":"))
    )
    print(
        f"  changes: added={len(report['changes']['added'])}, "
        f"modified={len(report['changes']['modified'])}, "
        f"removed={len(report['changes']['removed'])}"
    )
    return 0


def command_adopt_apply(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    require_blackbox_release_gate(root, "adopt-apply")
    adoption = adopt_return(root, Path(args.decisions))
    print(f"designer surface adopted: {adoption['adoption_stamp']}")
    print(f"  backup: {root / adoption['backup_root']}")
    print(f"  integration tasks: {len(adoption['integration_tasks'])}")
    print("  lifecycle: adopting")
    print(
        "  next: start a fresh BlackBox task from this adopted tree before any "
        "engineering integration or correction"
    )
    return 0


def command_adopt_integrate(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    require_blackbox_release_gate(root, "adopt-integrate")
    integration = complete_integration(root, Path(args.evidence), args.provenance)
    print(f"integration evidence accepted: {integration['evidence_sha256']}")
    print(f"  post-adoption provenance: {integration['provenance']['id']}")
    print("  lifecycle: proving")
    return 0


def command_reproof_record(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    reproof = record_reproof(root, Path(args.proof))
    print(f"post-adoption drop re-proved: {reproof['build_stamp']}")
    print(f"  proof SHA-256: {reproof['proof_sha256']}")
    return 0


def command_round_seal(args: argparse.Namespace) -> int:
    root = resolve_project_argument(args.project)
    require_blackbox_release_gate(root, "round-seal")
    sealed = seal_round(root, Path(args.evidence))
    print(f"designer round sealed: {sealed['evidence_sha256']}")
    print(f"  source tree: {sealed['source_tree_sha256']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize and inspect a self-contained GPT Design Bridge project"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="install an additive project-local kit")
    initialize.add_argument("--target", default=".")
    initialize.add_argument("--name", required=True)
    initialize.add_argument("--slug")
    initialize.add_argument("--database", choices=("sqlite", "postgresql"), default="sqlite")
    initialize.add_argument("--hostname")
    initialize.add_argument("--port", type=int, default=3000)
    initialize.set_defaults(handler=command_init)

    status = subparsers.add_parser("status", help="print lifecycle status")
    status.add_argument("--project", default=".")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    check = subparsers.add_parser("check", help="validate the project-local kit")
    check.add_argument("--project", default=".")
    check.set_defaults(handler=command_check)

    app_scaffold = subparsers.add_parser(
        "app-scaffold", help="create the selected application foundation without overwriting"
    )
    app_scaffold.add_argument("--project", default=".")
    app_scaffold.set_defaults(handler=command_app_scaffold)

    bootstrap_certify = subparsers.add_parser(
        "bootstrap-certify",
        help="certify the exact untouched large seed before the first BlackBox task",
    )
    bootstrap_certify.add_argument("--project", default=".")
    bootstrap_certify.set_defaults(handler=command_bootstrap_certify)

    bootstrap_check = subparsers.add_parser(
        "bootstrap-check",
        help="verify the certified seed immediately before the first BlackBox task",
    )
    bootstrap_check.add_argument("--project", default=".")
    bootstrap_check.set_defaults(handler=command_bootstrap_check)

    provenance_template = subparsers.add_parser(
        "provenance-template",
        help="print the strict production provenance declaration template",
    )
    provenance_template.add_argument("--task", required=True)
    provenance_template.set_defaults(handler=command_provenance_template)

    provenance_capture = subparsers.add_parser(
        "provenance-capture",
        help="capture production graph and capabilities before designer-facing UI edits",
    )
    provenance_capture.add_argument("--project", default=".")
    provenance_capture.add_argument("--declaration", required=True)
    provenance_capture.add_argument(
        "--proof",
        action="append",
        required=True,
        metavar="STATE=PROOF.JSON",
        help="controlled Chrome proof for one declared state; repeat for each proof state",
    )
    provenance_capture.set_defaults(handler=command_provenance_capture)

    provenance_verify = subparsers.add_parser(
        "provenance-verify",
        help="verify preservation and same-source topology after the BlackBox final gate",
    )
    provenance_verify.add_argument("--project", default=".")
    provenance_verify.add_argument("--id", required=True)
    provenance_verify.add_argument("--proof", action="append", required=True, metavar="STATE=PROOF.JSON")
    provenance_verify.add_argument("--decisions")
    provenance_verify.add_argument("--user-approved-capability-changes", action="store_true")
    provenance_verify.add_argument("--user-approved-compatibility-change", action="store_true")
    provenance_verify.set_defaults(handler=command_provenance_verify)

    round_open = subparsers.add_parser(
        "round-open", help="open one governed outbound designer round"
    )
    round_open.add_argument("--project", default=".")
    round_open.add_argument("--goal", required=True)
    round_open.add_argument("--owner-request", required=True)
    round_open.add_argument("--surface", action="append", required=True)
    round_open.add_argument("--entrypoint", action="append", required=True)
    round_open.add_argument(
        "--provenance",
        required=True,
        help="verified P-NNN production provenance record for this exact Git tree",
    )
    round_open.add_argument("--route-prefix", action="append")
    round_open.add_argument(
        "--adoption-mode",
        choices=("exact", "characterized", "reference"),
        default="exact",
        help=(
            "mode for every declared screen; exact is the default executable-source "
            "contract, characterized fixes behavior/fields, reference is informational"
        ),
    )
    round_open.set_defaults(handler=command_round_open)

    round_abandon = subparsers.add_parser(
        "round-abandon", help="abandon a pre-adoption round with a recorded reason"
    )
    round_abandon.add_argument("--project", default=".")
    round_abandon.add_argument("--reason", required=True)
    round_abandon.set_defaults(handler=command_round_abandon)

    ruling = subparsers.add_parser(
        "owner-ruling", help="record the owner's decision for an active-round ambiguity"
    )
    ruling.add_argument("--project", default=".")
    ruling.add_argument("--question", required=True)
    ruling.add_argument("--decision", required=True)
    ruling.add_argument("--date", required=True)
    ruling.set_defaults(handler=command_owner_ruling)

    capability_ruling = subparsers.add_parser(
        "capability-ruling",
        help="record one exact prompt-scoped owner approval for a parity difference",
    )
    capability_ruling.add_argument("--project", default=".")
    capability_ruling.add_argument("--capability", required=True)
    capability_ruling.add_argument("--change", required=True, choices=("remove", "replace", "change"))
    capability_ruling.add_argument("--replacement")
    capability_ruling.add_argument("--question", required=True)
    capability_ruling.add_argument("--decision", required=True)
    capability_ruling.add_argument("--date", required=True)
    capability_ruling.add_argument(
        "--user-approved",
        action="store_true",
        help="factual attestation that the owner approved this exact capability change",
    )
    capability_ruling.set_defaults(handler=command_capability_ruling)

    defer = subparsers.add_parser(
        "defer", help="record an obligation that remains debt until an explicit gate"
    )
    defer.add_argument("--project", default=".")
    defer.add_argument("--obligation", required=True)
    defer.add_argument("--reason", required=True)
    defer.add_argument("--gate", required=True)
    defer.set_defaults(handler=command_defer)

    deferral_correct = subparsers.add_parser(
        "deferral-correct",
        help="correct one open obligation through an immutable old/new audit record",
    )
    deferral_correct.add_argument("--project", default=".")
    deferral_correct.add_argument("--id", required=True)
    deferral_correct.add_argument("--obligation", required=True)
    deferral_correct.add_argument("--reason", required=True)
    deferral_correct.add_argument("--gate", required=True)
    deferral_correct.add_argument("--correction-reason", required=True)
    deferral_correct.add_argument("--audit", required=True)
    deferral_correct.set_defaults(handler=command_deferral_correct)

    discharge = subparsers.add_parser(
        "discharge", help="discharge one deferred obligation with concrete evidence"
    )
    discharge.add_argument("--project", default=".")
    discharge.add_argument("--id", required=True)
    discharge.add_argument("--evidence", required=True)
    discharge.set_defaults(handler=command_discharge)

    stage_prepare = subparsers.add_parser(
        "stage-prepare", help="assemble the active round's self-contained no-build stage"
    )
    stage_prepare.add_argument("--project", default=".")
    stage_prepare.add_argument("--output")
    stage_prepare.add_argument("--replace", action="store_true")
    stage_prepare.add_argument("--node")
    stage_prepare.add_argument(
        "--process-timeout",
        type=positive_seconds,
        help="optional explicit overall deadline per build command; omitted means no deadline",
    )
    stage_prepare.add_argument(
        "--process-stall-timeout",
        type=positive_seconds,
        help="optional explicit no-output policy; omitted means no stall termination",
    )
    stage_prepare.add_argument(
        "--process-heartbeat",
        type=positive_seconds,
        default=5.0,
        help="progress heartbeat interval in seconds (default: 5; never a deadline)",
    )
    stage_prepare.set_defaults(handler=command_stage_prepare)

    drop_build = subparsers.add_parser(
        "drop-build", help="build a deterministic outbound candidate from a prepared stage"
    )
    drop_build.add_argument("--project", default=".")
    drop_build.add_argument("--stage", required=True)
    drop_build.set_defaults(handler=command_drop_build)

    parity_template = subparsers.add_parser(
        "parity-template",
        help="print exact workflow bindings required for candidate parity proof",
    )
    parity_template.add_argument("--project", default=".")
    parity_template.set_defaults(handler=command_parity_template)

    parity_check = subparsers.add_parser(
        "parity-check",
        help="compare production capabilities with the exact extracted outbound candidate",
    )
    parity_check.add_argument("--project", default=".")
    parity_check.add_argument("--package", required=True)
    parity_check.add_argument("--bindings", required=True)
    parity_check.add_argument("--proof", action="append", required=True, metavar="STATE=PROOF.JSON")
    parity_check.add_argument("--output", required=True)
    parity_check.set_defaults(handler=command_parity_check)

    drop_release = subparsers.add_parser(
        "drop-release", help="validate exact-package browser proof and release the courier"
    )
    drop_release.add_argument("--project", default=".")
    drop_release.add_argument("--proof", required=True)
    drop_release.add_argument("--brief-data", required=True)
    drop_release.add_argument(
        "--correction-reason",
        help=(
            "owner-scoped reason for an immutable courier-only correction while "
            "awaiting_return"
        ),
    )
    drop_release.set_defaults(handler=command_drop_release)

    return_inspect = subparsers.add_parser(
        "return-inspect", help="quarantine and inspect one complete designer return"
    )
    return_inspect.add_argument("--project", default=".")
    return_inspect.add_argument("--archive", required=True)
    defaults = ArchiveLimits()
    return_inspect.add_argument(
        "--archive-max-members", type=int, default=defaults.max_members,
        help="explicit ZIP member safety limit; raising it is recorded against this round/archive",
    )
    return_inspect.add_argument(
        "--archive-max-file-bytes", type=int, default=defaults.max_file_bytes,
        help="explicit per-member uncompressed byte safety limit",
    )
    return_inspect.add_argument(
        "--archive-max-total-bytes", type=int, default=defaults.max_total_bytes,
        help="explicit total uncompressed byte safety limit",
    )
    return_inspect.add_argument(
        "--archive-max-compression-ratio", type=float,
        default=defaults.max_compression_ratio,
        help="explicit maximum uncompressed/compressed ratio",
    )
    return_inspect.set_defaults(handler=command_return_inspect)

    adopt_apply = subparsers.add_parser(
        "adopt-apply", help="replace the designer surface wholesale from the inspected return"
    )
    adopt_apply.add_argument("--project", default=".")
    adopt_apply.add_argument("--decisions", required=True)
    adopt_apply.set_defaults(handler=command_adopt_apply)

    adopt_integrate = subparsers.add_parser(
        "adopt-integrate", help="bind engineering integration checks and enter proving"
    )
    adopt_integrate.add_argument("--project", default=".")
    adopt_integrate.add_argument("--evidence", required=True)
    adopt_integrate.add_argument(
        "--provenance",
        required=True,
        help="verified post-adoption production provenance for the current integrated tree",
    )
    adopt_integrate.set_defaults(handler=command_adopt_integrate)

    reproof = subparsers.add_parser(
        "reproof-record", help="record visible Chrome proof for the latest rebuilt drop"
    )
    reproof.add_argument("--project", default=".")
    reproof.add_argument("--proof", required=True)
    reproof.set_defaults(handler=command_reproof_record)

    seal = subparsers.add_parser(
        "round-seal", help="seal the exact integrated and re-proved round"
    )
    seal.add_argument("--project", default=".")
    seal.add_argument("--evidence", required=True)
    seal.set_defaults(handler=command_round_seal)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BridgeError as exc:
        print(f"GPT Design Bridge ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("GPT Design Bridge interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
