"""Offline replay of projected real responses; mutations are counterfactual tests."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from test_doctor import load_doctor

FIXTURE=Path(__file__).parent/'fixtures/moonraker-kobra-s1-observed.json'

class ObservedHardwareTests(unittest.TestCase):
    def setUp(self):
        self.d=load_doctor()
        self.data=json.loads(FIXTURE.read_text())
        self.calls=[]

    def replay(self):
        owner=self
        class Replay:
            def __init__(self,*args):pass
            def get(self,path,objects=()):
                owner.calls.append((path,tuple(objects)))
                return copy.deepcopy(owner.data[path]['result'])
        report=self.d.Report()
        with patch.object(self.d,'Moonraker',Replay):
            self.d.network_checks(report,'http://127.0.0.1:1',False,1)
        return report

    def status(self,report,label):
        return next(s for s,k,_ in report.checks if k==label)

    def test_real_device_type_is_explicit_model_evidence(self):
        report=self.replay()
        self.assertEqual(self.status(report,'printer-model'),'PASS',report.render())

    def test_real_extension_status_is_queried_without_klipper_listing(self):
        report=self.replay()
        self.assertEqual(self.status(report,'mmu-status'),'PASS',report.render())
        self.assertIn(('/printer/objects/query',('mmu','mmu_machine')),self.calls)

    def test_disabled_observed_mmu_is_a_warning(self):
        self.data['/printer/objects/query']['result']['status']['mmu']['enabled']=False
        self.assertEqual(self.status(self.replay(),'mmu-status'),'WARN')

    def test_conflicting_explicit_mmu_gate_count_warns(self):
        self.data['/printer/objects/query']['result']['status']['mmu']['num_gates']=8
        self.assertEqual(self.status(self.replay(),'gate-count'),'WARN')

    def test_conflicting_active_unit_gate_count_warns(self):
        self.data['/printer/objects/query']['result']['status']['mmu_machine']['unit_0']['num_gates']=8
        self.assertEqual(self.status(self.replay(),'gate-count'),'WARN')

    def test_nonempty_inactive_unit_conflicts_with_declared_unit_count(self):
        self.data['/printer/objects/query']['result']['status']['mmu_machine']['unit_1']['num_gates']=4
        self.assertEqual(self.status(self.replay(),'gate-count'),'WARN')

    def test_conflicting_explicit_ace_models_cannot_pass(self):
        machine=self.data['/printer/objects/query']['result']['status']['mmu_machine']
        machine['model']='ACE Pro'
        machine['units']=[{'model':'Other MMU'}]
        self.assertEqual(self.status(self.replay(),'ace-pro'),'WARN')

    def test_invalid_top_level_machine_count_cannot_be_ignored(self):
        self.data['/printer/objects/query']['result']['status']['mmu_machine']['num_gates']=True
        self.assertEqual(self.status(self.replay(),'gate-count'),'WARN')

    def test_real_mapping_and_counts_pass_without_pro_identity(self):
        report=self.replay()
        self.assertEqual(self.status(report,'gate-count'),'PASS')
        self.assertEqual(self.status(report,'tool-gate-mapping'),'PASS')
        self.assertEqual(self.status(report,'ace-pro'),'NOTVERIFIED')
        self.assertEqual(report.exit_code,0)

    def test_component_declaration_alone_is_not_hardware_evidence(self):
        self.data['/printer/objects/query']['result']['status']={'mmu':{},'mmu_machine':{}}
        report=self.replay()
        for label in ('mmu-status','ace-pro','gate-count','tool-gate-mapping'):
            self.assertEqual(self.status(report,label),'NOTVERIFIED')

    def test_extension_requires_valid_successful_component_declaration(self):
        for change in ({'failed_components':['mmu_ace']}, {'components':'mmu_ace'},
                       {'failed_components':None}, {'klippy_connected':False}, {'klippy_state':'shutdown'}):
            with self.subTest(change=change):
                self.setUp()
                self.data['/server/info']['result'].update(change)
                report=self.replay()
                self.assertFalse(any(path=='/printer/objects/query' for path,_ in self.calls))
                self.assertEqual(self.status(report,'ace-pro'),'NOTVERIFIED')

    def test_device_type_conflicts_with_other_explicit_model(self):
        self.data['/printer/info']['result']['model']='Other printer'
        self.assertEqual(self.status(self.replay(),'printer-model'),'WARN')

    def test_unit_count_and_gate_types_are_strict(self):
        for field,value in (('num_gates',True),('num_gates','4'),('num_gates',0),
                            ('num_units',True),('num_units',-1),('num_units',3),('num_units',2)):
            with self.subTest(field=field,value=value):
                self.setUp()
                status=self.data['/printer/objects/query']['result']['status']
                (status['mmu'] if field=='num_gates' else status['mmu_machine'])[field]=value
                self.assertEqual(self.status(self.replay(),'gate-count'),'WARN')

    def test_mapping_conflicts_and_tool_names_without_gate_map(self):
        mmu=self.data['/printer/objects/query']['result']['status']['mmu']
        mmu['tool_to_gate_map']=[3,2,1,0]
        self.assertEqual(self.status(self.replay(),'tool-gate-mapping'),'WARN')
        del mmu['tool_to_gate_map'];del mmu['ttg_map']
        self.assertEqual(self.status(self.replay(),'tool-gate-mapping'),'NOTVERIFIED')

    def test_real_http_replay_uses_only_fixed_gets_and_selected_fields(self):
        from test_doctor import MockMoonraker
        from urllib.parse import urlsplit,parse_qs
        server=MockMoonraker();self.addCleanup(server.close)
        server.responses={path:copy.deepcopy(data) for path,data in self.data.items() if path.startswith('/')}
        report=self.d.Report();self.d.network_checks(report,server.url,False,1)
        self.assertEqual(self.status(report,'printer-model'),'PASS')
        self.assertEqual(self.status(report,'gate-count'),'PASS')
        self.assertEqual(self.status(report,'ace-pro'),'NOTVERIFIED')
        server.assert_safe(self)
        query=parse_qs(urlsplit(server.requests[-1][1]).query)
        self.assertEqual(set(query),{'mmu','mmu_machine'})
        self.assertIn('num_units',query['mmu_machine'][0].split(','))

    def test_active_unit_gate_offset_conflicts_with_total_gate_space(self):
        self.data['/printer/objects/query']['result']['status']['mmu_machine']['unit_0']['first_gate']=4
        self.assertEqual(self.status(self.replay(),'gate-count'),'WARN')

    def test_malformed_or_oversized_gate_array_prevents_pass(self):
        for gates in ([0,0,0,0,0,0,0,True],[0]*65,[True,0,0,0],None,{'slot':0}):
            with self.subTest(gates=gates):
                self.setUp()
                status=self.data['/printer/objects/query']['result']['status']
                status['mmu']['gate_status']=gates
                status['mmu_machine']['model']='ACE Pro'  # Counterfactual explicit identity.
                report=self.replay()
                self.assertEqual(self.status(report,'gate-count'),'WARN')
                self.assertEqual(report.exit_code,1)

if __name__=='__main__':unittest.main()
