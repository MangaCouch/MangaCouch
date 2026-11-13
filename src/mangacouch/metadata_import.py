from __future__ import annotations

from dataclasses import dataclass

from .models import Metadata, NativeSidecar


@dataclass(frozen=True)
class MetadataImport:
    metadata: Metadata
    candidates: dict[str, Metadata]
    conflicts: tuple[str, ...]
    ignored_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    native: NativeSidecar | None = None
    native_raw_hash: str | None = None
    eze_payload: dict | None = None

    @property
    def sources(self) -> list[str]:
        return (["native"] if self.native else []) + list(self.candidates)
