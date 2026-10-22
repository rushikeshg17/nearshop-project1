# WhatsApp message templates

The WhatsApp templates NearShop sends, for approval at Meta (B8) and for the code that sends them. Plan section 8.7;
P4.3 (outbound notifications); P4.1 (the login code). Every notification template is submitted in English (en) and
Kannada (kn), all under the **utility** category. The code is in backend/app/services/whatsapp.py (notifications)
and backend/app/services/messaging.py (the login code).

## How a notification becomes a message

1. A reservation or order changes. notify() writes the in-app notification and, in the same transaction, one row in
   notification_outbox when the kind has a template below, WhatsApp notifications are on, and the recipient has the
   WhatsApp consent (P3.1) and a verified phone (P4.1). A rolled-back transaction leaves no row.
2. The send_notifications job (every 30 s, under its lease) sends the row as the template below. A failed send is
   retried after 30 s, 2 minutes and 10 minutes, then the row is marked failed. Nothing is sent inside the
   reservation or order transaction, so a slow or failing send never blocks it.
3. Meta's status callbacks (sent, delivered, read, failed) reach POST /api/webhooks/whatsapp and are recorded once
   each on the row. The row keeps the seconds from the notification to the Cloud API's accept (seconds_to_sent) and
   to delivered (seconds_to_delivered). Target (plan section 8.7): median 60 s or less to delivered, 95th
   percentile 3 minutes or less.

Every template has two body variables:

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The notification's title, on one line | New reservation R-7KQ2MX |
| {{2}} | The notification's text, on one line (at most 400 characters) | Asha wants 1 x Astral PTFE Thread Seal Tape. Confirm within 20 min. |

Until P7.11 gives the notifications Kannada text, the variables are English, and the code sends the language in
NEARSHOP_WHATSAPP_TEMPLATE_LANGUAGE (en). Submit both languages now so they are approved when P7.11 lands.

## Notification templates

### nearshop_shop_new_reservation (shop, new reservation)

Kind reservation_new, to the shop's owner. Two quick-reply buttons. Each button's payload is
reservation:<reservation code>:confirm or reservation:<reservation code>:decline, which P4.4 reads.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: you have a new reservation request.<br><br>{{1}}.<br>Details: {{2}}<br><br>Tap Confirm to hold the item for the customer, or Decline if you cannot. Requests nobody answers expire on their own. | NearShop: ನಿಮಗೆ ಹೊಸ ಕಾಯ್ದಿರಿಸುವಿಕೆ ವಿನಂತಿ ಬಂದಿದೆ.<br><br>{{1}}.<br>ವಿವರ: {{2}}<br><br>ಗ್ರಾಹಕರಿಗಾಗಿ ವಸ್ತುವನ್ನು ಹಿಡಿದಿಡಲು ದೃಢೀಕರಿಸಿ ಒತ್ತಿ, ಸಾಧ್ಯವಾಗದಿದ್ದರೆ ನಿರಾಕರಿಸಿ ಒತ್ತಿ. ಯಾರೂ ಉತ್ತರಿಸದ ವಿನಂತಿಗಳು ತಾನಾಗಿಯೇ ಮುಕ್ತಾಯವಾಗುತ್ತವೆ. |
| Button 1 (quick reply) | Confirm | ದೃಢೀಕರಿಸಿ |
| Button 2 (quick reply) | Decline | ನಿರಾಕರಿಸಿ |

### nearshop_shop_new_order (shop, new order)

Kind order_new, to the shop's owner.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: you have a new delivery order.<br><br>{{1}}.<br>Details: {{2}}<br><br>Open your orders in NearShop to confirm it. Orders not confirmed within 60 minutes are cancelled automatically. | NearShop: ನಿಮಗೆ ಹೊಸ ಡೆಲಿವರಿ ಆರ್ಡರ್ ಬಂದಿದೆ.<br><br>{{1}}.<br>ವಿವರ: {{2}}<br><br>ಅದನ್ನು ದೃಢೀಕರಿಸಲು NearShop ನಲ್ಲಿ ನಿಮ್ಮ ಆರ್ಡರ್‌ಗಳನ್ನು ತೆರೆಯಿರಿ. 60 ನಿಮಿಷಗಳಲ್ಲಿ ದೃಢೀಕರಿಸದ ಆರ್ಡರ್‌ಗಳು ತಾನಾಗಿಯೇ ರದ್ದಾಗುತ್ತವೆ. |

### nearshop_reservation_confirmed (customer, reservation confirmed)

Kind reservation_confirmed, to the customer.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: your reservation is confirmed.<br><br>{{1}}.<br>Details: {{2}}<br><br>Show your reservation code at the counter when you collect it. | NearShop: ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆ ದೃಢೀಕರಿಸಲಾಗಿದೆ.<br><br>{{1}}.<br>ವಿವರ: {{2}}<br><br>ವಸ್ತುವನ್ನು ಪಡೆಯುವಾಗ ಕೌಂಟರ್‌ನಲ್ಲಿ ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆ ಕೋಡ್ ತೋರಿಸಿ. |

### nearshop_reservation_ready (customer, ready for pickup)

Kind reservation_ready, to the customer.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: your item is ready for pickup.<br><br>{{1}}.<br>Details: {{2}}<br><br>Show your reservation code at the counter. | NearShop: ನಿಮ್ಮ ವಸ್ತು ಪಡೆಯಲು ಸಿದ್ಧವಾಗಿದೆ.<br><br>{{1}}.<br>ವಿವರ: {{2}}<br><br>ಕೌಂಟರ್‌ನಲ್ಲಿ ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆ ಕೋಡ್ ತೋರಿಸಿ. |

### nearshop_reservation_closed (customer, reservation expired or rejected)

Kinds reservation_expired and reservation_rejected, to the customer. (The shop's own "reservation expired" stays in
the app.)

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: an update on your reservation.<br><br>{{1}}.<br>Details: {{2}}<br><br>You can search NearShop for the item at another shop nearby. | NearShop: ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆಯ ಬಗ್ಗೆ ಮಾಹಿತಿ.<br><br>{{1}}.<br>ವಿವರ: {{2}}<br><br>ಹತ್ತಿರದ ಇನ್ನೊಂದು ಅಂಗಡಿಯಲ್ಲಿ ಈ ವಸ್ತುವನ್ನು NearShop ನಲ್ಲಿ ಹುಡುಕಬಹುದು. |

### nearshop_order_status (customer, order status changed)

Kinds order_shop_confirmed, order_preparing, order_out_for_delivery, order_delivered, order_delivery_failed and
order_cancelled, to the customer. (The shop's "cancelled by customer" stays in the app.) When the shop confirms an
order, the text holds the customer's 4-digit delivery code: the message goes only to the customer's own verified
phone.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: your order has an update.<br><br>{{1}}.<br>Details: {{2}}<br><br>You can follow the order in NearShop under My orders. | NearShop: ನಿಮ್ಮ ಆರ್ಡರ್‌ನ ಸ್ಥಿತಿ ಬದಲಾಗಿದೆ.<br><br>{{1}}.<br>ವಿವರ: {{2}}<br><br>NearShop ನಲ್ಲಿ ನನ್ನ ಆರ್ಡರ್‌ಗಳು ವಿಭಾಗದಲ್ಲಿ ಆರ್ಡರ್ ಅನ್ನು ನೋಡಬಹುದು. |

Have a native Kannada speaker read every Kannada body and button before submission.

### Not sent on WhatsApp

Every other notification stays in the app only: pickup complete, cancellations the customer made, the shop's own
expiry notice, delivery-code reminders, grievances, shop verification, phone-migration warnings and admin notices.

## Other templates

| Template | Category | Variables | Where |
|---|---|---|---|
| The login code (NEARSHOP_WHATSAPP_OTP_TEMPLATE) | authentication | {{1}}: the code; copy-code button with the code | P4.1, backend/app/services/messaging.py |
| Shop daily stock check | utility (Meta may class it as marketing) | Listed by P4.4 | P4.4 |

Phases 9 and 10 add their templates here (P9.3 and later).

## Rates

India per-message rates, as published by the business service provider Interakt (Starter plan), **read on
18 Oct 2026** (plan section 8.7). They were not re-read for P4.3 (22 Oct 2026). Meta's own INR rate card (a CSV)
could not be read and may be lower. Recheck both before budgeting.

| Category | Per message |
|---|---|
| Utility | ₹0.160 |
| Authentication | ₹0.129 |
| Marketing | ₹0.970 |

Meta has charged per message, not per conversation, since 1 July 2025 (Meta's pricing docs, read 18 Oct 2026). A
utility template sent inside an open 24-hour customer-service window (the recipient messaged the business in the last
24 hours) is free. Marketing templates are always charged. NEARSHOP_WHATSAPP_UTILITY_COST_INR (default 0.16) is the
utility rate the code records on each row the Cloud API accepted (cost_estimate, in rupees; it does not know whether a
service window was open, so it is an upper bound).

Example: 50 reservations a day, each with a new-reservation message to the shop and about two customer messages,
is about 150 utility messages a day: about ₹24 a day, or ₹720 a month, before any free service-window sends.

## Configuration

Off by default. Nothing is sent until all of these are set on the environment (Render, sync: false for secrets):

| Variable | What |
|---|---|
| NEARSHOP_WHATSAPP_NOTIFICATIONS | true to write outbox rows and send them |
| NEARSHOP_WHATSAPP_ACCESS_TOKEN | The Cloud API system-user token (also used by P4.1) |
| NEARSHOP_WHATSAPP_PHONE_NUMBER_ID | The sending number's id (also used by P4.1) |
| NEARSHOP_WHATSAPP_APP_SECRET | The Meta app's secret: the webhook refuses every call without it |
| NEARSHOP_WHATSAPP_WEBHOOK_VERIFY_TOKEN | A long random value, also typed into the webhook settings at Meta |
| NEARSHOP_WHATSAPP_TEMPLATE_LANGUAGE | en until P7.11 |

In development, NEARSHOP_WHATSAPP_NOTIFICATIONS=true without a token uses the fake client: messages are logged with
the phone masked and nothing leaves the machine. The test suite always uses the fake client.

Webhook setup at Meta (App dashboard > WhatsApp > Configuration): callback URL
https://<site domain>/api/webhooks/whatsapp (through Vercel, which adds the edge secret; P2.3), the verify token
above, and a subscription to the messages field (status callbacks arrive on it; P4.4 adds incoming messages).

## Alert and retention

Each sender run computes the share of outbox rows that failed among those that finished in the last hour. Above 5%
it reports the warning event "WhatsApp notification failure rate above 5% over the last hour" to Sentry (tag
alert=whatsapp_failures, fingerprint whatsapp-failure-rate), at most once every 15 minutes while it stays above. The
WhatsApp alert of plan section 8.10 is a Sentry alert rule on that event, configured and test-fired at plan step 4-A
(docs/runbooks/monitoring.md).

notification_outbox rows never hold the phone number. The two variables are cleared when a row is sent, fails or is
cancelled, and the row is deleted 30 days after the notification (TTL index created_ttl_30d).
