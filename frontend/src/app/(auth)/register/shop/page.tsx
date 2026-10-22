import type { Metadata } from "next";
import { cookies, headers } from "next/headers";

import { I18nAreas } from "@/components/providers";
import { pickAreas, requestI18n } from "@/lib/i18n";

import { ShopSignup } from "./shop-signup";

export async function generateMetadata(): Promise<Metadata> {
  const { t } = await requestI18n({ cookies, headers });
  return { title: t("pages.titles.registerShop") };
}

export default async function RegisterShopPage() {
  // The shop form's strings (src/components/shop/shop-form-fields.tsx), shared with the owner's settings page.
  const { messages } = await requestI18n({ cookies, headers });
  return (
    <I18nAreas messages={pickAreas(messages, ["shopForm"])}>
      <ShopSignup />
    </I18nAreas>
  );
}
