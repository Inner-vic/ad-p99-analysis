"""Run offline groups separately; no CLI authorization or live Bot messages."""
import json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SKILL=ROOT/'workspace/.agents/skills/ad-p99-analysis'
groups=[('ad',SKILL/'tests','test_*.py'),('auth',ROOT/'workspace/deployment/tests','test_*.py'),('input',ROOT/'workspace/.agents/skills/chassis-business-routing/scripts','test_natural_input.py')]
env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONPATH=str(ROOT/'workspace/deployment'))
for name,directory,pattern in groups:
 result=subprocess.run([sys.executable,'-B','-X','utf8','-m','unittest','discover','-s',str(directory),'-p',pattern],cwd=ROOT,env=env)
 if result.returncode:print(json.dumps({'status':'failed','group':name}));raise SystemExit(result.returncode)
print(json.dumps({'status':'passed','groups':len(groups)}))
