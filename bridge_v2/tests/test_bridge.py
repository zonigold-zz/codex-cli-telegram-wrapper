import io
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests

from codex_telegram.config import Config, Redactor, load_env, project_environment
from codex_telegram.engine import BRIDGE_INSTRUCTIONS, Engine
from codex_telegram.policy import plan_hash, routine_question, validate_plan
from codex_telegram.rpc import AppServer, RPCError
from codex_telegram.runtime import InstanceLock, bootstrap_topic
from codex_telegram.store import Store
from codex_telegram.telegram import Sender, Telegram, TelegramError, chunks


class FakeRPC:
    def __init__(self):
        self.calls, self.responses = [], []
        self.events = queue.Queue()
        self.generation, self.alive = 1, True
        self.check_exit, self.on_start = 0, None

    def start(self):
        self.alive = True

    def request(self, method, params, timeout=45):
        self.calls.append((method, params))
        if method == 'model/list':
            return {'data': [{'id': name, 'model': name, 'displayName': name,
                    'supportedReasoningEfforts': [{'reasoningEffort': x} for x in
                        (('low', 'medium', 'high', 'max', 'ultra') if name == 'model-a' else ('medium', 'high'))],
                    'defaultReasoningEffort': 'medium', 'isDefault': name == 'model-a'}
                    for name in ('model-a', 'model-b')]}
        if method == 'thread/fork':
            return {'thread': {'id': 'fork-1'}}
        if method in ('thread/start', 'thread/resume'):
            return {'thread': {'id': params.get('threadId', 'thread-1')}}
        if method == 'thread/read':
            return {'thread': {'id': params['threadId'], 'cwd': self.cwd}}
        if method == 'turn/start':
            if self.on_start:
                self.on_start()
            return {'turn': {'id': 'turn-1'}}
        if method == 'command/exec':
            return {'exitCode': self.check_exit, 'stdout': 'verification', 'stderr': ''}
        return {}

    def respond(self, request_id, result, generation):
        if generation != self.generation:
            raise RPCError('stale generation')
        self.responses.append((request_id, result))

    def reject(self, *args):
        self.responses.append(('rejected', args))

    def close(self):
        self.alive = False


class FakeSender:
    def __init__(self):
        self.sent = []
        self.edits = []

    def send(self, text, **kwargs):
        self.sent.append((text, kwargs))

    def edit(self, *args):
        self.edits.append(args)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('CODEX_TG_TEST_TMPDIR'))
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state.sqlite3')
        self.config = Config(self.root, self.root / 'state', '1234567890:synthetic-token-value-abcdefghijk',
                             7, -10, model='model-a')
        self.redact = Redactor({'TELEGRAM_BOT_TOKEN': self.config.token, 'CODEX_API_KEY': 'synthetic-key-12345'})
        self.rpc, self.sender = FakeRPC(), FakeSender()
        self.rpc.cwd = str(self.root)
        self.engine = Engine(self.config, self.store, self.rpc, self.sender, self.redact)
        self.store.set('telegram_topic', 55)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def update(self, text, number=1, user=7, chat=-10, topic=55):
        return {'update_id': number, 'message': {'from': {'id': user}, 'chat': {'id': chat},
                                                'message_thread_id': topic, 'text': text}}

    def plan(self, steps=1):
        return {'goal': 'synthetic goal', 'scope': 'existing local authorization only',
                'steps': [{'title': f'step-{i}', 'instructions': 'perform the approved step',
                           'checks': [['python', '-c', 'print(1)']]} for i in range(steps)]}

    def approved(self, steps=1):
        plan = self.plan(steps)
        self.store.set('plan', plan)
        self.store.set('approved_plan_hash', plan_hash(plan))
        self.store.set('auto_mode', 'running')
        self.store.set('auto_nonce', 'test')
        self.store.set('auto_turns', 0)
        return plan

    def test_inbox_is_durable_before_offset(self):
        self.store.ingest([self.update('one', 8), self.update('two', 9)])
        self.assertEqual(self.store.get('offset'), 10)
        self.assertEqual(len(self.store.pending_inbox()), 2)
        self.store.ingest([self.update('one', 8)])
        self.assertEqual(len(self.store.pending_inbox()), 2)

    def test_inbox_transaction_rolls_back_offset_and_payload(self):
        with self.assertRaises(KeyError):
            self.store.ingest([self.update('one', 8), {'bad': 1}])
        self.assertEqual(self.store.get('offset', 0), 0)
        self.assertEqual(self.store.pending_inbox(), [])

    def test_duplicate_prompt_does_not_duplicate_job(self):
        self.engine.handle_update(self.update('one'))
        self.engine.handle_update(self.update('one'))
        self.assertIsNotNone(self.store.next_job())
        self.assertIsNone(self.store.next_job())

    def test_allowlist_user_chat_and_topic(self):
        for args in ({'user': 8}, {'chat': -11}, {'topic': 56}):
            self.engine.handle_update(self.update('bad', **args))
        self.assertIsNone(self.store.next_job())
        self.engine.handle_update(self.update('good'))
        self.assertIsNotNone(self.store.next_job())

    def test_long_prompt_persists_across_engine_recreation(self):
        self.engine.handle_update(self.update('/prompt_begin'))
        self.engine.handle_update(self.update('one', 2))
        engine = Engine(self.config, self.store, self.rpc, self.sender, self.redact)
        engine.handle_update(self.update('two', 3))
        engine.handle_update(self.update('/prompt_end', 4))
        self.assertEqual(json.loads(self.store.next_job()['prompt'])['text'], 'one\ntwo')

    def test_commands_are_not_swallowed_by_prompt_buffer(self):
        self.engine.handle_update(self.update('/prompt_begin'))
        self.engine.handle_update(self.update('/status', 2))
        self.assertEqual(self.store.get('prompt_buffer'), [])
        self.assertIn('Codex Bridge', self.sender.sent[-1][0])

    def test_cancel_stops_queued_and_auto(self):
        self.engine.enqueue('one', 'test')
        self.engine.cancel_current()
        self.assertIsNone(self.store.next_job())
        self.assertTrue(self.engine.cancel.is_set())
        self.assertEqual(self.store.get('auto_mode'), 'paused')

    def test_cancel_during_thread_start_prevents_turn_start(self):
        original = self.rpc.request
        def request(method, params, timeout=45):
            result = original(method, params, timeout)
            if method == 'thread/start': self.engine.cancel_current()
            return result
        self.rpc.request = request
        self.engine.enqueue('one', 'test')
        job = self.store.next_job()
        self.engine.run_job(job)
        self.assertFalse(any(name == 'turn/start' for name, _ in self.rpc.calls))
        self.assertEqual(self.store.job(job['id'])['status'], 'cancelled')

    def test_completed_turn_without_final_text_is_not_success(self):
        self.engine.enqueue('one', 'test')
        self.engine.wait_turn = lambda *_: {'id': 'turn-1', 'status': 'completed'}
        with self.assertRaises(RPCError):
            self.engine.run_job(self.store.next_job())

    def test_normal_turn_captures_final_and_reuses_thread(self):
        self.engine.enqueue('one', 'test')
        self.engine.messages['turn-1'] = ['working', 'final answer']
        self.engine.wait_turn = lambda *_: {'id': 'turn-1', 'status': 'completed'}
        self.engine.run_job(self.store.next_job())
        self.assertEqual(self.store.get('last_final'), 'final answer')
        self.assertEqual(self.store.get('codex_thread'), 'thread-1')
        self.rpc.generation += 1
        self.engine.thread()
        self.assertIn('thread/resume', [name for name, _ in self.rpc.calls])

    def test_model_switch_keeps_thread_and_applies_next_turn(self):
        self.store.set('codex_thread', 'saved-thread')
        self.engine.active_model = 'model-a'
        self.engine.model_command('model-b')
        self.assertEqual(self.store.get('codex_thread'), 'saved-thread')
        self.assertEqual(self.engine.active_model, 'model-a')
        self.assertEqual(self.engine.selected_model(), 'model-b')

    def test_model_switch_resets_unsupported_effort(self):
        self.store.set('effort', 'unsupported')
        self.engine.model_command('model-b')
        self.assertEqual(self.store.get('effort'), 'medium')

    def test_invalid_model_and_effort_do_not_change_selection(self):
        self.engine.model_command('bad')
        self.engine.effort_command('bad')
        self.assertEqual(self.engine.selected_model(), 'model-a')
        self.assertIsNone(self.store.get('effort'))

    def test_other_project_thread_is_rejected(self):
        self.rpc.cwd = str(self.root / 'other')
        self.engine.resume_thread('outside-thread')
        self.assertIsNone(self.store.get('codex_thread'))

    def test_callback_is_scoped_and_single_use(self):
        data = self.store.action({'type': 'queue', 'text': 'one'})
        update = {'update_id': 9, 'callback_query': {'id': 'callback-1', 'from': {'id': 8},
                  'message': {'chat': {'id': -10}, 'message_thread_id': 55}, 'data': data}}
        self.engine.handle_update(update)
        self.assertIsNone(self.store.next_job())
        update['callback_query']['from']['id'] = 7
        self.engine.handle_update(update)
        self.assertIsNotNone(self.store.next_job())
        self.engine.handle_update(update)
        self.assertIsNone(self.store.next_job())

    def test_expired_callback(self):
        data = self.store.action({'type': 'queue'}, seconds=-1)
        self.assertIsNone(self.store.consume(data))

    def test_steer_requires_matching_active_turn(self):
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.handle_action({'type': 'steer', 'thread': 't', 'turn': 'old', 'text': 'one'}, 'src')
        self.assertFalse(any(name == 'turn/steer' for name, _ in self.rpc.calls))
        self.engine.handle_action({'type': 'steer', 'thread': 't', 'turn': 'v', 'text': 'one'}, 'src')
        self.assertEqual(self.rpc.calls[-1][0], 'turn/steer')

    def test_action_after_restart_cannot_approve_old_request(self):
        self.engine.answer_request({'request': 'old', 'decision': 'accept'})
        self.assertEqual(self.rpc.responses, [])

    def test_unexpected_execution_approvals_are_declined_without_buttons(self):
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
            with self.subTest(method=method):
                self.engine.on_event({'id': 12, '_generation': 1, 'method': method,
                          'params': {'threadId': 't', 'turnId': 'v', 'command': 'synthetic command'}})
                self.assertEqual(self.rpc.responses[-1], (12, {'decision': 'decline'}))
                self.assertEqual(self.engine.pending_requests, {})
                self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    def test_permission_request_is_denied_without_prompt_or_grant(self):
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.on_event({'id': 12, '_generation': 1, 'method': 'item/permissions/requestApproval',
                  'params': {'threadId': 't', 'turnId': 'v', 'permissions': {'network': {'enabled': True}}}})
        self.assertEqual(self.rpc.responses, [(12, {'permissions': {}, 'scope': 'turn'})])
        self.assertEqual(self.engine.pending_requests, {})
        self.assertEqual(self.sender.sent, [])
        self.engine.answer_request({'request': '12', 'decision': 'deny'})
        self.assertEqual(self.rpc.responses[-1][1]['permissions'], {})

    def test_old_pending_permission_button_cannot_grant_new_access(self):
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        for method, decision, denied in (
                ('item/commandExecution/requestApproval', 'accept', {'decision': 'decline'}),
                ('item/fileChange/requestApproval', 'accept', {'decision': 'decline'}),
                ('item/permissions/requestApproval', 'grant', {'permissions': {}, 'scope': 'turn'})):
            with self.subTest(method=method):
                self.engine.pending_requests['12'] = {'event': {
                    'id': 12, '_generation': 1, 'method': method, 'params': {
                        'threadId': 't', 'turnId': 'v', 'permissions': {'network': {'enabled': True}}}}}
                self.engine.answer_request({'request': '12', 'decision': decision})
                self.assertEqual(self.rpc.responses[-1], (12, denied))
                self.assertEqual(self.engine.pending_requests, {})
                self.assertEqual(self.sender.sent, [])

    def test_old_generation_approval_button_cannot_respond_to_new_backend(self):
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.pending_requests['12'] = {'event': {
            'id': 12, '_generation': 0, 'method': 'item/commandExecution/requestApproval',
            'params': {'threadId': 't', 'turnId': 'v'}}}
        self.engine.answer_request({'request': '12', 'decision': 'accept'})
        self.assertEqual(self.rpc.responses, [])
        self.assertEqual(self.engine.pending_requests, {})

    def test_headless_thread_policy_replaces_stale_interactive_setting(self):
        self.assertEqual(self.config.approval_policy, 'never')
        self.config.approval_policy = 'on-request'
        self.engine.thread()
        self.rpc.generation += 1
        self.engine.thread()
        self.engine.thread(temporary=True)
        calls = [(method, params) for method, params in self.rpc.calls
                 if method in ('thread/start', 'thread/resume', 'thread/fork')]
        self.assertEqual([method for method, _ in calls], ['thread/start', 'thread/resume', 'thread/fork'])
        self.assertTrue(all(params['approvalPolicy'] == 'never' for _, params in calls))
        self.assertTrue(all(params['developerInstructions'] == BRIDGE_INSTRUCTIONS for _, params in calls))
        self.assertIn('foreground tool or PTY sessions', calls[1][1]['developerInstructions'])
        self.assertIn('without requesting elevated access', calls[2][1]['developerInstructions'])
        self.assertEqual(calls[-1][1]['permissions'], ':read-only')

    def test_cached_thread_turn_cannot_restore_interactive_approval(self):
        self.config.approval_policy = 'on-request'
        self.store.set('codex_thread', 'saved-thread')
        self.engine.loaded_thread = 'saved-thread'
        self.engine.loaded_generation = self.rpc.generation
        self.engine.enqueue('one', 'test')
        self.engine.wait_turn = lambda *_: {'id': 'turn-1', 'status': 'completed',
            'items': [{'type': 'agentMessage', 'text': 'done'}]}
        self.engine.run_job(self.store.next_job())
        self.assertFalse(any(method in ('thread/start', 'thread/resume') for method, _ in self.rpc.calls))
        params = next(params for method, params in self.rpc.calls if method == 'turn/start')
        self.assertEqual(params['approvalPolicy'], 'never')

    def test_known_answer_is_reused_and_audited(self):
        self.store.set('known_answers', {'Which output format?': 'HTML'})
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.on_event({'id': 18, '_generation': 1, 'method': 'item/tool/requestUserInput',
          'params': {'threadId': 't', 'turnId': 'v', 'questions': [{'id': 'q1', 'question': 'Which output format?'}]}})
        self.assertEqual(self.rpc.responses[-1][1], {'answers': {'q1': {'answers': ['HTML']}}})

    def test_sandbox_permission_question_is_denied_without_showing_a_prompt(self):
        question = 'Windows 샌드박스 관리자 권한을 허용할까요?'
        self.store.set('known_answers', {question: 'yes'})
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.on_event({'id': 18, '_generation': 1, 'method': 'item/tool/requestUserInput',
          'params': {'threadId': 't', 'turnId': 'v', 'questions': [{'id': 'q1', 'question': question}]}})
        answer = self.rpc.responses[-1][1]['answers']['q1']['answers'][0]
        self.assertIn('추가 권한은 허용하지 않습니다', answer)
        self.assertNotEqual(answer, 'yes')
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.engine.pending_requests, {})

    def test_mixed_permission_and_business_questions_keep_only_business_input(self):
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.on_event({'id': 18, '_generation': 1, 'method': 'item/tool/requestUserInput',
          'params': {'threadId': 't', 'turnId': 'v', 'questions': [
              {'id': 'permission', 'question': 'May I grant network access permission?'},
              {'id': 'format', 'question': 'Which output format?', 'options': [{'label': 'HTML'}]}]}})
        self.assertEqual(self.rpc.responses, [])
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn('Which output format?', self.sender.sent[0][0])
        self.assertNotIn('network access', self.sender.sent[0][0])
        self.engine.answer_input({'request': '18', 'question': 'format', 'value': 'HTML'})
        answers = self.rpc.responses[-1][1]['answers']
        self.assertEqual(answers['format']['answers'], ['HTML'])
        self.assertIn('허용하지 않습니다', answers['permission']['answers'][0])

    def test_planned_workflow_question_can_continue(self):
        self.approved()
        job_id = self.store.enqueue_job(json.dumps({'kind': 'plan_step', 'text': 'step'}), 'auto-input')
        self.engine.active_job = job_id
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.on_event({'id': 18, '_generation': 1, 'method': 'item/tool/requestUserInput',
          'params': {'threadId': 't', 'turnId': 'v', 'questions': [{'id': 'q1', 'question': '계획의 다음 단계를 계속 진행할까요?',
                     'options': [{'label': '계속'}, {'label': '중단'}]}]}})
        self.assertEqual(self.rpc.responses[-1][1]['answers']['q1']['answers'], ['계속'])

    def test_sensitive_question_is_not_routine(self):
        for text in ['설치를 진행할까요?', '업로드를 진행할까요?', 'Shall I continue with permission approval?',
                     '원본 삭제 후 계속 진행할까요?']:
            self.assertFalse(routine_question(text))

    def test_complete_plan_does_not_queue_more(self):
        self.approved()
        self.engine.finish_step({'status': 'step_done', 'summary': 'done', 'evidence': []}, {'id': 'fake'})
        self.assertEqual(self.store.get('plan_index'), 1)
        self.assertEqual(self.store.get('auto_mode'), 'complete')
        self.assertIsNone(self.store.next_job())

    def test_verified_step_automatically_queues_next(self):
        self.approved(2)
        self.engine.finish_step({'status': 'step_done', 'summary': 'done', 'evidence': []}, {'id': 'fake'})
        self.assertEqual(self.store.get('plan_index'), 1)
        self.assertEqual(json.loads(self.store.next_job()['prompt'])['kind'], 'plan_step')

    def test_auto_can_start_after_draft_completion_before_worker_cleanup(self):
        old = self.plan()
        old['scope'] = 'old scope that must not be approved'
        self.store.set('plan', old)
        revised = self.plan(2)
        revised['scope'] = 'new reviewed scope'
        draft_id = self.engine.enqueue('Draft revised plan', 'draft-ready', 'plan_draft')
        self.engine.active_job = draft_id
        self.engine.messages['turn-1'] = [json.dumps(revised)]
        self.engine.wait_turn = lambda *_: {'id': 'turn-1', 'status': 'completed'}

        def approve_immediately():
            self.assertEqual(self.store.job(draft_id)['status'], 'completed')
            self.assertEqual(self.store.get('plan'), revised)
            self.engine.auto_start(plan_hash(old))
            self.assertIsNone(self.store.get('approved_plan_hash'))
            self.engine.auto_start(plan_hash(revised))
            nonce = self.store.get('auto_nonce')
            self.engine.auto_start(plan_hash(revised))
            self.assertEqual(self.store.get('auto_nonce'), nonce)

        self.engine.show_plan = approve_immediately
        self.engine.run_job(self.store.next_job())
        self.assertEqual(self.engine.active_job, draft_id)  # Final cleanup has not run.
        self.assertEqual(self.store.get('auto_mode'), 'running')
        self.assertEqual(self.store.get('approved_plan_hash'), plan_hash(revised))
        queued = self.store.next_job()
        self.assertEqual(json.loads(queued['prompt'])['kind'], 'plan_step')
        self.assertIn('new reviewed scope', json.loads(queued['prompt'])['text'])
        self.assertIsNone(self.store.next_job())

    def test_auto_rejects_unfinished_failed_cancelled_foreign_or_stale_drafts(self):
        plan = self.plan()
        self.store.set('plan', plan)
        self.store.set('auto_mode', 'paused')
        other = {**plan, 'scope': 'unpublished scope'}
        cases = [('running', 'plan_draft', plan), ('queued', 'plan_draft', plan),
                 ('failed', 'plan_draft', plan), ('cancelled', 'plan_draft', plan),
                 ('completed', 'chat', plan), ('completed', 'plan_draft', other),
                 ('completed', 'plan_draft', {'invalid': 'draft'})]
        for index, (status, kind, result) in enumerate(cases):
            with self.subTest(status=status, kind=kind, result=result):
                job_id = self.store.enqueue_job(json.dumps({'kind': kind, 'text': 'draft'}), f'guard-{index}')
                self.store.update_job(job_id, status=status, result=json.dumps(result))
                self.engine.active_job = job_id
                self.engine.auto_start(plan_hash(plan))
                self.assertEqual(self.store.get('auto_mode'), 'paused')
                self.assertIsNone(self.store.get('approved_plan_hash'))
                self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE source LIKE 'auto:%'").fetchone()[0], 0)

    def test_failed_acceptance_check_never_advances_step(self):
        self.approved(2)
        self.rpc.check_exit = 1
        self.engine.finish_step({'status': 'step_done', 'summary': 'claimed done', 'evidence': []}, {'id': 'fake'})
        self.assertEqual(self.store.get('plan_index', 0), 0)
        self.assertIsNotNone(self.store.next_job())

    def test_permission_blocker_stops_auto_without_asking_to_allow(self):
        self.approved(2)
        self.engine.finish_step({'status': 'needs_approval', 'summary': 'permission required', 'question': '승인 필요'}, {'id': 'fake'})
        self.assertEqual(self.store.get('auto_mode'), 'blocked')
        self.assertIsNone(self.store.next_job())
        self.assertFalse(any('승인 필요' in text or kwargs.get('buttons') for text, kwargs in self.sender.sent))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM audit WHERE event='plan_permission_blocked_without_prompt'").fetchone()[0], 1)

    def test_plan_mutation_invalidates_prior_approval(self):
        plan = self.approved()
        plan['scope'] = 'different scope'
        self.store.set('plan', plan)
        with self.assertRaises(ValueError):
            self.engine.finish_step({'status': 'step_done'}, {'id': 'fake'})

    def test_turn_budget_stops_continuation(self):
        self.approved()
        self.store.set('auto_turns', self.config.max_auto_turns)
        self.engine._queue_step()
        self.assertEqual(self.store.get('auto_mode'), 'budget_reached')
        self.assertIsNone(self.store.next_job())

    def test_repeated_no_progress_stops(self):
        self.approved()
        for i in range(3):
            self.engine.finish_step({'status': 'continue', 'summary': 'same', 'evidence': []}, {'id': str(i)})
        self.assertEqual(self.store.get('auto_mode'), 'blocked')

    def test_plan_requires_real_acceptance_checks(self):
        plan = self.plan()
        plan['steps'][0]['checks'] = []
        with self.assertRaises(ValueError): validate_plan(plan)

    def test_restart_preserves_queue_and_marks_ambiguous_run(self):
        running = self.store.enqueue_job('running', 'r')
        self.store.next_job()
        queued = self.store.enqueue_job('queued', 'q')
        self.store.recover()
        self.assertEqual(self.store.job(running)['status'], 'review_required')
        self.assertEqual(self.store.next_job()['id'], queued)

    def test_outbox_revision_preserves_newer_live_update(self):
        self.store.queue('editMessageText', {'text': 'old'}, dedupe='live')
        old = self.store.next_outbound()
        self.store.queue('editMessageText', {'text': 'new'}, dedupe='live')
        self.store.outbound_done(old)
        self.assertEqual(json.loads(self.store.next_outbound()['payload'])['text'], 'new')

    def test_outbox_failure_is_persisted_and_nonblocking(self):
        self.store.queue('sendMessage', {'text': 'one'})
        first = self.store.next_outbound()
        self.store.outbound_error(first, 'timeout', 30)
        self.store.queue('sendMessage', {'text': 'urgent'}, priority=0)
        self.assertEqual(json.loads(self.store.next_outbound()['payload'])['text'], 'urgent')

    def test_redaction_covers_codex_key_and_token(self):
        text = self.redact(self.config.token + ' synthetic-key-12345')
        self.assertNotIn(self.config.token, text)
        self.assertNotIn('synthetic-key-12345', text)

    def test_unicode_chunks_fit_utf16_limit(self):
        text = '한글😀' * 4000
        parts = list(chunks(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3500 for p in parts))

    def test_topic_creation_is_not_repeated_after_uncertain_result(self):
        self.store.set('telegram_topic', None)
        class BadTelegram:
            def call(self, *args, **kwargs): raise TelegramError(0, 'timeout')
        with self.assertRaises(TelegramError): bootstrap_topic(BadTelegram(), self.store, self.config)
        with self.assertRaises(RuntimeError): bootstrap_topic(BadTelegram(), self.store, self.config)

    def test_saved_topic_is_reused_without_network_create(self):
        class BadTelegram:
            def call(self, *args, **kwargs): raise AssertionError('should not create')
        bootstrap_topic(BadTelegram(), self.store, self.config)
        self.assertEqual(self.store.get('telegram_topic'), 55)

    def test_os_lock_is_released_and_rejects_duplicate(self):
        key = 'unit-test-' + str(time.time_ns())
        with InstanceLock(key):
            with self.assertRaises(RuntimeError):
                with InstanceLock(key): pass
        with InstanceLock(key): pass

    def test_telegram_timeout_does_not_leak_token(self):
        tg = Telegram(self.config.token, self.redact)
        with patch.object(tg.write, 'post', side_effect=requests.exceptions.ReadTimeout('URL: ' + tg.base)):
            with self.assertRaises(TelegramError) as result: tg.call('sendMessage', {})
        self.assertNotIn(self.config.token, str(result.exception))
        self.assertEqual(result.exception.code, 0)
        tg.close()

    def test_telegram_429_retry_after_is_preserved(self):
        tg = Telegram(self.config.token, self.redact)
        response = type('Response', (), {'status_code': 429, 'json': lambda _: {'ok': False, 'error_code': 429,
                                        'description': 'rate limit', 'parameters': {'retry_after': 9}}})()
        with patch.object(tg.write, 'post', return_value=response):
            with self.assertRaises(TelegramError) as result: tg.call('sendMessage', {})
        self.assertEqual(result.exception.retry_after, 9)
        self.assertFalse(result.exception.permanent)
        tg.close()

    def test_telegram_bad_json_is_retryable_server_error(self):
        tg = Telegram(self.config.token, self.redact)
        response = type('Response', (), {'status_code': 200, 'json': lambda _: (_ for _ in ()).throw(ValueError())})()
        with patch.object(tg.write, 'post', return_value=response):
            with self.assertRaises(TelegramError) as result: tg.call('sendMessage', {})
        self.assertEqual(result.exception.code, 502)
        tg.close()

    def test_command_output_and_file_change_events_are_visible(self):
        self.engine.active_thread = 't'
        for item in ({'type': 'commandExecution', 'exitCode': 0, 'aggregatedOutput': 'visible output'},
                     {'type': 'fileChange', 'changes': [{'path': 'visible.txt'}]}):
            self.engine.on_event({'method': 'item/completed', 'params': {'threadId': 't', 'turnId': 'v', 'item': item}})
        self.assertIn('visible output', '\n'.join(self.engine.logs))
        self.assertIn('visible.txt', '\n'.join(self.engine.logs))

    def test_stale_backend_generation_cannot_complete_current_turn(self):
        self.engine.active_thread = 't'
        self.engine.on_event({'method': 'turn/completed', '_generation': 0,
                              'params': {'threadId': 't', 'turn': {'id': 'old', 'status': 'completed'}}})
        self.assertNotIn('old', self.engine.completed)

    def test_hard_deadline_interrupts_active_turn(self):
        self.engine.active_thread = 't'
        def request(method, params, timeout=45):
            self.rpc.calls.append((method, params))
            if method == 'turn/interrupt':
                self.engine.completed['v'] = {'id': 'v', 'status': 'interrupted'}
            return {}
        self.rpc.request = request
        result = self.engine.wait_turn('v', time.monotonic() - 1)
        self.assertEqual(result['status'], 'interrupted')
        self.assertIn('turn/interrupt', [name for name, _ in self.rpc.calls])

    def test_pause_preserves_user_queued_work(self):
        self.engine.enqueue('user task', 'user')
        self.store.enqueue_job(json.dumps({'kind': 'plan_step', 'text': 'step'}), 'auto')
        self.engine.handle_update(self.update('/pause'))
        self.assertEqual(json.loads(self.store.next_job()['prompt'])['text'], 'user task')
        self.assertIsNone(self.store.next_job())

    def test_cancel_interrupts_acceptance_command(self):
        release = threading.Event()
        def request(method, params, timeout=45):
            self.rpc.calls.append((method, params))
            if method == 'command/exec':
                self.engine.cancel.set()
                release.wait(1)
                return {'exitCode': 1, 'stdout': '', 'stderr': ''}
            if method == 'command/exec/terminate':
                release.set()
            return {}
        self.rpc.request = request
        from codex_telegram.engine import RunCancelled
        with self.assertRaises(RunCancelled):
            self.engine.execute_check(['python', '-c', 'print(1)'], 'test-check')
        self.assertIn('command/exec/terminate', [name for name, _ in self.rpc.calls])

    def test_token_budget_stops_auto(self):
        self.approved()
        self.store.set('auto_token_baseline', 0)
        self.store.set('token_usage', {'total': {'totalTokens': self.config.max_auto_tokens + 1}})
        self.engine._queue_step()
        self.assertEqual(self.store.get('auto_mode'), 'budget_reached')

    def test_elapsed_plan_time_stops_auto(self):
        self.approved()
        self.store.set('auto_started', time.time() - self.config.max_run_seconds - 1)
        self.engine._queue_step()
        self.assertEqual(self.store.get('auto_mode'), 'budget_reached')

    def test_known_answer_cannot_auto_answer_sensitive_prompt(self):
        self.store.set('known_answers', {'설치를 승인할까요?': 'yes'})
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.on_event({'id': 22, '_generation': 1, 'method': 'item/tool/requestUserInput',
          'params': {'threadId': 't', 'turnId': 'v', 'questions': [{'id': 'q', 'question': '설치를 승인할까요?'}]}})
        self.assertEqual(self.rpc.responses, [])

    def test_mcp_form_requires_user_answer(self):
        self.engine.active_thread, self.engine.active_turn = 't', 'v'
        self.engine.on_event({'id': 25, '_generation': 1, 'method': 'mcpServer/elicitation/request',
          'params': {'threadId': 't', 'turnId': 'v', 'message': 'form', 'requestedSchema': {'required': ['value']}}})
        self.assertEqual(self.rpc.responses, [])
        self.engine.free_answer('{"value":"explicit user answer"}')
        self.assertEqual(self.rpc.responses[-1][1]['content'], {'value': 'explicit user answer'})

    def test_acceptance_uses_active_permission_profile(self):
        self.engine.active_permission_profile = 'protected-project'
        self.engine.execute_check(['python', '-c', 'print(1)'], 'check-profile')
        params = next(params for method, params in self.rpc.calls if method == 'command/exec')
        self.assertEqual(params['permissionProfile'], 'protected-project')

    def test_planning_inherits_protected_profile(self):
        self.engine.project_config = lambda: {'default_permissions': 'protected-project'}
        options = self.engine._thread_options(temporary=True)
        self.assertEqual(options['permissions'], 'bridge_planning_readonly')
        self.assertEqual(options['config']['permissions']['bridge_planning_readonly']['extends'], 'protected-project')
        self.assertFalse(options['config']['permissions']['bridge_planning_readonly']['network']['enabled'])

    def test_implicit_workspace_planning_uses_native_readonly(self):
        self.engine.project_config = lambda: {}
        options = self.engine._thread_options(temporary=True)
        self.assertEqual(options['permissions'], ':read-only')
        self.assertTrue(options['ephemeral'])
        self.assertNotIn('permissions', options.get('config', {}))

    def test_builtin_default_does_not_create_invalid_derived_profile(self):
        for parent in (':workspace', ':read-only', ':danger-full-access'):
            with self.subTest(parent=parent):
                self.engine.project_config = lambda: {'default_permissions': parent}
                options = self.engine._thread_options(temporary=True)
                self.assertEqual(options['permissions'], ':read-only')
                self.assertNotIn('permissions', options.get('config', {}))

    def test_explicit_readonly_retains_custom_deny_parent(self):
        self.config.read_only = True
        self.engine.project_config = lambda: {'default_permissions': 'protected-project'}
        options = self.engine._thread_options()
        self.assertEqual(options['config']['permissions']['bridge_planning_readonly']['extends'], 'protected-project')
        self.assertNotIn('ephemeral', options)

    def test_suggestion_forks_history_without_changing_main_conversation(self):
        self.store.set('codex_thread', 'original-thread')
        self.engine.loaded_thread = 'original-thread'
        self.engine.loaded_generation = 1
        fork = self.engine.thread(temporary=True)
        self.assertEqual(fork, 'fork-1')
        params = next(params for method, params in self.rpc.calls if method == 'thread/fork')
        self.assertEqual(params['threadId'], 'original-thread')
        self.assertTrue(params['ephemeral'])
        self.assertTrue(params['excludeTurns'])
        self.assertEqual(params['permissions'], ':read-only')
        self.assertEqual(self.store.get('codex_thread'), 'original-thread')
        self.assertEqual(self.engine.loaded_thread, 'original-thread')

    def test_temporary_thread_rejects_other_project_history(self):
        self.store.set('codex_thread', 'wrong-project')
        self.rpc.cwd = str(self.root.parent / 'other-project')
        with self.assertRaisesRegex(RPCError, '다른 프로젝트'):
            self.engine.thread(temporary=True)
        self.assertNotIn('thread/fork', [method for method, _ in self.rpc.calls])

    def test_temporary_thread_without_history_starts_readonly(self):
        self.assertEqual(self.engine.thread(temporary=True), 'thread-1')
        params = next(params for method, params in self.rpc.calls if method == 'thread/start')
        self.assertEqual(params['permissions'], ':read-only')
        self.assertTrue(params['ephemeral'])

    def writer_conflict(self, active=False, foreign=False):
        self.store.set('codex_thread', 'original-thread')
        original = self.rpc.request
        def request(method, params, timeout=45):
            if method == 'thread/resume':
                self.rpc.calls.append((method, params))
                raise RPCError('thread original-thread already has an active writer', code=-32600)
            if method == 'thread/read':
                self.rpc.calls.append((method, params))
                return {'thread': {'id': 'original-thread',
                    'cwd': str(self.root.parent / 'foreign') if foreign else str(self.root),
                    'status': {'type': 'idle'}}}
            if method == 'thread/turns/list':
                self.rpc.calls.append((method, params))
                return {'data': [{'id': 'latest-turn', 'status': 'inProgress' if active else 'completed'}]}
            return original(method, params, timeout)
        self.rpc.request = request

    def test_idle_writer_conflict_recovers_history_once(self):
        self.writer_conflict()
        self.assertEqual(self.engine.thread(), 'fork-1')
        self.assertEqual(self.store.get('codex_thread'), 'fork-1')
        fork = next(params for method, params in self.rpc.calls if method == 'thread/fork')
        self.assertEqual(fork['lastTurnId'], 'latest-turn')
        self.assertNotIn('ephemeral', fork)
        self.assertIn('대화 이력', self.sender.sent[-1][0])
        self.engine.thread()
        self.assertEqual(sum(method == 'thread/fork' for method, _ in self.rpc.calls), 1)

    def test_active_writer_is_not_forked_into_concurrent_execution(self):
        self.writer_conflict(active=True)
        with self.assertRaisesRegex(RPCError, '진행 중'):
            self.engine.thread()
        self.assertEqual(self.store.get('codex_thread'), 'original-thread')
        self.assertNotIn('thread/fork', [method for method, _ in self.rpc.calls])

    def test_foreign_writer_history_is_not_copied(self):
        self.writer_conflict(foreign=True)
        with self.assertRaisesRegex(RPCError, '다른 프로젝트'):
            self.engine.thread()
        self.assertNotIn('thread/fork', [method for method, _ in self.rpc.calls])

    def test_non_writer_resume_error_does_not_fork_or_retry(self):
        self.store.set('codex_thread', 'original-thread')
        def request(method, params, timeout=45):
            self.rpc.calls.append((method, params))
            raise RPCError('permission denied', code=-32600)
        self.rpc.request = request
        with self.assertRaisesRegex(RPCError, 'permission denied'):
            self.engine.thread()
        self.assertEqual([method for method, _ in self.rpc.calls], ['thread/resume'])

    def test_ambiguous_resume_timeout_does_not_start_another_conversation(self):
        self.store.set('codex_thread', 'original-thread')
        def request(method, params, timeout=45):
            self.rpc.calls.append((method, params))
            raise RPCError('Codex response timeout: thread/resume')
        self.rpc.request = request
        with self.assertRaises(RPCError):
            self.engine.thread()
        self.assertEqual(len(self.rpc.calls), 1)

    def test_suggestion_button_and_slash_create_same_job_kind(self):
        self.store.set('last_final', 'synthetic completed result')
        self.engine.handle_update(self.update('/suggest'))
        self.engine.handle_action({'type': 'suggest'}, 'button-2')
        with self.store.lock:
            jobs = [json.loads(row[0]) for row in self.store.db.execute('SELECT prompt FROM jobs')]
        self.assertEqual([job['kind'] for job in jobs], ['suggest', 'suggest'])
        self.assertTrue(all('synthetic completed result' in job['text'] for job in jobs))

    def test_temporary_thread_cleanup_does_not_close_main_server(self):
        self.store.set('codex_thread', 'original-thread')
        self.engine.release_temporary_thread('temporary-1')
        self.assertEqual(self.rpc.calls[-1], ('thread/unsubscribe', {'threadId': 'temporary-1'}))
        self.assertTrue(self.rpc.alive)
        self.assertEqual(self.store.get('codex_thread'), 'original-thread')

    def test_cleanup_error_cannot_hide_generation_result(self):
        def request(*args, **kwargs): raise RPCError('cleanup unavailable')
        self.rpc.request = request
        self.engine.release_temporary_thread('temporary-1')
        self.assertIn('cleanup unavailable', self.engine.logs[-1])

    def test_failed_turn_shows_real_cause_and_persists_diagnostic(self):
        self.rpc.on_start = lambda: self.engine.completed.update({'turn-1': {'id': 'turn-1', 'status': 'failed',
            'error': {'message': 'failed to load workspace requirements: invalid input parameter'}}})
        job = self.store.enqueue_job(json.dumps({'kind': 'suggest', 'text': 'synthetic suggestion'}), 'failure-test')
        self.engine.run_job(self.store.job(job))
        self.assertEqual(self.store.job(job)['status'], 'failed')
        self.assertIn('failed to load workspace requirements', self.sender.sent[-1][0])
        self.assertEqual(self.store.get('last_failure')['job'], job)
        self.engine.status()
        self.assertIn('invalid input parameter', self.sender.sent[-1][0])

    def test_failure_details_are_redacted_and_never_reexecuted(self):
        self.engine.record_failure({'id': 'synthetic'}, {'message': 'request failed',
                'additionalDetails': self.config.token + ' synthetic-key-12345'})
        text = self.sender.sent[-1][0]
        self.assertIn('request failed', text)
        self.assertNotIn(self.config.token, text)
        self.assertNotIn('synthetic-key-12345', text)
        self.assertEqual(self.rpc.calls, [])
        self.assertIsNone(self.store.next_job())

    def test_failed_turn_does_not_emit_a_false_suggestion(self):
        self.rpc.on_start = lambda: self.engine.completed.update({'turn-1': {'id': 'turn-1', 'status': 'failed',
                        'error': {'message': 'upstream unavailable'}}})
        job = self.store.enqueue_job(json.dumps({'kind': 'suggest', 'text': 'synthetic'}), 'no-suggestion')
        self.engine.run_job(self.store.job(job))
        self.assertFalse(any('추천 후속 지시' in text for text, _ in self.sender.sent))

    def test_cancelled_turn_does_not_report_a_backend_failure(self):
        self.rpc.on_start = lambda: self.engine.completed.update({'turn-1': {'id': 'turn-1', 'status': 'interrupted'}})
        job = self.store.enqueue_job(json.dumps({'kind': 'chat', 'text': 'synthetic'}), 'interrupted')
        self.engine.run_job(self.store.job(job))
        self.assertEqual(self.store.job(job)['status'], 'cancelled')
        self.assertIsNone(self.store.get('last_failure'))

    def test_expired_callback_ack_does_not_fail_authorized_action_delivery(self):
        class FakeTelegram:
            def call(self, method, payload):
                if method == 'answerCallbackQuery':
                    raise TelegramError(400, 'Bad Request: query is too old and response timeout expired or query ID is invalid')
                return {'message_id': 1}
        stop = threading.Event()
        sender = Sender(FakeTelegram(), self.store, self.config, stop, self.redact)
        self.store.queue('answerCallbackQuery', {'callback_query_id': 'expired'}, 0)
        sender.thread.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.store.lock: status = self.store.db.execute('SELECT status FROM outbox').fetchone()[0]
                if status == 'delivered': break
                time.sleep(.02)
            self.assertEqual(status, 'delivered')
        finally:
            stop.set();sender.thread.join(1)

    def test_unregistered_extra_directory_is_rejected(self):
        self.config.add_dirs = (str(self.root.parent / 'unregistered-root'),)
        self.engine.project_config = lambda: {}
        with self.assertRaises(ValueError):
            self.engine.validate_add_dirs()

    def sender_case(self, errors):
        class FakeTelegram:
            def __init__(self): self.calls = 0
            def call(self, method, payload):
                self.calls += 1
                if errors: raise errors.pop(0)
                return {'message_id': 1}
        tg, stop = FakeTelegram(), threading.Event()
        self.config.send_interval = .01
        sender = Sender(tg, self.store, self.config, stop, self.redact)
        sender.send('test result')
        sender.thread.start()
        return tg, stop, sender

    def test_sender_recovers_from_429(self):
        tg, stop, sender = self.sender_case([TelegramError(429, 'rate limit', .01)])
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with self.store.lock:
                    row = self.store.db.execute('SELECT status,attempts FROM outbox').fetchone()
                if row['status'] == 'delivered': break
                time.sleep(.02)
            self.assertEqual(row['status'], 'delivered')
            self.assertEqual(row['attempts'], 1)
            self.assertEqual(tg.calls, 2)
        finally:
            stop.set(); sender.thread.join(1)

    def test_sender_recovers_from_network_error(self):
        tg, stop, sender = self.sender_case([TelegramError(0, 'ReadTimeout')])
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                with self.store.lock:
                    row = self.store.db.execute('SELECT status FROM outbox').fetchone()
                if row['status'] == 'delivered': break
                time.sleep(.02)
            self.assertEqual(row['status'], 'delivered')
            self.assertEqual(tg.calls, 2)
        finally:
            stop.set(); sender.thread.join(1)

    def test_permanent_send_error_is_not_retried_forever(self):
        tg, stop, sender = self.sender_case([TelegramError(403, 'forbidden')])
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.store.lock:
                    row = self.store.db.execute('SELECT status FROM outbox').fetchone()
                if row['status'] == 'failed': break
                time.sleep(.02)
            self.assertEqual(row['status'], 'failed')
            self.assertEqual(tg.calls, 1)
        finally:
            stop.set(); sender.thread.join(1)

    def test_chat_can_reference_saved_plan_without_reasking(self):
        self.store.set('plan', self.plan())
        self.store.set('auto_mode', 'paused')
        self.engine.enqueue('Explain the existing plan', 'plan-reference')
        self.engine.messages['turn-1'] = ['explanation']
        self.engine.wait_turn = lambda *_: {'id': 'turn-1', 'status': 'completed'}
        self.engine.run_job(self.store.next_job())
        params = next(params for method,params in self.rpc.calls if method == 'turn/start')
        self.assertIn('synthetic goal', params['input'][0]['text'])
        self.assertIn('paused', params['input'][0]['text'])
        self.assertEqual(self.store.get('auto_mode'), 'paused')

    def test_plan_revision_receives_current_plan_context(self):
        old = self.plan()
        self.store.set('plan', old)
        revised = self.plan()
        revised['goal'] = 'modified goal'
        self.engine.enqueue('Revise the second check', 'plan-revision', 'plan_draft')
        self.engine.messages['turn-1'] = [json.dumps(revised)]
        self.engine.wait_turn = lambda *_: {'id': 'turn-1', 'status': 'completed'}
        self.engine.run_job(self.store.next_job())
        params = next(params for method,params in self.rpc.calls if method == 'turn/start')
        self.assertIn('synthetic goal', params['input'][0]['text'])
        self.assertEqual(self.store.get('plan')['goal'], 'modified goal')
        self.assertIsNone(self.store.get('approved_plan_hash'))

    def test_project_environment_preserves_tools_without_secrets(self):
        selected=project_environment({'FS_LICENSE':'license-file','DCM2NIIX_EXE':'tool-path',
                    'TELEGRAM_BOT_TOKEN':'private-token','OPENAI_API_KEY':'private-key',
                    'CODEX_MODEL':'model-a','EEG_DRIFT_REPORTS_ROOT':'report-path'})
        self.assertEqual(selected,{'FS_LICENSE':'license-file','DCM2NIIX_EXE':'tool-path','EEG_DRIFT_REPORTS_ROOT':'report-path'})

    def test_model_help_lists_dynamic_model_and_effort_examples(self):
        self.engine.model_command('')
        text, options = self.sender.sent[-1]
        for example in ('/model model-a', '/model model-a high', '/model high', '/model max', '/model ultra', '/effort high'):
            self.assertIn(example, text)
        self.assertIn('model-b: medium, high', text)
        self.assertGreater(self.store.get('model_input_until'), time.time())
        self.assertTrue(options['buttons'])

    def test_invalid_model_argument_shows_help_without_changing_settings(self):
        self.store.set('effort', 'high')
        self.engine.model_command('typo')
        self.assertIn('/model ultra', self.sender.sent[-1][0])
        self.assertEqual(self.engine.selected_model(), 'model-a')
        self.assertEqual(self.store.get('effort'), 'high')
        self.assertIsNone(self.store.next_job())

    def test_invalid_plain_input_after_model_menu_never_runs_task(self):
        self.engine.handle_update(self.update('/model'))
        self.engine.handle_update(self.update('wrong model input', 2))
        self.assertIn('도움말', self.sender.sent[-1][0])
        self.assertIsNone(self.store.next_job())

    def test_effort_shorthand_keeps_model_and_conversation(self):
        self.store.set('codex_thread', 'existing-thread')
        self.engine.active_model = 'model-a'
        for effort in ('high', 'max', 'ultra'):
            with self.subTest(effort=effort):
                self.engine.model_command(effort)
                self.assertEqual(self.store.get('effort'), effort)
                self.assertEqual(self.engine.selected_model(), 'model-a')
                self.assertEqual(self.store.get('codex_thread'), 'existing-thread')
                self.assertEqual(self.engine.active_model, 'model-a')

    def test_model_and_effort_can_be_selected_together(self):
        self.engine.model_command('model-b high')
        self.assertEqual(self.engine.selected_model(), 'model-b')
        self.assertEqual(self.store.get('effort'), 'high')
        self.assertEqual(self.store.get('model_input_until'), 0)

    def test_unsupported_joint_selection_does_not_partially_change_model(self):
        self.store.set('effort', 'max')
        self.engine.model_command('model-b ultra')
        self.assertEqual(self.engine.selected_model(), 'model-a')
        self.assertEqual(self.store.get('effort'), 'max')
        self.assertIn('지원하지 않는', self.sender.sent[-1][0])

    def test_unsupported_effort_help_uses_current_model_capabilities(self):
        self.store.set('model', 'model-b')
        self.store.set('effort', 'high')
        self.engine.model_command('ultra')
        text = self.sender.sent[-1][0]
        self.assertIn('/model high', text)
        self.assertNotIn('\n/model ultra\n', text)
        self.assertEqual(self.store.get('effort'), 'high')

    def test_invalid_effort_alias_includes_complete_help(self):
        self.engine.effort_command('typo')
        self.assertIn('/model model-a high', self.sender.sent[-1][0])
        self.assertIn('/model ultra', self.sender.sent[-1][0])
        self.assertIsNone(self.store.get('effort'))

    def test_plain_selection_after_model_menu_is_consumed(self):
        self.engine.handle_update(self.update('/model'))
        self.engine.handle_update(self.update('ultra', 2))
        self.assertEqual(self.store.get('effort'), 'ultra')
        self.assertIsNone(self.store.next_job())
        self.engine.handle_update(self.update('ordinary task', 3))
        self.assertEqual(json.loads(self.store.next_job()['prompt'])['text'], 'ordinary task')

    def test_expired_model_menu_does_not_capture_ordinary_tasks(self):
        self.store.set('model_input_until', time.time() - 1)
        self.engine.handle_update(self.update('ordinary task'))
        self.assertIsNotNone(self.store.next_job())

    def test_model_menu_can_be_closed_by_text_and_button(self):
        for close in (lambda: self.engine.model_command('cancel'),
                      lambda: self.engine.handle_action({'type': 'model_close'}, 'close')):
            self.engine.model_command('')
            close()
            self.assertEqual(self.store.get('model_input_until'), 0)
        self.engine.handle_update(self.update('ordinary task'))
        self.assertIsNotNone(self.store.next_job())

    def test_other_workflow_command_leaves_model_menu(self):
        self.engine.handle_update(self.update('/model'))
        self.engine.handle_update(self.update('/prompt_begin', 2))
        self.assertEqual(self.store.get('model_input_until'), 0)
        self.engine.handle_update(self.update('prompt piece', 3))
        self.assertEqual(self.store.get('prompt_buffer'), ['prompt piece'])

    def test_model_choice_inside_prompt_buffer_is_not_saved_as_prompt(self):
        self.engine.handle_update(self.update('/prompt_begin'))
        self.engine.handle_update(self.update('/model', 2))
        self.engine.handle_update(self.update('high', 3))
        self.assertEqual(self.store.get('effort'), 'high')
        self.assertEqual(self.store.get('prompt_buffer'), [])

    def test_model_input_accepts_case_and_whitespace(self):
        self.engine.handle_update(self.update('/model\tMODEL-B\tHIGH'))
        self.assertEqual(self.engine.selected_model(), 'model-b')
        self.assertEqual(self.store.get('effort'), 'high')

    def test_extra_model_arguments_do_not_mutate_settings(self):
        self.engine.model_command('model-b high extra')
        self.assertIn('모델ID와 추론 강도까지만', self.sender.sent[-1][0])
        self.assertEqual(self.engine.selected_model(), 'model-a')

    def test_unauthorized_user_cannot_answer_model_menu(self):
        self.engine.handle_update(self.update('/model'))
        self.engine.handle_update(self.update('ultra', 2, user=8))
        self.assertIsNone(self.store.get('effort'))
        self.assertGreater(self.store.get('model_input_until'), time.time())

    def test_empty_model_catalog_can_render_help(self):
        self.engine.models = lambda: []
        self.engine.model_command('bad')
        self.assertIn('모델 목록이 없습니다', self.sender.sent[-1][0])
        self.assertIsNone(self.store.next_job())


if __name__ == '__main__':
    unittest.main()
