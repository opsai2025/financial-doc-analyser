import pytest
from sqlalchemy import select

from core.db import session_scope
from core.models import ApiKey, Role
from services import providers as pv
from services.auth import ensure_bootstrap_admin
from services.users import create_user, default_form


@pytest.fixture()
def setup(db):
    ensure_bootstrap_admin()
    provider = pv.save_provider(None, pv.ProviderForm("درگاه آزمایشی", "mock", "mock://?delay=0"))
    m1 = pv.save_model(None, provider.id, pv.ModelForm("model-a", "مدل الف"))
    m2 = pv.save_model(None, provider.id, pv.ModelForm("model-b", "مدل ب", supports_temperature=False))
    k1 = pv.save_key(None, provider.id, pv.KeyForm("کلید ۱", "secret-key-0001"))
    k2 = pv.save_key(None, provider.id, pv.KeyForm("کلید ۲", "secret-key-0002"))

    def user(name, role):
        form = default_form(role)
        form.display_name = name
        return create_user(1, name, form)[0]

    return provider, m1, m2, k1, k2, user("ali", Role.ANALYST), user("gst", Role.GUEST), user("viw", Role.VIEWER)


def pairs(user_id):
    return {(c.model_id, k.id) for c in pv.usable_models(user_id) for k in c.keys}


def test_key_is_encrypted_and_masked(setup):
    _, _, _, k1, *_ = setup
    with session_scope() as db:
        row = db.get(ApiKey, k1.id)
        assert "secret-key-0001" not in row.key_encrypted and row.key_last4 == "0001"


def test_no_grant_no_access_admin_all(setup):
    provider, m1, m2, k1, k2, ali, gst, viw = setup
    assert pairs(ali.id) == set()
    assert pairs(1) == {(m1.id, k1.id), (m1.id, k2.id), (m2.id, k1.id), (m2.id, k2.id)}
    assert pv.usable_models(viw.id) == []


def test_role_default_and_user_overrides(setup):
    provider, m1, m2, k1, k2, ali, gst, viw = setup
    pv.set_grant(pv.Subject(role=Role.ANALYST), provider.id, "allow", [m1.id], None)
    assert pairs(ali.id) == {(m1.id, k1.id), (m1.id, k2.id)}
    pv.set_grant(pv.Subject(user_id=ali.id), provider.id, "deny", None, [k2.id])
    assert pairs(ali.id) == {(m1.id, k1.id)}
    pv.set_grant(pv.Subject(user_id=ali.id), provider.id, "allow", [m2.id], [k1.id])
    assert pairs(ali.id) == {(m1.id, k1.id), (m2.id, k1.id)}
    pv.clear_grant(pv.Subject(user_id=ali.id), provider.id, "deny")
    assert (m1.id, k2.id) in pairs(ali.id)
    assert pairs(gst.id) == set()  # guests have their own role defaults
    with pytest.raises(pv.ProviderError):
        pv.set_grant(pv.Subject(role=Role.ANALYST), provider.id, "deny", None, None)


def test_own_provider_private(setup):
    provider, m1, m2, k1, k2, ali, gst, viw = setup
    own = pv.save_provider(ali.id, pv.ProviderForm("شخصی", "mock", ""))
    model = pv.save_model(ali.id, own.id, pv.ModelForm("own-model"))
    pv.save_key(ali.id, own.id, pv.KeyForm("کلید من", "my-own-key-9999"))
    choices = pv.usable_models(ali.id)
    assert [c.own for c in choices if c.model_id == model.id] == [True]
    assert all(c.model_id != model.id for c in pv.usable_models(gst.id))
    with pytest.raises(pv.ProviderError):
        pv.save_model(gst.id, own.id, pv.ModelForm("hack"))  # not the owner
    with pytest.raises(pv.ProviderError):
        pv.set_grant(pv.Subject(role=Role.ANALYST), own.id, "allow", None, None)


def test_inactive_and_non_pdf_hidden(setup):
    provider, m1, m2, k1, k2, ali, gst, viw = setup
    pv.save_model(None, provider.id, pv.ModelForm("model-a", "مدل الف", supports_pdf=False), m1.id)
    pv.save_key(None, provider.id, pv.KeyForm("کلید ۲", is_active=False), k2.id)
    assert pairs(1) == {(m2.id, k1.id)}


def test_validation(setup):
    provider, *_ = setup
    with pytest.raises(pv.ProviderError):
        pv.save_provider(None, pv.ProviderForm("درگاه آزمایشی", "mock"))  # duplicate name
    with pytest.raises(pv.ProviderError):
        pv.save_provider(None, pv.ProviderForm("x", "gemini", "ftp://bad"))
    with pytest.raises(pv.ProviderError):
        pv.save_model(None, provider.id, pv.ModelForm("has space"))
    with pytest.raises(pv.ProviderError):
        pv.save_key(None, provider.id, pv.KeyForm("بدون کلید", ""))


def test_connection_and_runtime(setup):
    provider, m1, m2, k1, *_ = setup
    ok, message = pv.test_connection(m1.id, k1.id)
    assert ok and "OK" in message
    model, prov, keys = pv.runtime_keys(m2.id, [k1.id])
    assert keys[0].cfg.api_key == "secret-key-0001" and keys[0].cfg.send_temperature is False


def test_delete_provider_cascades(setup):
    provider, *_ = setup
    pv.delete_provider(None, provider.id)
    with session_scope() as db:
        assert db.scalars(select(ApiKey)).all() == []
