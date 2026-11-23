"""Advisory locks for local state shared by guard and operator processes."""
from contextlib import contextmanager
from functools import wraps
import os


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def inbox_transaction(root):
    def decorate(operation):
        @wraps(operation)
        def synchronized(*args, **kwargs):
            with file_lock(root() / "transactions.lock"):
                return operation(*args, **kwargs)
        return synchronized
    return decorate
