"""Round-trip and constraint tests for the Phase-1 identity tables."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.auth.models import (
    ExternalAccount,
    ExternalAccountCredential,
    User,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_user_account_credential_round_trip(db_session) -> None:
    user = User(student_id="B11015000")
    db_session.add(user)
    await db_session.flush()

    account = ExternalAccount(
        user_id=user.id,
        provider="ntust_sso",
        external_user_id="B11015000",
    )
    db_session.add(account)
    await db_session.flush()

    credential = ExternalAccountCredential(
        external_account_id=account.id,
        encryption_key_id="v1",
        ciphertext=b"\x01\x02",
        nonce=b"\x00" * 12,
        aad=f"external_account:{account.id}:provider:ntust_sso",
    )
    db_session.add(credential)
    await db_session.commit()

    row = (
        await db_session.execute(select(User).where(User.student_id == "B11015000"))
    ).scalar_one()
    assert row.status == "active"
    assert row.locale == "zh-Hant"
    assert row.deleted_at is None

    cred_row = await db_session.get(ExternalAccountCredential, account.id)
    assert cred_row is not None
    assert cred_row.encryption_algorithm == "AES-256-GCM"
    assert cred_row.version == 1


async def test_student_id_unique_only_among_non_deleted(db_session) -> None:
    old = User(student_id="B11015001", deleted_at=datetime.now(UTC))
    db_session.add(old)
    await db_session.commit()

    # Same student_id is allowed because the older row is soft-deleted.
    fresh = User(student_id="B11015001")
    db_session.add(fresh)
    await db_session.commit()

    # But a second *active* row must violate ux_users_student_id_active.
    dup = User(student_id="B11015001")
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_external_account_provider_uid_unique(db_session) -> None:
    u1 = User(student_id="B11015002")
    u2 = User(student_id="B11015003")
    db_session.add_all([u1, u2])
    await db_session.flush()

    db_session.add(
        ExternalAccount(
            user_id=u1.id, provider="ntust_sso", external_user_id="B11015002"
        )
    )
    await db_session.commit()

    db_session.add(
        ExternalAccount(
            user_id=u2.id, provider="ntust_sso", external_user_id="B11015002"
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
