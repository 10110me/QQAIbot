import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from osa.infrastructure.v2.registry import Registry as V2Registry
from osa.infrastructure.v3.registry import Registry


class RegistryRegressions(unittest.TestCase):
    def test_requirement_commands_follow_submission_order_not_random_uuid_order(self):
        db = V2Registry(':memory:')
        task = db.create('u', None, 'x', 'one')
        db.command(task['id'], 'admin', None, 'APPROVE', {}, 1, 'approve', admin=True)
        task = db.claim('w')
        with patch('osa.infrastructure.v2.registry.uuid.uuid4', side_effect=[SimpleNamespace(hex='zz-last'), SimpleNamespace(hex='aa-first')]):
            first = db.command(task['id'], 'u', None, 'ADD_REQUIREMENT', {'text': 'first change'}, task['version'], 'one-change')
            db.command(task['id'], 'u', None, 'ADD_REQUIREMENT', {'text': 'second change'}, first['version'], 'two-change')
        updated = db.boundary(task['id'], task['generation'])
        self.assertEqual(updated['requirement_log'], ['first change', 'second change'])

    def test_v2_upgrade_backfills_projection_without_changing_private_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'registry.db')
            old = V2Registry(path)
            task = old.create('owner', '10001', 'private requirement', 'one')
            new = Registry(path)
            self.assertEqual(new.get(task['id'])['requirement'], 'private requirement')
            with new.tx() as db:
                projection = dict(db.execute('SELECT * FROM agent_tasks_v3 WHERE id=?', (task['id'],)).fetchone())
            self.assertEqual(projection['origin_group_id'], '10001')
            self.assertEqual(projection['owner_user_id'], 'owner')
            with self.assertRaises(PermissionError):
                new.visible(task['id'], 'owner', '10002')

    def test_approved_public_claim_is_sendable_and_its_revocation_invalidates_hash(self):
        db = Registry(':memory:')
        rid = db.record('public_claim', 'public', None, {'summary': 'reviewed public technical fact'}, 'claim', 'supported_with_limitations')
        row = db.records('public_claim', admin=True)[0]
        ref = {'id': rid, 'state': row['state'], 'hash': hashlib.sha256(json.dumps(row['payload'], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
        self.assertTrue(db.valid_experience([ref], 'actor', '10001'))
        db.update_record(rid, 'revoked', {})
        self.assertFalse(db.valid_experience([ref], 'actor', '10001'))
