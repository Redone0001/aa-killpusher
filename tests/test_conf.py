from django.core.checks import run_checks
from django.test import override_settings

from killpusher import __version__
from killpusher.clients import headers
from killpusher.conf import user_agent


@override_settings(SITE_URL="https://auth.example.org", ESI_USER_CONTACT_EMAIL="admin@example.org")
def test_uses_central_aa_settings_without_module_configuration():
    expected = f"aa-killpusher/{__version__} (https://auth.example.org; admin@example.org)"
    assert user_agent() == expected
    assert headers()["User-Agent"] == expected


@override_settings(SITE_URL="https://auth.example.org", ESI_USER_CONTACT_EMAIL=None)
def test_site_url_alone_is_sufficient():
    assert user_agent() == f"aa-killpusher/{__version__} (https://auth.example.org)"


@override_settings(SITE_URL="", ESI_USER_CONTACT_EMAIL="admin@example.org")
def test_contact_email_alone_is_sufficient():
    assert user_agent() == f"aa-killpusher/{__version__} (admin@example.org)"


@override_settings(SITE_URL=None, ESI_USER_CONTACT_EMAIL=None)
def test_fallback_identifies_the_project():
    assert "https://github.com/Redone0001/aa-killpusher" in user_agent()


@override_settings(KILLPUSHER_USER_AGENT="Legacy custom agent")
def test_explicit_legacy_override_still_works():
    assert user_agent() == "Legacy custom agent"


@override_settings(KILLPUSHER_USER_AGENT=" ", SITE_URL="https://auth.example.org")
def test_blank_legacy_override_uses_aa_settings():
    assert "https://auth.example.org" in user_agent()


@override_settings(
    SITE_URL="https://auth.example.org\r\n", ESI_USER_CONTACT_EMAIL=" admin@example.org "
)
def test_values_are_safe_for_http_headers():
    assert "\r" not in user_agent() and "\n" not in user_agent()
    assert "admin@example.org" in user_agent()


def test_no_module_user_agent_system_check(db):
    assert not any(issue.id.startswith("killpusher.") for issue in run_checks())
