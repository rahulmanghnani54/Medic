"""Test fixtures.

`Settings` is a frozen dataclass and the module-level `settings` instance is used
as a default argument value, which is bound at import time. So the environment is
set here, at conftest import, before anything from `medic` is imported -- that is
the only point at which it still takes effect.
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="medic-tests-")
os.environ["MEDIC_UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["MEDIC_DATABASE_URL"] = f"sqlite:///{_TMP}/unused.sqlite3"

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from medic.models import Base  # noqa: E402


@pytest.fixture
def session() -> Iterator[Session]:
    """A fresh in-memory database per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    db = maker()
    try:
        yield db
        db.commit()
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def seeded(session: Session) -> Session:
    from medic.seed import seed

    seed(session)
    session.commit()
    return session


@pytest.fixture
def client(seeded: Session):
    """TestClient wired to the per-test database."""
    from fastapi.testclient import TestClient

    from medic.db import get_session
    from medic.main import app

    def override() -> Iterator[Session]:
        yield seeded

    app.dependency_overrides[get_session] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def headers() -> dict[str, dict[str, str]]:
    """Per-role auth headers for the dev token scheme."""
    from medic.config import settings

    return {role: {"X-Medic-Token": token} for role, token in settings.role_tokens.items()}
