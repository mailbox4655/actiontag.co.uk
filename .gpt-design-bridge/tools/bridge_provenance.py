"""Fail-closed production provenance and capability-parity gates.

The bridge can prove that a package is deterministic while still packaging the wrong
product.  This module binds every new designer round to (1) the authoritative
production entrypoints, (2) a mechanically traversable path to the designer-owned
surface, (3) a production-derived capability baseline, and (4) controlled-browser
evidence from both production and the exact travelling package.

Runtime records live under ``.gpt-design-bridge/records/provenance``.  They are not a
replacement for GPT BlackBox Lite: capture requires an approved full task baseline and
verification requires that same task's passing final gate on the current Git tree.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter, deque
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from bridge_core import (
    BridgeError,
    atomic_write_json,
    canonical_json,
    kit_root,
    load_round,
    read_json,
    safe_relative_path,
    sha256_file,
    validate_iso_date,
)


DECLARATION_SCHEMA = "gpt-design-bridge/provenance-declaration/v1"
BASELINE_SCHEMA = "gpt-design-bridge/production-baseline/v1"
VERIFICATION_SCHEMA = "gpt-design-bridge/provenance-verification/v1"
DECISIONS_SCHEMA = "gpt-design-bridge/capability-decisions/v1"
PARITY_BINDINGS_SCHEMA = "gpt-design-bridge/parity-bindings/v1"
PARITY_SCHEMA = "gpt-design-bridge/capability-parity/v1"
CONTROLLED_PROOF_SCHEMA = "gpt-controlled-chrome-proof"
PROVENANCE_ID = re.compile(r"^P-[0-9]{3,}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
STANDARD_STATES = (
    "normal",
    "loading",
    "empty",
    "error",
    "success",
    "disabled",
    "busy",
    "permission",
    "responsive",
)
SOURCE_EXTENSIONS = {
    ".cjs", ".css", ".htm", ".html", ".js", ".jsx", ".mjs", ".php",
    ".svelte", ".ts", ".tsx", ".vue",
}
RESOLUTION_EXTENSIONS = ("", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".html", ".htm", ".vue", ".svelte", ".php")
IGNORED_PARTS = {
    ".git", ".gpt-design-bridge", "node_modules", "design-bridge-stage",
    "dist", "build", "coverage", ".next", ".nuxt",
}
INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea", "summary"}
INTERACTIVE_ROLES = {"button", "checkbox", "dialog", "link", "menuitem", "option", "radio", "slider", "switch", "tab", "textbox"}


def _git(project: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    completed = subprocess.run(
        ["git", "-C", str(project), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=merged,
        check=False,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise BridgeError(f"git {' '.join(args)} failed for {project}: {detail}")
    return completed


def worktree_tree(project: Path) -> str:
    """Return the same staged-plus-untracked tree identity used by BlackBox Lite."""
    git_dir = Path(_git(project, "rev-parse", "--absolute-git-dir").stdout.strip())
    temporary_root = git_dir / "gpt-design-bridge" / ".tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    index = temporary_root / f"index-{os.getpid()}-{uuid.uuid4().hex}"
    environment = {"GIT_INDEX_FILE": str(index)}
    try:
        head = _git(project, "rev-parse", "--verify", "HEAD", check=False)
        if head.returncode == 0:
            _git(project, "read-tree", "HEAD", env=environment)
        else:
            _git(project, "read-tree", "--empty", env=environment)
        _git(project, "add", "-A", "--", ".", env=environment)
        return _git(project, "write-tree", env=environment).stdout.strip()
    finally:
        index.unlink(missing_ok=True)


def _blackbox_task_dir(project: Path, task_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", task_id):
        raise BridgeError(f"invalid BlackBox task ID: {task_id!r}")
    git_dir = Path(_git(project, "rev-parse", "--absolute-git-dir").stdout.strip())
    return git_dir / "gpt-blackbox-lite" / task_id


def _path_matches(path: str, pattern: str) -> bool:
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    if path == pattern or path.startswith(pattern.rstrip("/") + "/"):
        return True
    return fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)


def _contract_covers(path: str, patterns: Iterable[str]) -> bool:
    return any(_path_matches(path, pattern) for pattern in patterns)


def _load_blackbox_contract(
    project: Path,
    task_id: str,
    *,
    final: bool,
    require_live_gate_tree: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    root = _blackbox_task_dir(project, task_id)
    contract_path = root / "contract.json"
    if not contract_path.is_file():
        raise BridgeError(f"BlackBox task contract does not exist: {task_id}")
    contract = read_json(contract_path)
    if Path(str(contract.get("repo_root", ""))).resolve() != project.resolve():
        raise BridgeError(f"BlackBox task {task_id} belongs to another repository")
    plan = contract.get("plan_approved")
    if not isinstance(plan, dict) or plan.get("tree") != contract.get("baseline_tree"):
        raise BridgeError(f"BlackBox task {task_id} has no approved current baseline")
    if contract.get("lane", {}).get("selected", contract.get("lane")) != "full":
        raise BridgeError("designer provenance requires the BlackBox full lane")
    risks = set(contract.get("risk_domains", []))
    if not {"designer", "preservation"}.issubset(risks):
        raise BridgeError("BlackBox task must declare designer and preservation risks")
    gate = None
    if final:
        gate_path = root / "final-gate.json"
        if not gate_path.is_file():
            raise BridgeError(f"BlackBox task {task_id} has no final gate")
        gate = read_json(gate_path)
        current = worktree_tree(project) if require_live_gate_tree else gate.get("current_tree")
        if gate.get("verdict") != "pass" or not isinstance(current, str) or gate.get("current_tree") != current:
            raise BridgeError(
                f"BlackBox final gate is absent, failed, or stale for current tree {current}"
            )
    return contract, gate


def guarded_product_sha256(project: Path) -> str:
    """Hash application/config bytes while excluding Design Bridge lifecycle ledgers.

    Round state, proof copies, returns, and outbound archives legitimately change after
    the BlackBox final gate.  They must not make the proved application tree stale.
    Source, contracts, project-local tools, compatibility maps, and every other file
    remain included.
    """
    operational = (
        ".gpt-design-bridge/records/",
        ".gpt-design-bridge/evidence/",
        ".gpt-design-bridge/runtime/",
        ".gpt-design-bridge/outbound/",
        ".gpt-design-bridge/returns/",
        ".gpt-design-bridge/baselines/",
    )
    excluded_exact = {".gpt-design-bridge/state.json"}
    listed = _git(project, "ls-files", "--cached", "--others", "--exclude-standard").stdout.splitlines()
    files: dict[str, dict[str, Any]] = {}
    for raw in listed:
        relative = raw.strip().replace("\\", "/")
        if not relative or relative in excluded_exact or relative.startswith(operational):
            continue
        path = project / relative
        if path.is_file() and not path.is_symlink():
            files[relative] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return hashlib.sha256(canonical_json(dict(sorted(files.items())))).hexdigest()


def _normal_paths(values: Any, label: str, *, require_files: bool = True, project: Path | None = None) -> list[str]:
    if not isinstance(values, list) or not values:
        raise BridgeError(f"{label} must be a non-empty array")
    result = [safe_relative_path(value, label=label) for value in values]
    if len(set(result)) != len(result):
        raise BridgeError(f"{label} contains duplicate paths")
    if project is not None:
        for relative in result:
            candidate = project / relative
            if require_files and not candidate.is_file():
                raise BridgeError(f"{label} is not a file: {relative}")
            if not require_files and not candidate.exists():
                raise BridgeError(f"{label} does not exist: {relative}")
    return result


def _source_files(project: Path) -> list[str]:
    completed = _git(project, "ls-files", "--cached", "--others", "--exclude-standard")
    files: list[str] = []
    for raw in completed.stdout.splitlines():
        relative = raw.strip().replace("\\", "/")
        if not relative or any(part in IGNORED_PARTS for part in PurePosixPath(relative).parts):
            continue
        path = project / relative
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in SOURCE_EXTENSIONS:
            files.append(relative)
    return sorted(set(files))


def _read_text(project: Path, relative: str) -> str:
    try:
        return (project / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BridgeError(f"cannot read source file {relative}: {exc}") from exc


REFERENCE_PATTERNS = (
    re.compile(r"(?:import|export)\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']"),
    re.compile(r"(?:import|require)\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
    re.compile(r"(?:src|href)\s*=\s*[\"']([^\"'#?]+)[\"']", re.I),
    re.compile(r"@import\s+(?:url\()?\s*[\"']?([^\"')\s]+)", re.I),
    re.compile(r"url\(\s*[\"']?([^\"')?#]+)", re.I),
)


def _resolve_reference(source: str, reference: str, known: set[str]) -> str | None:
    if reference.startswith(("http:", "https:", "data:", "//", "#")):
        return None
    clean = reference.split("?", 1)[0].split("#", 1)[0]
    if not clean:
        return None
    base = PurePosixPath(source).parent
    raw = PurePosixPath(clean.lstrip("/")) if clean.startswith("/") else base / clean
    parts: list[str] = []
    for part in raw.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            else:
                return None
        else:
            parts.append(part)
    stem = "/".join(parts)
    candidates = [stem + extension for extension in RESOLUTION_EXTENSIONS]
    candidates.extend(f"{stem}/index{extension}" for extension in RESOLUTION_EXTENSIONS[1:])
    return next((candidate for candidate in candidates if candidate in known), None)


def _explicit_edges(project: Path, declaration: dict[str, Any], known: set[str]) -> list[dict[str, Any]]:
    edges = declaration.get("edges", [])
    if not isinstance(edges, list):
        raise BridgeError("provenance declaration edges must be an array")
    normalized: list[dict[str, Any]] = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise BridgeError(f"provenance edge {index} must be an object")
        source = safe_relative_path(edge.get("from", ""), label=f"edge {index} from")
        target = safe_relative_path(edge.get("to", ""), label=f"edge {index} to")
        if source not in known or target not in known:
            raise BridgeError(f"provenance edge {source} -> {target} must join existing source files")
        kind = edge.get("kind")
        if kind not in {"build", "adapter"}:
            raise BridgeError(f"provenance edge {source} -> {target} has invalid kind")
        anchor = str(edge.get("source_anchor", ""))
        if not anchor or anchor not in _read_text(project, source):
            raise BridgeError(f"explicit {kind} edge {source} -> {target} lacks a real source anchor")
        item = {"from": source, "to": target, "kind": kind, "source_anchor": anchor}
        if kind == "build":
            mechanism = edge.get("mechanism")
            if not isinstance(mechanism, str) or not mechanism.strip():
                raise BridgeError(f"build edge {source} -> {target} requires a named mechanism")
            item["mechanism"] = mechanism.strip()
        if kind == "adapter":
            for name in ("forward", "inverse"):
                value = edge.get(name)
                if not isinstance(value, str) or not value.strip():
                    raise BridgeError(f"adapter edge {source} -> {target} requires {name}")
                item[name] = value.strip()
            if edge.get("preserves_topology") is not True:
                raise BridgeError(
                    f"adapter edge {source} -> {target} must preserve interaction topology; "
                    "fixture data may change, fixture topology may not"
                )
            item["preserves_topology"] = True
        normalized.append(item)
    return normalized


def build_graph(project: Path, declaration: dict[str, Any]) -> dict[str, Any]:
    files = _source_files(project)
    known = set(files)
    adjacency: dict[str, set[str]] = {path: set() for path in files}
    auto_edges: set[tuple[str, str]] = set()
    for source in files:
        text = _read_text(project, source)
        for pattern in REFERENCE_PATTERNS:
            for reference in pattern.findall(text):
                target = _resolve_reference(source, reference, known)
                if target:
                    adjacency[source].add(target)
                    auto_edges.add((source, target))
    explicit = _explicit_edges(project, declaration, known)
    for edge in explicit:
        adjacency[edge["from"]].add(edge["to"])
    production = _normal_paths(declaration.get("production_entrypoints"), "production entrypoint", project=project)
    travelling = _normal_paths(declaration.get("travelling_entrypoints"), "travelling entrypoint", project=project)
    surfaces = _normal_paths(declaration.get("designer_surface"), "designer surface", require_files=False, project=project)
    surface_files = sorted(
        path for path in files if any(path == root or path.startswith(root.rstrip("/") + "/") for root in surfaces)
    )
    if not surface_files:
        raise BridgeError("designer surface contains no source files")

    def reachable(starts: list[str]) -> set[str]:
        seen = set(starts)
        queue = deque(starts)
        while queue:
            for target in adjacency.get(queue.popleft(), ()):
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        return seen

    prod_reachable = reachable(production)
    travel_reachable = reachable(travelling)
    missing_prod = sorted(set(surface_files) - prod_reachable)
    missing_travel = sorted(set(surface_files) - travel_reachable)
    if missing_prod or missing_travel:
        raise BridgeError(
            "two-target/one-source provenance failed; "
            f"production cannot reach={missing_prod}; travelling cannot reach={missing_travel}"
        )
    payload = {
        "schema": "gpt-design-bridge/build-graph/v1",
        "production_entrypoints": production,
        "travelling_entrypoints": travelling,
        "designer_surface": surfaces,
        "surface_files": surface_files,
        "nodes": files,
        "automatic_edges": [{"from": left, "to": right} for left, right in sorted(auto_edges)],
        "explicit_edges": explicit,
    }
    payload["sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    return payload


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _capability_id(kind: str, identity: str) -> str:
    normalized = re.sub(r"[^a-z0-9._:/=-]+", "-", identity.lower()).strip("-")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"{kind}:{normalized[:90]}:{digest}"


class _CapabilityParser(HTMLParser):
    """Extract stable, user-visible capabilities from a completed browser DOM."""

    def __init__(self, state: str, proof_sha256: str) -> None:
        super().__init__(convert_charrefs=True)
        self.state = state
        self.proof_sha256 = proof_sha256
        self.stack: list[dict[str, Any]] = []
        self.capabilities: dict[str, dict[str, Any]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        role = values.get("role", "").lower()
        candidate = tag in INTERACTIVE_TAGS or role in INTERACTIVE_ROLES or tag in {"form", "dialog", "canvas"}
        node = {"tag": tag, "attrs": values, "text": [], "candidate": candidate}
        self.stack.append(node)
        if tag in {"input", "img", "br", "hr", "meta", "link"}:
            self._close_node(node)
            self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1]["tag"] == tag:
            self._close_node(self.stack.pop())

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i]["tag"] == tag), None)
        if index is None:
            return
        while len(self.stack) > index:
            node = self.stack.pop()
            self._close_node(node)
            if self.stack:
                self.stack[-1]["text"].extend(node["text"])

    def close(self) -> None:
        super().close()
        while self.stack:
            self._close_node(self.stack.pop())

    def _close_node(self, node: dict[str, Any]) -> None:
        if not node["candidate"]:
            return
        attrs = node["attrs"]
        tag = node["tag"]
        role = attrs.get("role", "")
        text = _normalized_text(" ".join(node["text"]))
        label = _normalized_text(
            attrs.get("aria-label")
            or attrs.get("title")
            or attrs.get("placeholder")
            or text
            or attrs.get("value")
        )
        identity = next(
            (attrs.get(name, "") for name in ("id", "data-testid", "data-test", "name") if attrs.get(name)),
            "",
        )
        if not identity:
            identity = f"{tag}|{role}|{label}|{attrs.get('href', '')}|{attrs.get('type', '')}"
        kind = role or (
            "route" if tag == "a" and attrs.get("href") else
            "dialog" if tag == "dialog" else
            "map" if tag == "canvas" or "map" in attrs.get("class", "").lower() else
            "form" if tag == "form" else
            "control"
        )
        capability_id = _capability_id(f"dom-{kind}", identity)
        self.capabilities[capability_id] = {
            "id": capability_id,
            "kind": f"dom-{kind}",
            "label": label or identity,
            "evidence": [{"state": self.state, "proof_sha256": self.proof_sha256}],
            "selector_identity": identity,
        }


def _proof_artifact(proof_path: Path, name: str) -> Path:
    path = proof_path.parent / name
    if not path.is_file() or path.is_symlink():
        raise BridgeError(f"controlled Chrome proof is missing regular artifact {name}: {path}")
    return path


def validate_controlled_proof(path: Path) -> dict[str, Any]:
    proof = read_json(path)
    browser = proof.get("browser")
    runtime = proof.get("runtime")
    actions = proof.get("actions")
    if proof.get("schema") != CONTROLLED_PROOF_SCHEMA or proof.get("version") != 2:
        raise BridgeError(f"unsupported controlled Chrome proof: {path}")
    if proof.get("status") != "pass":
        raise BridgeError(f"controlled Chrome proof did not pass: {path}")
    if not isinstance(browser, dict) or any(
        (
            browser.get("ownership") != "harness-only-child-process-and-fresh-temporary-profile",
            browser.get("product") != "Chrome for Testing",
            browser.get("headless") is not False,
            browser.get("normalFileSecurity") is not True,
            browser.get("existingProfileAccess") is not False,
            browser.get("ownerDefaultBrowserUsed") is not False,
            browser.get("temporaryProfileRemoved") is not True,
        )
    ):
        raise BridgeError(f"proof did not use owned visible Chrome for Testing safely: {path}")
    if not isinstance(runtime, dict) or runtime.get("errors") != []:
        raise BridgeError(f"controlled Chrome proof contains runtime errors: {path}")
    if not isinstance(actions, list) or not actions or any(
        not isinstance(action, dict) or action.get("status") != "pass" for action in actions
    ):
        raise BridgeError(f"controlled Chrome proof has no passing interaction trace: {path}")
    dom = _proof_artifact(path, "dom.html")
    screenshot = _proof_artifact(path, "final.png")
    return {
        "path": path,
        "proof": proof,
        "proof_sha256": sha256_file(path),
        "dom": dom,
        "dom_sha256": sha256_file(dom),
        "screenshot": screenshot,
        "screenshot_sha256": sha256_file(screenshot),
    }


def _proof_map(values: dict[str, Path], coverage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if set(coverage) != set(STANDARD_STATES):
        missing = sorted(set(STANDARD_STATES) - set(coverage))
        extra = sorted(set(coverage) - set(STANDARD_STATES))
        raise BridgeError(f"state coverage must declare every standard state; missing={missing}, extra={extra}")
    if set(values) - set(STANDARD_STATES):
        raise BridgeError(f"unknown proof states: {sorted(set(values) - set(STANDARD_STATES))}")
    result: dict[str, dict[str, Any]] = {}
    for state in STANDARD_STATES:
        declaration = coverage[state]
        if not isinstance(declaration, dict) or declaration.get("status") not in {"proof", "not-applicable"}:
            raise BridgeError(f"state coverage {state} must declare status proof or not-applicable")
        if declaration["status"] == "proof":
            if state not in values:
                raise BridgeError(f"state coverage {state} requires a controlled Chrome proof")
            result[state] = validate_controlled_proof(values[state])
        else:
            if state in values:
                raise BridgeError(f"state coverage {state} is marked not-applicable but received a proof")
            reason = declaration.get("reason")
            source = declaration.get("source")
            anchor = declaration.get("anchor")
            if not all(isinstance(item, str) and item.strip() for item in (reason, source, anchor)):
                raise BridgeError(f"not-applicable state {state} requires reason, source, and anchor")
            result[state] = {"not_applicable": True, "reason": reason, "source": source, "anchor": anchor}
    if "normal" not in values or "responsive" not in values:
        raise BridgeError("normal and responsive controlled Chrome proofs are mandatory")
    return result


def _extract_dom_capabilities(proofs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    for state, evidence in proofs.items():
        if evidence.get("not_applicable"):
            continue
        parser = _CapabilityParser(state, evidence["proof_sha256"])
        parser.feed(evidence["dom"].read_text(encoding="utf-8"))
        parser.close()
        for capability_id, capability in parser.capabilities.items():
            if capability_id in capabilities:
                capabilities[capability_id]["evidence"].extend(capability["evidence"])
            else:
                capabilities[capability_id] = capability
    return capabilities


def _declared_capabilities(project: Path, declaration: dict[str, Any], proofs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values = declaration.get("capabilities", [])
    if not isinstance(values, list):
        raise BridgeError("provenance declaration capabilities must be an array")
    capabilities: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise BridgeError(f"declared capability {index} must be an object")
        capability_id = raw.get("id")
        kind = raw.get("kind")
        label = raw.get("label")
        source = safe_relative_path(raw.get("source", ""), label=f"capability {index} source")
        anchor = raw.get("anchor")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]*", str(capability_id or "")):
            raise BridgeError(f"declared capability {index} has an invalid stable ID")
        if not all(isinstance(item, str) and item.strip() for item in (kind, label, anchor)):
            raise BridgeError(f"declared capability {capability_id} requires kind, label, and anchor")
        if not (project / source).is_file() or anchor not in _read_text(project, source):
            raise BridgeError(f"declared capability {capability_id} lacks its source anchor in {source}")
        evidence = raw.get("browser_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise BridgeError(f"declared capability {capability_id} requires browser_evidence")
        normalized_evidence: list[dict[str, Any]] = []
        for binding in evidence:
            if not isinstance(binding, dict) or binding.get("state") not in proofs:
                raise BridgeError(f"declared capability {capability_id} has invalid browser state evidence")
            proof = proofs[binding["state"]]
            if proof.get("not_applicable"):
                raise BridgeError(f"declared capability {capability_id} cites a not-applicable state")
            action_indexes = binding.get("action_indexes", [])
            if not isinstance(action_indexes, list) or not action_indexes or any(
                type(value) is not int or value < 0 or value >= len(proof["proof"]["actions"])
                for value in action_indexes
            ):
                raise BridgeError(f"declared capability {capability_id} has invalid action indexes")
            selectors = binding.get("selectors", [])
            if not isinstance(selectors, list) or not selectors or any(
                not isinstance(value, str) or not value.strip() for value in selectors
            ):
                raise BridgeError(f"declared capability {capability_id} requires selectors")
            dom_text = proof["dom"].read_text(encoding="utf-8")
            if any(selector not in dom_text for selector in selectors):
                raise BridgeError(
                    f"declared capability {capability_id} selector identity is absent from {binding['state']} DOM"
                )
            normalized_evidence.append({
                "state": binding["state"],
                "action_indexes": action_indexes,
                "selectors": selectors,
                "proof_sha256": proof["proof_sha256"],
            })
        capabilities[capability_id] = {
            "id": capability_id,
            "kind": kind.strip(),
            "label": label.strip(),
            "source": source,
            "source_anchor_sha256": hashlib.sha256(anchor.encode("utf-8")).hexdigest(),
            "evidence": normalized_evidence,
        }
    return capabilities


def _source_tripwires(project: Path, files: Iterable[str]) -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    library_patterns = {
        "leaflet": re.compile(r"\b(?:L\.|leaflet)\b", re.I),
        "mapbox": re.compile(r"\bmapbox(?:gl)?\b", re.I),
        "openlayers": re.compile(r"\bol\.(?:Map|View|layer)\b|openlayers", re.I),
    }
    export_pattern = re.compile(r"\b(?:export|download)[^\n]{0,80}\b(csv|json|kml|xlsx|pdf)\b", re.I)
    storage_pattern = re.compile(r"\b(localStorage|sessionStorage)\.(?:getItem|setItem|removeItem)\(\s*['\"]([^'\"]+)", re.I)
    for relative in files:
        text = _read_text(project, relative)
        for name, pattern in library_patterns.items():
            if pattern.search(text):
                capability_id = f"source-library:{name}"
                capabilities[capability_id] = {"id": capability_id, "kind": "source-library", "label": name, "sources": sorted(set([*capabilities.get(capability_id, {}).get("sources", []), relative]))}
        for match in export_pattern.finditer(text):
            name = match.group(1).lower()
            capability_id = f"source-export:{name}"
            capabilities[capability_id] = {"id": capability_id, "kind": "source-export", "label": name, "sources": sorted(set([*capabilities.get(capability_id, {}).get("sources", []), relative]))}
        for match in storage_pattern.finditer(text):
            storage, key = match.groups()
            capability_id = _capability_id("source-storage", f"{storage.lower()}:{key}")
            capabilities[capability_id] = {"id": capability_id, "kind": "source-storage", "label": f"{storage}:{key}", "sources": sorted(set([*capabilities.get(capability_id, {}).get("sources", []), relative]))}
    return capabilities


def build_capability_manifest(project: Path, declaration: dict[str, Any], proofs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    graph = build_graph(project, declaration)
    capabilities = _extract_dom_capabilities(proofs)
    for capability_id, capability in _declared_capabilities(project, declaration, proofs).items():
        if capability_id in capabilities:
            raise BridgeError(f"declared capability ID collides with derived capability: {capability_id}")
        capabilities[capability_id] = capability
    capabilities.update(_source_tripwires(project, graph["nodes"]))
    coverage = {
        state: (
            {"status": "not-applicable", "reason": item["reason"], "source": item["source"], "anchor": item["anchor"]}
            if item.get("not_applicable") else
            {"status": "proof", "proof_sha256": item["proof_sha256"], "dom_sha256": item["dom_sha256"], "screenshot_sha256": item["screenshot_sha256"]}
        )
        for state, item in proofs.items()
    }
    counts = dict(sorted(Counter(item["kind"] for item in capabilities.values()).items()))
    manifest = {
        "schema": "gpt-design-bridge/capability-manifest/v1",
        "capabilities": dict(sorted(capabilities.items())),
        "counts": counts,
        "total": len(capabilities),
        "state_coverage": coverage,
    }
    manifest["sha256"] = hashlib.sha256(canonical_json(manifest)).hexdigest()
    return manifest


def declaration_template(task_id: str) -> dict[str, Any]:
    return {
        "schema": DECLARATION_SCHEMA,
        "task_id": task_id,
        "mode": "direct",
        "production_entrypoints": ["app/src/main.jsx"],
        "travelling_entrypoints": ["index.html"],
        "designer_surface": ["app/src/ui", "app/src/styles"],
        "compatibility_records": [],
        "edges": [],
        "capabilities": [
            {
                "id": "primary-workflow",
                "kind": "workflow",
                "label": "Primary user workflow",
                "source": "app/src/ui/App.jsx",
                "anchor": "PrimaryAction",
                "browser_evidence": [
                    {"state": "normal", "action_indexes": [0, 1], "selectors": ["primary-action"]}
                ],
            }
        ],
        "state_coverage": {
            state: (
                {"status": "proof"}
                if state in {"normal", "responsive"}
                else {
                    "status": "not-applicable",
                    "reason": f"Explain why {state} has no distinct user-visible state.",
                    "source": "app/src/ui/App.jsx",
                    "anchor": "App",
                }
            )
            for state in STANDARD_STATES
        },
    }


def validate_declaration(project: Path, declaration: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema", "task_id", "mode", "production_entrypoints", "travelling_entrypoints",
        "designer_surface", "compatibility_records", "edges", "capabilities", "state_coverage",
    }
    if set(declaration) != expected or declaration.get("schema") != DECLARATION_SCHEMA:
        raise BridgeError(
            f"provenance declaration must use the strict v1 schema; missing={sorted(expected - set(declaration))}, "
            f"unexpected={sorted(set(declaration) - expected)}"
        )
    if declaration.get("task_id") != contract.get("task_id"):
        raise BridgeError("provenance declaration task_id does not match the BlackBox task")
    mode = declaration.get("mode")
    if mode not in {"direct", "adapter-seam"}:
        raise BridgeError("provenance mode must be direct or adapter-seam")
    production = _normal_paths(declaration["production_entrypoints"], "production entrypoint", project=project)
    travelling = _normal_paths(declaration["travelling_entrypoints"], "travelling entrypoint", project=project)
    surfaces = _normal_paths(declaration["designer_surface"], "designer surface", require_files=False, project=project)
    compatibility = declaration["compatibility_records"]
    if not isinstance(compatibility, list):
        raise BridgeError("compatibility_records must be an array")
    compatibility_paths = [safe_relative_path(value, label="compatibility record") for value in compatibility]
    if len(set(compatibility_paths)) != len(compatibility_paths):
        raise BridgeError("compatibility_records contains duplicates")
    for relative in compatibility_paths:
        if not (project / relative).is_file():
            raise BridgeError(f"compatibility record is not a file: {relative}")

    allowed = contract.get("allowed_paths", [])
    boundaries = contract.get("owner_boundaries", [])
    protected = contract.get("protected_anchors", [])
    for relative in surfaces:
        if not _contract_covers(relative, allowed):
            raise BridgeError(f"BlackBox allowlist does not cover designer surface: {relative}")
    protected_files = {str(item).split("::", 1)[0] for item in protected if isinstance(item, str)}
    for relative in production:
        if relative not in protected_files and not _contract_covers(relative, boundaries):
            raise BridgeError(
                f"authoritative production entrypoint is not a protected BlackBox anchor/boundary: {relative}"
            )
    for raw in declaration["capabilities"]:
        if isinstance(raw, dict):
            source = str(raw.get("source", ""))
            if source and source not in protected_files and not _contract_covers(source, boundaries):
                raise BridgeError(
                    f"declared production capability source is not protected by BlackBox: {source}"
                )
    for relative in compatibility_paths:
        if not _contract_covers(relative, boundaries):
            raise BridgeError(
                f"compatibility record must be an immutable BlackBox owner boundary before capture: {relative}"
            )
    if contract.get("require_browser") is not True:
        raise BridgeError("designer provenance BlackBox tasks must require browser proof")
    risks = set(contract.get("risk_domains", []))
    if mode == "adapter-seam" and "architecture" not in risks:
        raise BridgeError("adapter-seam provenance requires the BlackBox architecture risk")
    edges = _explicit_edges(project, declaration, set(_source_files(project)))
    if mode == "adapter-seam" and not any(edge["kind"] == "adapter" for edge in edges):
        raise BridgeError("adapter-seam mode requires at least one proved forward/inverse adapter edge")
    if mode == "direct" and any(edge["kind"] == "adapter" for edge in edges):
        raise BridgeError("direct provenance may not conceal an adapter edge")
    coverage = declaration["state_coverage"]
    if not isinstance(coverage, dict):
        raise BridgeError("state_coverage must be an object")
    # Validate N/A anchors now, not only when proof is captured.
    for state, item in coverage.items():
        if isinstance(item, dict) and item.get("status") == "not-applicable":
            source = safe_relative_path(item.get("source", ""), label=f"{state} state source")
            anchor = item.get("anchor")
            if not (project / source).is_file() or not isinstance(anchor, str) or anchor not in _read_text(project, source):
                raise BridgeError(f"not-applicable state {state} lacks a real source anchor")
    normalized = dict(declaration)
    normalized["production_entrypoints"] = production
    normalized["travelling_entrypoints"] = travelling
    normalized["designer_surface"] = surfaces
    normalized["compatibility_records"] = compatibility_paths
    return normalized


def _provenance_root(project: Path) -> Path:
    return kit_root(project) / "records" / "provenance"


def _provenance_ids(project: Path) -> list[str]:
    root = _provenance_root(project)
    if not root.is_dir():
        return []
    result: list[str] = []
    for item in root.iterdir():
        if item.name == ".gitkeep":
            continue
        if not item.is_dir() or not PROVENANCE_ID.fullmatch(item.name):
            raise BridgeError(f"unexpected provenance record entry: {item}")
        result.append(item.name)
    return sorted(result)


def next_provenance_id(project: Path) -> str:
    numbers = [int(value.split("-", 1)[1]) for value in _provenance_ids(project)]
    return f"P-{max(numbers, default=0) + 1:03d}"


def _copy_proofs(destination: Path, proofs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for state, evidence in proofs.items():
        if evidence.get("not_applicable"):
            result[state] = {
                "status": "not-applicable",
                "reason": evidence["reason"],
                "source": evidence["source"],
                "anchor": evidence["anchor"],
            }
            continue
        state_root = destination / state
        state_root.mkdir(parents=True)
        for source, name in (
            (evidence["path"], "proof.json"),
            (evidence["dom"], "dom.html"),
            (evidence["screenshot"], "final.png"),
        ):
            shutil.copyfile(source, state_root / name)
        result[state] = {
            "status": "proof",
            "proof": f"{state}/proof.json",
            "proof_sha256": evidence["proof_sha256"],
            "dom": f"{state}/dom.html",
            "dom_sha256": evidence["dom_sha256"],
            "screenshot": f"{state}/final.png",
            "screenshot_sha256": evidence["screenshot_sha256"],
        }
    return result


def _compatibility_hashes(project: Path, paths: list[str]) -> dict[str, str]:
    return {relative: sha256_file(project / relative) for relative in paths}


def _manifest_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    left = before["capabilities"]
    right = after["capabilities"]
    missing = sorted(set(left) - set(right))
    added = sorted(set(right) - set(left))
    changed = sorted(
        key for key in set(left) & set(right)
        if {name: left[key].get(name) for name in ("kind", "label")}
        != {name: right[key].get(name) for name in ("kind", "label")}
    )
    return {
        "missing": missing,
        "added": added,
        "changed": changed,
        "before_counts": before["counts"],
        "after_counts": after["counts"],
    }


def _validate_decisions(
    path: Path | None,
    *,
    task_id: str,
    missing: list[str],
    changed: list[str],
    compatibility_changes: dict[str, tuple[str, str]],
    after_capability_ids: set[str],
    user_approved_capability_changes: bool,
    user_approved_compatibility_change: bool,
) -> list[dict[str, Any]]:
    required = set(missing) | set(changed)
    if not required and not compatibility_changes:
        if path is not None:
            raise BridgeError("decision file was supplied but no decision is required")
        return []
    if path is None:
        raise BridgeError("unexplained capability or compatibility loss requires an exact owner decision file")
    data = read_json(path)
    expected = {"schema", "task_id", "capability_changes", "compatibility_changes"}
    if set(data) != expected or data.get("schema") != DECISIONS_SCHEMA or data.get("task_id") != task_id:
        raise BridgeError("capability decision file must use the strict v1 schema and current task ID")
    changes = data["capability_changes"]
    if not isinstance(changes, list):
        raise BridgeError("capability_changes must be an array")
    declared = {item.get("capability_id") for item in changes if isinstance(item, dict)}
    if declared != required:
        raise BridgeError(f"owner capability decisions do not match the exact diff; required={sorted(required)}, declared={sorted(declared)}")
    if required and not user_approved_capability_changes:
        raise BridgeError("capability loss needs --user-approved-capability-changes after the owner's exact approval")
    for item in changes:
        if set(item) != {"capability_id", "change", "replacement_id", "question", "decision", "date", "owner_approved"}:
            raise BridgeError("every capability decision must use the strict entry schema")
        if item["change"] not in {"remove", "replace", "change"} or item["owner_approved"] is not True:
            raise BridgeError(f"invalid owner capability decision: {item.get('capability_id')}")
        if item["change"] == "replace" and not isinstance(item["replacement_id"], str):
            raise BridgeError(f"replacement capability requires replacement_id: {item['capability_id']}")
        if item["change"] != "replace" and item["replacement_id"] is not None:
            raise BridgeError(f"non-replacement capability decision must use null replacement_id: {item['capability_id']}")
        if item["capability_id"] in missing and item["change"] not in {"remove", "replace"}:
            raise BridgeError(f"missing capability requires remove or replace: {item['capability_id']}")
        if item["capability_id"] in changed and item["change"] != "change":
            raise BridgeError(f"changed capability requires change: {item['capability_id']}")
        if item["change"] == "replace" and item["replacement_id"] not in after_capability_ids:
            raise BridgeError(
                f"replacement capability is absent from the verified product: {item['replacement_id']}"
            )
        for field in ("question", "decision"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise BridgeError(f"capability decision {item['capability_id']} requires {field}")
        validate_iso_date(item["date"], f"capability decision {item['capability_id']} date")
    compatibility = data["compatibility_changes"]
    if not isinstance(compatibility, list):
        raise BridgeError("compatibility_changes must be an array")
    declared_paths = {item.get("path") for item in compatibility if isinstance(item, dict)}
    if declared_paths != set(compatibility_changes):
        raise BridgeError("owner compatibility decisions do not match the exact changed records")
    if compatibility_changes and not user_approved_compatibility_change:
        raise BridgeError("compatibility record changes need --user-approved-compatibility-change")
    for item in compatibility:
        if set(item) != {"path", "before_sha256", "after_sha256", "question", "decision", "date", "owner_approved"}:
            raise BridgeError("every compatibility decision must use the strict entry schema")
        before, after = compatibility_changes[item["path"]]
        if item["before_sha256"] != before or item["after_sha256"] != after or item["owner_approved"] is not True:
            raise BridgeError(f"compatibility decision is not bound to exact bytes: {item['path']}")
        for field in ("question", "decision"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise BridgeError(f"compatibility decision {item['path']} requires {field}")
        validate_iso_date(item["date"], f"compatibility decision {item['path']} date")
    return changes


def capture_provenance(project: Path, declaration_path: Path, proof_paths: dict[str, Path]) -> dict[str, Any]:
    raw = read_json(declaration_path)
    task_id = raw.get("task_id", "")
    contract, _gate = _load_blackbox_contract(project, task_id, final=False)
    if worktree_tree(project) != contract["baseline_tree"]:
        raise BridgeError("provenance capture must run on the exact approved BlackBox baseline before UI edits")
    declaration = validate_declaration(project, raw, contract)
    proofs = _proof_map(proof_paths, declaration["state_coverage"])
    graph = build_graph(project, declaration)
    manifest = build_capability_manifest(project, declaration, proofs)
    provenance_id = next_provenance_id(project)
    root = _provenance_root(project)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / provenance_id
    temporary = root / f".{provenance_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    if destination.exists() or temporary.exists():
        raise BridgeError(f"provenance record path collision: {provenance_id}")
    temporary.mkdir()
    try:
        atomic_write_json(temporary / "declaration.json", declaration)
        atomic_write_json(temporary / "build-graph.json", graph)
        atomic_write_json(temporary / "capabilities.json", manifest)
        evidence = _copy_proofs(temporary / "baseline-evidence", proofs)
        baseline = {
            "schema": BASELINE_SCHEMA,
            "id": provenance_id,
            "task_id": task_id,
            "baseline_tree": contract["baseline_tree"],
            "mode": declaration["mode"],
            "production_entrypoints": declaration["production_entrypoints"],
            "travelling_entrypoints": declaration["travelling_entrypoints"],
            "designer_surface": declaration["designer_surface"],
            "compatibility_sha256": _compatibility_hashes(project, declaration["compatibility_records"]),
            "declaration_sha256": sha256_file(temporary / "declaration.json"),
            "build_graph_sha256": graph["sha256"],
            "capability_manifest_sha256": manifest["sha256"],
            "evidence": evidence,
        }
        atomic_write_json(temporary / "baseline.json", baseline)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    baseline["baseline_sha256"] = sha256_file(destination / "baseline.json")
    return baseline


def load_provenance(project: Path, provenance_id: str) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    if not PROVENANCE_ID.fullmatch(provenance_id):
        raise BridgeError(f"invalid provenance ID: {provenance_id!r}")
    root = _provenance_root(project) / provenance_id
    baseline_path = root / "baseline.json"
    declaration_path = root / "declaration.json"
    if not baseline_path.is_file() or not declaration_path.is_file():
        raise BridgeError(f"provenance record is incomplete: {provenance_id}")
    baseline = read_json(baseline_path)
    declaration = read_json(declaration_path)
    if baseline.get("schema") != BASELINE_SCHEMA or baseline.get("id") != provenance_id:
        raise BridgeError(f"invalid provenance baseline: {baseline_path}")
    if baseline.get("declaration_sha256") != sha256_file(declaration_path):
        raise BridgeError(f"provenance declaration bytes changed after capture: {provenance_id}")
    verification_path = root / "verification.json"
    verification = read_json(verification_path) if verification_path.is_file() else None
    return root, baseline, declaration, verification


def verify_provenance(
    project: Path,
    provenance_id: str,
    proof_paths: dict[str, Path],
    *,
    decisions_path: Path | None = None,
    user_approved_capability_changes: bool = False,
    user_approved_compatibility_change: bool = False,
) -> dict[str, Any]:
    root, baseline, declaration, previous = load_provenance(project, provenance_id)
    if previous is not None:
        raise BridgeError(
            f"provenance {provenance_id} is already verified; capture a new baseline for another tree"
        )
    contract, gate = _load_blackbox_contract(project, baseline["task_id"], final=True)
    declaration = validate_declaration(project, declaration, contract)
    current_tree = worktree_tree(project)
    assert gate is not None
    if gate.get("current_tree") != current_tree:
        raise BridgeError("BlackBox final gate is stale during provenance verification")
    proofs = _proof_map(proof_paths, declaration["state_coverage"])
    graph = build_graph(project, declaration)
    manifest = build_capability_manifest(project, declaration, proofs)
    baseline_manifest = read_json(root / "capabilities.json")
    diff = _manifest_diff(baseline_manifest, manifest)
    current_compatibility = _compatibility_hashes(project, declaration["compatibility_records"])
    compatibility_changes = {
        path: (before, current_compatibility.get(path, ""))
        for path, before in baseline["compatibility_sha256"].items()
        if current_compatibility.get(path) != before
    }
    decisions = _validate_decisions(
        decisions_path,
        task_id=baseline["task_id"],
        missing=diff["missing"],
        changed=diff["changed"],
        compatibility_changes=compatibility_changes,
        after_capability_ids=set(manifest["capabilities"]),
        user_approved_capability_changes=user_approved_capability_changes,
        user_approved_compatibility_change=user_approved_compatibility_change,
    )
    atomic_write_json(root / "current-build-graph.json", graph)
    atomic_write_json(root / "current-capabilities.json", manifest)
    evidence = _copy_proofs(root / "verification-evidence", proofs)
    verification = {
        "schema": VERIFICATION_SCHEMA,
        "id": provenance_id,
        "task_id": baseline["task_id"],
        "status": "pass",
        "guarded_tree": guarded_product_sha256(project),
        "blackbox_final_tree": current_tree,
        "blackbox_final_gate_sha256": sha256_file(_blackbox_task_dir(project, baseline["task_id"]) / "final-gate.json"),
        "baseline_sha256": sha256_file(root / "baseline.json"),
        "build_graph_sha256": graph["sha256"],
        "capability_manifest_sha256": manifest["sha256"],
        "capability_diff": diff,
        "approved_capability_changes": decisions,
        "compatibility_sha256": current_compatibility,
        "approved_compatibility_changes": sorted(compatibility_changes),
        "decisions_sha256": sha256_file(decisions_path) if decisions_path else None,
        "evidence": evidence,
    }
    atomic_write_json(root / "verification.json", verification)
    verification["verification_sha256"] = sha256_file(root / "verification.json")
    return verification


def validate_provenance_fresh(project: Path, provenance_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root, baseline, declaration, verification = load_provenance(project, provenance_id)
    if verification is None or verification.get("schema") != VERIFICATION_SCHEMA or verification.get("status") != "pass":
        raise BridgeError(f"provenance {provenance_id} has no passing verification")
    if verification.get("baseline_sha256") != sha256_file(root / "baseline.json"):
        raise BridgeError(f"provenance baseline changed after verification: {provenance_id}")
    if verification.get("build_graph_sha256") != read_json(root / "current-build-graph.json").get("sha256"):
        raise BridgeError(f"provenance build graph changed after verification: {provenance_id}")
    if verification.get("capability_manifest_sha256") != read_json(root / "current-capabilities.json").get("sha256"):
        raise BridgeError(f"provenance capability manifest changed after verification: {provenance_id}")
    current = guarded_product_sha256(project)
    if verification.get("guarded_tree") != current:
        raise BridgeError(
            f"provenance {provenance_id} is stale: verified {verification.get('guarded_tree')}, current {current}"
        )
    _contract, gate = _load_blackbox_contract(
        project,
        baseline["task_id"],
        final=True,
        require_live_gate_tree=False,
    )
    assert gate is not None
    gate_path = _blackbox_task_dir(project, baseline["task_id"]) / "final-gate.json"
    if verification.get("blackbox_final_gate_sha256") != sha256_file(gate_path):
        raise BridgeError(f"provenance {provenance_id} BlackBox final-gate bytes changed")
    if gate.get("current_tree") != verification.get("blackbox_final_tree"):
        raise BridgeError(f"provenance {provenance_id} is not bound to the current BlackBox gate")
    return baseline, declaration, verification


def round_provenance_binding(
    project: Path,
    provenance_id: str,
    designer_surface: list[str],
    entrypoints: list[str],
) -> dict[str, Any]:
    root, baseline, _declaration, verification = load_provenance(project, provenance_id)
    if verification is None:
        raise BridgeError(f"provenance {provenance_id} is not verified")
    validate_provenance_fresh(project, provenance_id)
    normalized_surfaces = [safe_relative_path(value, label="designer surface") for value in designer_surface]
    normalized_entries = [safe_relative_path(value, label="package entrypoint") for value in entrypoints]
    if normalized_surfaces != baseline["designer_surface"]:
        raise BridgeError(
            "round designer surface must exactly match the verified provenance baseline; "
            f"expected={baseline['designer_surface']}, received={normalized_surfaces}"
        )
    if normalized_entries != baseline["travelling_entrypoints"]:
        raise BridgeError(
            "round entrypoints must exactly match the verified travelling entrypoints; "
            f"expected={baseline['travelling_entrypoints']}, received={normalized_entries}"
        )
    return {
        "id": provenance_id,
        "mode": baseline["mode"],
        "baseline_sha256": sha256_file(root / "baseline.json"),
        "verification_sha256": sha256_file(root / "verification.json"),
        "guarded_tree": verification["guarded_tree"],
        "build_graph_sha256": verification["build_graph_sha256"],
        "capability_manifest_sha256": verification["capability_manifest_sha256"],
    }


def _validate_stored_binding(
    project: Path,
    binding: dict[str, Any],
    designer_surface: list[str],
    entrypoints: list[str],
    *,
    require_fresh: bool,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Validate immutable provenance bytes, optionally against the current product.

    An outbound binding remains historical truth after an accepted designer return
    changes production source.  It must still be byte-valid, but only the selected
    post-adoption binding is allowed to claim freshness for the rebuilt product.
    """
    if not isinstance(binding, dict) or not PROVENANCE_ID.fullmatch(str(binding.get("id", ""))):
        raise BridgeError("provenance binding has no valid ID")
    root, baseline, _declaration, verification = load_provenance(project, binding["id"])
    if verification is None or verification.get("status") != "pass":
        raise BridgeError(f"provenance {binding['id']} has no passing verification")
    normalized_surfaces = [safe_relative_path(value, label="designer surface") for value in designer_surface]
    normalized_entries = [safe_relative_path(value, label="package entrypoint") for value in entrypoints]
    if normalized_surfaces != baseline["designer_surface"]:
        raise BridgeError(f"provenance {binding['id']} designer surface no longer matches the round")
    if normalized_entries != baseline["travelling_entrypoints"]:
        raise BridgeError(f"provenance {binding['id']} travelling entrypoints no longer match the round")
    expected = {
        "id": binding["id"],
        "mode": baseline["mode"],
        "baseline_sha256": sha256_file(root / "baseline.json"),
        "verification_sha256": sha256_file(root / "verification.json"),
        "guarded_tree": verification["guarded_tree"],
        "build_graph_sha256": verification["build_graph_sha256"],
        "capability_manifest_sha256": verification["capability_manifest_sha256"],
    }
    if binding != expected:
        raise BridgeError(f"provenance {binding['id']} binding bytes are stale or altered")
    _contract, gate = _load_blackbox_contract(
        project,
        baseline["task_id"],
        final=True,
        require_live_gate_tree=False,
    )
    assert gate is not None
    gate_path = _blackbox_task_dir(project, baseline["task_id"]) / "final-gate.json"
    if verification.get("blackbox_final_gate_sha256") != sha256_file(gate_path):
        raise BridgeError(f"provenance {binding['id']} BlackBox final-gate bytes changed")
    if gate.get("current_tree") != verification.get("blackbox_final_tree"):
        raise BridgeError(f"provenance {binding['id']} BlackBox final-gate tree changed")
    if require_fresh:
        validate_provenance_fresh(project, binding["id"])
    return root, baseline, verification


def validate_round_provenance(project: Path, record: dict[str, Any]) -> dict[str, Any]:
    binding = record.get("provenance")
    if not isinstance(binding, dict) or not PROVENANCE_ID.fullmatch(str(binding.get("id", ""))):
        raise BridgeError(f"round {record.get('id')} has no valid production provenance binding")
    expected = round_provenance_binding(
        project,
        binding["id"],
        record["scope"]["designer_surface"],
        record["scope"]["entrypoints"],
    )
    if binding != expected:
        raise BridgeError(f"round {record.get('id')} provenance binding is stale or altered")
    return binding


def _transition_approvals(
    record: dict[str, Any],
    diff: dict[str, Any],
    after_capability_ids: set[str],
) -> list[dict[str, Any]]:
    required = set(diff["missing"]) | set(diff["changed"])
    if not required:
        return []
    approvals = _approved_round_capability_changes(record)
    missing_approval = sorted(required - set(approvals))
    if missing_approval:
        raise BridgeError(
            "post-adoption production lost or changed capabilities without exact owner rulings: "
            + ", ".join(missing_approval)
        )
    accepted: list[dict[str, Any]] = []
    for capability_id in sorted(required):
        approval = approvals[capability_id]
        change = approval.get("change")
        replacement = approval.get("replacement_id")
        if capability_id in diff["missing"] and change not in {"remove", "replace"}:
            raise BridgeError(f"missing capability {capability_id} requires a remove/replace ruling")
        if capability_id in diff["changed"] and change != "change":
            raise BridgeError(f"changed capability {capability_id} requires a change ruling")
        if change == "replace" and replacement not in after_capability_ids:
            raise BridgeError(
                f"approved replacement for {capability_id} is absent after adoption: {replacement}"
            )
        accepted.append(approval)
    return accepted


def post_adoption_provenance_binding(
    project: Path,
    record: dict[str, Any],
    provenance_id: str,
) -> dict[str, Any]:
    """Bind adopted production to a new verified identity and compare generations."""
    current = round_provenance_binding(
        project,
        provenance_id,
        record["scope"]["designer_surface"],
        record["scope"]["entrypoints"],
    )
    outbound_root, outbound_baseline, outbound_verification = _validate_stored_binding(
        project,
        record["provenance"],
        record["scope"]["designer_surface"],
        record["scope"]["entrypoints"],
        require_fresh=False,
    )
    current_root, current_baseline, current_verification = _validate_stored_binding(
        project,
        current,
        record["scope"]["designer_surface"],
        record["scope"]["entrypoints"],
        require_fresh=True,
    )
    if outbound_baseline["mode"] != current_baseline["mode"]:
        raise BridgeError(
            "post-adoption provenance cannot change the declared direct/adapter-seam mode"
        )
    if outbound_baseline["production_entrypoints"] != current_baseline["production_entrypoints"]:
        raise BridgeError(
            "post-adoption provenance must retain the authoritative production entrypoints; "
            f"outbound={outbound_baseline['production_entrypoints']}, "
            f"post_adoption={current_baseline['production_entrypoints']}"
        )
    outbound_manifest = read_json(outbound_root / "current-capabilities.json")
    current_manifest = read_json(current_root / "current-capabilities.json")
    diff = _manifest_diff(outbound_manifest, current_manifest)
    approvals = _transition_approvals(
        record,
        diff,
        set(current_manifest["capabilities"]),
    )
    outbound_compatibility = outbound_verification.get("compatibility_sha256", {})
    current_compatibility = current_verification.get("compatibility_sha256", {})
    if outbound_compatibility != current_compatibility:
        changed = sorted(set(outbound_compatibility) | set(current_compatibility))
        raise BridgeError(
            "post-adoption provenance changed compatibility records; those records are owner "
            "boundaries, not designer source: " + ", ".join(changed)
        )
    return {
        "binding": current,
        "transition": {
            "schema": "gpt-design-bridge/provenance-transition/v1",
            "from_provenance_id": record["provenance"]["id"],
            "to_provenance_id": current["id"],
            "capability_diff": diff,
            "approved_capability_changes": approvals,
            "compatibility_sha256": current_compatibility,
        },
    }


def validate_post_adoption_provenance(project: Path, record: dict[str, Any]) -> dict[str, Any]:
    integration = record.get("artifacts", {}).get("integration")
    if not isinstance(integration, dict):
        raise BridgeError("post-adoption rebuild requires recorded integration evidence")
    binding = integration.get("provenance")
    transition = integration.get("provenance_transition")
    if not isinstance(binding, dict) or not isinstance(transition, dict):
        raise BridgeError(
            "post-adoption rebuild requires a fresh provenance bound by adopt-integrate"
        )
    expected = post_adoption_provenance_binding(project, record, binding.get("id", ""))
    if binding != expected["binding"] or transition != expected["transition"]:
        raise BridgeError("post-adoption provenance binding or capability transition is stale")
    return binding


def public_preservation_baseline(
    project: Path,
    record: dict[str, Any],
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binding = validate_round_provenance(project, record) if binding is None else binding
    _validate_stored_binding(
        project,
        binding,
        record["scope"]["designer_surface"],
        record["scope"]["entrypoints"],
        require_fresh=True,
    )
    root, baseline, _declaration, verification = load_provenance(project, binding["id"])
    assert verification is not None
    manifest = read_json(root / "current-capabilities.json")
    transition = None
    integration = record.get("artifacts", {}).get("integration")
    if isinstance(integration, dict) and integration.get("provenance") == binding:
        transition = integration.get("provenance_transition")
    transition_approvals = (
        transition.get("approved_capability_changes", [])
        if isinstance(transition, dict)
        else []
    )
    return {
        "schema": "gpt-design-bridge/public-preservation-baseline/v1",
        "round_id": record["id"],
        "provenance_id": binding["id"],
        "mode": baseline["mode"],
        "production_entrypoints": baseline["production_entrypoints"],
        "travelling_entrypoints": baseline["travelling_entrypoints"],
        "designer_surface": baseline["designer_surface"],
        "build_graph_sha256": binding["build_graph_sha256"],
        "capability_manifest_sha256": binding["capability_manifest_sha256"],
        "capability_counts": manifest["counts"],
        "capability_total": manifest["total"],
        "previous_provenance_id": (
            transition.get("from_provenance_id") if isinstance(transition, dict) else None
        ),
        "approved_differences": [
            *transition_approvals,
            *verification["approved_capability_changes"],
        ],
        "law": "Fixture data may differ; interaction topology and unruled capabilities may not.",
    }


def _package_source_files(package: Path) -> list[str]:
    if not package.is_dir() or package.is_symlink():
        raise BridgeError(f"exact package root must be a real directory: {package}")
    result: list[str] = []
    for path in package.rglob("*"):
        if path.is_symlink():
            raise BridgeError(f"exact package contains a symbolic link: {path}")
        if path.is_file() and path.suffix.lower() in SOURCE_EXTENSIONS:
            result.append(path.relative_to(package).as_posix())
    return sorted(result)


def _package_tripwires(package: Path) -> dict[str, dict[str, Any]]:
    # Reuse the source scanner through a tiny path-compatible wrapper implementation.
    capabilities: dict[str, dict[str, Any]] = {}
    library_patterns = {
        "leaflet": re.compile(r"\b(?:L\.|leaflet)\b", re.I),
        "mapbox": re.compile(r"\bmapbox(?:gl)?\b", re.I),
        "openlayers": re.compile(r"\bol\.(?:Map|View|layer)\b|openlayers", re.I),
    }
    export_pattern = re.compile(r"\b(?:export|download)[^\n]{0,80}\b(csv|json|kml|xlsx|pdf)\b", re.I)
    storage_pattern = re.compile(r"\b(localStorage|sessionStorage)\.(?:getItem|setItem|removeItem)\(\s*['\"]([^'\"]+)", re.I)
    for relative in _package_source_files(package):
        try:
            text = (package / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BridgeError(f"cannot read exact package source {relative}: {exc}") from exc
        for name, pattern in library_patterns.items():
            if pattern.search(text):
                capability_id = f"source-library:{name}"
                capabilities[capability_id] = {"id": capability_id, "kind": "source-library", "label": name, "sources": sorted(set([*capabilities.get(capability_id, {}).get("sources", []), relative]))}
        for match in export_pattern.finditer(text):
            name = match.group(1).lower()
            capability_id = f"source-export:{name}"
            capabilities[capability_id] = {"id": capability_id, "kind": "source-export", "label": name, "sources": sorted(set([*capabilities.get(capability_id, {}).get("sources", []), relative]))}
        for match in storage_pattern.finditer(text):
            storage, key = match.groups()
            capability_id = _capability_id("source-storage", f"{storage.lower()}:{key}")
            capabilities[capability_id] = {"id": capability_id, "kind": "source-storage", "label": f"{storage}:{key}", "sources": sorted(set([*capabilities.get(capability_id, {}).get("sources", []), relative]))}
    return capabilities


def _binding_capabilities(
    path: Path,
    *,
    round_id: str,
    provenance_id: str,
    required_ids: set[str],
    proofs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    data = read_json(path)
    expected = {"schema", "round_id", "provenance_id", "capabilities"}
    if set(data) != expected or data.get("schema") != PARITY_BINDINGS_SCHEMA:
        raise BridgeError("parity bindings must use the strict v1 schema")
    if data.get("round_id") != round_id or data.get("provenance_id") != provenance_id:
        raise BridgeError("parity bindings are not bound to the active round and provenance")
    values = data["capabilities"]
    if not isinstance(values, list):
        raise BridgeError("parity binding capabilities must be an array")
    ids = {item.get("id") for item in values if isinstance(item, dict)}
    if ids != required_ids:
        raise BridgeError(
            f"parity bindings must cover every declared workflow exactly; required={sorted(required_ids)}, declared={sorted(ids)}"
        )
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        if set(item) != {"id", "evidence"} or not isinstance(item["evidence"], list) or not item["evidence"]:
            raise BridgeError(f"invalid parity binding for {item.get('id')}")
        normalized: list[dict[str, Any]] = []
        for evidence in item["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) != {"state", "action_indexes", "selectors"}:
                raise BridgeError(f"invalid parity evidence for {item['id']}")
            state = evidence["state"]
            if state not in proofs or proofs[state].get("not_applicable"):
                raise BridgeError(f"parity capability {item['id']} cites an absent state proof: {state}")
            actions = proofs[state]["proof"]["actions"]
            indexes = evidence["action_indexes"]
            selectors = evidence["selectors"]
            if not isinstance(indexes, list) or not indexes or any(
                type(index) is not int or index < 0 or index >= len(actions) or actions[index].get("status") != "pass"
                for index in indexes
            ):
                raise BridgeError(f"parity capability {item['id']} cites invalid action indexes")
            if not isinstance(selectors, list) or not selectors or any(
                not isinstance(selector, str) or not selector.strip() for selector in selectors
            ):
                raise BridgeError(f"parity capability {item['id']} requires selector identities")
            dom = proofs[state]["dom"].read_text(encoding="utf-8")
            if any(selector not in dom for selector in selectors):
                raise BridgeError(f"parity capability {item['id']} selector identity is absent from {state} DOM")
            normalized.append({
                "state": state,
                "action_indexes": indexes,
                "selectors": selectors,
                "proof_sha256": proofs[state]["proof_sha256"],
            })
        result[item["id"]] = {"id": item["id"], "evidence": normalized}
    return result


def parity_bindings_template(project: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Generate the exact explicit-workflow inventory the candidate proof must bind."""
    binding = validate_round_provenance(project, record)
    root, _baseline, _declaration, _verification = load_provenance(project, binding["id"])
    manifest = read_json(root / "current-capabilities.json")
    explicit = [
        capability_id
        for capability_id, capability in manifest["capabilities"].items()
        if capability.get("source")
    ]
    return {
        "schema": PARITY_BINDINGS_SCHEMA,
        "round_id": record["id"],
        "provenance_id": binding["id"],
        "capabilities": [
            {
                "id": capability_id,
                "evidence": [
                    {
                        "state": "normal",
                        "action_indexes": [0],
                        "selectors": ["replace-with-stable-DOM-selector-identity"],
                    }
                ],
            }
            for capability_id in explicit
        ],
    }


def _approved_round_capability_changes(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for ruling in record.get("owner_rulings", []):
        change = ruling.get("capability_change")
        if isinstance(change, dict):
            result[change.get("capability_id", "")] = change
    return result


def build_parity_report(
    project: Path,
    package: Path,
    proof_paths: dict[str, Path],
    bindings_path: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    _config = None
    from bridge_core import load_project
    _config, state = load_project(project)
    active = state.get("active_round")
    if not active:
        raise BridgeError("capability parity requires an active designer round")
    record = load_round(project, active["id"])
    binding = validate_round_provenance(project, record)
    root, _baseline, declaration, verification = load_provenance(project, binding["id"])
    assert verification is not None
    candidate_root = project / candidate.get("root", "")
    archive = candidate_root / candidate.get("archive_name", "")
    if not archive.is_file() or sha256_file(archive) != candidate.get("archive_sha256"):
        raise BridgeError("parity candidate archive is absent or changed")
    # The exact extracted package must match the tree recorded before archive creation.
    from bridge_artifacts import build_tree_manifest
    package_tree = build_tree_manifest(package)
    if package_tree["tree_sha256"] != candidate.get("package_tree_sha256"):
        raise BridgeError(
            "parity proof package is not the exact candidate tree; "
            f"expected={candidate.get('package_tree_sha256')}, found={package_tree['tree_sha256']}"
        )
    proofs = _proof_map(proof_paths, declaration["state_coverage"])
    current_manifest = read_json(root / "current-capabilities.json")
    declared_ids = {
        capability_id
        for capability_id, capability in current_manifest["capabilities"].items()
        if capability.get("source")
    }
    bound = _binding_capabilities(
        bindings_path,
        round_id=record["id"],
        provenance_id=binding["id"],
        required_ids=declared_ids,
        proofs=proofs,
    )
    travelling_capabilities = _extract_dom_capabilities(proofs)
    travelling_capabilities.update(_package_tripwires(package))
    for capability_id, evidence in bound.items():
        source = current_manifest["capabilities"][capability_id]
        travelling_capabilities[capability_id] = {
            "id": capability_id,
            "kind": source["kind"],
            "label": source["label"],
            "evidence": evidence["evidence"],
        }
    travelling_manifest = {
        "schema": "gpt-design-bridge/travelling-capability-manifest/v1",
        "capabilities": dict(sorted(travelling_capabilities.items())),
        "counts": dict(sorted(Counter(value["kind"] for value in travelling_capabilities.values()).items())),
        "total": len(travelling_capabilities),
        "state_coverage": {
            state: ({"status": "not-applicable"} if value.get("not_applicable") else {"status": "proof", "proof_sha256": value["proof_sha256"]})
            for state, value in proofs.items()
        },
    }
    travelling_manifest["sha256"] = hashlib.sha256(canonical_json(travelling_manifest)).hexdigest()
    diff = _manifest_diff(current_manifest, travelling_manifest)
    rulings = _approved_round_capability_changes(record)
    unexplained = sorted((set(diff["missing"]) | set(diff["changed"])) - set(rulings))
    stale_rulings = sorted(set(rulings) - (set(diff["missing"]) | set(diff["changed"])))
    invalid_rulings: list[str] = []
    for capability_id, ruling in rulings.items():
        if capability_id in diff["missing"] and ruling.get("change") not in {"remove", "replace"}:
            invalid_rulings.append(f"{capability_id}: missing capability requires remove or replace")
        if capability_id in diff["changed"] and ruling.get("change") != "change":
            invalid_rulings.append(f"{capability_id}: changed capability requires change")
        if ruling.get("change") == "replace" and ruling.get("replacement_id") not in travelling_capabilities:
            invalid_rulings.append(
                f"{capability_id}: replacement {ruling.get('replacement_id')!r} is absent"
            )
    if unexplained or stale_rulings or invalid_rulings:
        raise BridgeError(
            "production-to-travelling capability parity failed; "
            f"unexplained={unexplained}, stale_owner_rulings={stale_rulings}, "
            f"invalid_owner_rulings={invalid_rulings}"
        )
    report = {
        "schema": PARITY_SCHEMA,
        "status": "pass",
        "round_id": record["id"],
        "provenance_id": binding["id"],
        "guarded_tree": binding["guarded_tree"],
        "verification_sha256": binding["verification_sha256"],
        "candidate": {
            "candidate_id": candidate["candidate_id"],
            "build_stamp": candidate["build_stamp"],
            "archive_sha256": candidate["archive_sha256"],
            "package_tree_sha256": candidate["package_tree_sha256"],
        },
        "production_manifest_sha256": current_manifest["sha256"],
        "travelling_manifest": travelling_manifest,
        "diff": diff,
        "approved_differences": [rulings[key] for key in sorted(rulings)],
        "bindings_sha256": sha256_file(bindings_path),
        "proof_sha256": {state: value["proof_sha256"] for state, value in proofs.items() if not value.get("not_applicable")},
    }
    report["report_sha256"] = hashlib.sha256(canonical_json(report)).hexdigest()
    return report


def write_parity_report(
    project: Path,
    package: Path,
    proof_paths: dict[str, Path],
    bindings_path: Path,
    candidate: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    report = build_parity_report(project, package, proof_paths, bindings_path, candidate)
    resolved = output.resolve() if output.is_absolute() else (project / output).resolve()
    evidence_root = (kit_root(project) / "evidence").resolve()
    try:
        resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise BridgeError("parity report must be written under .gpt-design-bridge/evidence/") from exc
    if resolved.exists() or resolved.is_symlink():
        raise BridgeError(f"parity report already exists: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(resolved, report)
    return {
        "path": resolved.relative_to(project.resolve()).as_posix(),
        "sha256": sha256_file(resolved),
        "report": report,
    }


def validate_parity_reference(
    project: Path,
    record: dict[str, Any],
    candidate: dict[str, Any],
    reference: Any,
) -> dict[str, Any]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise BridgeError("browser proof must reference one exact capability parity report")
    relative = reference.get("path", "")
    if (
        not isinstance(relative, str)
        or "\\" in relative
        or not relative.startswith(".gpt-design-bridge/evidence/")
        or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
    ):
        raise BridgeError("capability parity report must live under bridge evidence")
    path = project / relative
    if not path.is_file() or path.is_symlink() or sha256_file(path) != reference.get("sha256"):
        raise BridgeError("capability parity report is missing or its bytes changed")
    report = read_json(path)
    binding = validate_round_provenance(project, record)
    expected_candidate = {
        "candidate_id": candidate["candidate_id"],
        "build_stamp": candidate["build_stamp"],
        "archive_sha256": candidate["archive_sha256"],
        "package_tree_sha256": candidate["package_tree_sha256"],
    }
    if (
        report.get("schema") != PARITY_SCHEMA
        or report.get("status") != "pass"
        or report.get("round_id") != record["id"]
        or report.get("provenance_id") != binding["id"]
        or report.get("guarded_tree") != binding["guarded_tree"]
        or report.get("verification_sha256") != binding["verification_sha256"]
        or report.get("candidate") != expected_candidate
    ):
        raise BridgeError("capability parity report is stale or bound to another product artifact")
    embedded_hash = report.pop("report_sha256", None)
    computed = hashlib.sha256(canonical_json(report)).hexdigest()
    report["report_sha256"] = embedded_hash
    if embedded_hash != computed:
        raise BridgeError("capability parity report's embedded identity is invalid")
    return report
