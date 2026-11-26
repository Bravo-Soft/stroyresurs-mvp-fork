"""Работник фиксированного этапа. Сеть, subprocess и запись вне попытки запрещены."""
import asyncio
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import signal
import sys
from types import SimpleNamespace


def guard(root, network=None, browser=False):
    root=Path(root).resolve()
    def writable(value):
        if isinstance(value,int):
            # Дескрипторы логов и наследованной блокировки создаёт диспетчер.
            return
        path=Path(os.fsdecode(value)).absolute()
        temporary=network is not None and path.resolve().is_relative_to('/tmp')
        if path.is_symlink() or (not path.resolve().is_relative_to(root) and not temporary):
            raise PermissionError('Запись вне попытки запрещена: '+str(path))
    def audit(event,args):
        if event.startswith('socket.') and event in ('socket.connect','socket.bind','socket.getaddrinfo','socket.sendto'):
            if network is None:raise PermissionError('Сеть выключена в очереди профилирования')
            network.network_audit(event,args)
        if event in ('subprocess.Popen','os.system','os.posix_spawn','os.fork','os.forkpty','os.exec'):
            if not (browser and event=='subprocess.Popen' and str(args[0]).endswith('/playwright/driver/node')):
                raise PermissionError('Дочерние процессы этапа запрещены')
        if event=='import' and (args[0].split('.')[0] in ('main','graph_db_uploader','kafka','pipeline_orchestrator')):
            raise PermissionError('Production pipeline/граф запрещены')
        if event=='open':
            path,mode,flags=args
            if (mode and any(c in mode for c in 'wax+')) or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
                writable(path)
        elif event in ('os.remove','os.rmdir'):
            raise PermissionError('Удаление материалов запрещено')
        elif event in ('os.rename','os.link','os.symlink'):
            raise PermissionError('Перемещения и ссылки в попытке запрещены')
        elif event in ('os.mkdir','os.chmod','os.chown','os.truncate','os.utime'):
            writable(args[0])
    sys.addaudithook(audit)


def execute(request, transport=None):
    snapshot=Path(request['snapshot']);folder=Path(request['folder'])
    spec=json.loads((snapshot/'job.json').read_text())
    stage=spec['stages'][request['stage']]
    manifest=snapshot/stage['manifest']
    output=folder/'output'
    if stage['kind']=='control':
        from site_profiles.control_run import execute as control
        data=json.loads(manifest.read_text())
        if data.get('structure_report'):
            data['structure_report']=str((manifest.parent/data['structure_report']).resolve())
        if request.get('structure_report'):data['structure_report']=request['structure_report']
        local=folder/'control_manifest.json';local.write_text(json.dumps(data,ensure_ascii=False))
        result=asyncio.run(control(local,snapshot/'profiles',output,transport=transport))
        return dict(status=result['status'],reason='control_'+result['status'],report='output/summary.json')
    if stage['kind']=='discover':
        from site_profiles.discover_structure import execute as discover
        limits=dict(pages=60,urls=10000,depth=5,seconds=120,sample=30)|stage.get('limits',{})
        limits['seconds']=min(limits['seconds'],max(1,int(request['timeout'])-1))
        args=SimpleNamespace(url=spec['url'],profiles=str(snapshot/'profiles'),
                             output=str(output),manifest=str(manifest),live=False,
                             control=stage.get('control',[]),holdout=stage.get('holdout',[]),
                             no_sitemap=stage.get('no_sitemap',False),**limits)
        asyncio.run(discover(args))
        report=json.loads((output/'structure.json').read_text())
        status='uncertain'  # Сохранённый корпус не доказывает живую полноту.
        return dict(status=status,reason='saved_discovery_requires_review',report='output/structure.json')
    if stage['kind']=='markdown':
        from site_profiles.check_markdown import run
        data=json.loads(manifest.read_text())
        for page in data['pages']:
            for key in ('html','markdown','model_input'):
                if page.get(key): page[key]=str((manifest.parent/page[key]).resolve())
        if data.get('structure_report'):
            data['structure_report']=str((manifest.parent/data['structure_report']).resolve())
        if request.get('structure_report'):
            data['structure_report']=request['structure_report']
        local=folder/'markdown_manifest.json';local.write_text(json.dumps(data,ensure_ascii=False))
        report=run(local,output,snapshot/'profiles',stage.get('model_char_limit',100000))
        return dict(status=report['status'],reason='markdown_'+report['status'],report='output/summary.json')
    raise ValueError('Неподдерживаемый этап')


def main():
    request=json.loads(Path(sys.argv[1]).read_text())
    folder=Path(request['folder']).resolve();snapshot=Path(request['snapshot']).resolve()
    sys.path.insert(0,str(snapshot/'runtime'))
    os.chdir(folder)
    # Ограничения действуют и при гибели родителя. SIGALRM завершает зависший этап.
    signal.signal(signal.SIGALRM,signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL,request['timeout'])
    memory=request['limits']['memory_mb']*1024**2
    spec=json.loads((snapshot/'job.json').read_text());stage=spec['stages'][request['stage']]
    browser=stage.get('browser',False)
    if not browser:resource.setrlimit(resource.RLIMIT_AS,(memory,memory))
    cpu=max(1,math.ceil(request['timeout']))
    resource.setrlimit(resource.RLIMIT_CPU,(cpu,cpu))
    size=request['limits']['attempt_mb']*1024**2
    resource.setrlimit(resource.RLIMIT_FSIZE,(size,size))
    transport=None
    if stage['kind']=='control':
        from site_profiles.control_run import validate_manifest,sandbox_check
        from site_profiles.control_transport import FreshTransport
        sandbox_check(folder)
        limits,policy=validate_manifest(json.loads((snapshot/stage['manifest']).read_text()))
        transport=FreshTransport(policy,limits)
        guard(folder,policy,browser)
    else:guard(folder)
    try:
        result=execute(request,transport)
    except Exception as exc:
        transient=isinstance(exc,(TimeoutError,ConnectionError)) or isinstance(exc,OSError) and exc.errno in (errno.EAGAIN,errno.ETIMEDOUT,errno.ECONNRESET)
        result=dict(status='transient' if transient else 'permanent',reason=type(exc).__name__+': '+str(exc))
    artifacts={}
    if (folder/'output').exists():
        for path in (folder/'output').rglob('*'):
            if path.is_file():
                artifacts[str(path.relative_to(folder))]=hashlib.sha256(path.read_bytes()).hexdigest()
    result.update(attempt=request['attempt'],job=request['job'],stage=request['stage'],artifacts=artifacts)
    # SQLite фиксируется после receipt. При гибели между ними новый диспетчер
    # проверит хеши и завершит ту же попытку без повторного исполнения.
    with (folder/'receipt.json').open('x') as file:
        json.dump(result,file,ensure_ascii=False);file.flush();os.fsync(file.fileno())
    with (folder/'receipt.ready').open('x') as file:
        file.write(hashlib.sha256((folder/'receipt.json').read_bytes()).hexdigest())
        file.flush();os.fsync(file.fileno())
    descriptor=os.open(folder,os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    return 0


if __name__=='__main__':raise SystemExit(main())
