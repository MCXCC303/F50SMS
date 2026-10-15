# -*- coding: utf-8 -*-
"""Tests for relative time wording."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from f50_sms.humanize import humanize_delta, humanize_seconds

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)


def ago(**kwargs):
    return humanize_delta(NOW - timedelta(**kwargs), now=NOW)


class HumanizeDeltaTests(unittest.TestCase):
    def test_none_is_empty(self):
        self.assertEqual(humanize_delta(None), "")

    def test_just_now(self):
        self.assertEqual(ago(seconds=0), "just now")
        self.assertEqual(ago(seconds=59), "just now")

    def test_minutes(self):
        self.assertEqual(ago(minutes=1), "about 1 minute ago")
        self.assertEqual(ago(minutes=14), "about 14 minutes ago")

    def test_hours(self):
        self.assertEqual(ago(hours=1), "about 1 hour ago")
        self.assertEqual(ago(hours=3), "about 3 hours ago")

    def test_days(self):
        self.assertEqual(ago(days=1), "about 1 day ago")
        self.assertEqual(ago(days=5), "about 5 days ago")

    def test_weeks(self):
        self.assertEqual(ago(weeks=2), "about 2 weeks ago")

    def test_months(self):
        self.assertEqual(ago(days=90), "about 3 months ago")

    def test_years(self):
        self.assertEqual(ago(days=800), "about 2 years ago")

    def test_future(self):
        self.assertEqual(humanize_delta(NOW + timedelta(minutes=14), now=NOW), "in about 14 minutes")
        self.assertEqual(humanize_delta(NOW + timedelta(seconds=5), now=NOW), "in a moment")

    def test_naive_timestamp_is_accepted(self):
        naive_now = datetime(2026, 10, 15, 12, 0, 0)
        moment = naive_now - timedelta(hours=2)
        self.assertEqual(humanize_delta(moment, now=naive_now), "about 2 hours ago")


class HumanizeSecondsTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(humanize_seconds(45), "45s")

    def test_minutes(self):
        self.assertEqual(humanize_seconds(65), "1m 05s")

    def test_hours(self):
        self.assertEqual(humanize_seconds(3900), "1h 05m")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
