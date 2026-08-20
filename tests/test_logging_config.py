"""Tests for the CLI's local file logging (logging_config.py)."""

from __future__ import annotations

import logging

from pydagy_research.logging_config import configure_file_logging


def _remove_all_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_configure_file_logging_writes_to_requested_file(tmp_path):
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    _remove_all_handlers(root)
    try:
        log_file = tmp_path / "sub" / "run.log"
        resolved = configure_file_logging(log_file, level=logging.INFO)

        assert resolved == log_file.resolve()
        assert resolved.exists()  # FileHandler creates parent-relative file eagerly

        logging.getLogger("pydagy_research.somewhere").info("hello from a test")

        contents = resolved.read_text()
        assert "hello from a test" in contents
        assert "pydagy_research.somewhere" in contents
    finally:
        _remove_all_handlers(root)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)


def test_configure_file_logging_defaults_to_cwd_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    _remove_all_handlers(root)
    try:
        resolved = configure_file_logging()
        assert resolved == (tmp_path / "pydagy-research.log").resolve()
    finally:
        _remove_all_handlers(root)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)


def test_configure_file_logging_reads_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("PYDAGY_RESEARCH_LOG_FILE", str(tmp_path / "env.log"))
    monkeypatch.setenv("PYDAGY_RESEARCH_LOG_LEVEL", "WARNING")
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    _remove_all_handlers(root)
    try:
        resolved = configure_file_logging()
        assert resolved == (tmp_path / "env.log").resolve()
        assert root.level == logging.WARNING
    finally:
        _remove_all_handlers(root)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
