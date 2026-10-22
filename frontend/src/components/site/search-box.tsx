"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, History, Loader2, Search, TrendingUp, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { AppIcon } from "@/components/common/app-icon";
import { useI18n } from "@/components/providers";
import { useDebounce } from "@/hooks/use-debounce";
import { api } from "@/lib/api";
import { richText } from "@/lib/i18n";
import type { Suggestions } from "@/lib/types";
import { cn } from "@/lib/utils";

type Option =
  | { kind: "query"; value: string; icon: "history" | "trending" | "search" }
  | { kind: "item"; value: string; icon: string; sub: string }
  | { kind: "category"; value: string; slug: string; icon: string };

interface SearchBoxProps {
  size?: "md" | "lg";
  className?: string;
  placeholder?: string;
}

/** The search box. Only on the search page does it read the query string: useSearchParams in a statically
 * rendered page makes everything up to the nearest Suspense boundary render in the browser only, which kept
 * the home page's hero out of its server HTML and its first paint (P2.9). */
export function SearchBox(props: SearchBoxProps) {
  const pathname = usePathname();
  return pathname === "/search" ? <SearchPageBox {...props} /> : <SearchBoxField {...props} query={null} />;
}

function SearchPageBox(props: SearchBoxProps) {
  const params = useSearchParams();
  return <SearchBoxField {...props} query={params.get("q") ?? ""} />;
}

/** query: the search page's q, kept in the box while navigating between searches; null elsewhere. */
function SearchBoxField({
  size = "md",
  className,
  placeholder,
  query,
}: SearchBoxProps & { query: string | null }) {
  const { t } = useI18n();
  const router = useRouter();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState(query ?? "");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const debounced = useDebounce(value.trim(), 150);

  useEffect(() => {
    // keep the box in sync when navigating between searches
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (query !== null) setValue(query);
  }, [query]);

  const suggest = useQuery({
    queryKey: ["suggest", debounced],
    queryFn: () => api.get<Suggestions>("/search/suggest", { q: debounced }),
    enabled: open && debounced.length >= 2,
    staleTime: 60_000,
    placeholderData: (prev) => prev,
  });
  const discover = useQuery({
    queryKey: ["discover"],
    queryFn: () => api.get<{ popular: string[]; recent: string[] }>("/search/discover"),
    enabled: open,
    staleTime: 60_000,
  });

  const options = useMemo<Option[]>(() => {
    if (debounced.length < 2) {
      const recent = (discover.data?.recent ?? []).slice(0, 4).map((q) => ({ kind: "query", value: q, icon: "history" }) as Option);
      const popular = (discover.data?.popular ?? [])
        .filter((q) => !discover.data?.recent.includes(q))
        .slice(0, 6 - recent.length)
        .map((q) => ({ kind: "query", value: q, icon: "trending" }) as Option);
      return [...recent, ...popular];
    }
    const s = suggest.data;
    return [
      { kind: "query", value: debounced, icon: "search" } as Option,
      ...(s?.categories ?? []).map((c) => ({ kind: "category", value: c.name, slug: c.slug, icon: c.icon }) as Option),
      ...(s?.queries ?? []).filter((q) => q !== debounced.toLowerCase()).map((q) => ({ kind: "query", value: q, icon: "trending" }) as Option),
      ...(s?.items ?? []).slice(0, 6).map((i) => ({ kind: "item", value: i.name, icon: i.icon, sub: i.brand ?? "" }) as Option),
    ];
  }, [debounced, suggest.data, discover.data]);

  function go(opt?: Option) {
    const q = opt ? opt.value : value.trim();
    setOpen(false);
    inputRef.current?.blur();
    if (opt?.kind === "category") {
      router.push(`/search?category=${opt.slug}`);
      return;
    }
    if (!q) return;
    setValue(q);
    router.push(`/search?q=${encodeURIComponent(q)}`);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(i + 1, options.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(active >= 0 ? options[active] : undefined);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  const lg = size === "lg";
  const showPanel = open && options.length > 0;

  return (
    <div className={cn("relative w-full", className)}>
      <form
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          go();
        }}
        className={cn(
          "group flex items-center gap-2 border bg-card shadow-soft transition-[box-shadow,border-color] focus-within:border-ring focus-within:ring-2 focus-within:ring-ring",
          lg ? "h-14 rounded-2xl pr-2 pl-4" : "h-10 rounded-xl pr-1.5 pl-3",
          showPanel && "rounded-b-none",
        )}
      >
        {suggest.isFetching && debounced.length >= 2 ? (
          <Loader2 className={cn("shrink-0 animate-spin text-muted-foreground", lg ? "size-5" : "size-4")} />
        ) : (
          <Search className={cn("shrink-0 text-muted-foreground", lg ? "size-5" : "size-4")} />
        )}
        <input
          ref={inputRef}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setOpen(true);
            setActive(-1);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 120)}
          onKeyDown={onKeyDown}
          placeholder={placeholder ?? t("common.searchBox.placeholder")}
          aria-label={t("common.searchBox.label")}
          aria-expanded={showPanel}
          aria-controls={listId}
          aria-autocomplete="list"
          role="combobox"
          aria-activedescendant={active >= 0 ? `${listId}-${active}` : undefined}
          enterKeyHint="search"
          className={cn(
            "min-w-0 flex-1 bg-transparent outline-none placeholder:text-muted-foreground/80",
            lg ? "text-base md:text-lg" : "text-sm",
          )}
        />
        {value && (
          <button
            type="button"
            onClick={() => {
              setValue("");
              inputRef.current?.focus();
            }}
            className="grid size-7 place-items-center rounded-full text-muted-foreground hover:bg-muted"
            aria-label={t("common.searchBox.clear")}
          >
            <X className="size-4" />
          </button>
        )}
        {lg && (
          <button
            type="submit"
            className="h-10 rounded-xl bg-primary px-5 text-sm font-medium text-primary-foreground transition-transform active:scale-[0.97]"
          >
            {t("common.searchBox.submit")}
          </button>
        )}
      </form>

      {showPanel && (
        <ul
          id={listId}
          role="listbox"
          className="absolute inset-x-0 top-full z-50 max-h-[60vh] animate-in overflow-auto rounded-b-2xl border border-t-0 bg-popover p-1.5 shadow-lift duration-150 fade-in slide-in-from-top-1"
        >
          {debounced.length < 2 && (
            <li className="px-3 pt-2 pb-1 text-xs font-medium text-muted-foreground">
              {discover.data?.recent.length ? t("common.searchBox.recentAndPopular") : t("common.searchBox.popularNearYou")}
            </li>
          )}
          {options.map((opt, i) => (
            // The combobox pattern (WAI-ARIA APG): options are not focusable, and the input handles the keys
            // (arrows, Enter, Escape) through aria-activedescendant, so the click needs no key handler here.
            // eslint-disable-next-line jsx-a11y/click-events-have-key-events
            <li
              key={`${opt.kind}-${opt.value}-${i}`}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => go(opt)}
              onMouseEnter={() => setActive(i)}
              className={cn(
                "flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2 text-sm",
                i === active && "bg-accent",
              )}
            >
              <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground [&_svg]:size-4">
                {opt.kind === "query" ? (
                  opt.icon === "history" ? <History /> : opt.icon === "trending" ? <TrendingUp /> : <Search />
                ) : (
                  <AppIcon name={opt.icon} />
                )}
              </span>
              <span className="min-w-0 flex-1 truncate">
                {opt.kind === "query" && opt.icon === "search" ? (
                  richText(t("common.searchBox.searchFor"), {
                    query: <span className="font-medium">&ldquo;{opt.value}&rdquo;</span>,
                  })
                ) : (
                  opt.value
                )}
              </span>
              {opt.kind === "category" && <span className="text-xs text-muted-foreground">{t("common.searchBox.category")}</span>}
              {opt.kind === "item" && opt.sub && <span className="hidden text-xs text-muted-foreground sm:inline">{opt.sub}</span>}
              {i === active && <ArrowUpRight className="size-4 text-muted-foreground" />}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
