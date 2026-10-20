import sys,tempfile,unittest
from pathlib import Path
from unittest import mock
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'Windows'),str(ROOT/'RemoteAgent')]
import controller
from hf_local import LocalHF

class LivePCCorrections(unittest.TestCase):
    def test_hub_tree_lfs_oid_is_verified_sha256_and_revision_is_honored(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);service=SimpleNamespace(configuration=SimpleNamespace(root=root,profiles_directory=root/'profiles',port=0));hf=LocalHF(service)
            urls=[]
            def fetch(url,token=None,**kwargs):
                urls.append(url)
                if '/tree/' in url:return [{'type':'file','path':'m-Q8_0.gguf','size':1024,'oid':'b'*40,'lfs':{'oid':'a'*64,'size':1024}}]
                return {'sha':'c'*40,'pipeline_tag':'text-generation','cardData':{'license':'apache-2.0'}}
            try:
                with mock.patch.object(hf,'_request_json',side_effect=fetch):report=hf._repo_report('owner/model','c'*40,512)
                self.assertTrue(report['download_available']);self.assertEqual(report['variants'][0]['files'][0]['sha256'],'a'*64)
                self.assertIn('/revision/'+'c'*40+'?blobs=true',urls[0])
            finally:hf.close()

    def test_expected_disconnect_is_quiet_but_real_fault_is_reported(self):
        server=controller.LocalHTTPServer.__new__(controller.LocalHTTPServer);server._shutdown_started=False
        with mock.patch.object(ThreadingHTTPServer,'handle_error') as report:
            try:raise ConnectionResetError('deliberate peer close')
            except ConnectionResetError:server.handle_error(None,('127.0.0.1',1))
            report.assert_not_called()
            try:raise RuntimeError('real failure')
            except RuntimeError:server.handle_error(None,('127.0.0.1',1))
            report.assert_called_once()

    def test_actual_http_put_settings_and_cross_origin_rejection(self):
        import json,socket,threading,urllib.request,urllib.error
        from modelmatrix_agent import AgentConfiguration
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);profiles=root/'profiles';profiles.mkdir()
            config=AgentConfiguration(root=root,profiles_dir=profiles,host='127.0.0.1',port=0)
            service=controller.LocalService(config);service.bootstrap()
            server=controller.LocalHTTPServer(('127.0.0.1',0),service,'test-global-token-32-characters!',ROOT/'Dashboard'/'dist')
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/api/hf/settings'
                headers={'Content-Type':'application/json','Authorization':'Bearer test-global-token-32-characters!'}
                request=urllib.request.Request(url,data=b'{"speed_limit_mbps":16}',method='PUT',headers=headers)
                with urllib.request.urlopen(request,timeout=8) as response:self.assertEqual(json.load(response)['settings']['speed_limit_mbps'],16)
                request=urllib.request.Request(url,data=b'{"speed_limit_mbps":32}',method='PUT',headers={**headers,'Origin':'https://untrusted.example'})
                with self.assertRaises(urllib.error.HTTPError) as result:urllib.request.urlopen(request,timeout=8)
                self.assertEqual(result.exception.code,403)
            finally:server.shutdown();server.server_close();service.stop_all_managed()

if __name__=='__main__':unittest.main()
