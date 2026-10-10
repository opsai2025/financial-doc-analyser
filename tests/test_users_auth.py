from datetime import timedelta

import pytest

from core.config import MAX_FAILED_LOGINS
from core.db import session_scope
from core.models import LoginTicket, Role, User, utcnow
from services import auth
from services.auth import LoginError, authenticate, change_password, ensure_bootstrap_admin
from services.users import (
    UserError,
    create_user,
    default_form,
    form_from_user,
    get_user,
    reset_password,
    unlock_user,
    update_user,
)


@pytest.fixture()
def admin(db):
    assert ensure_bootstrap_admin() is None
    with session_scope() as s:
        return s.query(User).filter_by(username="admin").one()


def make(admin, username, role=Role.ANALYST, **overrides):
    form = default_form(role)
    form.display_name = overrides.pop("display_name", "کاربر آزمایشی")
    for k, v in overrides.items():
        setattr(form, k, v)
    return create_user(admin.id, username, form)


def test_bootstrap_admin_created_once(admin):
    assert admin.role == Role.ADMIN and admin.must_change_password
    assert ensure_bootstrap_admin() is None
    with session_scope() as s:
        assert s.query(User).count() == 1


def test_login_and_forced_change(admin):
    res = authenticate("ADMIN ", "Admin1234")
    assert res.user is not None and res.user.must_change_password
    updated, errors = change_password(admin.id, "Admin1234", "NewPass123")
    assert errors == [] and updated is not None
    assert not updated.must_change_password
    assert updated.session_version == admin.session_version + 1
    assert authenticate("admin", "NewPass123").user is not None


def test_change_password_errors(admin):
    _, errors = change_password(admin.id, "wrong", "Admin1234")
    assert any("فعلی" in e for e in errors) and any("متفاوت" in e for e in errors)


def test_lockout_after_failed_logins(admin):
    for _ in range(MAX_FAILED_LOGINS - 1):
        assert authenticate("admin", "bad").error is LoginError.INVALID
    res = authenticate("admin", "bad")
    assert res.error is LoginError.LOCKED and res.locked_until is not None
    assert authenticate("admin", "Admin1234").error is LoginError.LOCKED  # even with correct password
    unlock_user(admin.id)
    assert authenticate("admin", "Admin1234").user is not None


def test_unknown_user_invalid(db):
    assert authenticate("nobody", "x").error is LoginError.INVALID


def test_create_analyst_defaults_and_duplicate(admin):
    user, temp = make(admin, "ali.r")
    assert user.role == Role.ANALYST and user.docs_per_day == 50 and user.guest_doc_cap is None
    assert authenticate("ali.r", temp).user.must_change_password
    with pytest.raises(UserError):
        make(admin, "ali.r")
    with pytest.raises(UserError):
        make(admin, "1bad")


def test_create_guest_requires_and_sets_expiry(admin):
    user, _ = make(admin, "guest1", Role.GUEST)
    assert user.guest_doc_cap == 5 and user.expires_at is not None and user.docs_per_day is None
    with pytest.raises(UserError):
        make(admin, "guest2", Role.GUEST, guest_valid_days=None)


def test_expired_guest_cannot_login(admin):
    user, temp = make(admin, "guest1", Role.GUEST)
    with session_scope() as s:
        s.get(User, user.id).expires_at = utcnow() - timedelta(minutes=1)
    assert authenticate("guest1", temp).error is LoginError.EXPIRED


def test_role_change_clears_irrelevant_quotas_and_revokes_sessions(admin):
    user, _ = make(admin, "sara")
    form = default_form(Role.VIEWER)
    form.display_name = "سارا"
    updated = update_user(admin.id, user.id, form)
    assert updated.role == Role.VIEWER and updated.docs_per_hour is None and updated.db_quota_mb is None
    assert updated.session_version == user.session_version + 1


def test_admin_cannot_demote_or_deactivate_self(admin):
    form = form_from_user(get_user(admin.id))
    form.is_active = False
    with pytest.raises(UserError):
        update_user(admin.id, admin.id, form)


def test_last_admin_protected(admin):
    other, _ = make(admin, "boss", Role.ADMIN)
    form = form_from_user(get_user(admin.id))
    form.role = Role.ANALYST
    form.docs_per_hour, form.docs_per_day, form.db_quota_mb, form.storage_quota_mb = 1, 2, 3, 4
    update_user(other.id, admin.id, form)  # allowed: another active admin remains
    form2 = form_from_user(get_user(other.id))
    form2.is_active = False
    with pytest.raises(UserError):
        update_user(admin.id, other.id, form2)


def test_deactivation_blocks_login_and_sessions(admin):
    user, temp = make(admin, "reza")
    token = auth.issue_session_token(auth.load_active_user(user.id))
    assert auth.user_from_session_token(token) is not None
    form = form_from_user(get_user(user.id))
    form.is_active = False
    update_user(admin.id, user.id, form)
    assert auth.user_from_session_token(token) is None
    assert authenticate("reza", temp).error is LoginError.INACTIVE


def test_reset_password_revokes_and_forces_change(admin):
    user, _ = make(admin, "nima")
    token = auth.issue_session_token(auth.load_active_user(user.id))
    temp = reset_password(admin.id, user.id)
    assert auth.user_from_session_token(token) is None
    res = authenticate("nima", temp)
    assert res.user is not None and res.user.must_change_password


def test_validation_messages(admin):
    with pytest.raises(UserError) as exc:
        make(admin, "valid.name", display_name="x", docs_per_hour=100, docs_per_day=10)
    msg = str(exc.value)
    assert "نام نمایشی" in msg and "ساعتی" in msg


def test_login_ticket_single_use(admin):
    user = auth.load_active_user(admin.id)
    ticket = auth.create_login_ticket(user)
    token, error = auth.redeem_login_ticket(ticket)
    assert error is None and auth.user_from_session_token(token).id == admin.id
    assert auth.redeem_login_ticket(ticket) == (None, auth.TicketError.UNKNOWN)
    assert auth.redeem_login_ticket("unknown") == (None, auth.TicketError.UNKNOWN)
    assert auth.redeem_login_ticket("") == (None, auth.TicketError.MISSING)
    with session_scope() as s:  # only a hash of the ticket is stored, and used rows are gone
        assert s.query(LoginTicket).count() == 0


def test_login_ticket_survives_module_reimport(admin):
    """D-11.2: a ticket made by one copy of services.auth is redeemable by another (Issue 1)."""
    import importlib.util

    spec = importlib.util.find_spec("services.auth")
    other = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other)  # a second, independent module object, as after a re-import
    ticket = other.create_login_ticket(auth.load_active_user(admin.id))
    token, error = auth.redeem_login_ticket(ticket)
    assert error is None and token


def test_login_ticket_expired_and_revoked(admin):
    user = auth.load_active_user(admin.id)
    ticket = auth.create_login_ticket(user)
    with session_scope() as s:
        s.query(LoginTicket).update({LoginTicket.expires_at: utcnow() - timedelta(seconds=1)})
    assert auth.redeem_login_ticket(ticket) == (None, auth.TicketError.EXPIRED)
    ticket = auth.create_login_ticket(user)
    with session_scope() as s:
        s.get(User, admin.id).session_version += 1  # e.g. password changed elsewhere
    assert auth.redeem_login_ticket(ticket) == (None, auth.TicketError.REVOKED)
