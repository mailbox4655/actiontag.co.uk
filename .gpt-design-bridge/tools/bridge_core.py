"""Strict project state and filesystem primitives for GPT Design Bridge."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator


KIT_VERSION = "0.1.0"
PROJECT_SCHEMA = "gpt-design-bridge/project/v1"
STATE_SCHEMA = "gpt-design-bridge/state/v1"
ROUND_SCHEMA = "gpt-design-bridge/round/v1"
KIT_DIRNAME = ".gpt-design-bridge"
PHASES = {
    "initialized",
    "building",
    "outbound_open",
    "awaiting_return",
    "return_received",
    "adopting",
    "proving",
    "sealed",
}
DATABASE_ENGINES = {"sqlite", "postgresql"}
ROUND_ID_PATTERN = re.compile(r"^[0-9]{3,}$")
ACTIVE_ROUND_STATUSES = {
    "outbound_open",
    "awaiting_return",
    "return_received",
    "adopting",
    "proving",
}
OPENABLE_PHASES = {"initialized", "building", "sealed"}


class BridgeError(RuntimeError):
    """A user-actionable refusal. Never downgrade this to a fallback."""


def _environment_positive_number(name: str, default: float, *, integer: bool) -> int | float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default) if integer else float(default)
    try:
        value = int(raw) if integer else float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


WINDOWS_DIRECTORY_PROMOTION_ATTEMPTS = int(
    _environment_positive_number("GDB_WINDOWS_PROMOTION_ATTEMPTS", 121, integer=True)
)
WINDOWS_DIRECTORY_PROMOTION_DELAY_SECONDS = float(
    _environment_positive_number("GDB_WINDOWS_PROMOTION_DELAY_SECONDS", 0.25, integer=False)
)
WINDOWS_DIRECTORY_MOVE_COMMAND = (
    "$ErrorActionPreference = 'Stop'; "
    "Move-Item -LiteralPath $env:GDB_PROMOTION_SOURCE "
    "-Destination $env:GDB_PROMOTION_DESTINATION"
)


def _windows_move_directory(source: Path, destination: Path) -> None:
    environment = os.environ.copy()
    environment["GDB_PROMOTION_SOURCE"] = str(source)
    environment["GDB_PROMOTION_DESTINATION"] = str(destination)
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                WINDOWS_DIRECTORY_MOVE_COMMAND,
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise PermissionError("native Windows directory promotion is unavailable") from exc
    if completed.returncode:
        raise PermissionError(
            "native Windows directory promotion failed "
            f"with exit code {completed.returncode}"
        )


def promote_directory(
    source: Path,
    destination: Path,
    *,
    replace=None,
    sleep=time.sleep,
    windows: bool = os.name == "nt",
    attempts: int = WINDOWS_DIRECTORY_PROMOTION_ATTEMPTS,
    delay_seconds: float = WINDOWS_DIRECTORY_PROMOTION_DELAY_SECONDS,
) -> None:
    incoming = source.resolve()
    outgoing = destination.resolve()
    if not incoming.is_dir() or source.is_symlink():
        raise BridgeError(f"directory promotion source is missing or symbolic: {source}")
    if outgoing.exists():
        raise BridgeError(f"directory promotion destination already exists: {destination}")
    if incoming == outgoing:
        raise BridgeError("directory promotion source and destination must differ")
    if windows and incoming.anchor.casefold() != outgoing.anchor.casefold():
        raise BridgeError("Windows directory promotion must remain on one volume")
    if not isinstance(attempts, int) or attempts < 1:
        raise BridgeError("directory promotion attempts must be a positive integer")
    promote = replace or (_windows_move_directory if windows else os.replace)
    for attempt in range(attempts):
        try:
            promote(incoming, outgoing)
            break
        except PermissionError:
            if not windows or attempt + 1 == attempts:
                raise
            if attempt == 3 or (attempt + 1) % 20 == 0:
                print(
                    "[GPT Design Bridge] Windows is still holding the directory; "
                    f"promotion retry {attempt + 1}/{attempts} after "
                    f"{(attempt + 1) * delay_seconds:.2f}s",
                    file=sys.stderr,
                    flush=True,
                )
            sleep(delay_seconds)
    if incoming.exists() or not outgoing.is_dir() or outgoing.is_symlink():
        raise BridgeError("directory promotion did not produce the exact destination")


def run_git(directory: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(directory), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise BridgeError(f"git {' '.join(args)} failed in {directory}: {detail}")
    return completed


def resolve_git_root(candidate: Path) -> Path:
    target = candidate.resolve()
    if not target.is_dir():
        raise BridgeError(f"project target is not a directory: {target}")
    completed = run_git(target, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode != 0:
        raise BridgeError(
            f"project target is not inside a Git repository: {target}. "
            "Git initialization requires explicit owner authorization."
        )
    root = Path(completed.stdout.strip()).resolve()
    if root != target:
        raise BridgeError(f"initialize at the Git root {root}, not nested path {target}")
    return root


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        raise BridgeError(f"project name cannot produce a safe slug: {value!r}")
    if len(slug) > 63:
        raise BridgeError(f"project slug exceeds 63 characters: {slug}")
    return slug


def validate_slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise BridgeError(
            "slug must be 1-63 lowercase letters, digits, or internal hyphens"
        )
    return value


def safe_relative_path(value: str, *, label: str = "path") -> str:
    if not isinstance(value, str) or not value:
        raise BridgeError(f"{label} must be a non-empty relative POSIX path")
    if "\x00" in value or "\\" in value:
        raise BridgeError(f"{label} must not contain NUL or backslashes: {value!r}")
    if re.match(r"^[A-Za-z]:", value):
        raise BridgeError(f"{label} must not be drive-qualified: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or value.endswith("/")
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise BridgeError(f"{label} must be a normalized relative POSIX path: {value!r}")
    if any(any(ord(character) < 32 for character in part) for part in path.parts):
        raise BridgeError(f"{label} must not contain control characters: {value!r}")
    if path.parts[0] in {".git", KIT_DIRNAME}:
        raise BridgeError(f"{label} is reserved and cannot enter the designer surface: {value!r}")
    return path.as_posix()


def non_empty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{label} must be a non-empty string")
    return value.strip()


def validate_iso_date(value: str, label: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError(f"{label} must be an ISO date in YYYY-MM-DD form") from exc
    if parsed.isoformat() != value:
        raise BridgeError(f"{label} must be an ISO date in YYYY-MM-DD form")
    return value


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BridgeError(f"required JSON file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"cannot read valid UTF-8 JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BridgeError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tool_sources(source_dir: Path) -> list[Path]:
    expected_main = source_dir / "gpt_design_bridge.py"
    lite_scripts = (
        source_dir.parent.parent
        / "gpt-blackbox-lite"
        / "scripts"
    )
    required_lite = [
        lite_scripts / name
        for name in (
            "gpt_blackbox.py",
            "artifact_lifecycle.py",
            "controlled_chrome.mjs",
            "controlled_chrome_actions.mjs",
            "controlled_chrome_cdp.mjs",
            "controlled_chrome_install.mjs",
        )
    ]
    sources = [expected_main, *sorted(source_dir.glob("bridge_*.py")), *required_lite]
    unique = list(dict.fromkeys(path.resolve() for path in sources if path.is_file()))
    if expected_main.resolve() not in unique:
        raise BridgeError(f"initializer entry point is missing: {expected_main}")
    if not any(path.name == "bridge_core.py" for path in unique):
        raise BridgeError(f"initializer core is missing beside {expected_main}")
    missing_lite = [path for path in required_lite if path.resolve() not in unique]
    if missing_lite:
        raise BridgeError(
            "vendored BlackBox/owned-Chrome/artifact tools are missing: "
            + ", ".join(str(path) for path in missing_lite)
        )
    return unique


def copy_tools(source_dir: Path, kit_root: Path) -> dict[str, dict[str, Any]]:
    destination = kit_root / "tools"
    destination.mkdir(parents=True)
    manifest: dict[str, dict[str, Any]] = {}
    for source in tool_sources(source_dir):
        target = destination / source.name
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        relative = target.relative_to(kit_root).as_posix()
        manifest[relative] = {"bytes": target.stat().st_size, "sha256": sha256_file(target)}
    return manifest


def copy_assets(asset_root: Path, kit_root: Path) -> dict[str, dict[str, Any]]:
    if not asset_root.is_dir():
        raise BridgeError(f"project-kit assets are missing: {asset_root}")
    sources = sorted(asset_root.rglob("*"), key=lambda path: path.as_posix())
    if any(path.is_symlink() for path in sources):
        offending = next(path for path in sources if path.is_symlink())
        raise BridgeError(f"project-kit assets may not contain symbolic links: {offending}")
    files = [path for path in sources if path.is_file()]
    if not files:
        raise BridgeError(f"project-kit assets contain no files: {asset_root}")
    manifest: dict[str, dict[str, Any]] = {}
    for source in files:
        relative = source.relative_to(asset_root)
        target = kit_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        key = relative.as_posix()
        manifest[key] = {"bytes": target.stat().st_size, "sha256": sha256_file(target)}
    return manifest


def validate_project(config: dict[str, Any], path: Path) -> None:
    if config.get("schema") != PROJECT_SCHEMA:
        raise BridgeError(f"unsupported project schema in {path}: {config.get('schema')!r}")
    project = config.get("project")
    if (
        not isinstance(project, dict)
        or not isinstance(project.get("name"), str)
        or not project["name"].strip()
    ):
        raise BridgeError(f"project.name must be a non-empty string in {path}")
    validate_slug(project.get("slug", ""))
    database = config.get("database")
    if not isinstance(database, dict) or database.get("engine") not in DATABASE_ENGINES:
        raise BridgeError(f"database.engine must be sqlite or postgresql in {path}")
    if database["engine"] == "sqlite":
        if database.get("journal_mode") != "DELETE":
            raise BridgeError("SQLite starts in DELETE journal mode until WAL safety is proven")
        if database.get("network_filesystem") is not False:
            raise BridgeError("SQLite network_filesystem must be false")
    deployment = config.get("deployment")
    if not isinstance(deployment, dict):
        raise BridgeError(f"deployment must be an object in {path}")
    required_deployment = {
        "runtime": "systemd",
        "containerization": "prohibited",
        "hosting": "hetzner-vps",
        "dns": "cloudflare",
        "system_mail": "postmark",
    }
    for key, expected in required_deployment.items():
        if deployment.get(key) != expected:
            raise BridgeError(f"deployment.{key} must be {expected!r}")
    reverse_proxy = deployment.get("reverse_proxy")
    if reverse_proxy not in {"host-selected-caddy-or-nginx", "caddy"}:
        raise BridgeError(
            "deployment.reverse_proxy must be 'host-selected-caddy-or-nginx'; "
            "legacy project records may retain 'caddy'"
        )
    hostname = deployment.get("hostname")
    if hostname is not None and (not isinstance(hostname, str) or not hostname.strip()):
        raise BridgeError(f"deployment.hostname must be null or a non-empty string in {path}")
    port = deployment.get("application_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise BridgeError(f"deployment.application_port must be an integer from 1 to 65535")
    for manifest_name in ("tool_manifest", "foundation_manifest"):
        manifest = config.get(manifest_name)
        if not isinstance(manifest, dict) or not manifest:
            raise BridgeError(f"{manifest_name} must be a non-empty object in {path}")
        for relative, expected in manifest.items():
            if (
                not isinstance(relative, str)
                or not relative
                or not isinstance(expected, dict)
                or not isinstance(expected.get("bytes"), int)
                or not isinstance(expected.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])
            ):
                raise BridgeError(f"invalid {manifest_name} entry for {relative!r} in {path}")


def validate_state(state: dict[str, Any], path: Path) -> None:
    if state.get("schema") != STATE_SCHEMA:
        raise BridgeError(f"unsupported state schema in {path}: {state.get('schema')!r}")
    if state.get("phase") not in PHASES:
        raise BridgeError(f"unknown lifecycle phase in {path}: {state.get('phase')!r}")
    if not isinstance(state.get("generation"), int) or state["generation"] < 1:
        raise BridgeError(f"state generation must be a positive integer in {path}")
    active_round = state.get("active_round")
    if state.get("phase") in ACTIVE_ROUND_STATUSES and active_round is None:
        raise BridgeError(f"active lifecycle phase requires active_round in {path}")
    if active_round is not None:
        if not isinstance(active_round, dict):
            raise BridgeError(f"active_round must be null or an object in {path}")
        if not ROUND_ID_PATTERN.fullmatch(active_round.get("id", "")):
            raise BridgeError(f"active_round.id must be a zero-padded numeric ID in {path}")
        if active_round.get("status") not in ACTIVE_ROUND_STATUSES:
            raise BridgeError(f"active_round.status is invalid in {path}: {active_round.get('status')!r}")
        if state.get("phase") != active_round["status"]:
            raise BridgeError(f"active_round.status must equal state.phase in {path}")
    sealed_rounds = state.get("sealed_rounds")
    if not isinstance(sealed_rounds, list):
        raise BridgeError(f"sealed_rounds must be an array in {path}")
    if any(not isinstance(item, str) or not ROUND_ID_PATTERN.fullmatch(item) for item in sealed_rounds):
        raise BridgeError(f"sealed_rounds must contain only zero-padded numeric IDs in {path}")
    if len(set(sealed_rounds)) != len(sealed_rounds):
        raise BridgeError(f"sealed_rounds contains duplicate IDs in {path}")
    deferred = state.get("deferred_obligations")
    if not isinstance(deferred, list):
        raise BridgeError(f"deferred_obligations must be an array in {path}")
    seen_deferrals: set[str] = set()
    for item in deferred:
        if not isinstance(item, dict):
            raise BridgeError(f"every deferred obligation must be an object in {path}")
        deferral_id = item.get("id")
        if not isinstance(deferral_id, str) or not re.fullmatch(r"D-[0-9]{3,}", deferral_id):
            raise BridgeError(f"deferred obligation ID is invalid in {path}: {deferral_id!r}")
        if deferral_id in seen_deferrals:
            raise BridgeError(f"duplicate deferred obligation ID in {path}: {deferral_id}")
        seen_deferrals.add(deferral_id)
        if item.get("status") not in {"open", "discharged"}:
            raise BridgeError(f"deferred obligation status is invalid in {path}: {deferral_id}")
        for key in ("obligation", "reason", "discharge_gate"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise BridgeError(f"deferred obligation {deferral_id}.{key} must be non-empty")
        if item["status"] == "discharged" and (
            not isinstance(item.get("evidence"), str) or not item["evidence"].strip()
        ):
            raise BridgeError(f"discharged obligation {deferral_id} requires evidence")
        round_id = item.get("round_id")
        if round_id is not None and (
            not isinstance(round_id, str) or not ROUND_ID_PATTERN.fullmatch(round_id)
        ):
            raise BridgeError(f"deferred obligation {deferral_id}.round_id is invalid")
        if not isinstance(item.get("created_generation"), int) or item["created_generation"] < 2:
            raise BridgeError(f"deferred obligation {deferral_id} has invalid generation")


def records_root(project_root: Path) -> Path:
    return kit_root(project_root) / "records" / "rounds"


def round_root(project_root: Path, round_id: str) -> Path:
    if not ROUND_ID_PATTERN.fullmatch(round_id):
        raise BridgeError(f"round ID must be zero-padded digits: {round_id!r}")
    return records_root(project_root) / round_id


def round_record_path(project_root: Path, round_id: str) -> Path:
    return round_root(project_root, round_id) / "round.json"


def round_ids(project_root: Path) -> list[str]:
    root = records_root(project_root)
    ids: list[str] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name == ".gitkeep":
            continue
        if not path.is_dir() or not ROUND_ID_PATTERN.fullmatch(path.name):
            raise BridgeError(f"unexpected entry in round records: {path}")
        ids.append(path.name)
    return ids


def validate_round(record: dict[str, Any], path: Path) -> None:
    if record.get("schema") != ROUND_SCHEMA:
        raise BridgeError(f"unsupported round schema in {path}: {record.get('schema')!r}")
    round_id = record.get("id")
    if not isinstance(round_id, str) or not ROUND_ID_PATTERN.fullmatch(round_id):
        raise BridgeError(f"round.id must be zero-padded digits in {path}")
    if record.get("status") not in {*ACTIVE_ROUND_STATUSES, "sealed", "abandoned"}:
        raise BridgeError(f"round.status is invalid in {path}: {record.get('status')!r}")
    if record.get("opened_from_phase") not in OPENABLE_PHASES:
        raise BridgeError(f"round.opened_from_phase is invalid in {path}")
    non_empty(record.get("goal"), f"{path}: goal")
    non_empty(record.get("owner_request"), f"{path}: owner_request")
    provenance = record.get("provenance")
    if provenance is not None:
        required_provenance = {
            "id",
            "mode",
            "baseline_sha256",
            "verification_sha256",
            "guarded_tree",
            "build_graph_sha256",
            "capability_manifest_sha256",
        }
        if not isinstance(provenance, dict) or set(provenance) != required_provenance:
            raise BridgeError(f"round.provenance must use the strict binding schema in {path}")
        if not re.fullmatch(r"P-[0-9]{3,}", provenance.get("id", "")):
            raise BridgeError(f"round.provenance.id is invalid in {path}")
        if provenance.get("mode") not in {"direct", "adapter-seam"}:
            raise BridgeError(f"round.provenance.mode is invalid in {path}")
        for key in required_provenance - {"id", "mode"}:
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", provenance.get(key, "")):
                raise BridgeError(f"round.provenance.{key} is not a Git/SHA-256 identity in {path}")
    scope = record.get("scope")
    if not isinstance(scope, dict):
        raise BridgeError(f"round.scope must be an object in {path}")
    for key in ("designer_surface", "entrypoints"):
        values = scope.get(key)
        if not isinstance(values, list) or not values:
            raise BridgeError(f"round.scope.{key} must be a non-empty array in {path}")
        normalized = [safe_relative_path(value, label=f"round.scope.{key}") for value in values]
        if normalized != values or len(set(values)) != len(values):
            raise BridgeError(f"round.scope.{key} must contain unique normalized paths in {path}")
    prefixes = scope.get("route_prefixes")
    if not isinstance(prefixes, list) or not prefixes:
        raise BridgeError(f"round.scope.route_prefixes must be a non-empty array in {path}")
    if any(
        not isinstance(prefix, str)
        or not prefix.startswith("/")
        or prefix.startswith("//")
        or any(marker in prefix for marker in ("\\", "?", "#"))
        for prefix in prefixes
    ):
        raise BridgeError(f"round.scope.route_prefixes contains an invalid prefix in {path}")
    screens = scope.get("screens")
    if screens is not None:
        if not isinstance(screens, list) or len(screens) != len(scope["entrypoints"]):
            raise BridgeError(
                f"round.scope.screens must declare every entrypoint exactly once in {path}"
            )
        screen_entries: list[str] = []
        screen_ids: list[str] = []
        for screen in screens:
            if not isinstance(screen, dict) or set(screen) != {"id", "entrypoint", "mode"}:
                raise BridgeError(f"round.scope.screens has an invalid entry in {path}")
            screen_id = safe_relative_path(screen.get("id", ""), label="round screen ID")
            entrypoint = safe_relative_path(
                screen.get("entrypoint", ""), label="round screen entrypoint"
            )
            if screen.get("mode") not in {"exact", "characterized", "reference"}:
                raise BridgeError(f"round screen adoption mode is invalid in {path}: {screen_id}")
            screen_ids.append(screen_id)
            screen_entries.append(entrypoint)
        if len(set(screen_ids)) != len(screen_ids):
            raise BridgeError(f"round.scope.screens contains duplicate IDs in {path}")
        if screen_entries != scope["entrypoints"]:
            raise BridgeError(
                f"round.scope.screens must preserve entrypoint order and coverage in {path}"
            )
    rulings = record.get("owner_rulings")
    if not isinstance(rulings, list):
        raise BridgeError(f"round.owner_rulings must be an array in {path}")
    ruling_ids: set[str] = set()
    for ruling in rulings:
        if not isinstance(ruling, dict) or not re.fullmatch(r"R-[0-9]{3,}", ruling.get("id", "")):
            raise BridgeError(f"invalid owner ruling in {path}")
        if ruling["id"] in ruling_ids:
            raise BridgeError(f"duplicate owner ruling {ruling['id']} in {path}")
        ruling_ids.add(ruling["id"])
        non_empty(ruling.get("question"), f"{path}: ruling question")
        non_empty(ruling.get("decision"), f"{path}: ruling decision")
        validate_iso_date(ruling.get("date"), f"{path}: ruling date")
        if ruling.get("by") != "owner":
            raise BridgeError(f"owner ruling {ruling['id']} must have by='owner' in {path}")
        capability_change = ruling.get("capability_change")
        if capability_change is not None:
            required_change = {"capability_id", "change", "replacement_id"}
            if not isinstance(capability_change, dict) or set(capability_change) != required_change:
                raise BridgeError(f"owner ruling {ruling['id']} has an invalid capability change")
            if not isinstance(capability_change.get("capability_id"), str) or not capability_change["capability_id"]:
                raise BridgeError(f"owner ruling {ruling['id']} capability ID is empty")
            if capability_change.get("change") not in {"remove", "replace", "change"}:
                raise BridgeError(f"owner ruling {ruling['id']} capability change is invalid")
            replacement = capability_change.get("replacement_id")
            if capability_change["change"] == "replace":
                if not isinstance(replacement, str) or not replacement:
                    raise BridgeError(f"owner ruling {ruling['id']} replacement ID is empty")
            elif replacement is not None:
                raise BridgeError(f"owner ruling {ruling['id']} must use null replacement ID")
    events = record.get("events")
    if not isinstance(events, list) or not events:
        raise BridgeError(f"round.events must be a non-empty array in {path}")
    generations = [event.get("generation") for event in events if isinstance(event, dict)]
    if (
        len(generations) != len(events)
        or any(not isinstance(item, int) or item < 2 for item in generations)
        or generations != sorted(set(generations))
        or any(not isinstance(event.get("event"), str) or not event["event"] for event in events)
    ):
        raise BridgeError(f"round events are invalid or unordered in {path}")
    if not isinstance(record.get("artifacts"), dict):
        raise BridgeError(f"round.artifacts must be an object in {path}")


def kit_root(project_root: Path) -> Path:
    return project_root / KIT_DIRNAME


def load_project(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = kit_root(project_root)
    config_path = root / "project.json"
    state_path = root / "state.json"
    config = read_json(config_path)
    state = read_json(state_path)
    validate_project(config, config_path)
    validate_state(state, state_path)
    return config, state


def load_round(project_root: Path, round_id: str) -> dict[str, Any]:
    path = round_record_path(project_root, round_id)
    value = read_json(path)
    validate_round(value, path)
    if value["id"] != round_id:
        raise BridgeError(f"round directory/record ID mismatch: {round_id} != {value['id']}")
    return value


@contextlib.contextmanager
def exclusive_lock(path: Path, operation: str) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json({"operation": operation, "pid": os.getpid()})
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        try:
            owner = path.read_text(encoding="utf-8").strip()
        except OSError:
            owner = "<unreadable>"
        raise BridgeError(f"exclusive lifecycle lock already exists: {path}; owner={owner}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        path.unlink(missing_ok=True)


def verify_manifest(
    project_root: Path, manifest: dict[str, Any], label: str
) -> list[str]:
    root = kit_root(project_root)
    errors: list[str] = []
    for relative, expected in sorted(manifest.items()):
        path = root / relative
        if not path.is_file():
            errors.append(f"missing {label}: {relative}")
            continue
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_size != expected.get("bytes"):
            errors.append(
                f"{label} byte-count mismatch: {relative}: expected {expected.get('bytes')}, "
                f"found {actual_size}"
            )
        if actual_hash != expected.get("sha256"):
            errors.append(
                f"{label} hash mismatch: {relative}: expected {expected.get('sha256')}, "
                f"found {actual_hash}"
            )
    return errors


def verify_tool_manifest(project_root: Path, config: dict[str, Any]) -> list[str]:
    """Preserved public wrapper for callers that verify only project-local tools."""
    return verify_manifest(project_root, config["tool_manifest"], "project-local tool")


def check_project(project_root: Path) -> list[str]:
    config, _state = load_project(project_root)
    root = kit_root(project_root)
    errors: list[str] = []
    for relative in (
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
        "tools",
    ):
        if not (root / relative).is_dir():
            errors.append(f"required directory is missing: {relative}")
    active_lock = root / "runtime" / "mutation.lock"
    if active_lock.exists():
        errors.append(f"lifecycle mutation lock is still present: {active_lock}")
    errors.extend(verify_tool_manifest(project_root, config))
    errors.extend(verify_manifest(project_root, config["foundation_manifest"], "foundation asset"))
    errors.extend(check_round_integrity(project_root))
    return errors


def check_round_integrity(project_root: Path) -> list[str]:
    errors: list[str] = []
    try:
        _config, state = load_project(project_root)
        ids = round_ids(project_root)
    except BridgeError as exc:
        return [str(exc)]
    records: dict[str, dict[str, Any]] = {}
    for round_id in ids:
        try:
            records[round_id] = load_round(project_root, round_id)
        except BridgeError as exc:
            errors.append(str(exc))
    active = state["active_round"]
    if active:
        record = records.get(active["id"])
        if record is None:
            errors.append(f"active round record is missing: {active['id']}")
        elif record["status"] != active["status"]:
            errors.append(
                f"active round status mismatch: state={active['status']}, record={record['status']}"
            )
    for round_id in state["sealed_rounds"]:
        record = records.get(round_id)
        if record is None:
            errors.append(f"sealed round record is missing: {round_id}")
        elif record["status"] != "sealed":
            errors.append(f"sealed round {round_id} has record status {record['status']!r}")
    referenced = set(state["sealed_rounds"])
    if active:
        referenced.add(active["id"])
    for round_id, record in records.items():
        if round_id not in referenced and record["status"] != "abandoned":
            errors.append(f"orphan round {round_id} has non-terminal status {record['status']!r}")
    return errors
