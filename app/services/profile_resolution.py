"""Match a payload to checks in the dataset config file.

The dataset folder holds payload JSON files plus config.json. A payload is
matched by payload.agent_registry.agent_id to an agent entry in that file.
The matched checks are frozen onto each ticket.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.models.enums import CheckType
from app.services.metric_validation import MetricDefinitionError, normalize_metric_definition


class ProfileResolutionError(ValueError):
    status_code = 400


class ProfileNotFoundError(ProfileResolutionError):
    status_code = 404


def extract_agent_id(payload: dict[str, Any], *, source: str) -> str:
    """Read payload['agent_registry']['agent_id']."""
    registry = payload.get("agent_registry")
    if registry is None:
        raise ProfileResolutionError(
            f"{source}: missing agent_registry; expected payload.agent_registry.agent_id"
        )
    if not isinstance(registry, dict):
        raise ProfileResolutionError(
            f"{source}: agent_registry must be an object, got {type(registry).__name__}"
        )
    agent_id = registry.get("agent_id")
    if agent_id is None or (isinstance(agent_id, str) and not agent_id.strip()):
        raise ProfileResolutionError(
            f"{source}: agent_registry.agent_id is required and must be non-empty"
        )
    if not isinstance(agent_id, str):
        raise ProfileResolutionError(f"{source}: agent_registry.agent_id must be a string")
    return agent_id.strip()


def freeze_check(check: dict[str, Any], *, agent_id: str) -> dict[str, Any]:
    """Normalize one config check and freeze it for a ticket."""
    defaults = {
        "priority": 0,
        "max_attempts": settings.runner_max_attempts,
        "on_missing_context": settings.default_on_missing_context,
    }
    check_id = str(check.get("check_id") or check.get("id") or "").strip() or "check"
    try:
        normalized = normalize_metric_definition(
            dict(check),
            where=f"config.json agent {agent_id} check {check_id}",
            defaults=defaults,
        )
    except MetricDefinitionError as exc:
        raise ProfileResolutionError(
            f"config.json agent {agent_id!r} check {check_id!r} is invalid: {exc}"
        ) from exc

    metric_record_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"eval-platform:{agent_id}:{normalized['check_id']}"
    )
    snapshot = {
        "metric_record_id": str(metric_record_id),
        "metric_id": normalized["check_id"],
        "metric_code": normalized["check_id"],
        "metric_name": normalized["check_id"],
        "metric_desc": normalized.get("description"),
        "metric_type": normalized["check_type"],
        "metric_version_number": 1,
        "definition_payload": normalized,
        "agent_id": agent_id,
        "evaluation_profile_id": agent_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }
    if normalized["check_type"] == CheckType.LLM_JUDGE.value:
        snapshot["model"] = {
            "model": settings.judge_model,
            "provider": settings.default_provider,
        }
    return snapshot


def checks_for_agent(
    config: dict[str, Any], agent_id: str, *, source: str
) -> list[dict[str, Any]]:
    """Return frozen checks for the config.json entry with this agent_id."""
    agents = config.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ProfileResolutionError(f"{source}: config.json must contain a non-empty agents list")

    seen: set[str] = set()
    matched: dict[str, Any] | None = None
    for entry in agents:
        if not isinstance(entry, dict):
            raise ProfileResolutionError(f"{source}: each config.json agents entry must be an object")
        entry_id = entry.get("agent_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ProfileResolutionError(
                f"{source}: each config.json agent requires a non-empty agent_id"
            )
        key = entry_id.strip()
        if key in seen:
            raise ProfileResolutionError(f"{source}: config.json lists agent_id {key!r} more than once")
        seen.add(key)
        if key == agent_id:
            matched = entry

    if matched is None:
        raise ProfileNotFoundError(
            f"{source}: agent_id {agent_id!r} was not found in config.json"
        )

    raw_checks = matched.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ProfileResolutionError(
            f"{source}: config.json agent {agent_id!r} has no checks"
        )

    snapshots: list[dict[str, Any]] = []
    seen_checks: set[str] = set()
    for raw in raw_checks:
        if not isinstance(raw, dict):
            raise ProfileResolutionError(
                f"{source}: checks for agent {agent_id!r} must be objects"
            )
        frozen = freeze_check(raw, agent_id=agent_id)
        check_id = frozen["definition_payload"]["check_id"]
        if check_id in seen_checks:
            raise ProfileResolutionError(
                f"{source}: duplicate check_id {check_id!r} for agent {agent_id!r}"
            )
        seen_checks.add(check_id)
        snapshots.append(frozen)
    return snapshots


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
