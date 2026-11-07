from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from .metadata_import import MetadataImport
from .models import Metadata, Source, normalized


def eze_path(archive):
    return archive.with_name(archive.name + ".eze.json")


def export_eze(metadata: Metadata, *, fetched_at: int | None = None, gdata: dict | None = None) -> dict:
    source = metadata.source
    tags: dict[str, list[str]] = {}
    for tag in metadata.tags:
        tags.setdefault(tag.namespace or "other", []).append(tag.value)

    def reference(item):
        return {
            "site": "e-hentai" if item.site == "ehentai" else item.site,
            "gid": item.gallery_id,
            "token": item.gallery_token,
        }

    info = {
        "title": metadata.title,
        "title_original": metadata.original_title,
        "language": metadata.language,
        "translated": metadata.translated,
        "category": metadata.work_type,
        "tags": tags,
        "source": reference(source) if source else {},
        "uploader": metadata.uploader,
        "rating": metadata.source_rating.model_dump() if metadata.source_rating else None,
        "filecount": metadata.declared_page_count,
        "filesize": metadata.declared_byte_size,
    }
    if metadata.uploaded_at:
        uploaded = datetime.fromisoformat(metadata.uploaded_at).astimezone(UTC)
        info["posted"] = int(uploaded.timestamp())
        info["upload_date"] = [
            uploaded.year,
            uploaded.month,
            uploaded.day,
            uploaded.hour,
            uploaded.minute,
            uploaded.second,
        ]
    if metadata.parent:
        info["source"]["parent_gallery"] = reference(metadata.parent)
    info["source"]["newer_versions"] = [reference(item) for item in metadata.newer_versions]
    result: dict[str, Any] = {"gallery_info": info}
    if fetched_at is not None:
        result["fetched_at"] = fetched_at
    if gdata is not None:
        result["gdata"] = gdata
    return result


MAX_EZE_BYTES = 2 * 1024 * 1024
INFO_KEYS = {
    "title",
    "title_original",
    "language",
    "translated",
    "category",
    "tags",
    "source",
    "uploader",
    "posted",
    "upload_date",
    "rating",
    "filecount",
    "filesize",
}
FULL_KEYS = {
    "title",
    "title_original",
    "language",
    "translated",
    "category",
    "tags",
    "gallery",
    "source_site",
    "uploader",
    "date_uploaded",
    "rating",
    "image_count",
    "total_file_size_approx",
    "parent",
    "newer_versions",
}
SOURCE_KEYS = {"site", "gid", "token", "parent_gallery", "newer_versions"}
FIELDS = {
    "title": "title",
    "title_original": "original_title",
    "language": "language",
    "translated": "translated",
    "category": "work_type",
    "tags": "tags",
    "uploader": "uploader",
}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value.strip() or None


def _language(value: object) -> str | None:
    value = _text(value)
    return None if value is None or normalized(value) in {"n/a", "unknown"} else value


def _tags(value: object) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError("Expected namespaced tags")
    tags = []
    for namespace, items in value.items():
        if not isinstance(namespace, str) or not isinstance(items, list):
            raise ValueError("Expected a tag array per namespace")
        for item in items:
            text = _text(item)
            if text:
                tags.append({"namespace": namespace.strip(), "value": text})
    return tags


def _source(value: object, site: object = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected a gallery reference")
    site = value.get("site", site)
    if site == "e-hentai":
        site = "ehentai"
    return Source.model_validate(
        {
            "site": site,
            "gallery_id": value.get("gid"),
            "gallery_token": value.get("token"),
        }
    ).model_dump(mode="json")


def _milliseconds(value: object) -> str:
    if type(value) is not int or not 1_000_000_000_000 <= value < 100_000_000_000_000:
        raise ValueError("Expected an Eze Unix timestamp in milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _seconds(value: object) -> str:
    if type(value) is not int or not 0 <= value < 100_000_000_000:
        raise ValueError("Expected Unix seconds")
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _rating(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {"average": value.get("average"), "count": value.get("count")}
    return {"average": value}


def _equivalent(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return normalized(left) == normalized(right)
    if isinstance(left, list) and isinstance(right, list):
        return {json.dumps(v, sort_keys=True, ensure_ascii=False).casefold() for v in left} == {
            json.dumps(v, sort_keys=True, ensure_ascii=False).casefold() for v in right
        }
    return left == right


def _ignored_reference_fields(value: object, path: str) -> set[str]:
    if isinstance(value, list):
        return set().union(*(_ignored_reference_fields(item, f"{path}[]") for item in value))
    if isinstance(value, dict):
        return {f"{path}.{key}" for key in value.keys() - {"site", "gid", "token"}}
    return set()


def _candidate(
    obj: dict,
    source: object,
    section: str,
    allowed: set[str],
    origin: str,
    ignored: set[str],
    warnings: set[str],
) -> Metadata:
    ignored.update(f"{section}.{key}" for key in obj.keys() - allowed)
    values: dict[str, Any] = {}
    provenance: dict[str, str] = {}

    def put(field: str, key: str, convert: Callable[[Any], Any] = lambda v: v) -> None:
        if obj.get(key) is None:
            return
        try:
            value = convert(obj[key])
            checked = Metadata.model_validate({field: value}).model_dump(mode="json")[field]
        except (ValueError, TypeError, OverflowError, OSError):
            warnings.add(f"{section}.{key}: invalid; needs review")
            return
        if checked is not None and checked != []:
            values[field] = checked
            provenance[field] = origin

    for key, field in FIELDS.items():
        converter = _tags if key == "tags" else _language if key == "language" else _text
        if key == "translated":
            put(field, key)
        else:
            put(field, key, converter)
    put("source_rating", "rating", _rating)
    if isinstance(obj.get("rating"), dict):
        ignored.update(f"{section}.rating.{key}" for key in obj["rating"].keys() - {"average", "count"})
    source_site = source.get("site") if isinstance(source, dict) else None
    if section == "gallery_info":
        put("source", "source", _source)
        put("uploaded_at", "posted", _seconds)
        put("declared_page_count", "filecount")
        put("declared_byte_size", "filesize")
        if isinstance(source, dict):
            ignored.update(f"{section}.source.{key}" for key in source.keys() - SOURCE_KEYS)
            for key, field in (("parent_gallery", "parent"), ("newer_versions", "newer_versions")):
                ignored.update(_ignored_reference_fields(source.get(key), f"{section}.source.{key}"))
                if source.get(key) is None:
                    continue
                try:
                    value = source[key]
                    if key == "newer_versions":
                        if not isinstance(value, list):
                            raise ValueError("Expected versions array")
                        mapped: Any = [_source(v, source_site) for v in value]
                    else:
                        mapped = _source(value, source_site)
                    values[field] = Metadata.model_validate({field: mapped}).model_dump(mode="json")[field]
                    provenance[field] = origin
                except (ValueError, TypeError):
                    warnings.add(f"{section}.source.{key}: invalid; needs review")
    else:
        site = obj.get("source_site", source_site)
        put("source", "gallery", lambda v: _source(v, site))
        put("uploaded_at", "date_uploaded", _milliseconds)
        put("declared_page_count", "image_count")
        put("declared_byte_size", "total_file_size_approx")
        put("parent", "parent", lambda v: _source(v, site))

        def versions(value: object) -> list[dict[str, Any]]:
            if not isinstance(value, list):
                raise ValueError("Expected versions array")
            return [_source(v, site) for v in value]

        put("newer_versions", "newer_versions", versions)
        for key in ("gallery", "parent", "newer_versions"):
            ignored.update(_ignored_reference_fields(obj.get(key), f"{section}.{key}"))
    values["field_sources"] = provenance
    try:
        return Metadata.model_validate(values)
    except ValidationError:
        raise ValueError("Combined Eze metadata exceeds supported bounds") from None


def import_eze(data: bytes) -> MetadataImport:
    if len(data) > MAX_EZE_BYTES:
        raise ValueError("Eze input exceeds the supported byte budget")
    try:
        raw = json.loads(data)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid Eze JSON") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("gallery_info"), dict):
        raise ValueError("Expected Eze gallery_info, not a library backup or native sidecar")
    info = raw["gallery_info"]
    full = raw.get("gallery_info_full", {})
    if not isinstance(full, dict):
        raise ValueError("Expected Eze gallery_info_full object")
    ignored = {f"$.{k}" for k in raw if k not in {"gallery_info", "gallery_info_full", "fetched_at", "gdata"}}
    warnings: set[str] = set()
    candidates: dict[str, Metadata] = {}
    for section, obj, allowed, origin in (
        ("gallery_info", info, INFO_KEYS, "eze:info"),
        ("gallery_info_full", full, FULL_KEYS, "eze:full"),
    ):
        candidates[origin] = _candidate(
            obj,
            info.get("source", {}),
            section,
            allowed,
            origin,
            ignored,
            warnings,
        )

    merged = candidates["eze:info"].model_dump(mode="json")
    secondary = candidates["eze:full"].model_dump(mode="json")
    conflicts = []
    source_conflict = (
        merged["source"] is not None
        and secondary["source"] is not None
        and merged["source"] != secondary["source"]
    )
    for field, value in secondary.items():
        if field in {"field_sources", "protected_fields"} or value is None or value == []:
            continue
        if merged[field] is None or merged[field] == []:
            if source_conflict:
                conflicts.append(field)
            else:
                merged[field] = value
                merged["field_sources"][field] = "eze:full"
        elif not _equivalent(merged[field], value):
            conflicts.append(field)
    return MetadataImport(
        metadata=Metadata.model_validate(merged),
        candidates=candidates,
        conflicts=tuple(sorted(conflicts)),
        ignored_fields=tuple(sorted(ignored)),
        warnings=tuple(sorted(warnings)),
        eze_payload=export_eze(Metadata.model_validate(merged)),
    )
