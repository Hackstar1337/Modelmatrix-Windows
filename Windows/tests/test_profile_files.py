from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'RemoteAgent'))
from agent_core import parse_env_profile, parse_json_profile
from modelmatrix_agent import ProfileRepository


class WindowsProfileFileTests(unittest.TestCase):
    def test_every_shipped_example_loads_with_the_original_repository(self):
        directory = ROOT / 'Windows' / 'examples'
        expected = {p.stem for p in directory.glob('*.json')}
        loaded = ProfileRepository(directory).load()
        self.assertEqual(set(loaded), expected)
        self.assertEqual(loaded['llama-cpp-windows'].runtime, 'llama.cpp')
        self.assertEqual(loaded['custom-external-windows'].runtime, 'command')
        for profile in loaded.values():
            self.assertTrue(profile.request_model)
        argv = json.loads(loaded['llama-cpp-windows'].values['START_COMMAND_JSON'])
        self.assertEqual(argv[0], r'C:\Apps\llama.cpp\llama-server.exe')
        self.assertIn('{port}', argv)

    def test_utf8_bom_and_crlf_from_windows_editors_are_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = root / 'fixture.env'
            doc = root / 'fixture.json'
            env.write_bytes(b'\xef\xbb\xbfREQUEST_MODEL=fixture\r\nRUNTIME=external\r\n')
            doc.write_bytes(b'\xef\xbb\xbf' + json.dumps({'REQUEST_MODEL':'fixture', 'RUNTIME':'external'}).encode())
            self.assertEqual(parse_env_profile(env)['REQUEST_MODEL'], 'fixture')
            self.assertEqual(parse_json_profile(doc)['REQUEST_MODEL'], 'fixture')


if __name__ == '__main__':
    unittest.main()
