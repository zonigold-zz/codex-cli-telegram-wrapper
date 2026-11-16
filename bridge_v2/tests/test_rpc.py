import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_telegram.config import Config, Redactor
from codex_telegram.rpc import AppServer, RPCError


SERVER = '''import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialized': continue
    if method == 'test/timeout': continue
    if method == 'test/exit': break
    if method == 'test/error':
        print(json.dumps({'id':message['id'],'error':{'code':-32600,
                          'message':'synthetic-token already has an active writer'}}), flush=True)
        continue
    if method == 'test/approval':
        print(json.dumps({'id': 9000, 'method': 'item/commandExecution/requestApproval',
                          'params': {'threadId': 'thread-1', 'turnId': 'turn-1'}}), flush=True)
    if method == 'test/project-env':
        result = {'license': os.environ.get('FS_LICENSE'), 'bot': 'TELEGRAM_BOT_TOKEN' in os.environ}
    elif method == 'test/env':
        result = {key: key in os.environ for key in ['TELEGRAM_BOT_TOKEN', 'CODEX_TG_TEST_SECRET']}
    else:
        result = {'echo': message.get('params', {})}
    if method:
        print(json.dumps({'id': message['id'], 'result': result}), flush=True)
    elif message.get('id') == 9000:
        print(json.dumps({'method': 'serverRequest/resolved', 'params': {'requestId': 9000}}), flush=True)
'''


class RPCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('CODEX_TG_TEST_TMPDIR'))
        self.root = Path(self.temp.name)
        self.fixture = self.root / 'fixture.py'
        self.fixture.write_text(SERVER, encoding='utf-8')
        self.cfg = Config(self.root, self.root, 'synthetic-token', 7, -10)
        self.rpc = AppServer(self.cfg, Redactor({'TELEGRAM_BOT_TOKEN': 'synthetic-token'}))
        self.original_popen = subprocess.Popen
        def launch(command, **kwargs):
            return self.original_popen([sys.executable, '-X', 'utf8', '-B', '-u', str(self.fixture)], **kwargs)
        self.patches = [patch('codex_telegram.rpc.codex_executable', return_value=sys.executable),
                        patch('codex_telegram.rpc.subprocess.Popen', side_effect=launch)]
        for item in self.patches: item.start()
        self.rpc.start()

    def tearDown(self):
        self.rpc.close()
        for stream in (self.rpc.proc.stdout, self.rpc.proc.stderr):
            stream.close()
        for item in self.patches: item.stop()
        self.temp.cleanup()

    def test_initialize_and_unicode_rpc(self):
        self.assertEqual(self.rpc.request('test/echo', {'text': '한글 😀'})['echo']['text'], '한글 😀')

    def test_response_timeout_is_bounded(self):
        with self.assertRaises(RPCError):
            self.rpc.request('test/timeout', {}, timeout=.05)
        self.assertEqual(self.rpc.pending, {})

    def test_structured_error_code_survives_with_secret_redaction(self):
        with self.assertRaises(RPCError) as caught:
            self.rpc.request('test/error', {})
        self.assertEqual(caught.exception.code, -32600)
        self.assertIn('already has an active writer', str(caught.exception))
        self.assertNotIn('synthetic-token', str(caught.exception))
        self.assertEqual(self.rpc.pending, {})

    def test_disconnect_fails_pending_request(self):
        with self.assertRaises(RPCError):
            self.rpc.request('test/exit', {}, timeout=2)

    def test_server_request_and_response_round_trip(self):
        self.rpc.request('test/approval', {})
        event = self.rpc.events.get(timeout=1)
        self.assertEqual(event['id'], 9000)
        self.rpc.respond(9000, {'decision': 'decline'}, event['_generation'])
        resolved = self.rpc.events.get(timeout=1)
        self.assertEqual(resolved['method'], 'serverRequest/resolved')

    def test_expired_generation_cannot_respond(self):
        with self.assertRaises(RPCError):
            self.rpc.respond(9000, {'decision': 'accept'}, self.rpc.generation - 1)

    def test_bot_secret_is_not_inherited_by_codex(self):
        self.rpc.close()
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'synthetic-token', 'CODEX_TG_TEST_SECRET': 'synthetic-secret'}):
            self.rpc.start()
        self.assertEqual(self.rpc.request('test/env', {}), {'TELEGRAM_BOT_TOKEN': False, 'CODEX_TG_TEST_SECRET': False})

    def test_project_tool_path_is_inherited_without_bot_token(self):
        self.rpc.close()
        self.cfg.child_env={'FS_LICENSE':'synthetic-license-path','TELEGRAM_BOT_TOKEN':'synthetic-token'}
        self.rpc.start()
        self.assertEqual(self.rpc.request('test/project-env',{}),{'license':'synthetic-license-path','bot':False})


if __name__ == '__main__':
    unittest.main()
