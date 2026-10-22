# Retention, export, deletion and nomination

This page lists each data-retention rule NearShop applies and what enforces it in the code: an index, a job or a request handler. It follows plan section 8.4 (retention) and 8.5 (deletion and export). It is an engineering record, not the privacy notice. The privacy notice (docs/legal/privacy-v*.md, plan step 3-A) is the text users read, and it must agree with this page. Periods marked "confirm with counsel" or "confirm with the CA" are the plan's reading and wait for that review.

When a rule, index or job changes, update this page in the same pull request. The tests in backend/tests/test_privacy.py check the TTL index periods and the job's effects.

## Retention rules

| Data | Rule | Enforced by |
|---|---|---|
| search_history: location | Stored rounded to 2 decimals (about 1.1 km) | backend/app/services/search.py, log_search (LOCATION_DECIMALS) |
| search_history: user_id | Stored only while the user's personalised-suggestions consent is in force. Removed from every row of that user when they withdraw it. Removed from all rows older than 30 days | log_search checks the consent; set_consent unlinks on withdrawal; the nightly retention job (run_retention) removes it after 30 days |
| search_history rows | Deleted 180 days after created_at | TTL index created_ttl_180d on search_history.created_at (expireAfterSeconds 15552000), built by migration 4 (backend/database/migrations/m0004_retention_ttl.py), which replaced the plain index created |
| notifications | Deleted 90 days after created_at | TTL index created_ttl_90d on notifications.created_at (expireAfterSeconds 7776000), built by migration 4 |
| notification_outbox (WhatsApp messages, P4.3): the template, its variables (the notification's title and text) and the send and delivery times | The variables are cleared once the row is sent, failed or cancelled. Rows are deleted 30 days after created_at, and at once when the account is deleted | The sender (backend/app/services/whatsapp.py) clears params; TTL index created_ttl_30d on notification_outbox.created_at (expireAfterSeconds 2592000), built by ensure_schema; anonymise in backend/app/services/privacy.py deletes the user's rows |
| whatsapp_status_pending (Meta status callbacks waiting for their message id) | Deleted once applied, and at expires_at, 1 hour after they arrived. They hold Meta's message id, the status, its time and an error code, no personal data | backend/app/services/whatsapp.py apply_pending; TTL index expires_ttl |
| Order contact_phone, delivery_address and note | Cleared 180 days after the order closes (delivered, cancelled or returned to the shop), unless an open grievance refers to the order. The delivery point is rounded to 2 decimals at the same time, and contact_cleared_at records when | The nightly retention job (run_retention, clear_order_contact). An open grievance is a grievances row (P3.8) whose status is not resolved and whose order_id or order_code names the order |
| Orders and reservations (amounts, items, shop) | Kept 8 years as transaction records (confirm with the CA) | Not deleted by any job. A later prompt adds the 8-year removal |
| consents | Append-only, kept as the record of what the user agreed to | backend/app/api/routes/auth.py and app/services/privacy.py only ever insert rows |
| Admin audit trail (admin_audit) | 1 year (confirm with counsel) | TTL index created_ttl_1y (P2.4) |
| rate_limits | Until the window ends | TTL index expires_ttl (P2.3) |
| KYC documents | While the shop account is live, plus 1 year after closure (confirm with counsel) | P3.3. Deleting an owner's account keeps them |
| Off-site backups | 30 daily plus 12 monthly; deleted data ages out of backups by these dates | backend/database/backup.py (P1.6) |

### The nightly retention job

The job is named retention in backend/app/jobs.py. It runs under the job lease (P1.7), so only one process runs it, once per day at 21:30 UTC (03:00 IST), or on the first tick after that if no process was running then. The lease's last_result records the slot it ran for (ran_for) and its counts: searches_unlinked, orders_cleared and orders_held_by_grievance. A failed run is retried an hour later. Its lease document in job_leases (_id retention) shows when it last ran and what it did.

## Data export

GET /api/me/export returns one JSON file. It holds the profile (with signed-in devices), every consent row and the consents in force, the nominee, an owner's shops, and the user's reservations, orders, reviews, notifications, WhatsApp messages (the notification outbox, P4.3) and search history. Passwords, session ids and second-factor material are never included. Users get it from the account page (Download my data). It is limited to 3 a day per user (backend/app/core/ratelimit.py, rule data_export). Search-measurement events (P5.6) are not in the export, because nothing links them to the account.

## Correction

Users correct their name and home location on the account page (/account, "Your details"), which calls PATCH /api/auth/me. A phone change takes effect at once and ends every other session (this device continues on a new one). No code is sent to the new number yet; that check comes with phone verification (P4.1). Support corrects anything else on request, after checking that the request comes from the account holder.

## Consent withdrawal

The account page has a toggle for each optional purpose: WhatsApp messages, personalised suggestions and search measurement. Turning one off calls DELETE /api/auth/consents/{purpose}, and turning it on calls PUT /api/auth/consents/{purpose}. Each change appends a new consents row against the current notice version, and it takes effect on the next request. The required purpose (account and order fulfilment) cannot be withdrawn except by deleting the account.

## Account deletion

POST /api/auth/delete needs the current password. GET /api/auth/deletion, which the account page shows before the user confirms, says which of these outcomes applies:

| Situation | Result |
|---|---|
| Nothing open | Done at once. The profile becomes "Deleted user". The email is replaced by deleted-{id}@deleted.invalid, a placeholder that identifies no one and keeps the unique email index. Phone, password hash, home location, nominee and sessions are removed, the account is inactive, the token version is bumped, and every optional consent is withdrawn by a new row. Notifications, their WhatsApp outbox rows and search history are deleted. Reviews keep their rating, lose their comment and show "Former customer". Reservations and orders stay for the shop, with their phone, address and note removed. sales_history.customer_id is cleared |
| Open reservations (REQUESTED, CONFIRMED, READY_FOR_PICKUP) or cancellable orders (PENDING, SHOP_CONFIRMED) | 409 deletion_blocked lists them. "Cancel all and delete" (cancel_open true) cancels them, which releases their stock, then continues |
| Orders PREPARING, OUT_FOR_DELIVERY or DELIVERY_FAILED | The deletion is scheduled, and the user sees the date, 7 days ahead. The process_deletions job (every tick, under its lease) runs it as soon as those orders close. If they are still open on the date, it sets deletion_flagged_at on the user and sends every admin a deletion_overdue notification. An admin resolves the orders, and the deletion then runs on the next tick |
| Owner | The same rules apply to the shop's open reservations and orders: requests are rejected and the rest cancelled, as the owner. Then the shop is hidden, its listings are deactivated and the owner is anonymised as above. KYC is kept per the table above |
| Admin | Refused (403 admin_self_delete). Another admin removes the account with the operator CLI |

## Nomination

Under DPDP Act section 14 (the plan's reading; confirm with counsel), a user may name one nominee to exercise their rights after their death or incapacity. The nominee has a name and a phone number or an email address. Users name, replace or remove the nominee on the account page, which calls PUT or DELETE /api/auth/nominee. The nominee is stored on the user (users.nominee), is included in the export and is removed when the account is deleted.

Support acts on a nomination only when all of the following hold:

1. The request comes from the phone number or email address recorded for the nominee, and it names the account.
2. The nominee sends documentary proof: a death certificate, or a medical certificate or court order showing incapacity, and their own government photo identity matching the recorded name.
3. A second team member checks the documents before any action. The request, the documents seen and the action taken are recorded in the support log. The documents themselves are deleted once checked.

The nominee may then ask for an export, a correction or a deletion, which support carries out with the same tools as for the user. Support never shares the user's password or sign-in access.
