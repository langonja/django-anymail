import os
import time
import unittest
import uuid
from email.utils import formataddr

from django.test import SimpleTestCase, tag

from anymail.exceptions import AnymailAPIError, AnymailRecipientsRefused
from anymail.message import AnymailMessage

from .utils import AnymailTestMixin, override_settings, sample_image_path

# Environment variables to run these live integration tests...
# The key of a Sendora test server (sk_test_...). Sendora checks, logs and
# simulates each message sent with it, and delivers none of them:
ANYMAIL_TEST_SENDORA_API_KEY = os.getenv("ANYMAIL_TEST_SENDORA_API_KEY")
# Optional, the key of a live Sendora server. It sends only wanted mail,
# to anymail.dev's own mailboxes, never a bounce or a complaint:
ANYMAIL_TEST_SENDORA_LIVE_API_KEY = os.getenv("ANYMAIL_TEST_SENDORA_LIVE_API_KEY")
# A verified sending domain of those servers:
ANYMAIL_TEST_SENDORA_DOMAIN = os.getenv("ANYMAIL_TEST_SENDORA_DOMAIN")
# Optional, to test against a Sendora API other than production's:
ANYMAIL_TEST_SENDORA_API_URL = os.getenv(
    "ANYMAIL_TEST_SENDORA_API_URL", "https://api.sendora.se/v1/"
)


def sendora_mailers(api_key):
    return {
        "default": {
            "BACKEND": "anymail.backends.sendora.EmailBackend",
            "OPTIONS": {"api_key": api_key, "api_url": ANYMAIL_TEST_SENDORA_API_URL},
        },
    }


@tag("sendora", "live")
@unittest.skipUnless(
    ANYMAIL_TEST_SENDORA_API_KEY and ANYMAIL_TEST_SENDORA_DOMAIN,
    "Set ANYMAIL_TEST_SENDORA_API_KEY and ANYMAIL_TEST_SENDORA_DOMAIN"
    " environment variables to run Sendora integration tests",
)
@override_settings(MAILERS=sendora_mailers(ANYMAIL_TEST_SENDORA_API_KEY))
class SendoraBackendIntegrationTests(AnymailTestMixin, SimpleTestCase):
    """
    Sendora API integration tests

    These tests run against the live Sendora API, using a test server:
    Sendora handles each message as a live server would, but delivers none.
    Bounces and complaints use the test server's simulator addresses.
    """

    def setUp(self):
        super().setUp()
        self.from_email = f"from@{ANYMAIL_TEST_SENDORA_DOMAIN}"
        self.message = AnymailMessage(
            "Anymail Sendora integration test",
            "Text content",
            self.from_email,
            ["test+to1@anymail.dev"],
        )
        self.message.attach_alternative("<p>HTML content</p>", "text/html")

    def test_simple_send(self):
        # Example of getting the Sendora send status and message id from the message
        sent_count = self.message.send()
        self.assertEqual(sent_count, 1)

        anymail_status = self.message.anymail_status
        sent_status = anymail_status.recipients["test+to1@anymail.dev"].status
        message_id = anymail_status.recipients["test+to1@anymail.dev"].message_id

        self.assertEqual(sent_status, "queued")  # Sendora always queues
        self.assertUUIDIsValid(message_id)
        self.assertEqual(anymail_status.status, {sent_status})
        self.assertEqual(anymail_status.message_id, message_id)
        # A test server's key (not a live server's):
        self.assertIs(anymail_status.esp_response.json()["test"], True)

    def test_all_options(self):
        message = AnymailMessage(
            subject="Anymail Sendora all-options integration test",
            body="This is the text body",
            from_email=formataddr(("Test From, with comma", self.from_email)),
            to=["test+to1@anymail.dev", "Recipient 2 <test+to2@anymail.dev>"],
            cc=["test+cc1@anymail.dev", "Copy 2 <test+cc2@anymail.dev>"],
            bcc=["test+bcc1@anymail.dev", "Blind Copy 2 <test+bcc2@anymail.dev>"],
            reply_to=["reply1@example.com", "Reply 2 <reply2@example.com>"],
            headers={"X-Anymail-Test": "value", "X-Anymail-Count": 3},
            metadata={"meta1": "simple string", "meta2": 2},
            tags=["tag 1"],  # max one tag
            # Sendora refuses track_opens/track_clicks True until tracking
            # has been switched on in its dashboard
            track_opens=False,
            track_clicks=False,
        )
        message.attach("attachment1.txt", "Here is some\ntext for you", "text/plain")
        message.attach("vedhæftet fil.csv", "ID,Name\n1,Åsa", "text/csv")
        cid = message.attach_inline_image_file(
            sample_image_path(), domain=ANYMAIL_TEST_SENDORA_DOMAIN
        )
        message.attach_alternative(
            "<p><b>HTML:</b> with <a href='https://example.com'>link</a>"
            f"and image: <img src='cid:{cid}'></div>",
            "text/html",
        )

        message.send()
        self.assertEqual(message.anymail_status.status, {"queued"})
        self.assertEqual(len(message.anymail_status.recipients), 6)
        # One message, so one message id for all recipients:
        self.assertUUIDIsValid(message.anymail_status.message_id)

    def test_batch_send(self):
        # merge_metadata or merge_headers sends a separate message to each "to"
        message = AnymailMessage(
            subject="Anymail Sendora batch send integration test",
            body="This is the text body",
            from_email=self.from_email,
            to=["test+to1@anymail.dev", "Recipient 2 <test+to2@anymail.dev>"],
            metadata={"meta1": "simple string"},
            merge_metadata={
                "test+to1@anymail.dev": {"customer-id": "ZXK9123"},
                "test+to2@anymail.dev": {"customer-id": "ZZT4192"},
            },
            merge_headers={
                "test+to1@anymail.dev": {
                    "List-Unsubscribe": "<https://example.com/a/>"
                },
                "test+to2@anymail.dev": {
                    "List-Unsubscribe": "<https://example.com/b/>"
                },
            },
        )
        message.send()
        recipients = message.anymail_status.recipients
        self.assertEqual(recipients["test+to1@anymail.dev"].status, "queued")
        self.assertEqual(recipients["test+to2@anymail.dev"].status, "queued")
        self.assertNotEqual(
            recipients["test+to1@anymail.dev"].message_id,
            recipients["test+to2@anymail.dev"].message_id,
        )

    def test_suppressed_recipient(self):
        # The simulator bounces this address and suppresses it, within seconds.
        # (A label of its own keeps this run's suppression from affecting the next.)
        bounce_address = f"hardbounce+anymail-{uuid.uuid4().hex}@simulator.sendora.se"
        self.message.to = [bounce_address]
        self.message.send()
        self.assertEqual(self.message.anymail_status.status, {"queued"})

        for _attempt in range(20):
            time.sleep(3)
            try:
                self.message.send()
            except AnymailRecipientsRefused:
                break
        else:
            self.fail("Sendora did not suppress the simulator's hard bounce")
        self.assertEqual(
            self.message.anymail_status.recipients[bounce_address].status, "rejected"
        )

    def test_invalid_from(self):
        self.message.from_email = "webmaster@localhost"  # Django's default From
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        err = cm.exception
        self.assertEqual(err.status_code, 400)
        self.assertIn("invalid_request", str(err))

    def test_unverified_from(self):
        self.message.from_email = "from@example.com"
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        err = cm.exception
        self.assertEqual(err.status_code, 422)
        self.assertIn("from_domain_not_verified", str(err))

    @override_settings(MAILERS=sendora_mailers("sk_test_not_a_valid_key"))
    def test_invalid_api_key(self):
        with self.assertRaises(AnymailAPIError) as cm:
            self.message.send()
        err = cm.exception
        self.assertEqual(err.status_code, 401)
        self.assertIn("unauthorized", str(err))


@tag("sendora", "live")
@unittest.skipUnless(
    ANYMAIL_TEST_SENDORA_LIVE_API_KEY and ANYMAIL_TEST_SENDORA_DOMAIN,
    "Set ANYMAIL_TEST_SENDORA_LIVE_API_KEY and ANYMAIL_TEST_SENDORA_DOMAIN"
    " environment variables to run Sendora live server integration tests",
)
@override_settings(MAILERS=sendora_mailers(ANYMAIL_TEST_SENDORA_LIVE_API_KEY))
class SendoraBackendLiveServerIntegrationTests(AnymailTestMixin, SimpleTestCase):
    """
    Sendora API integration tests that deliver mail

    These tests send real email, from a live server, to /dev/null mailboxes
    on the anymail.dev domain. Keep it to wanted mail to those mailboxes:
    bounce and complaint cases belong in SendoraBackendIntegrationTests,
    on the test server's simulator.
    """

    def setUp(self):
        super().setUp()
        self.from_email = f"from@{ANYMAIL_TEST_SENDORA_DOMAIN}"

    def test_simple_send(self):
        message = AnymailMessage(
            "Anymail Sendora live server integration test",
            "Text content",
            self.from_email,
            ["test+to1@anymail.dev"],
        )
        message.attach_alternative("<p>HTML content</p>", "text/html")
        message.send()
        self.assertEqual(message.anymail_status.status, {"queued"})
        # A live server's key (not a test server's):
        self.assertIs(message.anymail_status.esp_response.json()["test"], False)

    def test_all_options(self):
        message = AnymailMessage(
            subject="Anymail Sendora live server all-options integration test",
            body="This is the text body",
            from_email=formataddr(("Test From, with comma", self.from_email)),
            to=["test+to1@anymail.dev", "Recipient 2 <test+to2@anymail.dev>"],
            cc=["test+cc1@anymail.dev"],
            bcc=["test+bcc1@anymail.dev"],
            reply_to=["Reply <reply@example.com>"],
            headers={"X-Anymail-Test": "value"},
            metadata={"meta1": "simple string", "meta2": 2},
            tags=["tag 1"],
        )
        message.attach("attachment1.txt", "Here is some\ntext for you", "text/plain")
        cid = message.attach_inline_image_file(
            sample_image_path(), domain=ANYMAIL_TEST_SENDORA_DOMAIN
        )
        message.attach_alternative(
            f"<p><b>HTML:</b> with an image: <img src='cid:{cid}'></p>",
            "text/html",
        )
        message.send()
        self.assertEqual(message.anymail_status.status, {"queued"})
        self.assertEqual(len(message.anymail_status.recipients), 4)
