import json
import os
from pathlib import Path
import tempfile
import tomllib
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_telegram.isolation import MARKER, disable_remote_control, filtered_config, headless_config, isolated_environment, prepare_home


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('CODEX_TG_TEST_TMPDIR'))
        self.root = Path(self.temp.name)
        self.desktop = self.root / 'desktop'
        self.desktop.mkdir()
        self.config = SimpleNamespace(codex_home=self.root / 'bridge' / 'codex-home',workdir=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_only_auth_config_and_instructions_are_seeded(self):
        for name in ('auth.json', 'config.toml', 'AGENTS.md', 'state_5.sqlite', 'thread_history_1.sqlite', 'history.jsonl'):
            (self.desktop / name).write_text('model="synthetic"\n' if name=='config.toml' else '{}', encoding='utf-8')
        (self.desktop / 'sessions').mkdir()
        (self.desktop / 'sessions' / 'private.jsonl').write_text('synthetic-private-history')
        home = prepare_home(self.config, source_home=self.desktop)
        self.assertEqual({p.name for p in home.iterdir()}, {'auth.json', 'config.toml', 'AGENTS.md', MARKER})
        self.assertNotIn('synthetic-private-history', ''.join(p.read_text() for p in home.iterdir()))

    def test_config_removes_inline_and_multiline_desktop_routing(self):
        source='''model="synthetic-model"
notify=[
 "desktop-helper", "turn-ended"
]
sqlite_home="desktop-database"
log_dir="desktop-log"
mcp_servers={node_repl={command="desktop-node",env={CODEX_HOME="desktop-home"}}}
[desktop]
appearanceTheme="dark"
[plugins."app:desktop"]
enabled=true
[marketplaces]
directory="desktop-cache"
[windows]
sandbox="elevated"
'''
        parsed = tomllib.loads(filtered_config(source))
        self.assertEqual(parsed, {'model':'synthetic-model','windows':{'sandbox':'elevated'}})

    def test_permissions_provider_and_project_trust_are_exactly_preserved(self):
        source='''approval_policy="on-request"
default_permissions="protected-project"
[permissions.protected-project]
extends=":workspace"
[permissions.protected-project.filesystem.":workspace_roots"]
"."="write"
".env"="deny"
"raw"="read"
[permissions.protected-project.network]
enabled=false
[projects.'X:\\Synthetic Research']
trust_level="trusted"
[model_providers.synthetic]
name="Synthetic"
base_url="https://example.invalid/api"
'''
        self.assertEqual(tomllib.loads(filtered_config(source)), tomllib.loads(source))

    def test_profile_desktop_tools_are_removed_without_losing_security(self):
        source='''profile="protected"
[profiles.protected]
approval_policy="never"
notify=["desktop"]
[profiles.protected.mcp_servers.desktop]
command="desktop-node"
[profiles.protected.features]
apps=true
js_repl=true
[profiles.protected.permissions.restricted.network]
enabled=false
'''
        profile=tomllib.loads(filtered_config(source))['profiles']['protected']
        self.assertEqual(profile, {'approval_policy':'never','features':{'js_repl':True},
                                  'permissions':{'restricted':{'network':{'enabled':False}}}})

    def test_malformed_configuration_fails_closed(self):
        with self.assertRaises(RuntimeError):
            filtered_config('default_permissions = "protected\nnotify=["desktop"]')

    def test_refreshed_credentials_and_local_settings_are_not_overwritten(self):
        (self.desktop / 'auth.json').write_text('old-synthetic-token')
        (self.desktop / 'config.toml').write_text('model="one"')
        home=prepare_home(self.config, source_home=self.desktop)
        (home / 'auth.json').write_text('refreshed-synthetic-token')
        (home / 'config.toml').write_text('model="two"')
        (self.desktop / 'auth.json').write_text('another-desktop-token')
        prepare_home(self.config, source_home=self.desktop)
        self.assertEqual((home/'auth.json').read_text(),'refreshed-synthetic-token')
        current=tomllib.loads((home/'config.toml').read_text())
        self.assertEqual(current['model'],'two')
        self.assertEqual(current['approval_policy'],'never')

    def test_preexisting_credentials_are_never_replaced(self):
        self.config.codex_home.mkdir(parents=True)
        (self.config.codex_home/'auth.json').write_text('existing-synthetic')
        (self.desktop/'auth.json').write_text('desktop-synthetic')
        prepare_home(self.config, source_home=self.desktop)
        self.assertEqual((self.config.codex_home/'auth.json').read_text(),'existing-synthetic')

    def test_child_cli_profiles_keep_permission_limits_without_elevation(self):
        original='''approval_policy="on-request"
default_permissions="protected"
[windows]
sandbox="elevated"
[permissions.protected]
extends=":workspace"
[permissions.protected.filesystem.":workspace_roots"]
"."="write"
".env"="deny"
"raw"="read"
[permissions.protected.network]
enabled=false
[profiles.nested]
approval_policy="on-request"
[profiles.nested.windows]
sandbox="elevated"
'''
        source=tomllib.loads(original)
        result=tomllib.loads(headless_config(original,windows=True))
        self.assertEqual(result['windows']['sandbox'],'mxc')
        self.assertEqual(result['profiles']['nested']['windows']['sandbox'],'mxc')
        self.assertEqual(result['approval_policy'],'never')
        self.assertEqual(result['profiles']['nested']['approval_policy'],'never')
        self.assertEqual(result['permissions'],source['permissions'])
        self.assertEqual(result['default_permissions'],source['default_permissions'])
        self.assertEqual(tomllib.loads(headless_config(headless_config(original,windows=True),windows=True)),result)

    def test_non_windows_headless_config_does_not_select_windows_backend(self):
        result=tomllib.loads(headless_config('model="synthetic"',windows=False))
        self.assertEqual(result,{'model':'synthetic','approval_policy':'never'})

    def test_legacy_private_home_is_upgraded_without_touching_desktop(self):
        original='model="synthetic"\n[windows]\nsandbox="elevated"\n'
        (self.desktop/'config.toml').write_text(original)
        home=prepare_home(self.config,source_home=self.desktop)
        (home/'config.toml').write_text(original)
        prepare_home(self.config,source_home=self.desktop)
        result=tomllib.loads((home/'config.toml').read_text())
        self.assertEqual(result['windows']['sandbox'],'mxc' if os.name=='nt' else 'elevated')
        self.assertEqual(result['approval_policy'],'never')
        self.assertEqual((self.desktop/'config.toml').read_text(),original)

    def test_default_shared_home_and_nested_home_are_rejected(self):
        for target in (self.desktop, self.desktop/'bridge-home', self.desktop.parent):
            with self.subTest(target=target):
                self.config.codex_home=target
                with self.assertRaises(RuntimeError):
                    prepare_home(self.config,source_home=self.desktop)

    def test_inherited_home_is_used_only_as_seed_source(self):
        (self.desktop/'auth.json').write_text('synthetic-auth')
        with patch.dict(os.environ, {'CODEX_HOME':str(self.desktop)}):
            home=prepare_home(self.config)
        self.assertEqual((home/'auth.json').read_text(),'synthetic-auth')

    def test_symlink_target_or_seed_file_is_rejected(self):
        destination=self.root/'physical'
        destination.mkdir()
        linked=self.root/'linked'
        try:
            linked.symlink_to(destination,target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation is unavailable')
        self.config.codex_home=linked/'codex-home'
        with self.assertRaises(RuntimeError):
            prepare_home(self.config,source_home=self.desktop)

    def test_invalid_marker_is_not_silently_reseeded(self):
        home=prepare_home(self.config,source_home=self.desktop)
        (home/MARKER).write_text('{broken')
        with self.assertRaises(RuntimeError):
            prepare_home(self.config,source_home=self.desktop)

    def test_symlink_guard_is_enforced_without_requiring_symlink_privilege(self):
        with patch('codex_telegram.isolation._reparse',side_effect=lambda path: path==self.config.codex_home):
            with self.assertRaisesRegex(RuntimeError,'symlinks'):
                prepare_home(self.config,source_home=self.desktop)

    def test_config_routing_added_after_first_boot_fails_closed(self):
        home=prepare_home(self.config,source_home=self.desktop)
        (home/'config.toml').write_text('model="synthetic"\n[mcp_servers.desktop]\ncommand="desktop-helper"')
        with self.assertRaisesRegex(RuntimeError,'desktop/storage routing'):
            prepare_home(self.config,source_home=self.desktop)

    def test_static_autoresearch_skill_is_copied_without_run_state(self):
        skill=self.desktop/'skills'/'codex-autoresearch'
        (skill/'scripts').mkdir(parents=True)
        (skill/'references').mkdir()
        (skill/'runs').mkdir()
        for relative in ('SKILL.md','scripts/helper.py','references/guide.md','autoresearch-state.json','runs/private.json'):
            (skill/relative).write_text('synthetic-'+relative)
        home=prepare_home(self.config,source_home=self.desktop)
        copied=home/'skills'/'codex-autoresearch'
        self.assertTrue((copied/'SKILL.md').is_file())
        self.assertTrue((copied/'scripts/helper.py').is_file())
        self.assertTrue((copied/'references/guide.md').is_file())
        self.assertFalse((copied/'autoresearch-state.json').exists())
        self.assertFalse((copied/'runs').exists())
        (copied/'SKILL.md').write_text('local-update')
        prepare_home(self.config,source_home=self.desktop)
        self.assertEqual((copied/'SKILL.md').read_text(),'local-update')

    def test_existing_private_home_environment_does_not_self_seed(self):
        home=prepare_home(self.config,source_home=self.desktop)
        with patch.dict(os.environ,{'CODEX_HOME':str(home)}):
            self.assertEqual(prepare_home(self.config),home)

    def test_authorization_rules_are_preserved_without_copying_other_state(self):
        rules=self.desktop/'rules';rules.mkdir()
        (rules/'protected.rules').write_text('prefix_rule(pattern=["synthetic"],decision="forbidden")')
        (rules/'runtime.json').write_text('synthetic-private-state')
        home=prepare_home(self.config,source_home=self.desktop)
        self.assertEqual((home/'rules/protected.rules').read_text(),(rules/'protected.rules').read_text())
        self.assertFalse((home/'rules/runtime.json').exists())
        (rules/'protected.rules').write_text('changed-security-rule')
        with self.assertRaisesRegex(RuntimeError,'authorization rules changed'):
            prepare_home(self.config,source_home=self.desktop)

    def test_remote_control_is_disabled_with_only_private_paths(self):
        home=prepare_home(self.config,source_home=self.desktop)
        environment=isolated_environment(self.config,{'PATH':'synthetic-path'})
        with patch('codex_telegram.isolation.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            disable_remote_control(self.config,environment,'synthetic-codex.exe')
        command=run.call_args.args[0]
        self.assertEqual(command[-3:],['app-server','daemon','disable-remote-control'])
        self.assertIn('sqlite_home='+json.dumps(str(home)),command)
        self.assertIn('log_dir='+json.dumps(str(home/'log')),command)
        self.assertEqual(run.call_args.kwargs['env']['CODEX_HOME'],str(home))
        self.assertEqual(run.call_args.kwargs['timeout'],20)

    def test_source_rule_deletion_or_rename_blocks_reuse(self):
        rules=self.desktop/'rules';rules.mkdir()
        original=rules/'allow.rules';original.write_text('synthetic-allow-rule')
        home=prepare_home(self.config,source_home=self.desktop)
        receipt=json.loads((home/MARKER).read_text())
        self.assertEqual(set(receipt['source_rules']),{'allow.rules'})
        original.unlink()
        with self.assertRaisesRegex(RuntimeError,'deleted'):
            prepare_home(self.config,source_home=self.desktop)
        (rules/'renamed.rules').write_text('synthetic-allow-rule')
        with self.assertRaisesRegex(RuntimeError,'renamed'):
            prepare_home(self.config,source_home=self.desktop)

    def test_source_rules_directory_deletion_blocks_reuse(self):
        rules=self.desktop/'rules';rules.mkdir()
        (rules/'allow.rules').write_text('synthetic-allow-rule')
        prepare_home(self.config,source_home=self.desktop)
        (rules/'allow.rules').unlink();rules.rmdir()
        with self.assertRaisesRegex(RuntimeError,'deleted'):
            prepare_home(self.config,source_home=self.desktop)

    def test_legacy_marker_is_upgraded_only_with_validated_rule_copy(self):
        rules=self.desktop/'rules';rules.mkdir()
        (rules/'protected.rules').write_text('synthetic-rule')
        home=prepare_home(self.config,source_home=self.desktop)
        receipt=json.loads((home/MARKER).read_text())
        receipt.pop('source_rules');receipt.pop('rule_source')
        (home/MARKER).write_text(json.dumps(receipt))
        prepare_home(self.config,source_home=self.desktop)
        upgraded=json.loads((home/MARKER).read_text())
        self.assertIn('protected.rules',upgraded['source_rules'])
        self.assertEqual(upgraded['rule_source'],str(self.desktop.resolve()))

    def test_legacy_marker_cannot_adopt_deleted_old_allow_rule(self):
        rules=self.desktop/'rules';rules.mkdir()
        original=rules/'allow.rules';original.write_text('synthetic-allow-rule')
        home=prepare_home(self.config,source_home=self.desktop)
        receipt=json.loads((home/MARKER).read_text())
        receipt.pop('source_rules');receipt.pop('rule_source')
        (home/MARKER).write_text(json.dumps(receipt))
        original.unlink()
        with self.assertRaisesRegex(RuntimeError,'cannot be validated'):
            prepare_home(self.config,source_home=self.desktop)
        self.assertNotIn('source_rules',json.loads((home/MARKER).read_text()))

    def test_remote_control_nonzero_or_timeout_blocks_server_start(self):
        prepare_home(self.config,source_home=self.desktop)
        environment=isolated_environment(self.config,{})
        with patch('codex_telegram.isolation.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError,'not started'):
                disable_remote_control(self.config,environment,'synthetic-codex.exe')
        with patch('codex_telegram.isolation.subprocess.run',side_effect=subprocess.TimeoutExpired('synthetic',20)):
            with self.assertRaisesRegex(RuntimeError,'not started'):
                disable_remote_control(self.config,environment,'synthetic-codex.exe')

    def test_remote_control_cannot_modify_the_desktop_home(self):
        prepare_home(self.config,source_home=self.desktop)
        with patch('codex_telegram.isolation.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError,'exact private'):
                disable_remote_control(self.config,{'CODEX_HOME':str(self.desktop)},'synthetic-codex.exe')
            run.assert_not_called()

    def test_desktop_routing_is_removed_but_security_and_auth_are_retained(self):
        env={'CODEX_HOME':'desktop','CODEX_THREAD_ID':'desktop-thread','CODEX_APP_TOOLS_PIPE_PATH':'desktop-pipe',
             'NODE_REPL_NODE_PATH':'desktop-node','SKY_CUA_NATIVE_PIPE':'desktop-ui',
             'CODEX_SANDBOX_NETWORK_DISABLED':'1','CODEX_PERMISSION_PROFILE':'managed-restrictions',
             'CODEX_WINDOWS_REGISTERED_CORE':'1','CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY':'desktop-helper',
             'CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY':'stale-task','BROWSER_USE_ACCESS_NATIVE_PIPE':'desktop-pipe',
             'CODEX_API_KEY':'synthetic-key','PATH':'synthetic-path'}
        result=isolated_environment(self.config,env)
        self.assertEqual(result['CODEX_HOME'],str(self.config.codex_home.resolve()))
        for key in ('CODEX_THREAD_ID','CODEX_APP_TOOLS_PIPE_PATH','NODE_REPL_NODE_PATH','SKY_CUA_NATIVE_PIPE',
                    'CODEX_WINDOWS_REGISTERED_CORE','CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY',
                    'CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY','BROWSER_USE_ACCESS_NATIVE_PIPE'):
            self.assertNotIn(key,result)
        for key in ('CODEX_SANDBOX_NETWORK_DISABLED','CODEX_PERMISSION_PROFILE','CODEX_API_KEY','PATH'):
            self.assertEqual(result[key],env[key])
        self.assertEqual(env['CODEX_HOME'],'desktop')
