import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from common import unity_license as license


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.valid = license.LicenseStatus(True, self.now + timedelta(days=30), 'valid')
        self.expired = license.LicenseStatus(False, self.now - timedelta(days=1), 'expired')

    def output(self, days=30, rights=True, xml_rights=True):
        p = self.home / 'license.xml'
        date = (self.now + timedelta(days=days)).isoformat()
        ent = '<Entitlement Id="com.unity.editor"/><Entitlement Id="com.unity.editor.ui"/>' if xml_rights else '<Entitlement Id="other"/>'
        p.write_text(f'<root><License><EntitlementGroups><EntitlementGroup><Context><UpdateDate>{date}</UpdateDate></Context><Entitlements>{ent}</Entitlements></EntitlementGroup></EntitlementGroups></License></root>')
        return f'Path: {p}\n\tcom.unity.editor\n\tcom.unity.editor.ui\n' if rights else f'Path: {p}\n\tasset.store\n'

    def test_valid_entitlements(self):
        self.assertTrue(license.parse_status(self.output(), self.now).valid)

    def test_expired(self):
        self.assertFalse(license.parse_status(self.output(days=-1), self.now).valid)

    def test_asset_store_is_not_editor(self):
        self.assertFalse(license.parse_status(self.output(rights=False), self.now).valid)

    def test_client_rights_must_match_xml_group(self):
        self.assertFalse(license.parse_status(self.output(xml_rights=False), self.now).valid)

    def test_client_failure_is_not_valid(self):
        with patch.object(license.subprocess, 'run', return_value=Mock(returncode=1)):
            self.assertFalse(license.check('/fake/Unity').valid)

    def test_bad_xml_is_not_valid(self):
        output=self.output(); (self.home/'license.xml').write_text('bad')
        with patch.object(license.subprocess, 'run', return_value=Mock(returncode=0, stdout=output)):
            self.assertFalse(license.check('/fake/Unity').valid)

    def test_healthy_does_not_launch_hub(self):
        with patch.object(license,'check',return_value=self.valid), patch.object(license,'refresh') as r, patch.object(license,'_report_recovered'):
            self.assertEqual(license.ensure('editor'),self.valid);r.assert_not_called()

    def test_near_deadline_updates(self):
        near=license.LicenseStatus(True,self.now+timedelta(days=2),'near')
        with patch.object(license,'check',return_value=near), patch.object(license,'refresh',return_value=self.valid) as r, patch.object(license,'_report_recovered'):
            self.assertEqual(license.ensure('editor'),self.valid);r.assert_called_once()

    def test_update_failure_valid_continues(self):
        with patch.object(license,'check',return_value=self.valid), patch.object(license,'refresh',side_effect=RuntimeError('locked')), patch.object(license,'_report_failure') as report:
            self.assertEqual(license.ensure('editor',force=True),self.valid);report.assert_called_once()

    def test_update_failure_expired_stops(self):
        with patch.object(license,'check',return_value=self.expired), patch.object(license,'refresh',side_effect=RuntimeError('locked')), patch.object(license,'_report_failure'):
            with self.assertRaises(license.UnityLicenseError):license.ensure('editor')

    def test_error_log_fails_fast(self):
        p=self.home/'unity.log';p.write_text('No valid Unity Editor license found')
        with self.assertRaises(license.UnityLicenseError):license.raise_if_license_error(p)

    def test_no_log_is_allowed(self):
        license.raise_if_license_error(self.home/'missing')

    def test_daily_failure_suppression_and_recovery(self):
        with patch.object(license.Path,'home',return_value=self.home), patch.object(license.notify,'send',return_value=True) as send:
            license._report_failure('expired');license._report_failure('expired')
            self.assertEqual(send.call_count,1)
            license._report_recovered();self.assertEqual(send.call_count,2)
            self.assertFalse((self.home/'.cache/bottan/unity-license-notify.json').exists())

    def test_notification_failure_does_not_suppress_future_attempt(self):
        with patch.object(license.Path,'home',return_value=self.home), patch.object(license.notify,'send',return_value=False) as send:
            license._report_failure('expired');license._report_failure('expired')
            self.assertEqual(send.call_count,2)

    def test_license_error_is_not_retried(self):
        from common.llm import retry
        fn=Mock(side_effect=license.UnityLicenseError('expired'))
        with self.assertRaises(license.UnityLicenseError):retry('Unity',fn)
        self.assertEqual(fn.call_count,1)

    def test_hub_start_failure_cleans_display(self):
        display=Mock()
        with patch.object(license.shutil,'which',return_value='/hub'), patch.object(license,'close_hub'), patch.object(license.Path,'exists',return_value=True), patch.object(license.xvfb,'start_xvfb',return_value=(display,':150')), patch.object(license.subprocess,'Popen',side_effect=OSError('fail')):
            with self.assertRaises(OSError):license.refresh('editor',self.expired)
            display.terminate.assert_called_once()

    def test_refresh_waits_for_new_deadline_not_old_success(self):
        proc=Mock();proc.poll.return_value=None
        display=Mock()
        with patch.object(license.shutil,'which',return_value='/hub'), patch.object(license,'close_hub'), patch.object(license.Path,'exists',return_value=True), patch.object(license.xvfb,'start_xvfb',return_value=(display,':150')), patch.object(license.subprocess,'Popen',return_value=proc), patch.object(license,'check',return_value=self.valid), patch.object(license,'env_float',return_value=0):
            with self.assertRaises(license.UnityLicenseError):license.refresh('editor',self.valid)
            display.terminate.assert_called_once()

    def test_refresh_success_closes_hub_and_display(self):
        proc=Mock();proc.poll.return_value=None
        display=Mock()
        with patch.object(license.shutil,'which',return_value='/hub'), patch.object(license,'close_hub') as close, patch.object(license.Path,'exists',return_value=True), patch.object(license.xvfb,'start_xvfb',return_value=(display,':150')), patch.object(license.subprocess,'Popen',return_value=proc), patch.object(license,'check',return_value=self.valid):
            self.assertEqual(license.refresh('editor',self.expired),self.valid)
            self.assertEqual(close.call_count,2)
            display.terminate.assert_called_once()

    def test_dead_hub_is_a_bounded_failure(self):
        proc=Mock();proc.poll.return_value=1
        display=Mock()
        with patch.object(license.shutil,'which',return_value='/hub'), patch.object(license,'close_hub'), patch.object(license.Path,'exists',return_value=True), patch.object(license.xvfb,'start_xvfb',return_value=(display,':150')), patch.object(license.subprocess,'Popen',return_value=proc), patch.object(license,'check',return_value=self.expired):
            with self.assertRaisesRegex(license.UnityLicenseError,'更新前に終了'):
                license.refresh('editor',self.expired)
            display.terminate.assert_called_once()

    def test_busy_render_skips_periodic_update(self):
        import builtins
        import fcntl
        original_open=builtins.open
        p=self.home/'render.lock'
        with original_open(p,'a') as owner:
            fcntl.flock(owner,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('sys.argv',['license']), patch('builtins.open',side_effect=lambda *a,**kw: original_open(p,'a')), patch.object(license,'ensure') as ensure:
                self.assertEqual(license.main(),0)
                ensure.assert_not_called()

    def test_old_deadline_does_not_prove_refresh(self):
        proc=Mock();proc.poll.return_value=None
        display=Mock()
        with patch.object(license.shutil,'which',return_value='/hub'), patch.object(license,'close_hub'), patch.object(license.Path,'exists',return_value=True), patch.object(license.xvfb,'start_xvfb',return_value=(display,':150')), patch.object(license.subprocess,'Popen',return_value=proc), patch.object(license,'check',return_value=self.valid), patch.object(license.time,'monotonic',side_effect=[0,0,121]), patch.object(license.time,'sleep'):
            with self.assertRaisesRegex(license.UnityLicenseError,'時間内'):
                license.refresh('editor',self.valid)


if __name__=='__main__':unittest.main()
