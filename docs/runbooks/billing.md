# Billing: payments to NearShop, receipts, invoices and refunds

How shops pay NearShop's own fees, how every credit is matched, which documents are issued, and what to do when
something does not fit. Code: backend/app/services/billing.py (P9.5) and backend/app/services/gateway.py (UPI Autopay, P9.6). Plan section 8.18; decisions B5 (the GST
position) and B22 (NearShop's merchant UPI QR and the document formats approved by the CA).

This route only ever collects NearShop's own fees into NearShop's own current account. It never collects money for
a shop: customers pay shops directly (P6.1, plan section 8.8). Agents never collect cash.

## Payment route

1. The owner (or an admin, for them) creates a payment request for a plan year: Owner > Plan, or
   POST /api/owner/billing/requests; admins POST /api/admin/billing/requests with a reason, which is kept on the
   request (reason, with created_by admin).
   - The amount is the plan's list price in the catalogue in force, or the founding price (Growth, ₹999 for the
     first year) while the shop qualifies (P9.4). GST is added only when NearShop is registered (below).
   - Before NEARSHOP_BILLING_STARTS_ON (1 Aug 2027) the request is an advance; from then on it is a new plan, or a
     renewal of the shop's active plan (the same plan and period).
   - What a payment becomes follows the day the money was credited, not the day it is matched: credited before
     billing starts, it is an advance (a payment on 31 Jul matched on 2 Aug is still an advance, and the
     subscriptions job starts it from 1 Aug); credited on or after that day, it is a shop-paid plan from the match,
     and its receipt describes it so, even if the request was made as an advance.
   - Each request has a unique number, NSP and six digits (NSP000123). A new request cancels the shop's open ones.
   - Monthly Growth is not paid this way: it is UPI Autopay through a gateway (P9.6, below).
2. The owner pays by UPI to NearShop's merchant UPI ID and payee name (NEARSHOP_BILLING_UPI_ID,
   NEARSHOP_BILLING_PAYEE_NAME), with the request number as the payment note. The screen (P9.10) builds the UPI
   link and QR from these with frontend/src/lib/upi.ts.
3. An admin uploads the bank statement (below) at least every working day. Each credit is matched to its request;
   the payment, its subscription change (P9.4) and its document are written in one transaction.

Merchant discount rate: UPI merchant payments of ₹2,000 or less carry none; larger ones 0.4%, capped at ₹300
(business/BUSINESS_MODEL.md section 8.2). Every plan price today is below ₹2,000 except Pro and Business.

## UPI Autopay for Growth monthly (P9.6)

Monthly Growth is paid by UPI Autopay through an RBI-authorised payment gateway (Razorpay subscriptions, whose UPI
Autopay holds the mandate). NearShop stores no card or bank detail: the gateway holds the mandate. The money reaches
NearShop's current account through the gateway's settlement, less its fee. It is NearShop's own fee, never money for
a shop.

### The flow

1. From NEARSHOP_BILLING_STARTS_ON (1 Aug 2027) the owner chooses Growth monthly (Owner > Plan, or
   POST /api/owner/billing/autopay). The server creates a gateway subscription for the monthly price in the
   catalogue in force (₹149; GST added once NearShop is registered) and answers with the gateway's checkout link.
   The owner approves the mandate in their UPI app there. Asking again while it waits for approval returns the same
   link. A shop on a yearly plan, or with an advance, can start monthly Autopay only when that plan ends; a shop whose
   earlier monthly plan is still running is renewed from the end of that month.
2. The gateway sends its events to POST /api/webhooks/autopay (set this URL, on the site's domain, in the gateway's
   webhook settings, with the events below and the webhook secret). Each call must carry a valid
   X-Razorpay-Signature (HMAC-SHA256 of the raw body with NEARSHOP_AUTOPAY_WEBHOOK_SECRET); each event id
   (X-Razorpay-Event-Id) is processed once, so the gateway's retries and replays change nothing.

| Gateway event | What NearShop does |
|---|---|
| subscription.authenticated, subscription.activated | The mandate is authorised; the owner is told. A late one for a halted or cancelled mandate changes nothing. |
| subscription.charged | A debit on a halted mandate, or for a gateway subscription NearShop has no mandate for, is not applied: it goes to the unmatched queue at once ("Autopay debit not applied"). Otherwise the debit becomes a payment, the next month of the subscription (the first month on the first debit) and a receipt or tax invoice, in one transaction, through the same code as a bank payment. The receipt says "by UPI Autopay". |
| subscription.pending | A debit failed and the gateway will retry. The owner is told once. The plan is held, with its extras, for NEARSHOP_AUTOPAY_GRACE_DAYS (3) after its end date while the gateway retries; a debit in that time continues the plan without a break. |
| subscription.halted | The gateway's last retry failed. The mandate is halted, the plan lapses to Free at once (the shop keeps every stock tool and its place in search), and the owner is told how to restart: set up Autopay again, or pay for a year. NearShop then cancels the gateway subscription, so the halted mandate does not stay registered in the owner's UPI app; if that call fails (a warning "could not cancel gateway subscription … cancel it in the gateway's dashboard" in the logs), cancel it by hand in the gateway's dashboard. |
| subscription.cancelled, subscription.completed | The owner cancelled the mandate in their UPI app, or its debits ran out (NEARSHOP_AUTOPAY_TOTAL_COUNT, 60). The plan ends at the end of the paid month. |

3. The owner can also cancel in NearShop (Owner > Plan, or POST /api/owner/billing/autopay/cancel): the gateway
   subscription is cancelled first, then the mandate; the plan runs to the end of the paid month, then lapses to Free.
4. No "your plan ends" reminders go out under an authorised mandate: the gateway sends the pre-debit notice.
5. Deleting an owner's account first cancels every mandate of their shops (created, authorised or halted) at the
   gateway, then in NearShop, with no notice. While the gateway cannot be reached, or Autopay is switched off with
   mandates still registered, the deletion waits: the owner's request answers 502 or 503, and a scheduled deletion
   is retried on the next process_deletions tick (warning "deletion of user N waits"). If a deletion keeps waiting,
   cancel the subscription in the gateway's dashboard, then mark the mandate cancelled with the operator's help.

### Fee per debit

Razorpay's pricing page (read 18 Oct 2026, plan section 11): 2% per transaction plus 18% GST on the fee, about ₹3.52
on a ₹149 debit (₹2.98 fee and ₹0.54 GST), so ₹145.48 reaches the current account. Twelve monthly debits cost about
₹42 in fees against ₹0 for a yearly UPI payment of ₹1,499 into the account (no merchant discount rate at ₹2,000 or
less), which is why the business model pushes yearly plans. Record the fee the gateway actually agrees with B22 here:

| Date | Fee per debit agreed with the gateway | Recorded by |
|---|---|---|
| | | |

### The pre-debit notice (RBI e-mandate rules)

RBI's e-mandate framework for recurring payments requires the issuer to send the payer a pre-debit notification at
least 24 hours before each debit, with the option to opt out of that debit; recurring debits up to the RBI limit
(₹15,000 at the time of writing) need no additional factor of authentication after the first. The gateway and the
payer's UPI app send this notice; NearShop does not. Confirm the current rules and the gateway's retry schedule with
the gateway before switching on, and record them here:

| Date | Rule or schedule confirmed | Confirmed with (gateway contact) | Notes |
|---|---|---|---|
| | Pre-debit notice timing and content | | |
| | Retry schedule after a failed debit (sets NEARSHOP_AUTOPAY_GRACE_DAYS) | | |
| | Limit without additional authentication | | |

### Daily reconciliation and mismatches

The billing_reconciliation job runs once per IST day. It reads the gateway's settlement reports for the last
NEARSHOP_AUTOPAY_RECONCILE_DAYS (3) days and compares them with the Autopay payment rows. Every mismatch becomes a
credit in the unmatched queue (Admin > Billing > Credits), once, with its reason; the 3-working-day alert then
applies as for any credit. Target: 0 mismatches.

Two kinds of credit come from Autopay:

- source autopay: money the gateway took and settled to NearShop (a debit not applied, or a settled payment with no
  payment row). It can be matched by hand or returned like any credit.
- source reconciliation: information about money already recorded or never received (a settled amount that differs,
  or a payment missing from every settlement report; its reference ends in /amount or /unsettled). It is not money:
  it can never be matched to a request or returned. Close it once checked:
  POST /api/admin/billing/credits/{id}/close with a reason and the gateway's reference (settlement id, support ticket
  or refund id) in bank_reference. Closed credits are listed with ?status=closed.

| Reason in the queue | What to do |
|---|---|
| Autopay debit not applied: … (the price or GST position changed since the mandate was set up, or the shop's plan was cancelled or refunded) | The money was taken. Look up the payment in the gateway's dashboard. Refund it through the gateway's dashboard and ask the owner to set up Autopay again at the current price, or grant the month by hand (Admin > Subscriptions) with a reason. Then return the credit (POST /api/admin/billing/credits/{id}/return) with the gateway refund's id as the reference. |
| Autopay debit not applied: Mandate N was halted after its failed retries | A debit after the gateway halted the mandate (NearShop cancels it at the gateway on halt, so this should not happen). Refund it through the gateway's dashboard, or, if the owner wants the month, grant it by hand; then return the credit with the gateway refund's id. Check that the halted subscription is cancelled in the gateway's dashboard. |
| Autopay debit not applied: No Autopay mandate for gateway subscription sub_… | The gateway debited a subscription NearShop does not know (made by hand in the dashboard, or of a deleted test row). Cancel that subscription in the gateway's dashboard, refund the debit there and return the credit with the refund's id. Resending the event does nothing: it was already recorded. |
| Gateway reconciliation: settled by the gateway, but no Autopay payment is recorded (source autopay) | A webhook event was lost or refused (never recorded). Check the webhook's delivery log in the gateway's dashboard and resend the event: it is then applied once. If it cannot be resent, or the gateway's log shows it was delivered, handle as "Autopay debit not applied" above. |
| Gateway reconciliation: settled ₹X, but payment N recorded ₹Y (source reconciliation) | Compare the gateway's settlement with the payment. Raise the difference with the gateway; close the credit with their reference when it is settled. |
| Gateway reconciliation: payment N is in no gateway settlement report 5 days after it was paid (source reconciliation) | Look up the payment and its settlement in the gateway's dashboard. A late settlement: close the credit with the settlement id. A payment the gateway reversed: refund the NearShop payment (POST /api/admin/billing/payments/{id}/refund), then close the credit with the reversal's reference. |

When the monthly price or the GST position changes, existing mandates keep debiting the old amount and those debits
go to the queue: before publishing a new monthly price, or before GST registration takes effect, plan how the
mandates are moved (the gateway can update a subscription's plan; owners may need to approve again).

### Refunds of Autopay payments

An Autopay payment is refunded through the gateway's dashboard (the money goes back to the payer's UPI account),
then recorded in NearShop as for any payment (POST /api/admin/billing/payments/{id}/refund, with the gateway
refund's id as the reference), which issues the refund receipt or credit note. Cancel the mandate first when the
shop is leaving the plan.

### Switching Autopay on

1. Staging: set NEARSHOP_AUTOPAY_PROVIDER=razorpay, the gateway's test-mode key id and secret
   (NEARSHOP_AUTOPAY_KEY_ID rzp_test_…, NEARSHOP_AUTOPAY_KEY_SECRET), the webhook secret
   (NEARSHOP_AUTOPAY_WEBHOOK_SECRET) and NEARSHOP_AUTOPAY_ENABLED=true. A live key id outside production stops the
   API at startup. Run the acceptance check: 10 mandates with 3 debits each, including 2 failed debits and 1
   cancellation, reconcile with 0 mismatches. Record the run below.
2. Production (1 Aug 2027, only if B25 says go): the same settings with the live keys, set only in the Render
   environment of the production API, never in the repository or on staging. Check that the first live debit
   produces its document, and record it below.

| Date | Environment | Check | Result | By |
|---|---|---|---|---|
| | staging (test mode) | 10 mandates × 3 debits, 2 failed, 1 cancelled: 0 mismatches | | |
| | production | First live debit produced its document (number) | | |

## Statement layout

Upload the current account's statement as CSV (Admin > Billing > Upload statement, or
POST /api/admin/billing/statements as a multipart file field named file). Excel exports must be saved as CSV first.

- Any number of preamble lines (account name, period) may come before the header row; the header row is found
  within the first 40 lines.
- The header row must name, in any order, at least these columns (names are matched without case; the first name
  listed wins when a statement has two):

| Column | Accepted header names |
|---|---|
| Date | Date, Txn Date, Transaction Date, Value Date, Tran Date |
| Narration | Narration, Description, Particulars, Remarks, Transaction Remarks |
| Reference | UPI Reference, UTR, UTR No, Ref No, Reference, Reference No, Chq/Ref No, Ref No./Cheque No., Cheque/Ref No |
| Credit | Credit, Deposit, Deposits, Credit Amount, Cr Amount, Deposit Amt |
| Debit (optional) | Debit, Withdrawal, Withdrawals, Debit Amount, Dr Amount, Withdrawal Amt |

- Dates: DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD, DD-Mon-YYYY, DD Mon YYYY, or the same with a two-digit year.
- Amounts in rupees, with or without thousands separators, a ₹ sign or a trailing Cr (1,499.00).
- Rows with only a debit are skipped. Rows with neither (totals, footers) and credit rows without a date or a
  reference are counted as unreadable and skipped. When the reference column is blank, the 12-digit UPI reference
  in the narration is used.
- Size limits: NEARSHOP_IMPORT_MAX_MB and NEARSHOP_IMPORT_MAX_ROWS, as for stock imports.

Each credit is matched when its narration contains a request number (NSP000123; spaces, dashes or slashes after NSP
are allowed) and its amount equals the request's total. The upload answers with the counts and, per credit,
matched, unmatched (with the reason) or duplicate. A credit whose UPI reference was uploaded before (matched or not)
is reported as a duplicate and not recorded again, so uploading overlapping statements is safe.

## When a payment cannot be matched

The unmatched queue is Admin > Billing > Credits (GET /api/admin/billing/credits). Each credit shows why:

| Reason | What to do |
|---|---|
| No payment request number in the narration | Find the request from the payer's name and the amount (ask the owner if needed), then match by hand. |
| There is no payment request NSP… | A typing mistake in the note: find the right request, then match by hand. |
| Request NSP… is cancelled or paid | A second payment or an old request: if the shop meant to pay, make a new request and match the credit to it; otherwise return it. |
| The amount … is not the request's … | Return the credit by bank transfer and ask the owner to pay the exact amount; or, if they paid too little, ask for a new payment of the full amount and return this one. |
| The price of request … has changed | The founding offer filled up or a new catalogue came into force after the request: make a new request at the current price, then match by hand if the amount fits, or return the credit. |
| The GST position changed | Registration took effect between the request and the payment: make a new request and handle as for a wrong amount. |
| Documents are still in the draft format | Production will not issue documents until the CA's format is set (below). Match by hand once it is. |

- Match by hand: POST /api/admin/billing/credits/match with the UPI reference, the request number and a reason (at
  least 10 characters). A credit not yet on any uploaded statement can be recorded here with its amount and date.
- Return: POST /api/admin/billing/credits/{id}/return with the bank transfer's reference and a reason, after the
  money has been sent back.
- Target: no credit unmatched for more than 3 working days (plan section 8.18). The billing_reconciliation job runs
  once per IST day and alerts every admin in the app (and with a "bank credit unmatched" warning event) about each
  credit still unmatched 3 working days (Monday to Friday, except NEARSHOP_BILLING_BANK_HOLIDAYS) after the day it
  was credited, once per credit. Resolve each alert the same day.

## Documents

The document type follows NearShop's GST position on the payment's date:

| Position (B5) | Payment | Refund |
|---|---|---|
| Unregistered (today, NEARSHOP_BILLING_GST_MODE=unregistered) | Payment receipt stating that no GST is charged | Refund receipt |
| Registered (from NEARSHOP_BILLING_GST_REGISTERED_FROM, the date the CA sets) | Tax invoice: CGST and SGST when the payer's state code is Karnataka's (29), IGST otherwise | Credit note |

- Every document carries the supplier's name, address and GSTIN (once registered); the number; the date; the
  recipient's name, address and GSTIN if registered (from the billing account, as it was when issued); the SAC
  (NEARSHOP_BILLING_SAC, chosen by the CA); the taxable value; the GST; the total; the place of supply; the payment's
  date and UPI reference; and, on a refund document, the original's number and the refund transfer's reference.
- Numbers: one consecutive series per financial year (1 April to 31 March, IST), shared by every document type,
  <prefix>/<YY-YY>/<NNNNNN>, for example NS/27-28/000001 (15 characters; at most 16 allowed). The prefix is
  NEARSHOP_BILLING_NUMBER_PREFIX (1 to 3 characters). A number is allocated in the payment's transaction, so a
  payment that fails leaves no gap, and two payments at once never share a number. Confirm the format with the CA,
  including whether credit notes need their own series.
- Documents never change after issue. The app refuses every write to them except the first insert, and each carries
  a content hash: the admin view shows intact false if a stored document was altered outside the app. To reverse a
  document, refund the payment.
- Wording: the CA's approved text only, copied exactly into NEARSHOP_BILLING_WORDING_RECEIPT, _TAX_INVOICE,
  _REFUND_RECEIPT and _CREDIT_NOTE. The team never writes this wording.
- Until the format is approved, NEARSHOP_BILLING_DOCUMENT_FORMAT is draft: every document shows "Draft format, not
  for use" and a placeholder in place of the wording, and production refuses to issue any document (a credit stays
  unmatched with the reason). Staging and development issue drafts.

### Format approval record (B22)

| Document | Format label | Approved by (the CA, by name and firm) | Date of the written approval | Where the approval is filed |
|---|---|---|---|---|
| Payment receipt | | | | |
| Refund receipt | | | | |
| Tax invoice | | | | |
| Credit note | | | | |

When the CA approves a format: set NEARSHOP_BILLING_DOCUMENT_FORMAT to its label and the wording settings to the
approved text, on staging first; issue a test receipt on staging and send it to the CA; record the approval above.

### First real receipt check (after release, by 1 Jun 2027)

| Date | Receipt number | Checked by (the CA) | Result against the approved format |
|---|---|---|---|
| | | | |

## Refunds

- An admin refunds a payment in full: POST /api/admin/billing/payments/{id}/refund with the bank transfer's
  reference and a reason. Send the money back by bank transfer (NEFT or IMPS from the current account) first, then
  record it.
- The subscription loses only the refunded payment's term. When no other payment on it is still matched, it becomes
  refunded at once: the shop is on Free, with every stock tool and the same place in search (P9.4). When the payment
  is a renewal and earlier paid terms remain, only the renewal's term is taken off (ends_at and paid_until move back
  by it; the subscription lapses if that end has passed), and the shop keeps the time it paid for and kept. The
  payment and its request are marked refunded, the owner is notified, and a refund receipt (for a receipt) or a
  credit note (for a tax invoice) is issued with the original's number and the same amounts.
- Payments on one subscription are refunded latest first. Refunding an earlier one while a later one is still
  matched is refused (refund_later_first) and names the later payment; refund that one first, then the earlier one.
  A matched payment can therefore always be refunded.
- Advance subscriptions are refunded on request before billing starts, and all of them if billing does not start
  (plan section 8.18). An advance not used 300 days after its payment raises an admin alert (P9.4).
- A refund is always of a whole payment. To refund part of a term, refund the payment and grant the remaining months
  by hand (Admin > Subscriptions), with a reason.

## The pilot ledger

The advance payments R1 recorded by hand before this release are imported once, before billing starts:
POST /api/admin/billing/pilot-ledger with a reason and one row per payment: paid_on (the payment date), shop_id,
amount_paise and upi_reference. Each row becomes a matched payment, an advance subscription and a receipt that shows
the original payment date. Rows are processed in order of payment date, so founding slots go in the order shops
paid. The import is all or nothing: a UPI reference repeated in the file or already recorded, a shop not found, a
shop that already has a plan, an amount that is not the advance's price, or a payment date later than today (IST)
refuses the whole import.

## Settings to configure

| Setting | Value | When |
|---|---|---|
| NEARSHOP_BILLING_UPI_ID, NEARSHOP_BILLING_PAYEE_NAME | The merchant UPI ID on the company's current account and its payee name | B22, before the first payment request |
| NEARSHOP_BILLING_LEGAL_NAME, NEARSHOP_BILLING_ADDRESS, NEARSHOP_BILLING_STATE_CODE | NearShop's legal name, registered address and GST state code (29) | Before the format is approved |
| NEARSHOP_BILLING_DOCUMENT_FORMAT and NEARSHOP_BILLING_WORDING_* | The CA's approved format label and wording | B22 |
| NEARSHOP_BILLING_GST_MODE, NEARSHOP_BILLING_GST_REGISTERED_FROM, NEARSHOP_BILLING_GSTIN, NEARSHOP_BILLING_GST_RATE_PCT, NEARSHOP_BILLING_SAC | registered, the date the CA sets, the GSTIN, 18, the SAC | Only if B5 says NearShop must register |
| NEARSHOP_BILLING_NUMBER_PREFIX | NS unless the CA asks otherwise | B22 |
| NEARSHOP_BILLING_BANK_HOLIDAYS | The year's bank holidays, comma-separated ISO dates | Each January |
| NEARSHOP_AUTOPAY_ENABLED, NEARSHOP_AUTOPAY_PROVIDER | true, razorpay | Staging in test mode first; production from 1 Aug 2027 if B25 says go |
| NEARSHOP_AUTOPAY_KEY_ID, NEARSHOP_AUTOPAY_KEY_SECRET | The gateway's key id and secret: test mode (rzp_test_…) on staging, live only in production's Render environment | B22 (a gateway account in the company's name) |
| NEARSHOP_AUTOPAY_WEBHOOK_SECRET | The secret typed into the gateway's webhook settings for /api/webhooks/autopay | With the keys |
| NEARSHOP_AUTOPAY_GRACE_DAYS, NEARSHOP_AUTOPAY_TOTAL_COUNT, NEARSHOP_AUTOPAY_RECONCILE_DAYS, NEARSHOP_AUTOPAY_SETTLEMENT_DAYS | 3, 60, 3, 5 unless the gateway's retry and settlement schedules say otherwise | Before switching on |

None of these values is ever committed to the repository; set them in the Render environment of each service.
