"""Run the frozen full-target Jev backtest; never submit public forecasts."""
import argparse,concurrent.futures,hashlib,json,os,re,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manifest',type=Path,required=True)
parser.add_argument('--execute',action='store_true')
args=parser.parse_args()
OUT=args.manifest.resolve().parent
manifest=json.loads(args.manifest.read_text())
if not args.execute:
 print(json.dumps({k:manifest[k] for k in ['scope','counts','window','calls_before_retry','approx_cost_usd','interpretation']},indent=2))
 raise SystemExit(0)
manifest_digest=hashlib.sha256(args.manifest.read_bytes()).hexdigest()
binding=OUT/'manifest-binding.json'
if binding.exists() and json.loads(binding.read_text())['sha256']!=manifest_digest:
 raise SystemExit('Manifest changed since this run began; choose a new output directory.')
binding.write_text(json.dumps({'sha256':manifest_digest})+'\n')
os.environ['SSA_PROVIDER_LIMIT']='32'
os.environ['SSA_REPLIES_DIR']=str(OUT/'replies')
os.environ['SSA_JEV_AUDIT_DIR']=str(OUT/'provider-responses')
from ssa import envfile
envfile.load(str(ROOT/'.env'))
from ssa import harness,jev,scoring
assert harness.model_id('jev')==manifest['model'] and not harness.ALLOW_MOCK
ENTRANTS=('jev','jev-zeroshot-persona')
_original_post=jev.requests.post
lock=threading.Lock();next_call=0.0
stats={'http_attempts':0,'input_tokens':0,'output_tokens':0,'http_errors':0,'started_at':time.time()}
def atomic(path,value):
 path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n');os.replace(tmp,path)
def post(url,**kwargs):
 global next_call
 if url!='https://api.typesafe.ai/v1/systemone':raise ValueError('unexpected provider URL')
 with lock:
  if stats['http_attempts']>=80000 or stats['input_tokens']>=119000000:raise RuntimeError('local backtest call/token cap reached')
  slot=max(next_call,time.monotonic());next_call=slot+.06
  stats['http_attempts']+=1
 time.sleep(max(0,slot-time.monotonic()))
 response=_original_post(url,**kwargs)
 try:
  usage=response.json().get('usage',{})
 except (ValueError,AttributeError):usage={}
 with lock:
  stats['input_tokens']+=usage.get('input_tokens',0)
  stats['output_tokens']+=usage.get('output_tokens',0)
  if response.status_code!=200:stats['http_errors']+=1
  if stats['http_attempts']%100==0:atomic(OUT/'provider-stats.json',stats)
 return response
jev.requests.post=post

def job(task,entrant):
 rid=task['round']['round_id'];dest=OUT/'forecasts'/rid/(entrant+'.json')
 identity=harness.call_identity(entrant)
 if dest.exists():
  old=json.loads(dest.read_text())
  if (old.get('complete') and old.get('call_identity')==identity and
      old.get('manifest_sha256')==manifest_digest):return old
 rec={'manifest_sha256':manifest_digest,'round_id':rid,'series':task['series'],'date':task['date'],'entrant':entrant,
      'official_matched':task['official_matched'],'outcome':task['outcome'],
      'persistence':task['round']['baselines']['persistence'],'call_identity':identity,
      'forecast':None,'complete':False,'errors':[]}
 for attempt in range(3):
  try:
   fc=harness.forecast(entrant,task['round'],history=task['history'])
   if entrant.endswith('persona') and '192/192 respondents' not in fc['notes']:
    raise RuntimeError('partial panel; resume only missing replies')
   rec['forecast']=fc;rec['complete']=True
   rec['crps']=scoring.crps_forecast(fc['topline'],rec['outcome'])
   break
  except Exception as exc:
   rec['errors'].append({'attempt':attempt+1,'type':type(exc).__name__,'error':str(exc)[:1500]})
   time.sleep(min(2**attempt,4))
 atomic(dest,rec)
 return rec

def summary(records,matched_only=False):
 result=[]
 for sid in manifest['counts']:
  rows=[r for r in records if r['series']==sid and (r['official_matched'] or not matched_only)]
  by={e:{r['round_id']:r for r in rows if r['entrant']==e and r['complete']} for e in ENTRANTS}
  shared=set(by[ENTRANTS[0]])&set(by[ENTRANTS[1]])
  if not shared:continue
  r={'series':sid,'rounds':len(shared)}
  for e,label in zip(ENTRANTS,('direct_crps','persona_crps')):
   r[label]=sum(by[e][k]['crps'] for k in shared)/len(shared)
  r['persistence_crps']=sum(scoring.crps_forecast(by[ENTRANTS[0]][k]['persistence'],by[ENTRANTS[0]][k]['outcome']) for k in shared)/len(shared)
  for e,label in zip(ENTRANTS,('direct','persona')):
   rs=[by[e][k] for k in shared]
   r[label+'_mae']=sum(abs(x['forecast']['topline']['mean']-x['outcome']) for x in rs)/len(rs)
   r[label+'_bias']=sum(x['forecast']['topline']['mean']-x['outcome'] for x in rs)/len(rs)
   r[label+'_mean_sd']=sum(x['forecast']['topline']['sd'] for x in rs)/len(rs)
  result.append(r)
 return result

records=[]
# Prioritize the official shared 22 so the primary comparison finishes first.
tasks=sorted(manifest['tasks'],key=lambda t:(not t['official_matched'],t['date'],t['series']))
print('START',len(tasks),'rounds;',len(tasks)*193,'planned calls',flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 futures=[pool.submit(job,t,e) for t in tasks for e in ENTRANTS]
 for fut in concurrent.futures.as_completed(futures):
  records.append(fut.result())
  snapshot={'completed_forecasts':len(records),'successful_forecasts':sum(r['complete'] for r in records),
            'planned_forecasts':len(futures),'elapsed_seconds':round(time.time()-stats['started_at']),
            'provider':dict(stats),'per_series':summary(records),'official_matched':summary(records,True)}
  atomic(OUT/'progress.json',snapshot)
  if len(records)%10==0 or not records[-1]['complete']:
   print('PROGRESS',len(records),'/',len(futures),'success',snapshot['successful_forecasts'],'calls',stats['http_attempts'],'elapsed',snapshot['elapsed_seconds'],flush=True)
report={'manifest_sha256':manifest_digest,'status':'completed' if all(r['complete'] for r in records) else 'partial',
        'model':manifest['model'],'persona_mode':manifest['persona_adapter'],'evaluation':manifest['interpretation'],
        'records':sorted(records,key=lambda r:(r['series'],r['date'],r['entrant'])),
        'per_series_scores':summary(records),'official_matched_scores':summary(records,True),
        'provider':stats,'estimated_billed_usd':stats['input_tokens']*.042/1e6}
atomic(OUT/'results.json',report);atomic(OUT/'provider-stats.json',stats)
print('DONE',report['status'],'USD estimate',report['estimated_billed_usd'],flush=True)
