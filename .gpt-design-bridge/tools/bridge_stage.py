"""Transactional assembly of a self-contained travelling designer stage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from bridge_artifacts import build_tree_manifest
from bridge_process import ProcessResult, run_process
from bridge_core import (
    BridgeError,
    atomic_write_json,
    canonical_json,
    exclusive_lock,
    kit_root,
    load_project,
    load_round,
    promote_directory,
    sha256_file,
)
from bridge_drop import verify_prepared_stage
from bridge_rounds import build_source_manifest, scope_covers
from bridge_provenance import validate_round_provenance
from bridge_stage_page import render_stage_page
from bridge_surface import designer_global_contract, render_index
from bridge_travelling_assets import build_travelling_asset_pack


STAGE_SCHEMA = "gpt-design-bridge/travelling-stage/v1"
SURFACE = {
    "schema": "gpt-design-bridge/design-surface/v1",
    "embedded": {
        "package_path": "index.html",
        "source_root": "app/src/ui",
        "extensions": [".js", ".jsx"],
    },
    "direct": [
        {
            "package_root": "styles",
            "source_root": "app/src/styles",
            "extensions": [".css"],
        }
    ],
}
VENDORS = {
    "react.production.min.js": "node_modules/react/umd/react.production.min.js",
    "react-dom.production.min.js": "node_modules/react-dom/umd/react-dom.production.min.js",
    "babel.min.js": "node_modules/@babel/standalone/babel.min.js",
}
WASM_SOURCE = "node_modules/@sqlite.org/sqlite-wasm/dist/sqlite3.wasm"
EXPECTED_NODE = {"node": "v24.18.0", "sqlite": "3.53.1"}
VENDOR_PROBE = r"""
const fs=require("fs"),vm=require("vm"),path=require("path");
const root=process.argv[1],lucide=process.argv[2],icons=JSON.parse(process.argv[3]);
const c={console,setTimeout,clearTimeout,setInterval,clearInterval,queueMicrotask,
 navigator:{userAgent:"gdb-stage-probe"},location:{protocol:"file:"}};
c.self=c;c.window=c;c.global=c;c.globalThis=c;vm.createContext(c);
for(const file of ["react/umd/react.production.min.js","react-dom/umd/react-dom.production.min.js"]){
 vm.runInContext(fs.readFileSync(path.join(root,"node_modules",file),"utf8"),c,{filename:file});
}
c.react=c.React;
vm.runInContext(fs.readFileSync(lucide,"utf8"),c,{filename:"lucide-react.min.js"});
vm.runInContext(fs.readFileSync(path.join(root,"node_modules/@babel/standalone/babel.min.js"),"utf8"),c);
const missing=icons.filter((name)=>!c.LucideReact||!c.LucideReact[name]);
if(missing.length)throw new Error(`missing Lucide globals: ${missing.join(", ")}`);
if(typeof c.ReactDOM?.createRoot!=="function"||typeof c.Babel?.transform!=="function"){
 throw new Error("ReactDOM.createRoot or Babel.transform is missing");
}
process.stdout.write(JSON.stringify({react:c.React?.version,babel:c.Babel?.version,icons:icons.length}));
"""


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _source_files(
    project: Path,
    root: str,
    extensions: tuple[str, ...],
) -> dict[str, bytes]:
    allowed = tuple(dict.fromkeys(extensions))
    if (
        not allowed
        or len(allowed) != len(extensions)
        or any(not extension.startswith(".") for extension in allowed)
    ):
        raise BridgeError(f"designer stage extensions are invalid for {root}: {extensions}")
    directory = project / root
    if not directory.is_dir() or directory.is_symlink():
        raise BridgeError(f"designer stage source root is missing or symbolic: {root}")
    result: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(project).as_posix()
        if path.is_symlink():
            raise BridgeError(f"designer stage source contains a symbolic link: {relative}")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix not in allowed:
            raise BridgeError(f"designer stage source has an undeclared file type: {relative}")
        result[relative] = path.read_bytes()
    if not result:
        raise BridgeError(
            f"designer stage source root contains no declared {allowed} files: {root}"
        )
    return result


def _stage_destination(project: Path, round_id: str, output: str | None) -> Path:
    stage_root = (project / "design-bridge-stage").resolve()
    destination = (project / output).resolve() if output else stage_root / round_id
    try:
        relative = destination.relative_to(stage_root)
    except ValueError as exc:
        raise BridgeError(f"stage output must stay beneath {stage_root}: {destination}") from exc
    if not relative.parts:
        raise BridgeError("stage output must be a child of design-bridge-stage")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise BridgeError(f"stage output is not normalized: {destination}")
    return destination


def _run(
    command: list[str],
    project: Path,
    label: str,
    *,
    timeout: float | None = None,
    stall_timeout: float | None = None,
    heartbeat: float = 5.0,
) -> ProcessResult:
    return run_process(
        command,
        project,
        label,
        timeout=timeout,
        stall_timeout=stall_timeout,
        heartbeat=heartbeat,
    )


def _exact_node(project: Path, override: str | None) -> Path:
    selected = override or os.environ.get("GDB_NODE") or shutil.which("node")
    if not selected:
        raise BridgeError("Node is unavailable; install exact Node 24.18.0 or pass --node")
    discovered = shutil.which(selected) if not Path(selected).is_absolute() else selected
    node = Path(discovered or selected).resolve()
    if not node.is_file():
        raise BridgeError(f"selected Node executable is missing: {node}")
    probe = _run(
        [str(node), "-p", "JSON.stringify({node:process.version,sqlite:process.versions.sqlite})"],
        project,
        "Node runtime probe",
    )
    try:
        measured = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"Node runtime probe returned invalid JSON: {probe.stdout!r}") from exc
    if measured != EXPECTED_NODE:
        raise BridgeError(f"stage requires exactly {EXPECTED_NODE}; measured {measured}")
    return node


def _vendor_probe(
    project: Path, node: Path, icons: list[str], lucide_bundle: Path
) -> dict[str, Any]:
    completed = _run(
        [str(node), "-e", VENDOR_PROBE, str(project), str(lucide_bundle), json.dumps(icons)],
        project,
        "browser vendor global probe",
    )
    try:
        measured = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"browser vendor probe returned invalid JSON: {completed.stdout!r}") from exc
    expected = {"react": "18.3.1", "babel": "7.29.7", "icons": len(icons)}
    if measured != expected:
        raise BridgeError(f"browser vendor globals do not match {expected}: {measured}")
    return measured


def _copy_new(source: Path, target: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise BridgeError(f"required stage input is missing or symbolic: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)


def _stage_data(seed: Path, wasm: Path, stamp: str) -> bytes:
    assignments = {
        "__GDB_SEED_DB_BASE64__": base64.b64encode(seed.read_bytes()).decode("ascii"),
        "__GDB_SQLITE_WASM_BASE64__": base64.b64encode(wasm.read_bytes()).decode("ascii"),
        "__GDB_STAGE_STAMP__": stamp,
    }
    return (
        '"use strict";\n'
        + "\n".join(
            f"globalThis.{name}={json.dumps(value, separators=(',', ':'))};"
            for name, value in assignments.items()
        )
        + "\n"
    ).encode("ascii")


def prepare_stage(
    project: Path,
    *,
    output: str | None = None,
    replace: bool = False,
    node_override: str | None = None,
    process_timeout: float | None = None,
    process_stall_timeout: float | None = None,
    process_heartbeat: float = 5.0,
) -> dict[str, Any]:
    kit = kit_root(project)
    with exclusive_lock(kit / "runtime" / "mutation.lock", "stage-prepare"):
        config, state = load_project(project)
        active = state["active_round"]
        if not active or active["status"] not in {"outbound_open", "awaiting_return", "proving"}:
            raise BridgeError("stage-prepare requires an active outbound_open, awaiting_return, or proving round")
        record = load_round(project, active["id"])
        provenance = validate_round_provenance(project, record)
        for root in ("app/src/ui", "app/src/styles"):
            if not scope_covers(root, record["scope"]["designer_surface"]):
                raise BridgeError(f"active round does not grant the required designer source root: {root}")
        if "index.html" not in record["scope"]["entrypoints"]:
            raise BridgeError("active round must declare index.html as a package entrypoint")

        ui = _source_files(
            project,
            "app/src/ui",
            tuple(SURFACE["embedded"]["extensions"]),
        )
        styles = _source_files(
            project,
            "app/src/styles",
            tuple(SURFACE["direct"][0]["extensions"]),
        )
        contract = designer_global_contract(ui, "app/src/ui")
        if "App" not in contract["declarations"]:
            raise BridgeError("designer UI must declare an App component")
        node = _exact_node(project, node_override)
        destination = _stage_destination(project, record["id"], output)
        if destination.exists() and not replace:
            raise BridgeError(f"stage already exists; pass --replace to rebuild it: {destination}")

        stage_root = project / "design-bridge-stage"
        stage_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".stage-{record['id']}.", dir=stage_root))
        package = temporary / "package"
        package.mkdir()
        backup = temporary / "previous"
        try:
            seed = temporary / "designer-seed.sqlite3"
            runtime_build = temporary / "runtime-build"
            seed_build = _run(
                [str(node), "scripts/create-designer-seed.mjs", "--out", str(seed)],
                project,
                "designer SPECIMEN seed build",
                timeout=process_timeout,
                stall_timeout=process_stall_timeout,
                heartbeat=process_heartbeat,
            )
            try:
                seed_facts = json.loads(seed_build.stdout)
            except json.JSONDecodeError as exc:
                raise BridgeError(
                    f"designer SPECIMEN seed build returned invalid JSON: {seed_build.stdout!r}"
                ) from exc
            measured_seed = {
                "bytes": seed.stat().st_size,
                "sha256": sha256_file(seed),
            }
            if any(seed_facts.get(key) != value for key, value in measured_seed.items()):
                raise BridgeError(
                    f"designer SPECIMEN seed report disagrees with its bytes: "
                    f"reported={seed_facts}, measured={measured_seed}"
                )
            _run(
                [str(node), "scripts/build-designer-runtime.mjs", "--out", str(runtime_build)],
                project,
                "travelling runtime build",
                timeout=process_timeout,
                stall_timeout=process_stall_timeout,
                heartbeat=process_heartbeat,
            )
            icon_input = temporary / "designer-icons.json"
            icon_build = temporary / "designer-icons"
            icon_names = sorted({*contract["icons"], "LoaderCircle"})
            _write_new(icon_input, canonical_json({
                "schema": "gpt-design-bridge/designer-lucide-input/v1",
                "icons": icon_names,
            }))
            icon_result = _run(
                [
                    str(node), "scripts/build-designer-icons.mjs",
                    "--icons", str(icon_input), "--out", str(icon_build),
                ],
                project,
                "designer Lucide subset build",
                timeout=process_timeout,
                stall_timeout=process_stall_timeout,
                heartbeat=process_heartbeat,
            )
            try:
                icon_facts = json.loads(icon_result.stdout)
            except json.JSONDecodeError as exc:
                raise BridgeError(
                    f"designer Lucide subset build returned invalid JSON: {icon_result.stdout!r}"
                ) from exc
            if icon_facts.get("icons") != icon_names or icon_facts.get("files") != [
                "lucide-react.min.js"
            ]:
                raise BridgeError(
                    f"designer Lucide subset report disagrees with requested icons: {icon_facts}"
                )
            lucide_bundle = icon_build / "lucide-react.min.js"
            vendor_versions = _vendor_probe(project, node, icon_names, lucide_bundle)
            wasm = project / WASM_SOURCE
            vendor_hashes: dict[str, str] = {}
            for name, relative in VENDORS.items():
                source = project / relative
                _copy_new(source, package / "vendor" / name)
                vendor_hashes[name] = sha256_file(source)
            _copy_new(lucide_bundle, package / "vendor" / "lucide-react.min.js")
            vendor_hashes["lucide-react.min.js"] = sha256_file(lucide_bundle)
            _copy_new(runtime_build / "gdb-runtime.js", package / "runtime" / "gdb-runtime.js")
            runtime_hash = sha256_file(runtime_build / "gdb-runtime.js")
            travelling_assets = build_travelling_asset_pack(
                project, package, ui, styles, icon_names
            )
            source_manifest = build_source_manifest({**ui, **styles})
            stamp_inputs = {
                "schema": STAGE_SCHEMA,
                "round_id": record["id"],
                "source_tree_sha256": source_manifest["tree_sha256"],
                "seed_sha256": sha256_file(seed),
                "wasm_sha256": sha256_file(wasm),
                "runtime_sha256": runtime_hash,
                "vendor_sha256": vendor_hashes,
                "travelling_assets_sha256": travelling_assets["manifest_sha256"],
                "provenance_verification_sha256": provenance["verification_sha256"],
                "capability_manifest_sha256": provenance["capability_manifest_sha256"],
            }
            stamp = hashlib.sha256(canonical_json(stamp_inputs)).hexdigest()
            _write_new(package / "runtime" / "stage-data.js", _stage_data(seed, wasm, stamp))
            _write_new(package / "DESIGN-SURFACE.json", canonical_json(SURFACE))
            for source_path, content in styles.items():
                relative = Path(source_path).relative_to("app/src/styles")
                _write_new(package / "styles" / relative, content)
            page = render_stage_page(
                config["project"]["name"],
                stamp,
                contract["prelude"],
                sorted(styles),
            )
            _write_new(package / "index.html", render_index(page, ui))
            metadata = {
                **stamp_inputs,
                "stage_stamp": stamp,
                "node": EXPECTED_NODE,
                "browser_vendors": vendor_versions,
                "designer_contract": {
                    "imports": contract["imports"],
                    "icons": contract["icons"],
                    "bindings": contract["bindings"],
                    "declarations": contract["declarations"],
                },
                "provenance": provenance,
                "travelling_assets": travelling_assets,
                "specimen": {
                    "authority": "SPECIMEN only; never production truth",
                    "rows": seed_facts.get("specimenRows"),
                    "migration_sha256": seed_facts.get("migrationSha256"),
                },
                "index_sha256": sha256_file(package / "index.html"),
            }
            atomic_write_json(package / "STAGE-RUNTIME.json", metadata)
            _surface, verified_sources = verify_prepared_stage(project, package, record)
            if verified_sources["tree_sha256"] != source_manifest["tree_sha256"]:
                raise BridgeError("stage verification returned a different source tree")
            tree = build_tree_manifest(package)

            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                promote_directory(destination, backup)
            try:
                promote_directory(package, destination)
            except Exception:
                if backup.exists() and not destination.exists():
                    promote_directory(backup, destination)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            return {
                "schema": STAGE_SCHEMA,
                "round_id": record["id"],
                "stage_stamp": stamp,
                "root": destination.relative_to(project).as_posix(),
                "file_count": tree["file_count"],
                "tree_sha256": tree["tree_sha256"],
                "source_tree_sha256": source_manifest["tree_sha256"],
            }
        finally:
            if temporary.exists() and not backup.exists():
                shutil.rmtree(temporary)
