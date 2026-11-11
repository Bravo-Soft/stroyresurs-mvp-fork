"""Общая очередь выдачи: атомарные резервы, снимки и подтверждения доставки.

SQLite и блокировки находятся в общем bind-mount Bot обоих контейнеров.
Резервы не истекают по таймеру: после сбоя отправка могла состояться.
"""
import argparse
import asyncio
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def data_version(company):
    if company.get('data_version'):
        return str(company['data_version'])
    # Совместимость со старыми компаниями без явной версии загрузки.
    data = {k: v for k, v in company.items()
            if k not in {'report_date', 'report_version'}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def company_state_path(bot_dir, company_id):
    name = hashlib.sha256(str(company_id).encode()).hexdigest()
    return Path(bot_dir) / 'company_locks' / (name + '.state')


def company_ready(bot_dir, company_id):
    path = company_state_path(bot_dir, company_id)
    return not path.exists() or path.read_text() == 'ready'


@contextmanager
def company_lock(bot_dir, company_id):
    """Не ждём блокировку синхронно внутри event loop."""
    directory = Path(bot_dir) / 'company_locks'
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(company_id).encode()).hexdigest()
    with (directory / (name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@asynccontextmanager
async def company_update_lock(bot_dir, company_id):
    while True:
        guard = company_lock(bot_dir, company_id)
        try:
            guard.__enter__()
            break
        except BlockingIOError:
            await asyncio.sleep(1)
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


class ReportQueue:
    def __init__(self, bot_dir):
        self.path = Path(bot_dir) / 'report_queue.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                    user_id TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, report_date TEXT,
                    download_url TEXT, message_id TEXT, note TEXT,
                    synced INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS items (
                    request_id TEXT NOT NULL, company_id TEXT NOT NULL,
                    version TEXT NOT NULL, snapshot TEXT NOT NULL,
                    PRIMARY KEY (request_id, company_id));
                CREATE TABLE IF NOT EXISTS reservations (
                    company_id TEXT PRIMARY KEY, request_id TEXT);
            ''')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _previous(db):
        rows = db.execute('''SELECT i.*, r.report_date FROM items i
            JOIN requests r ON r.id=i.request_id WHERE r.state='delivered'
            ORDER BY r.rowid''').fetchall()
        return {row['company_id']: dict(row) for row in rows}

    def reserve(self, companies, limit, user_id, event_key, rank):
        """Чтение последней выдачи, выбор и резервирование — одна транзакция."""
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM requests WHERE event_key=?', (event_key,)).fetchone():
                raise ValueError('Этот запрос уже принят; повторная выдача не выполняется')
            previous = self._previous(db)
            busy = {r[0] for r in db.execute('SELECT company_id FROM reservations WHERE request_id IS NOT NULL')}
            eligible = []
            for original in companies:
                company = dict(original)
                cid = str(company.get('id', ''))
                if not cid or cid in busy:
                    continue
                last = previous.get(cid)
                if last:
                    if data_version(company) == last['version']:
                        continue
                    company['report_date'] = last['report_date']
                    company['report_version'] = last['version']
                    company['data_version'] = data_version(original)
                eligible.append(company)
            selected = rank(eligible, limit)
            if not selected:
                return None
            rid = uuid.uuid4().hex
            db.execute('INSERT INTO requests(id,event_key,user_id,state,created_at) VALUES(?,?,?,?,?)',
                       (rid, event_key, str(user_id), 'reserved', now()))
            for company in selected:
                cid = str(company['id'])
                db.execute('INSERT INTO reservations VALUES(?,?) ON CONFLICT(company_id) '
                           'DO UPDATE SET request_id=excluded.request_id', (cid, rid))
                db.execute('INSERT INTO items VALUES(?,?,?,?)',
                           (rid, cid, data_version(company), json.dumps(company, ensure_ascii=False)))
            return dict(id=rid, companies=selected, previous={
                str(c['id']): json.loads(previous[str(c['id'])]['snapshot'])
                for c in selected if str(c['id']) in previous})

    def mark_sending(self, request_id, download_url):
        with self.transaction() as db:
            changed = db.execute("UPDATE requests SET state='sending',download_url=? "
                                 "WHERE id=? AND state='reserved'", (download_url, request_id)).rowcount
            if changed != 1:
                raise ValueError('Запрос не находится в состоянии подготовки')

    def delivered(self, request_id, message_id, report_date=None):
        if not message_id:
            raise ValueError('Для подтверждения выдачи нужен ID сообщения')
        with self.transaction() as db:
            changed = db.execute("UPDATE requests SET state='delivered',message_id=?,report_date=? "
                                 "WHERE id=? AND state='sending'",
                                 (str(message_id), report_date or now(), request_id)).rowcount
            if changed != 1:
                raise ValueError('Запрос не находится в состоянии отправки')
            db.execute('UPDATE reservations SET request_id=NULL WHERE request_id=?', (request_id,))

    def release(self, request_id, note, verified=False):
        """Автоматически освобождается только резерв до начала отправки ссылки."""
        with self.transaction() as db:
            states = ('reserved', 'sending') if verified else ('reserved',)
            row = db.execute('SELECT state FROM requests WHERE id=?', (request_id,)).fetchone()
            if not row or row['state'] not in states:
                return False
            db.execute("UPDATE requests SET state='failed',note=? WHERE id=?", (note, request_id))
            db.execute('UPDATE reservations SET request_id=NULL WHERE request_id=?', (request_id,))
            return True

    def unsynced(self):
        with self.transaction() as db:
            return [dict(r) for r in db.execute('''SELECT r.id, r.report_date,
                i.company_id, i.version FROM requests r JOIN items i ON i.request_id=r.id
                WHERE r.state='delivered' AND r.synced=0 ORDER BY r.rowid''')]

    def mark_synced(self, request_id):
        with self.transaction() as db:
            db.execute('UPDATE requests SET synced=1 WHERE id=?', (request_id,))

    def inspect(self):
        with self.transaction() as db:
            return [dict(r, company_ids=[i[0] for i in db.execute(
                'SELECT company_id FROM items WHERE request_id=? ORDER BY company_id', (r['id'],))])
                for r in db.execute('SELECT * FROM requests ORDER BY rowid')]


def main():
    parser = argparse.ArgumentParser(description='Проверка и восстановление общей очереди отчётов')
    parser.add_argument('--bot-dir', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    delivered = sub.add_parser('confirm-delivery')
    delivered.add_argument('request_id')
    delivered.add_argument('--message-id', required=True)
    delivered.add_argument('--report-date', required=True)
    release = sub.add_parser('release')
    release.add_argument('request_id')
    release.add_argument('--reason', required=True,
                         help='Подтверждение, что ссылка не доставлена и обработчик остановлен')
    args = parser.parse_args()
    queue = ReportQueue(args.bot_dir)
    if args.command == 'status':
        print(json.dumps(queue.inspect(), ensure_ascii=False, indent=2))
    elif args.command == 'confirm-delivery':
        datetime.strptime(args.report_date, '%Y-%m-%d %H:%M:%S')
        queue.delivered(args.request_id, args.message_id, args.report_date)
    elif not queue.release(args.request_id, args.reason, verified=True):
        parser.error('Запрос не найден или уже завершён')


if __name__ == '__main__':
    main()
