# Billing: payments to NearShop, receipts, invoices and refunds

How shops pay NearShop's own fees, how every credit is matched, which documents are issued, and what to do when
something does not fit. Code: backend/app/services/billing.py (P9.5). Plan section 8.18; decisions B5 (the GST
position) and B22 (NearShop's merchant UPI QR and the document formats approved by the CA).

This route only ever collects NearShop's own fees into NearShop's own current account. It never collects money for
a shop: customers pay shops directly (P6.1, plan section 8.8). Agents never collect cash.

## Payment route

1. The owner (or an admin, for them) creates a payment request for a plan year: Owner > Plan, or
   POST /api/owner/billing/requests; admins POST /api/admin/billing/requests with a reason.
   - The amount is the plan's list price in the catalogue in force, or the founding price (Growth, ₹999 for the
     first year) while the shop qualifies (P9.4). GST is added only when NearShop is registered (below).
   - Before NEARSHOP_BILLING_STARTS_ON (1 Aug 2027) the request is an advance; from then on it is a new plan, or a
     renewal of the shop's active plan (the same plan and period).
   - Each request has a unique number, NSP and six digits (NSP000123). A new request cancels the shop's open ones.
   - Monthly Growth is not paid this way: it is UPI Autopay through a gateway (P9.6).
2. The owner pays by UPI to NearShop's merchant UPI ID and payee name (NEARSHOP_BILLING_UPI_ID,
   NEARSHOP_BILLING_PAYEE_NAME), with the request number as the payment note. The screen (P9.10) builds the UPI
   link and QR from these with frontend/src/lib/upi.ts.
3. An admin uploads the bank statement (below) at least every working day. Each credit is matched to its request;
   the payment, its subscription change (P9.4) and its document are written in one transaction.

Merchant discount rate: UPI merchant payments of ₹2,000 or less carry none; larger ones 0.4%, capped at ₹300
(business/BUSINESS_MODEL.md section 8.2). Every plan price today is below ₹2,000 except Pro and Business.

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
- The subscription becomes refunded at once: the shop is on Free, with every stock tool and the same place in search
  (P9.4). The payment and its request are marked refunded, the owner is notified, and a refund receipt (for a
  receipt) or a credit note (for a tax invoice) is issued with the original's number and the same amounts.
- Advance subscriptions are refunded on request before billing starts, and all of them if billing does not start
  (plan section 8.18). An advance not used 300 days after its payment raises an admin alert (P9.4).
- A refund ends the whole subscription. To refund part of a term, refund the payment and grant the remaining months
  by hand (Admin > Subscriptions), with a reason.

## The pilot ledger

The advance payments R1 recorded by hand before this release are imported once, before billing starts:
POST /api/admin/billing/pilot-ledger with a reason and one row per payment: paid_on (the payment date), shop_id,
amount_paise and upi_reference. Each row becomes a matched payment, an advance subscription and a receipt that shows
the original payment date. Rows are processed in order of payment date, so founding slots go in the order shops
paid. The import is all or nothing: a UPI reference repeated in the file or already recorded, a shop not found, a
shop that already has a plan, or an amount that is not the advance's price refuses the whole import.

## Settings to configure

| Setting | Value | When |
|---|---|---|
| NEARSHOP_BILLING_UPI_ID, NEARSHOP_BILLING_PAYEE_NAME | The merchant UPI ID on the company's current account and its payee name | B22, before the first payment request |
| NEARSHOP_BILLING_LEGAL_NAME, NEARSHOP_BILLING_ADDRESS, NEARSHOP_BILLING_STATE_CODE | NearShop's legal name, registered address and GST state code (29) | Before the format is approved |
| NEARSHOP_BILLING_DOCUMENT_FORMAT and NEARSHOP_BILLING_WORDING_* | The CA's approved format label and wording | B22 |
| NEARSHOP_BILLING_GST_MODE, NEARSHOP_BILLING_GST_REGISTERED_FROM, NEARSHOP_BILLING_GSTIN, NEARSHOP_BILLING_GST_RATE_PCT, NEARSHOP_BILLING_SAC | registered, the date the CA sets, the GSTIN, 18, the SAC | Only if B5 says NearShop must register |
| NEARSHOP_BILLING_NUMBER_PREFIX | NS unless the CA asks otherwise | B22 |
| NEARSHOP_BILLING_BANK_HOLIDAYS | The year's bank holidays, comma-separated ISO dates | Each January |

None of these values is ever committed to the repository; set them in the Render environment of each service.
