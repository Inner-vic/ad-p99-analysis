"""Offline regressions for correlation, durable dispatch and recovery ownership."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import asgard_worker as worker
import asgard_bridge as bridge
from ad_p99 import InputError

VIN='DEM0VEH1CLEX00001'
META={'vin':VIN,'start':'2026-10-30T15:00:00','end':'2026-10-30T15:35:00'}
TEXT=f'时延分析 {VIN} Baseline：2026-10-30 15:00:00 - 2026-10-30 15:35:00；替换后：2026-10-30 17:00:00 - 2026-10-30 17:35:00'
URL='https://li.feishu.cn/sheets/BoundResult'
DOC='https://li.feishu.cn/docx/BoundReport'


class CardOwnershipTests(unittest.TestCase):
    def message(self,text,reply=''):
        return {'message_id':'om_card','sender':{'open_bot_id':'ou_asgard'},'content':text,'reply_to':reply}

    def test_unrelated_failure_cannot_fail_current_window(self):
        good=self.message(f'{VIN} 2026-10-30 15:00:00 2026-10-30 15:35:00 {URL}')
        for failure in ['log 分析失败',f'{VIN} 2026-10-30 17:00:00 2026-10-30 17:35:00 分析失败']:
            result=worker.find_result([self.message(failure),good],META,'om_request','ou_asgard')
            self.assertEqual(result['sheet_url'],URL)

    def test_explicit_other_reply_cannot_fallback_by_window(self):
        text=f'{VIN} 2026-10-30 15:00:00 2026-10-30 15:35:00 {URL}'
        self.assertIsNone(worker.find_result([self.message(text,'om_other')],META,'om_request','ou_asgard'))

    def test_bound_failure_still_fails(self):
        with self.assertRaisesRegex(InputError,'本时段'):
            worker.find_result([self.message('log 分析失败','om_request')],META,'om_request','ou_asgard')

    def test_conflicting_result_urls_do_not_pick_first(self):
        messages=[self.message(url,'om_request') for url in [URL,URL+'Other']]
        with self.assertRaisesRegex(InputError,'多份'):
            worker.find_result(messages,META,'om_request','ou_asgard')

    def test_reply_cannot_override_explicit_wrong_vin_or_window(self):
        for content in [f'DEM0VEH1CLEX00002 {URL}',f'2026-10-30 17:00:00 {URL}']:
            with self.assertRaisesRegex(InputError,'冲突'):
                worker.find_result([self.message(content,'om_request')],META,'om_request','ou_asgard')

    def test_partial_dispatch_does_not_query_or_accept_unbound_cards(self):
        with patch.object(worker,'list_messages') as messages, self.assertRaisesRegex(InputError,'派发不完整'):
            worker.wait_for_cards({}, {'dispatch':{'sent':[{'role':'baseline','message_id':'om_base','sent_at':'2026-11-03T01:00:00Z'}]}},'oc_chat','now')
        messages.assert_not_called()

    def test_matching_old_card_cannot_cross_dispatch_boundary(self):
        card=self.message(f'{VIN} 2026-10-30 15:00:00 2026-10-30 15:35:00 {URL}')
        card['create_time']='2026-11-03T00:59:59Z'
        self.assertIsNone(worker.find_result([card],META,'om_request','ou_asgard',sent_at='2026-11-03T01:00:00Z'))

    def test_display_time_is_hydrated_without_guessing_zone_or_seconds(self):
        card=self.message(f'{VIN} 2026-10-30 15:00:00 2026-10-30 15:35:00 {URL}')
        card['create_time']='2026-11-03 14:36'
        boundary='2026-11-03T06:36:14.287707+00:00'
        for stamp, accepted in [('1793687774000', False), ('1793687775000', True)]:
            response={'ok':True,'data':{'items':[{'message_id':'om_card','chat_id':'oc_chat',
                                               'create_time':stamp,'deleted':False}]}}
            with self.subTest(stamp=stamp),patch.object(worker,'run_lark',return_value=response) as run:
                reader=lambda m:worker._message_creation_instant({},m,'oc_chat')
                result=worker.find_result([card],META,'om_request','ou_asgard',sent_at=boundary,timestamp_reader=reader)
                self.assertEqual(result is not None,accepted)
                self.assertEqual(run.call_args.args[1][:3],['api','GET','/open-apis/im/v1/messages/om_card'])

    def test_raw_time_lookup_rejects_wrong_source_missing_or_coarse_time(self):
        card=self.message(URL);card['create_time']='2026-11-03 14:36'
        valid={'message_id':'om_card','chat_id':'oc_chat','create_time':'1793687775000','deleted':False}
        bad=[[],[dict(valid,message_id='om_other')],[dict(valid,chat_id='oc_other')],
             [dict(valid,deleted=True)],[dict(valid,create_time='2026-11-03 14:36')],
             [dict(valid,create_time=None)],[valid,valid]]
        for items in bad:
            with self.subTest(items=items),patch.object(worker,'run_lark',return_value={'ok':True,'data':{'items':items}}):
                with self.assertRaises(InputError):worker._message_creation_instant({},card,'oc_chat')

    def test_precise_existing_timestamp_needs_no_raw_request(self):
        with patch.object(worker,'run_lark') as run:
            instant=worker._message_creation_instant({}, {'create_time':'1793687775000'},'oc_chat')
        self.assertEqual(instant.isoformat(),'2026-11-03T06:36:15+00:00')
        run.assert_not_called()

    def test_progress_acknowledgements_do_not_trigger_time_lookup(self):
        card=self.message(f'{VIN} 2026-10-30 15:00:00 2026-10-30 15:35:00 正在分析')
        card['create_time']='2026-11-03 14:36'
        reader=unittest.mock.Mock()
        self.assertIsNone(worker.find_result([card],META,'om_request','ou_asgard',
            sent_at='2026-11-03T06:36:14Z',timestamp_reader=reader))
        reader.assert_not_called()

    def test_each_accepted_card_has_an_immediate_receipt_callback(self):
        other={**META,'start':'2026-10-30T17:00:00','end':'2026-10-30T17:35:00'}
        job={'comparison_key':'synthetic','vin':VIN,'baseline_metadata':META,'replacement_metadata':other,
             'dispatch':{'sent':[{'role':'baseline','message_id':'om_base','sent_at':'2026-11-03T01:00:00Z'},
                                 {'role':'replacement','message_id':'om_repl','sent_at':'2026-11-03T01:00:00Z'}]}}
        cards=[]
        for reply in ('om_base','om_repl'):
            card=self.message(URL,reply);card.update(create_time='2026-11-03T01:00:01Z',message_id=reply+'_card')
            cards.append(card)
        with tempfile.TemporaryDirectory() as folder:
            settings={'runtime_state_dir':folder,'asgard_open_id':'ou_asgard'}
            callback=unittest.mock.Mock()
            with patch.object(worker,'list_messages',return_value=cards) as read:
                result=worker.wait_for_cards(settings,job,'oc_chat','2026-11-03T01:00:00Z',on_received=callback)
            self.assertEqual(set(result),{'baseline','replacement'})
            self.assertEqual(callback.call_count,2)
            job['received']=result
            with patch.object(worker,'list_messages') as read:
                self.assertEqual(worker.wait_for_cards(settings,job,'oc_chat','now'),result)
            read.assert_not_called()

    def test_missing_or_truncated_result_list_is_an_error(self):
        for response in [{'ok':True},{'data':{'messages':[],'has_more':True}},{'data':{'messages':None}},
                         {'data':{'messages':[],'truncated':True}},
                         {'data':{'messages':[],'has_more':'false'}}]:
            with self.subTest(response=response),patch.object(worker,'run_lark',return_value=response),self.assertRaises(InputError):
                worker.list_messages({},'oc_chat','2026-11-03T00:00:00Z')

    def test_result_scan_requests_all_pages_with_a_finite_budget(self):
        with patch.object(worker,'run_lark',return_value={'ok':True,'data':{'messages':[]}}) as run:
            self.assertEqual(worker.list_messages({},'oc_chat','2026-11-03T00:00:00Z'),[])
        args=run.call_args.args[1]
        self.assertIn('--page-all',args)
        self.assertEqual(args[args.index('--page-limit')+1],'20')

    def test_ambiguous_unbound_card_is_ignored_but_exact_reply_remains_valid(self):
        text=f'{VIN} 2026-10-30 15:00:00 2026-10-30 15:35:00 {URL}'
        self.assertIsNone(worker.find_result([self.message(text)],META,'om_request','ou_asgard',allow_unbound=False))
        self.assertEqual(worker.find_result([self.message(text,'om_request')],META,'om_request','ou_asgard',allow_unbound=False)['sheet_url'],URL)


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.settings={'runtime_state_dir':str(self.root),'state_dir':str(self.root/'state'),
                       'asgard_platform':'CCUFSD','report_lark_profile':'offline-bot','asgard_open_id':'ou_asgard'}
        self.key=hashlib.sha256(b'om_source').hexdigest()[:24]
        self.path=self.root/(self.key+'.json')
        self.record={'job_key':self.key,'source_message_id':'om_source','source_chat_id':'oc_chat',
                     'sender_id':'ou_requester','vin':VIN,'status':'queued','comparison_key':'offline-key'}

    def save(self):worker.atomic_json(self.path,self.record)

    def status_args(self,*,sender='ou_requester'):
        return SimpleNamespace(record=self.path,settings=self.root/'settings',
            ccconnect_context=f'[cc-connect request_context_v=1 sender_id={sender} platform=feishu chat_id=oc_chat message_id=om_statusquestion thread_id=]')

    def test_status_is_readonly_bound_to_owner_and_does_not_authorize(self):
        self.record['status']='waiting_cards';self.save();before=self.path.read_bytes()
        with patch.object(worker,'load_settings',return_value=self.settings),patch.object(worker,'run_lark') as lark:
            status=worker.status_job(self.status_args())
            with self.assertRaisesRegex(InputError,'不属于'):
                worker.status_job(self.status_args(sender='ou_other'))
        lark.assert_not_called();self.assertIn('等待两段',status['message']);self.assertEqual(self.path.read_bytes(),before)

    def test_legacy_completion_without_receipt_does_not_claim_delivery(self):
        self.record.update(status='completed',report_url=DOC);self.save()
        with patch.object(worker,'load_settings',return_value=self.settings):status=worker.status_job(self.status_args())
        self.assertFalse(status['delivered']);self.assertNotIn('report_url',status);self.assertIn('尚未确认',status['message'])

    def test_verified_completion_exposes_only_its_actual_report(self):
        self.record.update(status='completed',report_url=DOC,delivery_receipt={
            'identity':'bot','profile':'offline-bot','source_message_id':'om_source','source_chat_id':'oc_chat',
            'report_url':DOC,'message_id':'om_delivered'});self.save()
        with patch.object(worker,'load_settings',return_value=self.settings):status=worker.status_job(self.status_args())
        self.assertTrue(status['delivered']);self.assertEqual(status['report_url'],DOC)

    def test_status_failure_does_not_expose_private_diagnostics_or_ungoverned_url(self):
        self.record.update(status='failed',report_url=DOC,error='https://private.invalid/signed?token=secret C:\\private\\details');self.save()
        with patch.object(worker,'load_settings',return_value=self.settings):status=worker.status_job(self.status_args())
        self.assertNotIn('report_url',status);self.assertNotIn('secret',json.dumps(status));self.assertIn('结果已保留',status['message'])

    def test_completed_worker_and_resume_have_no_external_actions(self):
        self.record['status']='completed';self.save();before=self.path.read_bytes()
        with patch.object(worker,'load_settings',return_value=self.settings),patch.object(worker,'ensure_report_bot_authorization') as bot,patch.object(worker,'ensure_user_authorization') as user,patch.object(worker,'finish_dispatched_job') as finish:
            self.assertEqual(worker.run_worker(self.path,self.root/'settings'),0)
            self.assertEqual(worker.resume_worker(self.path,self.root/'settings'),0)
        bot.assert_not_called();user.assert_not_called();finish.assert_not_called()
        self.assertEqual(self.path.read_bytes(),before)

    def test_competing_worker_cannot_mark_owner_failed(self):
        self.save();before=self.path.read_bytes()
        with worker._worker_lock(self.path),patch.object(worker,'load_settings',return_value=self.settings):
            with self.assertRaisesRegex(InputError,'已有 worker'):
                worker.resume_worker(self.path,self.root/'settings')
        self.assertEqual(self.path.read_bytes(),before)

    def test_killed_lock_owner_does_not_leave_a_stale_global_block(self):
        ready=self.root/'ready'
        code=('import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
              'import asgard_worker as w;'
              '\nwith w._worker_lock(Path(sys.argv[2])):\n Path(sys.argv[3]).write_text("ready");time.sleep(30)')
        child=subprocess.Popen([sys.executable,'-B','-X','utf8','-c',code,str(Path(worker.__file__).parent),str(self.path),str(ready)],
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            deadline=time.monotonic()+8
            while not ready.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.05)
            self.assertTrue(ready.exists(),'offline lock child failed to start')
            with self.assertRaises(InputError):
                with worker._worker_lock(self.path):pass
            child.kill();child.wait(timeout=5)
            with worker._worker_lock(self.path):pass
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)

    def test_existing_report_does_not_collect_or_create_again(self):
        self.record.update(report_url=DOC,report_permissions={'collaborator_management':{},'source_chat_view':{},'owner_transfer':{}})
        self.record['report_permissions']={k:True for k in self.record['report_permissions']}
        self.save()
        receipt={'message_id':'om_delivered','identity':'bot','profile':'offline-bot',
                 'source_message_id':'om_source','source_chat_id':'oc_chat','report_url':DOC}
        with patch.object(worker,'ensure_report_bot_authorization',return_value={'profile':'offline-bot'}),patch.object(worker,'collect_and_analyze_job') as collect,patch.object(worker,'create_report') as create,patch.object(worker,'deliver',return_value=receipt) as deliver:
            self.assertEqual(worker.finish_dispatched_job(self.path,self.settings,self.record),0)
        collect.assert_not_called();create.assert_not_called();deliver.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text())['delivery_receipt'],receipt)

    def test_pending_delivery_cannot_be_blindly_resent(self):
        self.record.update(report_url=DOC,delivery_intent={'pending':True},
            report_permissions={'collaborator_management':True,'source_chat_view':True,'owner_transfer':True})
        self.save()
        with patch.object(worker,'ensure_report_bot_authorization',return_value={'profile':'offline-bot'}),patch.object(worker,'deliver') as deliver,self.assertRaisesRegex(InputError,'发送意图'):
            worker.finish_dispatched_job(self.path,self.settings,self.record)
        deliver.assert_not_called()

    def test_delivery_without_real_message_id_is_not_success(self):
        with patch.object(worker,'run_lark',return_value={'ok':True}),self.assertRaisesRegex(InputError,'回执'):
            worker.deliver(self.settings,self.record,DOC)

    def test_delivery_receipt_from_another_chat_cannot_confirm_success(self):
        payload={'ok':True,'identity':'bot','data':{'message_id':'om_delivered','chat_id':'oc_other'}}
        with patch.object(worker,'run_lark',return_value=payload),self.assertRaisesRegex(InputError,'回执'):
            worker.deliver(self.settings,self.record,DOC)

    def test_unknown_creation_outcome_does_not_reanalyze_or_create_again(self):
        self.record['report_create_intent']={'pending':True};self.save()
        with patch.object(worker,'ensure_report_bot_authorization',return_value={'profile':'offline-bot'}),patch.object(worker,'collect_and_analyze_job') as collect,patch.object(worker,'create_report') as create,self.assertRaisesRegex(InputError,'创建意图'):
            worker.finish_dispatched_job(self.path,self.settings,self.record)
        collect.assert_not_called();create.assert_not_called()

    def test_worker_cannot_redispatch_partial_record(self):
        self.save()
        with patch.object(worker,'load_settings',return_value=self.settings),patch.object(worker,'dispatch') as dispatch,self.assertRaisesRegex(InputError,'禁止重派'):
            worker.run_worker(self.path,self.root/'settings')
        dispatch.assert_not_called()

    def test_start_rejects_changed_same_message_before_authorization(self):
        self.record.update(commands=['wrong']);self.save()
        args=SimpleNamespace(ccconnect_context='[cc-connect request_context_v=1 sender_id=ou_requester platform=feishu chat_id=oc_chat message_id=om_source thread_id=]',chat_id='oc_chat',sender_id='ou_requester',message_id='om_source',text=TEXT,settings=self.root/'settings')
        with patch.object(worker,'load_settings',return_value=self.settings),patch.object(worker,'ensure_user_authorization') as auth,self.assertRaisesRegex(InputError,'冲突'):
            worker.start_job(args)
        auth.assert_not_called()

    def test_manual_identity_without_transport_header_cannot_create_job(self):
        args=SimpleNamespace(ccconnect_context=None,chat_id='oc_chat',sender_id='ou_requester',message_id='om_source',text=TEXT,settings=self.root/'settings')
        with patch.object(worker,'load_settings') as settings,self.assertRaisesRegex(InputError,'请求上下文'):
            worker.start_job(args)
        settings.assert_not_called()

    def test_other_runtime_same_window_blocks_unbound_compatibility(self):
        other=dict(self.record,comparison_key='another-key',commands=[
            f'fsdlog {VIN}, 2026-10-30 15:00:00, 2026-10-30 15:35:00, CCUFSD',
            f'fsdlog {VIN}, 2026-10-30 17:00:00, 2026-10-30 17:35:00, CCUFSD'])
        worker.atomic_json(self.path,other)
        self.assertFalse(worker._unique_runtime_window(self.settings,{'comparison_key':'current-key'},META,'oc_chat'))
        self.assertTrue(worker._unique_runtime_window(self.settings,{'comparison_key':'current-key'},META,'oc_another'))

    def test_dispatch_binding_exists_before_first_send_and_survives_partial_failure(self):
        commands=[f'fsdlog {VIN}, 2026-10-30 15:00:00, 2026-10-30 15:35:00, CCUFSD',f'fsdlog {VIN}, 2026-10-30 17:00:00, 2026-10-30 17:35:00, CCUFSD']
        prepared=[]
        def send(command,**kwargs):
            self.assertEqual(len(prepared),1)
            if kwargs['role']=='baseline':raise OSError('offline transport failure')
            return {'message_id':'om_version'}
        with self.assertRaises(OSError):
            bridge.dispatch(commands,state_dir=self.settings['state_dir'],send_fn=send,chat_id='oc_chat',sender_id='ou_requester',on_prepared=prepared.append)
        _,state=worker.load_job(self.settings['state_dir'],prepared[0])
        self.assertEqual(state['dispatch']['sent'][0]['message_id'],'om_version')


if __name__=='__main__':unittest.main()
