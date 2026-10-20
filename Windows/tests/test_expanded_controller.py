from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Windows'))
sys.path.insert(0, str(ROOT / 'RemoteAgent'))
import controller
from modelmatrix_agent import AgentConfiguration
from Windows.tests.test_capture_usage import FIXTURE, free_port


class ExpandedControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = self.root / 'profiles with spaces'
        self.profiles.mkdir()
        config = AgentConfiguration(root=self.root, profiles_dir=self.profiles, host='127.0.0.1', port=free_port(), auth_token=None)
        config.relay_port = free_port()
        self.service = controller.LocalService(config)
        self.service.bootstrap()
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.service.stop_all_managed()
        self.temp.cleanup()

    def route(self, method, path, body=None, query=None):
        result = self.service.module_route(method, path, query or {}, body or {})
        self.assertIsNotNone(result, path)
        self.assertEqual(result[0], 200, result)
        return result[1]

    def record(self, **changes):
        row = dict(started_at=time.time(), client_id='client-one', client_label='fixture client', model='fixture-model', model_requested='fixture-model', profile='fixture', runtime='llama.cpp', tier='local', endpoint='/v1/chat/completions', method='POST', status=200, stream=0, prompt_tokens=3, completion_tokens=4, duration_ms=100, ttft_ms=10, tokens_per_second=40)
        row.update(changes)
        self.service.usage.store.record(row)

    def test_profile_crud_round_trip_and_invalid_names(self):
        values = {'RUNTIME':'external','REQUEST_MODEL':'model','PORT':'54321'}
        with mock.patch.object(controller, '_profile_running', return_value=False):
            self.route('POST','/api/profiles/create',{'profile':'One','values':values})
            self.assertEqual(self.route('GET','/api/profiles/document',query={'profile':'One'})['values']['REQUEST_MODEL'],'model')
            self.route('POST','/api/profiles/duplicate',{'profile':'One','new_profile':'Two'})
            self.route('POST','/api/profiles/save',{'profile':'Three','original_profile':'Two','text':'REQUEST_MODEL=new\r\nRUNTIME=external\r\nPORT=54322\r\n'})
            self.assertFalse((self.profiles/'Two.env').exists())
            self.assertEqual(self.route('GET','/api/profiles/document',query={'profile':'Three'})['values']['REQUEST_MODEL'],'new')
            self.route('POST','/api/profiles/delete',{'profile':'Three'})
            self.assertEqual([x['name'] for x in self.route('GET','/api/profiles')['profiles']],['One'])
        for name in ('CON','CON.env','lpt9.profile','../escape','name.','name '):
            with self.assertRaises(controller.UsageError): controller._profile_name(name)
        with self.assertRaises(controller.UsageError):
            self.route('POST','/api/profiles/create',{'profile':'one','values':values})

    def test_usage_filters_pagination_rename_csv_reset_and_previous_period(self):
        self.record()
        self.record(started_at=time.time()-90000, client_id='client-two', tier='cloud', completion_tokens=9)
        summary = self.route('GET','/api/usage/summary',query={'range':'24h','host':'local'})
        self.assertEqual(summary['requests'],1)
        self.assertEqual(summary['previous']['requests'],1)
        self.assertEqual(summary['previous']['tokens'],12)
        overview = self.route('GET','/api/usage/overview',query={'range':'24h','host':'local'})
        self.assertEqual(overview['summary']['previous']['requests'],1)
        self.assertTrue(overview['timeseries'])
        self.route('POST','/api/usage/clients/rename',{'client_id':'client-one','name':'Owned fixture'})
        requests = self.route('GET','/api/usage/requests',query={'range':'all','q':'Owned fixture','limit':'1','offset':'0'})
        self.assertEqual(requests['total'],1)
        self.assertEqual(requests['requests'][0]['client_name'],'Owned fixture')
        self.assertEqual(len(self.route('GET','/api/usage/models',query={'range':'all'})['models']),1)
        self.assertEqual(len(self.route('GET','/api/usage/clients',query={'range':'all'})['clients']),2)
        self.assertEqual(self.route('GET','/api/usage/summary',query={'range':'all','tier':'cloud'})['requests'],1)
        code,data,headers=self.service.usage_local.csv({'range':'all'})
        self.assertEqual(code,200); self.assertIn(b'Owned fixture',data); self.assertIn('text/csv',headers['Content-Type'])
        self.assertIn('in_flight',self.route('GET','/api/usage/live'))
        with self.assertRaises(controller.UsageError): self.route('POST','/api/usage/reset',{})
        self.route('POST','/api/usage/reset',{'confirm':True})
        self.assertEqual(self.route('GET','/api/usage/summary')['requests'],0)

    def test_recording_enablement_and_retention_are_real(self):
        self.record(started_at=time.time()-3*86400)
        state=self.route('POST','/api/usage/settings',{'retention_days':1})
        self.assertEqual(state['requests_recorded'],0)
        self.route('POST','/api/usage/settings',{'enabled':False})
        self.record()
        self.assertEqual(self.service.usage.store.count(),0)
        self.assertFalse(self.route('GET','/api/usage/settings')['enabled'])
        self.route('POST','/api/usage/settings',{'enabled':True})
        self.record()
        self.assertEqual(self.service.usage.store.count(),1)
        for body in ({'enabled':'false'},{'retention_days':True},{'retention_days':0}):
            with self.assertRaises(controller.UsageError): self.route('POST','/api/usage/settings',body)

    def test_optimize_protects_latest_activity_and_measures_memory(self):
        self.record(started_at=time.time()-1000)
        self.record(started_at=time.time()-5)
        status={'statuses':[{'profile':'fixture','running':True,'pid':999,'rss_mb':2}]}
        memory={'memory':{'total_mb':1000,'used_mb':500,'available_mb':500,'percent':50}}
        with mock.patch.object(self.service,'ollama_status',return_value={'loaded':[]}), mock.patch.object(self.service,'status_payload',return_value=status), mock.patch.object(controller,'host_metrics_payload',return_value=memory), mock.patch('native_windows.inventory_apps',return_value=[]), mock.patch.object(self.service,'stop') as stop:
            plan=self.route('GET','/api/optimize/plan')
            self.assertTrue(plan['items'][0]['in_use'])
            result=self.route('POST','/api/optimize/run',{'items':['profile:fixture']})
            self.assertTrue(result['results'][0]['skipped']); stop.assert_not_called()
            self.assertEqual(result['before']['used_bytes'],500*1024*1024)
            self.route('POST','/api/usage/reset',{'confirm':True})
            result=self.route('POST','/api/optimize/run',{'items':['profile:fixture']})
            stop.assert_called_once_with('fixture')
            self.assertIn('freed_bytes',result)

    def test_controller_metadata_static_pages_and_invalid_host(self):
        self.server=controller.LocalHTTPServer(('127.0.0.1',0),self.service,None,ROOT/'Dashboard'/'dist')
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{self.server.server_port}'
        for path in ('/api/status','/api/host/metrics','/api/profiles','/api/profiles/runtimes','/api/dashboard/info','/api/usage/overview','/api/hf/settings','/api/bridge/state','/api/connect/targets'):
            with urllib.request.urlopen(base+path,timeout=8) as response:
                self.assertEqual(response.status,200,path); self.assertIsInstance(json.loads(response.read()),dict)
        info=self.route('GET','/api/dashboard/info')
        self.assertTrue(info['capabilities']['optimize'])
        self.assertEqual(info['platform']['credential_store'],'Windows DPAPI')
        with urllib.request.urlopen(base+'/',timeout=5) as response:
            self.assertIn(b'<html',response.read().lower())

    def test_running_capture_disable_restores_public_port_and_enable_restores_metering(self):
        fixture=self.root/'owned fixture.py'; fixture.write_text(FIXTURE,encoding='utf-8'); public=free_port()
        values={'REQUEST_MODEL':'fixture-model','SERVER_MODEL_ID':'fixture-model','RUNTIME':'llama.cpp','PORT':str(public),'HOST':'127.0.0.1','CAPTURE_MODE':'1','HEALTHCHECK_MODE':'http-200','START_COMMAND_JSON':json.dumps([sys.executable,str(fixture),'{port}'])}
        self.service.save_profile('fixture',values,True)
        self.service.start('fixture')
        def ready():
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                row=self.service.status_payload(['fixture'])['statuses'][0]
                if row.get('ready'):return row
                time.sleep(.1)
            self.fail(f'fixture not ready: {row}')
        ready(); self.assertTrue(self.service.capture_state()[0]['listening'])
        self.service.capture('fixture',False); ready()
        self.assertNotIn('fixture',self.service.capture_ports)
        self.assertNotIn('fixture',self.service.capture_relays)
        with urllib.request.urlopen(f'http://127.0.0.1:{public}/v1/models',timeout=4) as response:self.assertEqual(response.status,200)
        self.service.capture('fixture',True); ready()
        self.assertTrue(self.service.capture_state()[0]['listening'])
        self.service.stop('fixture')
        with socket.socket() as probe:
            probe.settimeout(.2)
            self.assertNotEqual(probe.connect_ex(('127.0.0.1',public)),0)


if __name__ == '__main__':
    unittest.main()
