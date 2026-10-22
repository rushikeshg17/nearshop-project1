"use client";

import { Smartphone } from "lucide-react";
import Link from "next/link";

import { useI18n } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { formatNumber, formatPrice } from "@/lib/format";
import { richText } from "@/lib/i18n";
import { REFUND_POLICY_HREF, UPI_REFUND_DAYS, upiPaymentLink, useUpiQr } from "@/lib/upi";

/**
 * "Pay the shop by UPI" (P6.2, plan section 8.8): a upi://pay link to the shop's own UPI ID, with the amount and the
 * order or reservation code as the note, and the same link as a QR code made in the browser, for paying from another
 * phone. The money goes to the shop directly; NearShop never holds it. Shared by the order and reservation pages.
 */
export function PayShopByUpi({ upiId, payeeName, amount, note, cash }: {
  upiId: string;
  payeeName: string;
  amount: number;
  note: string;
  /** How cash works instead, in the interface language: transactions.upi.cashOnDelivery or cashAtCounter. */
  cash: string;
}) {
  const { t } = useI18n();
  const link = upiPaymentLink({ upiId, payeeName, amount, note });
  const qr = useUpiQr(link);
  return (
    <section aria-labelledby="pay-upi-title" className="rounded-3xl border bg-card p-5">
      <div className="flex items-start gap-3">
        <Smartphone className="mt-0.5 size-5 shrink-0 text-primary" />
        <div>
          <h2 id="pay-upi-title" className="font-semibold">{t("transactions.upi.title")}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {t("transactions.upi.body", { payee: payeeName, price: formatPrice(amount) })} {cash}
          </p>
        </div>
      </div>
      <div className="mt-4 flex flex-col items-center gap-4 sm:flex-row sm:items-start">
        <div className="shrink-0 rounded-xl border bg-white p-2">
          {qr ? (
            <svg
              role="img"
              aria-label={t("transactions.upi.qrLabel", { payee: payeeName, price: formatPrice(amount) })}
              viewBox={`-4 -4 ${qr.size + 8} ${qr.size + 8}`}
              shapeRendering="crispEdges"
              className="size-40"
            >
              <path d={qr.path} fill="#000" />
            </svg>
          ) : (
            <div aria-hidden className="size-40 animate-pulse rounded-lg bg-neutral-200" />
          )}
        </div>
        <div className="w-full min-w-0 space-y-3 text-sm">
          <Button asChild className="h-11 w-full sm:w-auto">
            <a href={link}><Smartphone /> {t("transactions.upi.payButton", { price: formatPrice(amount) })}</a>
          </Button>
          <p className="text-muted-foreground">{t("transactions.upi.scanHint")}</p>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
            <dt className="text-muted-foreground">{t("transactions.upi.upiId")}</dt>
            <dd className="break-all font-mono">{upiId}</dd>
            <dt className="text-muted-foreground">{t("transactions.upi.note")}</dt>
            <dd className="font-mono">{note}</dd>
          </dl>
          <p className="text-xs text-muted-foreground">
            {richText(t("transactions.upi.refund", { days: formatNumber(UPI_REFUND_DAYS) }), {
              policy: (
                <Link href={REFUND_POLICY_HREF} className="font-medium text-foreground underline underline-offset-4">
                  {t("transactions.policy.title")}
                </Link>
              ),
            })}
          </p>
        </div>
      </div>
    </section>
  );
}
