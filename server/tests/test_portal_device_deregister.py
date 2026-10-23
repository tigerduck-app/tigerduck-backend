"""The portal's device deregister, run against the server's tables. In the
server suite for the reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.devices import deregister_devices  # noqa: E402
from server.auth.models import (  # noqa: E402
    ExternalAccount,
    ExternalAccountCredential,
    User,
    UserDevice,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_REQUEST = SimpleNamespace(state=SimpleNamespace(actor="ops@example.com"))


@pytest_asyncio.fixture(loop_scope="session")
async def portal_pool(test_settings, db_session):
    pool = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield pool
    await pool.close()


async def test_deregistering_the_last_device_drops_the_moodle_token(
    db_session, portal_pool
):
    """tigerduck-backend#85: an operator's deregister signs the device out,
    as the app's own does."""
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    account = ExternalAccount(user_id=user.id, provider="ntust_sso", external_user_id="b1")
    phone = UserDevice(user_id=user.id, client_device_id="iphone-1", platform="ios")
    mac = UserDevice(user_id=user.id, client_device_id="mac-1", platform="macos")
    db_session.add_all([account, phone, mac])
    await db_session.flush()
    db_session.add(
        ExternalAccountCredential(
            external_account_id=account.id,
            encryption_key_id="v1",
            ciphertext=b"c",
            nonce=b"n",
            aad="a",
        )
    )
    await db_session.commit()

    async def stored() -> int:
        return await portal_pool.fetchval("SELECT count(*) FROM external_account_credentials")

    await deregister_devices(_REQUEST, pool=portal_pool, body={"device_ids": [str(phone.id)]})
    assert await stored() == 1

    await deregister_devices(_REQUEST, pool=portal_pool, body={"device_ids": [str(mac.id)]})
    assert await stored() == 0
