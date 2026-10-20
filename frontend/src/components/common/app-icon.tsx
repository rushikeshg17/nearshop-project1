"use client";

import { Package } from "lucide-react";
import type { IconName } from "lucide-react/dynamic";
import dynamic from "next/dynamic";
import { lazy, Suspense } from "react";

import { cn } from "@/lib/utils";

type IconProps = { name: string; className?: string; strokeWidth: number };

// lucide's DynamicIcon carries a map of every icon name (about 21 KB gzip): it loads only when a name outside
// the catalogue's icon map is shown.
const DynamicIcon = dynamic(() => import("lucide-react/dynamic").then((m) => m.DynamicIcon), { ssr: false });

function toKebab(name: string) {
  return name
    .replace(/([a-z])([A-Z0-9])/g, "$1-$2")
    .replace(/([0-9])([A-Z])/g, "$1-$2")
    .toLowerCase();
}

// The catalogue's 144 icons (src/lib/icon-map.ts, about 19 KB gzip) are not first-load JavaScript (P2.9). The
// server renders them into the page; in the browser they hydrate once their chunk arrives, after the rest of
// the page, and React keeps the server's markup in place until then.
const CatalogIcon = lazy(() =>
  import("@/lib/icon-map").then(({ ICONS }) => ({
    default: function CatalogIcon({ name, className, strokeWidth }: IconProps) {
      const Static = ICONS[name];
      if (Static) return <Static className={className} strokeWidth={strokeWidth} aria-hidden />;
      return (
        <DynamicIcon
          name={toKebab(name) as IconName}
          className={className}
          strokeWidth={strokeWidth}
          aria-hidden
          fallback={() => <Package className={className} strokeWidth={strokeWidth} aria-hidden />}
        />
      );
    },
  })),
);

/** Renders a Lucide icon from a name stored in the database (e.g. "ShoppingBag"): one of the catalogue's
 *  icons, or, for any other name, loaded on demand. */
export function AppIcon({ name, className, strokeWidth = 1.75 }: { name: string; className?: string; strokeWidth?: number }) {
  return (
    <Suspense fallback={<span className={cn("inline-block size-[1em] shrink-0", className)} aria-hidden />}>
      <CatalogIcon name={name} className={className} strokeWidth={strokeWidth} />
    </Suspense>
  );
}
