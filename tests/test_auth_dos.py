"""S7 auth-DoS fixes.

scrypt (~200 ms, ~128 MiB) ran on the event loop through the login/register/
reset submit routes, and unbounded concurrent hashes could OOM the box. These
tests pin the two fixes: the scrypt-costing routes are plain `def` (FastAPI
threadpools them), and scrypt is gated by a bounded semaphore.
"""

import inspect
import threading

import security


def test_auth_submit_routes_are_sync():
    """The scrypt-costing submit routes must be `def`, not `async def`.

    `async def` runs on the single event loop; `def` is threadpool-offloaded.
    """
    import app as app_module

    for name in ("login_submit", "register_submit", "reset_submit"):
        handler = getattr(app_module, name)
        assert not inspect.iscoroutinefunction(handler), name


def test_scrypt_semaphore_is_bounded():
    """Concurrent scrypt hashes are gated by a bounded semaphore."""
    assert isinstance(security._scrypt_semaphore, threading.BoundedSemaphore)


def test_scrypt_roundtrip_still_works():
    hashed = security.hash_password("correct horse battery staple")
    assert security.verify_password("correct horse battery staple", hashed)
    assert not security.verify_password("wrong", hashed)
