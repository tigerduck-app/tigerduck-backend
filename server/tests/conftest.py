"""Shared fixtures: isolated Postgres schema per test session + httpx AsyncClient."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from sqlalchemy.ext.asyncio import create_async_engine

from server.auth.crypto import CredentialCipher, build_credential_aad
from server.auth.models import ExternalAccount, ExternalAccountCredential, User
from server.bulletins.llm.openai_compat import OpenAICompatibleClient
from server.config import Settings
from server.course_announcements.classifier import (
    Classification,
    ClassifierError,
    ClassifierInfo,
    Post,
)
from server.db import Base, build_engine, build_session_factory
from server.main import create_app
from server.sync.models import UserAssignment
from server.syncjobs.moodle_client import FetchedSubmission
from server.syncjobs.policies import ensure_default_policies

# PostgreSQL identifiers cannot be passed as bind params in DDL, so we must
# interpolate. Enforce a strict whitelist instead to neutralise SQL injection
# if `db_name` ever becomes configurable.
_SAFE_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")

# Default admin database to connect to when issuing DROP/CREATE DATABASE
# against the test DB. PostgreSQL forbids DROPping the DB you are connected
# to, so we attach to a separate maintenance DB that we assume already
# exists on the dev instance.
_ADMIN_DB = "tigerduck"

REPO_ROOT = Path(__file__).resolve().parents[2]

# What production settings refuse to load without, for a test that builds them.
PRODUCTION_SECRETS = {
    "api_shared_secret": "test-shared-secret",
    "auth_jwt_secret": "test-jwt-secret-0123456789abcdef0123456789abcdef",
    "auth_refresh_hmac_key": "test-hmac-key-0123456789abcdef0123456789abcdef",
    "credential_keys": {"v1": base64.b64encode(b"0" * 32).decode()},
    "credential_active_key_id": "v1",
}


def _assert_safe_ident(db_name: str) -> None:
    if not _SAFE_IDENT.fullmatch(db_name):
        raise ValueError(f"Unsafe db identifier: {db_name!r}")


def _admin_url(settings: Settings) -> str:
    """Derive the admin-DB URL from test_settings so credentials stay in one place."""
    # `str(url)` masks passwords as `***`, which asyncpg then treats as the
    # literal password and fails with InvalidPasswordError. Render explicitly
    # without masking so the real credential reaches the driver.
    return make_url(settings.database_url).set(database=_ADMIN_DB).render_as_string(
        hide_password=False
    )


async def _drop_create_db(admin_url: str, db_name: str) -> None:
    _assert_safe_ident(db_name)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {db_name}")
        await conn.exec_driver_sql(f"CREATE DATABASE {db_name}")
        # Production's pooled connections plan a prepared statement generically
        # after its first runs, and some statements fail only then (an ON CONFLICT
        # whose predicate is a bound value). Tests plan that way from the start.
        await conn.exec_driver_sql(
            f"ALTER DATABASE {db_name} SET plan_cache_mode = force_generic_plan"
        )
    await admin.dispose()


async def _drop_db(admin_url: str, db_name: str) -> None:
    _assert_safe_ident(db_name)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {db_name}")
    await admin.dispose()


def _run_alembic(
    database_url: str, *args: str, timeout: float = 300
) -> subprocess.CompletedProcess[str]:
    """Alembic runs in a subprocess because `server/migrations/env.py`
    starts its own event loop."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "TIGERDUCK_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _alembic(database_url: str, *args: str) -> None:
    result = _run_alembic(database_url, *args)
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    # Override DB to the same dev Postgres, but drop+recreate schema per session.
    # The `*_tick_seconds=99999` overrides neuter the background APScheduler
    # jobs that ASGITransport starts via the lifespan handler — otherwise a
    # worker could mark test rows `sent` and make assertions flaky.
    #
    # Honor TIGERDUCK_TEST_DATABASE_URL so the same tests can run in a
    # CI/container setup where Postgres lives on a different host (e.g. the
    # compose `postgres` service) without editing this file.
    #
    # `metrics_port=0` and `admin_port=0` keep a test that runs the lifespan
    # from binding the fixed :9000 and :9001, where two concurrent test runs,
    # or anything else on the host already listening there, would collide.
    database_url = os.environ.get(
        "TIGERDUCK_TEST_DATABASE_URL",
        "postgresql+asyncpg://tigerduck:tigerduck@localhost:5432/tigerduck_test",
    )
    return Settings(
        env="development",
        database_url=database_url,
        apns_env="development",
        metrics_port=0,
        admin_port=0,
        sync_job_tick_seconds=99999,
        push_pipeline_tick_seconds=99999,
        assignment_reminder_scan_interval_seconds=99999,
        course_reminder_scan_interval_seconds=99999,
        bulletin_user_dispatch_interval_seconds=99999,
        session_reauth_scan_interval_seconds=99999,
        tls_pin_watch_interval_seconds=0,
        tls_pin_signing_key_path=None,
        link_check_tick_seconds=0,
        course_catalog_refresh_interval_minutes=0,
        api_shared_secret="",
        auth_jwt_secret="test-jwt-secret-0123456789abcdef0123456789abcdef",
        auth_refresh_hmac_key="test-hmac-key-0123456789abcdef0123456789abcdef",
        credential_keys={"v1": base64.b64encode(b"0" * 32).decode()},
        credential_active_key_id="v1",
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def prepared_engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    admin_url = _admin_url(test_settings)
    test_db = make_url(test_settings.database_url).database
    assert test_db is not None, "test_settings.database_url is missing a database name"

    await _drop_create_db(admin_url, test_db)
    engine = build_engine(test_settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()
    await _drop_db(admin_url, test_db)


@pytest_asyncio.fixture(loop_scope="session")
async def client(
    test_settings: Settings,
    prepared_engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    # Fresh tables for every test
    async with prepared_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    app = create_app(test_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # ASGITransport DOES invoke lifespan, so the app now owns its own
        # engine + session_factory. Swap in our prepared engine *after* the
        # transport context enters so our shared DB survives per-test cycles.
        app.state.engine = prepared_engine
        app.state.session_factory = build_session_factory(prepared_engine)
        # ASGITransport does not run the lifespan handler, so mirror its
        # sync-policy seeding here for e2e tests that expect defaults.
        async with app.state.session_factory() as seed_session:
            await ensure_default_policies(seed_session)
            await seed_session.commit()
        # Expose the app so tests can override app.state collaborators
        # (e.g. swap in a StaticMoodleVerifier for /v3 auth tests).
        ac.app = app
        yield ac


@pytest_asyncio.fixture(loop_scope="session")
async def admin_client(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    """The operator endpoints, as the admin listener serves them. Shares
    `client`'s app state, so a collaborator swapped on one is seen by both."""
    transport = ASGITransport(app=client.app.state.admin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.app = client.app
        yield ac


@pytest_asyncio.fixture(loop_scope="session")
async def migration_db_url(test_settings: Settings) -> AsyncIterator[str]:
    """An empty database of its own, for a test that runs the migrations
    over it the way a deploy does."""
    admin_url = _admin_url(test_settings)
    db_name = "tigerduck_test_migrations"
    await _drop_create_db(admin_url, db_name)
    yield (
        make_url(test_settings.database_url)
        .set(database=db_name)
        .render_as_string(hide_password=False)
    )
    await _drop_db(admin_url, db_name)


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(
    prepared_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    """Yield a per-test AsyncSession against a freshly reset schema.

    Used by unit tests that exercise DB helpers without going through the
    HTTP transport (e.g. custom-push targeting resolver).
    """
    async with prepared_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = build_session_factory(prepared_engine)
    async with factory() as s:
        yield s


@pytest_asyncio.fixture
async def make_assignment(db_session: AsyncSession):
    """Factory fixture for a minimal `UserAssignment` row.

    Field defaults mirror the existing ad hoc `UserAssignment(...)`
    construction in `test_syncjobs_assignments.py`. Calls that omit
    `user_id` share one lazily-created default `User` for the whole test,
    so assignments built across several calls in one test genuinely
    belong to the same user unless the test passes `user_id` explicitly
    to set up a second one.
    """
    default_user_id: dict[str, uuid.UUID] = {}

    async def _make(
        *,
        moodle_assignment_id: int,
        due_at: datetime | None = None,
        provider_is_submitted: bool = False,
        deleted_at: datetime | None = None,
        user_id: uuid.UUID | None = None,
        moodle_course_id: int = 7001,
        title: str | None = None,
    ) -> UserAssignment:
        if user_id is None:
            if "id" not in default_user_id:
                user = User()
                db_session.add(user)
                await db_session.flush()
                default_user_id["id"] = user.id
            user_id = default_user_id["id"]
        row = UserAssignment(
            user_id=user_id,
            moodle_course_id=moodle_course_id,
            moodle_assignment_id=moodle_assignment_id,
            title=title or f"Assignment {moodle_assignment_id}",
            due_at=due_at,
            provider_is_submitted=provider_is_submitted,
            deleted_at=deleted_at,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def store_moodle_token(db_session: AsyncSession, test_settings: Settings):
    """Factory: store a Moodle token for a user the way login does, for the
    push pipeline's pre-send check to read back with the test keys
    (`CredentialCipher.from_settings(test_settings)`)."""
    cipher = CredentialCipher.from_settings(test_settings)

    async def _store(
        user_id: uuid.UUID, *, token: str = "moodle-token", status: str = "active"
    ) -> ExternalAccount:
        account = ExternalAccount(
            user_id=user_id,
            provider="ntust_sso",
            external_user_id=f"student-{user_id}",
            credential_status=status,
        )
        db_session.add(account)
        await db_session.flush()
        blob = cipher.encrypt(
            {"token_cache": {"moodle_token": token}},
            build_credential_aad(account.id, "ntust_sso"),
        )
        db_session.add(
            ExternalAccountCredential(
                external_account_id=account.id,
                encryption_key_id=blob.key_id,
                ciphertext=blob.ciphertext,
                nonce=blob.nonce,
                aad=blob.aad,
            )
        )
        await db_session.flush()
        return account

    return _store


class SubmissionStatusStub:
    """Moodle as the push pipeline's pre-send check sees it
    (`SubmissionStatusChecker`).

    Each call plays the next step of `script`, the last one repeating: the
    fields of the FetchedSubmission to answer with, None to answer nothing,
    or an exception to raise. With no script every assignment is open.
    Records the ids and tokens it was asked with.
    """

    def __init__(self, *script: dict | BaseException | None) -> None:
        self.script = list(script) or [{"is_submitted": False, "submitted_at": None}]
        self.asked: list[int] = []
        self.tokens: list[str] = []

    async def check_submission_status(
        self, *, token: str, assignment_id: int, timeout_seconds: float
    ) -> FetchedSubmission | None:
        self.asked.append(assignment_id)
        self.tokens.append(token)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        if step is None:
            return None
        return FetchedSubmission(moodle_assignment_id=assignment_id, **step)


# The service's labels (tigerduck-app/announcement-classifier, labeling_guide.md).
_LABELS = (
    "exam_info",
    "exam_change",
    "exam_seating",
    "exam_review",
    "exam_prep",
    "grade_release",
    "assignment_new",
    "assignment_change",
    "assignment_note",
    "class_cancel",
    "class_change",
    "class_prep",
    "attendance",
    "materials",
    "grading_policy",
    "course_info",
    "grouping",
    "admin",
    "survey",
    "event",
)


class RecordingClassifier:
    """Test double: scores a post by its subject from `scores` (every label
    0 otherwise), converts it as the service does, and records the posts it
    was sent. Set `down` to make every call fail as an unreachable service,
    and put a subject in `unanswered` to get no result for its posts."""

    def __init__(
        self,
        scores: Mapping[str, Mapping[str, float]] | None = None,
        *,
        threshold: float = 0.35,
        release: str | None = "model-test",
    ) -> None:
        self.scores = dict(scores or {})
        self.threshold = threshold
        self.release = release
        self.down = False
        self.unanswered: set[str] = set()
        self.posts: list[Post] = []

    async def info(self) -> ClassifierInfo:
        if self.down:
            raise ClassifierError("ConnectError")
        return ClassifierInfo(threshold=self.threshold, release=self.release)

    async def classify(self, posts: Sequence[Post]) -> list[Classification | None]:
        if self.down:
            raise ClassifierError("ConnectError")
        self.posts += posts
        results: list[Classification | None] = []
        for post in posts:
            if post.subject in self.unanswered:
                results.append(None)
                continue
            scores = {label: 0.0 for label in _LABELS} | dict(self.scores.get(post.subject, {}))
            results.append(
                Classification(
                    labels=tuple(k for k, v in scores.items() if v >= self.threshold),
                    scores=scores,
                    text=f"{post.subject}\n\n{post.message}".strip(),
                )
            )
        return results


class ScriptedLLM(OpenAICompatibleClient):
    """The LLM client over an offline transport. Each request takes the next
    answer, the last repeating: a dict is sent as the JSON content, a str as
    the content itself, an int as that HTTP status. Records the requests'
    JSON bodies and never sleeps between retries."""

    def __init__(self, *answers: dict | str | int, model: str = "test-model") -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(answer, int):
                return httpx.Response(answer, json={"error": "scripted"})
            content = json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        async def no_sleep(seconds: float) -> None:
            pass

        super().__init__(
            base_url="http://llm.test/v1",
            api_key="sk-test",
            model=model,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            sleep=no_sleep,
        )
