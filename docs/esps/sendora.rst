.. _sendora-backend:

Sendora
=======

.. versionadded:: vNext

Anymail integrates Django with the `Sendora`_ transactional email service,
using its `email API`_.

.. note::

    **Troubleshooting:**
    If your Sendora messages aren't being delivered as expected, check the
    server's message log in Sendora's dashboard. Each message's events
    show what the receiving mail server answered.

.. _Sendora: https://sendorahq.com/
.. _email API: https://sendora.se/docs/api/post-v1-email


Settings
--------

.. rubric:: EMAIL_BACKEND

To use Anymail's Sendora backend, set:

  .. code-block:: python

      EMAIL_BACKEND = "anymail.backends.sendora.EmailBackend"
      ANYMAIL = {
          "SENDORA_API_KEY": "<your server key>",
      }

in your settings.py.


.. setting:: ANYMAIL_SENDORA_API_KEY

.. rubric:: SENDORA_API_KEY

Required for sending, and for the :ref:`inbound webhook <sendora-inbound>`.
A server key, which you create on a server's **API keys** tab in Sendora's
dashboard:

  .. code-block:: python

      ANYMAIL = {
          ...
          "SENDORA_API_KEY": "sk_...",
      }

Use a key of a :ref:`test server <sendora-test-servers>` (which starts with
``sk_test_``) in development, testing and staging environments.

Anymail will also look for ``SENDORA_API_KEY`` at the
root of the settings file if neither ``ANYMAIL["SENDORA_API_KEY"]``
nor ``ANYMAIL_SENDORA_API_KEY`` is set.


.. setting:: ANYMAIL_SENDORA_TRACKING_SECRET

.. rubric:: SENDORA_TRACKING_SECRET

Required for :ref:`status tracking webhooks <sendora-webhooks>`:
the signing secret of the Sendora webhook that posts tracking events.


.. setting:: ANYMAIL_SENDORA_INBOUND_SECRET

.. rubric:: SENDORA_INBOUND_SECRET

Required for the :ref:`inbound webhook <sendora-inbound>`:
the signing secret of the Sendora webhook that posts received mail.


.. setting:: ANYMAIL_SENDORA_API_URL

.. rubric:: SENDORA_API_URL

The base url for calling the Sendora API.

The default is ``SENDORA_API_URL = "https://api.sendora.se/v1/"``.
(It's unlikely you would need to change this.) The inbound webhook also
uses this setting, to download received messages.


.. _sendora-esp-extra:

esp_extra support
-----------------

To use Sendora features not directly supported by Anymail, you can
set a message's :attr:`~anymail.message.AnymailMessage.esp_extra` to
a `dict` of Sendora's `send email API body parameters`_.
Your :attr:`esp_extra` dict will be deeply merged into the Sendora
API payload, with `esp_extra` having precedence in conflicts.
(For batch sends, the `esp_extra` values apply to every message in the batch.)

For example, to send on one of the server's streams other than its default
transactional stream:

    .. code-block:: python

        message.esp_extra = {
            "streamId": "3d1a2b4c-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        }


(You can also set `"esp_extra"` in Anymail's :ref:`global send defaults <send-defaults>`
to apply it to all messages.)

.. _send email API body parameters:
   https://sendora.se/docs/api/post-v1-email


.. _sendora-quirks:

Limitations and quirks
----------------------

**A suppressed recipient refuses the whole message**
  If any recipient (to, cc, or bcc) is on the stream's suppression list,
  Sendora refuses the entire message, and sends it to nobody. Anymail reports
  the :attr:`~anymail.message.AnymailMessage.anymail_status` of each suppressed
  recipient as ``"rejected"``, and of each other recipient as ``"failed"``. If
  all recipients are suppressed, you'll get an
  :exc:`~anymail.exceptions.AnymailRecipientsRefused` error---unless you've
  set :setting:`ANYMAIL_IGNORE_RECIPIENT_STATUS`.

**Single tag**
  Sendora allows only a single tag per message. If your message has two or more
  :attr:`~anymail.message.AnymailMessage.tags`, you'll get an
  :exc:`~anymail.exceptions.AnymailUnsupportedFeature` error---or
  if you've enabled :setting:`ANYMAIL_IGNORE_UNSUPPORTED_FEATURES`,
  Anymail will use only the first tag.

**Metadata values are strings**
  Sendora's metadata values must be strings. Anymail converts numeric
  :attr:`~anymail.message.AnymailMessage.metadata` values to strings,
  and they are strings in status tracking events.

**No delayed sending**
  Sendora does not support :attr:`~anymail.message.AnymailMessage.send_at`.

**No ESP templates**
  Sendora does not offer stored templates or merge fields, so Anymail's
  :attr:`~anymail.message.AnymailMessage.template_id`,
  :attr:`~anymail.message.AnymailMessage.merge_data`
  and :attr:`~anymail.message.AnymailMessage.merge_global_data`
  are unsupported. (Batch sending with per-recipient metadata and headers
  is supported. See :ref:`sendora-batch` below.)

**No envelope sender overrides**
  Sendora does not support overriding
  :attr:`~anymail.message.AnymailMessage.envelope_sender`.
  (Sendora's own bounce domain handles each message's Return-Path.)

**Attachments require filenames**
  Sendora requires that all attachments and inline images have filenames.
  If you don't supply a filename, Anymail will use ``"attachment"`` (with an
  extension that matches the attachment's type).

**Inline images require a Content-ID**
  An inline attachment without a Content-ID will cause an
  :exc:`~anymail.exceptions.AnymailUnsupportedFeature` error.

**Text attachment charset**
  Sendora's API doesn't accept a charset for attachments, so Anymail sends
  text attachments with utf-8 encoding. Because the sent message doesn't
  identify that charset, some email apps may show non-ASCII characters in text
  attachments incorrectly.

**Non-ASCII mailboxes (EAI)**
  Sendora does not support Unicode mailboxes (the *user* part of
  *user\@domain*---see :ref:`EAI <eai>`). Trying to use an EAI address in
  ``from_email``, ``to``, ``cc``, or ``bcc`` results in a Sendora API error,
  and in ``reply_to`` an :exc:`~anymail.exceptions.AnymailUnsupportedFeature`
  error. (Non-ASCII *domains* are supported.)


.. _sendora-test-servers:

Test servers
------------

A Sendora test server takes mail exactly as a live server does, and delivers
none of it. Each message still appears in the server's message log, and
Sendora's webhooks fire for it. Test servers work before Sendora approves an
account, so you can develop against one right away.

A test server's keys start with ``sk_test_``. Messages sent with one get the
same :attr:`~anymail.message.AnymailMessage.anymail_status` as on a live
server. To check that a message went to a test server, look at the "test" field
in Sendora's API response:

  .. code-block:: python

      message.send()
      message.anymail_status.esp_response.json()["test"]  # True for a test server

Sending to the `simulator's addresses`_, such as
``hardbounce@simulator.sendora.se``, acts out a bounce, a complaint, or an open
or click, along with its webhook events.

.. _simulator's addresses:
   https://sendora.se/docs/test-servers#the-simulators-addresses


.. _sendora-batch:

Batch sending
-------------

Sendora supports :ref:`batch sending <batch-send>` with per-recipient
:attr:`~anymail.message.AnymailMessage.merge_metadata` and
:attr:`~anymail.message.AnymailMessage.merge_headers`. When you send a
message with multiple ``to`` addresses, those properties determine
how many distinct messages are sent:

* If the ``merge_...`` properties are *not* set (the default), Anymail
  will tell Sendora to send a single message, and all recipients will see
  the complete list of To addresses.
* If *any* of the ``merge_...`` properties are set---even to an empty `{}` dict,
  Anymail will tell Sendora to send a separate message for each ``to``
  address, and the recipients won't see the other To addresses. Each of
  those messages also goes to every cc and bcc recipient.

  .. code-block:: python

      message = EmailMessage(
          to=["alice@example.com", "Bob <bob@example.com>"],
          ...
      )
      message.merge_metadata = {
          "alice@example.com": {"order_no": "12345"},
          "bob@example.com": {"order_no": "54321"},
      }
      message.send()


.. _sendora-webhooks:

Status tracking webhooks
------------------------

If you are using Anymail's normalized :ref:`status tracking <event-tracking>`,
create a webhook on your Sendora server's **Webhooks** tab in Sendora's
dashboard (or through Sendora's API). Enter:

* URL: :samp:`https://{yoursite.example.com}/anymail/sendora/tracking/`
  (where *yoursite.example.com* is your Django site)

* Events: check all you want to receive, except ``inbound``.
  (Received mail uses its own :ref:`inbound webhook <sendora-inbound>`.)
  Sendora sends ``opened`` and ``clicked`` events only to a webhook that
  includes them.

Sendora shows the webhook's signing secret once, when you create the webhook.
Provide it as Anymail's :setting:`SENDORA_TRACKING_SECRET
<ANYMAIL_SENDORA_TRACKING_SECRET>` in your Django settings:

.. code-block:: python

    ANYMAIL = {
        # ...
        "SENDORA_TRACKING_SECRET": "whsec_...",
    }

Anymail verifies Sendora's signature on each webhook call, and refuses one
that is missing, incorrect, or more than five minutes old.
Sendora doesn't allow credentials in a webhook's URL, so its webhooks don't use
Anymail's shared :setting:`WEBHOOK_SECRET <ANYMAIL_WEBHOOK_SECRET>`.
(Other ESPs' webhooks in the same project still can.)
While you `roll a webhook's secret`_, Sendora signs each call with both secrets,
so either value works.

Sendora will report these Anymail :attr:`~anymail.signals.AnymailTrackingEvent.event_type`\s:
delivered, bounced, deferred, complained, unsubscribed, opened, and clicked.
Its other events, such as ``tracking_opted_out`` and ``cap_warning``, have the
event_type unknown.

Sendora's ``bounced`` event covers both hard and soft bounces. For a message
Sendora gave up on after repeated deferrals, Anymail sets the
:attr:`~anymail.signals.AnymailTrackingEvent.reject_reason` to ``"timed_out"``.

The event's :attr:`~anymail.signals.AnymailTrackingEvent.event_id` is the id
of Sendora's delivery, which is the same on each retry. And the event's
:attr:`~anymail.signals.AnymailTrackingEvent.esp_event` is the parsed JSON
payload of Sendora's `webhook event`_.

.. _roll a webhook's secret: https://sendora.se/docs/webhooks#roll-the-secret
.. _webhook event: https://sendora.se/docs/api/webhook-events


.. _sendora-inbound:

Inbound webhook
---------------

To receive email from Sendora through Anymail's normalized :ref:`inbound
<inbound>` handling, first set up an inbound stream on a Sendora server, as
`Sendora's inbound guide`_ explains. Then create a webhook through
Sendora's `create webhook API`_, with only the ``inbound`` event,
and ``"reference"`` for its ``inboundContent``. For example, from a
Django shell:

.. code-block:: python

    import requests

    response = requests.post(
        "https://api.sendora.se/v1/webhooks",
        headers={"Authorization": "Bearer <a key of the receiving server>"},
        json={
            "url": "https://yoursite.example.com/anymail/sendora/inbound/",
            "events": ["inbound"],
            "inboundContent": "reference",
        },
    )
    response.raise_for_status()
    print(response.json()["secret"])  # Sendora shows the secret only now

(A webhook created in Sendora's dashboard includes each message's parsed
content, which Anymail doesn't use, and which can make a webhook call larger
than Django's :setting:`DATA_UPLOAD_MAX_MEMORY_SIZE` allows.)

The response includes the webhook's signing secret. Provide it as Anymail's
:setting:`SENDORA_INBOUND_SECRET <ANYMAIL_SENDORA_INBOUND_SECRET>`. (Inbound
and tracking webhooks use different secrets, so have different setting names.)

The inbound webhook downloads each received message from Sendora's API, so
Anymail's :setting:`SENDORA_API_KEY <ANYMAIL_SENDORA_API_KEY>` must be a key of
the server whose stream receives the mail:

.. code-block:: python

    ANYMAIL = {
        # ...
        "SENDORA_API_KEY": "<a key of the receiving server>",
        "SENDORA_INBOUND_SECRET": "whsec_...",
    }

Anymail sets the inbound message's
:attr:`~anymail.inbound.AnymailInboundMessage.envelope_sender` and
:attr:`~anymail.inbound.AnymailInboundMessage.envelope_recipient` from
Sendora's data. Sendora doesn't score spam, but it checks each message's SPF,
DKIM, DMARC, and ARC, and reports the results in the event's
:attr:`~anymail.signals.AnymailInboundEvent.esp_event` ``["authentication"]``.
Only a DMARC ``"pass"`` proves that the From domain sent the message.

Sendora keeps received messages for its stream's retention period. A webhook
call that Sendora replays after that will fail, because Anymail can no longer
download the message.

.. _Sendora's inbound guide: https://sendora.se/docs/inbound
.. _create webhook API: https://sendora.se/docs/api/post-v1-webhooks
