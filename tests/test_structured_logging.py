"""Tests for the structured logging module."""
from __future__ import annotations

import io
import json
import logging
import unittest

from backend.services.structured_logging import (
    JsonFormatter,
    TaskLogAdapter,
    configure_structured_logging,
)


class TestJsonFormatter(unittest.TestCase):
    def setUp(self):
        self.handler = logging.StreamHandler(io.StringIO())
        self.handler.setFormatter(JsonFormatter())
        self.logger = logging.getLogger("test.json_formatter")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers = [self.handler]
        self.logger.propagate = False

    def _output(self):
        return self.handler.stream.getvalue().strip()

    def test_basic_record_has_required_fields(self):
        self.logger.info("hello world")
        record = json.loads(self._output())
        self.assertIn("ts", record)
        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["logger"], "test.json_formatter")
        self.assertEqual(record["msg"], "hello world")

    def test_extra_fields_are_preserved(self):
        self.logger.info("task queued", extra={"task_id": "abc", "providers": 2})
        record = json.loads(self._output())
        self.assertEqual(record["task_id"], "abc")
        self.assertEqual(record["providers"], 2)
        self.assertEqual(record["msg"], "task queued")

    def test_exception_info_included(self):
        try:
            raise ValueError("test error")
        except ValueError:
            self.logger.exception("something failed")
        record = json.loads(self._output())
        self.assertIn("exc", record)
        self.assertIn("ValueError", record["exc"])
        self.assertIn("test error", record["exc"])

    def test_message_formatting_with_args(self):
        self.logger.info("task %s status=%s", "abc", "processing")
        record = json.loads(self._output())
        self.assertEqual(record["msg"], "task abc status=processing")


class TestTaskLogAdapter(unittest.TestCase):
    def setUp(self):
        self.handler = logging.StreamHandler(io.StringIO())
        self.handler.setFormatter(JsonFormatter())
        self.base_logger = logging.getLogger("test.adapter")
        self.base_logger.setLevel(logging.DEBUG)
        self.base_logger.handlers = [self.handler]
        self.base_logger.propagate = False

    def _output(self):
        return self.handler.stream.getvalue().strip()

    def test_adapter_injects_context_fields(self):
        adapter = TaskLogAdapter(self.base_logger, {"task_id": "t1", "history_id": "h1"})
        adapter.info("task queued")
        record = json.loads(self._output())
        self.assertEqual(record["task_id"], "t1")
        self.assertEqual(record["history_id"], "h1")
        self.assertEqual(record["msg"], "task queued")

    def test_adapter_extra_overrides_adapter_context(self):
        adapter = TaskLogAdapter(self.base_logger, {"task_id": "t1"})
        adapter.info("retry", extra={"task_id": "t2", "attempt": 3})
        record = json.loads(self._output())
        self.assertEqual(record["task_id"], "t2")
        self.assertEqual(record["attempt"], 3)

    def test_adapter_without_extra_works(self):
        adapter = TaskLogAdapter(self.base_logger, {})
        adapter.warning("no context")
        record = json.loads(self._output())
        self.assertEqual(record["msg"], "no context")
        self.assertEqual(record["level"], "WARNING")


class TestConfigureStructuredLogging(unittest.TestCase):
    def test_sets_json_formatter_on_root(self):
        original_handlers = logging.getLogger().handlers[:]
        original_level = logging.getLogger().level
        try:
            configure_structured_logging(logging.DEBUG)
            root = logging.getLogger()
            self.assertEqual(root.level, logging.DEBUG)
            self.assertTrue(len(root.handlers) >= 1)
            handler = root.handlers[0]
            self.assertIsInstance(handler.formatter, JsonFormatter)
        finally:
            logging.getLogger().handlers = original_handlers
            logging.getLogger().setLevel(original_level)

    def test_replaces_existing_handlers(self):
        logging.getLogger().addHandler(logging.NullHandler())
        original_handlers = logging.getLogger().handlers[:]
        original_level = logging.getLogger().level
        try:
            configure_structured_logging()
            root = logging.getLogger()
            # All previous handlers removed, exactly one new handler.
            self.assertEqual(len(root.handlers), 1)
            self.assertIsInstance(root.handlers[0].formatter, JsonFormatter)
        finally:
            logging.getLogger().handlers = original_handlers
            logging.getLogger().setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
