import pytest

from urja_api.portal import PortalAuthError, PortalError


def test_login_and_export_signed(portal, fake):
    assert len(portal.export()) == 2
    assert fake.logins == 1


def test_relogin_when_session_expires(portal, fake):
    portal.export()
    fake.valid = False  # portal drops our session
    assert len(portal.dts()) == 1
    assert fake.logins == 2


def test_retries_on_5xx_then_succeeds(portal, fake):
    portal.login()
    fake.fail_next = [503, 502]
    assert len(portal.dts()) == 1


def test_gives_up_after_retries(portal, fake):
    portal.login()
    fake.fail_next = [500] * 10
    with pytest.raises(PortalError):
        portal.dts()


def test_bad_credentials(settings, fake):
    import httpx
    from urja_api.portal import PortalClient
    settings.portal_password = "nope"
    c = PortalClient(settings, transport=httpx.MockTransport(fake), sleep=lambda s: None)
    with pytest.raises(PortalAuthError):
        c.login()


def test_429_backs_off_and_recovers(portal, fake):
    portal.login()
    fake.fail_next = [429, 429]
    assert len(portal.dts()) == 1
