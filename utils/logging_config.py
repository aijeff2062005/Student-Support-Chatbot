"""Centralized logging configuration with optional color support."""

from __future__ import annotations

import logging
from typing import Any

colorlog: Any | None = None
try:
    import colorlog as _colorlog

    colorlog = _colorlog
except ImportError:  # pragma: no cover - fallback when colorlog is not installed
    pass

_DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_COLOR_LOG_FORMAT = "%(log_color)s%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def _resolve_level(level: str | None) -> int:
    if level is None:
        try:
            from configs.config_service import get_settings

            level = get_settings().log_level
        except Exception:
            level = "INFO"

    value = level.upper()
    return getattr(logging, value, logging.INFO)


def _build_formatter(use_color: bool) -> logging.Formatter:
    if use_color and colorlog is not None:
        return colorlog.ColoredFormatter(
            _COLOR_LOG_FORMAT,
            datefmt=_DATE_FORMAT,
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    return logging.Formatter(_DEFAULT_LOG_FORMAT, datefmt=_DATE_FORMAT)


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once for the entire application."""
    global _CONFIGURED

    root = logging.getLogger()
    root.setLevel(_resolve_level(level))

    if _CONFIGURED:
        return

    try:
        from configs.config_service import get_settings

        use_color = bool(get_settings().log_color)
    except Exception:
        use_color = True
    formatter = _build_formatter(use_color=use_color)

    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    else:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Get logger after ensuring centralized configuration is active."""
    configure_logging()
    return logging.getLogger(name)


def short_id(value: Any, keep: int = 8) -> str:
    """Compact long identifiers for readable logs."""
    text = str(value or "")
    if len(text) <= keep:
        return text
    return f"{text[:keep]}..."


def truncate_text(value: Any, max_len: int = 120) -> str:
    """Truncate long text payloads to keep logs concise."""
    text = str(value or "")
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}..."


def log_event(logger: logging.Logger, level: str, event: str, **context: Any) -> None:
    """Emit a structured event log line with stable key ordering."""
    parts = [f"event={event}"]
    for key in sorted(context):
        value = context[key]
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list, tuple, set)):
            value = truncate_text(value, max_len=240)
        parts.append(f"{key}={value}")

    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn(" | ".join(parts))
