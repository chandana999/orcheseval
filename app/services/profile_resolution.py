"""Extract evaluation_profile_id from a payload and freeze mapped metrics.

The profile id is taken only from payload.agent_registry.evaluation_profile_id.
It is never inferred from producer, workflow, agent name, or spans.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg import Connection

from app.core.config import settings
from app.models.entities import MetricRecord
from app.models.enums import CheckType
from app.repositories.profile_repository import ProfileRepository
from app.services.metric_validation import MetricDefinitionError, normalize_metric_definition


class ProfileResolutionError(ValueError):
    status_code = 400


class ProfileNotFoundError(ProfileResolutionError):
    status_code = 404


class InactiveProfileError(ProfileResolutionError):
    status_code = 409


def extract_evaluation_profile_id(payload: dict[str, Any], *, source: str) -> str:
    """Read payload['agent_registry']['evaluation_profile_id']."""
    registry = payload.get("agent_registry")
    if registry is None:
        raise ProfileResolutionError(
            f"{source}: missing agent_registry; expected "
            "payload.agent_registry.evaluation_profile_id"
        )
    if not isinstance(registry, dict):
        raise ProfileResolutionError(
            f"{source}: agent_registry must be an object, got {type(registry).__name__}"
        )
    profile_id = registry.get("evaluation_profile_id")
    if profile_id is None or (isinstance(profile_id, str) and not profile_id.strip()):
        raise ProfileResolutionError(
            f"{source}: agent_registry.evaluation_profile_id is required and must be non-empty"
        )
    if not isinstance(profile_id, str):
        raise ProfileResolutionError(
            f"{source}: agent_registry.evaluation_profile_id must be a string"
        )
    return profile_id.strip()


def freeze_metric_snapshot(
    record: MetricRecord,
    *,
    evaluation_profile_id: str,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize and freeze the metric definition used for this ticket.

    Later changes to metric_records or the active version cannot affect retries:
    the ticket stores this snapshot and the runner reads it from the ticket row.
    """
    default_policy = {
        "priority": 0,
        "max_attempts": settings.runner_max_attempts,
        "on_missing_context": settings.default_on_missing_context,
    }
    if defaults:
        default_policy.update(defaults)

    definition = record.definition_payload
    if not isinstance(definition, dict):
        raise ProfileResolutionError(
            f"metric record {record.metric_record_id} has an invalid definition_payload"
        )

    # Allow a wrapped {"checks": [one check]} or a bare check object.
    if "checks" in definition and isinstance(definition.get("checks"), list):
        if len(definition["checks"]) != 1:
            raise ProfileResolutionError(
                f"metric record {record.metric_record_id} definition_payload.checks "
                "must contain exactly one check"
            )
        raw_check = dict(definition["checks"][0] or {})
    else:
        raw_check = dict(definition)

    if not raw_check.get("check_id") and not raw_check.get("id"):
        raw_check["check_id"] = record.metric_code or record.metric_id

    try:
        check = normalize_metric_definition(
            raw_check,
            where=f"metric {record.metric_id} v{record.metric_version_number}",
            defaults=default_policy,
        )
    except MetricDefinitionError as exc:
        raise ProfileResolutionError(
            f"metric record {record.metric_record_id} ({record.metric_id}) is invalid: {exc}"
        ) from exc

    declared_type = (record.metric_type or "").upper()
    if declared_type:
        try:
            CheckType(declared_type)
        except ValueError as exc:
            raise ProfileResolutionError(
                f"metric record {record.metric_record_id} has invalid metric_type "
                f"{record.metric_type!r}"
            ) from exc
        if declared_type != check["check_type"]:
            raise ProfileResolutionError(
                f"metric record {record.metric_record_id} metric_type {declared_type} "
                f"does not match evaluator '{check['evaluator']}' ({check['check_type']})"
            )

    snapshot = {
        "metric_record_id": str(record.metric_record_id),
        "metric_id": record.metric_id,
        "metric_code": record.metric_code,
        "metric_name": record.metric_name,
        "metric_desc": record.metric_desc,
        "metric_type": check["check_type"],
        "metric_version_number": record.metric_version_number,
        "definition_payload": check,
        "default_threshold_operator": record.default_threshold_operator,
        "llm_model_name": record.llm_model_name,
        "llm_model_version": record.llm_model_version,
        "llm_deployed_id": record.llm_deployed_id,
        "evaluation_profile_id": evaluation_profile_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }
    if record.llm_model_name or check["check_type"] == CheckType.LLM_JUDGE.value:
        snapshot["model"] = {
            "model": record.llm_model_name or settings.judge_model,
            "provider": settings.default_provider,
        }
    return snapshot


def load_profile_metrics(
    conn: Connection, evaluation_profile_id: str, *, source: str
) -> tuple[Any, list[MetricRecord], list[dict[str, Any]]]:
    """Load an active profile and freeze each mapped metric version."""
    profiles = ProfileRepository(conn)
    profile = profiles.get(evaluation_profile_id)
    if profile is None:
        raise ProfileNotFoundError(
            f"{source}: evaluation profile {evaluation_profile_id!r} was not found"
        )
    if not profile.is_active:
        raise InactiveProfileError(
            f"{source}: evaluation profile {evaluation_profile_id!r} is inactive"
        )

    records = profiles.list_metric_records(profile.id)
    if not records:
        raise ProfileResolutionError(
            f"{source}: evaluation profile {evaluation_profile_id!r} has no mapped metrics"
        )

    snapshots: list[dict[str, Any]] = []
    seen_records: set[str] = set()
    for record in records:
        key = str(record.metric_record_id)
        if key in seen_records:
            raise ProfileResolutionError(
                f"{source}: duplicate metric mapping {record.metric_id} on profile "
                f"{evaluation_profile_id!r}"
            )
        seen_records.add(key)
        snapshots.append(
            freeze_metric_snapshot(record, evaluation_profile_id=evaluation_profile_id)
        )
    return profile, records, snapshots


def check_from_snapshot(metric_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the frozen check definition stored on a ticket."""
    if not metric_snapshot:
        return None
    definition = metric_snapshot.get("definition_payload")
    if isinstance(definition, dict) and definition.get("check_id"):
        return definition
    if metric_snapshot.get("check_id"):
        return metric_snapshot
    return None
