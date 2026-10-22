"use client";

import { Bike, ChevronRight, Footprints, Timer } from "lucide-react";
import Link from "next/link";

import { ProductThumb } from "@/components/common/product-thumb";
import { Countdown, StatusBadge } from "@/components/common/status";
import { useI18n } from "@/components/providers";
import { formatDateTime, formatPrice } from "@/lib/format";
import { richText } from "@/lib/i18n";
import type { Order, Reservation } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ActivityCard({ item, href, className }: { item: Reservation | Order; href: string; className?: string }) {
  const { t, language } = useI18n();
  const isRes = item.kind === "reservation";
  const title = isRes ? item.product.name : item.items.map((i) => `${i.quantity} x ${i.name}`).join(", ");
  const icon = isRes ? item.product.icon : item.items[0]?.icon ?? "Package";
  const image = isRes ? item.product.image_url : item.items[0]?.image_url;
  const thumbnail = isRes ? item.product.thumbnail_url : item.items[0]?.thumbnail_url;
  const live = isRes && ["REQUESTED", "CONFIRMED", "READY_FOR_PICKUP"].includes(item.status);

  return (
    <Link href={href} className={cn("group flex items-center gap-4 rounded-2xl border bg-card p-4 transition-shadow hover:shadow-lift", className)}>
      <ProductThumb icon={icon} imageUrl={image} thumbnailUrl={thumbnail} alt="" size="md" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={item.status} kind={item.kind} />
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            {isRes ? <Footprints className="size-3.5" /> : <Bike className="size-3.5" />}
            {isRes ? t("transactions.activity.pickup") : t("transactions.activity.delivery")} · <span className="font-mono">{item.code}</span>
          </span>
        </div>
        <p className="mt-1.5 truncate font-medium">{title}</p>
        <p className="truncate text-sm text-muted-foreground">
          {item.shop.name} · {formatPrice(item.total)} · {formatDateTime(item.created_at, language)}
        </p>
        {live && (
          <p className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-brand-ink">
            <Timer className="size-3.5" />
            {richText(t(item.status === "REQUESTED" ? "transactions.activity.shopHas" : "transactions.activity.heldFor"), {
              time: <Countdown to={(item as Reservation).expires_at} />,
            })}
          </p>
        )}
      </div>
      <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
    </Link>
  );
}
