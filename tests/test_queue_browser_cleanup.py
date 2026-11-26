from types import SimpleNamespace
import pytest
from site_profiles.profile_queue import attempt_size


class Entry:
    def __init__(self, size=0, error=None):
        self.size, self.error = size, error

    def is_file(self):
        return True

    def is_symlink(self):
        return False

    def stat(self):
        if self.error:
            raise self.error
        return SimpleNamespace(st_size=self.size)


def test_browser_cleanup_does_not_kill_live_worker():
    folder = SimpleNamespace(rglob=lambda _: iter([
        Entry(10), Entry(error=FileNotFoundError()), Entry(20)]))
    assert attempt_size(folder) == 30


def test_other_io_errors_are_not_hidden():
    folder = SimpleNamespace(rglob=lambda _: iter([Entry(error=PermissionError())]))
    with pytest.raises(PermissionError):
        attempt_size(folder)
