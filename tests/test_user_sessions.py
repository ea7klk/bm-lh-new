from types import SimpleNamespace

from starlette.responses import Response

import bminfo.web as web


class FakeStore:
    def __init__(self, user):
        self.user = user
        self.calls = []

    def user_by_session(self, token_hash, inactivity_seconds):
        self.calls.append((token_hash, inactivity_seconds))
        return self.user


def _request(token="session-token"):
    return SimpleNamespace(
        cookies={"session_token": token} if token else {},
        state=SimpleNamespace(),
    )


def test_authenticated_session_is_touched_for_one_week(monkeypatch):
    store = FakeStore({"id": 7, "callsign": "EA7KLK"})
    monkeypatch.setattr(web, "get_store", lambda: store)
    request = _request()

    user = web._current_user(request)

    assert user["id"] == 7
    assert store.calls == [(web.session_token_hash("session-token"), 7 * 24 * 60 * 60)]
    assert request.state.user_session_valid is True


def test_authenticated_session_cookie_is_sliding():
    request = _request()
    request.state.user_session_checked = True
    request.state.user_session_valid = True
    response = Response()

    web._refresh_user_session_cookie(request, response)

    cookie = response.headers["set-cookie"]
    assert "session_token=session-token" in cookie
    assert "Max-Age=604800" in cookie
    assert "HttpOnly" in cookie


def test_expired_session_cookie_is_removed():
    request = _request()
    request.state.user_session_checked = True
    request.state.user_session_valid = False
    response = Response()

    web._refresh_user_session_cookie(request, response)

    cookie = response.headers["set-cookie"]
    assert 'session_token=""' in cookie
    assert "Max-Age=0" in cookie
