import mimetypes
import uuid

from ..exceptions import AnymailRequestsAPIError
from ..message import AnymailRecipientStatus
from ..utils import (
    BASIC_NUMERIC_TYPES,
    CaseInsensitiveCasePreservingDict,
    get_anymail_setting,
    update_deep,
)
from .base_requests import AnymailRequestsBackend, RequestsPayload


class EmailBackend(AnymailRequestsBackend):
    """
    Sendora API Email Backend
    """

    esp_name = "Sendora"

    def __init__(self, **kwargs):
        """Init options from Django settings"""
        esp_name = self.esp_name
        self.api_key = get_anymail_setting(
            "api_key", esp_name=esp_name, kwargs=kwargs, allow_bare=True
        )
        api_url = get_anymail_setting(
            "api_url",
            esp_name=esp_name,
            kwargs=kwargs,
            default="https://api.sendora.se/v1/",
        )
        if not api_url.endswith("/"):
            api_url += "/"
        super().__init__(api_url, **kwargs)

    def build_message_payload(self, message, defaults):
        return SendoraPayload(message, defaults, self)

    def raise_for_status(self, response, payload, message):
        # Sendora answers 422 when it refuses a message because a recipient
        # is suppressed. That is a recipient status, so parse_recipient_status
        # handles it (and raises for any other 422).
        if response.status_code != 422:
            super().raise_for_status(response, payload, message)

    def parse_recipient_status(self, response, payload, message):
        parsed_response = self.deserialize_json_response(response, payload, message)
        if response.status_code == 422 and not is_suppressed_refusal(parsed_response):
            raise AnymailRequestsAPIError(
                email_message=message,
                payload=payload,
                response=response,
                backend=self,
            )

        try:
            if response.status_code == 422:
                return self.suppressed_status(parsed_response, payload.recipients)
            if not payload.is_batch():
                return accepted_status(parsed_response["messageId"], payload.recipients)

            results = parsed_response["results"]
            if len(results) != len(payload.to_recipients):
                raise AnymailRequestsAPIError(
                    f"Expected {len(payload.to_recipients)} Sendora batch results,"
                    f" got {len(results)}",
                    email_message=message,
                    payload=payload,
                    response=response,
                    backend=self,
                )
            # Each message in a batch goes to one "to" address, plus every cc
            # and bcc, so a cc or bcc gets the status of the last message.
            recipient_status = CaseInsensitiveCasePreservingDict()
            for to, result in zip(payload.to_recipients, results):
                recipients = [to, *payload.cc_and_bcc_recipients]
                if result["status"] == "accepted":
                    status = accepted_status(result["messageId"], recipients)
                elif is_suppressed_refusal(result):
                    status = self.suppressed_status(result, recipients)
                else:
                    status = {
                        recipient.addr_spec: AnymailRecipientStatus(
                            message_id=None, status="failed"
                        )
                        for recipient in recipients
                    }
                recipient_status.update(status)
            return dict(recipient_status)
        except (KeyError, TypeError) as err:
            raise AnymailRequestsAPIError(
                "Invalid Sendora API response format",
                email_message=message,
                payload=payload,
                response=response,
                backend=self,
            ) from err

    def suppressed_status(self, refusal, recipients):
        # Sendora refuses the whole message if any recipient is suppressed,
        # so nothing was sent to the others either. It reports each
        # suppressed address in lowercase, as sent (with IDNA encoding).
        suppressed = {item["address"].lower() for item in refusal["suppressed"]}
        recipient_status = {}
        for recipient in recipients:
            sent_as = recipient.format_addr_spec(idna_encode=self.idna_encode)
            recipient_status[recipient.addr_spec] = AnymailRecipientStatus(
                message_id=None,
                status="rejected" if sent_as.lower() in suppressed else "failed",
            )
        return recipient_status


def is_suppressed_refusal(result):
    return isinstance(result, dict) and result.get("error") == "recipient_suppressed"


def accepted_status(message_id, recipients):
    return {
        recipient.addr_spec: AnymailRecipientStatus(
            message_id=message_id, status="queued"
        )
        for recipient in recipients
    }


def stringify_values(values):
    """
    Return a copy of values with numbers converted to str.
    (Sendora requires string metadata and header values.)
    """
    return {
        key: str(value) if isinstance(value, BASIC_NUMERIC_TYPES) else value
        for key, value in values.items()
    }


class SendoraPayload(RequestsPayload):
    def __init__(self, message, defaults, backend, *args, **kwargs):
        # Needed for parse_recipient_status:
        self.recipients = []
        self.to_recipients = []
        self.cc_and_bcc_recipients = []
        # Late bound in serialize_data for batch sends:
        self.merge_metadata = {}
        self.merge_headers = {}
        headers = {
            "Authorization": f"Bearer {backend.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Required for batch sends. Any retry of this same request
            # gets Sendora's first answer, and sends nothing twice.
            "Idempotency-Key": str(uuid.uuid4()),
        }
        super().__init__(message, defaults, backend, headers=headers, *args, **kwargs)

    def get_api_endpoint(self):
        if self.is_batch():
            return "email/batch"
        return "email"

    def serialize_data(self):
        if self.is_batch():
            return self.serialize_json(self.burst_for_batch())
        return self.serialize_json(self.data)

    def burst_for_batch(self):
        """Return a list of messages, one for each "to" address."""
        base = {key: value for key, value in self.data.items() if key != "to"}
        messages = []
        for to_dict, to in zip(self.data.get("to", []), self.to_recipients):
            message = {**base, "to": [to_dict]}
            if to.addr_spec in self.merge_metadata:
                message["metadata"] = {
                    **base.get("metadata", {}),
                    **stringify_values(self.merge_metadata[to.addr_spec]),
                }
            if to.addr_spec in self.merge_headers:
                headers = CaseInsensitiveCasePreservingDict(base.get("headers", {}))
                headers.update(stringify_values(self.merge_headers[to.addr_spec]))
                message["headers"] = dict(headers)
            messages.append(message)
        return messages

    #
    # Payload construction
    #

    def init_payload(self):
        self.data = {}  # becomes json

    def set_from_email(self, email):
        self.data["from"] = email.as_dict(idna_encode=self.backend.idna_encode)

    def set_recipients(self, recipient_type, emails):
        assert recipient_type in ["to", "cc", "bcc"]
        if emails:
            self.data[recipient_type] = [
                email.as_dict(idna_encode=self.backend.idna_encode) for email in emails
            ]
            self.recipients += emails
            if recipient_type == "to":
                self.to_recipients = emails
            else:
                self.cc_and_bcc_recipients += emails

    def set_subject(self, subject):
        self.data["subject"] = subject

    def set_reply_to(self, emails):
        if emails:
            # Sendora's API takes Reply-To as a header, which it encodes itself,
            # but it can't encode a non-ASCII mailbox (EAI).
            if any(email.uses_eai for email in emails):
                self.unsupported_feature("EAI in reply_to")
            self.data.setdefault("headers", {})["Reply-To"] = ", ".join(
                email.format(idna_encode=self.backend.idna_encode) for email in emails
            )

    def set_extra_headers(self, headers):
        self.data.setdefault("headers", {}).update(stringify_values(headers))

    def set_text_body(self, body):
        if body:
            self.data["text"] = body

    def set_html_body(self, body):
        if "html" in self.data:
            # second html body could show up through multiple alternatives,
            # or html body + alternative
            self.unsupported_feature("multiple html parts")
        if body:
            self.data["html"] = body

    def add_attachment(self, attachment):
        name = attachment.name
        if not name:
            # Sendora requires a filename
            name = "attachment" + (mimetypes.guess_extension(attachment.mimetype) or "")
        att = {
            "name": name,
            # Sendora's contentType can't include a charset param,
            # so send text attachments as utf-8.
            "content": attachment.b64content_utf8,
            "contentType": attachment.mimetype,
        }
        if attachment.inline:
            if attachment.cid:
                att["contentId"] = attachment.cid
            else:
                self.unsupported_feature("inline attachments without Content-ID")
        self.data.setdefault("attachments", []).append(att)

    def set_metadata(self, metadata):
        self.data["metadata"] = stringify_values(metadata)

    def set_tags(self, tags):
        if len(tags) > 1:
            self.unsupported_feature("multiple tags")
        if len(tags) > 0:
            self.data["tag"] = tags[0]

    def set_track_clicks(self, track_clicks):
        self.data["trackLinks"] = track_clicks

    def set_track_opens(self, track_opens):
        self.data["trackOpens"] = track_opens

    def set_merge_data(self, merge_data):
        # Sendora has no templates. Empty merge_data is a request
        # for a batch send; any other merge_data is unsupported.
        if any(recipient_data for recipient_data in merge_data.values()):
            self.unsupported_feature("merge_data")

    def set_merge_metadata(self, merge_metadata):
        self.merge_metadata = merge_metadata  # late bound in serialize_data

    def set_merge_headers(self, merge_headers):
        self.merge_headers = merge_headers  # late bound in serialize_data

    def set_esp_extra(self, extra):
        update_deep(self.data, extra)
