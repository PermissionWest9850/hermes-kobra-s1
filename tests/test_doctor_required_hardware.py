"""Release-critical fields versus an explicitly unexposed ACE marketing identity."""
import unittest
from test_doctor import load_doctor
import test_doctor_observed_hardware as observed

class RequiredHardwarePolicyTests(unittest.TestCase):
    def test_unexposed_exact_identity_is_informational(self):
        d=load_doctor();r=d.Report()
        r.add('NOTVERIFIED','ace-pro','exact ACE model identity not exposed by API')
        self.assertEqual(r.exit_code,0)
        self.assertIn('NOT VERIFIED',r.render())

    def test_missing_enabled_field_remains_release_critical(self):
        case=observed.ObservedHardwareTests();case.setUp()
        del case.data['/printer/objects/query']['result']['status']['mmu']['enabled']
        report=case.replay()
        self.assertEqual(report.exit_code,1)
        self.assertEqual(case.status(report,'mmu-enabled'),'NOTVERIFIED')

    def test_required_hardware_uncertainty_and_conflicts_are_not_informational(self):
        mutations=[('info','device_type','Other printer'),('mmu','enabled',False),
                   ('mmu','enabled','true'),('mmu','num_gates',8),
                   ('mmu','ttg_map',[0,0,2,3]),('machine','unit_0',{'num_gates':8}),
                   ('mmu','gate_status',[True,0,0,0])]
        for obj,key,value in mutations:
            with self.subTest(obj=obj,key=key,value=value):
                case=observed.ObservedHardwareTests();case.setUp()
                status=case.data['/printer/objects/query']['result']['status']
                target=case.data['/printer/info']['result'] if obj=='info' else status['mmu_machine' if obj=='machine' else 'mmu']
                target[key]=value
                self.assertNotEqual(case.replay().exit_code,0)

    def test_only_notverified_ace_identity_is_exempt(self):
        d=load_doctor()
        for state,label,expected in [('WARN','ace-pro',1),('FAIL','ace-pro',2),
                                     ('NOTVERIFIED','gate-count',1),('NOTVERIFIED','tool-gate-mapping',1),
                                     ('NOTVERIFIED','mmu-status',1),('NOTVERIFIED','mmu-enabled',1),
                                     ('NOTVERIFIED','printer-model',1)]:
            with self.subTest(state=state,label=label):
                r=d.Report();r.add(state,label,'fixture');self.assertEqual(r.exit_code,expected)

    def test_real_observed_packet_passes_required_checks_but_keeps_honest_identity(self):
        case=observed.ObservedHardwareTests();case.setUp();report=case.replay()
        self.assertEqual(report.exit_code,0)
        self.assertIn('NOT VERIFIED ace-pro: exact ACE model identity not exposed by API',report.render())
        self.assertEqual(case.status(report,'mmu-enabled'),'PASS')

if __name__=='__main__':unittest.main()
