import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from codex_telegram.config import Config, Redactor
from codex_telegram.rpc import AppServer, RPCError


SERVER = '''import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if os.environ.get('FAKE_METHOD_LOG'):
        with open(os.environ['FAKE_METHOD_LOG'],'a',encoding='utf-8') as log: print(method,file=log)
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
    if method == 'remoteControl/status/read':
        result = {'status':os.environ.get('FAKE_REMOTE_STATUS','disabled')}
    elif method == 'windowsSandbox/readiness':
        result = {'status':os.environ.get('FAKE_SANDBOX_STATUS','ready')}
    elif method == 'windowsSandbox/setupStart':
        os.environ['FAKE_SANDBOX_STATUS']='ready'
        result = {'started':True}
    elif method == 'config/read':
        if os.environ.get('FAKE_PREFLIGHT_ERROR'):
            print(json.dumps({'id':message['id'],'error':{'code':-32000,'message':'preflight unavailable'}}),flush=True)
            continue
        result = {'config':{'mcp_servers':{'project_desktop':{}}} if os.environ.get('FAKE_PROJECT_MCP') else
                  {'windows':{'sandbox':os.environ.get('FAKE_SANDBOX_MODE','mxc')},
                   'approval_policy':os.environ.get('FAKE_APPROVAL_POLICY','never')}}
    elif method == 'test/home':
        result = {'home':os.environ.get('CODEX_HOME')}
    elif method == 'test/project-env':
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
        self.cfg.child_env['FAKE_METHOD_LOG']=str(self.root/'methods.txt')
        self.rpc = AppServer(self.cfg, Redactor({'TELEGRAM_BOT_TOKEN': 'synthetic-token'}))
        self.original_popen = subprocess.Popen
        self.commands=[]
        def launch(command, **kwargs):
            self.commands.append(list(command))
            return self.original_popen([sys.executable, '-X', 'utf8', '-B', '-u', str(self.fixture)], **kwargs)
        self.patches = [patch('codex_telegram.rpc.codex_executable', return_value=sys.executable),
                        patch('codex_telegram.rpc.disable_remote_control'),
                        patch('codex_telegram.rpc.prepare_home', side_effect=lambda cfg: (cfg.codex_home.mkdir(parents=True, exist_ok=True), cfg.codex_home)[1]),
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

    def test_rpc_uses_its_own_home_even_when_parent_points_to_desktop(self):
        self.assertEqual(self.rpc.request('test/home',{})['home'],str(self.cfg.codex_home.resolve()))

    def test_remote_control_enabled_fails_before_any_conversation(self):
        self.rpc.close()
        with patch.dict(os.environ,{'FAKE_REMOTE_STATUS':'enabled'}):
            with self.assertRaisesRegex(RPCError,'원격 제어'):
                self.rpc.start()
        self.assertFalse(self.rpc.alive)

    def test_project_config_cannot_reintroduce_desktop_mcp(self):
        self.rpc.close()
        with patch.dict(os.environ,{'FAKE_PROJECT_MCP':'1'}):
            with self.assertRaisesRegex(RPCError,'MCP'):
                self.rpc.start()
        self.assertFalse(self.rpc.alive)

    def test_failed_preflight_cannot_leave_a_reusable_unverified_process(self):
        self.rpc.close()
        with patch.dict(os.environ,{'FAKE_PREFLIGHT_ERROR':'1'}):
            with self.assertRaisesRegex(RPCError,'preflight unavailable'):
                self.rpc.start()
        self.assertFalse(self.rpc.alive)
        self.assertFalse(self.rpc.ready)
        failed_generation=self.rpc.generation
        self.rpc.start()
        self.assertTrue(self.rpc.ready)
        self.assertGreater(self.rpc.generation,failed_generation)

    @unittest.skipUnless(os.name=='nt','Windows sandbox preflight')
    def test_missing_mxc_readiness_does_not_launch_elevated_setup(self):
        self.rpc.close()
        with patch.dict(os.environ,{'FAKE_SANDBOX_STATUS':'updateRequired'}):
            with self.assertRaisesRegex(RPCError,'MXC'):
                self.rpc.start()
        self.assertFalse(self.rpc.alive)
        methods=(self.root/'methods.txt').read_text().splitlines()
        self.assertNotIn('windowsSandbox/setupStart',methods)
        self.assertNotIn('thread/start',methods)

    @unittest.skipUnless(os.name=='nt','Windows setup readiness')
    def test_explicit_setup_command_cannot_launch_elevated_fallback(self):
        self.rpc.close()
        with patch.dict(os.environ,{'FAKE_SANDBOX_STATUS':'updateRequired'}):
            with self.assertRaisesRegex(RPCError,'MXC'):
                self.rpc.start(setup_sandbox=True)
        self.assertFalse(self.rpc.ready)
        self.assertFalse(self.rpc.alive)
        self.assertNotIn('windowsSandbox/setupStart',(self.root/'methods.txt').read_text().splitlines())

    @unittest.skipUnless(os.name=='nt','Windows strict MXC selection')
    def test_backend_cannot_silently_select_legacy_elevation(self):
        self.rpc.close()
        with patch.dict(os.environ,{'FAKE_SANDBOX_MODE':'elevated'}):
            with self.assertRaisesRegex(RPCError,'MXC'):
                self.rpc.start()
        self.assertFalse(self.rpc.alive)
        self.assertNotIn('windowsSandbox/setupStart',(self.root/'methods.txt').read_text().splitlines())

    def test_launcher_pins_headless_approval_and_strict_windows_backend(self):
        command=self.commands[-1]
        self.assertIn('approval_policy="never"',command)
        if os.name=='nt':
            self.assertIn('windows.sandbox="mxc"',command)
            self.assertNotIn('features.prefer_mxc=true',command)

    def test_interactive_approval_policy_cannot_reappear_from_configuration(self):
        self.rpc.close()
        with patch.dict(os.environ,{'FAKE_APPROVAL_POLICY':'on-request'}):
            with self.assertRaisesRegex(RPCError,'비대화형'):
                self.rpc.start()
        self.assertFalse(self.rpc.alive)

    def test_close_does_not_block_on_a_reader_with_inherited_pipe(self):
        self.rpc.close()
        proc=Mock();proc.poll.return_value=0
        readers=[Mock(),Mock()]
        for reader in readers:reader.is_alive.return_value=True
        with patch.object(self.rpc,'proc',proc),patch.object(self.rpc,'reader_threads',readers):
            self.rpc.close()
        proc.stdout.close.assert_not_called()
        proc.stderr.close.assert_not_called()

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
