import { AppIcon } from "@/components/common/app-icon";
import { cn } from "@/lib/utils";

const CATEGORY_HUES: Record<string, number> = {};

export function registerCategoryHues(categories: { slug: string; color_hue: number }[]) {
  for (const c of categories) CATEGORY_HUES[c.slug] = c.color_hue;
}

// The rendered size of each thumb in CSS pixels, for the image's width and height attributes (the browser
// reserves the space before the image arrives). xl is a square as wide as its column.
const PIXELS = { sm: 44, md: 64, lg: 96, xl: 640 } as const;

/** Product image, or a calm tinted tile with the item's icon when the shop has not uploaded a photo.
 *
 * Lists and small previews (sm, md, lg) show the 320 px thumbnail from P2.7 (thumbnailUrl), lazily loaded;
 * only the product page's large image (xl) uses the full photo, loaded first, as it is often the page's
 * largest paint (P2.9). A photo stored before thumbnails existed falls back to the full image. */
export function ProductThumb({
  icon,
  imageUrl,
  thumbnailUrl,
  category,
  hue,
  alt,
  size = "md",
  className,
}: {
  icon: string;
  imageUrl?: string | null;
  thumbnailUrl?: string | null;
  category?: string | null;
  hue?: number;
  alt: string;
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
}) {
  const h = hue ?? (category ? CATEGORY_HUES[category] : undefined) ?? 60;
  const sizes = {
    sm: "size-11 rounded-lg [&_svg]:size-5",
    md: "size-16 rounded-xl [&_svg]:size-7",
    lg: "size-24 rounded-2xl [&_svg]:size-10",
    xl: "aspect-square w-full rounded-3xl [&_svg]:size-24",
  }[size];

  const large = size === "xl";
  const src = large ? imageUrl : (thumbnailUrl ?? imageUrl);
  if (src) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- images come from the API via /media (or the media domain), already resized to WebP
      <img
        src={src}
        alt={alt}
        width={PIXELS[size]}
        height={PIXELS[size]}
        loading={large ? "eager" : "lazy"}
        fetchPriority={large ? "high" : undefined}
        decoding="async"
        className={cn(sizes, "shrink-0 bg-muted object-cover", className)}
      />
    );
  }
  return (
    <div
      role="img"
      aria-label={alt}
      className={cn(sizes, "grid shrink-0 place-items-center", className)}
      style={{
        background: `light-dark(oklch(0.955 0.028 ${h}), oklch(0.27 0.035 ${h}))`,
        color: `light-dark(oklch(0.5 0.11 ${h}), oklch(0.8 0.09 ${h}))`,
      }}
    >
      <AppIcon name={icon} />
    </div>
  );
}
