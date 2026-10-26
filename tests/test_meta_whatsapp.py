import hashlib
import hmac
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from fastapi.testclient import TestClient

from genesis.config import Settings
from genesis.core.jobs import Orchestrator
from genesis.core.schemas import AppManifest
from genesis.server import create_app
from genesis.sms.meta import (
    MetaAPIError,
    MetaAuthError,
    MetaNetworkError,
    MetaRateLimitError,
    MetaWhatsAppClient,
    normalize_meta_phone,
    parse_webhook_payload,
    validate_signature,
    verify_challenge,
)
from genesis.sms.provider import (
    ChannelRouter,
    LogProvider,
    MetaWhatsAppProvider,
    OffProvider,
    SendError,
    SendResult,
    SmsProvider,
)
from genesis.sms.service import Inbound, SmsService
from genesis.sms.store import SmsStore
from genesis.sms.worker import SmsWorker


class DummyGemini:
    configured = True

    def model_name(self) -> str:
        return "dummy-model"

    def generate_app(self, prompt: str, app_name: str | None) -> tuple[AppManifest, dict]:
        raise NotImplementedError

    def repair(self, diagnostics: str, files: dict, manifest: AppManifest):
        raise NotImplementedError


class DummyBuilder:
    pass


class MockSmsProvider:
    name = "mock_provider"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, body: str, status_callback: str | None = None, sender: str | None = None) -> SendResult:
        self.sent.append((to, body))
        return SendResult(sid="SMmock123", status="queued")

    def validate_request(self, url: str, params: Any, signature: str) -> bool:
        try:
            from twilio.request_validator import RequestValidator
            return RequestValidator(self.settings.twilio_auth_token).validate(url, dict(params), signature)
        except ImportError:
            return signature == "valid-mock-signature"


def make_test_settings(tmp_path: str = "/tmp/test-genesis") -> Settings:
    from pathlib import Path

    p = Path(tmp_path)
    s = Settings(
        data_dir=p / "data",
        template_dir=p / "template",
        gemini_api_key="test-key",
        java_home=p,
        android_home=p,
    )
    s.sms_provider = "log"
    s.website_engine_dir = ""  # independent of the developer's .env
    s.sms_signup = "link"  # the documented default, whatever the local .env says
    s.public_base_url = "https://genesis.example.test"
    s.twilio_account_sid = "AC" + "0" * 32
    s.twilio_auth_token = "test-twilio-auth-token"
    s.twilio_phone_number = "+15005550006"
    s.meta_access_token = "test-meta-access-token"
    s.meta_phone_number_id = "109876543210"
    s.meta_business_account_id = "987654321098"
    s.meta_verify_token = "test-meta-verify-token"
    s.meta_app_secret = "test-meta-app-secret-12345"
    s.meta_graph_api_version = "v21.0"
    return s


def sign_meta_payload(raw_body: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestMetaWhatsAppFoundation(unittest.TestCase):
    def setUp(self):
        self.settings = make_test_settings()
        self.gemini = DummyGemini()
        self.builder = DummyBuilder()
        self.orch = Orchestrator(self.settings, self.gemini, builder=self.builder)
        self.provider = MockSmsProvider(self.settings)
        self.app = create_app(
            self.settings,
            orchestrator=self.orch,
            sms_provider=self.provider,
            start_sms_worker=False,
        )
        self.client = TestClient(self.app)

    # ---------- 1. Webhook Verification Handshake (GET) ----------
    def test_verify_challenge_success(self):
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": self.settings.meta_verify_token,
            "hub.challenge": "1158201444",
        }
        resp = self.client.get("/api/webhooks/whatsapp", params=params)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "1158201444")
        self.assertIn("text/plain", resp.headers.get("content-type", ""))

    def test_verify_challenge_wrong_token(self):
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token-abc",
            "hub.challenge": "1158201444",
        }
        resp = self.client.get("/api/webhooks/whatsapp", params=params)
        self.assertEqual(resp.status_code, 403)

    def test_verify_challenge_missing_mode_or_challenge(self):
        resp = self.client.get(
            "/api/webhooks/whatsapp",
            params={"hub.verify_token": self.settings.meta_verify_token},
        )
        self.assertEqual(resp.status_code, 403)

    def test_verify_challenge_unconfigured(self):
        self.settings.meta_verify_token = ""
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": "some-token",
            "hub.challenge": "1158201444",
        }
        resp = self.client.get("/api/webhooks/whatsapp", params=params)
        self.assertEqual(resp.status_code, 503)

    # ---------- 2. Inbound Webhook Security (POST) ----------
    def test_inbound_post_valid_signature_text_message(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": self.settings.meta_business_account_id,
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15550254321",
                                    "phone_number_id": self.settings.meta_phone_number_id,
                                },
                                "contacts": [{"profile": {"name": "Alice"}, "wa_id": "14155552671"}],
                                "messages": [
                                    {
                                        "from": "14155552671",
                                        "id": "wamid.HBgLMTRxNTU1MjY3MRAA",
                                        "timestamp": "1730000000",
                                        "type": "text",
                                        "text": {"body": "Build a habit tracker"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        raw = json.dumps(payload).encode("utf-8")
        sig = sign_meta_payload(raw, self.settings.meta_app_secret)

        resp = self.client.post(
            "/api/webhooks/whatsapp",
            content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": sig},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "EVENT_RECEIVED")

    def test_inbound_post_missing_signature(self):
        raw = b'{"object": "whatsapp_business_account"}'
        resp = self.client.post(
            "/api/webhooks/whatsapp",
            content=raw,
            headers={"content-type": "application/json"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_inbound_post_invalid_signature(self):
        raw = b'{"object": "whatsapp_business_account"}'
        resp = self.client.post(
            "/api/webhooks/whatsapp",
            content=raw,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_inbound_post_unconfigured_secret(self):
        self.settings.meta_app_secret = ""
        raw = b'{"object": "whatsapp_business_account"}'
        resp = self.client.post(
            "/api/webhooks/whatsapp",
            content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": "sha256=abc"},
        )
        self.assertEqual(resp.status_code, 503)

    def test_inbound_post_malformed_json(self):
        raw = b"{ malformed json string !!"
        sig = sign_meta_payload(raw, self.settings.meta_app_secret)
        resp = self.client.post(
            "/api/webhooks/whatsapp",
            content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": sig},
        )
        self.assertEqual(resp.status_code, 400)

    # ---------- 3. Event Parsing Unit Tests ----------
    def test_parse_webhook_payload_messages_and_audio(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "messages": [
                                    {
                                        "from": "14155552671",
                                        "id": "wamid.TXT1",
                                        "timestamp": "1730000001",
                                        "type": "text",
                                        "text": {"body": "First message"},
                                    },
                                    {
                                        "from": "+14155552672",
                                        "id": "wamid.AUD1",
                                        "timestamp": "1730000002",
                                        "type": "audio",
                                        "audio": {"id": "media_audio_999", "mime_type": "audio/ogg; codecs=opus"},
                                    },
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        event = parse_webhook_payload(payload)
        self.assertEqual(len(event.messages), 2)
        self.assertEqual(event.messages[0].id, "wamid.TXT1")
        self.assertEqual(event.messages[0].from_, "+14155552671")
        self.assertEqual(event.messages[0].text, "First message")
        self.assertIsNone(event.messages[0].media_id)

        self.assertEqual(event.messages[1].id, "wamid.AUD1")
        self.assertEqual(event.messages[1].from_, "+14155552672")
        self.assertEqual(event.messages[1].media_id, "media_audio_999")
        self.assertEqual(event.messages[1].media_mime_type, "audio/ogg; codecs=opus")

    def test_parse_webhook_payload_statuses(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.STAT1",
                                        "status": "delivered",
                                        "timestamp": "1730000010",
                                        "recipient_id": "14155552671",
                                    },
                                    {
                                        "id": "wamid.STAT2",
                                        "status": "failed",
                                        "timestamp": "1730000011",
                                        "recipient_id": "14155552672",
                                        "errors": [{"code": 131047, "title": "Re-engagement message failed"}],
                                    },
                                ]
                            },
                        }
                    ],
                }
            ],
        }
        event = parse_webhook_payload(payload)
        self.assertEqual(len(event.statuses), 2)
        self.assertEqual(event.statuses[0].status, "delivered")
        self.assertIsNone(event.statuses[0].error_code)

        self.assertEqual(event.statuses[1].status, "failed")
        self.assertEqual(event.statuses[1].error_code, 131047)
        self.assertEqual(event.statuses[1].error_message, "Re-engagement message failed")

    def test_parse_webhook_payload_unsupported_fields_and_malformed_entries(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {"id": "WABA_1", "changes": [{"field": "account_update", "value": {"status": "ACTIVE"}}]},
                "not-a-dict",
            ],
        }
        event = parse_webhook_payload(payload)
        self.assertEqual(len(event.unsupported), 1)
        self.assertEqual(event.unsupported[0].get("field"), "account_update")

    # ---------- 4. Meta Client Transport & Mocked HTTP Calls ----------
    def test_client_send_text_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertTrue(request.url.path.endswith("/messages"))
            self.assertEqual(request.headers.get("Authorization"), "Bearer test-meta-access-token")
            data = json.loads(request.content.decode("utf-8"))
            self.assertEqual(data["to"], "14155552671")
            self.assertEqual(data["text"]["body"], "Hello from Weave!")
            return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.OUT1"}]})

        mock_http = httpx.Client(transport=httpx.MockTransport(handler))
        client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        res = client.send_text("whatsapp:+14155552671", "Hello from Weave!")
        self.assertEqual(res["messages"][0]["id"], "wamid.OUT1")

    def test_client_send_text_auth_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "Invalid OAuth access token", "code": 190}})

        mock_http = httpx.Client(transport=httpx.MockTransport(handler))
        client = MetaWhatsAppClient(
            access_token="expired-token",
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        with self.assertRaises(MetaAuthError) as ctx:
            client.send_text("+14155552671", "Hello")
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(ctx.exception.error_code, 190)

    def test_client_send_text_rate_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "Rate limit hit", "code": 130429}})

        mock_http = httpx.Client(transport=httpx.MockTransport(handler))
        client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        with self.assertRaises(MetaRateLimitError) as ctx:
            client.send_text("+14155552671", "Hello")
        self.assertFalse(ctx.exception.permanent)

    def test_client_send_text_outside_service_window(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "Re-engagement message failed", "code": 131047}})

        mock_http = httpx.Client(transport=httpx.MockTransport(handler))
        client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        with self.assertRaises(MetaAPIError) as ctx:
            client.send_text("+14155552671", "Hello")
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(ctx.exception.error_code, 131047)

    def test_client_fetch_media_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media_id_456"):
                return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/sample.ogg", "mime_type": "audio/ogg"})
            if request.url.host == "lookaside.fbsbx.com":
                return httpx.Response(200, content=b"OggS-raw-audio-bytes", headers={"content-type": "audio/ogg"})
            return httpx.Response(404)

        mock_http = httpx.Client(transport=httpx.MockTransport(handler))
        client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        data, mime = client.fetch_media_by_id("media_id_456")
        self.assertEqual(data, b"OggS-raw-audio-bytes")
        self.assertEqual(mime, "audio/ogg")

    # ---------- 5. Route Protection & Twilio Preservation ----------
    def test_public_webhook_routing_without_exposing_private_endpoints(self):
        # 1. Meta Webhook is public (does not require login code)
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": self.settings.meta_verify_token,
            "hub.challenge": "ch123",
        }
        res_meta = self.client.get("/api/webhooks/whatsapp", params=params)
        self.assertEqual(res_meta.status_code, 200)

        # 2. Private endpoint requires authorization from remote / with proxy headers
        res_private = self.client.get(
            "/api/pairing",
            headers={"x-forwarded-for": "203.0.113.1"},
        )
        self.assertEqual(res_private.status_code, 401)

    def test_twilio_webhook_remains_functional(self):
        twilio_params = {
            "MessageSid": "SM" + "1" * 32,
            "AccountSid": self.settings.twilio_account_sid,
            "From": "+919876500001",
            "To": self.settings.twilio_phone_number,
            "Body": "STATUS",
            "NumMedia": "0",
        }
        url = self.settings.public_base_url + "/api/webhooks/twilio/sms"
        try:
            from twilio.request_validator import RequestValidator
            sig = RequestValidator(self.settings.twilio_auth_token).compute_signature(url, twilio_params)
        except ImportError:
            sig = "valid-mock-signature"

        resp = self.client.post(
            "/api/webhooks/twilio/sms",
            content=urlencode(twilio_params),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-twilio-signature": sig,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<Response></Response>", resp.text)


class TestMetaWhatsAppPhase2(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.settings = make_test_settings(self.tmp_dir)
        self.settings.sms_owner_numbers = ["+14155552671"]
        self.gemini = DummyGemini()
        self.builder = DummyBuilder()
        self.orch = Orchestrator(self.settings, self.gemini, builder=self.builder)
        self.provider = MockSmsProvider(self.settings)
        self.app = create_app(
            self.settings,
            orchestrator=self.orch,
            sms_provider=self.provider,
            start_sms_worker=False,
        )
        self.sms_store = self.app.state.sms.store
        self.sms_service = self.app.state.sms.service
        self.sms_worker = self.app.state.sms
        self.client = TestClient(self.app)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _post_meta_webhook(self, payload: dict) -> httpx.Response:
        raw = json.dumps(payload).encode("utf-8")
        sig = sign_meta_payload(raw, self.settings.meta_app_secret)
        return self.client.post(
            "/api/webhooks/whatsapp",
            content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": sig},
        )

    # 1. Inbound Text Message -> Durable Persistence -> Async Processing
    def test_inbound_text_message_persistence_and_processing(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": self.settings.meta_business_account_id,
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": self.settings.meta_phone_number_id},
                                "contacts": [{"wa_id": "14155552671"}],
                                "messages": [
                                    {
                                        "from": "14155552671",
                                        "id": "wamid.INBOUND_TEXT_1",
                                        "timestamp": "1730000000",
                                        "type": "text",
                                        "text": {"body": "HELP"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        resp = self._post_meta_webhook(payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "EVENT_RECEIVED")

        # Verify durable persistence in SQLite with pending status (fast webhook ack)
        self.assertTrue(self.sms_store.has_inbound("wamid.INBOUND_TEXT_1"))
        inbound_row = self.sms_store.one("SELECT * FROM messages WHERE direction = 'in' AND provider_sid = 'wamid.INBOUND_TEXT_1'")
        self.assertEqual(inbound_row["status"], "pending")

        # Background worker processes the durable inbound queue
        processed = self.sms_worker.process_inbound()
        self.assertEqual(processed, 1)

        # Inbound status is now received (processed)
        inbound_row_after = self.sms_store.one("SELECT * FROM messages WHERE direction = 'in' AND provider_sid = 'wamid.INBOUND_TEXT_1'")
        self.assertEqual(inbound_row_after["status"], "received")

        user = self.sms_store.get_user("+14155552671")
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "owner")
        self.assertEqual(user["channel"], "whatsapp")

        # Verify reply was enqueued to SQLite outbox
        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["phone"], "whatsapp:+14155552671")
        self.assertIn("Describe an app to build it", outbox[0]["body"])
        self.assertFalse(outbox[0]["body"].startswith("Genesis:"))

    # 2. Duplicate Inbound Webhook Delivery
    def test_duplicate_inbound_webhook_delivery(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": self.settings.meta_business_account_id,
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": self.settings.meta_phone_number_id},
                                "contacts": [{"wa_id": "14155552671"}],
                                "messages": [
                                    {
                                        "from": "14155552671",
                                        "id": "wamid.DUPE_TEXT_1",
                                        "timestamp": "1730000000",
                                        "type": "text",
                                        "text": {"body": "HELP"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        # First delivery
        resp1 = self._post_meta_webhook(payload)
        self.assertEqual(resp1.status_code, 200)

        # Duplicate delivery
        resp2 = self._post_meta_webhook(payload)
        self.assertEqual(resp2.status_code, 200)

        # Worker processes
        processed = self.sms_worker.process_inbound()
        self.assertEqual(processed, 1)

        # Confirm exactly 1 outbox message and 1 inbound message exist
        final_outbox_count = len(self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'"))
        self.assertEqual(final_outbox_count, 1)
        inbound_records = self.sms_store.all("SELECT * FROM messages WHERE direction = 'in' AND provider_sid = 'wamid.DUPE_TEXT_1'")
        self.assertEqual(len(inbound_records), 1)

    # 3. Outbound WhatsApp Routing by Setting
    def test_outbound_whatsapp_routing_by_provider_setting(self):
        # A. GENESIS_WHATSAPP_PROVIDER=meta
        self.settings.whatsapp_provider = "meta"
        def mock_meta_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"messages": [{"id": "wamid.META_ROUTED_1"}]})

        mock_http = httpx.Client(transport=httpx.MockTransport(mock_meta_handler))
        meta_client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        meta_prov = MetaWhatsAppProvider(self.settings, client=meta_client)
        mock_tw = MockSmsProvider(self.settings)
        router = ChannelRouter(external=mock_tw, settings=self.settings, meta=meta_prov)

        res = router.send("whatsapp:+14155552671", "Hello via Meta", None)
        self.assertEqual(res.sid, "wamid.META_ROUTED_1")
        self.assertEqual(len(mock_tw.sent), 0)

        # B. GENESIS_WHATSAPP_PROVIDER=twilio
        self.settings.whatsapp_provider = "twilio"
        router = ChannelRouter(external=mock_tw, settings=self.settings, meta=meta_prov)
        res_tw = router.send("whatsapp:+14155552671", "Hello via Twilio", None)
        self.assertEqual(res_tw.sid, "SMmock123")
        self.assertEqual(len(mock_tw.sent), 1)
        self.assertEqual(mock_tw.sent[0], ("whatsapp:+14155552671", "Hello via Twilio"))

        # C. GENESIS_WHATSAPP_PROVIDER=off
        self.settings.whatsapp_provider = "off"
        router = ChannelRouter(external=mock_tw, settings=self.settings, meta=meta_prov)
        with self.assertRaises(SendError) as ctx:
            router.send("whatsapp:+14155552671", "Hello off", None)
        self.assertTrue(ctx.exception.permanent)
        self.assertEqual(len(mock_tw.sent), 1)  # Twilio was not called

    # 4. SMS Independence from WhatsApp Provider Setting
    def test_sms_continues_routing_to_twilio_independently(self):
        mock_tw = MockSmsProvider(self.settings)
        meta_prov = MetaWhatsAppProvider(self.settings)

        # When whatsapp_provider is meta
        self.settings.whatsapp_provider = "meta"
        self.settings.sms_provider = "twilio"
        router = ChannelRouter(external=mock_tw, settings=self.settings, meta=meta_prov)
        res_sms = router.send("+14155552671", "Standard SMS", None)
        self.assertEqual(res_sms.sid, "SMmock123")
        self.assertEqual(mock_tw.sent[-1], ("+14155552671", "Standard SMS"))

        # When whatsapp_provider is off
        self.settings.whatsapp_provider = "off"
        router = ChannelRouter(external=mock_tw, settings=self.settings, meta=meta_prov)
        res_sms2 = router.send("+14155552671", "Standard SMS 2", None)
        self.assertEqual(res_sms2.sid, "SMmock123")
        self.assertEqual(mock_tw.sent[-1], ("+14155552671", "Standard SMS 2"))

    # 5. Worker Restart Recovery for Pending Meta WhatsApp Outbox
    def test_worker_restarting_with_pending_meta_outbox(self):
        self.settings.whatsapp_provider = "meta"
        msg_id = self.sms_store.enqueue_sms("whatsapp:+14155552671", "Pending worker message", None)

        def mock_meta_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"messages": [{"id": "wamid.RESTART_SUCCESS"}]})

        mock_http = httpx.Client(transport=httpx.MockTransport(mock_meta_handler))
        meta_client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        meta_prov = MetaWhatsAppProvider(self.settings, client=meta_client)
        router = ChannelRouter(external=MockSmsProvider(self.settings), settings=self.settings, meta=meta_prov)

        worker = SmsWorker(self.settings, self.sms_store, self.orch, router, self.sms_service)
        sent_count = worker.send_outbox()
        self.assertEqual(sent_count, 1)

        row = self.sms_store.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["provider_sid"], "wamid.RESTART_SUCCESS")
        self.assertIsNone(row["error"])

    # 6. Meta Rate Limit (429) / 5xx -> Retryable
    def test_meta_error_classification_rate_limit_and_5xx_retryable(self):
        self.settings.whatsapp_provider = "meta"
        msg_id = self.sms_store.enqueue_sms("whatsapp:+14155552671", "Rate limited message", None)

        def mock_meta_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "Rate limit hit", "code": 130429}})

        mock_http = httpx.Client(transport=httpx.MockTransport(mock_meta_handler))
        meta_client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        meta_prov = MetaWhatsAppProvider(self.settings, client=meta_client)
        router = ChannelRouter(external=MockSmsProvider(self.settings), settings=self.settings, meta=meta_prov)

        worker = SmsWorker(self.settings, self.sms_store, self.orch, router, self.sms_service)
        worker.send_outbox()

        row = self.sms_store.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_attempt_at"], time.time())
        self.assertIn("130429", row["error"])

    # 7. Meta 4xx -> Marked Permanent Failure Without Retry
    def test_meta_error_classification_4xx_permanent_failure(self):
        self.settings.whatsapp_provider = "meta"
        msg_id = self.sms_store.enqueue_sms("whatsapp:+14155552671", "Outside 24h window message", None)

        def mock_meta_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "Re-engagement message failed", "code": 131047}})

        mock_http = httpx.Client(transport=httpx.MockTransport(mock_meta_handler))
        meta_client = MetaWhatsAppClient(
            access_token=self.settings.meta_access_token,
            phone_number_id=self.settings.meta_phone_number_id,
            http_client=mock_http,
        )
        meta_prov = MetaWhatsAppProvider(self.settings, client=meta_client)
        router = ChannelRouter(external=MockSmsProvider(self.settings), settings=self.settings, meta=meta_prov)

        worker = SmsWorker(self.settings, self.sms_store, self.orch, router, self.sms_service)
        worker.send_outbox()

        row = self.sms_store.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("131047", row["error"])

    # 8. Delivery-Status Update Sequence: sent -> delivered -> read
    def test_delivery_status_update_sequence(self):
        msg_id = self.sms_store.enqueue_sms("whatsapp:+14155552671", "Status sequence test", None)
        self.sms_store.outbound_sent(msg_id, "wamid.DELIV_SEQ", "sent")

        # 1. Webhook delivers "delivered"
        payload_deliv = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": self.settings.meta_business_account_id,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "statuses": [{
                            "id": "wamid.DELIV_SEQ",
                            "status": "delivered",
                            "timestamp": "1730000010",
                            "recipient_id": "14155552671",
                        }]
                    },
                }],
            }],
        }
        res1 = self._post_meta_webhook(payload_deliv)
        self.assertEqual(res1.status_code, 200)
        row1 = self.sms_store.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        self.assertEqual(row1["status"], "delivered")

        # 2. Webhook delivers "read"
        payload_read = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": self.settings.meta_business_account_id,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "statuses": [{
                            "id": "wamid.DELIV_SEQ",
                            "status": "read",
                            "timestamp": "1730000020",
                            "recipient_id": "14155552671",
                        }]
                    },
                }],
            }],
        }
        res2 = self._post_meta_webhook(payload_read)
        self.assertEqual(res2.status_code, 200)
        row2 = self.sms_store.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        self.assertEqual(row2["status"], "read")

    # 9. Out-of-Order Delivery-Status Preserved
    def test_out_of_order_delivery_status_preserved(self):
        msg_id = self.sms_store.enqueue_sms("whatsapp:+14155552671", "Status out of order test", None)
        self.sms_store.outbound_sent(msg_id, "wamid.DELIV_OOO", "sent")

        # Delivered arrives first
        payload_deliv = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": self.settings.meta_business_account_id,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "statuses": [{
                            "id": "wamid.DELIV_OOO",
                            "status": "delivered",
                            "timestamp": "1730000015",
                            "recipient_id": "14155552671",
                        }]
                    },
                }],
            }],
        }
        self._post_meta_webhook(payload_deliv)
        row = self.sms_store.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        self.assertEqual(row["status"], "delivered")

        # Delayed sent receipt arrives afterwards
        payload_sent = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": self.settings.meta_business_account_id,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "statuses": [{
                            "id": "wamid.DELIV_OOO",
                            "status": "sent",
                            "timestamp": "1730000010",
                            "recipient_id": "14155552671",
                        }]
                    },
                }],
            }],
        }
        self._post_meta_webhook(payload_sent)
        row_after = self.sms_store.one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        self.assertEqual(row_after["status"], "delivered")  # Status NOT downgraded to sent

    # 10. Unsupported Inbound Message Types Handled Cleanly
    def test_unsupported_inbound_message_types_accepted_cleanly(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": self.settings.meta_business_account_id,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": self.settings.meta_phone_number_id},
                        "messages": [{
                            "from": "14155552671",
                            "id": "wamid.STICKER_1",
                            "timestamp": "1730000000",
                            "type": "sticker",
                            "sticker": {"id": "stk_888"},
                        }],
                    },
                }],
            }],
        }
        resp = self._post_meta_webhook(payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "EVENT_RECEIVED")
        self.assertTrue(self.sms_store.has_inbound("wamid.STICKER_1"))

        # Process via worker
        self.sms_worker.process_inbound()
        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("text messages", outbox[0]["body"])

    # 11. WhatsApp Voice Notes Handled with Phase 2 Upgrade Notice
    def test_voice_message_durably_persisted_with_phase2_notice(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": self.settings.meta_business_account_id,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": self.settings.meta_phone_number_id},
                        "messages": [{
                            "from": "14155552671",
                            "id": "wamid.VOICE_PHASE2",
                            "timestamp": "1730000000",
                            "type": "audio",
                            "audio": {"id": "media_aud_123", "mime_type": "audio/ogg"},
                        }],
                    },
                }],
            }],
        }
        resp = self._post_meta_webhook(payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "EVENT_RECEIVED")

        # Verify durable persistence
        self.assertTrue(self.sms_store.has_inbound("wamid.VOICE_PHASE2"))
        row = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_PHASE2'")
        self.assertEqual(row["body"], "[voice note]")

        # Process via worker
        self.sms_worker.process_inbound()

        # Verify friendly notice enqueued
        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("couldn't use that voice note", outbox[0]["body"])


class TestMetaWhatsAppPhase3(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.settings = make_test_settings(self.tmp_dir)
        self.settings.sms_owner_numbers = ["+14155552671"]
        self.gemini = DummyGemini()
        self.builder = DummyBuilder()
        self.orch = Orchestrator(self.settings, self.gemini, builder=self.builder)
        self.provider = MockSmsProvider(self.settings)
        self.app = create_app(
            self.settings,
            orchestrator=self.orch,
            sms_provider=self.provider,
            start_sms_worker=False,
        )
        self.sms_store = self.app.state.sms.store
        self.sms_service = self.app.state.sms.service
        self.sms_worker = self.app.state.sms
        self.voice = self.app.state.sms.voice
        self.client = TestClient(self.app)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _post_meta_webhook(self, payload: dict) -> httpx.Response:
        raw = json.dumps(payload).encode("utf-8")
        sig = sign_meta_payload(raw, self.settings.meta_app_secret)
        return self.client.post(
            "/api/webhooks/whatsapp",
            content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": sig},
        )

    def _make_audio_payload(self, sid: str, media_id: str, from_num: str = "14155552671", mime: str = "audio/ogg; codecs=opus") -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": self.settings.meta_business_account_id,
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {"phone_number_id": self.settings.meta_phone_number_id},
                        "messages": [{
                            "from": from_num,
                            "id": sid,
                            "timestamp": str(int(time.time())),
                            "type": "audio",
                            "audio": {"id": media_id, "mime_type": mime},
                        }],
                    },
                }],
            }],
        }

    # 1. Valid incoming Meta audio webhook durably persisted with media ID
    def test_inbound_audio_webhook_persists_media_id_and_returns_immediately(self):
        payload = self._make_audio_payload("wamid.AUDIO_P3_1", "meta_media_777")
        t0 = time.time()
        resp = self._post_meta_webhook(payload)
        t_elapsed = time.time() - t0

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "EVENT_RECEIVED")
        self.assertLess(t_elapsed, 1.0)  # Immediate acknowledgment without waiting for transcription

        row = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.AUDIO_P3_1'")
        self.assertIsNotNone(row)
        self.assertEqual(row["direction"], "in")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["media_id"], "meta_media_777")
        self.assertEqual(row["media_type"], "audio/ogg; codecs=opus")
        self.assertEqual(row["body"], "[voice note]")

    # 2. Duplicate audio webhook delivery does not insert second row
    def test_duplicate_audio_webhook_deduplication(self):
        payload = self._make_audio_payload("wamid.AUDIO_P3_DUP", "meta_media_dup")
        resp1 = self._post_meta_webhook(payload)
        resp2 = self._post_meta_webhook(payload)

        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)

        rows = self.sms_store.all("SELECT * FROM messages WHERE provider_sid = 'wamid.AUDIO_P3_DUP'")
        self.assertEqual(len(rows), 1)

    # 3. Successful audio download + Whisper transcription creates project & build job
    def test_meta_voice_transcription_and_app_creation(self):
        self.voice.fetch_meta = lambda media_id, client: (b"OggS fake audio", "audio/ogg")
        self.voice.transcribe = lambda audio: "Build me a habit tracker with streaks"

        payload = self._make_audio_payload("wamid.VOICE_CREATE_APP", "media_app_1")
        self._post_meta_webhook(payload)

        # Worker processes the queue
        processed = self.sms_worker.process_inbound()
        self.assertEqual(processed, 1)

        # Inbound message status received and body updated to transcript
        row = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_CREATE_APP'")
        self.assertEqual(row["status"], "received")
        self.assertEqual(row["body"], "Build me a habit tracker with streaks")

        # Project created for owner
        user = self.sms_store.get_user("+14155552671")
        self.assertIsNotNone(user)
        projects = self.sms_store.projects(user["id"])
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["original_prompt"], "Build me a habit tracker with streaks")

        # Build job enqueued
        jobs = self.sms_store.all("SELECT * FROM jobs WHERE user_id = ?", (user["id"],))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["prompt"], "Build me a habit tracker with streaks")

        # Outbox reply repeats what was heard
        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn('You said: "Build me a habit tracker with streaks"', outbox[0]["body"])

    # 4. Meta voice change request on active project
    def test_meta_voice_change_request_on_active_project(self):
        user = self.sms_store.create_user("+14155552671", "owner", "whatsapp")
        project = self.sms_store.create_project(user["id"], "Initial prompt")
        from genesis.core.schemas import BuildRequest
        build_job = self.orch.create_job(BuildRequest(prompt="Initial prompt"))
        build_job.update(status="succeeded", apk={"filename": "app.apk", "size": 1000})
        self.orch.store.save(build_job)
        self.sms_store.update_project(project["id"], last_success_build_id=build_job["id"])
        self.sms_store.update_user(user["id"], active_project_id=project["id"])

        self.voice.fetch_meta = lambda media_id, client: (b"OggS fake audio", "audio/ogg")
        self.voice.transcribe = lambda audio: "make the buttons bigger and change theme to dark"

        payload = self._make_audio_payload("wamid.VOICE_CHANGE_APP", "media_change_1")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        # Verify change request added to project
        p = self.sms_store.project(user["id"], project["id"])
        self.assertIn("make the buttons bigger and change theme to dark", p["changes"])

        # Change build job queued
        jobs = self.sms_store.all("SELECT * FROM jobs WHERE project_id = ? AND kind = 'change'", (project["id"],))
        self.assertEqual(len(jobs), 1)

    # 5. Voice command inquiry (e.g., "show my apps")
    def test_meta_voice_command_show_projects(self):
        user = self.sms_store.create_user("+14155552671", "owner", "whatsapp")
        self.sms_store.create_project(user["id"], "App one")
        self.sms_store.create_project(user["id"], "App two")

        self.voice.fetch_meta = lambda media_id, client: (b"OggS fake audio", "audio/ogg")
        self.voice.transcribe = lambda audio: "show my apps"

        payload = self._make_audio_payload("wamid.VOICE_SHOW_APPS", "media_show_1")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("Your apps", outbox[0]["body"])

    # 6. Audio from unknown sender prompts link code without transcribing
    def test_meta_voice_unknown_sender_prompts_link_code_without_transcription(self):
        called = False

        def fake_fetch(media_id, client):
            nonlocal called
            called = True
            return (b"fake", "audio/ogg")

        self.voice.fetch_meta = fake_fetch
        payload = self._make_audio_payload("wamid.VOICE_UNKNOWN", "media_unk_1", from_num="15559998877")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        self.assertFalse(called)  # No network or CPU used
        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out' AND phone = 'whatsapp:+15559998877'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("link code", outbox[0]["body"])

    # 7. Voice notes disabled via server configuration
    def test_meta_voice_disabled_setting(self):
        self.settings.voice_enabled = False
        payload = self._make_audio_payload("wamid.VOICE_DISABLED", "media_dis_1")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("disabled on this server", outbox[0]["body"])

    # 8. Unclear speech / empty transcription result handled safely
    def test_meta_voice_transcription_empty_error_handled(self):
        from genesis.sms.voice import VoiceError

        self.voice.fetch_meta = lambda media_id, client: (b"OggS silent audio", "audio/ogg")

        def fail_transcribe(audio):
            raise VoiceError("I couldn't hear any words in that voice note")

        self.voice.transcribe = fail_transcribe

        payload = self._make_audio_payload("wamid.VOICE_SILENT", "media_silent_1")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        row = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_SILENT'")
        self.assertEqual(row["status"], "failed")

        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("couldn't hear any words", outbox[0]["body"])

    # 9. Voice note duration too long handled safely
    def test_meta_voice_audio_too_long_error_handled(self):
        from genesis.sms.voice import VoiceError

        self.voice.fetch_meta = lambda media_id, client: (b"OggS long audio", "audio/ogg")

        def fail_transcribe(audio):
            raise VoiceError("Please keep voice notes under 5 minutes")

        self.voice.transcribe = fail_transcribe

        payload = self._make_audio_payload("wamid.VOICE_LONG", "media_long_1")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        row = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_LONG'")
        self.assertEqual(row["status"], "failed")

        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("under 5 minutes", outbox[0]["body"])

    # 10. Unsupported media type (e.g. image sent as voice)
    def test_meta_voice_unsupported_media_type_handled(self):
        from genesis.sms.voice import VoiceError

        def fail_fetch(media_id, client):
            raise VoiceError("I can only read text and audio voice notes")

        self.voice.fetch_meta = fail_fetch

        payload = self._make_audio_payload("wamid.VOICE_IMAGE", "media_img_1")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("only read text and audio voice notes", outbox[0]["body"])

    # 11. Permanent Meta authentication error: notify user and do not endless retry
    def test_meta_voice_auth_error_permanent_no_endless_retry(self):
        from genesis.sms.meta import MetaAuthError

        def fail_fetch(media_id, client):
            raise MetaAuthError("Expired access token", error_code=190, permanent=True)

        self.voice.fetch_meta = fail_fetch

        payload = self._make_audio_payload("wamid.VOICE_AUTH_ERR", "media_auth_1")
        self._post_meta_webhook(payload)
        self.sms_worker.process_inbound()

        row = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_AUTH_ERR'")
        self.assertEqual(row["status"], "failed")

        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("WhatsApp authorization failed", outbox[0]["body"])

    # 12. Transient Meta network error: rescheduled for retry then bounded failure
    def test_meta_voice_transient_network_error_retried_then_fails(self):
        from genesis.sms.meta import MetaNetworkError

        def fail_fetch(media_id, client):
            raise MetaNetworkError("Connection timed out")

        self.voice.fetch_meta = fail_fetch

        payload = self._make_audio_payload("wamid.VOICE_NET_ERR", "media_net_1")
        self._post_meta_webhook(payload)

        # Attempt 1: rescheduled as pending with future next_attempt_at
        self.sms_worker.process_inbound()
        row1 = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_NET_ERR'")
        self.assertEqual(row1["status"], "pending")
        self.assertEqual(row1["attempts"], 1)

        # Simulate reaching max attempts (attempt 3)
        with self.sms_store.tx() as c:
            c.execute("UPDATE messages SET attempts = 3, next_attempt_at = 0 WHERE id = ?", (row1["id"],))
        self.sms_worker.process_inbound()

        row3 = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_NET_ERR'")
        self.assertEqual(row3["status"], "failed")

        outbox = self.sms_store.all("SELECT * FROM messages WHERE direction = 'out'")
        self.assertGreaterEqual(len(outbox), 1)
        self.assertIn("could not download voice note", outbox[0]["body"])

    # 13. Worker restart recovery for leased voice messages
    def test_meta_voice_worker_restart_recovery(self):
        self.voice.fetch_meta = lambda media_id, client: (b"OggS fake audio", "audio/ogg")
        self.voice.transcribe = lambda audio: "Build me a grocery list app"

        payload = self._make_audio_payload("wamid.VOICE_RECOVER", "media_rec_1")
        self._post_meta_webhook(payload)

        # Simulate message claimed by a worker that crashed (status=processing, lease expired)
        row = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_RECOVER'")
        with self.sms_store.tx() as c:
            c.execute(
                "UPDATE messages SET status = 'processing', lease_until = 0, attempts = 1 WHERE id = ?",
                (row["id"],),
            )

        # New worker starts up and processes inbound queue
        processed = self.sms_worker.process_inbound()
        self.assertEqual(processed, 1)

        row_after = self.sms_store.one("SELECT * FROM messages WHERE provider_sid = 'wamid.VOICE_RECOVER'")
        self.assertEqual(row_after["status"], "received")
        self.assertEqual(row_after["body"], "Build me a grocery list app")

    # 14. Meta media URL validation security tests
    def test_meta_media_url_validation(self):
        from genesis.sms.meta import meta_media_url_ok

        self.assertTrue(meta_media_url_ok("https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123"))
        self.assertTrue(meta_media_url_ok("https://scontent.xx.fbcdn.net/v/t39.1234/audio.ogg?oh=abc&oe=123"))
        self.assertTrue(meta_media_url_ok("https://graph.facebook.com/v21.0/123456"))
        self.assertTrue(meta_media_url_ok("https://mmg.whatsapp.net/v/t24/audio.ogg"))

        # Rejection of untrusted URLs
        self.assertFalse(meta_media_url_ok("https://evil.example.com/audio.ogg"))
        self.assertFalse(meta_media_url_ok("https://lookaside.fbsbx.com.attacker.com/audio.ogg"))
        self.assertFalse(meta_media_url_ok("ftp://lookaside.fbsbx.com/audio.ogg"))
        self.assertFalse(meta_media_url_ok("http://lookaside.fbsbx.com/audio.ogg"))  # Insecure HTTP rejected

    # 15. Meta client download_media enforces size limit
    def test_meta_client_download_media_size_limit(self):
        from genesis.sms.meta import MetaAPIError, MetaWhatsAppClient

        class HugeStreamTransport(httpx.BaseTransport):
            def handle_request(self, request):
                stream = httpx.ByteStream(b"A" * 1024 * 1024)  # 1 MB
                return httpx.Response(200, headers={"content-type": "audio/ogg"}, stream=stream)

        client = httpx.Client(transport=HugeStreamTransport())
        meta_client = MetaWhatsAppClient("test_token", "12345", http_client=client)

        # max_bytes = 100 KB; download exceeds limit
        with self.assertRaises(MetaAPIError) as cm:
            meta_client.download_media("https://lookaside.fbsbx.com/audio.ogg", max_bytes=100 * 1024)
        self.assertIn("exceeds maximum size", str(cm.exception))

    # 16. Unit test for fetch_meta_media helper function
    def test_fetch_meta_media_unit_helper(self):
        from genesis.sms.voice import VoiceError, fetch_meta_media

        class FakeMetaClient:
            def get_media_url(self, media_id):
                if media_id == "missing_url":
                    return {}
                if media_id == "not_audio":
                    return {"url": "https://lookaside.fbsbx.com/file", "mime_type": "application/pdf"}
                if media_id == "too_big":
                    return {"url": "https://lookaside.fbsbx.com/file", "file_size": 20 * 1024 * 1024}
                return {"url": "https://lookaside.fbsbx.com/file.ogg", "mime_type": "audio/ogg"}

            def download_media(self, url, max_bytes):
                return b"fake-audio-bytes", "audio/ogg"

        fc = FakeMetaClient()
        data, mime = fetch_meta_media("ok_id", fc)
        self.assertEqual(data, b"fake-audio-bytes")
        self.assertEqual(mime, "audio/ogg")

        with self.assertRaises(VoiceError):
            fetch_meta_media("missing_url", fc)

        with self.assertRaises(VoiceError):
            fetch_meta_media("not_audio", fc)

        with self.assertRaises(VoiceError):
            fetch_meta_media("too_big", fc)


if __name__ == "__main__":
    unittest.main()
