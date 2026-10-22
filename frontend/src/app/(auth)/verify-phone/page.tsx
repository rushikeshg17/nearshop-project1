import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import { Suspense } from "react";

import { requestI18n } from "@/lib/i18n";

import { VerifyPhoneForm } from "../login/login-form";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  return { title: t("pages.titles.verifyPhone"), robots: { index: false } };
}

/** P4.2: a signed-in customer or owner verifies a phone number before the email sign-in sunset. The banner on every
 * page links here, and from the sunset every page sends such an account here (components/providers.tsx). */
export default function VerifyPhonePage() {
  return (
    <Suspense>
      <VerifyPhoneForm />
    </Suspense>
  );
}
