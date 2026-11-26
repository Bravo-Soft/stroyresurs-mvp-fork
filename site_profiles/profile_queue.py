"""Сохраняемая очередь профилирования; отдельная от production pipeline и Kafka."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

VERSION = 1
DEFAULTS = dict(workers=2, per_host=1, host_delay=1.0, max_attempts=3,
                retry_delay=10.0, retry_cap=300.0, stage_seconds=180,
                job_seconds=900, memory_mb=2048, attempt_mb=100, snapshot_mb=256,
                min_free_mb=100, browser_slots=0, model_slots=0)
STAGES = ('discover', 'markdown', 'control')
TERMINAL = ('complete', 'review', 'failed', 'exhausted')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''): value.update(chunk)
    return value.hexdigest()


def environment():
    return dict(python=sys.version, executable=str(Path(sys.executable).resolve()),
                executable_sha256=digest(sys.executable),
                packages=sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions()
                                if d.metadata['Name']))


def workspace():
    for parent in Path(__file__).resolve().parents:
        if parent.name == 'codex_work':
            return parent
    return Path(__file__).resolve().parents[2] / 'codex_work'


def contained(path, root):
    path = Path(path).absolute()
    root = Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValueError('Путь должен находиться внутри изолированного каталога: '+str(path))
    return path.resolve()


def inventory(root):
    """Не переходим по символьным ссылкам; входы фиксируются побайтно."""
    root = Path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Символьная ссылка в пакете: '+str(path))
        if path.is_file():
            result[str(path.relative_to(root))] = digest(path)
    return result


def validate_limits(limits):
    if set(limits) != set(DEFAULTS):
        raise ValueError('Неизвестные или отсутствующие лимиты')
    for key, value in limits.items():
        if type(value) not in (int, float) or not 0 <= value < 10**9:
            raise ValueError('Неверный лимит '+key)
    for key in ('workers', 'per_host', 'max_attempts', 'stage_seconds', 'job_seconds',
                'memory_mb', 'attempt_mb', 'snapshot_mb', 'min_free_mb', 'browser_slots', 'model_slots'):
        if type(limits[key]) is not int:
            raise ValueError('Нужен целый лимит '+key)
    if not (1 <= limits['per_host'] <= limits['workers'] <= 32):
        raise ValueError('Неверные ограничения параллельности')
    if not 1 <= limits['max_attempts'] <= 10 or min(limits[k] for k in ('stage_seconds','job_seconds','attempt_mb','snapshot_mb')) < 1:
        raise ValueError('Нулевой либо избыточный бюджет')
    if limits['memory_mb'] < 256 or max(limits['browser_slots'],limits['model_slots'])>limits['workers']:
        raise ValueError('Нужно >=256 MB; ресурсные слоты не должны превышать workers')


class Queue:
    def __init__(self, root):
        self.root = contained(root, workspace())
        if not (self.root/'queue.sqlite3').is_file():
            raise ValueError('Очередь не создана')
        contained(self.root/'queue.sqlite3', self.root)
        for name in ('queue.sqlite3-journal','queue.sqlite3-wal','queue.sqlite3-shm'):
            contained(self.root/name,self.root)
        self.db = sqlite3.connect(self.root/'queue.sqlite3', timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.config = json.loads(self.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0])
        if self.config['version'] != VERSION:
            raise ValueError('Неподдерживаемая версия очереди')
        validate_limits(self.config['limits'])
        self.limits = self.config['limits']

    @classmethod
    def create(cls, root, limits=None):
        root = contained(root, workspace())
        values = DEFAULTS | (limits or {})
        validate_limits(values)
        root.mkdir(parents=True, exist_ok=False)
        for name in ('submissions','attempts'):
            (root/name).mkdir()
        config = dict(version=VERSION, limits=values)
        db = sqlite3.connect(root/'queue.sqlite3')
        # DELETE-journal оставляет совместимость с локальными файловыми системами;
        # SQL-транзакция фиксирует попытку раньше запуска дочернего процесса.
        db.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY, host TEXT NOT NULL, snapshot TEXT NOT NULL,
          spec TEXT NOT NULL, status TEXT NOT NULL, stage INTEGER NOT NULL DEFAULT 0,
          used_seconds REAL NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
          created REAL NOT NULL, reason TEXT);
        CREATE TABLE attempts(id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id),
          stage INTEGER NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL,
          started REAL NOT NULL, finished REAL, timeout REAL NOT NULL, folder TEXT NOT NULL,
          error TEXT, result TEXT, UNIQUE(job,stage,number));
        CREATE TABLE hosts(host TEXT PRIMARY KEY, next_at REAL NOT NULL);
        CREATE TABLE events(id INTEGER PRIMARY KEY, at REAL NOT NULL, job TEXT,
          attempt TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL);
        ''')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('config',encode(config)),('paused','false')])
        db.commit(); db.close()
        return cls(root)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def event(self, kind, job=None, attempt=None, **detail):
        self.db.execute('INSERT INTO events(at,job,attempt,kind,detail) VALUES (?,?,?,?,?)',
                        (time.time(),job,attempt,kind,encode(detail)))

    def pause(self, value):
        with self.transaction():
            self.db.execute("UPDATE meta SET value=? WHERE key='paused'", (encode(value),))
            self.event('pause' if value else 'resume')

    def paused(self):
        return json.loads(self.db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0])

    @contextmanager
    def dispatcher_lock(self):
        path = contained(self.root/'dispatcher.lock', self.root)
        with path.open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Очередь уже обслуживается диспетчером или его дочерним процессом')
            # FD наследуется работником: новый диспетчер не объявит живого сироту
            # погибшим. Собственный alarm работника ограничивает время удержания.
            yield handle.fileno()

    def enqueue(self, manifest):
        if shutil.disk_usage(self.root).free < self.limits['min_free_mb']*1024**2:
            raise ValueError('Недостаточно свободного диска для постановки задания')
        manifest = Path(manifest).resolve()
        if manifest.stat().st_size>self.limits['snapshot_mb']*1024**2:
            raise ValueError('Превышен размер задания')
        spec = json.loads(manifest.read_text())
        if not isinstance(spec,dict) or set(spec) - {'version','url','profiles','stages','label'} or type(spec.get('version')) is not int or spec['version'] != VERSION:
            raise ValueError('Неизвестные поля/версия задания')
        parsed = urlsplit(spec.get('url',''))
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Нужен URL сайта без учётных данных')
        host = parsed.hostname.lower().encode('idna').decode().removeprefix('www.')
        stages = spec.get('stages', [])
        if not isinstance(stages,list) or any(not isinstance(s,dict) for s in stages):
            raise ValueError('Этапы должны быть списком объектов')
        if not isinstance(spec.get('profiles'),str) or not spec['profiles']:
            raise ValueError('Нужен явный каталог профилей')
        names = [s.get('kind') for s in stages]
        if not names or any(n not in STAGES for n in names) or len(set(names)) != len(names):
            raise ValueError('Разрешены этапы discover, markdown и control, каждый один раз')
        if names != sorted(names, key=STAGES.index):
            raise ValueError('Обнаружение должно предшествовать Markdown')
        # Не даём manifest задавать произвольную команду, env или production-пути.
        for s in stages:
            allowed = {'kind','manifest','limits','control','holdout','no_sitemap'} if s['kind']=='discover' else {'kind','manifest','model_char_limit'} if s['kind']=='markdown' else {'kind','manifest'}
            if set(s)-allowed or not s.get('manifest'):
                raise ValueError('Неверная конфигурация этапа')
            if s['kind']=='markdown' and (type(s.get('model_char_limit',100000)) is not int or s.get('model_char_limit',100000)<1):
                raise ValueError('Неверный лимит Markdown')
            if s['kind']=='discover':
                if type(s.get('no_sitemap',False)) is not bool:
                    raise ValueError('no_sitemap должен быть bool')
                for field in ('control','holdout'):
                    if not isinstance(s.get(field,[]),list) or any(not isinstance(url,str) for url in s.get(field,[])):
                        raise ValueError('Контрольные URL должны быть списком строк')
                if set(s.get('limits',{}))-{'pages','urls','depth','seconds','sample'}:
                    raise ValueError('Неизвестные лимиты обнаружения')
                if any(type(v) is not int or v<1 for v in s.get('limits',{}).values()):
                    raise ValueError('Лимиты обнаружения должны быть положительными целыми')
        folder = contained(self.root/'submissions'/uuid.uuid4().hex, self.root)
        folder.mkdir()
        copied=0
        def reserve(path):
            nonlocal copied
            copied+=Path(path).stat().st_size
            if copied>self.limits['snapshot_mb']*1024**2:
                raise ValueError('Превышен размер снимка задания')
        runtime = folder/'runtime'; runtime.mkdir()
        source = Path(__file__).resolve().parents[1]
        for file in source.glob('*.py'):
            if file.is_symlink(): raise ValueError('Ссылка в исходниках')
            reserve(file)
            shutil.copyfile(file, runtime/file.name)
        for directory in ('site_profiles','Profile_Markdown'):
            origin = source/directory
            for file in origin.rglob('*.py'):
                if '__pycache__' in file.parts: continue
                if file.is_symlink(): raise ValueError('Ссылка в исходниках')
                reserve(file)
                target = runtime/file.relative_to(source); target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
        profile_dir = (manifest.parent/spec['profiles']).resolve()
        inventory(profile_dir)  # Проверяем дерево до copytree (никаких ссылок).
        if not profile_dir.is_dir(): raise ValueError('Нет каталога профилей')
        for file in profile_dir.rglob('*'):
            if file.is_file(): reserve(file)
        shutil.copytree(profile_dir, folder/'profiles')
        assets = folder/'assets'; assets.mkdir()
        def capture(path, base):
            reserve(base/path)
            raw = (base/path).resolve().read_bytes()
            key = hashlib.sha256(raw).hexdigest()
            target = assets/key
            if not target.exists(): target.write_bytes(raw)
            return '../assets/'+key
        frozen_stages = []
        manifests = folder/'manifests'; manifests.mkdir()
        for index, stage in enumerate(stages):
            origin = (manifest.parent/stage['manifest']).resolve()
            reserve(origin)
            data = json.loads(origin.read_text())
            if stage['kind']=='discover':
                for section in ('pages','resources'):
                    for row in data.get(section,{}).values():
                        if 'path' in row: row['path'] = capture(row['path'],origin.parent)
            elif stage['kind']=='control':
                from .control_run import validate_manifest
                validate_manifest(data)
                if urlsplit(data['company']['url']).hostname.lower().removeprefix('www.')!=host:
                    raise ValueError('Контрольная выборка относится к другому хосту')
                needs_browser=any(p.get('transport')=='browser' for p in data['pages'])
                if not self.limits['model_slots'] or (needs_browser and not self.limits['browser_slots']):
                    raise ValueError('Для контроля нужны явно выделенные model_slots/browser_slots')
                stage=stage|{'browser':needs_browser}
                if data.get('structure_report'):
                    data['structure_report']=capture(data['structure_report'],origin.parent)
            else:
                for page in data['pages']:
                    for field in ('html','markdown','model_input'):
                        if page.get(field): page[field] = capture(page[field],origin.parent)
                if data.get('structure_report'):
                    data['structure_report'] = capture(data['structure_report'],origin.parent)
            target = manifests/f'{index}.json'; target.write_text(encode(data))
            frozen_stages.append(stage | {'manifest':str(target.relative_to(folder))})
        frozen = spec | {'profiles':'profiles','stages':frozen_stages}
        (folder/'job.json').write_text(encode(frozen))
        (folder/'environment.json').write_text(encode(environment()))
        hashes = inventory(folder)
        job_id = hashlib.sha256(encode(dict(files=hashes, limits=self.limits)).encode()).hexdigest()
        (folder/'hashes.json').write_text(encode(hashes))
        with self.transaction():
            existing = self.db.execute('SELECT id FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not existing:
                self.db.execute('INSERT INTO jobs(id,host,snapshot,spec,status,created) VALUES (?,?,?,?,?,?)',
                                (job_id,host,str(folder.relative_to(self.root)),encode(frozen),'pending',time.time()))
                self.event('enqueued',job_id,snapshot=str(folder.relative_to(self.root)))
            else:
                self.event('duplicate_submission',job_id,snapshot=str(folder.relative_to(self.root)))
        return job_id

    def verify_snapshot(self, job):
        folder = contained(self.root/job['snapshot'], self.root)
        expected = json.loads((folder/'hashes.json').read_text())
        actual = inventory(folder); actual.pop('hashes.json')
        if actual != expected or hashlib.sha256(encode(dict(files=actual,limits=self.limits)).encode()).hexdigest()!=job['id']:
            raise ValueError('Изменены зафиксированные входы или код задания')
        if json.loads((folder/'job.json').read_text()) != json.loads(job['spec']):
            raise ValueError('Состояние очереди не соответствует снимку задания')
        if (folder/'environment.json').read_text()!=encode(environment()):
            raise ValueError('Изменены Python или установленные зависимости; нужно новое задание')
        return folder

    def verify_completed(self, job):
        for row in self.db.execute("SELECT * FROM attempts WHERE job=? AND state='done'",(job['id'],)):
            result=self.receipt(row)
            if result is None or result!=json.loads(row['result']):
                raise ValueError('Изменён результат завершённого этапа')

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self.transaction():
            if self.paused(): return None
            running = self.db.execute("SELECT j.host,j.spec,a.stage FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.state='running'").fetchall()
            if len(running) >= self.limits['workers']: return None
            for job in self.db.execute("SELECT * FROM jobs WHERE status IN ('pending','retry') AND next_at<=? ORDER BY created,id",(now,)).fetchall():
                if sum(r['host']==job['host'] for r in running) >= self.limits['per_host']: continue
                stage_spec=json.loads(job['spec'])['stages'][job['stage']]
                resources=[json.loads(r['spec'])['stages'][r['stage']] for r in running]
                if stage_spec['kind']=='control':
                    if sum(r['kind']=='control' for r in resources)>=self.limits['model_slots']: continue
                    if stage_spec.get('browser') and sum(bool(r.get('browser')) for r in resources)>=self.limits['browser_slots']: continue
                cooldown = self.db.execute('SELECT next_at FROM hosts WHERE host=?',(job['host'],)).fetchone()
                if cooldown and cooldown[0]>now: continue
                remaining = self.limits['job_seconds']-job['used_seconds']
                if remaining<=0:
                    self.db.execute("UPDATE jobs SET status='exhausted',reason='job_time_budget' WHERE id=?",(job['id'],))
                    self.event('exhausted',job['id'],reason='job_time_budget'); continue
                number = self.db.execute('SELECT count(*) FROM attempts WHERE job=? AND stage=?',(job['id'],job['stage'])).fetchone()[0]+1
                if number>self.limits['max_attempts']:
                    self.db.execute("UPDATE jobs SET status='exhausted',reason='attempt_budget' WHERE id=?",(job['id'],));continue
                attempt = uuid.uuid4().hex
                folder = f"attempts/{job['id']}/{job['stage']}/{number}_{attempt}"
                timeout = min(self.limits['stage_seconds'],remaining)
                self.db.execute('INSERT INTO attempts(id,job,stage,number,state,started,timeout,folder) VALUES (?,?,?,?,?,?,?,?)',
                                (attempt,job['id'],job['stage'],number,'running',now,timeout,folder))
                self.db.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
                self.db.execute('INSERT OR REPLACE INTO hosts VALUES (?,?)',(job['host'],now+self.limits['host_delay']))
                self.event('started',job['id'],attempt,stage=job['stage'],number=number)
                return dict(self.db.execute('SELECT * FROM attempts WHERE id=?',(attempt,)).fetchone())
        return None

    def finish(self, attempt_id, result, elapsed):
        with self.transaction():
            a = self.db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if a['state'] != 'running': return  # Повторный commit не повторяет этап.
            job = self.db.execute('SELECT * FROM jobs WHERE id=?',(a['job'],)).fetchone()
            status = result['status']
            if status not in ('pass','uncertain','fail','transient','permanent'):
                raise ValueError('Неизвестный статус результата')
            state = 'done' if status in ('pass','uncertain') else 'error'
            used = job['used_seconds']+max(0,min(elapsed,a['timeout']))
            stage = job['stage']; next_at = 0
            reason = result.get('reason',status)
            if status in ('pass','uncertain'):
                stage += 1
                status = 'pending' if stage<len(json.loads(job['spec'])['stages']) else 'complete'
                if status=='complete':
                    previous = [json.loads(r[0]) for r in self.db.execute("SELECT result FROM attempts WHERE job=? AND state='done'",(job['id'],))]
                    if result['status']=='uncertain' or any(r['status']=='uncertain' for r in previous): status='review'
            elif status=='transient':
                status = 'retry'
                if a['number']>=self.limits['max_attempts'] or used>=self.limits['job_seconds']:
                    status = 'exhausted'
                next_at = time.time()+min(self.limits['retry_cap'],self.limits['retry_delay']*2**(a['number']-1))
            else: status='failed'
            self.db.execute('UPDATE attempts SET state=?,finished=?,error=?,result=? WHERE id=?',
                            (state,time.time(),None if state=='done' else reason,encode(result),attempt_id))
            self.db.execute('UPDATE jobs SET status=?,stage=?,used_seconds=?,next_at=?,reason=? WHERE id=?',
                            (status,stage,used,next_at,reason,job['id']))
            self.db.execute('INSERT OR REPLACE INTO hosts VALUES (?,?)',(job['host'],time.time()+self.limits['host_delay']))
            self.event('finished',job['id'],attempt_id,status=status,result=result)

    def receipt(self, attempt):
        folder = contained(self.root/attempt['folder'], self.root)
        receipt = folder/'receipt.json'
        ready = folder/'receipt.ready'
        if (folder/'unavailable_receipt.json').is_file():
            receipt=contained(folder/'unavailable_receipt.json',folder)
            result=json.loads(receipt.read_text())
            if result.get('status')!='uncertain' or result.get('attempt')!=attempt['id'] or result.get('job')!=attempt['job']:
                raise ValueError('Неверная квитанция недоступности')
            for name,value in result['artifacts'].items():
                if digest(contained(folder/name,folder))!=value:raise ValueError('Изменён отчёт недоступности')
            return result
        if not ready.is_file(): return None  # Незаконченная запись после аварии.
        contained(ready,folder)
        contained(receipt,folder)
        if ready.read_text()!=digest(receipt):
            raise ValueError('Повреждена завершённая квитанция')
        result = json.loads(receipt.read_text())
        if not isinstance(result,dict) or result.get('status') not in ('pass','uncertain','fail','transient','permanent') or not isinstance(result.get('artifacts'),dict):
            raise ValueError('Неверный контракт квитанции')
        if result['status'] in ('pass','uncertain') and result.get('report') not in result['artifacts']:
            raise ValueError('У завершённого этапа нет отчёта с контрольной суммой')
        if result['attempt']!=attempt['id'] or result['job']!=attempt['job'] or result['stage']!=attempt['stage']:
            raise ValueError('Результат относится к другой попытке')
        for name, sha in result['artifacts'].items():
            if digest(contained(folder/name,folder))!=sha:
                raise ValueError('Изменены доказательства попытки')
        return result

    def recover(self):
        # Вызывать только под dispatcher_lock: нет живых работников этой очереди.
        for row in self.db.execute("SELECT * FROM attempts WHERE state='running'").fetchall():
            try:
                job=self.db.execute('SELECT * FROM jobs WHERE id=?',(row['job'],)).fetchone()
                self.verify_snapshot(job)
                result=self.receipt(row)
                if result is None:
                    if json.loads(job['spec'])['stages'][row['stage']]['kind']=='control':
                        result=self.unavailable_control(row,'interrupted_control_no_automatic_model_retry')
                    else:result=dict(status='transient',reason='interrupted_without_receipt')
            except (ValueError,OSError,KeyError) as exc:
                result=dict(status='permanent',reason='integrity: '+str(exc))
            self.finish(row['id'],result,time.time()-row['started'])

    def unavailable_control(self, attempt, reason):
        """Сохраняем неполноту инфраструктуры, не выдавая её за дефект профиля."""
        folder=contained(self.root/attempt['folder'],self.root);folder.mkdir(parents=True,exist_ok=True)
        output=contained(folder/'output',folder);output.mkdir(exist_ok=True)
        report=output/('infrastructure_'+uuid.uuid4().hex+'.json')
        report.write_text(encode(dict(status='uncertain',reason=reason,scope='incomplete_control',
                                     automatic_model_retry=False,graph=False)))
        result=dict(status='uncertain',reason=reason,report=str(report.relative_to(folder)),
                    job=attempt['job'],stage=attempt['stage'],attempt=attempt['id'],
                    artifacts={'output/'+name:value for name,value in inventory(output).items()})
        if (folder/'worker.log').is_file():result['artifacts']['worker.log']=digest(folder/'worker.log')
        # Отдельная квитанция сохраняет незавершённые receipt.* работника для разбора.
        receipt=folder/'unavailable_receipt.json';receipt.write_text(encode(result))
        return result

    def status(self):
        return dict(version=VERSION,paused=self.paused(),limits=self.limits,
                    jobs=[dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY created')],
                    attempts=[dict(r) for r in self.db.execute('SELECT * FROM attempts ORDER BY started')],
                    events=[dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id')])


def process_memory(pid):
    """RSS дерева процессов на Linux; используется вместе с таймером/изоляцией PID."""
    rows=[]
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit(): continue
        try:
            fields={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in (entry/'status').read_text().splitlines() if ':' in line}
            rows.append((int(entry.name),int(fields['PPid']),int(fields.get('VmRSS','0 kB').split()[0])*1024))
        except (OSError,ValueError,KeyError):continue
    ids={pid}
    while True:
        more={child for child,parent,_ in rows if parent in ids}-ids
        if not more:break
        ids.update(more)
    return sum(rss for child,_,rss in rows if child in ids)


def attempt_size(folder):
    """Chromium удаляет временные файлы одновременно с проверкой лимита."""
    total = 0
    for path in folder.rglob('*'):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def run_queue(queue, drain=False):
    active = {}; stopping = False; capacity_blocked = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    previous = {s:signal.signal(s,stop) for s in (signal.SIGTERM,signal.SIGINT)}
    try:
        with queue.dispatcher_lock() as lock_fd:
            queue.recover()
            while True:
                for aid, item in list(active.items()):
                    process, attempt, log, start = item
                    folder = contained(queue.root/attempt['folder'],queue.root)
                    elapsed = time.monotonic()-start
                    reason = None
                    if elapsed>attempt['timeout']+2: reason='stage_timeout'
                    size = attempt_size(folder)
                    if size>queue.limits['attempt_mb']*1024**2: reason='attempt_disk_budget'
                    if process_memory(process.pid)>queue.limits['memory_mb']*1024**2: reason='process_memory_budget'
                    if reason and process.poll() is None: process.kill()
                    if process.poll() is not None:
                        process.wait();log.close()
                        try:
                            spec=json.loads(queue.db.execute('SELECT spec FROM jobs WHERE id=?',(attempt['job'],)).fetchone()[0])
                            is_control=spec['stages'][attempt['stage']]['kind']=='control'
                            result=queue.receipt(attempt)
                            if is_control and (reason or result is None):
                                result=queue.unavailable_control(attempt,reason or 'control_unavailable_without_receipt')
                            elif reason:result=dict(status='transient' if reason=='stage_timeout' else 'permanent',reason=reason)
                            elif result is None:result=dict(status='transient',reason='worker_exit_'+str(process.returncode))
                        except (OSError,ValueError,KeyError) as exc:
                            result=dict(status='permanent',reason='receipt: '+str(exc))
                        queue.finish(aid,result,elapsed);del active[aid]
                if not stopping:
                    while shutil.disk_usage(queue.root).free >= queue.limits['min_free_mb']*1024**2:
                        attempt = queue.claim()
                        if attempt is None: break
                        job=queue.db.execute('SELECT * FROM jobs WHERE id=?',(attempt['job'],)).fetchone()
                        try:
                            snapshot=queue.verify_snapshot(job)
                            queue.verify_completed(job)
                            folder=contained(queue.root/attempt['folder'],queue.root);folder.mkdir(parents=True)
                            for name in ('home','tmp','cache'): (folder/name).mkdir()
                            request=dict(attempt=attempt['id'],job=job['id'],stage=attempt['stage'],
                                         snapshot=str(snapshot),folder=str(folder),timeout=attempt['timeout'],limits=queue.limits)
                            if attempt['stage']:
                                previous_stages=queue.db.execute("SELECT folder FROM attempts WHERE job=? AND stage<? AND state='done' ORDER BY stage DESC,number DESC",(job['id'],attempt['stage'])).fetchall()
                                for prev in previous_stages:
                                    structure=queue.root/prev[0]/'output/structure.json'
                                    if structure.exists():
                                        request['structure_report']=str(contained(structure,queue.root));break
                            (folder/'request.json').write_text(encode(request))
                            env=dict(PATH=os.environ.get('PATH','/usr/bin:/bin'), HOME=str(folder/'home'),
                                     TMPDIR=str(folder/'tmp'), XDG_CACHE_HOME=str(folder/'cache'),
                                     PYTHONDONTWRITEBYTECODE='1', LANG='C.UTF-8', OPENBLAS_NUM_THREADS='1',
                                     OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
                            control_stage=json.loads(job['spec'])['stages'][attempt['stage']]['kind']=='control'
                            if control_stage:
                                env['CONTROL_MODEL_API_KEY']=os.environ.get('CONTROL_MODEL_API_KEY','')
                                if os.environ.get('PLAYWRIGHT_BROWSERS_PATH'):
                                    env['PLAYWRIGHT_BROWSERS_PATH']=os.environ['PLAYWRIGHT_BROWSERS_PATH']
                                if os.environ.get('FONTCONFIG_FILE'):
                                    env['FONTCONFIG_FILE']=os.environ['FONTCONFIG_FILE']
                            log=(folder/'worker.log').open('xb')
                            try:
                                command=[sys.executable,'-B',str(snapshot/'runtime/site_profiles/queue_worker.py'),str(folder/'request.json')]
                                if control_stage:
                                    if not shutil.which('bwrap'): raise ValueError('Нет обязательного bwrap для живого контроля')
                                    command=['bwrap','--ro-bind','/','/','--bind',str(folder),str(folder),
                                             '--bind',str(folder/'tmp'),'/tmp','--proc','/proc','--dev','/dev',
                                             '--unshare-pid','--die-with-parent','--new-session','--sync-fd',str(lock_fd),
                                             '--chdir',str(folder),'--',*command]
                                proc=subprocess.Popen(command,
                                                      cwd=folder,env=env,stdout=log,stderr=subprocess.STDOUT,pass_fds=(lock_fd,),start_new_session=control_stage)
                            except BaseException:
                                log.close();raise
                            active[attempt['id']] = (proc,attempt,log,time.monotonic())
                        except (OSError,ValueError,KeyError) as exc:
                            queue.finish(attempt['id'],dict(status='permanent',reason='preflight: '+str(exc)),0)
                pending=queue.db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','retry','running')").fetchone()[0]
                if not active and (stopping or (drain and (not pending or queue.paused()))): break
                if not active and shutil.disk_usage(queue.root).free < queue.limits['min_free_mb']*1024**2:
                    if not capacity_blocked: queue.event('capacity_blocked',reason='min_free_mb')
                    capacity_blocked=True
                    if drain: break
                else: capacity_blocked=False
                time.sleep(0.1 if active else 0.5)
    finally:
        for process,attempt,log,start in active.values():
            if process.poll() is None: process.kill()
            process.wait();log.close()
            # Попытка остаётся running и будет восстановлена следующим диспетчером.
        for s, handler in previous.items(): signal.signal(s,handler)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue',required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init');init.add_argument('--limits',help='JSON-файл переопределения лимитов')
    add=commands.add_parser('add');add.add_argument('manifest')
    run=commands.add_parser('run');run.add_argument('--drain',action='store_true')
    for name in ('status','pause','resume'):commands.add_parser(name)
    args=parser.parse_args();q=None
    try:
        if args.command=='init':
            q=Queue.create(args.queue,json.loads(Path(args.limits).read_text()) if args.limits else None)
        else:q=Queue(args.queue)
        if args.command=='add':print(q.enqueue(args.manifest))
        elif args.command=='run':run_queue(q,args.drain)
        elif args.command in ('pause','resume'):q.pause(args.command=='pause')
        else:print(json.dumps(q.status(),ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,RuntimeError,sqlite3.Error,KeyError) as exc:
        print('Ошибка очереди: '+str(exc));return 2
    finally:
        if q:q.close()


if __name__=='__main__':raise SystemExit(main())
