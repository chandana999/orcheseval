import logging
import sys

import structlog

from app.core.config import settings
from app.core.redaction import redact_secrets


def _standard_context(_logger, _method, event_dict: dict) -> dict:
    event_dict.setdefault("service", "eval-platform")
    event_dict.setdefault("environment", settings.app_env)
    return event_dict


def _redact_event(_logger, _method, event_dict: dict) -> dict:
    """Redact credential-shaped strings, including rendered exception text."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact_secrets(value)
    return event_dict


def setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _standard_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_event,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
