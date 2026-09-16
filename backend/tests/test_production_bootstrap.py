"""R07 回归测试：正式部署默认不创建源码可推导密码的演示账号。

用独立的临时 SQLite 库 + 桩配置验证空库初始化分支，不触碰会话共享数据库。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import User
from app.models.base import Base
from app.services import auth_service


def _temp_session_factory(tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _stub_settings(**overrides) -> SimpleNamespace:
    values = {
        "AUTH_CREATE_DEMO_USERS": False,
        "AUTH_FORCE_DEMO_PASSWORD_CHANGE": True,
        "AUTH_BOOTSTRAP_ADMIN_USERNAME": "admin",
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _count_users(factory) -> int:
    with factory() as session:
        return int(session.scalar(select(func.count()).select_from(User)) or 0)


def _users(factory) -> list[User]:
    with factory() as session:
        return list(session.scalars(select(User).order_by(User.username)))


def test_production_default_creates_no_accounts(tmp_path, monkeypatch):
    """生产默认（不建演示账号、无初始口令）→ 一个账号都不创建。"""
    factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setattr(
        auth_service, "get_settings", lambda: _stub_settings()
    )
    auth_service.ensure_default_users()
    assert _count_users(factory) == 0


def test_bootstrap_admin_created_with_forced_password_change(tmp_path, monkeypatch):
    factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: _stub_settings(AUTH_BOOTSTRAP_ADMIN_PASSWORD="OneTime-Secret-2026"),
    )
    auth_service.ensure_default_users()

    users = _users(factory)
    assert len(users) == 1
    assert users[0].username == "admin"
    assert users[0].role == "super_admin"
    assert users[0].must_change_password is True
    assert auth_service.verify_password("OneTime-Secret-2026", users[0].password_hash)

    # 重复启动不覆盖、不重复创建
    auth_service.ensure_default_users()
    assert _count_users(factory) == 1


def test_placeholder_bootstrap_password_is_rejected(tmp_path, monkeypatch):
    """占位符/示例口令不得当成真实初始口令（否则等于公开默认口令）。"""
    factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: _stub_settings(AUTH_BOOTSTRAP_ADMIN_PASSWORD="replace-with-..."),
    )
    auth_service.ensure_default_users()
    assert _count_users(factory) == 0


def test_demo_mode_is_explicit_and_forces_password_change(tmp_path, monkeypatch):
    """演示模式必须显式开启；开启后创建的账号按开关强制改密。"""
    factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: _stub_settings(
            AUTH_CREATE_DEMO_USERS=True, AUTH_FORCE_DEMO_PASSWORD_CHANGE=True
        ),
    )
    auth_service.ensure_default_users()
    users = _users(factory)
    assert len(users) == len(auth_service.DEFAULT_USERS)
    assert all(user.must_change_password for user in users)


def test_demo_passwords_are_never_logged(tmp_path, monkeypatch, caplog):
    """演示账号创建日志不得出现任何口令。"""
    factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: _stub_settings(AUTH_CREATE_DEMO_USERS=True),
    )
    with caplog.at_level("WARNING"):
        auth_service.ensure_default_users()
    joined = "\n".join(record.getMessage() for record in caplog.records)
    for _, password, _, _ in auth_service.DEFAULT_USERS:
        assert password not in joined


def test_existing_database_is_never_modified(tmp_path, monkeypatch):
    """已有用户时直接返回：不新增、不重置既有账号（不碰真实账号）。"""
    factory = _temp_session_factory(tmp_path)
    monkeypatch.setattr(auth_service, "SessionLocal", factory)
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: _stub_settings(AUTH_BOOTSTRAP_ADMIN_PASSWORD="Another-Secret-2026"),
    )
    auth_service.ensure_default_users()
    before = {user.username: user.password_hash for user in _users(factory)}

    monkeypatch.setattr(
        auth_service, "get_settings", lambda: _stub_settings(AUTH_CREATE_DEMO_USERS=True)
    )
    auth_service.ensure_default_users()
    after = {user.username: user.password_hash for user in _users(factory)}
    assert before == after
