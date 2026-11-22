import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from unittest.mock import ANY

from django.test import override_settings, tag

from anymail.exceptions import AnymailConfigurationError, AnymailInsecureWebhookWarning
from anymail.signals import AnymailTrackingEvent
from anymail.webhooks.sendora import SendoraTrackingWebhookView

from .webhook_cases import WebhookTestCase

TEST_TRACKING_SECRET = "whsec_test_tracking_secret"
TEST_INBOUND_SECRET = "whsec_test_inbound_secret"


def sendora_signature(body, secret, timestamp):
    """Return a Sendora webhook signature of body (bytes) at timestamp"""
    # https://sendora.se/docs/webhooks#verify-the-signature
    return hmac.new(
        key=secret.encode(),
        msg=f"{timestamp}.".encode() + body,
        digestmod=hashlib.sha256,
    ).hexdigest()


class SendoraWebhookTestCase(WebhookTestCase):
    def client_post_signed(
        self, url, json_data, secret=TEST_TRACKING_SECRET, timestamp=None
    ):
        """Return self.client.post(url, serialized json_data) signed with secret"""
        body = json.dumps(json_data).encode()
        if timestamp is None:
            timestamp = int(time.time())
        signature = sendora_signature(body, secret, timestamp)
        return self.client.post(
            url,
            content_type="application/json",
            data=body,
            headers={"Sendora-Signature": f"t={timestamp},v1={signature}"},
        )


# A delivered event, from https://sendora.se/docs/api/webhook-events
DELIVERED_EVENT = {
    "id": "9b2f4c1e-7a3d-4e5f-8c6b-1d2e3f4a5b6c",
    "attempt": 1,
    "messageId": "5f432ffd-c005-499b-aa3a-5b8a088ea20e",
    "recipient": "anna@example.com",
    "occurredAt": "2026-09-15T12:00:03.000Z",
    "serverId": "0f8a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b2c",
    "streamId": "3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "broadcastId": None,
    "tag": "invoice",
    "metadata": {"invoiceId": "2026-0912"},
    "test": False,
    "event": "delivered",
    "details": {"code": 250, "response": "2.0.0 OK"},
}


@tag("sendora")
@override_settings(ANYMAIL={"SENDORA_TRACKING_SECRET": TEST_TRACKING_SECRET})
class SendoraWebhookSecurityTestCase(SendoraWebhookTestCase):
    def post_with_signature_header(self, signature_header, body=b"{}"):
        return self.client.post(
            "/anymail/sendora/tracking/",
            content_type="application/json",
            data=body,
            headers={"Sendora-Signature": signature_header},
        )

    def test_valid_signature(self):
        response = self.client_post_signed(
            "/anymail/sendora/tracking/", DELIVERED_EVENT
        )
        self.assertEqual(response.status_code, 200)

    def test_any_valid_signature(self):
        # While a webhook's secret is rolled, Sendora signs with both secrets
        body = json.dumps(DELIVERED_EVENT).encode()
        timestamp = int(time.time())
        old_signature = sendora_signature(body, "whsec_old_secret", timestamp)
        new_signature = sendora_signature(body, TEST_TRACKING_SECRET, timestamp)
        response = self.post_with_signature_header(
            f"t={timestamp},v1={old_signature},v1={new_signature.upper()}", body
        )
        self.assertEqual(response.status_code, 200)

    def test_invalid_signature(self):
        response = self.client_post_signed(
            "/anymail/sendora/tracking/", DELIVERED_EVENT, secret="whsec_wrong"
        )
        self.assertEqual(response.status_code, 400)

    def test_modified_body(self):
        body = json.dumps(DELIVERED_EVENT).encode()
        timestamp = int(time.time())
        signature = sendora_signature(body, TEST_TRACKING_SECRET, timestamp)
        response = self.post_with_signature_header(
            f"t={timestamp},v1={signature}", body.replace(b"anna", b"eric")
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_signature(self):
        response = self.client.post(
            "/anymail/sendora/tracking/",
            content_type="application/json",
            data=DELIVERED_EVENT,
        )
        self.assertEqual(response.status_code, 400)

    def test_malformed_signature(self):
        body = json.dumps(DELIVERED_EVENT).encode()
        timestamp = int(time.time())
        signature = sendora_signature(body, TEST_TRACKING_SECRET, timestamp)
        for header in [
            f"v1={signature}",  # missing timestamp
            f"t={timestamp}",  # missing signature
            f"t=-{timestamp},v1={signature}",
            f"t={timestamp}.0,v1={signature}",
            f"t={'9' * 19},v1={signature}",
        ]:
            with self.subTest(header=header):
                response = self.post_with_signature_header(header, body)
                self.assertEqual(response.status_code, 400)

    def test_stale_signature(self):
        response = self.client_post_signed(
            "/anymail/sendora/tracking/",
            DELIVERED_EVENT,
            timestamp=int(time.time()) - 301,
        )
        self.assertEqual(response.status_code, 400)

    def test_future_signature(self):
        response = self.client_post_signed(
            "/anymail/sendora/tracking/",
            DELIVERED_EVENT,
            timestamp=int(time.time()) + 301,
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(
        ANYMAIL={
            "SENDORA_TRACKING_SECRET": TEST_TRACKING_SECRET,
            # Basic auth for other ESPs' webhooks:
            "WEBHOOK_SECRET": "username:password",
        }
    )
    def test_ignores_shared_webhook_secret(self):
        # Sendora doesn't allow credentials in a webhook's url,
        # so its webhooks don't use Anymail's basic auth
        self.clear_basic_auth()
        with self.assertDoesNotWarn(AnymailInsecureWebhookWarning):
            response = self.client_post_signed(
                "/anymail/sendora/tracking/", DELIVERED_EVENT
            )
        self.assertEqual(response.status_code, 200)

    @override_settings(ANYMAIL={})
    def test_requires_tracking_secret(self):
        with self.assertRaisesMessage(
            AnymailConfigurationError, "SENDORA_TRACKING_SECRET"
        ):
            self.client_post_signed("/anymail/sendora/tracking/", DELIVERED_EVENT)

    @override_settings(
        ANYMAIL={
            "SENDORA_TRACKING_SECRET": TEST_TRACKING_SECRET,
            "SENDORA_INBOUND_SECRET": TEST_INBOUND_SECRET,
        }
    )
    def test_inbound_secret_not_accepted_for_tracking(self):
        response = self.client_post_signed(
            "/anymail/sendora/tracking/", DELIVERED_EVENT, secret=TEST_INBOUND_SECRET
        )
        self.assertEqual(response.status_code, 400)

    def test_set_secret_in_view_param(self):
        view = SendoraTrackingWebhookView.as_view(tracking_secret="whsec_custom")
        view_instance = view.view_class(**view.view_initkwargs)
        self.assertEqual(view_instance.signing_secret, b"whsec_custom")


@tag("sendora")
@override_settings(ANYMAIL={"SENDORA_TRACKING_SECRET": TEST_TRACKING_SECRET})
class SendoraTrackingTestCase(SendoraWebhookTestCase):
    def post_event(self, esp_event):
        response = self.client_post_signed("/anymail/sendora/tracking/", esp_event)
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(
            self.tracking_handler,
            sender=SendoraTrackingWebhookView,
            event=ANY,
            esp_name="Sendora",
        )
        event = kwargs["event"]
        self.assertIsInstance(event, AnymailTrackingEvent)
        self.assertEqual(event.esp_event, esp_event)
        return event

    def test_delivered_event(self):
        event = self.post_event(DELIVERED_EVENT)
        self.assertEqual(event.event_type, "delivered")
        self.assertEqual(
            event.timestamp, datetime(2026, 9, 15, 12, 0, 3, tzinfo=timezone.utc)
        )
        self.assertEqual(event.event_id, "9b2f4c1e-7a3d-4e5f-8c6b-1d2e3f4a5b6c")
        self.assertEqual(event.message_id, "5f432ffd-c005-499b-aa3a-5b8a088ea20e")
        self.assertEqual(event.recipient, "anna@example.com")
        self.assertEqual(event.mta_response, "250 2.0.0 OK")
        self.assertEqual(event.tags, ["invoice"])
        self.assertEqual(event.metadata, {"invoiceId": "2026-0912"})
        self.assertIsNone(event.reject_reason)
        self.assertIsNone(event.description)
        self.assertIsNone(event.click_url)

    def test_hard_bounced_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "bounced",
                "details": {
                    "code": 550,
                    "response": "5.1.1 The email account that you tried to reach"
                    " does not exist.",
                    "classification": "InvalidRecipient",
                    "hard": True,
                    "expired": False,
                },
            }
        )
        self.assertEqual(event.event_type, "bounced")
        self.assertEqual(event.reject_reason, "bounced")
        self.assertEqual(event.description, "InvalidRecipient")
        self.assertEqual(
            event.mta_response,
            "550 5.1.1 The email account that you tried to reach does not exist.",
        )

    def test_soft_bounced_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "bounced",
                "details": {
                    "code": 552,
                    "response": "5.2.2 Mailbox full",
                    "classification": "QuotaIssues",
                    "hard": False,
                    "expired": False,
                },
            }
        )
        self.assertEqual(event.event_type, "bounced")
        self.assertEqual(event.reject_reason, "bounced")
        self.assertIs(event.esp_event["details"]["hard"], False)

    def test_expired_bounced_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "bounced",
                "details": {
                    "code": None,
                    "response": None,
                    "classification": None,
                    "hard": False,
                    "expired": True,
                },
            }
        )
        self.assertEqual(event.event_type, "bounced")
        self.assertEqual(event.reject_reason, "timed_out")
        self.assertIsNone(event.mta_response)
        self.assertIsNone(event.description)

    def test_deferred_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "deferred",
                "details": {
                    "code": 451,
                    "response": "4.7.1 Greylisted, try again later",
                },
            }
        )
        self.assertEqual(event.event_type, "deferred")
        self.assertIsNone(event.reject_reason)
        self.assertEqual(event.mta_response, "451 4.7.1 Greylisted, try again later")

    def test_spam_complaint_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "spam_complaint",
                "details": {"feedbackType": "abuse"},
            }
        )
        self.assertEqual(event.event_type, "complained")
        self.assertEqual(event.reject_reason, "spam")
        self.assertEqual(event.description, "abuse")

    def test_unsubscribed_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "broadcastId": "2c4e6a8b-0d1f-4a3b-8c5d-7e9f1a3b5c7d",
                "tag": None,
                "metadata": {},
                "event": "unsubscribed",
                "details": {"source": "one_click"},
            }
        )
        self.assertEqual(event.event_type, "unsubscribed")
        self.assertEqual(event.reject_reason, "unsubscribed")
        self.assertEqual(event.tags, [])
        self.assertEqual(event.metadata, {})

    def test_opened_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "opened",
                "details": {
                    "automated": "unknown",
                    "agent": "apple_privacy",
                    "reasons": ["privacy_proxy"],
                    "client": "apple_mail",
                    "device": None,
                    "first": True,
                    "firstHuman": False,
                },
            }
        )
        self.assertEqual(event.event_type, "opened")
        self.assertIsNone(event.user_agent)  # Sendora doesn't report it

    def test_clicked_event(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "clicked",
                "details": {
                    "automated": "unlikely",
                    "agent": None,
                    "reasons": [],
                    "client": "browser",
                    "device": "desktop",
                    "first": True,
                    "firstHuman": True,
                    "link": {
                        "index": 0,
                        "url": "https://example.se/invoices/2026-0912",
                    },
                },
            }
        )
        self.assertEqual(event.event_type, "clicked")
        self.assertEqual(event.click_url, "https://example.se/invoices/2026-0912")

    def test_clicked_event_after_content_deleted(self):
        event = self.post_event(
            {
                **DELIVERED_EVENT,
                "event": "clicked",
                "details": {
                    "automated": "unlikely",
                    "agent": None,
                    "reasons": [],
                    "client": None,
                    "device": None,
                    "first": False,
                    "firstHuman": False,
                    "link": {"index": 2, "url": None},
                },
            }
        )
        self.assertEqual(event.event_type, "clicked")
        self.assertIsNone(event.click_url)

    def test_tracking_opted_out_event(self):
        event = self.post_event(
            {key: value for key, value in DELIVERED_EVENT.items() if key != "details"}
            | {"event": "tracking_opted_out"}
        )
        self.assertEqual(event.event_type, "unknown")
        self.assertEqual(event.recipient, "anna@example.com")

    def test_cap_warning_event(self):
        event = self.post_event(
            {
                "id": "4d6f8a0b-2c4e-4f6a-8b0c-2d4e6f8a0b1c",
                "attempt": 1,
                "scope": "account",
                "serverId": None,
                "cap": 50000,
                "used": 40000,
                "periodStart": "2026-09-01T00:00:00.000Z",
                "periodEnd": "2026-10-01T00:00:00.000Z",
                "occurredAt": "2026-09-24T09:30:00.000Z",
                "event": "cap_warning",
            }
        )
        self.assertEqual(event.event_type, "unknown")
        self.assertEqual(event.event_id, "4d6f8a0b-2c4e-4f6a-8b0c-2d4e6f8a0b1c")
        self.assertEqual(
            event.timestamp, datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)
        )
        self.assertIsNone(event.message_id)
        self.assertIsNone(event.recipient)

    def test_future_event(self):
        event = self.post_event(
            {**DELIVERED_EVENT, "event": "some_future_event", "details": {}}
        )
        self.assertEqual(event.event_type, "unknown")
        self.assertEqual(event.message_id, "5f432ffd-c005-499b-aa3a-5b8a088ea20e")

    def test_misconfigured_inbound(self):
        errmsg = (
            "You seem to have set a Sendora webhook that receives `inbound`"
            " events to Anymail's Sendora *tracking* webhook URL."
        )
        with self.assertRaisesMessage(AnymailConfigurationError, errmsg):
            self.client_post_signed(
                "/anymail/sendora/tracking/",
                {"id": "6e8a0c2e-4a6c-4e8a-8c0e-2a4c6e8a0c2e", "event": "inbound"},
            )
