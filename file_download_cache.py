"""Постоянная сверка загрузок. Метаданные и прежние версии хранятся вне Documents."""
import hashlib
import json
import logging
from pathlib import Path
import shutil
from uuid import uuid4

log = logging.getLogger('file_download_cache')


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    temporary.write_bytes(content)
    temporary.replace(path)


class FileDownloadCache:
    def __init__(self, base_dir):
        self.root = Path(base_dir) / 'file_download_cache'

    def folder(self, save_dir):
        key = hashlib.sha256(str(Path(save_dir).resolve()).encode()).hexdigest()
        return self.root / key

    def metadata_path(self, save_dir, url):
        return self.folder(save_dir) / (hashlib.sha256(url.encode()).hexdigest() + '.json')

    def load(self, save_dir, url):
        try:
            data = json.loads(self.metadata_path(save_dir, url).read_text('utf-8'))
            return data if isinstance(data, dict) and data.get('url') == url else {}
        except (OSError, ValueError):
            return {}

    def save(self, save_dir, url, data):
        atomic_write(self.metadata_path(save_dir, url),
                     json.dumps(data, ensure_ascii=False, indent=2).encode())

    def entries(self, save_dir):
        for path in self.folder(save_dir).glob('*.json'):
            try:
                data = json.loads(path.read_text('utf-8'))
                if isinstance(data, dict):
                    yield data
            except (OSError, ValueError):
                log.warning('Повреждён кэш загрузки: %s', path)

    @staticmethod
    def local_path(save_dir, name):
        # Имя из HTTP/метаданных не может выйти из папки назначения.
        if not isinstance(name, str) or not name or Path(name).name != name or '\\' in name or name in {'.', '..'}:
            return None
        path = Path(save_dir) / name
        return None if path.is_symlink() else path

    def usable_path(self, save_dir, entry):
        for name_key, hash_key in [('filename', 'sha256'), ('converted_filename', 'converted_sha256')]:
            if name_key.startswith('converted') and entry.get('converted_source_sha256') != entry.get('sha256'):
                continue
            path = self.local_path(save_dir, entry.get(name_key))
            if path and path.is_file():
                if path.stat().st_size and file_sha256(path) == entry.get(hash_key):
                    return path
                if name_key == 'filename':
                    return None  # повреждённый оригинал нужно восстановить, даже если есть PDF
        return None

    def destination(self, save_dir, url, name, entry):
        if entry.get('filename'):
            return self.local_path(save_dir, entry['filename'])
        path = self.local_path(save_dir, name)
        if path is None:
            return None
        # Два разных адреса могут выдавать одинаковый Content-Disposition.
        convertible = {'.doc', '.docx', '.xls', '.xlsx', '.rtf', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
        output_name = path.with_suffix('.pdf').name if path.suffix.lower() in convertible else name
        owners = set()
        for other in self.entries(save_dir):
            other_name = other.get('filename', '')
            other_path = Path(other_name)
            other_output = (other_path.with_suffix('.pdf').name
                            if other_path.suffix.lower() in convertible else other_name)
            if name in (other_name, other.get('converted_filename')) or output_name == other_output:
                owners.add(other.get('url'))
        if owners - {url}:
            suffix = hashlib.sha256(url.encode()).hexdigest()[:16]
            path = path.with_name(path.stem + '_' + suffix + path.suffix)
        return path

    def archive(self, path):
        """До замены сохраняем прежние байты и время; ничего не удаляем."""
        path = Path(path)
        if path.is_file():
            target = self.root / 'history' / file_sha256(path) / path.name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)

    def conversion_entries(self, source):
        source = Path(source)
        digest = file_sha256(source)
        return [e for e in self.entries(source.parent)
                if e.get('filename') == source.name and e.get('sha256') == digest]

    def conversion_current(self, source, output):
        entries = self.conversion_entries(source)
        if not entries:
            return True  # прежний режим для файлов, загруженных до появления кэша
        return all(e.get('converted_source_sha256') == e.get('sha256')
                   and e.get('converted_sha256') == file_sha256(output) for e in entries)

    def record_conversion(self, source, output):
        for entry in self.conversion_entries(source):
            entry.update(converted_filename=Path(output).name,
                         converted_sha256=file_sha256(output),
                         converted_source_sha256=entry['sha256'])
            self.save(Path(source).parent, entry['url'], entry)
