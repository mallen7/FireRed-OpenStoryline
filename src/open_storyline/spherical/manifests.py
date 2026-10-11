"""Deterministic hashing and atomic JSON helpers for spherical artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from open_storyline.spherical.analysis_schemas import ANALYSIS_SCHEMA_MODELS
from open_storyline.spherical.direction_schemas import DIRECTION_SCHEMA_MODELS
from open_storyline.spherical.schemas import SCHEMA_MODELS


ModelT = TypeVar("ModelT", bound=BaseModel)
PUBLIC_SCHEMA_MODELS = {
    **SCHEMA_MODELS,
    **ANALYSIS_SCHEMA_MODELS,
    **DIRECTION_SCHEMA_MODELS,
}


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as file_handle:
        while chunk := file_handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value in a stable form suitable for cache keys."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def atomic_write_json(path: str | Path, value: Any) -> Path:
    """Write formatted JSON through a same-directory atomic replacement."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(
                value,
                file_handle,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
                default=_json_default,
            )
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def load_json_model(path: str | Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate_json(Path(path).read_text(encoding="utf-8"))


def manifest_path_for(output_path: str | Path) -> Path:
    output = Path(output_path)
    return output.with_name(f"{output.name}.manifest.json")


def export_json_schemas(output_dir: str | Path) -> list[Path]:
    """Materialize all public spherical Pydantic contracts as JSON Schema files."""

    root = Path(output_dir)
    written: list[Path] = []
    for name, model_type in sorted(PUBLIC_SCHEMA_MODELS.items()):
        schema_path = root / f"{name}.schema.json"
        atomic_write_json(schema_path, model_type.model_json_schema())
        written.append(schema_path)
    return written
