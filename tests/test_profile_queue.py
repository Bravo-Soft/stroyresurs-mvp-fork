"""Очередь: процессы, сбои, повтор, версии и изоляция."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import pytest
from site_profiles.profile_queue import Queue, run_queue, encode, DEFAULTS, validate_limits

@pytest.fixture
def packet(tmp_path):
    profiles=tmp_path/'profiles';profiles.mkdir()
    (profiles/'factory.test.yaml').write_text('domain: factory.test\n')
    (tmp_path/'page.html').write_text('<main><h1>Насос</h1><p>Масса 12 кг</p></main>')
    (tmp_path/'page.md').write_text('# Насос\n\nМасса 12 кг\n')
    pages=[dict(url='https://factory.test/'+s,html='page.html',markdown='page.md',model_input='page.md',split=s,
                reference=dict(selectors=['main'],origin='Ручной контроль')) for s in ('control','holdout')]
    (tmp_path/'markdown.json').write_text(encode(dict(version=1,pages=pages)))
    (tmp_path/'saved.json').write_text(encode(dict(pages={'https://factory.test/':{'path':'page.html'}})))
    path=tmp_path/'job.json'
    path.write_text(encode(dict(version=1,url='https://factory.test/',profiles='profiles',stages=[dict(kind='markdown',manifest='markdown.json')])))
    return path

@pytest.fixture
def q(tmp_path):
    q=Queue.create(tmp_path/'queue',dict(host_delay=0,retry_delay=0,stage_seconds=15,min_free_mb=0))
    yield q
    q.close()

def change(packet,**updates):
    spec=json.loads(packet.read_text());spec.update(updates);packet.write_text(encode(spec));return packet

def job(q,jid):return dict(q.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())

def test_duplicate_snapshot_content_and_source_edits(q,packet):
    a=q.enqueue(packet);assert q.enqueue(packet)==a and len(q.status()['jobs'])==1
    (packet.parent/'page.md').write_text('Другие данные')
    assert q.enqueue(packet)!=a
    assert q.verify_snapshot(job(q,a))
    assert len(list((q.root/'submissions').iterdir()))==3

def test_profile_version_and_limits_identity(q,packet,tmp_path):
    a=q.enqueue(packet)
    (packet.parent/'profiles/factory.test.yaml').write_text('domain: factory.test\nprofile_version: 2\n')
    b=q.enqueue(packet);assert a!=b
    other=Queue.create(tmp_path/'other',dict(workers=1))
    assert other.enqueue(packet)!=b
    other.close()

@pytest.mark.parametrize('updates',[
    {'version':2},{'url':'file:///etc/passwd'},{'url':'https://user:secret@factory.test'}, {'command':['sh']},
    {'stages':[{'kind':'live','manifest':'saved.json'}]},
    {'stages':[{'kind':'markdown','manifest':'markdown.json','env':{}}]},
    {'stages':[{'kind':'markdown','manifest':'markdown.json'},{'kind':'markdown','manifest':'markdown.json'}]},
    {'stages':[{'kind':'markdown','manifest':'markdown.json'},{'kind':'discover','manifest':'saved.json'}]},
    {'stages':[{'kind':'discover','manifest':'saved.json','limits':{'pages':0}}]}])
def test_invalid_jobs(q,packet,updates):
    with pytest.raises(ValueError):q.enqueue(change(packet,**updates))
    assert not q.status()['jobs']

@pytest.mark.parametrize('updates',[{'workers':0},{'workers':33},{'per_host':3},{'max_attempts':0},
    {'stage_seconds':0},{'memory_mb':100},{'browser_slots':3},{'model_slots':3},
    {'workers':True},{'host_delay':float('nan')},{'extra':1}])
def test_invalid_limits(updates):
    with pytest.raises(ValueError):validate_limits(DEFAULTS|updates)

def test_pause_persist_restart_and_lock(q,packet):
    q.enqueue(packet);q.pause(True);other=Queue(q.root)
    assert other.paused() and other.claim() is None
    with q.dispatcher_lock():
        with pytest.raises(RuntimeError):
            with other.dispatcher_lock():pass
    other.pause(False);assert q.claim() is not None
    other.close()

def test_global_and_host_limits(q,packet):
    a=q.enqueue(packet);change(packet,label='вторая версия');q.enqueue(packet)
    change(packet,url='https://other.test/');c=q.enqueue(packet)
    first=q.claim();second=q.claim();assert {first['job'],second['job']}=={a,c}
    assert q.claim() is None
    q.finish(first['id'],dict(status='pass'),0.1)
    assert q.claim()['job']!=c

def test_host_alias_and_cooldown(q,packet):
    q.limits['host_delay']=10
    q.enqueue(packet);change(packet,url='https://www.factory.test/');q.enqueue(packet)
    now=time.time();a=q.claim(now);q.finish(a['id'],dict(status='pass'),0)
    assert q.claim(now+1) is None and q.claim(now+11) is not None

def test_retry_bounded_and_no_stage_reexecution(q,packet):
    change(packet,stages=[dict(kind='discover',manifest='saved.json'),dict(kind='markdown',manifest='markdown.json')])
    jid=q.enqueue(packet);first=q.claim();q.finish(first['id'],dict(status='uncertain'),0.1)
    q.finish(first['id'],dict(status='pass'),0.1)
    assert job(q,jid)['stage']==1
    for i in range(3):
        a=q.claim();assert a['stage']==1 and a['number']==i+1
        q.finish(a['id'],dict(status='transient',reason='timeout'),0.1)
    assert job(q,jid)['status']=='exhausted' and q.claim() is None
    assert len(q.status()['attempts'])==4

def test_retry_delay_persisted(q,packet):
    q.limits['retry_delay']=5;q.limits['retry_cap']=8
    jid=q.enqueue(packet);a=q.claim();now=time.time();q.finish(a['id'],dict(status='transient'),1)
    assert q.claim(now+4) is None
    b=q.claim(now+6);q.finish(b['id'],dict(status='transient'),1)
    assert 7.9 <= job(q,jid)['next_at']-time.time() <= 8

def test_total_job_time_budget(q,packet):
    q.limits['job_seconds']=2
    jid=q.enqueue(packet);a=q.claim();assert a['timeout']==2
    q.finish(a['id'],dict(status='transient'),10)
    assert job(q,jid)['status']=='exhausted' and q.claim() is None

@pytest.mark.parametrize('result,expected',[('pass','complete'),('uncertain','review'),('fail','failed'),('permanent','failed')])
def test_report_status_not_exit(q,packet,result,expected):
    jid=q.enqueue(packet);a=q.claim();q.finish(a['id'],dict(status=result),0.1)
    assert job(q,jid)['status']==expected and q.claim() is None

def test_recovery_without_result_new_attempt(q,packet):
    jid=q.enqueue(packet);a=q.claim()
    with q.dispatcher_lock():q.recover()
    assert job(q,jid)['status']=='retry'
    b=q.claim();assert b['id']!=a['id'] and b['number']==2 and b['folder']!=a['folder']
    assert q.status()['attempts'][0]['error']=='interrupted_without_receipt'

def write_receipt(q,a,status='pass'):
    folder=q.root/a['folder'];folder.mkdir(parents=True)
    (folder/'proof.txt').write_text('доказательство')
    result=dict(status=status,attempt=a['id'],job=a['job'],stage=a['stage'],report='proof.txt',
                artifacts={'proof.txt':hashlib.sha256((folder/'proof.txt').read_bytes()).hexdigest()})
    (folder/'receipt.json').write_text(encode(result))
    (folder/'receipt.ready').write_text(hashlib.sha256((folder/'receipt.json').read_bytes()).hexdigest())
    return folder

def test_recovery_committed_result_does_not_repeat(q,packet):
    jid=q.enqueue(packet);a=q.claim();write_receipt(q,a)
    with q.dispatcher_lock():q.recover()
    assert job(q,jid)['status']=='complete' and q.claim() is None
    assert len(q.status()['attempts'])==1

def test_corrupt_artifact_blocks_recovery(q,packet):
    jid=q.enqueue(packet);a=q.claim();folder=write_receipt(q,a)
    (folder/'proof.txt').write_text('искажение')
    with q.dispatcher_lock():q.recover()
    assert job(q,jid)['status']=='failed'

def test_mutated_snapshot_blocks_launch(q,packet):
    jid=q.enqueue(packet);snapshot=q.root/job(q,jid)['snapshot']
    (snapshot/'profiles/factory.test.yaml').write_text('domain: changed.test')
    run_queue(q,drain=True)
    assert job(q,jid)['status']=='failed' and 'зафиксированные' in job(q,jid)['reason']

def test_symlink_rejected(q,packet,tmp_path):
    with pytest.raises(ValueError):Queue.create('/tmp/not-codex-queue')
    alias=tmp_path/'link';alias.symlink_to('/tmp',target_is_directory=True)
    with pytest.raises(ValueError):Queue.create(alias/'queue')
    (packet.parent/'profiles/alias.yaml').symlink_to(packet.parent/'page.html')
    with pytest.raises(ValueError):q.enqueue(packet)

def test_real_markdown_worker_resume(q,packet):
    jid=q.enqueue(packet);run_queue(q,drain=True)
    assert job(q,jid)['status']=='complete',q.status()
    before=q.status()['attempts'];run_queue(q,drain=True);assert q.status()['attempts']==before
    folder=q.root/before[0]['folder'];receipt=json.loads((folder/'receipt.json').read_text())
    assert receipt['status']=='pass' and receipt['artifacts']
    assert json.loads((folder/'output/summary.json').read_text())['isolation']['graph'] is False

def test_real_fail_no_retry(q,packet):
    (packet.parent/'page.md').write_text('# Насос\nМасса 15 кг')
    jid=q.enqueue(packet);run_queue(q,drain=True)
    assert job(q,jid)['status']=='failed' and len(q.status()['attempts'])==1

def test_real_missing_input_uncertain(q,packet):
    path=packet.parent/'markdown.json';data=json.loads(path.read_text())
    for row in data['pages']:row.pop('model_input')
    path.write_text(encode(data));jid=q.enqueue(packet);run_queue(q,drain=True)
    assert job(q,jid)['status']=='review'

def test_real_two_stages_propagate_gaps(q,packet):
    change(packet,stages=[dict(kind='discover',manifest='saved.json',no_sitemap=True),dict(kind='markdown',manifest='markdown.json')])
    jid=q.enqueue(packet);run_queue(q,drain=True)
    assert job(q,jid)['status']=='review',q.status()
    attempts=q.status()['attempts'];assert len(attempts)==2
    second=json.loads((q.root/attempts[1]['folder']/'output/summary.json').read_text())
    assert any(isinstance(g,dict) and g['code']=='discovery_sample_missing' for g in second['uncertainties'])

def test_guard_real_process(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script='''
import sys, pathlib, socket, subprocess
from site_profiles.queue_worker import guard
guard(pathlib.Path(sys.argv[1]))
checks=[lambda: pathlib.Path(sys.argv[2]).write_text('bad'),
 lambda: socket.create_connection(('127.0.0.1',4242)),
 lambda: subprocess.run(['true']),lambda: __import__('graph_db_uploader'),
 lambda: pathlib.Path(sys.argv[1],'link').symlink_to(sys.argv[2])]
for call in checks:
 try: call()
 except PermissionError: pass
 else: raise AssertionError('Запрет не сработал')
pathlib.Path(sys.argv[1],'allowed.txt').write_text('ok')
'''
    out=tmp_path/'sandbox';out.mkdir();outside=tmp_path/'production.txt'
    result=subprocess.run([sys.executable,'-B','-c',script,str(out),str(outside)],cwd=root,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert not outside.exists() and (out/'allowed.txt').read_text()=='ok'

def test_incomplete_receipt_retried(q,packet):
    jid=q.enqueue(packet);a=q.claim();folder=q.root/a['folder'];folder.mkdir(parents=True)
    (folder/'receipt.json').write_text('{"status":')
    with q.dispatcher_lock():q.recover()
    assert job(q,jid)['status']=='retry'


def test_completed_artifact_changed_blocks_next_stage(q,packet):
    change(packet,stages=[dict(kind='discover',manifest='saved.json'),dict(kind='markdown',manifest='markdown.json')])
    jid=q.enqueue(packet);a=q.claim();folder=write_receipt(q,a,'uncertain')
    q.finish(a['id'],q.receipt(a),0.1)
    (folder/'proof.txt').write_text('изменено')
    run_queue(q,drain=True)
    assert job(q,jid)['status']=='failed'
    assert q.status()['attempts'][1]['error'].startswith('preflight:')


def run_script(script,q):
    return subprocess.run([sys.executable,'-B','-c',script,str(q.root)],
                          cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)


def test_real_worker_crash_retries_then_succeeds(q,packet):
    jid=q.enqueue(packet)
    result=run_script('''
import sys
import site_profiles.profile_queue as m
original=m.subprocess.Popen
first=True
def launch(args,**kw):
 global first
 if first:
  first=False
  args=[sys.executable,'-B','-c','import os;os._exit(7)']
 return original(args,**kw)
m.subprocess.Popen=launch
q=m.Queue(sys.argv[1]);m.run_queue(q,drain=True)
''',q)
    assert result.returncode==0,result.stderr
    assert job(q,jid)['status']=='complete'
    assert [a['state'] for a in q.status()['attempts']]==['error','done']
    assert q.status()['attempts'][0]['error']=='worker_exit_7'


def test_real_dispatcher_crash_after_receipt_adopts_without_repeat(q,packet):
    jid=q.enqueue(packet)
    result=run_script('''
import os,sys
from site_profiles.profile_queue import Queue,run_queue
q=Queue(sys.argv[1])
def crash(*args):os._exit(19)
q.finish=crash
run_queue(q,drain=True)
''',q)
    assert result.returncode==19
    assert job(q,jid)['status']=='running'
    run_queue(q,drain=True)
    assert job(q,jid)['status']=='complete' and len(q.status()['attempts'])==1


def test_orphan_holds_lock_until_bounded_exit(q,packet,tmp_path):
    jid=q.enqueue(packet)
    script='''
import os,sys,time
import site_profiles.profile_queue as m
original=m.subprocess.Popen
def orphan(args,**kw):
 proc=original([sys.executable,'-B','-c','import signal,time;signal.alarm(2);time.sleep(20)'],**kw)
 os._exit(21)
m.subprocess.Popen=orphan
m.run_queue(m.Queue(sys.argv[1]),drain=True)
'''
    # stdout процесса диспетчера в файл: сирота не держит pipe тестового раннера.
    log=(tmp_path/'parent.log').open('wb')
    parent=subprocess.Popen([sys.executable,'-B','-c',script,str(q.root)],
                            cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=log)
    assert parent.wait(timeout=10)==21;log.close()
    with pytest.raises(RuntimeError):
        with q.dispatcher_lock():pass
    deadline=time.monotonic()+5
    while True:
        try:
            with q.dispatcher_lock():break
        except RuntimeError:
            assert time.monotonic()<deadline;time.sleep(0.05)
    run_queue(q,drain=True)
    assert job(q,jid)['status']=='complete' and len(q.status()['attempts'])==2


def test_real_stage_timeout_bounded(q,packet):
    q.limits['stage_seconds']=1;q.limits['max_attempts']=2
    # Сохраняем согласованный конфиг перед постановкой задания.
    q.config['limits']=q.limits
    q.db.execute("UPDATE meta SET value=? WHERE key='config'",(encode(q.config),))
    jid=q.enqueue(packet)
    result=run_script('''
import sys
import site_profiles.profile_queue as m
original=m.subprocess.Popen
def hung(args,**kw):
 return original([sys.executable,'-B','-c','import time;time.sleep(20)'],**kw)
m.subprocess.Popen=hung
m.run_queue(m.Queue(sys.argv[1]),drain=True)
''',q)
    assert result.returncode==0,result.stderr
    assert job(q,jid)['status']=='exhausted'
    assert len(q.status()['attempts'])==2
    assert all(a['error']=='stage_timeout' for a in q.status()['attempts'])


def test_capacity_blocks_without_claim(q,packet,monkeypatch):
    jid=q.enqueue(packet)
    q.limits['min_free_mb']=1
    from collections import namedtuple
    usage=namedtuple('usage','total used free')
    monkeypatch.setattr('site_profiles.profile_queue.shutil.disk_usage',lambda p:usage(10,10,0))
    run_queue(q,drain=True)
    assert job(q,jid)['status']=='pending' and not q.status()['attempts']
    assert q.status()['events'][-1]['kind']=='capacity_blocked'


def test_worker_log_disk_budget(q,packet):
    q.limits['attempt_mb']=1;q.config['limits']=q.limits
    q.db.execute("UPDATE meta SET value=? WHERE key='config'",(encode(q.config),))
    jid=q.enqueue(packet)
    result=run_script('''
import sys
import site_profiles.profile_queue as m
original=m.subprocess.Popen
def disk(args,**kw):
 return original([sys.executable,'-B','-c',"import pathlib;pathlib.Path('huge').write_bytes(b'x'*2000000)"],**kw)
m.subprocess.Popen=disk
m.run_queue(m.Queue(sys.argv[1]),drain=True)
''',q)
    assert result.returncode==0,result.stderr
    assert job(q,jid)['status']=='failed' and len(q.status()['attempts'])==1
    assert q.status()['attempts'][0]['error']=='attempt_disk_budget'

def test_environment_change_requires_new_job(q,packet,monkeypatch):
    jid=q.enqueue(packet)
    from site_profiles import profile_queue as module
    original=module.environment
    monkeypatch.setattr(module,'environment',lambda:original()|{'python':'другая версия'})
    run_queue(q,drain=True)
    assert job(q,jid)['status']=='failed' and 'зависимости' in job(q,jid)['reason']


def test_queue_database_symlink_denied(q,tmp_path):
    outside=tmp_path/'other.db';outside.write_text('не менять')
    (q.root/'queue.sqlite3-journal').symlink_to(outside)
    with pytest.raises(ValueError):Queue(q.root)
    assert outside.read_text()=='не менять'


def test_snapshot_size_bound(q,packet):
    q.limits['snapshot_mb']=1
    (packet.parent/'page.html').write_text('x'*2000000)
    with pytest.raises(ValueError,match='размер снимка'):q.enqueue(packet)
    assert not q.status()['jobs']


def test_pause_drains_active_then_resumes(q,packet):
    jid=q.enqueue(packet)
    result=run_script('''
import sys
import site_profiles.profile_queue as m
q=m.Queue(sys.argv[1]);original=m.subprocess.Popen
def pause(args,**kw):
 proc=original(args,**kw)
 q.pause(True)
 return proc
m.subprocess.Popen=pause
m.run_queue(q,drain=True)
''',q)
    assert result.returncode==0,result.stderr
    assert q.paused() and job(q,jid)['status']=='complete'
    change(packet,label='вторая версия');other=q.enqueue(packet)
    run_queue(q,drain=True);assert job(q,other)['status']=='pending'
    q.pause(False);run_queue(q,drain=True);assert job(q,other)['status']=='complete'


def test_simultaneous_claims_are_transactional(q,packet):
    q.enqueue(packet)
    script='''
import sys,json
from site_profiles.profile_queue import Queue
q=Queue(sys.argv[1]);print(json.dumps(q.claim()))
'''
    cwd=Path(__file__).resolve().parents[1]
    processes=[subprocess.Popen([sys.executable,'-B','-c',script,str(q.root)],cwd=cwd,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(2)]
    results=[]
    for p in processes:
        out,err=p.communicate(timeout=10);assert p.returncode==0,err;results.append(json.loads(out))
    assert sum(r is not None for r in results)==1
    assert len(q.status()['attempts'])==1


def test_original_inputs_unmodified_during_success_failure_resume(q,packet):
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in packet.parent.glob('*') if p.is_file()}
    profiles={str(p):p.read_bytes() for p in (packet.parent/'profiles').glob('*')}
    jid=q.enqueue(packet);run_queue(q,drain=True);run_queue(q,drain=True)
    assert job(q,jid)['status']=='complete'
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in before.items())
    assert all(Path(p).read_bytes()==raw for p,raw in profiles.items())

def test_real_generation_uses_frozen_profile(q,packet):
    path=packet.parent/'markdown.json';data=json.loads(path.read_text())
    for row in data['pages']:row.pop('markdown')
    path.write_text(encode(data))
    (packet.parent/'profiles/factory.test.yaml').write_text('domain: factory.test\nextract:\n  markdown:\n    product_container: main\n')
    jid=q.enqueue(packet)
    # После постановки исходный кандидат меняется, задание продолжает старую версию.
    (packet.parent/'profiles/factory.test.yaml').write_text('broken: [')
    run_queue(q,drain=True)
    assert job(q,jid)['status']=='complete',q.status()
    folder=q.root/q.status()['attempts'][0]['folder']
    report=json.loads((folder/'output/summary.json').read_text())
    assert all(row['profile']['effective_sha256'] for row in report['pages'])
