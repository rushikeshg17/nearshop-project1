# WhatsApp message templates

The WhatsApp templates NearShop sends, for approval at Meta (B8) and for the code that sends them, and how shops
reply. Plan section 8.7; P4.3 (outbound notifications); P4.4 (replies from shops and the daily stock check); P4.1
(the login code). Every notification template is submitted in English (en) and Kannada (kn), all under the
**utility** category. The code is in backend/app/services/whatsapp.py (notifications and replies) and
backend/app/services/messaging.py (the login code). P9.3 sets the stock check's cadence (daily inside the shop's
service window, otherwise weekly) and lists the Phase 9 templates, written in English for the team to translate.

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

Since P7.11 the variables are in the recipient's language (User.language, en or kn; the text comes from
backend/app/services/notification_text.py). A Kannada-preference user is sent the kn version of the template; everyone
else gets the language in NEARSHOP_WHATSAPP_TEMPLATE_LANGUAGE (en). The exception is a kind whose message still awaits
the team's Kannada (notification_text.AWAITING_KANNADA, for example arrival_failure and low_stock_alert): its text is
English for everyone and its row always uses the en template, since Meta has no kn version of it yet
(whatsapp._row_language). Approval of every template at Meta in both languages is a precondition for offering Kannada
to users: until a template's kn version is approved, a kn send of it is rejected and its outbox row marked failed.

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

Have a native Kannada speaker read every Kannada body and button before submission (the stock check's too).

### nearshop_shop_stock_check (shop, weekly stock check)

Kind stock_check, to the shop's owner (P4.4; cadence P9.3, see "The stock-check cadence" below). Considered once a
day from the shop's opening time (IST, read from its opening hours; 9:00 AM when they cannot be read) until 3 hours
after it, when the shop has active listings counted more than 48 hours ago. This template goes only to a shop whose
owner's service window is not open, and only on the weekly check day; a shop whose window is open gets the same
words daily as a free-form message instead. {{2}} lists up to 5 listings, oldest count first, with their counts. The
owner answers with a stock message (below). Submitted as **utility**; Meta may class it as **marketing** (see
Rates).

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: time for a quick stock check.<br><br>{{1}}.<br>{{2}}<br><br>Reply with each item's name and its count, one per line. Nothing changes until you reply Yes to the summary we send back. | NearShop: ಒಂದು ಸಣ್ಣ ಸ್ಟಾಕ್ ಪರಿಶೀಲನೆಯ ಸಮಯ.<br><br>{{1}}.<br>{{2}}<br><br>ಪ್ರತಿ ವಸ್ತುವಿನ ಹೆಸರು ಮತ್ತು ಅದರ ಸಂಖ್ಯೆಯನ್ನು ಸಾಲಿಗೊಂದರಂತೆ ಉತ್ತರಿಸಿ. ನಾವು ಕಳುಹಿಸುವ ಸಾರಾಂಶಕ್ಕೆ ನೀವು Yes ಎಂದು ಉತ್ತರಿಸುವವರೆಗೆ ಏನೂ ಬದಲಾಗುವುದಿಲ್ಲ. |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | Stock check for the shop | Stock check for Sri Ganesh Hardware |
| {{2}} | The listings, counts and age, on one line (at most 400 characters: long names are shortened to fit, so every listing is shown). How to reply is the body's last line, so {{2}} does not repeat it | Are these counts still right? Samsung 25W Charger: 3 (counted 5 days ago); Astral PTFE Thread Seal Tape: 50 (counted 3 days ago) |

### Not sent on WhatsApp

Every other notification stays in the app only: pickup complete, cancellations the customer made, the shop's own
expiry notice, delivery-code reminders, grievances, shop verification, phone-migration warnings and admin notices.

## Other templates

| Template | Category | Variables | Where |
|---|---|---|---|
| The login code (NEARSHOP_WHATSAPP_OTP_TEMPLATE) | authentication | {{1}}: the code; copy-code button with the code | P4.1, backend/app/services/messaging.py |
| nearshop_shop_stock_check | utility (Meta may class it as marketing) | {{1}}, {{2}}: see above | P4.4, P9.3 |

The Phase 9 templates are in "Phase 9 templates" below. Phase 10 adds its templates there too.

## The stock-check cadence (P9.3)

Plan section 8.17. The stock check is free for every shop, on every plan, including after a plan lapses. To keep it
affordable when Meta classes the template as marketing, it is sent where WhatsApp does not charge for it whenever it
can be (backend/app/services/whatsapp.py send_stock_checks, run by the stock_check job every 30 seconds under its
lease):

1. **Daily, inside the window.** At the shop's opening hour, when the owner's 24-hour customer-service window will
   still be open at least 30 minutes later, the check goes as a free-form text inside it: "NearShop: {{1}}." then
   {{2}} then the bot's usual how-to-reply text, in the owner's language (English or Kannada, from
   backend/app/services/notification_text.py). The window is the owner's own phone's: the time of the last message
   from the owner's verified phone (whatsapp_sessions owner_inbound_at). A message from the shop's
   call-back-verified number opens that number's window, not the owner's, so it does not count. If the window has
   closed by the time the outbox sends the row, the row is cancelled (window_closed), never sent as a template.
2. **Weekly, as the template.** Every other shop with a stale listing gets nearshop_shop_stock_check once a week, at
   the opening hour of the weekly check day, NEARSHOP_STOCK_CHECK_WEEKLY_DAY (default Monday). A shop closed on that
   day gets it the next day.
3. **Limits.** At most one check per shop per IST day (whatsapp_stock_checks claims the shop's day once), none on the
   shop's closed day (closed_on), and none when no active listing is older than 48 hours. On other days a shop
   without an open window gets nothing (decision waiting).

**What each send records.** Every notification_outbox row has route (template, or in_window for a free-form text)
and category (the category it was submitted under: utility for a template, service for a free-form text). Meta's
status callbacks carry a pricing object; the row records the category Meta reports (meta_category: utility,
marketing, authentication or service), billable and pricing_type (for example regular or free_customer_service),
and cost_estimate is set from them (₹0 when not billable, service, or a free pricing_type such as
free_customer_service or free_entry_point; otherwise the rate of Meta's category, below). The day's
whatsapp_stock_checks row records the decision (in_window, template, fresh, unreachable, closed or waiting) and the
route. The share of stock checks sent inside the window is the stock_check rows with route in_window over all
stock_check rows sent in the period.

Outbox rows are deleted 30 days after created_at, and whatsapp_stock_checks rows after 3 days, so neither can give a
whole calendar month after it ends: on the last day of a 31-day month its first day is already gone. Measure over the
last 30 days at most, and run the query on the day the measurement is due, not later. For the June 2027 acceptance
check (stock checks per shop and the in-window share), run it on 30 June 2027 with created_at from 1 June 2027 IST:
June has 30 days, so all its rows are still kept that day, but not on 1 July. For a 31-day month, or any figure
needed later, save the counts daily before the rows expire.

**Cost, both cases** (rates below; per shop with stale listings, a 30-day month, about 4.3 weekly days):

| Shop | Utility | Marketing |
|---|---|---|
| Window open at opening time every day (30 free-form checks) | ₹0 | ₹0 |
| Window never open (4.3 templates) | 4.3 × ₹0.160 = ₹0.69 | 4.3 × ₹0.970 = ₹4.17 |
| Before P9.3, the template every day (30 templates), for comparison | ₹4.80 | ₹29.10 |

For 300 shops that never open a window, about 1,300 templates a month: ₹208 as utility, ₹1,261 as marketing, the
plan's section 11 budget. Every shop whose window is open at opening time costs nothing for that day. Shops that
reply every day get up to 30 checks a month; the rest get at most 5 (a month has at most 5 of any weekday).
Results after release (fill in): the category Meta gave the template, and for June 2027, the share of checks sent
inside the window and the largest number of checks any shop received minus its in-window days (target: 5 or fewer).

## Phase 9 templates

Written for P9.3, to be submitted to Meta under B23 by 15 May 2027 (for the prompts named, which send them). All are
submitted as **utility**. The Kannada column is empty: the team writes it, and a native Kannada speaker reads it
before submission. Variables follow the same rules as above: one line each, at most 400 characters, never a rupee
value of a shop's sales. Each line ends with whether the wording risks Meta classing it as **marketing**, and what a
send would then cost (₹0.970 instead of ₹0.160 outside the window; inside an open window a utility template is free,
a marketing one never is).

### nearshop_shop_statement_free (shop on Free, monthly statement; P9.9)

To the owner of a shop on Free (and to every shop before billing starts), on the 1st of the month. Four quick-reply
buttons, whose payloads P9.9 reads (statement:<id>:accurate, :not_accurate, :would_pay, :not_now).

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: your statement for {{1}}.<br><br>{{2}}<br><br>Amount due: ₹0. You are on the Free plan, and every stock tool stays free.<br>{{3}}<br><br>Is this statement accurate? Would you pay for Growth? Tap a reply below. | |
| Button 1 (quick reply) | Accurate | |
| Button 2 (quick reply) | Not accurate | |
| Button 3 (quick reply) | I'd pay | |
| Button 4 (quick reply) | Not now | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The month and the shop | May 2027, Sri Ganesh Hardware |
| {{2}} | The month's counts, and the top 3 "asked for near you" items when the P9.7 report is shown | 2 reservation requests (1 collected), 0 delivery orders, 4 call taps and 3 directions taps. Asked for near you: PVC elbow, wall putty, LED bulb. |
| {{3}} | The founding-price line (the price the shop qualifies for) | Growth would cost ₹83 a month at the founding price (₹999 for the first year). |

Marketing risk: **high**. The founding-price line and the "I'd pay" button promote a paid plan, which Meta's
guidelines treat as marketing. As marketing, ₹0.970 a send, always charged: about ₹97 a month for 100 Free shops,
against ₹16 as utility. To lower the risk, keep {{3}} a plain statement of the price (no "offer ends", no
exclamation marks), and send the statement inside the shop's window when it is open.

### nearshop_shop_statement_roi (paying shop, monthly ROI statement; P9.9)

To the owner of a shop on a paid plan, on the 1st of the month. Two quick-reply buttons (statement:<id>:accurate,
:not_accurate).

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: your statement for {{1}}.<br><br>{{2}}<br><br>Your plan: {{3}}<br>Outcome guarantee: {{4}}<br><br>Is this statement accurate? Tap a reply below. | |
| Button 1 (quick reply) | Accurate | |
| Button 2 (quick reply) | Not accurate | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The month and the shop | September 2027, Sri Ganesh Hardware |
| {{2}} | The month's counts and the customer requests so far this quarter | 6 reservation requests (5 collected), 2 delivery orders, 14 call taps and 9 directions taps. Customer requests this quarter: 8. |
| {{3}} | The plan and its end date | Growth, paid until 31 Jul 2028 |
| {{4}} | Progress against the guarantee in the current quarter | 8 customer requests this quarter, 3 needed: met. A paid quarter that ends with fewer than 3 adds 3 months free. |

Marketing risk: **low**. It reports on a service the shop already pays for. As marketing, ₹0.970 a send (about
₹19 a month for 20 paying shops, against ₹3 as utility). Keep upgrade or Boost offers out of it.

### nearshop_shop_demand_summary (Growth shop, weekly "asked for near you"; P9.9)

To the owner of a shop on Growth or above, after the weekly P9.7 report is built (Monday), only while the report is
shown. No buttons.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: asked for near you this week.<br><br>{{1}}<br>{{2}}<br><br>People near your shop searched for these and could not find them in stock. Open Insights in NearShop for the full list. | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The shop and the report's week | Sri Ganesh Hardware, week of 7 Jun 2027 |
| {{2}} | Up to 5 items with their bands of distinct people (never a count below 5) | PVC elbow 1 inch (10 to 19 people); wall putty (5 to 9); LED bulb 9W (5 to 9) |

Marketing risk: **medium**. It is a report the plan pays for, but suggesting items to stock could read as promotion.
Keep it factual and do not name brands' offers. As marketing, ₹0.970 a send, about 4.3 a month per shop: ₹4.17 a
month per Growth shop, against ₹0.69 as utility.

### nearshop_shop_arrival_report (shop, a customer found the item missing; P9.1)

Kind arrival_failure (P9.1), to the shop's owner, when a customer reports "Not available when I arrived" on a
confirmed or ready reservation. No buttons; the owner answers with a stock message, as for the stock check.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: a customer could not find an item at your shop.<br><br>{{1}}.<br>Details: {{2}}<br><br>The listing shows "stock not confirmed" until you update its count. Reply with the item's name and count, or update it in NearShop. | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The notification's title | Item not available for reservation R-7KQ2MX |
| {{2}} | The notification's text: the listing and the count NearShop showed | The customer found Astral PTFE Thread Seal Tape missing. NearShop showed 50 in stock. |

Marketing risk: **low**: it reports an event in a transaction the shop took part in. As marketing, ₹0.970 a send.

### nearshop_shop_payment_request (shop, payment request; P9.5)

To the owner, when they or an admin create a payment request.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: payment request {{1}}.<br><br>{{2}}<br>Amount: {{3}}<br><br>Pay by UPI to {{4}}, with {{1}} as the payment note. NearShop never collects cash. | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The request number | PR-2027-00042 |
| {{2}} | What it is for | Growth plan, 1 Aug 2027 to 31 Jul 2028 (founding price) |
| {{3}} | The amount | ₹999 |
| {{4}} | NearShop's UPI handle and payee name (settings) | nearshop@examplebank (NearShop Technologies) |

Marketing risk: **low**: a request the owner asked for. As marketing, ₹0.970 a send.

### nearshop_shop_payment_receipt (shop, receipt link; P9.5)

To the owner, when a payment is matched and its receipt or tax invoice is issued.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: we received your payment.<br><br>{{1}}<br>Receipt: {{2}}<br><br>Keep this link for your records. | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The payment and what it paid for | ₹999 for request PR-2027-00042: Growth plan until 31 Jul 2028 |
| {{2}} | The receipt or invoice number and its link | NS-R-2027-0007, https://<site domain>/shop/billing/receipts/7 |

Marketing risk: **low** (a receipt). As marketing, ₹0.970 a send.

### nearshop_shop_plan_renewal (shop, renewal reminders; P9.4)

To the owner 30, 7 and 1 days before a paid plan ends. One template for all three.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: your plan ends soon.<br><br>{{1}} ends on {{2}}.<br>{{3}}<br><br>Your shop stays listed after that, on the Free plan, with every stock tool. | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The plan | Your Growth plan for Sri Ganesh Hardware |
| {{2}} | The end date and days left | 31 Jul 2028 (7 days left) |
| {{3}} | How to renew | To renew, open Plan and billing in NearShop and create a payment request. |

Marketing risk: **medium**. Renewal reminders for an existing subscription are usually utility, but a renewal
discount or upgrade pitch would make it marketing; keep {{3}} to how to renew. As marketing, ₹0.970 a send, 3 a year
per paying shop.

### nearshop_shop_plan_lapsed (shop, lapse notice; P9.4)

To the owner when a plan lapses and the shop moves to Free.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: {{1}} ended on {{2}}.<br><br>Your shop is now on the Free plan. It stays exactly as visible to customers, and every stock tool stays free. {{3}} | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The plan | Your Growth plan for Sri Ganesh Hardware |
| {{2}} | The end date | 31 Jul 2028 |
| {{3}} | What stopped | The weekly demand report, low-stock alerts and extra logins are paused until you renew. |

Marketing risk: **low to medium** (an account change; avoid a win-back offer in it). As marketing, ₹0.970 a send.

### nearshop_shop_low_stock (Growth shop, daily low-stock alert; P9.14)

To the owner of an entitled shop, daily at its alert hour, when listings are at or below their low-stock level.
Inside an open window P9.14 sends the same words as a free-form message instead.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: low stock at {{1}}.<br><br>{{2}}<br><br>Reply with each item's name and its new count to update NearShop, or update them in NearShop. | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The shop | Sri Ganesh Hardware |
| {{2}} | Up to 10 listings, lowest first, with their counts and low-stock levels | Astral PTFE Thread Seal Tape: 1 (alert at 2); LED bulb 9W: 0 (alert at 3) |

Marketing risk: **low**: an alert the owner turned on about their own stock. As marketing, ₹0.970 a send, up to 30
a month per Growth shop (₹29.10, against ₹4.80 as utility), less on days the window is open.

### nearshop_staff_invitation (staff, invitation; P9.13)

To the invited phone, when an owner invites a staff member. The invitee has not messaged NearShop and may have no
account, so this always goes as a template outside any window.

| | English (en) | Kannada (kn) |
|---|---|---|
| Body | NearShop: {{1}} invited you to help run {{2}} on NearShop.<br><br>To accept, sign in at {{3}} with this phone number. If you did not expect this, ignore this message. | |

| Variable | Holds | Example |
|---|---|---|
| {{1}} | The owner's first name | Ramesh |
| {{2}} | The shop | Sri Ganesh Hardware |
| {{3}} | The sign-in link | https://<site domain>/login |

Marketing risk: **low to medium**: Meta may treat a message to someone who has not opted in as promotional. As
marketing, ₹0.970 a send. P9.13 must record the owner's statement that the invitee agreed to be messaged.

### Kannada still to write

Every Phase 9 template above has an empty Kannada column: the bodies and buttons of nearshop_shop_statement_free
(4 buttons), nearshop_shop_statement_roi (2 buttons), nearshop_shop_demand_summary, nearshop_shop_arrival_report,
nearshop_shop_payment_request, nearshop_shop_payment_receipt, nearshop_shop_plan_renewal, nearshop_shop_plan_lapsed,
nearshop_shop_low_stock and nearshop_staff_invitation. The variables are filled in the owner's language by the code
(P7.11), except for the kinds in notification_text.AWAITING_KANNADA, which are sent in English with the en template
until the team's Kannada is added and its kn template approved. Record Meta's outcome and category for each after submission (B23).

## Replies from shops (P4.4)

Meta sends every message a phone sends to NearShop's number to POST /api/webhooks/whatsapp (the messages field),
signed like the status callbacks. backend/app/services/whatsapp.py handle_messages acts on each one once: its message
id is claimed in whatsapp_inbound before anything is done, so Meta's retries and duplicates change nothing. A message
is handled only while WhatsApp is on (NEARSHOP_WHATSAPP_NOTIFICATIONS).

Who is speaking: a message counts as the shop's only when it comes from the owner's verified phone (P4.1, the one the
notifications go to) or from the shop's own number after an admin verified it by calling back (P3.3), and the
owner's account is active. Every other number gets a polite reply with the support number (the grievance officer's
phone, NEARSHOP_GRIEVANCE_OFFICER_PHONE; without it, a pointer to Help on the website), and nothing else happens.

| The shop sends | What happens | The bot replies |
|---|---|---|
| Confirm or Decline on nearshop_shop_new_reservation | The reservation is confirmed (stock held) or rejected, as the owner, in one transaction; its status event has the note "via WhatsApp". A request whose 20 minutes ran out is expired first. | The result, or why nothing changed (already answered, expired, not enough stock) |
| Lines of item name and count ("Thread seal tape 20", "Charger: 5", "4 x Notebook") | Each line is matched to the shop's listings with the importer's matcher and thresholds (backend/app/services/importer.py): an exact name or one match scoring 86 or more is taken; otherwise the listings scoring 72 or more are candidates. Nothing changes yet. At most 20 lines. | The matched listings with the new and current counts, and "Reply Yes" |
| A line with several or weak candidates | Held until it is answered | Up to 3 numbered candidates: reply 1 to 3, or 0 to leave the line out |
| Yes (or ok, haan, houdu) to the summary, within 30 minutes | set_stock for each listing with reason whatsapp_update and the owner as the actor, all in one transaction: one inventory_events row per changed count | The counts saved |
| No | The summary is dropped | Cancelled |
| Anything else | Nothing | How to send a stock update |

Replies are free-form text messages, sent only inside the shop's 24-hour customer-service window (they are answers to
a message the shop has just sent, so Meta does not charge for them). A reply that would fall outside the window (a
webhook call Meta retried a day later) is not sent. The bot's replies are in the owner's language (English or Kannada, from
backend/app/services/notification_text.py, P7.11).

What is stored: whatsapp_inbound keeps each message id, its type, the outcome and whether the reply went, never the
number or the text, for 8 days (Meta retries for up to 7). whatsapp_sessions keeps, per owner, the time of their
last message, the time of the last one from the owner's own verified phone (owner_inbound_at, the window the stock
check uses, P9.3) and the summary waiting for Yes (listing names and counts), removed 2 days after the last message.
whatsapp_stock_checks records that a shop's stock check was considered on an IST day, with the decision and route
(3 days).

## Rates

India per-message rates, as published by the business service provider Interakt (Starter plan), **read on
18 Oct 2026** (plan section 8.7). They were not re-read for P4.3 or P4.4 (22 Oct 2026). Meta's own INR rate card (a
CSV) could not be read and may be lower. Recheck both before budgeting.

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

**The stock check under both categories.** It is submitted as utility, but Meta decides the category and may
class it as marketing. Since P9.3 it goes daily only as a free-form message inside an open window (no charge in
either case) and otherwise as the template once a week: see "The stock-check cadence" for the costs. Record the
category Meta gives when the template is approved. NEARSHOP_WHATSAPP_MARKETING_COST_INR (default 0.97) is the
marketing rate recorded on a row when Meta's status callback reports the marketing category.

Bot replies to shops (P4.4) are free-form messages inside the service window the shop's own message opened: no
charge, and the code records none.

## Configuration

Off by default. Nothing is sent until all of these are set on the environment (Render, sync: false for secrets):

| Variable | What |
|---|---|
| NEARSHOP_WHATSAPP_NOTIFICATIONS | true to write outbox rows and send them |
| NEARSHOP_WHATSAPP_ACCESS_TOKEN | The Cloud API system-user token (also used by P4.1) |
| NEARSHOP_WHATSAPP_PHONE_NUMBER_ID | The sending number's id (also used by P4.1) |
| NEARSHOP_WHATSAPP_APP_SECRET | The Meta app's secret: the webhook refuses every call without it |
| NEARSHOP_WHATSAPP_WEBHOOK_VERIFY_TOKEN | A long random value, also typed into the webhook settings at Meta |
| NEARSHOP_WHATSAPP_TEMPLATE_LANGUAGE | The template language for users who have not chosen Kannada (en); Kannada users get kn |
| NEARSHOP_STOCK_CHECK_WEEKLY_DAY | The weekday the stock-check template goes to shops without an open window (Monday; P9.3) |
| NEARSHOP_WHATSAPP_MARKETING_COST_INR | The marketing rate recorded when Meta reports a message as marketing (0.97) |

In development, NEARSHOP_WHATSAPP_NOTIFICATIONS=true without a token uses the fake client: messages are logged with
the phone masked and nothing leaves the machine. The test suite always uses the fake client.

Webhook setup at Meta (App dashboard > WhatsApp > Configuration): callback URL
https://<site domain>/api/webhooks/whatsapp (through Vercel, which adds the edge secret; P2.3), the verify token
above, and a subscription to the messages field (status callbacks and the shops' replies both arrive on it).
NEARSHOP_GRIEVANCE_OFFICER_PHONE is the support number the bot gives unknown numbers (P3.8 sets it on production).

## Alert and retention

Each sender run computes the share of outbox rows that failed among those that finished in the last hour. Above 5%
it reports the warning event "WhatsApp notification failure rate above 5% over the last hour" to Sentry (tag
alert=whatsapp_failures, fingerprint whatsapp-failure-rate), at most once every 15 minutes while it stays above. The
WhatsApp alert of plan section 8.10 is a Sentry alert rule on that event, configured and test-fired at plan step 4-A
(docs/runbooks/monitoring.md).

notification_outbox rows never hold the phone number. The two variables are cleared when a row is sent, fails or is
cancelled, and the row is deleted 30 days after the notification (TTL index created_ttl_30d).
