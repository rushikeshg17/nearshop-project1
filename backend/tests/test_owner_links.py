"""The links NearShop puts in owners' notifications and WhatsApp texts open real console pages (frontend/src/app/shop):
the owner console lives under /shop, and /owner/... is the API's prefix, not a page."""

from pathlib import Path

import pytest

from app.services import alerts, billing, boost, gateway, plans, statements

APP = Path(__file__).resolve().parents[2] / "frontend" / "src" / "app"
LINKS = {"statements.LINK": statements.LINK, "billing.BILLING_LINK": billing.BILLING_LINK,
         "plans.PLAN_LINK": plans.PLAN_LINK, "gateway.PLAN_LINK": gateway.PLAN_LINK,
         "boost.BOOST_LINK": boost.BOOST_LINK, "alerts.INVENTORY_LINK": alerts.INVENTORY_LINK}


@pytest.mark.parametrize("name", sorted(LINKS))
def test_each_owner_link_is_a_console_page(name):
    link = LINKS[name]
    assert link.startswith("/shop/") and not link.startswith("/owner"), name
    if not APP.is_dir():
        pytest.skip("the frontend is not checked out next to the backend")
    assert (APP / link.lstrip("/").split("?")[0] / "page.tsx").is_file(), f"{name} = {link} has no page"
