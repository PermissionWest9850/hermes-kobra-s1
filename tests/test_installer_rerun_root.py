"""Package-complete reruns must not invoke sudo/su or a privileged bootstrap."""
import os
import subprocess
import unittest
from test_installer_safety import functions_only

class RerunRootTests(unittest.TestCase):
    def test_complete_system_rerun_needs_no_root_authentication(self):
        sh=functions_only()+'''
system_requirements_ready() { return 0; }
configure_root_access() { printf ROOT_AUTH_UNEXPECTED; exit 94; }
bootstrap_system_packages() { printf ROOT_BOOTSTRAP_UNEXPECTED; exit 95; }
check_internet_dns() { :; }
resolve_source_tree() { :; }
install_hermes_if_needed() { :; }
onboard_hermes_auth() { :; }
install_node_if_needed() { :; }
install_klippermcp() { :; }
install_freecad() { :; }
install_orcaslicer() { :; }
load_existing_kobra_profile_config() { return 0; }
create_project_dir() { :; }
install_profile_files() { :; }
prepare_orca_profiles() { :; }
install_wrapper() { :; }
check_moonraker_readonly() { :; }
final_verify() { :; }
main
'''
        r=subprocess.run(['bash','-c',sh],env=dict(os.environ,KOBRA_INSTALL_DRY_RUN='0'),capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertNotIn('ROOT_AUTH_UNEXPECTED',r.stdout)
        self.assertNotIn('ROOT_BOOTSTRAP_UNEXPECTED',r.stdout)
        self.assertIn('[9/9]',r.stdout)

    def test_missing_system_requirements_request_one_bootstrap(self):
        sh=functions_only()+'''
system_requirements_ready() { return 1; }
configure_root_access() { printf 'ROOT_REQUEST\\n'; }
bootstrap_system_packages() { printf 'ONE_BOOTSTRAP\\n'; }
check_internet_dns() { :; }
resolve_source_tree() { :; }
install_hermes_if_needed() { :; }
onboard_hermes_auth() { :; }
install_node_if_needed() { :; }
install_klippermcp() { :; }
install_freecad() { :; }
install_orcaslicer() { :; }
load_existing_kobra_profile_config() { return 0; }
create_project_dir() { :; }
install_profile_files() { :; }
prepare_orca_profiles() { :; }
install_wrapper() { :; }
check_moonraker_readonly() { :; }
final_verify() { :; }
main
'''
        r=subprocess.run(['bash','-c',sh],env=dict(os.environ,KOBRA_INSTALL_DRY_RUN='0'),capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual(r.stdout.count('ROOT_REQUEST'),1)
        self.assertEqual(r.stdout.count('ONE_BOOTSTRAP'),1)

    def test_real_preflight_requires_packages_commands_fuse_and_supported_node(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dpkg=root/'dpkg-query';node=root/'node'
            dpkg.write_text('#!/bin/sh\npkg="$3"\n[ "$pkg" = "$MISSING_PACKAGE" ] && exit 1\ncase "$pkg" in libfuse*) [ "$MISSING_FUSE" = 1 ] && exit 1;; esac\nprintf "install ok installed"\n')
            node.write_text('#!/bin/sh\n[ -n "$NODE_OPTIONS$NODE_PATH" ] && exit 96\nprintf "%s" "${TEST_NODE_VERSION:-v20.19.2}"\n')
            dpkg.chmod(0o755);node.chmod(0o755)
            script=functions_only()+'''\nneed_cmd() { [ "$1" != "${MISSING_COMMAND:-}" ]; }\nsystem_requirements_ready\n'''
            base=dict(os.environ,PATH=str(root)+':'+os.environ['PATH'],KOBRA_INSTALL_DRY_RUN='0',NODE_OPTIONS='MUST_BE_REMOVED',NODE_PATH='MUST_BE_REMOVED')
            cases=[({},0),({'MISSING_PACKAGE':'freecad'},1),({'MISSING_PACKAGE':'python3-venv'},1),
                   ({'MISSING_COMMAND':'npm'},1),({'MISSING_COMMAND':'freecadcmd'},1),
                   ({'MISSING_PACKAGE':'libfuse2'},0),({'MISSING_FUSE':'1'},1),
                   ({'TEST_NODE_VERSION':'v18.0.0'},1),({'TEST_NODE_VERSION':'v20.invalid'},1)]
            for change,expected in cases:
                with self.subTest(change=change):
                    r=subprocess.run(['bash','-c',script],env=dict(base,**change),capture_output=True,text=True)
                    self.assertEqual(r.returncode,expected,r.stdout+r.stderr)

if __name__=='__main__':unittest.main()
