import type { FocusEvent } from "react";

export { cn } from "cn"

/**
 * onFocus for a row that scrolls sideways (category chips, cards): brings the element that just got keyboard
 * focus fully into view. Browsers scroll a focused element into view only when none of it shows, so a chip
 * peeking 1 px from the edge would otherwise stay hidden (WCAG 2.4.11; P2.10, docs/accessibility.md).
 */
export function revealFocused(event: FocusEvent<HTMLElement>) {
  const target = event.target as HTMLElement;
  if (target !== event.currentTarget && target.matches(":focus-visible")) {
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
}
