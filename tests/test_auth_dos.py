"""S7 auth-DoS fixes.

scrypt (~200 ms, ~128 MiB) ran on the event loop through the login/register/
reset submit routes, and unbounded concurrent hashes could OOM the box. These
tests pin the three fixes: the scrypt-costing routes are plain `def` (FastAPI
threadpools them), scrypt is gated by a bounded semaphore, and a per-account
throttle stops a distributed attacker rotating IPs against one account.
"""

import inspect
import threading

import auth
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


def test_per_account_throttle_blocks_after_failures():
    auth._account_attempts.clear()
    email = "victim@example.com"
    for _ in range(auth._MAX_ACCOUNT_ATTEMPTS):
        auth._record_account_failure(email)
    assert auth._account_throttled(email)
    auth._clear_account_failures(email)
    assert not auth._account_throttled(email)


def test_per_account_throttle_is_independent_of_ip():
    """One account throttled by email is not throttled under a different email."""
    auth._account_attempts.clear()
    email = "victim@example.com"
    for _ in range(auth._MAX_ACCOUNT_ATTEMPTS):
        auth._record_account_failure(email)
    assert auth._account_throttled(email)
    assert not auth._account_throttled("someone-else@example.com")
