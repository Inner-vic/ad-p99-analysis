"""Create an absent AD P99 workspace and venv without activating business work."""
import argparse,json,os,shutil,stat,subprocess,sys
from pathlib import Path
from check_repository import ROOT,verify

def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def prepare(workspace,base_python):
    if os.name!='nt':raise ValueError('Windows x64 required')
    verify()
    if not workspace.is_absolute() or '..' in workspace.parts or workspace.exists():raise ValueError('Absent absolute target required')
    workspace=workspace.absolute()
    if workspace.is_relative_to(ROOT) or ROOT.is_relative_to(workspace):raise ValueError('Repository and deployment must be separate')
    for path in (workspace,base_python):
        for item in (path,*path.parents):
            if item.exists():
                info=item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:raise ValueError('Linked path refused')
    subprocess.run([str(base_python),'-I','-c','import sys,struct;assert sys.implementation.name=="cpython";assert sys.version_info[:2]==(3,13);assert struct.calcsize("P")==8'],check=True)
    shutil.copytree(ROOT/'workspace',workspace)
    venv=workspace/'runtime/ad-p99-python'
    subprocess.run([str(base_python),'-I','-m','venv',str(venv)],check=True)
    private=workspace/'private';private.mkdir()
    s=json.loads((ROOT/'examples/asgard.settings.json.example').read_text('utf-8'))
    s.update(lark_cli=str(workspace/'tools/lark-cli/lark-cli.exe'),lark_home=str(private/'lark-home'),state_dir=str(workspace/'data/ad-p99/jobs'),runtime_state_dir=str(workspace/'data/ad-p99/runtime'),auth_credential_replica_path=str(private/'lark-home/.lark-cli/credential-replica.json'))
    save(private/'asgard.settings.json.example',s)
    shutil.copyfile(ROOT/'examples/cloud.settings.json.example',private/'cloud.settings.json.example')
    m=json.loads((ROOT/'examples/isolation.example.json').read_text('utf-8'))
    for key in ('project_id','lark_home','lark_cli','auth_credential_replica_path','expected_lark_app_id','expected_dispatch_open_id'):m[key]=s[key]
    m.update(workspace_root=str(workspace),settings_path=str(private/'asgard.settings.json'),python_executable=str(venv/'Scripts/python.exe'))
    m['profiles']={'dispatch_user':s['lark_profile'],'report_bot':s['report_lark_profile']}
    m['auth_maintenance']['check_interval_minutes']=60
    save(workspace/'deployment/cc-connect-lark-isolation.json.example',m)
    return {'status':'candidate_prepared','venv_created':True,'dependencies_installed':False,'services_registered':False,'authorization_started':False,'business_jobs_created':0}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',type=Path,required=True);p.add_argument('--base-python',type=Path,required=True);a=p.parse_args()
    try:print(json.dumps(prepare(a.workspace,a.base_python)))
    except Exception as error:print(json.dumps({'status':'not_activated','error_type':type(error).__name__}));sys.exit(1)
