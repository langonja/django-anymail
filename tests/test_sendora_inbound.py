from base64 import encodebytes
from datetime import datetime, timezone
from textwrap import dedent
from unittest.mock import ANY

import requests
import responses
from django.test import override_settings, tag
from responses.matchers import header_matcher, request_kwargs_matcher

from anymail.exceptions import AnymailConfigurationError
from anymail.inbound import AnymailInboundMessage
from anymail.signals import AnymailInboundEvent
from anymail.webhooks.sendora import SendoraInboundWebhookView

from .test_sendora_webhooks import (
    TEST_INBOUND_SECRET,
    TEST_TRACKING_SECRET,
    SendoraWebhookTestCase,
)
from .utils import sample_image_content

INBOUND_MESSAGE_ID = "1b3d5f7a-9c1e-4b3d-8f7a-9c1e3b5d7f9a"
MESSAGE_URL = f"https://api.sendora.se/v1/inbound/{INBOUND_MESSAGE_ID}"
RAW_URL = f"{MESSAGE_URL}/raw"

# An inbound event for a webhook with inboundContent "reference",
# from https://sendora.se/docs/api/webhook-events
INBOUND_REFERENCE_EVENT = {
    "id": "6e8a0c2e-4a6c-4e8a-8c0e-2a4c6e8a0c2e",
    "attempt": 1,
    "event": "inbound",
    "inboundMessageId": INBOUND_MESSAGE_ID,
    "serverId": "0f8a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b2c",
    "streamId": "8a0c2e4a-6c8e-4a0c-8e4a-6c8e0a2c4e6a",
    "receivedAt": "2026-09-15T12:00:00.000Z",
    "envelopeRecipient": "support+order-1042@example.se",
    "mailboxHash": "order-1042",
    "sizeBytes": 18432,
    "attachmentCount": 1,
    "hasText": True,
    "hasHtml": True,
    "authentication": {
        "spf": "pass",
        "spfHelo": "pass",
        "dkim": "pass",
        "dmarc": "pass",
        "dmarcPolicy": None,
        "arc": "none",
        "checkedAt": "2026-09-15T12:00:00.000Z",
    },
    "parseIssue": None,
}

RAW_MIME = dedent("""\
    From: Anna Andersson <anna@example.com>
    To: support+order-1042@example.se
    Subject: A question about the invoice
    Date: Tue, 15 Sep 2026 11:59:58 +0000
    Message-ID: <20260915115958.a1b2@example.com>
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary=mixed

    --mixed
    Content-Type: multipart/alternative; boundary=alternative

    --alternative
    Content-Type: text/plain; charset=utf-8

    Hej! I have a question about the invoice for September.
    --alternative
    Content-Type: text/html; charset=utf-8

    <p>Hej! I have a question about the invoice for September.</p>
    --alternative--
    --mixed
    Content-Type: image/png
    Content-Disposition: attachment; filename="sample_image.png"
    Content-Transfer-Encoding: base64

    """).replace("\n", "\r\n").encode()


def raw_mime_with_attachment():
    image = encodebytes(sample_image_content()).replace(b"\n", b"\r\n")
    return RAW_MIME + image + b"--mixed--\r\n"


@tag("sendora")
@override_settings(
    ANYMAIL={
        "SENDORA_API_KEY": "sk_api_key",
        "SENDORA_INBOUND_SECRET": TEST_INBOUND_SECRET,
    }
)
class SendoraInboundTestCase(SendoraWebhookTestCase):
    def setUp(self):
        super().setUp()
        self.match_api_key = header_matcher({"Authorization": "Bearer sk_api_key"})

    def add_raw_response(self, body=None, status=200):
        responses.add(
            responses.GET,
            RAW_URL,
            content_type="message/rfc822",
            body=raw_mime_with_attachment() if body is None else body,
            status=status,
            match=[self.match_api_key, request_kwargs_matcher({"stream": True})],
        )

    def add_message_response(self, envelope):
        responses.add(
            responses.GET,
            MESSAGE_URL,
            json={
                "inboundMessageId": INBOUND_MESSAGE_ID,
                "envelopeRecipient": "support+order-1042@example.se",
                "envelope": envelope,
                # (and many other fields Anymail doesn't use)
            },
            match=[self.match_api_key],
        )

    def post_inbound(self, esp_event):
        response = self.client_post_signed(
            "/anymail/sendora/inbound/", esp_event, secret=TEST_INBOUND_SECRET
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(
            self.inbound_handler,
            sender=SendoraInboundWebhookView,
            event=ANY,
            esp_name="Sendora",
        )
        event = kwargs["event"]
        self.assertIsInstance(event, AnymailInboundEvent)
        self.assertEqual(event.esp_event, esp_event)
        self.assertIsInstance(event.message, AnymailInboundMessage)
        return event

    @responses.activate
    def test_inbound_reference(self):
        self.add_raw_response()
        self.add_message_response(envelope={"sender": "bounces@example.com"})
        event = self.post_inbound(INBOUND_REFERENCE_EVENT)

        self.assertEqual(event.event_type, "inbound")
        self.assertEqual(
            event.timestamp, datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(event.event_id, "6e8a0c2e-4a6c-4e8a-8c0e-2a4c6e8a0c2e")

        message = event.message
        self.assertEqual(message.envelope_sender, "bounces@example.com")
        self.assertEqual(message.envelope_recipient, "support+order-1042@example.se")
        self.assertEqual(message.from_email.display_name, "Anna Andersson")
        self.assertEqual(message.from_email.addr_spec, "anna@example.com")
        self.assertEqual(
            [str(email) for email in message.to], ["support+order-1042@example.se"]
        )
        self.assertEqual(message.subject, "A question about the invoice")
        self.assertEqual(
            message.text, "Hej! I have a question about the invoice for September."
        )
        self.assertEqual(
            message.html,
            "<p>Hej! I have a question about the invoice for September.</p>",
        )
        self.assertEqual(message["Message-ID"], "<20260915115958.a1b2@example.com>")
        attachments = message.attachments
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "sample_image.png")
        self.assertEqual(attachments[0].get_content_type(), "image/png")
        self.assertEqual(attachments[0].get_content_bytes(), sample_image_content())

    @responses.activate
    def test_inbound_full(self):
        # A webhook with inboundContent "full" includes the envelope (and parsed
        # content, but no attachment content), so only the raw message is fetched
        self.add_raw_response()
        esp_event = {
            **INBOUND_REFERENCE_EVENT,
            "envelope": {"sender": "anna@example.com"},
            "from": {"address": "anna@example.com", "name": "Anna Andersson"},
            "subject": "A question about the invoice",
            # ...
        }
        event = self.post_inbound(esp_event)
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(event.message.envelope_sender, "anna@example.com")
        self.assertEqual(event.message.subject, "A question about the invoice")

    @responses.activate
    def test_inbound_bounce(self):
        # A bounce has a null envelope sender
        self.add_raw_response()
        esp_event = {**INBOUND_REFERENCE_EVENT, "envelope": {"sender": None}}
        event = self.post_inbound(esp_event)
        self.assertIsNone(event.message.envelope_sender)

    @responses.activate
    def test_content_expired(self):
        # Sendora answers 410 once it no longer keeps the message
        self.add_raw_response(
            status=410,
            body=b'{"error": "content_expired", "message": "The content is gone."}',
        )
        with self.assertRaises(requests.HTTPError):
            self.client_post_signed(
                "/anymail/sendora/inbound/",
                INBOUND_REFERENCE_EVENT,
                secret=TEST_INBOUND_SECRET,
            )

    def test_invalid_signature(self):
        response = self.client_post_signed(
            "/anymail/sendora/inbound/",
            INBOUND_REFERENCE_EVENT,
            secret=TEST_TRACKING_SECRET,
        )
        self.assertEqual(response.status_code, 400)

    def test_misconfigured_tracking(self):
        errmsg = (
            "You seem to have set a Sendora webhook that receives events"
            " other than `inbound` to Anymail's Sendora *inbound* webhook URL."
        )
        with self.assertRaisesMessage(AnymailConfigurationError, errmsg):
            self.client_post_signed(
                "/anymail/sendora/inbound/",
                {"id": "9b2f4c1e-7a3d-4e5f-8c6b-1d2e3f4a5b6c", "event": "delivered"},
                secret=TEST_INBOUND_SECRET,
            )

    @override_settings(ANYMAIL={"SENDORA_INBOUND_SECRET": TEST_INBOUND_SECRET})
    def test_requires_api_key(self):
        with self.assertRaisesMessage(AnymailConfigurationError, "SENDORA_API_KEY"):
            self.client_post_signed(
                "/anymail/sendora/inbound/",
                INBOUND_REFERENCE_EVENT,
                secret=TEST_INBOUND_SECRET,
            )

    @override_settings(ANYMAIL={"SENDORA_API_KEY": "sk_api_key"})
    def test_requires_inbound_secret(self):
        with self.assertRaisesMessage(
            AnymailConfigurationError, "SENDORA_INBOUND_SECRET"
        ):
            self.client_post_signed(
                "/anymail/sendora/inbound/",
                INBOUND_REFERENCE_EVENT,
                secret=TEST_INBOUND_SECRET,
            )

    @responses.activate
    @override_settings(
        ANYMAIL={
            "SENDORA_API_KEY": "sk_api_key",
            "SENDORA_API_URL": "https://api.staging.example.com/v1",
            "SENDORA_INBOUND_SECRET": TEST_INBOUND_SECRET,
        }
    )
    def test_api_url_setting(self):
        responses.add(
            responses.GET,
            f"https://api.staging.example.com/v1/inbound/{INBOUND_MESSAGE_ID}/raw",
            content_type="message/rfc822",
            body=raw_mime_with_attachment(),
        )
        esp_event = {**INBOUND_REFERENCE_EVENT, "envelope": None}
        event = self.post_inbound(esp_event)
        self.assertIsNone(event.message.envelope_sender)

    def test_set_options_in_view_params(self):
        view = SendoraInboundWebhookView.as_view(
            api_url="https://api.staging.example.com/v1",
            api_key="sk_special_key",
            inbound_secret="whsec_custom",
        )
        view_instance = view.view_class(**view.view_initkwargs)
        self.assertEqual(view_instance.api_url, "https://api.staging.example.com/v1/")
        self.assertEqual(view_instance.api_key, "sk_special_key")
        self.assertEqual(view_instance.signing_secret, b"whsec_custom")
