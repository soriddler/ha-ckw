from ckw_client import kc


def test_password_page_error(fx):
    html = fx("kc_wrong_password.js")
    assert kc.page_id(html) == "login"
    typ, summary = kc.message(html)
    assert typ == "error" and "Invalid username or password" in summary
    assert "session_code=SESSION_CODE" in kc.login_action(html)


def test_sms_page(fx):
    html = fx("kc_after_password_sms_page.js")
    assert kc.page_id(html) == "login-sms"
    assert kc.message(html) is None
    assert kc.login_action(html).startswith("https://id.ckw.ch/realms/CKW-prod/login-actions/authenticate?")


def test_wrong_code_page_has_error_and_new_action(fx):
    html = fx("kc_wrong_sms_code.js")
    assert kc.page_id(html) == "login-sms"
    assert kc.message(html)[0] == "error"
    assert kc.login_action(html)


def test_without_session_code():
    url = "https://id.ckw.ch/x/authenticate?session_code=abc&execution=e1&client_id=dtp-web&tab_id=t&client_data=cd"
    out = kc.without_session_code(url)
    assert "session_code" not in out
    assert "execution=e1" in out and "client_data=cd" in out


def test_unrelated_html():
    assert kc.page_id("<html></html>") is None
    assert kc.login_action("<html></html>") is None
    assert kc.message("<html></html>") is None
