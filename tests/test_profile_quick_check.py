"""Stage 1 contracts: passive, scoped evidence and honest missing coverage."""
import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from site_profiles import SiteProfile
from site_profiles import quick_check as q

COMPANY = dict(company_id='42', website='https://example.test/')
URL = COMPANY['website'] + 'product'
HTML = '<html><body><main class="product"><h1>Насос</h1><p>Мощность 25 кВт</p></main></body></html>'
PROFILE = replace(SiteProfile.default('example.test'), source='accepted')
SUCCESS = dict(status='success', crawling_result=dict(crawl_status='complete'))


def checker(tmp_path, **kw):
    return q.QuickCheck(tmp_path, COMPANY, PROFILE, **kw)


def observe(check, html=HTML, role='product', is_new=False):
    check.observe('page', dict(url=URL, role=role, html=html, is_new=is_new))
    check.observe('fetch', dict(url=URL, reason='ok', status=200, transport='http'))
    check.observe('route', dict(url=URL, role=role))


def finish(check, result=None):
    check.finish(result or SUCCESS, {'cards_generated': 0})
    return json.loads(check.path.read_text())


def reference(tmp_path, check, **page):
    path = tmp_path / 'references' / (check.company_key + '.json')
    q.atomic_json(path, dict(schema_version=1, company_id='42', website=COMPANY['website'],
                            effective_profile_sha256=q.digest(check.profile),
                            accepted_at='2026-11-25T00:00:00Z', reviewed_by='expert',
                            provenance='saved review/page_audit.json', independent=True,
                            pages={URL: page}))
    return str(path.parent), path


def test_initial_observation_is_not_quality_acceptance(tmp_path):
    check = checker(tmp_path)
    assert json.loads(check.path.read_text())['status'] == 'in_progress'
    observe(check)
    report = finish(check)
    assert report['status'] == 'insufficient_evidence'
    assert len(report['metrics']) == 6
    assert report['metrics']['routing']['correct_rate'] is None
    assert report['metrics']['output_quality']['semantic_status'] == 'not_checked'
    assert not report['findings']
    assert not list(tmp_path.rglob('*accepted*'))


def test_unchanged_no_cards_is_normal_and_class_change_detected(tmp_path):
    first = checker(tmp_path)
    observe(first)
    finish(first)
    second = checker(tmp_path)
    observe(second)
    assert finish(second)['status'] == 'no_signals'
    third = checker(tmp_path)
    observe(third, HTML.replace('class="product"', 'class="renamed"'))
    report = finish(third)
    assert report['status'] == 'attention'
    assert [f['code'] for f in report['findings']] == ['observed_structure_changed']


def test_retry_denominator_latest_final_outcome(tmp_path):
    check = checker(tmp_path)
    check.observe('fetch', dict(url=URL, reason='timeout'))
    check.observe('challenge', dict(url=URL))
    observe(check)
    check.observe('fetch', dict(url=URL + '/missing', reason='not_found', status=404))
    metric = finish(check)['metrics']['availability']
    assert metric['denominator'] == 2 and metric['attempts'] == 3
    assert metric['success_rate'] == metric['not_found_rate'] == .5
    assert metric['timeout_rate'] == 0 and metric['antibot_encounter_rate'] == .5


def test_independent_routing_and_missing_block(tmp_path):
    first = checker(tmp_path)
    refdir, path = reference(tmp_path, first, role='excluded', required_selectors=['.missing'])
    original = path.read_bytes()
    check = checker(tmp_path, reference_dir=refdir)
    observe(check)
    report = finish(check)
    metric = report['metrics']['routing']
    assert metric['correct_rate'] == 0 and metric['false_inclusions'] == 1
    assert {'routing_mismatch', 'required_block_missing'} <= {f['code'] for f in report['findings']}
    assert path.read_bytes() == original


def test_source_loss_and_actual_model_input_binding(tmp_path):
    first = checker(tmp_path)
    refdir, _ = reference(tmp_path, first, role='product', source_selectors=['main'], html_sha256=q.digest(HTML))
    check = checker(tmp_path, reference_dir=refdir)
    observe(check)
    check.observe('markdown', dict(url=URL, page_type='', html=HTML, markdown='# Насос\n\nМощность 25 кВт'))
    request = dict(model='test', messages=[dict(role='system', content='rules'), dict(role='user', content='# Насос')])
    check.observe('model_input', dict(url=URL, model='test', request=request))
    report = finish(check)
    record = report['pages'][0]
    assert record['request_sha256'] == q.digest(request)
    assert record['preservation']['model_input']['status'] == 'fail'
    assert any(f['code'] == 'source_data_loss' for f in report['findings'])


def test_changed_source_is_uncertain_not_false_loss(tmp_path):
    refdir, _ = reference(tmp_path, checker(tmp_path), role='product',
                          source_selectors=['main'], html_sha256=q.digest(HTML))
    check = checker(tmp_path, reference_dir=refdir)
    changed = HTML.replace('25', '30')
    observe(check, changed)
    check.observe('markdown', dict(url=URL, html=changed, markdown='# Насос'))
    report = finish(check)
    assert report['pages'][0]['preservation']['status'] == 'uncertain'
    assert not any(f['code'] == 'source_data_loss' for f in report['findings'])


def test_versions_and_failed_run_do_not_replace_comparable_history(tmp_path):
    first = checker(tmp_path)
    observe(first)
    finish(first)
    history = (first.directory / 'last_observed.json').read_bytes()
    failed = checker(tmp_path)
    observe(failed)
    finish(failed, dict(status='interrupted'))
    assert (first.directory / 'last_observed.json').read_bytes() == history
    changed = checker(tmp_path, limits=dict(pages=1))
    observe(changed)
    report = finish(changed)
    assert 'previous_observation_not_comparable' in report['limitations']


def test_budgets_and_roles_are_explicit(tmp_path):
    check = checker(tmp_path, max_pages=1, max_urls=1)
    observe(check)
    check.observe('page', dict(url=URL + '/2', role='contacts', html=HTML, is_new=False))
    check.observe('fetch', dict(url=URL + '/2', reason='ok'))
    report = finish(check)
    assert len(report['pages']) == 1
    assert {'page_budget_reached', 'url_budget_reached'} <= set(report['limitations'])


@pytest.mark.asyncio
async def test_storage_unchanged_keeps_fresh_structure(tmp_path):
    from temp_storage_manager import TempStorageManager
    storage = TempStorageManager(base_dir=str(tmp_path / 'storage'), max_size_mb=10)
    first = await storage.save_cleaned_html(URL, URL, HTML, 'Test', category='product')
    check = checker(tmp_path / 'reports')
    token = q._active.set(check)
    try:
        changed = HTML.replace('class="product"', 'class="replacement"')
        second = await storage.save_cleaned_html(URL, URL, changed, 'Test', category='product')
    finally:
        q._active.reset(token)
    assert second[0] == first[0] and second[1] is False
    assert check.pages[(URL, 'product')]['html'] == changed
    assert check.counts['unchanged'] == 1


@pytest.mark.asyncio
async def test_context_isolation_and_fail_open(tmp_path, monkeypatch):
    config = SimpleNamespace(profile_quick_check_enabled=True, profile_metrics_dir=str(tmp_path))
    handle = q.start_run(config, COMPANY, PROFILE, is_update=True)
    async def child():
        q.emit('page', url=URL, role='product', html=HTML, is_new=False)
    await asyncio.create_task(child())
    result = dict(SUCCESS)
    def broken(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(q, 'atomic_json', broken)
    await q.finish_run(handle, result, {})
    assert result['profile_quick_check']['status'] == 'check_error'
    assert result['status'] == 'success' and q._active.get() is None
    assert json.loads(handle[1].path.read_text())['status'] == 'in_progress'


@pytest.mark.asyncio
async def test_extractor_hook_keeps_original_html(tmp_path):
    from text_extractor import html_to_markdown
    check = checker(tmp_path)
    token = q._active.set(check)
    try:
        md = html_to_markdown(HTML, URL)
    finally:
        q._active.reset(token)
    row = check.pages[(URL, 'product')]
    assert row['extraction_html'] == HTML and row['markdown'] == md


def test_disabled_no_files_no_observations(tmp_path):
    config = SimpleNamespace(profile_quick_check_enabled=False, profile_metrics_dir=str(tmp_path))
    assert q.start_run(config, COMPANY, PROFILE, is_update=True) is None
    config.profile_quick_check_enabled = True
    assert q.start_run(config, COMPANY, PROFILE, is_update=False) is None
    q.emit('page', url=URL, html=HTML)
    assert not list(tmp_path.iterdir())


def test_archived_twenty_unchanged_pairs():
    inventory = next((p / 'codex_work/profile_benchmark_inventory_20261120/html_pair_integrity.json'
                      for p in Path(__file__).resolve().parents if (p / 'codex_work/profile_benchmark_inventory_20261120/html_pair_integrity.json').is_file()), None)
    if inventory is None or not inventory.is_file():
        pytest.skip('historical HTML pair corpus not installed')
    rows = json.loads(inventory.read_text())
    grouped = {}
    for row in rows:
        grouped.setdefault((row['company'], row['url']), {})[row['phase']] = Path(row['path'])
    assert len(grouped) == 20
    for phases in grouped.values():
        from html_comparison import html_comparison_hash
        assert html_comparison_hash(phases['baseline'].read_text()) == html_comparison_hash(phases['update'].read_text())
        assert q.shape(phases['baseline'].read_text())[0] == q.shape(phases['update'].read_text())[0]


def test_missing_required_profile_block_without_gold_reference(tmp_path):
    profile = replace(copy.deepcopy(PROFILE), schema_version=2)
    profile.extract.markdown.content_blocks = [dict(name='specs', selector='.specs', required=True)]
    check = q.QuickCheck(tmp_path, COMPANY, profile)
    observe(check)
    report = finish(check)
    assert any(f['code'] == 'profile_required_block_missing' for f in report['findings'])
    source = report['pages'][0]['evidence']['html']
    assert q.digest(Path(source['path']).read_text()) == source['sha256']


def test_bad_reference_is_explicit_error_and_old_signals_stay_unresolved(tmp_path):
    first = checker(tmp_path)
    observe(first)
    first.observe('markdown_fallback', dict(url=URL, reason='failed_adapter'))
    assert finish(first)['status'] == 'attention'
    second = checker(tmp_path)
    observe(second)
    assert 'previous_signals_require_review' in finish(second)['limitations']
    third = checker(tmp_path)
    observe(third)
    assert finish(third)['status'] == 'insufficient_evidence'
    refdir, path = reference(tmp_path, third, role='product')
    path.write_text('{bad JSON')
    broken = checker(tmp_path, reference_dir=refdir)
    observe(broken)
    assert finish(broken)['status'] == 'check_error'


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['complete', 'partial', 'failed', 'cancelled', 'startup_error'])
async def test_actual_company_lifecycle_always_finishes_report(tmp_path, outcome):
    from test_crawl_pipeline_completeness import system, crawl_data, COMPANY as company
    obj = system(tmp_path, crawl_data(outcome if outcome in ('complete', 'partial', 'failed') else 'complete'))
    obj.config.profile_quick_check_enabled = True
    obj.config.profile_metrics_dir = str(tmp_path / 'metrics')
    obj._resolved_profile = PROFILE
    (tmp_path / '1_factory').mkdir()
    if outcome == 'cancelled':
        obj.crawler.crawl_site.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await obj.process_company(dict(company))
    else:
        if outcome == 'startup_error':
            obj._resolve_streaming_mode.side_effect = RuntimeError('worker setup failed')
        result = await obj.process_company(dict(company))
        assert 'profile_quick_check' in result
        obj.crawler.crawl_site.assert_awaited_once() if outcome != 'startup_error' else None
    reports = list((tmp_path / 'metrics/quick_check').glob('*/*.json'))
    reports = [json.loads(p.read_text()) for p in reports if p.name != 'last_observed.json']
    assert len(reports) == 1 and reports[0]['status'] != 'in_progress'
    assert reports[0]['run_complete'] == (outcome == 'complete')
    assert q._active.get() is None


@pytest.mark.asyncio
async def test_model_hook_observes_actual_truncation_without_extra_requests(tmp_path):
    from ai_integration import AITunnelClient
    check = checker(tmp_path)
    observe(check)
    token = q._active.set(check)
    calls = []
    client = object.__new__(AITunnelClient)
    client.model = 'gemma4-31b-nvfp4'
    async def request(data):
        calls.append(data)
        return None, dict(input_tokens=0, output_tokens=0)
    client._make_request_with_retry = request
    try:
        await client._sgr_single('x' * 100000 + 'MUST_NOT_REACH_MODEL', '', COMPANY['website'], URL)
    finally:
        q._active.reset(token)
    assert len(calls) == 1
    row = check.pages[(URL, 'product')]
    assert row['request_sha256'] == q.digest(calls[0])
    assert 'MUST_NOT_REACH_MODEL' not in row['model_input']


@pytest.mark.asyncio
async def test_parallel_companies_do_not_mix_events(tmp_path):
    async def one(company_id):
        config = SimpleNamespace(profile_quick_check_enabled=True, profile_metrics_dir=str(tmp_path))
        company = dict(COMPANY, company_id=company_id)
        handle = q.start_run(config, company, PROFILE, is_update=True)
        await asyncio.sleep(0)
        q.emit('fetch', url=URL + company_id, reason='ok', status=200)
        result = dict(SUCCESS)
        await q.finish_run(handle, result, {})
        return handle[1]
    a, b = await asyncio.gather(one('a'), one('b'))
    assert set(a.fetches) == {URL + 'a'} and set(b.fetches) == {URL + 'b'}
    assert a.directory != b.directory and q._active.get() is None


@pytest.mark.asyncio
async def test_bad_budget_config_does_not_break_update(tmp_path, monkeypatch):
    from config import Config
    monkeypatch.setenv('PROFILE_QUICK_CHECK_ENABLED', 'true')
    monkeypatch.setenv('PROFILE_QUICK_CHECK_MAX_PAGES', 'bad-number')
    config = Config.load_from_env()
    config.profile_metrics_dir = str(tmp_path)
    handle = q.start_run(config, COMPANY, PROFILE, is_update=True)
    result = dict(SUCCESS)
    await q.finish_run(handle, result, {})
    assert result['status'] == 'success' and result['profile_quick_check']['status'] == 'check_error'


def test_source_capture_remains_bounded_if_same_page_grows(tmp_path):
    check = checker(tmp_path, max_chars=200)
    observe(check)
    observe(check, HTML * 100)
    assert not check.pages
    assert finish(check)['status'] == 'insufficient_evidence'


def test_limited_budget_and_unvisited_reference_are_not_deletions(tmp_path):
    first = checker(tmp_path)
    refdir, path = reference(tmp_path, first, role='product')
    ref = json.loads(path.read_text())
    ref['pages'][URL + '/unvisited'] = dict(role='product')
    q.atomic_json(path, ref)
    check = checker(tmp_path, reference_dir=refdir)
    observe(check)
    report = finish(check)
    assert report['metrics']['coverage_freshness']['unvisited_reference_urls'] == [URL + '/unvisited']
    assert not report['findings']
