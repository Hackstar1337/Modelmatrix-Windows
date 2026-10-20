from __future__ import annotations
import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'Windows'),str(ROOT/'RemoteAgent')]
import portable_pc as pc

class PortablePCTests(unittest.TestCase):
    def test_settings_rejects_port_types_and_gpu_conflicts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'Data').mkdir()
            for changes in [{'controller_port':True},{'relay_port':18700},{'model_port':18700},{'model_port':65535},{'controller_port':18711},{'backend':'arbitrary'}]:
                (root/'Data'/'portable-settings.json').write_text(json.dumps(changes))
                with self.assertRaises(ValueError):pc.settings(root)

    def test_assets_reject_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'bundle';root.mkdir();(Path(d)/'outside').write_text('x')
            with self.assertRaises(ValueError):pc.asset(root,'../outside')

    def test_builtin_profile_paths_refresh_after_relocation_and_preserve_capture(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'bundle with spaces';(root/'Runtime').mkdir(parents=True);(root/'Models').mkdir()
            (root/'Runtime'/'llama-server.exe').write_bytes(b'fixture');(root/'Models'/'model.gguf').write_bytes(b'fixture')
            manifest={'model':{'path':'Models/model.gguf'},'engines':{'cpu':{'path':'Runtime/llama-server.exe'}}}
            pc.configure_profiles(root,manifest,pc.settings(root))
            profile=root/'Data'/'profiles'/'smollm2-cpu.json';value=json.loads(profile.read_text());value['CAPTURE_MODE']='0';profile.write_text(json.dumps(value))
            relocated=Path(d)/'moved Unicode Ω';root.rename(relocated)
            pc.configure_profiles(relocated,manifest,pc.settings(relocated))
            new=json.loads((relocated/'Data'/'profiles'/'smollm2-cpu.json').read_text())
            self.assertEqual(new['CAPTURE_MODE'],'0')
            self.assertEqual(new['MODEL_FILE'],str((relocated/'Models'/'model.gguf').resolve()))
            self.assertEqual(json.loads(new['START_COMMAND_JSON'])[0],str((relocated/'Runtime'/'llama-server.exe').resolve()))

    def test_custom_same_name_profile_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'Runtime').mkdir();(root/'Models').mkdir();(root/'Data'/'profiles').mkdir(parents=True)
            (root/'Runtime'/'server').write_bytes(b'x');(root/'Models'/'model').write_bytes(b'x')
            profile=root/'Data'/'profiles'/'smollm2-cpu.json';profile.write_text('{"REQUEST_MODEL":"user"}')
            with self.assertRaises(ValueError):pc.configure_profiles(root,{'model':{'path':'Models/model'},'engines':{'cpu':{'path':'Runtime/server'}}},pc.settings(root))
            self.assertEqual(json.loads(profile.read_text())['REQUEST_MODEL'],'user')

    def test_unicode_diagnostics_are_lossless_on_ascii_redirected_stdout(self):
        from unittest import mock
        class ASCIIPipe:
            def __init__(self):self.parts=[]
            def write(self,text):text.encode('ascii');self.parts.append(text)
            def flush(self):pass
        output=ASCIIPipe()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(pc,'read_json',return_value={}),mock.patch.object(pc,'doctor',return_value={'path':'folder Ω'}),mock.patch.object(sys,'stdout',output):
                code=pc.main(['doctor','--root',d])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(''.join(output.parts))['path'],'folder Ω')

if __name__=='__main__':unittest.main()
