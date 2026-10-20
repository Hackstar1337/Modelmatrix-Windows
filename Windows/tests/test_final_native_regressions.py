from __future__ import annotations
import json, os, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'Windows'),str(ROOT/'RemoteAgent')]
from bridge import LocalBridge


class FinalPortableRegressions(unittest.TestCase):
    def test_jsonc_preserves_comma_and_comment_markers_inside_strings(self):
        text='{"value":"inside,} and ,] // preserve","array":[1,2,], /* comment */}'
        stripped=LocalBridge._strip_jsonc_comments(text)
        parsed=json.loads(LocalBridge._strip_jsonc_trailing_commas(stripped))
        self.assertEqual(parsed['value'],'inside,} and ,] // preserve')
        self.assertEqual(parsed['array'],[1,2])

    def test_scoped_ipv6_base_url_is_bracketed(self):
        bridge=LocalBridge.__new__(LocalBridge)
        bridge.service=SimpleNamespace(configuration=SimpleNamespace(host='::1',port=17700))
        self.assertEqual(bridge._relay_url('model','identity','connection'),'http://[::1]:17700/api/connect/connection/v1')

    def test_accepted_http_pollers_drain_before_usage_store_close(self):
        from controller import LocalHTTPServer, make_service
        errors=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); profiles=root/'profiles';profiles.mkdir()
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));relay=sock.getsockname()[1]
            service=make_service(root,profiles,'127.0.0.1',0,None,relay_port=relay)
            server=LocalHTTPServer(('127.0.0.1',0),service,None,ROOT/'Dashboard'/'dist')
            original=server.handle_error
            def on_error(request,address):
                if not isinstance(sys.exc_info()[1],OSError):errors.append(repr(sys.exc_info()[1]))
                original(request,address)
            server.handle_error=on_error
            serving=threading.Thread(target=server.serve_forever);serving.start()
            stop=threading.Event();base=f'http://127.0.0.1:{server.server_port}'
            def poll():
                while not stop.is_set():
                    try:
                        with urllib.request.urlopen(base+'/api/usage/summary',timeout=1) as response:json.loads(response.read())
                    except OSError:pass
            clients=[threading.Thread(target=poll) for _ in range(8)]
            try:
                for client in clients:client.start()
                time.sleep(.2)
                server.graceful_shutdown()
            finally:
                stop.set()
                server.shutdown();server.server_close();service.stop_all_managed()
                for client in clients:client.join(3)
                serving.join(3)
            self.assertFalse(errors)
            self.assertFalse(serving.is_alive())
            self.assertFalse(any(client.is_alive() for client in clients))


@unittest.skipUnless(os.name=='nt','native tray and Windows handle inheritance require Windows')
class FinalWindowsRegressions(unittest.TestCase):
    def test_tray_starts_cmd_launcher_with_native_switch_without_list2cmdline(self):
        import tray
        with tempfile.TemporaryDirectory() as directory:
            launcher=Path(directory)/'controller with spaces.cmd'; launcher.write_text('@echo off\n',encoding='utf-8')
            tray._launching=False
            with mock.patch.object(tray,'_status',side_effect=[OSError('not started'),{'statuses':[]}]), mock.patch.object(tray,'_controller_launcher',return_value=launcher), mock.patch.object(tray.subprocess,'Popen') as spawn, mock.patch.object(tray.time,'sleep'):
                self.assertTrue(tray._ensure_controller())
            command=spawn.call_args.args[0]
            self.assertIsInstance(command,str)
            self.assertIn('"'+str(launcher)+'" -NoBrowser',command)
            self.assertNotIn('--no-browser',command)

    def test_background_launcher_does_not_keep_parent_capture_pipes_open(self):
        from platform_support import process_is_alive
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory); stop=folder/'stop'
            script='import pathlib,sys,time; end=time.monotonic()+12; p=pathlib.Path(sys.argv[1]);\nwhile not p.exists() and time.monotonic()<end: time.sleep(.05)\n'
            config={'command':[sys.executable,'-c',script,str(stop)],'cwd':str(folder),'stdout':str(folder/'out.log'),'stderr':str(folder/'err.log')}
            config_file=folder/'launch.json';config_file.write_text(json.dumps(config),encoding='utf-8')
            started=time.monotonic();pid=None
            try:
                result=subprocess.run([sys.executable,str(ROOT/'Windows'/'background_launch.py'),'--config',str(config_file)],capture_output=True,text=True,timeout=4)
                self.assertEqual(result.returncode,0,result.stderr)
                pid=json.loads(result.stdout)['pid']
                self.assertTrue(process_is_alive(pid))
                self.assertLess(time.monotonic()-started,3)
            finally:
                stop.write_text('stop',encoding='utf-8')
                if pid:
                    end=time.monotonic()+5
                    while process_is_alive(pid) and time.monotonic()<end:time.sleep(.05)
                    self.assertFalse(process_is_alive(pid))


if __name__=='__main__':unittest.main()
