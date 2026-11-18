import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_telegram.migration import _normalized_path, migrate_conversation
from codex_telegram.rpc import RPCError
from codex_telegram.store import Store


class MigrationRPC:
    def __init__(self,workdir):
        self.calls=[]
        self.workdir=workdir
        self.new_id='private-thread'
        self.turns=[{'id':'completed-own-turn','status':'completed'}]
        self.error=None

    def request(self,method,params,timeout=45):
        self.calls.append((method,params,timeout))
        if self.error: raise self.error
        if method=='thread/fork':return {'thread':{'id':self.new_id,'cwd':str(self.workdir)}}
        if method=='thread/turns/list':return {'data':self.turns}
        raise AssertionError('Migration must not call '+method)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('CODEX_TG_TEST_TMPDIR'))
        self.root=Path(self.temp.name)
        self.source=self.root/'desktop'
        self.source.mkdir()
        self.project=self.root/'project';self.project.mkdir()
        self.config=SimpleNamespace(codex_home=self.root/'private'/'codex-home',workdir=self.project,
                                    model='synthetic-model',effort='high',approval_policy='on-request',reviewer='user')
        self.config.codex_home.mkdir(parents=True)
        self.store=Store(self.root/'bridge.sqlite3')
        self.store.set('codex_thread','owned-thread')
        self.rpc=MigrationRPC(self.project)
        self.rollout=self.source/'sessions'/'2026'/'11'/'18'/'rollout-owned-thread.jsonl'
        self.rollout.parent.mkdir(parents=True)
        self.header={'type':'session_meta','payload':{'id':'owned-thread','cwd':str(self.project),
                                                    'originator':'codex_telegram_bridge'}}
        self.rollout.write_text(json.dumps(self.header)+'\n'+json.dumps({'type':'response_item','payload':{'text':'synthetic-history'}})+'\n',encoding='utf-8')
        self.index=sqlite3.connect(self.source/'state_5.sqlite')
        self.index.execute('CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,originator TEXT)')
        self.index.execute('INSERT INTO threads VALUES(?,?,?,?)',('owned-thread',str(self.rollout),str(self.project),'codex_telegram_bridge'))
        self.index.commit()
        old=self.store.enqueue_job(json.dumps({'kind':'chat','text':'synthetic'}),'own-completed')
        self.store.update_job(old,status='completed',thread_id='owned-thread',turn_id='completed-own-turn',finished=1)

    def tearDown(self):
        self.index.close();self.store.close();self.temp.cleanup()

    def update_index(self,**fields):
        for name,value in fields.items():self.index.execute('UPDATE threads SET '+name+'=?',(value,))
        self.index.commit()

    def test_only_own_rollout_is_copied_and_forked_without_model_execution(self):
        unrelated=self.source/'sessions'/'unrelated.jsonl';unrelated.write_text('private-desktop-history')
        before=hashlib.sha256(self.rollout.read_bytes()).hexdigest()
        result=migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(result,'private-thread')
        self.assertEqual(self.store.get('codex_thread'),'private-thread')
        self.assertEqual(self.store.get('codex_home'),str(self.config.codex_home.resolve()))
        imported=list((self.config.codex_home/'sessions/imported').iterdir())
        self.assertEqual(len(imported),1)
        self.assertEqual(imported[0].read_bytes(),self.rollout.read_bytes())
        self.assertEqual(hashlib.sha256(self.rollout.read_bytes()).hexdigest(),before)
        method,params,timeout=self.rpc.calls[0]
        self.assertEqual(method,'thread/fork')
        self.assertEqual(Path(params['path']),imported[0])
        self.assertEqual(params['lastTurnId'],'completed-own-turn')
        self.assertTrue(params['excludeTurns'])
        self.assertTrue(params['deferGoalContinuation'])
        self.assertEqual(timeout,180)
        self.assertEqual(self.index.execute('SELECT id FROM threads').fetchall(),[('owned-thread',)])

    def test_marker_is_idempotent(self):
        migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.rpc.calls.clear()
        self.assertEqual(migrate_conversation(self.config,self.store,self.rpc,self.source),'private-thread')
        self.assertEqual(self.rpc.calls,[])

    def test_no_conversation_only_initializes_private_namespace(self):
        self.store.set('codex_thread',None)
        self.assertIsNone(migrate_conversation(self.config,self.store,self.rpc,self.source))
        self.assertEqual(self.store.get('codex_home'),str(self.config.codex_home.resolve()))
        self.assertEqual(self.rpc.calls,[])

    def test_foreign_project_or_originator_is_rejected(self):
        for change in ({'cwd':str(self.root/'other-project')},{'originator':'desktop-client'}):
            with self.subTest(change=change):
                self.update_index(cwd=str(self.project),originator='codex_telegram_bridge',**{})
                self.update_index(**change)
                with self.assertRaises(RuntimeError):migrate_conversation(self.config,self.store,self.rpc,self.source)
                self.assertEqual(self.store.get('codex_thread'),'owned-thread')
                self.assertEqual(self.rpc.calls,[])

    def test_outside_rollout_path_is_rejected(self):
        outside=self.root/'research-data.jsonl';outside.write_bytes(self.rollout.read_bytes())
        self.update_index(rollout_path=str(outside))
        with self.assertRaisesRegex(RuntimeError,'outside'):
            migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(self.rpc.calls,[])

    def test_header_mismatch_does_not_reach_native_rpc(self):
        self.header['payload']['originator']='desktop-client'
        self.rollout.write_text(json.dumps(self.header)+'\n')
        with self.assertRaisesRegex(RuntimeError,'metadata'):
            migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(self.rpc.calls,[])
        self.assertIsNone(self.store.get('codex_home'))

    def test_archived_bridge_rollout_is_a_valid_migration_source(self):
        archived=self.source/'archived_sessions'/self.rollout.name
        archived.parent.mkdir()
        archived.write_bytes(self.rollout.read_bytes())
        self.update_index(rollout_path=str(archived))
        self.assertEqual(migrate_conversation(self.config,self.store,self.rpc,self.source),'private-thread')

    def test_audit_failure_rolls_back_namespace_and_conversation_together(self):
        with patch.object(self.store,'audit',side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaises(RuntimeError):
                migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(self.store.get('codex_thread'),'owned-thread')
        self.assertIsNone(self.store.get('codex_home'))

    def test_timeout_preserves_original_state_and_has_no_success_audit(self):
        self.rpc.error=RPCError('Codex response timeout: thread/fork')
        with self.assertRaisesRegex(RuntimeError,'no model turn was executed'):
            migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(self.store.get('codex_thread'),'owned-thread')
        self.assertIsNone(self.store.get('codex_home'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM audit').fetchone()[0],0)

    def test_empty_native_history_or_unchanged_id_is_rejected(self):
        for empty,same in ((True,False),(False,True)):
            with self.subTest(empty=empty,same=same):
                self.rpc.new_id='owned-thread' if same else 'private-thread'
                self.rpc.turns=[] if empty else [{'id':'completed-own-turn'}]
                with self.assertRaises(RuntimeError):migrate_conversation(self.config,self.store,self.rpc,self.source)
                self.assertEqual(self.store.get('codex_thread'),'owned-thread')
                self.assertIsNone(self.store.get('codex_home'))

    def test_newer_foreign_turn_cannot_extend_selected_bridge_history(self):
        foreign=self.store.enqueue_job(json.dumps({'kind':'chat','text':'synthetic-foreign'}),'other-thread')
        self.store.update_job(foreign,status='completed',thread_id='unrelated-thread',turn_id='newer-foreign-turn',finished=100)
        migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(self.rpc.calls[0][1]['lastTurnId'],'completed-own-turn')

    def test_native_boundary_must_match_selected_completed_turn(self):
        self.rpc.turns=[{'id':'newer-desktop-turn','status':'completed'}]
        with self.assertRaises(RuntimeError):migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(self.store.get('codex_thread'),'owned-thread')

    def test_selected_model_and_reviewer_are_preserved_with_noninteractive_policy(self):
        self.store.set('model','selected-model');self.store.set('effort','max')
        self.config.reviewer='auto_review'
        migrate_conversation(self.config,self.store,self.rpc,self.source)
        params=self.rpc.calls[0][1]
        self.assertEqual(params['model'],'selected-model')
        self.assertEqual(params['config']['model_reasoning_effort'],'max')
        self.assertEqual(params['approvalsReviewer'],'auto_review')
        self.assertEqual(params['approvalPolicy'],'never')

    def test_extended_windows_cwd_prefix_is_normalized(self):
        if os.name!='nt':self.skipTest('Windows path normalization')
        self.assertEqual(_normalized_path('\\\\?\\'+str(self.project)),_normalized_path(self.project))

    def test_reparse_rollout_is_rejected_without_symlink_privilege(self):
        with patch('codex_telegram.migration._reparse',side_effect=lambda path:path==self.rollout):
            with self.assertRaisesRegex(RuntimeError,'symlinks'):
                migrate_conversation(self.config,self.store,self.rpc,self.source)
        self.assertEqual(self.rpc.calls,[])
