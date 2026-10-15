# -*- coding: utf-8 -*-
"""Tests for the git-style message identity (content hash and ref lookup)."""
from __future__ import annotations

import base64
import unittest

from f50_sms.models import Message, resolve_ref


def make(message_id="1", number="10010", content="hello", date="26,01,01,10,00,00,+0800", tag="0"):
    payload = {
        "id": message_id,
        "number": number,
        "content": base64.b64encode(content.encode()).decode(),
        "date": date,
        "tag": tag,
        "draft_group_id": "",
    }
    return Message.from_api(payload)


class DigestTests(unittest.TestCase):
    def test_short_id_is_seven_hex_chars(self):
        short = make().short_id
        self.assertEqual(len(short), 7)
        int(short, 16)  # must be valid hex

    def test_digest_is_stable_for_same_content(self):
        a = make(message_id="1")
        b = make(message_id="2")  # different router id, same content
        self.assertEqual(a.digest, b.digest)

    def test_digest_changes_with_content(self):
        self.assertNotEqual(make(content="a").digest, make(content="b").digest)

    def test_digest_changes_with_number(self):
        self.assertNotEqual(make(number="10010").digest, make(number="10011").digest)

    def test_short_id_is_prefix_of_digest(self):
        m = make()
        self.assertTrue(m.digest.startswith(m.short_id))


class RefTests(unittest.TestCase):
    def test_matches_exact_short_id(self):
        m = make()
        self.assertTrue(m.matches_ref(m.short_id))

    def test_matches_prefix(self):
        m = make()
        self.assertTrue(m.matches_ref(m.short_id[:4]))

    def test_matches_hash_padded(self):
        m = make()
        self.assertTrue(m.matches_ref("#" + m.short_id))

    def test_matches_numeric_id(self):
        m = make(message_id="57")
        self.assertTrue(m.matches_ref("57"))

    def test_rejects_unrelated(self):
        self.assertFalse(make().matches_ref("zzzzzzz"))


class ResolveRefTests(unittest.TestCase):
    def test_resolves_unique(self):
        m = make()
        hits = resolve_ref([make(message_id="2", content="other"), m], m.short_id)
        self.assertEqual([h.id for h in hits], [m.id])

    def test_resolves_nothing(self):
        self.assertEqual(resolve_ref([make()], "zzzzzz"), [])

    def test_empty_ref(self):
        self.assertEqual(resolve_ref([make()], ""), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
