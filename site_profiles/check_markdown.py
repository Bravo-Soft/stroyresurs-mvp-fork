"""Офлайн-проверка пакета HTML/Markdown и входа модели; только новый каталог codex_work."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
from collections import Counter

from .markdown_validation import VERSION, check_markdown, compare


def new_output(path):
    parent=next((p for p in Path(__file__).resolve().parents if p.name=='codex_work'),None)
    root=(parent or Path(__file__).resolve().parents[2]/'codex_work').resolve()
    target=Path(path).absolute();resolved=target.resolve()
    if resolved==root or not resolved.is_relative_to(root) or target.is_symlink():
        raise ValueError('Выход разрешён только в новой папке внутри codex_work')
    if target.exists(): raise ValueError('Каталог уже существует; предыдущие результаты не перезаписываются')
    target.mkdir(parents=True,exist_ok=False)
    return resolved


def sha(raw): return hashlib.sha256(raw).hexdigest()


def run(manifest_path,output_path,profiles=None,model_char_limit=100000):
    if type(model_char_limit) is not int or model_char_limit<1: raise ValueError('Неверный лимит входа модели')
    manifest_path=Path(manifest_path).resolve();raw=manifest_path.read_bytes();manifest=json.loads(raw)
    if type(manifest.get('version')) is not int or manifest['version']!=1 or not isinstance(manifest.get('pages'),list) or not manifest['pages']:
        raise ValueError('Нужен manifest version=1 с непустым pages')
    output=new_output(output_path);pages=[];inputs=[];packet_gaps=[]
    def read(path):
        file=(manifest_path.parent/path).resolve();data=file.read_bytes()
        inputs.append(dict(path=str(file),sha256=sha(data)))
        return data.decode('utf-8')
    resolver=None;old_resolver=None;te=None;old_sink=None
    try:
        if any('markdown' not in page for page in manifest['pages']):
            if not profiles: raise ValueError('Для генерации Markdown нужен явный --profiles')
            from . import resolver as rm
            from .resolver import ProfileResolver
            import text_extractor
            te=text_extractor;old_resolver=rm._resolver;old_sink=te._census_sink
            resolver=ProfileResolver(profiles);rm._resolver=resolver
        for index,page in enumerate(manifest['pages']):
            entry=dict(index=index,url=page.get('url',''),split=page.get('split','control'))
            folder=output/f'{index:04d}';folder.mkdir()
            try:
                if entry['split'] not in ('control','holdout'): raise ValueError('split: control либо holdout')
                html=read(page['html']);(folder/'source.html').write_text(html)
                events=[]
                if 'markdown' in page:
                    md=read(page['markdown'])
                else:
                    profile=resolver.resolve(page['url'])
                    te.set_census_sink(lambda event,**data:events.append(dict(event=event,**data)))
                    md=te.html_to_markdown(html,page['url'],page.get('page_type',''))
                    from dataclasses import asdict
                    from .format_v2 import digest
                    entry['profile']=dict(domain=profile.domain,schema_version=profile.schema_version,
                                          profile_version=profile.profile_version,effective_sha256=digest(asdict(profile)))
                (folder/'markdown.md').write_text(md)
                reference=page.get('reference',{})
                actual=read(page['model_input']) if page.get('model_input') else None
                if actual is not None: (folder/'model_input.txt').write_text(actual)
                result=check_markdown(html,md,url=page['url'],source_selectors=reference.get('selectors'),
                                      public_selectors=reference.get('public_selectors',[]),
                                      exclusions=reference.get('exclusions',[]),model_input=actual)
                if actual is None:
                    simulated=md[:model_char_limit]
                    (folder/'simulated_model_input.txt').write_text(simulated)
                    result['model_input_preview']=compare(result['source'],simulated,'simulated_model_input')
                    result['model_input_preview'].update(limit=model_char_limit,kind='simulation_not_capture')
                    if result['model_input_preview']['status']=='fail': result['status']='fail'
                    elif result['status']=='pass': result['status']='uncertain'
                if reference.get('origin'): result['reference_origin']=reference['origin']
                else:
                    result['uncertainties'].append('reference_origin_not_provided')
                    if result['status']=='pass': result['status']='uncertain'
                result['events']=events
                result['split']=entry['split']
                result['artifact']=str(folder.relative_to(output)/'result.json')
                (folder/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
                stages=[result,result['model_input']]
                if 'model_input_preview' in result: stages.append(result['model_input_preview'])
                entry.update(status=result['status'],result=result['artifact'],
                             finding_counts=dict(Counter(f['code'] for s in stages for f in s.get('findings',[]))))
            except Exception as exc:
                entry.update(status='error',error=f'{type(exc).__name__}: {exc}')
                (folder/'error.json').write_text(json.dumps(entry,ensure_ascii=False,indent=2))
            pages.append(entry)
        if not any(p['split']=='holdout' for p in pages): packet_gaps.append('holdout_not_provided')
        control_urls={p['url'] for p in pages if p['split']=='control'}
        if any(p['url'] in control_urls for p in pages if p['split']=='holdout'):
            packet_gaps.append('holdout_overlaps_control')
        if manifest.get('structure_report'):
            structure=json.loads(read(manifest['structure_report']))
            by_key={r['key']:r['url'] for r in structure['urls']}
            supplied={p['url'] for p in pages}
            missing=[by_key[k] for k in structure['sample'] if by_key[k] not in supplied]
            if missing: packet_gaps.append(dict(code='discovery_sample_missing',urls=missing))
            for gap in structure.get('coverage',{}).get('gaps',[]):
                packet_gaps.append(dict(code='discovery_coverage_open',reason=gap))
        counts=dict(Counter(p['status'] for p in pages))
        status='fail' if counts.get('fail') or counts.get('error') else 'uncertain' if counts.get('uncertain') or packet_gaps else 'pass'
        code_paths=[Path(__file__),Path(__file__).with_name('markdown_validation.py')]
        if te:
            base=Path(te.__file__).parent
            code_paths += [base/'text_extractor.py',base/'product_tables.py',base/'source_product_tables.py',base/'product_interactions.py',base/'universal_extractor.py']
            code_paths += sorted((base/'Profile_Markdown').glob('*.py'))
        summary=dict(checker_version=VERSION,status=status,counts=counts,pages=pages,uncertainties=packet_gaps,
                     manifest_sha256=sha(raw),inputs=inputs,
                     code_sha256={str(p):sha(p.read_bytes()) for p in code_paths},
                     isolation=dict(output=str(output),network=False,graph=False,llm=False),
                     scope='Проверка сохранённых HTML/Markdown; карточки и живая доступность не проверены')
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
        with (output/'pages.tsv').open('w',newline='') as handle:
            writer=csv.writer(handle,delimiter='\t');writer.writerow(['URL','Выборка','Статус','Дефекты','Отчёт'])
            for p in pages: writer.writerow([p['url'],p['split'],p['status'],json.dumps(p.get('finding_counts',{}),ensure_ascii=False),p.get('result',p.get('error'))])
        (output/'SUMMARY.txt').write_text('Проверка Markdown: '+status+'\n'+json.dumps(counts,ensure_ascii=False)+'\nКод возврата 0 означает сохранение отчёта, не приёмку профиля.\n')
        return summary
    except Exception as exc:
        (output/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False,indent=2));raise
    finally:
        if te is not None:
            te.set_census_sink(old_sink)
            from . import resolver as rm
            rm._resolver=old_resolver


def no_network(event,args):
    if event in ('socket.connect','socket.bind','socket.getaddrinfo'):
        raise RuntimeError('Проверка Markdown запрещает сеть')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--profiles');parser.add_argument('--model-char-limit',type=int,default=100000)
    args=parser.parse_args();sys.addaudithook(no_network)
    try: result=run(args.manifest,args.output,args.profiles,args.model_char_limit)
    except Exception as exc: print(str(exc));return 2
    print(json.dumps(dict(status=result['status'],counts=result['counts']),ensure_ascii=False));return 0


if __name__=='__main__': raise SystemExit(main())
