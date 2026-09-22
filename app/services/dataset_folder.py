"""Resolve a dataset_id to a folder under EVALUATION_TEMP_ROOT.

The temporary folder is an input location only. Payloads are never written to
PostgreSQL; the runner re-reads the same files when a ticket executes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.services.errors import SourcePayloadMissingError
from app.services.payload_validation import PayloadValidationError, extract_identifiers

logger = get_logger(__name__)

SUPPORTED_PAYLOAD_SUFFIXES = {".json"}
CONFIG_FILENAME = "config.json"
DATASET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class DatasetFolderError(ValueError):
    """Invalid dataset path or contents. `status_code` drives the HTTP mapping."""

    status_code = 400


class DatasetNotFoundError(DatasetFolderError):
    status_code = 404


@dataclass
class PayloadFile:
    """One JSON payload read from the temporary dataset folder."""

    filename: str
    payload: dict[str, Any]
    payload_id: str | None
    skipped: bool = False


@dataclass
class DatasetFolderContents:
    dataset_id: str
    folder: Path
    payloads: list[PayloadFile]
    config: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def temp_root() -> Path:
    return Path(settings.evaluation_temp_root).expanduser().resolve()


def resolve_dataset_dir(dataset_id: str, *, root: Path | None = None) -> Path:
    """Map dataset_id to `{EVALUATION_TEMP_ROOT}/{dataset_id}/`.

    Rejects path traversal, absolute paths, and identifiers that are not a
    single folder name. The resolved directory must remain inside the root.
    """
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise DatasetFolderError("dataset_id is required")
    identifier = dataset_id.strip()
    if identifier in {".", ".."} or not DATASET_ID_RE.match(identifier):
        raise DatasetFolderError(
            "dataset_id must be a single folder name using letters, digits, "
            "dot, underscore, or hyphen"
        )
    if any(sep in identifier for sep in ("/", "\\")):
        raise DatasetFolderError("dataset_id must not contain path separators")

    base = (root or temp_root()).resolve()
    candidate = (base / identifier).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise DatasetFolderError("dataset_id escapes EVALUATION_TEMP_ROOT") from exc
    if candidate == base:
        raise DatasetFolderError("dataset_id must name a folder inside EVALUATION_TEMP_ROOT")
    if candidate.exists() and not candidate.is_dir():
        raise DatasetFolderError(f"dataset path {identifier!r} is not a directory")
    if not candidate.exists():
        raise DatasetNotFoundError(f"dataset folder {identifier!r} was not found")
    return candidate


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        # utf-8-sig so files exported by editors that prepend a BOM still parse.
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PayloadValidationError(f"{path.name} is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise DatasetFolderError(f"could not read {path.name}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PayloadValidationError(
            f"invalid JSON in {path.name}: {exc.msg} (line {exc.lineno})"
        ) from exc
    if not isinstance(document, dict):
        raise PayloadValidationError(
            f"{path.name} must contain a JSON object, got {type(document).__name__}"
        )
    if not document:
        raise PayloadValidationError(f"{path.name} must not be empty")
    return document


def read_dataset_folder(dataset_id: str, *, root: Path | None = None) -> DatasetFolderContents:
    """Read every supported JSON payload in the dataset folder.

    Behaviour:
    - Missing folder: DatasetNotFoundError (HTTP 404).
    - Path traversal / invalid identifier: DatasetFolderError (HTTP 400).
    - Empty folder or no .json files: DatasetFolderError (HTTP 400).
    - Unsupported file types (.txt, .jsonl, directories, ...): skipped, warning.
    - Invalid JSON or non-object JSON: PayloadValidationError (HTTP 400); the
      job is not created (the caller's transaction never starts).
    """
    folder = resolve_dataset_dir(dataset_id, root=root)
    warnings: list[str] = []
    payloads: list[PayloadFile] = []

    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        raise DatasetFolderError(f"could not list dataset folder {dataset_id!r}: {exc}") from exc

    for entry in entries:
        if entry.name.startswith(".") or entry.name.lower() == CONFIG_FILENAME:
            continue
        if entry.is_dir():
            warnings.append(f"skipped directory {entry.name!r}")
            continue
        suffix = entry.suffix.lower()
        if suffix not in SUPPORTED_PAYLOAD_SUFFIXES:
            warnings.append(
                f"skipped unsupported file {entry.name!r} "
                f"(supported extensions: {', '.join(sorted(SUPPORTED_PAYLOAD_SUFFIXES))})"
            )
            continue
        document = _read_json_object(entry)
        identifiers = extract_identifiers(document)
        payloads.append(
            PayloadFile(
                filename=entry.name,
                payload=document,
                payload_id=identifiers.get("external_payload_id"),
            )
        )

    if not payloads:
        raise DatasetFolderError(
            f"dataset folder {dataset_id!r} contains no supported JSON payload files"
        )

    config_path = folder / CONFIG_FILENAME
    if not config_path.is_file():
        raise DatasetFolderError(
            f"dataset folder {dataset_id!r} is missing {CONFIG_FILENAME}"
        )
    config = _read_json_object(config_path)

    logger.info(
        "dataset_folder_read",
        dataset_id=dataset_id,
        payload_count=len(payloads),
        warning_count=len(warnings),
    )
    return DatasetFolderContents(
        dataset_id=dataset_id,
        folder=folder,
        payloads=payloads,
        config=config,
        warnings=warnings,
    )


def load_source_payload(
    *,
    dataset_id: str,
    payload_ref: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Re-read a payload file for ticket execution.

    If the file was deleted or moved after job creation, raise
    SourcePayloadMissingError so the ticket fails permanently rather than
    silently using a different configuration.
    """
    if not payload_ref or Path(payload_ref).name != payload_ref:
        raise SourcePayloadMissingError(f"source payload reference {payload_ref!r} is invalid")
    try:
        folder = resolve_dataset_dir(dataset_id, root=root)
    except DatasetNotFoundError as exc:
        raise SourcePayloadMissingError(
            f"dataset folder {dataset_id!r} is no longer available"
        ) from exc
    except DatasetFolderError as exc:
        raise SourcePayloadMissingError(str(exc)) from exc

    path = (folder / payload_ref).resolve()
    try:
        path.relative_to(folder)
    except ValueError as exc:
        raise SourcePayloadMissingError(
            f"source payload {payload_ref!r} escapes the dataset folder"
        ) from exc
    if not path.is_file():
        raise SourcePayloadMissingError(
            f"source payload {payload_ref!r} is missing from dataset {dataset_id!r}"
        )
    try:
        return _read_json_object(path)
    except PayloadValidationError as exc:
        raise SourcePayloadMissingError(str(exc)) from exc
