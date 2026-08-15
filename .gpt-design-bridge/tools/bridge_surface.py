"""Closed reversible transform for the no-build designer surface."""

from __future__ import annotations

import base64
import json
import posixpath
import re
from pathlib import Path
from typing import Any

from bridge_core import BridgeError, read_json, safe_relative_path


SURFACE_SCHEMA = "gpt-design-bridge/design-surface/v1"
REGION_BEGIN = "<!-- gpt-design-bridge:designer-blocks-begin -->"
REGION_END = "<!-- gpt-design-bridge:designer-blocks-end -->"
REGION_PLACEHOLDER = "<!-- gpt-design-bridge:designer-blocks -->"
IMPORT_MARKER = re.compile(r"^/\*@gdb-import:([A-Za-z0-9+/=]+)\*/(?:\r?\n)?$")
EXPORT_MARKER = re.compile(r"^/\*@gdb-export:([A-Za-z0-9+/=]+)\*/")
SOURCE_BLOCK = re.compile(
    r'<script type="text/babel" data-gdb-source="([^"]+)" data-presets="react">'
    r"\r?\n(.*?)</script>",
    re.DOTALL,
)
STATIC_IMPORT = re.compile(
    r"^\s*import\s+(?:[\"'][^\"']+[\"']|.+\s+from\s+[\"'][^\"']+[\"'])\s*;?\s*(?:\r?\n)?$"
)
EXPORT_PREFIX = re.compile(r"^(\s*export(?:\s+default)?\s+)")
ALLOWED_SURFACE_KEYS = {"schema", "embedded", "direct"}
IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
IMPORT_FROM = re.compile(r"^\s*import\s+(.+?)\s+from\s+[\"']([^\"']+)[\"']\s*;?\s*$")
TOP_LEVEL_DECLARATION = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?"
    r"(?:function|class|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
NAMED_EXPORT = re.compile(
    r"^export\s+(?!default\b)(?:async\s+)?"
    r"(?:function|class|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
DEFAULT_DECLARATION = re.compile(
    r"^export\s+default\s+(?:async\s+)?(?:function|class)\s+"
    r"([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
DEFAULT_IDENTIFIER = re.compile(
    r"^export\s+default\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*;?\s*$",
    re.MULTILINE,
)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _unb64(value: str, label: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise BridgeError(f"malformed {label} marker") from exc


def _line_ending(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""


def embed_source(source_path: str, source: bytes) -> str:
    safe_relative_path(source_path, label="embedded source path")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError(f"designer source is not valid UTF-8: {source_path}") from exc
    lowered = text.lower()
    if "@gdb-" in text or "</script" in lowered:
        raise BridgeError(f"designer source contains a reserved embed token: {source_path}")
    if re.search(r"\bimport\s*\(", text):
        raise BridgeError(f"dynamic import is unsupported in designer source: {source_path}")
    transformed: list[str] = []
    code_seen = False
    block_comment = False
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        stripped = line.strip()
        comment_only = not stripped or stripped.startswith("//") or block_comment
        if stripped.startswith("/*"):
            comment_only = True
            block_comment = "*/" not in stripped
        elif block_comment and "*/" in stripped:
            block_comment = False
        if re.match(r"^\s*import\b", line):
            if code_seen or not STATIC_IMPORT.fullmatch(line):
                raise BridgeError(
                    f"unsupported non-leading or multiline import in {source_path}:{line_number}"
                )
            ending = _line_ending(line)
            transformed.append(f"/*@gdb-import:{_b64(line)}*/{ending}")
            continue
        if not comment_only:
            code_seen = True
        if re.match(r"^\s*export\s+\*\s+from\b", line):
            raise BridgeError(f"re-export is unsupported in designer source: {source_path}:{line_number}")
        match = EXPORT_PREFIX.match(line)
        if match:
            prefix = match.group(1)
            line = f"/*@gdb-export:{_b64(prefix)}*/" + line[match.end() :]
        transformed.append(line)
    result = "".join(transformed)
    if re.search(r"(?m)^\s*(?:import|export)\b", result):
        raise BridgeError(f"bare import/export survived embed transform: {source_path}")
    return result


def unembed_source(source_path: str, transformed: str) -> bytes:
    safe_relative_path(source_path, label="embedded source path")
    restored: list[str] = []
    for line in transformed.splitlines(keepends=True):
        import_match = IMPORT_MARKER.fullmatch(line)
        if import_match:
            restored.append(_unb64(import_match.group(1), "import"))
            continue
        export_match = EXPORT_MARKER.match(line)
        if export_match:
            restored.append(_unb64(export_match.group(1), "export") + line[export_match.end() :])
            continue
        if "@gdb-" in line:
            raise BridgeError(f"unknown or damaged embed marker in {source_path}")
        restored.append(line)
    try:
        return "".join(restored).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BridgeError(f"restored source is not UTF-8 encodable: {source_path}") from exc


def plumbing_markers(transformed: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).rstrip("\r\n")
        for line in transformed.splitlines(keepends=True)
        if (match := IMPORT_MARKER.fullmatch(line)) or (match := EXPORT_MARKER.match(line))
    )


def _split_region(index: str) -> tuple[str, str, str]:
    if index.count(REGION_BEGIN) != 1 or index.count(REGION_END) != 1:
        raise BridgeError("index must contain exactly one designer-region begin and end marker")
    before, remainder = index.split(REGION_BEGIN, 1)
    content, after = remainder.split(REGION_END, 1)
    return before, content, after


def index_skeleton(index: bytes) -> bytes:
    try:
        text = index.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError("designer index is not valid UTF-8") from exc
    before, _content, after = _split_region(text)
    return f"{before}{REGION_BEGIN}{REGION_PLACEHOLDER}{REGION_END}{after}".encode("utf-8")


def _source_block(source_path: str, transformed: str) -> str:
    begin = f"/*@gdb-source-begin:{source_path}*/"
    end = f"/*@gdb-source-end:{source_path}*/"
    return (
        f'<script type="text/babel" data-gdb-source="{source_path}" data-presets="react">\n'
        f"{begin}\n{transformed}{end}\n</script>"
    )


def _source_text(source_path: str, source: bytes) -> str:
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError(f"designer source is not valid UTF-8: {source_path}") from exc


def _binding(local: str, imported: str, kind: str) -> dict[str, str]:
    if not IDENTIFIER.fullmatch(local) or (
        kind != "default" and not IDENTIFIER.fullmatch(imported)
    ):
        raise BridgeError(f"unsupported import identifier: {imported} as {local}")
    return {"kind": kind, "imported": imported, "local": local}


def _parse_import(source_path: str, statement: str) -> tuple[str, list[dict[str, str]]]:
    match = IMPORT_FROM.fullmatch(statement)
    if not match:
        raise BridgeError(f"designer import is not a supported from-import: {source_path}")
    clause, module = match.groups()
    bindings: list[dict[str, str]] = []
    remainder = clause.strip()
    if not remainder.startswith(("{", "*")):
        default, separator, remainder = remainder.partition(",")
        bindings.append(_binding(default.strip(), "default", "default"))
        remainder = remainder.strip() if separator else ""
    if remainder.startswith("*"):
        namespace = re.fullmatch(r"\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)", remainder)
        if not namespace:
            raise BridgeError(f"malformed namespace import in {source_path}")
        bindings.append(_binding(namespace.group(1), "*", "namespace"))
    elif remainder:
        named = re.fullmatch(r"\{(.*)\}", remainder)
        if not named:
            raise BridgeError(f"malformed named import in {source_path}")
        for item in named.group(1).split(","):
            item = item.strip()
            if not item:
                continue
            halves = re.split(r"\s+as\s+", item)
            if len(halves) not in (1, 2):
                raise BridgeError(f"malformed import alias in {source_path}: {item}")
            bindings.append(
                _binding(
                    halves[-1].strip(),
                    halves[0].strip(),
                    "named",
                )
            )
    if not bindings:
        raise BridgeError(f"side-effect-only imports are outside the designer surface: {source_path}")
    return module, bindings


def _imports(source_path: str, source: bytes) -> list[tuple[str, list[dict[str, str]]]]:
    text = _source_text(source_path, source)
    embed_source(source_path, source)
    result: list[tuple[str, list[dict[str, str]]]] = []
    for line in text.splitlines():
        if re.match(r"^\s*import\b", line):
            result.append(_parse_import(source_path, line))
    return result


def _exports(source_path: str, source: bytes) -> dict[str, Any]:
    text = _source_text(source_path, source)
    declared = set(TOP_LEVEL_DECLARATION.findall(text))
    named = set(NAMED_EXPORT.findall(text))
    defaults = DEFAULT_DECLARATION.findall(text) + DEFAULT_IDENTIFIER.findall(text)
    if len(set(defaults)) > 1:
        raise BridgeError(f"designer source has multiple default export names: {source_path}")
    default = defaults[0] if defaults else None
    if default and default not in declared:
        raise BridgeError(f"default export is not a top-level declaration in {source_path}: {default}")
    return {"declared": declared, "named": named, "default": default}


def _relative_target(
    source_path: str, module: str, sources: dict[str, bytes], source_root: str
) -> str:
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), module))
    if normalized == source_root or not normalized.startswith(source_root + "/"):
        raise BridgeError(f"designer relative import leaves {source_root}: {source_path} -> {module}")
    candidates = [normalized] if posixpath.splitext(normalized)[1] else [
        normalized + ".js",
        normalized + ".jsx",
        normalized + "/index.js",
        normalized + "/index.jsx",
    ]
    found = [candidate for candidate in candidates if candidate in sources]
    if len(found) != 1:
        raise BridgeError(
            f"designer relative import resolves to {len(found)} files: {source_path} -> {module}"
        )
    return found[0]


def designer_global_contract(
    sources: dict[str, bytes], source_root: str
) -> dict[str, Any]:
    safe_relative_path(source_root, label="designer source root")
    if not sources:
        raise BridgeError("global-name contract requires designer sources")
    normalized = {
        safe_relative_path(path, label="designer source path"): content
        for path, content in sources.items()
    }
    if any(not path.startswith(source_root + "/") for path in normalized):
        raise BridgeError(f"global-name contract source falls outside {source_root}")
    exports = {path: _exports(path, content) for path, content in normalized.items()}
    declaration_owner: dict[str, str] = {}
    for path in sorted(exports, key=str.casefold):
        for name in sorted(exports[path]["declared"]):
            if name in declaration_owner:
                raise BridgeError(
                    f"duplicate top-level designer declaration {name}: "
                    f"{declaration_owner[name]} and {path}"
                )
            declaration_owner[name] = path

    providers: dict[str, tuple[str, str]] = {}
    icons: set[str] = set()
    imported_count = 0

    def provide(local: str, expression: str, origin: str) -> None:
        prior = providers.get(local)
        if prior and prior[0] != expression:
            raise BridgeError(
                f"designer global {local} has conflicting providers: {prior[1]} and {origin}"
            )
        if local in declaration_owner and expression != local:
            raise BridgeError(
                f"designer global {local} is both declared by {declaration_owner[local]} "
                f"and supplied by {origin}"
            )
        if prior is None:
            providers[local] = (expression, origin)

    for source_path in sorted(normalized, key=str.casefold):
        for module, bindings in _imports(source_path, normalized[source_path]):
            imported_count += len(bindings)
            if module == "react":
                for item in bindings:
                    if item["kind"] in {"default", "namespace"}:
                        provide(item["local"], "React", f"react import in {source_path}")
                    else:
                        provide(
                            item["local"],
                            f"React.{item['imported']}",
                            f"react import in {source_path}",
                        )
            elif module == "lucide-react":
                for item in bindings:
                    if item["kind"] != "named":
                        raise BridgeError(f"lucide imports must be named in {source_path}")
                    icons.add(item["imported"])
                    provide(
                        item["local"],
                        f"LucideReact.{item['imported']}",
                        f"lucide-react import in {source_path}",
                    )
            elif module.startswith("."):
                target = _relative_target(source_path, module, normalized, source_root)
                target_exports = exports[target]
                for item in bindings:
                    if item["kind"] == "namespace":
                        raise BridgeError(f"namespace sibling import is unsupported in {source_path}")
                    exported = (
                        target_exports["default"]
                        if item["kind"] == "default"
                        else item["imported"]
                    )
                    if not exported or (
                        item["kind"] == "named" and exported not in target_exports["named"]
                    ):
                        raise BridgeError(
                            f"{source_path} imports missing {item['imported']} from {target}"
                        )
                    provide(
                        item["local"],
                        exported,
                        f"sibling export {target}:{exported}",
                    )
            else:
                raise BridgeError(
                    f"designer source imports unsupported package {module}: {source_path}"
                )

    prelude = [
        f"const {local} = {expression};"
        for local, (expression, _origin) in sorted(providers.items())
        if local != expression
    ]
    return {
        "prelude": "\n".join(prelude) + ("\n" if prelude else ""),
        "icons": sorted(icons),
        "imports": imported_count,
        "bindings": sorted(providers),
        "declarations": sorted(declaration_owner),
    }


def render_index(template: bytes, sources: dict[str, bytes]) -> bytes:
    try:
        text = template.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError("index template is not valid UTF-8") from exc
    before, existing, after = _split_region(text)
    if existing.strip() not in ("", REGION_PLACEHOLDER):
        extract_embedded_sources(template)
    if not sources:
        raise BridgeError("at least one embedded designer source is required")
    blocks = [
        _source_block(path, embed_source(path, sources[path]))
        for path in sorted(sources, key=str.casefold)
    ]
    return f"{before}{REGION_BEGIN}\n{'\n'.join(blocks)}\n{REGION_END}{after}".encode("utf-8")


def extract_embedded_sources(index: bytes) -> dict[str, bytes]:
    try:
        text = index.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BridgeError("returned index is not valid UTF-8") from exc
    _before, content, _after = _split_region(text)
    sources: dict[str, bytes] = {}
    cursor = 0
    for match in SOURCE_BLOCK.finditer(content):
        if content[cursor : match.start()].strip():
            raise BridgeError("designer region contains content outside recognized source blocks")
        source_path = safe_relative_path(match.group(1), label="embedded source path")
        if source_path.casefold() in {item.casefold() for item in sources}:
            raise BridgeError(f"designer region contains duplicate source path: {source_path}")
        body = match.group(2)
        begin = f"/*@gdb-source-begin:{source_path}*/\n"
        end = f"/*@gdb-source-end:{source_path}*/\n"
        if not body.startswith(begin) or not body.endswith(end):
            raise BridgeError(f"source block markers are missing or mismatched: {source_path}")
        transformed = body[len(begin) : -len(end)]
        sources[source_path] = unembed_source(source_path, transformed)
        cursor = match.end()
    if content[cursor:].strip():
        raise BridgeError("designer region contains trailing unrecognized content")
    if not sources:
        raise BridgeError("designer region contains no source blocks")
    return sources


def assert_outside_region_unchanged(baseline: bytes, returned: bytes) -> None:
    if index_skeleton(baseline) != index_skeleton(returned):
        raise BridgeError("engineering-owned index content changed outside the designer region")


def assert_plumbing_unchanged(baseline: bytes, returned: bytes) -> None:
    extract_embedded_sources(baseline)
    extract_embedded_sources(returned)
    baseline_sources = _transformed_sources(baseline)
    returned_sources = _transformed_sources(returned)
    for source_path in set(baseline_sources) & set(returned_sources):
        if plumbing_markers(baseline_sources[source_path]) != plumbing_markers(
            returned_sources[source_path]
        ):
            raise BridgeError(f"engineering plumbing markers changed: {source_path}")


def _transformed_sources(index: bytes) -> dict[str, str]:
    text = index.decode("utf-8")
    _before, content, _after = _split_region(text)
    result: dict[str, str] = {}
    for match in SOURCE_BLOCK.finditer(content):
        path, body = match.group(1), match.group(2)
        begin, end = f"/*@gdb-source-begin:{path}*/\n", f"/*@gdb-source-end:{path}*/\n"
        if body.startswith(begin) and body.endswith(end):
            result[path] = body[len(begin) : -len(end)]
    return result


def load_surface(path: Path) -> dict[str, Any]:
    surface = read_json(path)
    if set(surface) != ALLOWED_SURFACE_KEYS or surface.get("schema") != SURFACE_SCHEMA:
        raise BridgeError(f"unsupported or non-strict design surface schema: {path}")
    if not isinstance(surface.get("embedded"), dict) or not isinstance(surface.get("direct"), list):
        raise BridgeError(f"design surface embedded/direct shape is invalid: {path}")
    entries = [surface["embedded"], *surface["direct"]]
    package_roots: list[str] = []
    source_roots: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "package_root" if entry is not surface["embedded"] else "package_path",
            "source_root",
            "extensions",
        }:
            raise BridgeError(f"design surface entry shape is invalid: {path}")
        package_key = "package_path" if entry is surface["embedded"] else "package_root"
        package_roots.append(safe_relative_path(entry[package_key], label=package_key))
        source_roots.append(safe_relative_path(entry["source_root"], label="source_root"))
        extensions = entry["extensions"]
        if (
            not isinstance(extensions, list)
            or not extensions
            or len(set(extensions)) != len(extensions)
            or any(not re.fullmatch(r"\.[a-z0-9]+", item) for item in extensions)
        ):
            raise BridgeError(f"design surface extensions are invalid: {path}")
    for roots, label in ((package_roots, "package"), (source_roots, "source")):
        if any(
            left == right or left.startswith(right + "/") or right.startswith(left + "/")
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise BridgeError(f"design surface {label} roots overlap: {path}")
    return surface


def _under(path: str, root: str) -> bool:
    return path.startswith(root + "/")


def validate_embedded_sources(surface: dict[str, Any], sources: dict[str, bytes]) -> None:
    embedded = surface["embedded"]
    root, extensions = embedded["source_root"], tuple(embedded["extensions"])
    invalid = [
        path
        for path in sources
        if safe_relative_path(path, label="embedded source path") != path
        or not _under(path, root)
        or not path.endswith(extensions)
    ]
    if invalid:
        raise BridgeError("embedded source falls outside its declared surface: " + ", ".join(invalid))


def map_direct_package_path(surface: dict[str, Any], package_path: str) -> str | None:
    normalized = safe_relative_path(package_path, label="package path")
    for entry in surface["direct"]:
        package_root = entry["package_root"]
        if _under(normalized, package_root):
            relative = normalized[len(package_root) + 1 :]
            if not normalized.endswith(tuple(entry["extensions"])):
                raise BridgeError(f"designer file extension is not allowed: {normalized}")
            return f"{entry['source_root']}/{relative}"
    return None


def is_designer_package_path(surface: dict[str, Any], package_path: str) -> bool:
    normalized = safe_relative_path(package_path, label="package path")
    return normalized == surface["embedded"]["package_path"] or any(
        _under(normalized, entry["package_root"]) for entry in surface["direct"]
    )
