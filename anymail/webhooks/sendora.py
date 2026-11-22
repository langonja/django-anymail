import hashlib
import hmac
import json
import re
import time
from collections.abc import Iterator
from urllib.parse import quote, urljoin

import requests
from django.utils.crypto import constant_time_compare
from django.utils.dateparse import parse_datetime

from ..exceptions import AnymailConfigurationError, AnymailWebhookValidationFailure
from ..inbound import AnymailInboundMessage
from ..signals import (
    AnymailInboundEvent,
    AnymailTrackingEvent,
    EventType,
    RejectReason,
    inbound,
    tracking,
)
from ..utils import DEFAULT_DOWNLOAD_CHUNK_SIZE, get_anymail_setting
from .base import AnymailCoreWebhookView


class SendoraBaseWebhookView(AnymailCoreWebhookView):
    """Base view class for Sendora webhooks"""

    # Sendora doesn't allow credentials in a webhook url, so it can't use
    # Anymail's shared WEBHOOK_SECRET basic auth. (Other ESPs' webhooks in
    # the same project still can.) Instead, Sendora signs every delivery, and
    # these views require its signing secret.

    esp_name = "Sendora"

    #: Maximum difference between the signature's timestamp and this server's clock
    signature_tolerance_seconds = 300

    def __init__(self, _secret_name, **kwargs):
        signing_secret = get_anymail_setting(
            _secret_name, esp_name=self.esp_name, kwargs=kwargs
        )
        # hmac.new requires bytes key:
        self.signing_secret = signing_secret.encode()
        self._secret_setting_name = f"{self.esp_name}_{_secret_name}".upper()
        super().__init__(**kwargs)

    def validate_request(self, request):
        # Sendora-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...]
        # There is a v1 signature for each of the webhook's current secrets.
        timestamp = None
        signatures = []
        for part in request.headers.get("Sendora-Signature", "").split(","):
            name, _, value = part.strip().partition("=")
            if name == "t":
                timestamp = value
            elif name == "v1":
                signatures.append(value.lower())
        if timestamp is None or not signatures:
            raise AnymailWebhookValidationFailure(
                "Sendora webhook called without a valid Sendora-Signature header"
            )
        if not re.fullmatch(r"[0-9]{1,18}", timestamp, re.ASCII):
            raise AnymailWebhookValidationFailure(
                "Sendora webhook called with an invalid signature timestamp"
            )
        if abs(time.time() - int(timestamp)) > self.signature_tolerance_seconds:
            raise AnymailWebhookValidationFailure(
                "Sendora webhook signature timestamp is more than five minutes"
                " from this server's clock"
            )

        expected_signature = hmac.new(
            key=self.signing_secret,
            msg=f"{timestamp}.".encode() + request.body,
            digestmod=hashlib.sha256,
        ).hexdigest()
        if not any(
            constant_time_compare(signature, expected_signature)
            for signature in signatures
        ):
            raise AnymailWebhookValidationFailure(
                f"Sendora webhook called with incorrect signature"
                f" (check Anymail {self._secret_setting_name} setting)"
            )

    def parse_events(self, request):
        # Sendora posts a single event in each delivery
        esp_event = json.loads(request.body.decode("utf-8"))
        return [self.esp_to_anymail_event(esp_event)]


class SendoraTrackingWebhookView(SendoraBaseWebhookView):
    """Handler for Sendora delivery and engagement tracking webhooks"""

    signal = tracking

    # (Declaring class attr allows override by kwargs in View.as_view.)
    tracking_secret = None

    def __init__(self, **kwargs):
        super().__init__(_secret_name="tracking_secret", **kwargs)

    event_types = {
        # Map Sendora event: Anymail normalized type
        "delivered": EventType.DELIVERED,
        "bounced": EventType.BOUNCED,
        "deferred": EventType.DEFERRED,
        "spam_complaint": EventType.COMPLAINED,
        "unsubscribed": EventType.UNSUBSCRIBED,
        "opened": EventType.OPENED,
        "clicked": EventType.CLICKED,
        # Others (tracking_opted_out, cap_warning, cap_reached,
        # inbound_cap_warning, inbound_cap_reached) are EventType.UNKNOWN
    }

    reject_reasons = {
        # Map Sendora event: Anymail normalized reject_reason
        "bounced": RejectReason.BOUNCED,
        "spam_complaint": RejectReason.SPAM,
        "unsubscribed": RejectReason.UNSUBSCRIBED,
    }

    def esp_to_anymail_event(self, esp_event):
        event_name = esp_event.get("event")
        if event_name == "inbound":
            raise AnymailConfigurationError(
                "You seem to have set a Sendora webhook that receives `inbound`"
                " events to Anymail's Sendora *tracking* webhook URL."
            )

        details = esp_event.get("details") or {}
        reject_reason = self.reject_reasons.get(event_name)
        if event_name == "bounced" and details.get("expired"):
            # Sendora gave up after repeated deferrals
            reject_reason = RejectReason.TIMED_OUT

        mta_response = details.get("response")
        if mta_response and details.get("code"):
            mta_response = f"{details['code']} {mta_response}"

        tag = esp_event.get("tag")
        return AnymailTrackingEvent(
            event_type=self.event_types.get(event_name, EventType.UNKNOWN),
            timestamp=parse_datetime(esp_event.get("occurredAt") or ""),
            # The delivery's id, which is the same on every attempt:
            event_id=esp_event.get("id"),
            message_id=esp_event.get("messageId"),
            recipient=esp_event.get("recipient"),
            reject_reason=reject_reason,
            description=details.get("classification") or details.get("feedbackType"),
            mta_response=mta_response,
            tags=[tag] if tag else [],
            metadata=esp_event.get("metadata") or {},
            click_url=(details.get("link") or {}).get("url"),
            esp_event=esp_event,
        )


class SendoraInboundWebhookView(SendoraBaseWebhookView):
    """Handler for Sendora inbound webhook"""

    signal = inbound

    # (Declaring class attr allows override by kwargs in View.as_view.)
    api_key = None
    api_url = None
    inbound_secret = None

    def __init__(self, **kwargs):
        self.api_key = get_anymail_setting(
            "api_key", esp_name=self.esp_name, kwargs=kwargs, allow_bare=True
        )
        self.api_url = get_anymail_setting(
            "api_url",
            esp_name=self.esp_name,
            kwargs=kwargs,
            default="https://api.sendora.se/v1/",
        )
        if not self.api_url.endswith("/"):
            self.api_url += "/"
        self.chunk_size = get_anymail_setting(
            "download_chunk_size",
            esp_name=self.esp_name,
            kwargs=kwargs,
            default=DEFAULT_DOWNLOAD_CHUNK_SIZE,
        )
        super().__init__(_secret_name="inbound_secret", **kwargs)

    def esp_to_anymail_event(self, esp_event):
        if esp_event.get("event") != "inbound":
            raise AnymailConfigurationError(
                "You seem to have set a Sendora webhook that receives events"
                " other than `inbound` to Anymail's Sendora *inbound* webhook URL."
            )

        # The event may or may not include the message's parsed content
        # (depending on the webhook's inboundContent option), but never
        # attachment content. Download the complete raw message instead.
        inbound_message_id = esp_event["inboundMessageId"]
        message = AnymailInboundMessage.parse_raw_mime_chunks(
            self.fetch_raw_message_chunks(inbound_message_id)
        )

        message.envelope_recipient = esp_event.get("envelopeRecipient")
        if "envelope" in esp_event:
            envelope = esp_event["envelope"]
        else:
            # inboundContent "reference" events omit the envelope sender
            envelope = self.fetch_inbound_message(inbound_message_id).get("envelope")
        # (envelope is null if Sendora no longer has the message content;
        # sender is null for a bounce)
        message.envelope_sender = (envelope or {}).get("sender")

        return AnymailInboundEvent(
            event_type=EventType.INBOUND,
            timestamp=parse_datetime(esp_event.get("receivedAt") or ""),
            # The delivery's id, which is the same on every attempt:
            event_id=esp_event.get("id"),
            esp_event=esp_event,
            message=message,
        )

    def message_url(self, inbound_message_id):
        return urljoin(self.api_url, f"inbound/{quote(inbound_message_id, safe='')}")

    def fetch_inbound_message(self, inbound_message_id: str) -> dict:
        response = requests.get(
            self.message_url(inbound_message_id),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        response.raise_for_status()
        return response.json()

    def fetch_raw_message_chunks(self, inbound_message_id: str) -> Iterator[bytes]:
        with requests.get(
            self.message_url(inbound_message_id) + "/raw",
            headers={"Authorization": f"Bearer {self.api_key}"},
            stream=True,
        ) as response:
            response.raise_for_status()
            yield from response.iter_content(chunk_size=self.chunk_size)
