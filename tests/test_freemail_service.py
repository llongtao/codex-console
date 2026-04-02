from src.services.freemail import FreemailService


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.responses:
            raise AssertionError(f"未准备响应: {method} {url}")
        return self.responses.pop(0)


def _service():
    service = FreemailService({
        "base_url": "https://mail.example.com",
        "admin_token": "admin-secret",
        "domain": "example.com",
    })
    return service


def test_get_verification_code_skips_last_used_mail_id_between_calls():
    service = _service()
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload=[
                {
                    "id": "mail-1",
                    "sender": "noreply@openai.com",
                    "subject": "OpenAI verification",
                    "verification_code": "111111",
                    "created_at": 1_700_000_000.0,
                }
            ]
        ),
        FakeResponse(
            payload=[
                {
                    "id": "mail-1",
                    "sender": "noreply@openai.com",
                    "subject": "OpenAI verification",
                    "verification_code": "111111",
                    "created_at": 1_700_000_000.0,
                },
                {
                    "id": "mail-2",
                    "sender": "noreply@openai.com",
                    "subject": "OpenAI verification",
                    "verification_code": "222222",
                    "created_at": 1_700_000_030.0,
                },
            ]
        ),
    ])
    service.http_client = fake_client

    code_1 = service.get_verification_code(email="reuse@example.com", timeout=1)
    code_2 = service.get_verification_code(email="reuse@example.com", timeout=1)

    assert code_1 == "111111"
    assert code_2 == "222222"


def test_get_verification_code_filters_old_mails_by_otp_sent_at():
    service = _service()
    otp_sent_at = 1_700_000_000.0
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload=[
                {
                    "id": "mail-old",
                    "sender": "noreply@openai.com",
                    "subject": "OpenAI verification",
                    "verification_code": "333333",
                    "created_at": otp_sent_at - 30,
                },
                {
                    "id": "mail-new",
                    "sender": "noreply@openai.com",
                    "subject": "OpenAI verification",
                    "verification_code": "444444",
                    "created_at": otp_sent_at + 5,
                },
            ]
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(
        email="filter@example.com",
        timeout=1,
        otp_sent_at=otp_sent_at,
    )

    assert code == "444444"


def test_get_verification_code_fetches_detail_when_summary_lacks_code():
    service = _service()
    otp_sent_at = 1_700_000_000.0
    fake_client = FakeHTTPClient([
        FakeResponse(
            payload=[
                {
                    "id": "mail-new",
                    "sender": "noreply@openai.com",
                    "subject": "OpenAI verification",
                    "preview": "",
                    "created_at": otp_sent_at + 5,
                }
            ]
        ),
        FakeResponse(
            payload={
                "id": "mail-new",
                "sender": "noreply@openai.com",
                "subject": "OpenAI verification",
                "content": "Your verification code is 555666",
                "created_at": otp_sent_at + 6,
            }
        ),
    ])
    service.http_client = fake_client

    code = service.get_verification_code(
        email="detail@example.com",
        timeout=1,
        otp_sent_at=otp_sent_at,
    )

    assert code == "555666"
    assert fake_client.calls[1]["url"].endswith("/api/email/mail-new")
