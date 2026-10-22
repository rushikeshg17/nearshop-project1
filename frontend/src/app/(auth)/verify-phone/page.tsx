import type { Metadata } from "next";
import { Suspense } from "react";

import { VerifyPhoneForm } from "../login/login-form";

export const metadata: Metadata = { title: "Verify your phone", robots: { index: false } };

/** P4.2: a signed-in customer or owner verifies a phone number before the email sign-in sunset. The banner on every
 * page links here, and from the sunset every page sends such an account here (components/providers.tsx). */
export default function VerifyPhonePage() {
  return (
    <Suspense>
      <VerifyPhoneForm />
    </Suspense>
  );
}
