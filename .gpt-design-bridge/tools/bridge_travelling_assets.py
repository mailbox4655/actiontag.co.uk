"""Measured used-assets plus limited picker samples for a travelling stage."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from bridge_core import BridgeError, canonical_json, read_json, safe_relative_path

SCHEMA = "gpt-design-bridge/travelling-assets/v1"
CONFIG_SCHEMA = "gpt-design-bridge/designer-assets/v1"
PICKER_ROOT = Path("public/assets/pickers/icon-picker")
PACKAGE_PICKER = Path("assets/pickers/icon-picker")
LUCIDE_SAMPLE = (
    "calendar", "check", "circle", "circle-alert", "clock", "copy", "download",
    "file", "folder", "globe", "heart", "home", "image", "link", "list-checks",
    "loader-circle", "mail", "map-pin", "pencil", "plus", "refresh-cw", "save",
    "search", "settings", "star", "trash-2", "upload", "user", "users", "phone",
)
TWEMOJI_SAMPLE = (
    "🧮", "🧪", "🎨", "🛠️", "🧭", "🧩", "🧰", "🧱", "🪄", "🎯",
    "🧵", "🧶", "🪡", "🧷", "🪛", "🔧", "🔨", "⚙️", "🗜️", "🪚",
    "📐", "📏", "✂️", "🗂️", "🗃️", "📝", "🖍️", "🖌️", "🖋️", "🧠",
)
RUNTIME_FILES = (
    "icon-picker.css", "LICENSES.md", "licenses/dompurify.LICENSE.txt",
    "licenses/lucide-static.LICENSE.txt", "licenses/twemoji.LICENSE.txt",
    "licenses/unicode-emoji-json.LICENSE.txt", "src/components/icon-picker.js",
    "src/data/emoji-list.js", "src/data/lucide-names.js", "src/index.js",
    "src/utils/config.js", "src/utils/formatters.js", "src/utils/lucide-loader.js",
    "src/utils/sanitize.js", "src/utils/twemoji-loader.js", "src/vendor/purify.es.mjs",
)
ASSET_REFERENCE = re.compile(r"(?<![A-Za-z0-9._-])/?assets/[A-Za-z0-9._/-]+\.svg")

def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())

def _copy_new(source: Path, target: Path) -> bytes:
    if not source.is_file() or source.is_symlink():
        raise BridgeError(f"required travelling asset is missing or symbolic: {source}")
    content = source.read_bytes()
    _write_new(target, content)
    return content

def _exported_json(path: Path, prefix: str) -> Any:
    text = path.read_text(encoding="utf-8")
    start = text.find(prefix)
    if start < 0:
        raise BridgeError(f"travelling asset data is missing {prefix!r}: {path}")
    body = text[start + len(prefix):].strip()
    if not body.endswith(";"):
        raise BridgeError(f"travelling asset data lacks its final semicolon: {path}")
    try:
        return json.loads(body[:-1])
    except json.JSONDecodeError as exc:
        raise BridgeError(f"travelling asset data is not strict JSON: {path}: {exc}") from exc

def lucide_component_name(component: str) -> str:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", component)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    value = re.sub(r"([A-Za-z])([0-9])", r"\1-\2", value)
    value = re.sub(r"([0-9])([A-Za-z])", r"\1-\2", value)
    return value.lower()

def twemoji_key(char: str, strip_fe0f: bool = False) -> str:
    return "-".join(
        f"{ord(codepoint):x}" for codepoint in char
        if not (strip_fe0f and ord(codepoint) == 0xFE0F)
    )

def _reason(target: dict[str, set[str]], name: str, reason: str) -> None:
    target.setdefault(name, set()).add(reason)

def discover_assets(
    ui: dict[str, bytes],
    styles: dict[str, bytes],
    imported_components: list[str],
    config: dict[str, Any],
    lucide_names: set[str],
    emoji_data: dict[str, Any],
    twemoji_keys: set[str],
) -> dict[str, Any]:
    lucide: dict[str, set[str]] = {}
    emoji: dict[str, set[str]] = {}
    for name in LUCIDE_SAMPLE:
        _reason(lucide, name, "picker-sample")
    for char in TWEMOJI_SAMPLE:
        _reason(emoji, char, "picker-sample")
    for component in imported_components:
        _reason(lucide, lucide_component_name(component), "used-ui-import")
    for name in config["lucide"]:
        _reason(lucide, name, "used-explicit")
    for char in config["twemoji"]:
        _reason(emoji, char, "used-explicit")
    source_text = "\n".join(
        content.decode("utf-8") for _path, content in sorted({**ui, **styles}.items())
    )
    emoji_pattern = re.compile("|".join(
        re.escape(char) for char in sorted(emoji_data, key=lambda item: (-len(item), item))
    ))
    for match in emoji_pattern.finditer(source_text):
        _reason(emoji, match.group(0), "used-source-literal")
    custom_files: dict[str, set[str]] = {}
    for reference in ASSET_REFERENCE.findall(source_text):
        relative = reference.lstrip("/")
        lucide_match = re.fullmatch(
            r"assets/pickers/icon-picker/assets/lucide/([a-z0-9-]+)\.svg", relative
        )
        twemoji_match = re.fullmatch(
            r"assets/pickers/icon-picker/assets/twemoji/([a-f0-9-]+)\.svg", relative
        )
        if lucide_match:
            _reason(lucide, lucide_match.group(1), "used-source-asset")
        elif twemoji_match:
            matching = [
                char for char in emoji_data
                if twemoji_key(char) == twemoji_match.group(1)
                or twemoji_key(char, True) == twemoji_match.group(1)
            ]
            if len(matching) != 1:
                raise BridgeError(
                    f"Twemoji asset reference resolves to {len(matching)} characters: {reference}"
                )
            _reason(emoji, matching[0], "used-source-asset")
        else:
            _reason(custom_files, f"public/{relative}", "used-source-asset")
    for relative in config["svg_files"]:
        _reason(custom_files, relative, "used-explicit")
    unknown_lucide = sorted(set(lucide) - lucide_names)
    unknown_emoji = sorted(set(emoji) - set(emoji_data))
    if unknown_lucide:
        raise BridgeError(f"travelling Lucide name(s) are unavailable: {', '.join(unknown_lucide)}")
    if unknown_emoji:
        raise BridgeError(
            "travelling Twemoji character(s) lack metadata: "
            + ", ".join(repr(item) for item in unknown_emoji)
        )
    resolved_emoji: dict[str, str] = {}
    for char in emoji:
        full, stripped = twemoji_key(char), twemoji_key(char, True)
        key = full if full in twemoji_keys else stripped if stripped in twemoji_keys else None
        if key is None:
            raise BridgeError(f"travelling Twemoji character has no local SVG: {char!r}")
        resolved_emoji[char] = key
    return {
        "lucide": lucide,
        "twemoji": emoji,
        "twemoji_keys": resolved_emoji,
        "svg_files": custom_files,
    }

def _config(project: Path) -> dict[str, Any]:
    path = project / "designer-assets.json"
    value = read_json(path)
    if not isinstance(value, dict) or set(value) != {"schema", "lucide", "twemoji", "svg_files"}:
        raise BridgeError(f"designer asset config must have exact v1 fields: {path}")
    if value["schema"] != CONFIG_SCHEMA:
        raise BridgeError(f"unsupported designer asset config schema: {path}")
    for field in ("lucide", "twemoji", "svg_files"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
            raise BridgeError(f"designer asset config {field} must be non-empty strings")
        if len(items) != len(set(items)):
            raise BridgeError(f"designer asset config {field} contains duplicates")
    normalized = [
        safe_relative_path(item, label="designer asset svg_files") for item in value["svg_files"]
    ]
    if any(not item.startswith("public/") or not item.lower().endswith(".svg") for item in normalized):
        raise BridgeError("designer asset svg_files must be SVGs beneath public/")
    return {**value, "svg_files": normalized}

def _file_row(path: str, content: bytes) -> dict[str, Any]:
    return {"path": path, "bytes": len(content), "sha256": _sha(content)}

def build_travelling_asset_pack(
    project: Path,
    package: Path,
    ui: dict[str, bytes],
    styles: dict[str, bytes],
    imported_components: list[str],
) -> dict[str, Any]:
    source = project / PICKER_ROOT
    lucide_dir, twemoji_dir = source / "assets/lucide", source / "assets/twemoji"
    lucide_files = sorted(lucide_dir.glob("*.svg"), key=lambda item: item.name)
    twemoji_files = sorted(twemoji_dir.glob("*.svg"), key=lambda item: item.name)
    if (len(lucide_files), len(twemoji_files)) != (1952, 3720):
        raise BridgeError(
            "full project picker libraries drifted; expected Lucide=1952 and Twemoji=3720, "
            f"measured Lucide={len(lucide_files)} Twemoji={len(twemoji_files)}"
        )
    lucide_manifest = _exported_json(
        source / "src/data/lucide-manifest.js", "export const LUCIDE_MANIFEST = "
    )
    twemoji_manifest = _exported_json(
        source / "src/data/twemoji-keys.js", "export const TWEMOJI_KEYS = "
    )
    emoji_data = _exported_json(source / "src/data/emoji-data.js", "export default ")
    if set(lucide_manifest) != {path.stem for path in lucide_files}:
        raise BridgeError("full Lucide manifest disagrees with local SVG files")
    if set(twemoji_manifest) != {path.stem for path in twemoji_files}:
        raise BridgeError("full Twemoji manifest disagrees with local SVG files")
    selected = discover_assets(
        ui, styles, imported_components, _config(project), set(lucide_manifest),
        emoji_data, set(twemoji_manifest),
    )
    picker = package / PACKAGE_PICKER
    runtime_rows: list[dict[str, Any]] = []
    for relative in RUNTIME_FILES:
        content = _copy_new(source / relative, picker / relative)
        runtime_rows.append(_file_row((PACKAGE_PICKER / relative).as_posix(), content))
    empty_icons = (
        "/* Travelling picker uses only its measured SVG subset. */\n"
        "export const ICONS = Object.freeze({});\n"
        "export const icon = () => '<span class=\"ico\"></span>';\n"
    ).encode("utf-8")
    _write_new(picker / "src/utils/icons.js", empty_icons)
    runtime_rows.append(_file_row(
        (PACKAGE_PICKER / "src/utils/icons.js").as_posix(), empty_icons
    ))

    lucide_names = sorted(selected["lucide"])
    twemoji_chars = sorted(selected["twemoji"], key=lambda char: twemoji_key(char))
    generated = {
        "src/data/lucide-manifest.js": (
            "/* GENERATED for this travelling drop; not the full project catalog. */\n"
            f"export const LUCIDE_MANIFEST = {json.dumps(lucide_names)};\n"
        ).encode("utf-8"),
        "src/data/twemoji-keys.js": (
            "/* GENERATED for this travelling drop; not the full project catalog. */\n"
            f"export const TWEMOJI_KEYS = {json.dumps(sorted(set(selected['twemoji_keys'].values())))};\n"
        ).encode("utf-8"),
        "src/data/emoji-data.js": (
            "/* GENERATED for this travelling drop; not the full project catalog. */\n"
            "export default "
            + json.dumps(
                {char: emoji_data[char] for char in twemoji_chars},
                ensure_ascii=False, indent=2,
            )
            + ";\n"
        ).encode("utf-8"),
    }
    for relative, content in generated.items():
        _write_new(picker / relative, content)
        runtime_rows.append(_file_row((PACKAGE_PICKER / relative).as_posix(), content))
    lucide_rows, twemoji_rows, custom_rows = [], [], []
    for name in lucide_names:
        relative = (PACKAGE_PICKER / f"assets/lucide/{name}.svg").as_posix()
        content = _copy_new(lucide_dir / f"{name}.svg", package / relative)
        lucide_rows.append({
            "name": name, "reasons": sorted(selected["lucide"][name]),
            **_file_row(relative, content),
        })
    for char in twemoji_chars:
        key = selected["twemoji_keys"][char]
        relative = (PACKAGE_PICKER / f"assets/twemoji/{key}.svg").as_posix()
        content = _copy_new(twemoji_dir / f"{key}.svg", package / relative)
        twemoji_rows.append({
            "char": char, "name": emoji_data[char].get("name", ""),
            "key": key, "reasons": sorted(selected["twemoji"][char]),
            **_file_row(relative, content),
        })
    for source_path in sorted(selected["svg_files"]):
        package_path = source_path.removeprefix("public/")
        content = _copy_new(project / source_path, package / package_path)
        custom_rows.append({
            "source": source_path, "reasons": sorted(selected["svg_files"][source_path]),
            **_file_row(package_path, content),
        })
    manifest = {
        "schema": SCHEMA,
        "claim": "limited picker sample plus every discovered or explicitly declared used asset",
        "full_project": {"lucide_svg": 1952, "twemoji_svg": 3720},
        "picker_sample": {
            "lucide": list(LUCIDE_SAMPLE), "twemoji": list(TWEMOJI_SAMPLE),
            "lucide_count": 30, "twemoji_count": 30,
        },
        "carried": {
            "lucide": lucide_rows, "twemoji": twemoji_rows,
            "svg_files": custom_rows, "runtime_files": sorted(runtime_rows, key=lambda row: row["path"]),
        },
    }
    manifest_bytes = canonical_json(manifest)
    _write_new(package / "TRAVELLING-ASSETS.json", manifest_bytes)
    return {
        "manifest_sha256": _sha(manifest_bytes),
        "lucide_svg": len(lucide_rows),
        "twemoji_svg": len(twemoji_rows),
        "custom_svg": len(custom_rows),
        "picker_sample_each": 30,
    }


def validate_travelling_asset_pack(stage: Path) -> dict[str, Any]:
    manifest = read_json(stage / "TRAVELLING-ASSETS.json")
    if manifest.get("schema") != SCHEMA or set(manifest) != {
        "schema", "claim", "full_project", "picker_sample", "carried"
    }:
        raise BridgeError("travelling asset manifest is not strict v1")
    sample = manifest["picker_sample"]
    if sample != {
        "lucide": list(LUCIDE_SAMPLE), "twemoji": list(TWEMOJI_SAMPLE),
        "lucide_count": 30, "twemoji_count": 30,
    }:
        raise BridgeError("travelling picker sample is not the exact governed 30+30 set")
    carried = manifest["carried"]
    if not isinstance(carried, dict) or set(carried) != {
        "lucide", "twemoji", "svg_files", "runtime_files"
    }:
        raise BridgeError("travelling asset carried inventory is malformed")
    rows = [
        *carried["lucide"], *carried["twemoji"],
        *carried["svg_files"], *carried["runtime_files"],
    ]
    paths = [row.get("path") for row in rows]
    if any(not isinstance(path, str) for path in paths) or len(paths) != len(set(paths)):
        raise BridgeError("travelling asset inventory has missing or duplicate paths")
    for row in rows:
        relative = safe_relative_path(row["path"], label="travelling asset path")
        file = stage / relative
        if (
            not file.is_file() or file.is_symlink()
            or file.stat().st_size != row.get("bytes")
            or hashlib.sha256(file.read_bytes()).hexdigest() != row.get("sha256")
        ):
            raise BridgeError(f"travelling asset bytes disagree with manifest: {relative}")
    picker = stage / PACKAGE_PICKER
    actual_picker = sorted(
        path.relative_to(stage).as_posix() for path in picker.rglob("*") if path.is_file()
    )
    expected_picker = sorted(path for path in paths if path.startswith(f"{PACKAGE_PICKER.as_posix()}/"))
    if actual_picker != expected_picker:
        raise BridgeError("travelling picker contains an unmanifested or missing file")
    for library, sample_names in (("lucide", LUCIDE_SAMPLE), ("twemoji", TWEMOJI_SAMPLE)):
        rows_by_name = {
            row["name" if library == "lucide" else "char"]: row for row in carried[library]
        }
        if any("picker-sample" not in rows_by_name.get(name, {}).get("reasons", []) for name in sample_names):
            raise BridgeError(f"travelling {library} picker sample is incomplete")
    return manifest
