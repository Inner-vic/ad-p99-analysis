"""Error envelopes cannot silently trigger Prod -> Test or truncate a page."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import cloud_log_client as cloud
import performance_log_source as source


class CloudResponseContractTests(unittest.TestCase):
    def setUp(self):
        self.params=cloud.build_query_params(vin='DEM0VEH1CLEX00001',start='2026-10-30 15:00:00',end='2026-10-30 15:35:00',log_class='log_fsdA_service',username='offline-user')

    def test_observed_prod_success_null_is_empty_only_on_first_page(self):
        payload={'success':True,'code':200,'message':'操作成功！','data':None}
        validated=cloud.validate_list_response(payload,self.params,environment='prod',http_status=200)
        self.assertEqual(validated['data'],{'list':[],'total':0})
        for params,env,status in [(dict(self.params,pageNo=2),'prod',200),(self.params,'test',200),(self.params,'prod',None)]:
            with self.subTest(params=params,env=env,status=status),self.assertRaises(cloud.CloudResponseError):
                cloud.validate_list_response(payload,params,environment=env,http_status=status)

    def test_failed_or_malformed_envelopes_never_become_empty_results(self):
        invalid=[{}, {'code':500,'data':None},{'code':False,'data':{'list':[]}},
                 {'success':'false','data':{'list':[]}}, {'success':False,'data':{'list':[]}},
                 {'success':True,'data':None}, {'data':{}}, {'data':{'list':None}},
                 {'data':{'list':[],'records':[{'fileName':'AVM_Service.zst'}]}},
                 {'data':{'list':[],'total':True}}, {'data':{'list':[],'total':0.5}}]
        for payload in invalid:
            with self.subTest(payload=payload),patch.object(cloud,'request_json',return_value=payload),self.assertRaises(cloud.CloudResponseError):
                cloud.query_files(vin='DEM0VEH1CLEX00001',start='a',end='b',log_class='log_fsdA_service',username='offline-user')

    def test_query_error_does_not_fallback_to_test(self):
        with patch.object(cloud,'request_json',return_value={'code':500,'data':None}) as query,self.assertRaises(cloud.CloudResponseError):
            source.resolve_pair_queries(vin='DEM0VEH1CLEX00001',baseline_start='a',baseline_end='b',replacement_start='c',replacement_end='d',username='offline-user')
        self.assertEqual(query.call_count,1)


if __name__=='__main__':unittest.main()
