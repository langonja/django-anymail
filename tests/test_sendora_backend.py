from datetime import datetime, timezone
from decimal import Decimal

from django.core import mail
from django.test import SimpleTestCase, tag

from anymail.exceptions import (
    AnymailAPIError,
    AnymailConfigurationError,
    AnymailRecipientsRefused,
    AnymailSerializationError,
    AnymailUnsupportedFeature,
)
from anymail.message import AnymailMessage, attach_inline_image

from .mock_requests_backend import (
    RequestsBackendMockAPITestCase,
    SessionSharingTestCases,
)
from .utils import (
    AnymailTestMixin,
    create_text_attachment,
    decode_att,
    ignore_fail_silently_warning,
    override_settings,
    sample_image_content,
)


@tag("sendora")
@override_settings(
    MAILERS={
        "default": {
            "BACKEND": "anymail.backends.sendora.EmailBackend",
            "OPTIONS": {"api_key": "sk_test_api_key"},
        },
    },
)
class SendoraBackendMockAPITestCase(RequestsBackendMockAPITestCase):
    DEFAULT_RAW_RESPONSE = b"""{
        "messageId": "5f432ffd-c005-499b-aa3a-5b8a088ea20e",
        "status": "accepted",
        "submittedAt": "2026-11-22T12:00:00.000Z",
        "test": true
    }"""

    def setUp(self):
        super().setUp()
        # Simple message useful for many tests
        self.message = mail.EmailMultiAlternatives(
            "Subject", "Text Body", "from@example.com", ["to@example.com"]
        )

    @staticmethod
    def batch_result(index, message_id=None, **error):
        """Return one item of a batch response: accepted, or else the error"""
        if error:
            return {"index": index, "status": "error", **error}
        return {
            "index": index,
            "status": "accepted",
            "messageId": message_id,
            "submittedAt": "2026-11-22T12:00:00.000Z",
            "test": False,
            "replayed": False,
        }


@tag("sendora")
class SendoraBackendStandardEmailTests(SendoraBackendMockAPITestCase):
    """Test backend support for Django standard email features"""

    def test_send_mail(self):
        """Test basic API for simple send"""
        mail.send_mail(
            "Subject here",
            "Here is the message.",
            "from@sender.example.com",
            ["to@example.com"],
            fail_silently=False,
        )
        self.assert_esp_called("https://api.sendora.se/v1/email")
        headers = self.get_api_call_headers()
        self.assertEqual(headers["Authorization"], "Bearer sk_test_api_key")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertUUIDIsValid(headers["Idempotency-Key"])
        data = self.get_api_call_json()
        self.assertEqual(data["subject"], "Subject here")
        self.assertEqual(data["text"], "Here is the message.")
        self.assertEqual(data["from"], {"email": "from@sender.example.com"})
        self.assertEqual(data["to"], [{"email": "to@example.com"}])

    def test_idempotency_key_per_send(self):
        self.message.send()
        first_key = self.get_api_call_headers()["Idempotency-Key"]
        self.message.send()
        second_key = self.get_api_call_headers()["Idempotency-Key"]
        self.assertNotEqual(first_key, second_key)

    def test_name_addr(self):
        """Make sure RFC2822 name-addr format (with display-name) is allowed

        (Test both sender and recipient addresses)
        """
        msg = mail.EmailMessage(
            "Subject",
            "Message",
            '"From, Name" <from@example.com>',
            ["Recipient #1 <to1@example.com>", "to2@example.com"],
            cc=["Carbon Copy <cc1@example.com>", "cc2@example.com"],
            bcc=["Blind Copy <bcc1@example.com>", "bcc2@example.com"],
        )
        msg.send()
        data = self.get_api_call_json()
        self.assertEqual(
            data["from"], {"name": "From, Name", "email": "from@example.com"}
        )
        self.assertEqual(
            data["to"],
            [
                {"name": "Recipient #1", "email": "to1@example.com"},
                {"email": "to2@example.com"},
            ],
        )
        self.assertEqual(
            data["cc"],
            [
                {"name": "Carbon Copy", "email": "cc1@example.com"},
                {"email": "cc2@example.com"},
            ],
        )
        self.assertEqual(
            data["bcc"],
            [
                {"name": "Blind Copy", "email": "bcc1@example.com"},
                {"email": "bcc2@example.com"},
            ],
        )

    def test_email_message(self):
        email = mail.EmailMessage(
            "Subject",
            "Body goes here",
            "from@example.com",
            ["to1@example.com", "Also To <to2@example.com>"],
            bcc=["bcc1@example.com", "Also BCC <bcc2@example.com>"],
            cc=["cc1@example.com", "Also CC <cc2@example.com>"],
            headers={
                "Reply-To": "another@example.com",
                "X-MyHeader": "my value",
            },
        )
        email.send()
        data = self.get_api_call_json()
        self.assertEqual(data["subject"], "Subject")
        self.assertEqual(data["text"], "Body goes here")
        self.assertEqual(data["from"], {"email": "from@example.com"})
        self.assertEqual(
            data["to"],
            [
                {"email": "to1@example.com"},
                {"name": "Also To", "email": "to2@example.com"},
            ],
        )
        self.assertEqual(
            data["cc"],
            [
                {"email": "cc1@example.com"},
                {"name": "Also CC", "email": "cc2@example.com"},
            ],
        )
        self.assertEqual(
            data["bcc"],
            [
                {"email": "bcc1@example.com"},
                {"name": "Also BCC", "email": "bcc2@example.com"},
            ],
        )
        self.assertEqual(
            data["headers"],
            {"Reply-To": "another@example.com", "X-MyHeader": "my value"},
        )

    def test_html_message(self):
        text_content = "This is an important message."
        html_content = "<p>This is an <strong>important</strong> message.</p>"
        email = mail.EmailMultiAlternatives(
            "Subject", text_content, "from@example.com", ["to@example.com"]
        )
        email.attach_alternative(html_content, "text/html")
        email.send()
        data = self.get_api_call_json()
        self.assertEqual(data["text"], text_content)
        self.assertEqual(data["html"], html_content)
        # Don't accidentally send the html part as an attachment:
        self.assertNotIn("attachments", data)

    def test_html_only_message(self):
        html_content = "<p>This is an <strong>important</strong> message.</p>"
        email = mail.EmailMessage(
            "Subject", html_content, "from@example.com", ["to@example.com"]
        )
        email.content_subtype = "html"  # Main content is now text/html
        email.send()
        data = self.get_api_call_json()
        self.assertNotIn("text", data)
        self.assertEqual(data["html"], html_content)

    def test_empty_body_with_html_alternative(self):
        # Sendora requires text (if provided) to be non-empty
        email = mail.EmailMultiAlternatives(
            "Subject", "", "from@example.com", ["to@example.com"]
        )
        email.attach_alternative("<p>HTML only</p>", "text/html")
        email.send()
        data = self.get_api_call_json()
        self.assertNotIn("text", data)
        self.assertEqual(data["html"], "<p>HTML only</p>")

    def test_extra_headers(self):
        self.message.extra_headers = {"X-Custom": "string", "X-Num": 123}
        self.message.send()
        data = self.get_api_call_json()
        # Sendora requires header values to be strings
        self.assertEqual(data["headers"], {"X-Custom": "string", "X-Num": "123"})

    def test_extra_headers_serialization_error(self):
        self.message.extra_headers = {"X-Custom": Decimal(12.5)}
        with self.assertRaisesMessage(AnymailSerializationError, "Decimal"):
            self.message.send()

    def test_reply_to(self):
        self.message.reply_to = ['"Reply, with comma" <reply@example.com>']
        self.message.send()
        data = self.get_api_call_json()
        self.assertEqual(
            data["headers"], {"Reply-To": '"Reply, with comma" <reply@example.com>'}
        )

    def test_multiple_reply_to(self):
        self.message.reply_to = [
            "reply@example.com",
            '"Other, with comma" <reply2@example.com>',
            "Інше <reply3@příklad.example.cz>",
        ]
        self.message.extra_headers = {"X-Other": "Keep"}
        self.message.send()
        data = self.get_api_call_json()
        self.assertEqual(
            data["headers"],
            {
                # Sendora encodes the non-ASCII display-name itself,
                # but requires IDNA encoding for the domain
                "Reply-To": "reply@example.com,"
                ' "Other, with comma" <reply2@example.com>,'
                " Інше <reply3@xn--pklad-zsa96e.example.cz>",
                "X-Other": "Keep",
            },
        )

    def test_eai_reply_to(self):
        self.message.reply_to = ["відповідь@example.com"]
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "EAI in reply_to"):
            self.message.send()

    def test_non_ascii_headers(self):
        email = mail.EmailMessage(
            from_email='"Odesílatel, z adresy" <sender@příklad.example.cz>',
            to=['"Příjemce, na adresu" <recipient@příklad.example.cz>'],
            subject="Předmět e-mailu",
            headers={"X-Extra": "Další"},
            body="Prostý text",
        )
        email.send()
        data = self.get_api_call_json()
        self.assertEqual(
            data["from"],
            {
                "name": "Odesílatel, z adresy",
                "email": "sender@xn--pklad-zsa96e.example.cz",
            },
        )
        self.assertEqual(
            data["to"],
            [
                {
                    "name": "Příjemce, na adresu",
                    "email": "recipient@xn--pklad-zsa96e.example.cz",
                }
            ],
        )
        self.assertEqual(data["subject"], "Předmět e-mailu")
        self.assertEqual(data["headers"], {"X-Extra": "Další"})

    def test_attachments(self):
        text_content = "pièce jointe\n"
        self.message.attach(create_text_attachment(text_content, charset="iso-8859-1"))
        self.message.attach("émoticône.img", b";-)", "image/x-emoticon")
        self.message.attach("data.csv", "ID,Name\n1,Åsa", "text/csv")
        image_data = sample_image_content()
        cid = attach_inline_image(self.message, image_data, "test.png")

        self.message.send()
        data = self.get_api_call_json()
        attachments = data["attachments"]
        self.assertEqual(len(attachments), 4)

        # Sendora's contentType doesn't allow a charset param,
        # so text attachments are converted to utf-8
        self.assertEqual(
            attachments[0],
            {
                "name": "attachment.txt",  # Sendora requires a name
                "contentType": "text/plain",
                "content": attachments[0]["content"],
            },
        )
        self.assertEqual(decode_att(attachments[0]["content"]).decode(), text_content)

        self.assertEqual(attachments[1]["name"], "émoticône.img")
        self.assertEqual(attachments[1]["contentType"], "image/x-emoticon")
        self.assertEqual(decode_att(attachments[1]["content"]), b";-)")
        self.assertNotIn("contentId", attachments[1])

        self.assertEqual(attachments[2]["name"], "data.csv")
        self.assertEqual(attachments[2]["contentType"], "text/csv")
        self.assertEqual(
            decode_att(attachments[2]["content"]).decode(), "ID,Name\n1,Åsa"
        )

        self.assertEqual(attachments[3]["name"], "test.png")
        self.assertEqual(attachments[3]["contentType"], "image/png")
        self.assertEqual(attachments[3]["contentId"], cid)
        self.assertEqual(decode_att(attachments[3]["content"]), image_data)

    def test_unnamed_attachment_unknown_type(self):
        self.message.attach(None, b"data", "application/x-anymail-unknown")
        self.message.send()
        attachment = self.get_api_call_json()["attachments"][0]
        self.assertEqual(attachment["name"], "attachment")

    def test_inline_attachment_without_content_id(self):
        image = create_text_attachment("not really an image")
        del image["Content-Disposition"]
        image.add_header("Content-Disposition", "inline")
        self.message.attach(image)
        with self.assertRaisesMessage(
            AnymailUnsupportedFeature, "inline attachments without Content-ID"
        ):
            self.message.send()

    def test_multiple_html_alternatives(self):
        # Multiple alternatives not allowed
        self.message.attach_alternative("<p>First html is OK</p>", "text/html")
        self.message.attach_alternative("<p>But not second html</p>", "text/html")
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "multiple html parts"):
            self.message.send()

    def test_html_alternative(self):
        # Only html alternatives allowed
        self.message.attach_alternative("{'not': 'allowed'}", "application/json")
        with self.assertRaisesMessage(
            AnymailUnsupportedFeature, "alternative part with type 'application/json'"
        ):
            self.message.send()

    @ignore_fail_silently_warning()
    def test_alternatives_fail_silently(self):
        # Make sure fail_silently is respected
        self.message.attach_alternative("{'not': 'allowed'}", "application/json")
        sent = self.message.send(fail_silently=True)
        self.assert_esp_not_called("API should not be called when send fails silently")
        self.assertEqual(sent, 0)

    def test_multiple_from_emails(self):
        self.message.from_email = 'first@example.com, "From, also" <second@example.com>'
        with self.assertRaisesMessage(
            AnymailUnsupportedFeature, "multiple from emails"
        ):
            self.message.send()

    def test_api_failure(self):
        self.set_mock_response(
            status_code=400,
            json_data={
                "error": "invalid_request",
                "message": "The request is invalid: to.0: Invalid email address",
                "issues": [{"path": "to.0", "message": "Invalid email address"}],
            },
        )
        with self.assertRaisesMessage(
            AnymailAPIError, "Sendora API response 400"
        ) as cm:
            self.message.send()
        # Error message includes response details:
        self.assertIn("to.0: Invalid email address", str(cm.exception))

    @ignore_fail_silently_warning()
    def test_api_failure_fail_silently(self):
        # Make sure fail_silently is respected
        self.set_mock_response(status_code=500)
        sent = self.message.send(fail_silently=True)
        self.assertEqual(sent, 0)

    def test_other_422_error(self):
        self.set_mock_response(
            status_code=422,
            json_data={
                "error": "from_domain_not_verified",
                "message": "The From domain is not a verified sending domain.",
            },
        )
        with self.assertRaisesMessage(AnymailAPIError, "from_domain_not_verified"):
            self.message.send()


@tag("sendora")
class SendoraBackendAnymailFeatureTests(SendoraBackendMockAPITestCase):
    """Test backend support for Anymail added features"""

    def test_envelope_sender(self):
        self.message.envelope_sender = "anything@bounces.example.com"
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "envelope_sender"):
            self.message.send()

    def test_metadata(self):
        self.message.metadata = {"user_id": "12345", "items": 6, "price": 9.5}
        self.message.send()
        data = self.get_api_call_json()
        # Sendora requires metadata values to be strings
        self.assertEqual(
            data["metadata"], {"user_id": "12345", "items": "6", "price": "9.5"}
        )

    def test_send_at(self):
        self.message.send_at = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "send_at"):
            self.message.send()

    def test_tags(self):
        self.message.tags = ["receipt"]
        self.message.send()
        data = self.get_api_call_json()
        self.assertEqual(data["tag"], "receipt")

    def test_multiple_tags(self):
        self.message.tags = ["receipt", "repeat-user"]
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "multiple tags"):
            self.message.send()

    @override_settings(ANYMAIL_IGNORE_UNSUPPORTED_FEATURES=True)
    def test_multiple_tags_ignore_unsupported_features(self):
        # First tag only when ignoring unsupported features
        self.message.tags = ["receipt", "repeat-user"]
        self.message.send()
        data = self.get_api_call_json()
        self.assertEqual(data["tag"], "receipt")

    def test_track_opens(self):
        self.message.track_opens = True
        self.message.send()
        data = self.get_api_call_json()
        self.assertIs(data["trackOpens"], True)

    def test_track_clicks(self):
        self.message.track_clicks = False
        self.message.send()
        data = self.get_api_call_json()
        self.assertIs(data["trackLinks"], False)

    def test_template_id(self):
        self.message.template_id = "welcome"
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "template_id"):
            self.message.send()

    def test_merge_data(self):
        self.message.merge_data = {"to@example.com": {"name": "Alice"}}
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "merge_data"):
            self.message.send()

    def test_merge_global_data(self):
        self.message.merge_global_data = {"site": "ExampleCo"}
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "merge_global_data"):
            self.message.send()

    def test_empty_merge_data(self):
        # Empty merge_data is a request for a batch send:
        # a separate message to each "to" address
        self.set_mock_response(
            json_data={
                "results": [
                    self.batch_result(0, "message-id-alice"),
                    self.batch_result(1, "message-id-bob"),
                ]
            }
        )
        self.message.to = ["alice@example.com", "Bob <bob@example.com>"]
        self.message.merge_data = {}
        self.message.send()

        self.assert_esp_called("https://api.sendora.se/v1/email/batch")
        self.assertUUIDIsValid(self.get_api_call_headers()["Idempotency-Key"])
        data = self.get_api_call_json()
        self.assertEqual(
            data,
            [
                {
                    "from": {"email": "from@example.com"},
                    "to": [{"email": "alice@example.com"}],
                    "subject": "Subject",
                    "text": "Text Body",
                },
                {
                    "from": {"email": "from@example.com"},
                    "to": [{"name": "Bob", "email": "bob@example.com"}],
                    "subject": "Subject",
                    "text": "Text Body",
                },
            ],
        )
        recipients = self.message.anymail_status.recipients
        self.assertEqual(recipients["alice@example.com"].status, "queued")
        self.assertEqual(recipients["alice@example.com"].message_id, "message-id-alice")
        self.assertEqual(recipients["bob@example.com"].status, "queued")
        self.assertEqual(recipients["bob@example.com"].message_id, "message-id-bob")

    def test_merge_metadata(self):
        self.set_mock_response(
            json_data={
                "results": [
                    self.batch_result(0, "message-id-alice"),
                    self.batch_result(1, "message-id-bob"),
                    self.batch_result(2, "message-id-cam"),
                ]
            }
        )
        self.message.to = [
            "alice@example.com",
            "Bob <bob@example.com>",
            "cam@example.com",
        ]
        self.message.merge_metadata = {
            "alice@example.com": {"order_id": 123, "tier": "premium"},
            "bob@example.com": {"order_id": 678},
        }
        self.message.metadata = {"notification_batch": "zx912", "tier": "basic"}
        self.message.send()

        self.assert_esp_called("/v1/email/batch")
        data = self.get_api_call_json()
        self.assertEqual(len(data), 3)
        self.assertEqual(
            data[0]["metadata"],
            {"notification_batch": "zx912", "tier": "premium", "order_id": "123"},
        )
        self.assertEqual(
            data[1]["metadata"],
            {"notification_batch": "zx912", "tier": "basic", "order_id": "678"},
        )
        # No merge_metadata for cam, so global metadata applies:
        self.assertEqual(
            data[2]["metadata"], {"notification_batch": "zx912", "tier": "basic"}
        )

    def test_merge_headers(self):
        self.set_mock_response(
            json_data={
                "results": [
                    self.batch_result(0, "message-id-alice"),
                    self.batch_result(1, "message-id-bob"),
                ]
            }
        )
        self.message.to = ["alice@example.com", "Bob <bob@example.com>"]
        self.message.reply_to = ["support@example.com"]
        self.message.extra_headers = {
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "List-Unsubscribe": "<mailto:unsubscribe@example.com>",
        }
        self.message.merge_headers = {
            "alice@example.com": {
                "list-unsubscribe": "<https://example.com/a/>",
                "X-Count": 1,
            },
        }
        self.message.send()

        data = self.get_api_call_json()
        self.assertEqual(
            data[0]["headers"],
            {
                "Reply-To": "support@example.com",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                # merge_headers replace headers case-insensitively,
                # preserving the original case:
                "List-Unsubscribe": "<https://example.com/a/>",
                "X-Count": "1",
            },
        )
        self.assertEqual(
            data[1]["headers"],
            {
                "Reply-To": "support@example.com",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                "List-Unsubscribe": "<mailto:unsubscribe@example.com>",
            },
        )

    def test_batch_send_with_cc_and_bcc(self):
        self.set_mock_response(
            json_data={
                "results": [
                    self.batch_result(0, "message-id-alice"),
                    self.batch_result(1, "message-id-bob"),
                ]
            }
        )
        message = AnymailMessage(
            from_email="from@example.com",
            to=["alice@example.com", "Bob <bob@example.com>"],
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
            merge_metadata={},
        )
        message.send()

        data = self.get_api_call_json()
        for item in data:
            # Every message goes to the cc and bcc recipients
            self.assertEqual(item["cc"], [{"email": "cc@example.com"}])
            self.assertEqual(item["bcc"], [{"email": "bcc@example.com"}])
        recipients = message.anymail_status.recipients
        self.assertEqual(recipients["alice@example.com"].message_id, "message-id-alice")
        self.assertEqual(recipients["bob@example.com"].message_id, "message-id-bob")
        # cc and bcc recipients get the status of the last message:
        self.assertEqual(recipients["cc@example.com"].status, "queued")
        self.assertEqual(recipients["cc@example.com"].message_id, "message-id-bob")
        self.assertEqual(recipients["bcc@example.com"].message_id, "message-id-bob")

    def test_batch_send_with_mixed_results(self):
        self.set_mock_response(
            json_data={
                "results": [
                    self.batch_result(0, "message-id-alice"),
                    self.batch_result(
                        1,
                        error="recipient_suppressed",
                        message="One or more recipients are on the suppression list.",
                        streamId="3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                        suppressed=[
                            {"address": "bob@example.com", "reason": "hard_bounce"}
                        ],
                    ),
                    self.batch_result(
                        2,
                        error="rate_limited",
                        message="This server may send 60 emails per minute.",
                        scope="server",
                        limit=60,
                        retryAfter=12,
                    ),
                ]
            }
        )
        message = AnymailMessage(
            from_email="from@example.com",
            to=["alice@example.com", "Bob@Example.com", "cam@example.com"],
            merge_metadata={},
        )
        message.send()

        recipients = message.anymail_status.recipients
        self.assertEqual(recipients["alice@example.com"].status, "queued")
        self.assertEqual(recipients["alice@example.com"].message_id, "message-id-alice")
        self.assertEqual(recipients["Bob@Example.com"].status, "rejected")
        self.assertIsNone(recipients["Bob@Example.com"].message_id)
        self.assertEqual(recipients["cam@example.com"].status, "failed")
        self.assertIsNone(recipients["cam@example.com"].message_id)

    def test_batch_result_count_mismatch(self):
        self.set_mock_response(
            json_data={"results": [self.batch_result(0, "message-id-alice")]}
        )
        self.message.to = ["alice@example.com", "bob@example.com"]
        self.message.merge_metadata = {}
        with self.assertRaisesMessage(
            AnymailAPIError, "Expected 2 Sendora batch results, got 1"
        ):
            self.message.send()

    def test_default_omits_options(self):
        """Make sure by default we don't send any ESP-specific options.

        Options not specified by the caller should be omitted entirely from
        the API call (*not* sent as False or empty). This ensures
        that your ESP account settings apply by default.
        """
        self.message.send()
        data = self.get_api_call_json()
        self.assertEqual(set(data), {"from", "to", "subject", "text"})

    def test_esp_extra(self):
        self.message.extra_headers = {"X-Custom": "keep"}
        self.message.esp_extra = {
            "streamId": "3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "headers": {"X-Extra": "merged"},
        }
        self.message.send()
        data = self.get_api_call_json()
        self.assertEqual(data["streamId"], "3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
        # esp_extra is merged deeply into the payload
        self.assertEqual(data["headers"], {"X-Custom": "keep", "X-Extra": "merged"})

    @override_settings(
        ANYMAIL_SENDORA_SEND_DEFAULTS={
            "esp_extra": {"streamId": "3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d"}
        }
    )
    def test_stream_in_send_defaults(self):
        self.message.send()
        data = self.get_api_call_json()
        self.assertEqual(data["streamId"], "3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d")

    def test_send_attaches_anymail_status(self):
        """The anymail_status should be attached to the message when it is sent"""
        msg = mail.EmailMessage(
            "Subject",
            "Message",
            "from@example.com",
            ["Recipient <to1@example.com>"],
            cc=["cc@example.com"],
        )
        sent = msg.send()
        self.assertEqual(sent, 1)
        self.assertEqual(msg.anymail_status.status, {"queued"})
        self.assertEqual(
            msg.anymail_status.message_id, "5f432ffd-c005-499b-aa3a-5b8a088ea20e"
        )
        for recipient in ["to1@example.com", "cc@example.com"]:
            self.assertEqual(msg.anymail_status.recipients[recipient].status, "queued")
            self.assertEqual(
                msg.anymail_status.recipients[recipient].message_id,
                "5f432ffd-c005-499b-aa3a-5b8a088ea20e",
            )
        self.assertEqual(
            msg.anymail_status.esp_response.content, self.DEFAULT_RAW_RESPONSE
        )

    @ignore_fail_silently_warning()
    def test_send_failed_anymail_status(self):
        """If the send fails, anymail_status should contain initial values"""
        self.set_mock_response(status_code=500)
        sent = self.message.send(fail_silently=True)
        self.assertEqual(sent, 0)
        self.assertIsNone(self.message.anymail_status.status)
        self.assertIsNone(self.message.anymail_status.message_id)
        self.assertEqual(self.message.anymail_status.recipients, {})
        self.assertIsNone(self.message.anymail_status.esp_response)

    def test_send_unparsable_response(self):
        """
        If the send succeeds, but a non-JSON API response, should raise an API exception
        """
        mock_response = self.set_mock_response(
            status_code=200, raw=b"yikes, this isn't a real response"
        )
        with self.assertRaises(AnymailAPIError):
            self.message.send()
        self.assertIsNone(self.message.anymail_status.status)
        self.assertIsNone(self.message.anymail_status.message_id)
        self.assertEqual(self.message.anymail_status.recipients, {})
        self.assertEqual(self.message.anymail_status.esp_response, mock_response)

    def test_send_unexpected_response_format(self):
        self.set_mock_response(json_data={"id": "not what Sendora returns"})
        with self.assertRaisesMessage(
            AnymailAPIError, "Invalid Sendora API response format"
        ):
            self.message.send()

    def test_json_serialization_errors(self):
        """Try to provide more information about non-json-serializable data"""
        self.message.metadata = {"price": Decimal("19.99")}  # yeah, don't do this
        with self.assertRaises(AnymailSerializationError) as cm:
            self.message.send()
        err = cm.exception
        self.assertIsInstance(err, TypeError)  # compatibility with json.dumps
        # our added context:
        self.assertIn("Don't know how to send this data to Sendora", str(err))
        # original message:
        self.assertRegex(str(err), r"Decimal.*is not JSON serializable")

    @override_settings(
        MAILERS={
            "default": {
                "BACKEND": "anymail.backends.sendora.EmailBackend",
                "OPTIONS": {
                    "api_key": "sk_test_api_key",
                    "api_url": "https://api.staging.example.com/v1",
                },
            },
        },
    )
    def test_override_api_url(self):
        self.message.send()
        self.assert_esp_called("https://api.staging.example.com/v1/email")


@tag("sendora")
class SendoraBackendRecipientsRefusedTests(SendoraBackendMockAPITestCase):
    """
    Sendora refuses the whole message, with a 422 error,
    if any of its recipients is suppressed.
    """

    def set_mock_suppressed(self, *addresses):
        self.set_mock_response(
            status_code=422,
            json_data={
                "error": "recipient_suppressed",
                "message": "One or more recipients are on the suppression list"
                " of this stream; suppressed says which and why.",
                "streamId": "3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                "suppressed": [
                    {"address": address, "reason": "hard_bounce"}
                    for address in addresses
                ],
            },
        )

    def test_recipients_refused(self):
        self.set_mock_suppressed("invalid@localhost", "reject@example.com")
        msg = mail.EmailMessage(
            "Subject",
            "Body",
            "from@example.com",
            ["invalid@localhost", "Reject@Example.com"],
        )
        with self.assertRaises(AnymailRecipientsRefused):
            msg.send()

    @override_settings(ANYMAIL_IGNORE_RECIPIENT_STATUS=True)
    def test_recipients_refused_ignored(self):
        self.set_mock_suppressed("reject@example.com")
        msg = mail.EmailMessage(
            "Subject", "Body", "from@example.com", ["reject@example.com"]
        )
        msg.send()
        self.assertEqual(
            msg.anymail_status.recipients["reject@example.com"].status, "rejected"
        )

    def test_some_recipients_refused(self):
        # Nothing is sent to the other recipients either
        self.set_mock_suppressed("reject@xn--pklad-zsa96e.example.cz")
        msg = mail.EmailMessage(
            "Subject",
            "Body",
            "from@example.com",
            ["to@example.com", "Reject@příklad.example.cz"],
            cc=["cc@example.com"],
        )
        msg.send()
        self.assertEqual(msg.anymail_status.status, {"rejected", "failed"})
        recipients = msg.anymail_status.recipients
        self.assertEqual(recipients["to@example.com"].status, "failed")
        self.assertEqual(recipients["Reject@příklad.example.cz"].status, "rejected")
        self.assertEqual(recipients["cc@example.com"].status, "failed")
        self.assertIsNone(recipients["to@example.com"].message_id)


@tag("sendora")
class SendoraBackendSessionSharingTestCase(
    SessionSharingTestCases, SendoraBackendMockAPITestCase
):
    """Requests session sharing tests"""

    pass  # tests are defined in SessionSharingTestCases


@tag("sendora")
@override_settings(
    MAILERS={"default": {"BACKEND": "anymail.backends.sendora.EmailBackend"}},
)
class SendoraBackendImproperlyConfiguredTests(AnymailTestMixin, SimpleTestCase):
    """Test ESP backend without required settings in place"""

    def test_missing_api_key(self):
        with self.assertRaisesRegex(
            AnymailConfigurationError,
            r"'api_key'|\bSENDORA_API_KEY.*ANYMAIL_SENDORA_API_KEY",
        ):
            mail.send_mail("Subject", "Message", "from@example.com", ["to@example.com"])
