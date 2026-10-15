# -*- coding: utf-8 -*-
"""Tests for settings resolution and the config file."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from f50_sms import config
from f50_sms.client import DEFAULT_HOST, DEFAULT_TIMEOUT


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(config.load_config(str(self.path)), {})

    def test_broken_file_reads_as_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(config.load_config(str(self.path)), {})

    def test_unknown_keys_are_dropped(self):
        self.path.write_text(json.dumps({"host": "http://x", "bogus": 1}), encoding="utf-8")
        self.assertEqual(config.load_config(str(self.path)), {"host": "http://x"})

    def test_save_merges_by_default(self):
        config.save_config({"host": "http://one"}, str(self.path))
        config.save_config({"timeout": 5}, str(self.path))
        stored = config.load_config(str(self.path))
        self.assertEqual(stored["host"], "http://one")
        self.assertEqual(stored["timeout"], 5)

    def test_save_can_replace_and_therefore_drop_keys(self):
        config.save_config({"host": "http://one", "password": "secret"}, str(self.path))
        config.save_config({"host": "http://one"}, str(self.path), replace=True)
        stored = config.load_config(str(self.path))
        self.assertEqual(stored, {"host": "http://one"})

    def test_file_is_private(self):
        config.save_config({"password": "secret"}, str(self.path))
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)


class ConfigPathTests(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(config.config_path("/tmp/explicit.json"), Path("/tmp/explicit.json"))

    def test_xdg_config_home_is_respected(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}, clear=False):
            self.assertEqual(
                config.config_path(), Path("/tmp/xdg/f50-sms/config.json"),
            )


class ResolveSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, **kwargs):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("F50_SMS_HOST", "F50_SMS_PASSWORD", "F50_SMS_TIMEOUT")}
        with mock.patch.dict(os.environ, clean, clear=True):
            return config.resolve_settings(config_file=str(self.path), **kwargs)

    def test_defaults(self):
        settings = self._resolve()
        self.assertEqual(settings.host, DEFAULT_HOST)
        self.assertEqual(settings.timeout, DEFAULT_TIMEOUT)
        self.assertIsNone(settings.password)

    def test_file_beats_defaults(self):
        self.path.write_text(json.dumps({"host": "http://file", "timeout": 3}), encoding="utf-8")
        settings = self._resolve()
        self.assertEqual(settings.host, "http://file")
        self.assertEqual(settings.timeout, 3)

    def test_environment_beats_file(self):
        self.path.write_text(json.dumps({"host": "http://file"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"F50_SMS_HOST": "http://env"}, clear=False):
            settings = config.resolve_settings(config_file=str(self.path))
        self.assertEqual(settings.host, "http://env")

    def test_arguments_beat_everything(self):
        self.path.write_text(json.dumps({"host": "http://file"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"F50_SMS_HOST": "http://env"}, clear=False):
            settings = config.resolve_settings(config_file=str(self.path), host="http://arg")
        self.assertEqual(settings.host, "http://arg")

    def test_sources_are_recorded(self):
        settings = self._resolve(host="http://arg")
        self.assertIn("command line", settings.sources["host"])
        self.assertIn("built-in", settings.sources["timeout"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
