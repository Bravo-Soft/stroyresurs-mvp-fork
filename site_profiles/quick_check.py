"""Passive profile checks. No crawling, model calls or automatic acceptance.

The hot path retains bounded inputs; parsing and report writing run in a worker.
An accepted reference is external, read-only evidence, never a previous run.
"""
import asyncio
import csv
import copy
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import Counter
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
_active = ContextVar('profile_quick_check', default=None)
log = logging.getLogger(__name__)


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def shape(html):
    """Text-independent structure, including selectors lost by content hashing.

    A set of paths ignores list order and repeated products. A changed hash is
    a review signal, not proof that extraction has failed.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    paths = set()
    for node in soup.find_all(True):
        if node.name in ('script', 'style', 'noscript', 'iframe', 'ymaps') or node.find_parent('ymaps'):
            continue
        parts = []
        current = node
        for _ in range(5):
            if current is None or not getattr(current, 'name', None):
                break
            classes = sorted(c for c in current.get('class', [])
                             if c not in ('loading', 'loaded') and not re.search(r'\d{3,}', c))
            identifier = current.get('id', '')
            if re.search(r'\d{3,}|[a-f0-9]{16,}', identifier):
                identifier = ''
            parts.append(current.name + ('#' + identifier if identifier else '') + ''.join('.' + c for c in classes))
            current = current.parent
        paths.add('/'.join(reversed(parts)))
    return digest(sorted(paths)), soup


def emit(event, **data):
    check = _active.get()
    if check is not None and not check.closed:
        started = time.perf_counter()
        try:
            check.observe(event, data)
        except Exception as exc:
            check.errors.add('observe:' + type(exc).__name__)
            log.exception('profile_quick_check observation failed')
        finally:
            check.hot_seconds += time.perf_counter() - started


class QuickCheck:
    def __init__(self, root, company, profile=None, *, crawl_run_id='', limits=None,
                 reference_dir='', max_pages=24, max_chars=500000, max_urls=10000,
                 budget_seconds=5.0, max_per_role=6):
        self.started_at = now()
        self.started_clock = time.perf_counter()
        self.run_id = uuid.uuid4().hex
        self.company_id = str(company.get('company_id', ''))
        self.website = company.get('website', '')
        self.company_key = digest([self.company_id, self.website])[:24]
        self.directory = Path(root) / self.company_key
        self.path = self.directory / (self.run_id + '.json')
        self.crawl_run_id = crawl_run_id
        self.profile = asdict(profile) if profile is not None else None
        self.profile_object = copy.deepcopy(profile)
        self.profile_kind = 'individual' if profile is not None and not profile.is_default() else 'generic_or_missing'
        self.limits = limits or {}
        self.max_pages = max(1, int(max_pages))
        self.max_chars = max(1, int(max_chars))
        self.max_urls = max(1, int(max_urls))
        self.budget_seconds = max(0.001, float(budget_seconds))
        self.max_per_role = max(1, int(max_per_role))
        self.pages = {}
        self.roles = Counter()
        self.fetches = {}
        self.fetch_attempt_outcomes = Counter()
        self.challenges = {}
        self.routes = {}
        self.counts = Counter()
        self.rule_hits = Counter()
        self.fallbacks = Counter()
        self.observed_roles = Counter()
        self.errors = set()
        self.limitations = set()
        self.hot_seconds = 0.0
        self.closed = False
        self.reference = {}
        self.previous = {}
        self.reference_path = ''
        root_source = Path(__file__).resolve().parents[1]
        source_names = [str(p.relative_to(root_source)) for folder in
                        (root_source, root_source / 'site_profiles', root_source / 'Profile_Markdown')
                        for p in sorted(folder.glob('*.py'))]
        self.source_paths = {name: root_source / name for name in source_names}
        self.versions = dict(checker=VERSION, effective_profile_sha256=digest(self.profile),
                             source_sha256=self._source_hashes(), limits=self.limits,
                             sampling=dict(max_pages=self.max_pages, max_chars=self.max_chars,
                                           max_urls=self.max_urls, max_per_role=self.max_per_role,
                                           budget_seconds=self.budget_seconds))
        # A crash leaves this explicit marker. It is never interpreted as success.
        atomic_json(self.path, self._identity() | dict(status='in_progress'))
        previous_path = self.directory / 'last_observed.json'
        try:
            if previous_path.exists():
                self.previous = json.loads(previous_path.read_text(encoding='utf-8'))
            self._load_reference(reference_dir)
        except Exception as exc:
            self.errors.add('reference_or_history:' + type(exc).__name__)
            log.exception('profile_quick_check invalid reference/history')

    def _load_reference(self, reference_dir):
        if reference_dir:
            path = Path(reference_dir) / (self.company_key + '.json')
            if path.exists():
                ref = json.loads(path.read_text(encoding='utf-8'))
                if (ref.get('schema_version') != 1 or ref.get('company_id') != self.company_id
                        or ref.get('website') != self.website or not ref.get('accepted_at')
                        or not ref.get('reviewed_by') or not ref.get('provenance')
                        or ref.get('independent') is not True or not isinstance(ref.get('pages'), dict)):
                    raise ValueError('invalid independent reference')
                self.reference, self.reference_path = ref, str(path)

    def _source_hashes(self):
        return {name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in self.source_paths.items() if path.is_file()}

    def _identity(self):
        return dict(schema_version=VERSION, run_id=self.run_id, crawl_run_id=self.crawl_run_id,
                    company_id=self.company_id, company_key=self.company_key, website=self.website,
                    started_at=self.started_at, versions=self.versions, profile_kind=self.profile_kind,
                    runtime=dict(pid=os.getpid(), executable=sys.executable, module=__file__))

    def _bounded_set(self, mapping, key, value):
        if key in mapping or len(mapping) < self.max_urls:
            mapping[key] = value
        else:
            self.limitations.add('url_budget_reached')

    def _sample(self, url, role, html, source_kind):
        key = (url, role)
        if not url or len(html) > self.max_chars:
            self.limitations.add('html_budget_or_missing_url')
            self.pages.pop(key, None)
            return None
        if key in self.pages:
            return self.pages[key]
        if len(self.pages) >= self.max_pages or self.roles[role] >= self.max_per_role:
            self.limitations.add('page_budget_reached')
            return None
        self.roles[role] += 1
        row = dict(url=url, role=role, html=html, source_kind=source_kind)
        self.pages[key] = row
        return row

    def observe(self, event, data):
        url = data.get('url', '')
        if event == 'fetch':
            # One final outcome per fetch_page call; latest attempt per URL in rates.
            self.counts['fetch_attempts'] += 1
            self.fetch_attempt_outcomes[str(data.get('reason', 'unknown'))] += 1
            self._bounded_set(self.fetches, url, {k: data.get(k) for k in ('reason', 'status', 'transport')})
        elif event == 'challenge':
            self._bounded_set(self.challenges, url, True)
        elif event == 'route':
            self._bounded_set(self.routes, url, 'excluded' if data['role'].startswith('excluded') else data['role'])
        elif event == 'page':
            self.counts['storage_observations'] += 1
            self.observed_roles[data.get('role', 'other')] += 1
            if 'path' in data and not data['path']:
                self.limitations.add('storage_write_failed')
            self.counts['changed' if data['is_new'] else 'unchanged'] += 1
            row = self._sample(url, data.get('role', 'other'), data['html'], 'current_storage_input')
            if row is not None and len(data['html']) <= self.max_chars:
                # Fresh HTML must survive an unchanged-content early return.
                row.update(html=data['html'], path=data.get('path'), is_new=data['is_new'])
        elif event == 'markdown':
            role = data.get('page_type') or 'product'
            # Company extraction operates on contacts but may combine several pages.
            row = self._sample(url, role, data['html'], 'extraction_input')
            if row is not None and max(len(data['html']), len(data['markdown'])) <= self.max_chars:
                row.update(extraction_html=data['html'], markdown=data['markdown'])
            else:
                self.limitations.add('markdown_budget_reached')
        elif event == 'model_input':
            matches = [row for (page_url, role), row in self.pages.items()
                       if page_url == url and role == 'product']
            if not matches:
                self.counts['model_inputs_outside_sample'] += 1
            for row in matches:
                content = data['request']['messages'][1]['content']
                if len(content) <= self.max_chars:
                    row['model_input'] = content
                    row['model'] = data.get('model')
                    row['request_sha256'] = digest(data['request'])
                else:
                    self.limitations.add('model_input_budget_reached')
        elif event == 'profile_rule':
            self.rule_hits[str(data.get('selected_rule') or 'default')] += 1
        elif event == 'markdown_fallback':
            self.fallbacks[str(data.get('reason', 'unknown'))] += 1
        elif event == 'result':
            self.counts['result_' + data['outcome']] += 1

    def finish(self, result, output):
        self.closed = True
        started = time.perf_counter()
        findings = []
        def signal(code, **evidence):
            findings.append(dict(code=code, **evidence))
        complete = result.get('status') == 'success'
        if not complete:
            self.limitations.add('company_run_not_successful')
        crawl = result.get('crawling_result') or {}
        if crawl.get('crawl_status') != 'complete':
            self.limitations.add('crawl_not_confirmed_complete')
        if self.profile_kind != 'individual':
            self.limitations.add('individual_profile_missing')
        if self._source_hashes() != self.versions['source_sha256']:
            self.limitations.add('source_changed_during_run')
        comparable = (self.previous.get('versions') == self.versions
                      and self.previous.get('run_complete') is True)
        if self.previous and not comparable:
            self.limitations.add('previous_observation_not_comparable')
        if not self.previous and not self.reference:
            self.limitations.add('initial_observation_no_reference')
        reference_current = (self.reference.get('effective_profile_sha256') == self.versions['effective_profile_sha256'])
        if self.reference and not reference_current:
            self.limitations.add('reference_profile_version_mismatch')
        unresolved = (self.previous.get('unresolved_signals', False)
                      or self.previous.get('status') == 'attention') and not reference_current
        if unresolved:
            self.limitations.add('previous_signals_require_review')
        labels = self.reference.get('pages', {}) if reference_current else {}
        route_checked = route_correct = false_included = false_excluded = 0
        for url, expected in labels.items():
            role = self.routes.get(url)
            if role is None or 'role' not in expected:
                continue
            route_checked += 1
            route_correct += role == expected['role']
            false_included += expected['role'] == 'excluded' and role != 'excluded'
            false_excluded += expected['role'] != 'excluded' and role == 'excluded'
            if role != expected['role']:
                signal('routing_mismatch', url=url, expected=expected['role'], actual=role)
        outcomes = Counter(row['reason'] for row in self.fetches.values())
        denominator = len(self.fetches)
        if any(reason != 'ok' for reason in outcomes):
            signal('fetch_not_successful', outcomes=dict(outcomes))
        checked_pages, snapshots = [], {}
        evidence_bytes = 0
        max_evidence_bytes = 2 * 1024 * 1024
        previous_pages = self.previous.get('pages', {}) if comparable else {}
        for (url, role), row in self.pages.items():
            if time.perf_counter() - started > self.budget_seconds:
                self.limitations.add('analysis_time_budget_reached')
                break
            page_key = digest([url, role])
            html = row['html']
            structural_hash, soup = shape(html)
            source_hash = digest(html)
            record = dict(url=url, role=role, html_sha256=source_hash,
                          structure_sha256=structural_hash, source_kind=row['source_kind'],
                          stored_path=row.get('path'), is_new=row.get('is_new'))
            snapshots[page_key] = record.copy()
            expected = labels.get(url, {})
            previous = previous_pages.get(page_key, {})
            if self.profile_object is not None:
                effective = self.profile_object
                if effective.schema_version == 2:
                    from .format_v2 import select_page, ProfileRuleError
                    try:
                        effective = select_page(effective, url, 'company' if role == 'contacts' else role, soup)
                    except ProfileRuleError as exc:
                        signal('profile_rule_error', url=url, detail=str(exc))
                        effective = None
                if effective is not None:
                    for block in effective.extract.markdown.content_blocks:
                        if block.get('required') and not any(
                                n.get_text(strip=True) or n.find('img') for n in soup.select(block['selector'])):
                            signal('profile_required_block_missing', url=url, selector=block['selector'])
            reference_shape = expected.get('structure_sha256')
            if reference_shape and reference_shape != structural_hash:
                signal('reference_structure_changed', url=url, role=role)
            elif previous and previous.get('structure_sha256') != structural_hash:
                signal('observed_structure_changed', url=url, role=role,
                       previous_run_id=self.previous.get('run_id'))
            for selector in expected.get('required_selectors', []):
                if soup.select_one(selector) is None:
                    signal('required_block_missing', url=url, selector=selector)
            record['preservation'] = dict(status='not_checked', reason='no_independent_source_scope')
            if expected.get('source_selectors') and 'markdown' in row:
                extraction_html = row.get('extraction_html', html)
                if expected.get('html_sha256') != digest(extraction_html):
                    record['preservation'] = dict(status='uncertain', reason='reference_source_changed')
                else:
                    from .markdown_validation import check_markdown
                    comparison = check_markdown(extraction_html, row['markdown'], url=url,
                                                source_selectors=expected['source_selectors'],
                                                model_input=row.get('model_input'))
                    comparison.pop('source', None)
                    record['preservation'] = comparison
                    if comparison['status'] == 'fail':
                        signal('source_data_loss', url=url, role=role)
            if 'markdown' in row:
                record['markdown_sha256'] = digest(row['markdown'])
                record['markdown_chars'] = len(row['markdown'])
                if not row['markdown'].strip():
                    signal('empty_markdown', url=url, role=role)
            if 'model_input' in row:
                record.update(model_input_sha256=digest(row['model_input']),
                              request_sha256=row['request_sha256'], model=row.get('model'))
            record['model_input_status'] = 'observed' if 'model_input' in row else 'not_checked'
            evidence_dir = self.directory / self.run_id / page_key
            evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence = {}
            for field, suffix in [('html', '.html'), ('extraction_html', '.html'),
                                  ('markdown', '.md'), ('model_input', '.txt')]:
                if field in row:
                    encoded = row[field].encode('utf-8')
                    if evidence_bytes + len(encoded) > max_evidence_bytes:
                        self.limitations.add('evidence_storage_budget_reached')
                        evidence[field] = dict(path=None, sha256=digest(row[field]), reason='storage_budget')
                        continue
                    path = evidence_dir / (field + suffix)
                    path.write_bytes(encoded)
                    evidence_bytes += len(encoded)
                    evidence[field] = dict(path=str(path), sha256=digest(row[field]))
            record['evidence'] = evidence
            checked_pages.append(record)
        if self.fallbacks:
            signal('extraction_fallback', reasons=dict(self.fallbacks))
        if not checked_pages:
            self.limitations.add('no_pages_checked')
        if not denominator:
            self.limitations.add('no_live_fetch_observations')
        if any(p['preservation']['status'] == 'uncertain' for p in checked_pages):
            self.limitations.add('source_comparison_uncertain')
        status = ('check_error' if self.errors else 'attention' if findings else
                  'insufficient_evidence' if self.limitations else 'no_signals')
        ratio = lambda n, d: round(n / d, 6) if d else None
        preservation_counts = Counter(p['preservation']['status'] for p in checked_pages)
        report = self._identity() | dict(
            status=status, finished_at=now(), run_complete=complete,
            scope='technical_observation_only; not a semantic or visual acceptance',
            findings=findings, limitations=sorted(self.limitations), errors=sorted(self.errors),
            reference=dict(path=self.reference_path or None, sha256=digest(self.reference) if self.reference else None,
                           accepted_at=self.reference.get('accepted_at'), independent=bool(self.reference),
                           previous_run_id=self.previous.get('run_id'), previous_comparable=comparable),
            metrics={
                'availability': dict(denominator=denominator, denominator_kind='last_final_fetch_outcome_per_url',
                                     attempts=self.counts['fetch_attempts'], outcomes=dict(outcomes),
                                     attempt_outcomes=dict(self.fetch_attempt_outcomes),
                                     success_rate=ratio(outcomes['ok'], denominator),
                                     timeout_rate=ratio(outcomes['timeout'], denominator),
                                     blocked_rate=ratio(outcomes['blocked'], denominator),
                                     antibot_encounter_rate=ratio(len(set(self.challenges) & set(self.fetches)), denominator),
                                     not_found_rate=ratio(outcomes['not_found'], denominator)),
                'routing': dict(status='checked' if route_checked else 'not_checked', checked=route_checked,
                                correct_rate=ratio(route_correct, route_checked),
                                false_inclusions=false_included, false_exclusions=false_excluded,
                                false_inclusion_rate=ratio(false_included, sum(labels[u].get('role') == 'excluded' for u in self.routes if u in labels)),
                                false_exclusion_rate=ratio(false_excluded, sum(labels[u].get('role') != 'excluded' for u in self.routes if u in labels and 'role' in labels[u])),
                                labeled=len(labels), observed=len(self.routes)),
                'structure': dict(checked=len(checked_pages), templates=len({p['structure_sha256'] for p in checked_pages}),
                                  rule_hits=dict(self.rule_hits), fallbacks=dict(self.fallbacks)),
                'preservation': dict(results=dict(preservation_counts), visibility='not_checked',
                                     scope='independently scoped saved HTML; only checker-supported facts'),
                'output_quality': dict(semantic_status='not_checked', rejection_correctness='not_checked',
                                       prohibited_data='not_checked',
                                       processed=output.get('products_processed'), generated=output.get('cards_generated'),
                                       reused=self.counts['result_reused'], failed=output.get('products_failed'),
                                       rejected=self.counts['result_rejected'],
                                       rejected_below_gate=output.get('products_below_min_specs')),
                'coverage_freshness': dict(roles=dict(self.roles), sampled=len(self.pages), checked=len(checked_pages),
                                          observed_roles=dict(self.observed_roles),
                                          crawl_status=crawl.get('crawl_status', 'unknown'),
                                          stop_reason=crawl.get('stop_reason'), holdout_status='not_checked',
                                          unvisited_reference_urls=sorted(set(labels) - set(self.routes)),
                                          storage_observations=dict(self.counts),
                                          sample_policy='first N per role, bounded; not random or independent'),
            }, pages=checked_pages,
            timing=dict(hot_path_seconds=self.hot_seconds, analysis_seconds=time.perf_counter() - started,
                        wall_seconds=time.perf_counter() - self.started_clock),
            evidence_storage=dict(bytes_written=evidence_bytes, budget_bytes=max_evidence_bytes))
        atomic_json(self.path, report)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['run_id', 'company_id', 'status', 'checked_pages', 'findings', 'limitations'])
        writer.writerow([self.run_id, self.company_id, status, len(checked_pages),
                         '|'.join(f['code'] for f in findings), '|'.join(sorted(self.limitations))])
        self.path.with_suffix('.csv').write_text(out.getvalue(), encoding='utf-8-sig')
        # Observations and acceptance are separate. Never overwrite the reference.
        if complete and status != 'check_error':
            atomic_json(self.directory / 'last_observed.json', dict(
                run_id=self.run_id, run_complete=True, versions=self.versions, pages=snapshots, status=status,
                unresolved_signals=bool(unresolved or findings)))
        log.info('profile_quick_check company=%s run=%s status=%s report=%s',
                 self.company_id, self.run_id, status, self.path)
        return dict(status=status, run_id=self.run_id, report_path=str(self.path))


def start_run(config, company, profile, *, is_update, crawl_run_id='', limits=None):
    if not is_update or not getattr(config, 'profile_quick_check_enabled', False):
        return None
    root = None
    try:
        root = Path(config.profile_metrics_dir) / 'quick_check'
        check = QuickCheck(root, company, profile, crawl_run_id=crawl_run_id, limits=limits,
                           reference_dir=getattr(config, 'profile_quick_check_reference_dir', ''),
                           max_pages=getattr(config, 'profile_quick_check_max_pages', 24),
                           budget_seconds=getattr(config, 'profile_quick_check_budget_seconds', 5.0))
        return (_active.set(check), check)
    except Exception as exc:
        log.exception('profile_quick_check check_error during initialization')
        error = dict(status='check_error', reason='initialization_failed', error_type=type(exc).__name__,
                     company_id=company.get('company_id'), website=company.get('website'), started_at=now())
        try:
            atomic_json(root / ('error-' + uuid.uuid4().hex + '.json'), error)
        except Exception:
            log.exception('profile_quick_check check_error: cannot write error report')
        return (None, error)


async def finish_run(handle, result, output):
    if handle is None:
        return
    token, check = handle
    if token is None:
        result['profile_quick_check'] = check
        return
    try:
        _active.reset(token)
        result['profile_quick_check'] = await asyncio.to_thread(check.finish, result, output)
    except Exception as exc:
        log.exception('profile_quick_check check_error during finalization')
        summary = dict(status='check_error', run_id=check.run_id,
                       report_path=str(check.path), reason='finalization_failed', error_type=type(exc).__name__)
        result['profile_quick_check'] = summary
        try:
            atomic_json(check.path, check._identity() | summary)
        except Exception:
            log.exception('profile_quick_check check_error: report storage unavailable')
