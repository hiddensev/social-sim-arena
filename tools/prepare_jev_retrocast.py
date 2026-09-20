"""Freeze all published legacy retrocast targets without fetching fresh data."""
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
from collections import Counter,defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from ssa import series,model_backtest,baselines,harness,jev
from ssa.adapters import silverbulletin as sb,umich
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out',type=Path,required=True,help='new local evidence directory')
args=parser.parse_args()
OUT=args.out
if OUT.exists():raise SystemExit('Output directory already exists; keep the original frozen manifest intact.')
OUT.mkdir(parents=True)
old={}; per_entrant=defaultdict(set); conflicts=[]
for line in (ROOT/'backtest/runs/2026-08-09.jsonl').read_text().splitlines():
 r=json.loads(line);key=(r['series'],r['date'])
 if key in old and old[key]!=r['outcome']:conflicts.append({'series':key[0],'date':key[1],'earlier':old[key],'selected_latest':r['outcome']})
 old[key]=r['outcome']
 if r['topline'] is not None:per_entrant[r['entrant']].add(key)
matched=set.intersection(*per_entrant.values())
sources={k:ROOT/f'sources/{k}/2026-08-18.csv' for k in ['sb_approval','sb_generic','umich']}
raw={k:sb.parse(p.read_text()) for k,p in sources.items() if k!='umich'}
tasks=[];warmups={};vintage_differences=[]
for sid in sorted({k[0] for k in old}):
 spec=series.SERIES[sid];filters={k:v for k,v in spec.get('filters',{}).items() if k!='sponsor'}
 if spec['source']=='umich':hist=umich.parse(sources['umich'].read_text())
 else:
  fn=sb.approval_polls if spec['source']=='sb_approval' else sb.generic_ballot_polls
  hist=sb.to_series(fn(rows=raw[spec['source']],**filters),spec['value'])
 targets=sorted((d,v) for (s,d),v in old.items() if s==sid)
 # Prefix is the earliest committed source vintage. Every scored target and
 # all subsequent history values come from the original stored run outcomes.
 past=[p for p in hist if p['date']<targets[0][0]]
 if len(past)<model_backtest.WARMUP:raise ValueError((sid,'insufficient warmup'))
 warmups[sid]=past.copy()
 archive={p['date']:p['value'] for p in hist}
 for date,value in targets:
  if archive.get(date)!=value:vintage_differences.append({'series':sid,'date':date,'original_run':value,'later_archive':archive.get(date)})
  r=model_backtest.pseudo_round(sid,date)
  r['baselines']=baselines.all_baselines(past,date)
  jev.request_for(harness.build_prompt(r,past),harness.model_id('jev'))
  for p in past:assert p['date']<date
  tasks.append({'round':r,'history':past.copy(),'outcome':value,'date':date,'series':sid,'official_matched':(sid,date) in matched})
  past.append({'date':date,'value':value})
assert len(tasks)==339 and sum(t['official_matched'] for t in tasks)==22
manifest={'scope':'All 339 unique targets in official 2026-08-09 model run; 22 common targets separately scored',
 'interpretation':'historical-functional-only; Jev training cutoff unknown; current frozen harness, reconstructed historical inputs, not an exact original-prompt replay',
 'tasks':tasks,'counts':dict(Counter(t['series'] for t in tasks)),
 'window':{'first':min(t['date'] for t in tasks),'last':max(t['date'] for t in tasks)},
 'source_run_sha256':hashlib.sha256((ROOT/'backtest/runs/2026-08-09.jsonl').read_bytes()).hexdigest(),
 'warmup_archive':{k:{'path':str(p.relative_to(ROOT)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for k,p in sources.items()},
 'warmup_lengths':{k:len(v) for k,v in warmups.items()},'original_outcome_conflicts':conflicts,'later_vintage_differences':vintage_differences,
 'original_filters':'YouGov pollster filter without sponsor restriction, matching historical run; live current sponsor filter is narrower',
 'prompt_version':'current local harness at 4e31a752 plus party_margin probability aggregation support',
 'inference_protocol':'fresh 192-persona panel per round, original within-round reply caching; no cross-round response reuse',
 'model':'jev-1.13.0','persona_adapter':'jev-persona-expectation-v2','calls_before_retry':339*193,
 'approx_cost_usd':339*(7500+192*550)*0.042/1e6}
(OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({k:v for k,v in manifest.items() if k not in ['tasks','warmup_archive','later_vintage_differences']},indent=2))
print('later vintage differences:',len(vintage_differences))
