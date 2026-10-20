"use client";

// Errors in the root layout itself. This replaces the whole document, so it carries its own <html>, <body>
// and styles (globals.css does not reach it), following the OS color scheme.
import { useEffect } from "react";

import { captureException } from "@/lib/sentry-browser";

export default function GlobalError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  // Reported here because the boundary catches the error first (a no-op without NEXT_PUBLIC_SENTRY_DSN).
  useEffect(() => {
    captureException(error);
  }, [error]);

  return (
    <html lang="en-IN">
      <body
        style={{
          margin: 0,
          minHeight: "100dvh",
          display: "grid",
          placeItems: "center",
          padding: "0 16px",
          fontFamily: "system-ui, sans-serif",
          colorScheme: "light dark",
          background: "Canvas",
          color: "CanvasText",
        }}
      >
        <title>Something went wrong · NearShop</title>
        <main style={{ maxWidth: 420, textAlign: "center" }}>
          <h1 style={{ fontSize: 24, fontWeight: 600, margin: 0 }}>Something went wrong</h1>
          <p style={{ marginTop: 8, opacity: 0.75 }}>
            It&apos;s on our side, not yours. Try again, and if it keeps happening, come back in a little while.
          </p>
          <button
            type="button"
            onClick={() => retry()}
            style={{ marginTop: 24, padding: "10px 18px", borderRadius: 10, border: "1px solid currentColor", background: "transparent", color: "inherit", font: "inherit", cursor: "pointer" }}
          >
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
