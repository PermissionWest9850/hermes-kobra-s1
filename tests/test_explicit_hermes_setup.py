"""Hermes installation must not implicitly start the OAuth/setup wizard."""
import os,subprocess,tempfile,unittest
from pathlib import Path
from test_installer_safety import functions_only

class ExplicitHermesSetupTests(unittest.TestCase):
    def test_official_installer_is_invoked_with_skip_setup(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);shims=root/'bin';shims.mkdir()
            curl=shims/'curl'
            curl.write_text('''#!/usr/bin/env python3
import sys
print('''+repr('''#!/usr/bin/env bash
set -eu
printf '%s\\n' "$@" > "$TEST_INSTALL_ARGS"
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$HOME/.local/bin/hermes"
chmod +x "$HOME/.local/bin/hermes"
''')+''')
''');curl.chmod(0o755)
            env=dict(os.environ,HOME=str(root),HERMES_HOME=str(root/'.hermes'),PATH=str(shims)+':'+os.environ['PATH'],TEST_INSTALL_ARGS=str(root/'args'))
            cmd=functions_only()+'''\nneed_cmd() { if [[ "$1" == hermes ]]; then [[ -x "$HOME/.local/bin/hermes" ]]; else command -v "$1" >/dev/null; fi; }\ninstall_hermes_if_needed\n'''
            r=subprocess.run(['bash','-c',cmd],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertEqual((root/'args').read_text().splitlines(),['--skip-setup'])

    def test_fresh_auto_defaults_are_pending_without_starting_setup(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'.hermes').mkdir()
            env=dict(os.environ,HOME=str(root),HERMES_HOME=str(root/'.hermes'),TEST_SETUP_CALL=str(root/'setup-called'))
            helper=root/'source/scripts/hermes_setup_status.py';helper.parent.mkdir(parents=True)
            helper.write_text("print('missing')\n")
            cmd=functions_only()+'''\nhermes() { case "$*" in
 "config get model.provider") printf 'auto\\n';;
 "config get model.default") printf 'anthropic/claude-opus-4.6\\n';;
 setup*) printf called > "$TEST_SETUP_CALL"; return 93;;
 *) return 1;; esac; }
work_src="$HOME/source"
onboard_hermes_auth
'''
            r=subprocess.run(['bash','-c',cmd],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertIn('Hermes auth/setup pending',r.stdout+r.stderr)
            self.assertIn('hermes model',r.stdout+r.stderr)
            self.assertNotIn('Existing Hermes configuration found',r.stdout+r.stderr)
            self.assertFalse((root/'setup-called').exists())

    def test_final_summary_keeps_auth_pending_visible(self):
        with tempfile.TemporaryDirectory() as td:
            env=dict(os.environ,HOME=td,HERMES_HOME=td+'/.hermes')
            cmd=functions_only()+'''\nHERMES_SETUP_STATE=missing
KLIPPER_MCP_PATH="$HOME/mcp"; FREECADCMD_PATH=/usr/bin/true
ORCA_SLICER_PATH=/usr/bin/true; ORCA_PROFILE_ROOT="$HOME/orca"
PROJECT_DIR="$HOME/3d-print"
need_cmd() { [[ "$1" == hermes ]]; }
final_verify
'''
            r=subprocess.run(['bash','-c',cmd],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertIn('Hermes auth/setup pending',r.stdout+r.stderr)
            self.assertIn('hermes model',r.stdout+r.stderr)
            self.assertIn('not tested',r.stdout+r.stderr)

    def test_existing_local_credentials_reused_without_any_hermes_command(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);home=root/'.hermes';home.mkdir()
            payload={'config.yaml':b'PRIVATE_CONFIG_SENTINEL\n','.env':b'FAKE_TEST_SECRET=PRIVATE_SECRET_SENTINEL\n','auth.json':b'{"synthetic_test_only":true}\n'}
            for name,data in payload.items():(home/name).write_bytes(data)
            helper=root/'source/scripts/hermes_setup_status.py';helper.parent.mkdir(parents=True);helper.write_text("print('local_credentials_present')\n")
            before={n:((home/n).read_bytes(),(home/n).stat().st_mtime_ns,(home/n).stat().st_mode) for n in payload}
            env=dict(os.environ,HOME=str(root),HERMES_HOME=str(home),TEST_CALL=str(root/'unexpected-hermes'))
            cmd=functions_only()+'''\nwork_src="$HOME/source"
hermes() { printf unexpected > "$TEST_CALL"; return 93; }
onboard_hermes_auth
onboard_hermes_auth
'''
            r=subprocess.run(['bash','-c',cmd],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertIn('Existing Hermes configuration found',r.stdout+r.stderr)
            self.assertNotIn('Hermes auth/setup pending',r.stdout+r.stderr)
            self.assertFalse((root/'unexpected-hermes').exists())
            self.assertNotIn('PRIVATE_SECRET_SENTINEL',r.stdout+r.stderr)
            self.assertNotIn('PRIVATE_CONFIG_SENTINEL',r.stdout+r.stderr)
            after={n:((home/n).read_bytes(),(home/n).stat().st_mtime_ns,(home/n).stat().st_mode) for n in payload}
            self.assertEqual(before,after)

    def test_probe_noise_errors_and_pending_states_never_log_secrets_or_start_setup(self):
        for status in ['missing','unknown','refresh_needed','PRIVATE_SECRET_SENTINEL']:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as td:
                root=Path(td);helper=root/'source/scripts/hermes_setup_status.py';helper.parent.mkdir(parents=True)
                helper.write_text("import sys\nprint("+repr(status)+")\nprint('PRIVATE_STDERR_SENTINEL',file=sys.stderr)\n")
                env=dict(os.environ,HOME=str(root),HERMES_HOME=str(root/'.hermes'),TEST_CALL=str(root/'unexpected-hermes'))
                cmd=functions_only()+'''\nwork_src="$HOME/source"
hermes() { printf unexpected > "$TEST_CALL"; return 93; }
onboard_hermes_auth
'''
                r=subprocess.run(['bash','-c',cmd],env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertIn('Hermes auth/setup pending',r.stdout+r.stderr)
                self.assertNotIn('PRIVATE_SECRET_SENTINEL',r.stdout+r.stderr)
                self.assertNotIn('PRIVATE_STDERR_SENTINEL',r.stdout+r.stderr)
                self.assertFalse((root/'unexpected-hermes').exists())

    def test_pending_auth_does_not_stop_nine_step_install_or_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);helper=root/'source/scripts/hermes_setup_status.py';helper.parent.mkdir(parents=True);helper.write_text("print('missing')\n")
            env=dict(os.environ,HOME=str(root),HERMES_HOME=str(root/'.hermes'),TEST_CALL=str(root/'unexpected-hermes'))
            cmd=functions_only()+'''\nsystem_requirements_ready() { return 0; }
configure_root_access() { exit 94; }
bootstrap_system_packages() { exit 95; }
check_internet_dns() { :; }
resolve_source_tree() { work_src="$HOME/source"; }
install_hermes_if_needed() { :; }
hermes() { printf unexpected > "$TEST_CALL"; return 93; }
install_node_if_needed() { :; }
install_klippermcp() { KLIPPER_MCP_PATH="$HOME/mcp"; }
install_freecad() { FREECADCMD_PATH=/usr/bin/true; }
install_orcaslicer() { ORCA_SLICER_PATH=/usr/bin/true; ORCA_PROFILE_ROOT="$HOME/orca"; }
load_existing_kobra_profile_config() { PROJECT_DIR="$HOME/3d-print"; return 0; }
create_project_dir() { :; }
install_profile_files() { :; }
prepare_orca_profiles() { :; }
install_wrapper() { :; }
check_moonraker_readonly() { :; }
main
'''
            for _ in range(2):
                r=subprocess.run(['bash','-c',cmd],env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertIn('[9/9]',r.stdout+r.stderr)
                self.assertIn('Hermes auth/setup pending',r.stdout+r.stderr)
                self.assertFalse((root/'unexpected-hermes').exists())

    def test_probe_uses_installed_hermes_interpreter_in_isolated_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);helper=root/'source/scripts/hermes_setup_status.py';helper.parent.mkdir(parents=True);helper.write_text("print('missing')\n")
            python=root/'.hermes/hermes-agent/.venv/bin/python';python.parent.mkdir(parents=True)
            python.write_text('''#!/bin/sh
printf '%s\\n' "$@" > "$HOME/probe-args"
exec /usr/bin/python3 "$@"
''');python.chmod(0o755)
            env=dict(os.environ,HOME=str(root),HERMES_HOME=str(root/'.hermes'))
            r=subprocess.run(['bash','-c',functions_only()+'\nwork_src="$HOME/source"\nonboard_hermes_auth\n'],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertTrue((root/'probe-args').exists(),'installed interpreter was not used')
            self.assertEqual((root/'probe-args').read_text().splitlines()[:2],['-I','-B'])
            self.assertIn('Hermes auth/setup pending',r.stdout+r.stderr)

if __name__=='__main__':unittest.main()
