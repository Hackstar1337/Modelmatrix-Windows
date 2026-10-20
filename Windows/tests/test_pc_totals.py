import sys,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'Windows'),str(ROOT/'RemoteAgent')]
import controller
from modelmatrix_agent import AgentConfiguration

class DashboardTotalsTests(unittest.TestCase):
    def test_all_time_tokens_alias_matches_recorded_database_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);profiles=root/'profiles';profiles.mkdir()
            service=controller.LocalService(AgentConfiguration(root=root,profiles_dir=profiles,host='127.0.0.1',port=0))
            service.bootstrap()
            try:
                service.usage.store.record({'started_at':time.time(),'model':'m','profile':'p','client_id':'c','status':200,'prompt_tokens':40,'completion_tokens':8,'duration_ms':100})
                _,data=service.module_route('GET','/api/usage/overview',{'range':'all'},{})
                self.assertEqual(data['totals']['tokens'],48)
                self.assertEqual(data['totals']['tokens'],data['totals']['total_tokens'])
                self.assertEqual(data['totals']['tokens_24h'],48)
            finally:service.stop_all_managed()

if __name__=='__main__':unittest.main()
