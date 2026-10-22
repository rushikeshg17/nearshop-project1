// Shapes returned by the NearShop API (see backend/app/schemas/serializers.py).

export type Role = "customer" | "owner" | "admin";
export type StockStatus = "in_stock" | "low_stock" | "out_of_stock";

export interface User {
  id: number;
  name: string;
  email: string;
  phone: string | null;
  role: Role;
  home_lat: number | null;
  home_lng: number | null;
  home_label: string | null;
  shop: { id: number; slug: string; name: string } | null;
  /** P4.2: whether the phone is verified by a code (phone sign-in reaches only a verified phone). */
  phone_verified: boolean;
  /** The email sign-in sunset, an IST day (YYYY-MM-DD, NEARSHOP_EMAIL_LOGIN_SUNSET). */
  email_login_sunset: string;
  /** What the site asks of this account (components/providers.tsx): "warn" shows the banner with the sunset date,
   * "required" sends every page to /verify-phone, null asks nothing (a verified phone, an admin, or phone sign-in
   * unavailable here). */
  phone_migration: PhoneMigration;
  /** P7.11: the language notifications and WhatsApp messages come in (PATCH /api/auth/me sets it). */
  language?: "en" | "kn";
}

export type PhoneMigration = "warn" | "required" | null;

export type OtpChannel = "whatsapp" | "sms" | "console";

/** GET /api/auth/phone-signin (P4.1, P4.2): whether the phone screens are shown, the code's rules and the sunset. */
export interface PhoneSignin {
  available: boolean;
  channels: OtpChannel[];
  code_length: number;
  /** Seconds a code stays valid. */
  expires_in: number;
  /** Seconds before another code may be asked for. */
  resend_after: number;
  email_login_sunset: string;
  /** True from the sunset: customers and owners without a verified phone can no longer sign in with email. */
  email_login_ended: boolean;
}

/** POST /api/auth/otp/request. */
export interface OtpSent {
  ok: true;
  channel: OtpChannel;
  code_length: number;
  expires_in: number;
  resend_after: number;
}

export interface Category {
  id: number;
  slug: string;
  name: string;
  icon: string;
  color_hue: number;
  description: string | null;
  in_stock_listings?: number;
}

export interface Locality {
  name: string;
  lat: number;
  lng: number;
  pincode?: string;
}

export interface Meta {
  city: {
    slug: string;
    name: string;
    state: string;
    center: { lat: number; lng: number };
    default_zoom: number;
    localities: Locality[];
  };
  categories: Category[];
  stats: { shops: number; listings: number; in_stock: number };
  /** False until launch: the sign-up screens show a paused state and the API refuses sign-ups. */
  signups_open: boolean;
  /** The site runs on generated data; site pages show a banner saying so. */
  demo_mode: boolean;
}

export interface ShopBrief {
  id: number;
  slug: string;
  name: string;
  tagline: string | null;
  locality: string | null;
  city: string;
  lat: number;
  lng: number;
  distance_km: number | null;
  is_open: boolean | null;
  hours_label: string;
  offers_pickup: boolean;
  offers_delivery: boolean;
  delivery_radius_km: number;
  delivery_fee: number;
  free_delivery_above: number | null;
  is_verified: boolean;
  image_url: string | null;
  thumbnail_url: string | null; // the 320 px thumbnail (P2.7), for lists and small previews
  inventory_updated_at: string | null;
  rating_avg: number | null;
  rating_count: number;
  categories: string[];
  in_stock_listings?: number;
}

export interface Reliability {
  inventory_updated_at: string | null;
  inventory_update_days_7d: number;
  frequently_updated: boolean;
  reservation_requests_90d: number;
  reservations_confirmed_90d: number;
  acceptance_rate: number | null;
  completion_rate: number | null;
  shop_cancellations_90d: number;
  median_response_minutes: number | null;
  orders_delivered_90d: number;
  rating_avg: number | null;
  rating_count: number;
  accuracy_avg: number | null;
  highlights: string[];
}

export interface ShopDetail extends ShopBrief {
  description: string | null;
  phone: string | null;
  address_line: string;
  pincode: string | null;
  opening_hours: string | null;
  closed_on: string | null;
  established_year: number | null;
  hold_minutes: number;
  is_active: boolean;
  reliability: Reliability;
  category_details: Category[];
  listing_counts?: { total: number; in_stock: number };
}

// ---------------------------------------------------------------- shop verification (P3.3, P3.4)

export type VerificationStatus = "draft" | "submitted" | "verified" | "rejected" | "suspended";
export type KycType = "gstin" | "udyam" | "trade_licence";

/** The owner's view of their shop's verification (GET /owner/verification, and inside GET /owner/shop). */
export interface ShopVerification {
  status: VerificationStatus;
  visible_to_customers: boolean;
  kyc_type: KycType | null;
  kyc_number: string | null;
  has_document_photo: boolean;
  has_storefront_photo: boolean;
  phone_verified: boolean;
  submitted_at: string | null;
  decided_at: string | null;
  /** Set by a rejection or suspension, and by a rejected renewal; cleared by the next submission or approval. */
  rejection_reason: string | null;
  verified_until: string | null;
  renewal_pending: boolean;
  /** From this date the owner may submit fresh evidence (30 days before verified_until); null when not in force. */
  renewal_opens_at: string | null;
  can_submit: boolean;
  /** A change of address or phone sends the shop back to review, hidden until an admin approves it again. */
  changes_need_review: boolean;
}

/** GET and PATCH /owner/shop. review_started: this save sent the verified shop back to review. */
export interface OwnerShopSettings extends ShopDetail {
  verification: ShopVerification;
  review_started: boolean;
  /** The shop's own UPI ID and the payee name UPI apps show (P6.1). Only the owner's settings and the customer's own
   * orders and reservations carry them, never a public shop response. */
  upi_id: string | null;
  upi_payee_name: string | null;
}

/** How the customer paid, recorded by the owner when completing a pickup or delivering an order (P6.1). */
export type PaymentMethod = "cod" | "upi_direct";

/** Where the customer can pay the shop directly by UPI (P6.1, P6.2): only in the customer's own order and
 * reservation responses. Both null while the shop has no UPI ID; upi_payee_name falls back to the shop's name. */
export interface PayTo {
  upi_id?: string | null;
  upi_payee_name?: string | null;
}

/** One row of GET /admin/shops. */
export interface AdminShopRow extends ShopBrief {
  is_active: boolean;
  verification_status: VerificationStatus;
  verified_until: string | null;
  visible_to_customers: boolean;
  owner: string;
  owner_email: string;
  listings: number;
  created_at: string;
}

/** One shop in the admin verification queue (GET /admin/verification), oldest submission first. */
export interface VerificationQueueItem {
  id: number;
  name: string;
  slug: string;
  locality: string | null;
  address_line: string;
  pincode: string | null;
  phone: string | null;
  owner: string | null;
  kind: "new" | "renewal" | "review";
  status: VerificationStatus;
  kyc_type: KycType | null;
  kyc_number: string | null;
  submitted_at: string | null;
  age_working_days: number | null;
  phone_verified_at: string | null;
  verified_until: string | null;
  /** Signed links, valid for 5 minutes from the time the queue was loaded. */
  document_photo_url: string | null;
  storefront_photo_url: string | null;
}

export interface Listing {
  id: number;
  catalog_item_id: number | null;
  name: string;
  brand: string | null;
  unit: string | null;
  icon: string;
  image_url: string | null;
  thumbnail_url: string | null; // the 320 px thumbnail (P2.7), for lists and small previews
  category: string | null;
  price: number;
  mrp: number | null;
  quantity: number;
  low_stock_threshold: number;
  stock_status: StockStatus;
  stock_updated_at: string | null;
  is_active: boolean;
  shop: ShopBrief | null;
}

export interface OwnerListing extends Listing {
  sku: string | null;
  description: string | null;
  specs: Record<string, string>;
  keywords: string;
}

export interface ResultGroup {
  key: string;
  catalog_item_id: number | null;
  name: string;
  brand: string | null;
  unit: string | null;
  icon: string;
  image_url: string | null;
  thumbnail_url: string | null; // the 320 px thumbnail (P2.7), for lists and small previews
  category: string;
  min_price: number;
  max_price: number;
  mrp: number | null;
  nearest_km: number;
  shop_count: number;
  in_stock_count: number;
  relevance: number;
  score: number;
  best_offer: Listing & { shop: ShopBrief };
  offers: (Listing & { shop: ShopBrief })[];
}

export interface MapPin extends ShopBrief {
  match_count: number;
  in_stock_count: number;
  min_price: number | null;
}

export interface SearchResponse {
  query: string;
  normalized_query: string;
  expanded_terms: string[];
  did_you_mean: string | null;
  corrected_from: string | null;
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  shops_in_radius: number;
  groups: ResultGroup[];
  shops: MapPin[];
  category_counts: Record<string, number>;
  engine: { semantic: string; keyword: string };
}

export interface Suggestions {
  queries: string[];
  items: { id: number; name: string; brand: string | null; icon: string; category: string }[];
  categories: { slug: string; name: string; icon: string }[];
}

export interface Review {
  id: number;
  rating: number;
  accuracy_rating: number | null;
  delivery_rating: number | null;
  comment: string | null;
  created_at: string;
  customer_name: string;
  product_name: string | null;
  source: "pickup" | "delivery";
  /** The shop's public reply (P3.6). Public lists carry it only while it is visible. */
  reply: ReviewReply | null;
  /** Moderation status: only in the owner's own list (GET /owner/reviews). */
  status?: ModerationStatus;
}

export type ModerationStatus = "visible" | "hidden" | "removed";

export interface ReviewReply {
  text: string;
  created_at: string;
  edited_at: string | null;
  /** Owner's list only: the reply's moderation status, and whether reports have it waiting for an admin. */
  status?: ModerationStatus;
  under_review?: boolean;
}

/** Reasons a review or a reply can be reported for (backend/app/services/reviews.py, ReportReason). */
export type ReviewReportReason = "fake" | "abusive" | "personal_info" | "off_topic" | "other";

/** One item in the admin moderation queue (GET /admin/reviews/queue, P3.6), oldest first. */
export interface ModerationQueueItem {
  target: "review" | "reply";
  review_id: number;
  shop_id: number;
  shop: { id: number; name: string; slug: string } | null;
  entered_at: string;
  age_hours: number;
  status: ModerationStatus;
  triaged_at: string | null;
  rating: number;
  comment: string | null;
  created_at: string;
  reply_text: string | null;
  reports: { count: number; reasons: Partial<Record<ReviewReportReason, number>>; notes: string[] };
  flags: ("reported" | "held_self_dealing" | "hidden_by_reports")[];
  actions: ("keep" | "hide" | "remove")[];
}

export interface ModerationQueue {
  /** The oldest first, at most 200 (QUEUE_LIMIT in backend/app/services/reviews.py). */
  items: ModerationQueueItem[];
  /** Everything waiting, beyond the 200 in items too. */
  total: number;
  triage_hours: number;
  decide_hours: number;
}

/** The monthly moderation SLA (GET /admin/reviews/sla). A share is null while nothing has reached its deadline. */
export interface ReviewSla {
  month: string;
  items: number;
  open: number;
  triage_counted: number;
  triaged_within_24h: number;
  triaged_share: number | null;
  decision_counted: number;
  decided_within_72h: number;
  decided_share: number | null;
}

export interface DeliveryQuote {
  eligible: boolean;
  distance_km: number;
  delivery_fee: number | null;
  free_delivery_above: number | null;
  reason: string | null;
  subtotal?: number;
}

export interface PriceInsight {
  peer_count: number;
  median: number | null;
  min: number;
  max: number;
  deviation_pct?: number;
  statement: string | null;
}

export interface ProductDetail extends Listing {
  description: string | null;
  specs: Record<string, string>;
  sku: string | null;
  category_detail: { slug: string; name: string; icon: string; color_hue: number };
  subcategory: string | null;
  shop: ShopDetail;
  other_offers: (Listing & { shop: ShopBrief })[];
  price_insight: PriceInsight | null;
  delivery_quote: DeliveryQuote | null;
  reviews: Review[];
  sold_last_30d: number;
  my_reservation: { id: number; code: string; status: ReservationStatus } | null;
}

/** GET /api/sitemap (P2.8): what customers may see, for src/app/sitemap.ts. Ids, slugs and dates only. */
export interface SitemapEntries {
  generated_at: string;
  shops: { slug: string; updated_at: string | null }[];
  listings: { id: number; updated_at: string | null }[];
}

export interface Recommendation {
  confidence: number | null;
  lift: number | null;
  support?: number | null;
  same_shop?: boolean;
  offer: Listing & { shop: ShopBrief };
}

export type ReservationStatus =
  | "REQUESTED"
  | "CONFIRMED"
  | "READY_FOR_PICKUP"
  | "COMPLETED"
  | "REJECTED"
  | "CANCELLED"
  | "EXPIRED";

export type OrderStatus =
  | "PENDING"
  | "SHOP_CONFIRMED"
  | "PREPARING"
  | "OUT_FOR_DELIVERY"
  | "DELIVERED"
  | "CANCELLED"
  | "DELIVERY_FAILED"
  | "RETURNED_TO_SHOP";

export interface TimelineEvent {
  from_status: string | null;
  to_status: string;
  actor_role: string;
  note: string | null;
  at: string;
}

export interface Person {
  id: number;
  name: string;
  phone: string | null;
}

export interface Reservation {
  id: number;
  code: string;
  kind: "reservation";
  status: ReservationStatus;
  quantity: number;
  unit_price: number;
  total: number;
  note: string | null;
  hold_minutes: number;
  expires_at: string;
  created_at: string;
  confirmed_at: string | null;
  ready_at: string | null;
  completed_at: string | null;
  closed_at: string | null;
  close_reason: string | null;
  product: {
    id: number;
    name: string;
    brand: string | null;
    unit: string | null;
    icon: string;
    image_url: string | null;
    thumbnail_url: string | null; // the 320 px thumbnail (P2.7), for lists and small previews
    quantity_in_stock: number;
  };
  shop: {
    id: number;
    slug: string;
    name: string;
    locality: string | null;
    address_line: string;
    phone: string | null;
    lat: number;
    lng: number;
    hours_label: string;
  } & PayTo;
  actions: string[];
  reviewed: boolean;
  /** Both null until the owner completes the pickup, then the method and "paid" (P6.1). */
  payment_method?: PaymentMethod | null;
  payment_status?: "paid" | null;
  customer?: Person;
  timeline?: TimelineEvent[];
}

export interface Order {
  id: number;
  code: string;
  kind: "order";
  status: OrderStatus;
  /** cod until delivery, when the owner records how the customer paid (P6.1). */
  payment_method: PaymentMethod;
  payment_status: "unpaid" | "paid" | "refunded";
  delivery_address: string;
  delivery_lat: number;
  delivery_lng: number;
  contact_phone: string | null;
  distance_km: number;
  subtotal: number;
  delivery_fee: number;
  total: number;
  note: string | null;
  created_at: string;
  delivered_at: string | null;
  closed_at: string | null;
  close_reason: string | null;
  items: {
    product_id: number;
    name: string;
    unit_price: number;
    quantity: number;
    icon: string;
    image_url: string | null;
    thumbnail_url: string | null; // the 320 px thumbnail (P2.7), for lists and small previews
  }[];
  shop: { id: number; slug: string; name: string; locality: string | null; phone: string | null; lat: number; lng: number } & PayTo;
  actions: string[];
  reviewed: boolean;
  /** The 4-digit code the customer gives the shop at delivery (P3.5). Only in the customer's own responses, and
   * only once the shop has confirmed the order; owner responses never carry it. */
  delivery_code?: string | null;
  customer?: Person;
  timeline?: TimelineEvent[];
}

export interface Notification {
  id: number;
  kind: string;
  title: string;
  body: string | null;
  link: string | null;
  read: boolean;
  created_at: string;
}

export interface CustomerDashboard {
  active_reservations: Reservation[];
  active_orders: Order[];
  history: (Reservation | Order)[];
  to_review: (Reservation | Order)[];
  recent_searches: string[];
  recommendations: Recommendation[];
  stats: { completed_pickups: number; delivered_orders: number; shops_visited: number };
}

export interface SalesPoint {
  date: string;
  revenue: number;
  units: number;
}

export interface OwnerOverview {
  today: { revenue: number; units: number; reservations: number; orders: number };
  revenue_7d: number;
  revenue_prev_7d: number;
  revenue_30d: number;
  queue: {
    reservation_requests: number;
    pickups_waiting: number;
    orders_pending: number;
    deliveries_in_progress: number;
  };
  inventory: { listings: number; in_stock: number; low_stock: number; out_of_stock: number; updated_at: string | null };
  sales_series: SalesPoint[];
  top_products: (Listing & { units: number; revenue: number })[];
  low_stock: (Listing & { days_to_stockout: number | null; recommended_restock: number | null })[];
  rating: { avg: number | null; count: number };
  includes_demo_data: boolean;
  reservation_requests: Reservation[];
  pending_orders: Order[];
  shop: { id: number; name: string; slug: string; offers_delivery: boolean };
}

export interface ForecastItem extends Listing {
  predicted_7d: number;
  daily_rate: number;
  days_to_stockout: number | null;
  recommended_restock: number;
  trend: "rising" | "steady" | "falling";
  confidence: "low" | "medium" | "high";
  is_demo: boolean;
}

export interface ModelSummary {
  algorithm: string;
  trained_at: string;
  uses_demo_data: boolean;
  metrics: Record<string, unknown>;
  note: string | null;
}

export interface OwnerInsights {
  restock: ForecastItem[];
  rising: ForecastItem[];
  price_flags: {
    id: number;
    product: Listing;
    price: number;
    reference_price: number;
    deviation_pct: number;
    direction: "high" | "low";
    peer_count: number;
  }[];
  area_demand: { query: string; searches: number; you_stock_it: boolean }[];
  missed_demand: { query: string; searches: number }[];
  bundle_gaps: {
    item: { id: number; name: string; icon: string; typical_price: number | null };
    because_of: string;
    confidence: number;
    lift: number;
  }[];
  models: Record<string, ModelSummary>;
}

export interface CatalogEntry {
  id: number;
  name: string;
  brand: string | null;
  unit: string | null;
  icon: string;
  mrp: number | null;
  typical_price: number | null;
  local_median: number | null;
  category: string;
  subcategory: string | null;
  already_listed: boolean;
}

export interface AdminOverview {
  totals: {
    customers: number;
    owners: number;
    shops: number;
    shops_verified: number;
    listings: number;
    in_stock: number;
    reservations: number;
    orders: number;
    revenue_30d: number;
    revenue_30d_real: number;
    searches_30d: number;
    open_price_flags: number;
  };
  reservations_by_status: Record<string, number>;
  orders_by_status: Record<string, number>;
  sales_series: SalesPoint[];
  categories: { name: string; slug: string; color_hue: number; revenue_30d: number; searches: number }[];
  top_shops: {
    id: number;
    name: string;
    slug: string;
    locality: string | null;
    revenue_30d: number;
    baskets: number;
    rating_avg: number | null;
  }[];
  popular_searches: { query: string; count: number; avg_results: number }[];
  zero_result_searches: { query: string; count: number }[];
  activity: { date: string; inventory_updates: number; searches: number }[];
}

export interface PriceFlag {
  id: number;
  price: number;
  reference_price: number;
  deviation_pct: number;
  score: number;
  direction: "high" | "low";
  peer_count: number;
  status: "open" | "reviewed" | "dismissed";
  review_note: string | null;
  created_at: string;
  reviewed_at: string | null;
  product: Listing;
  shop: { id: number; name: string; slug: string; locality: string | null };
}

export interface AiModel {
  name: string;
  title: string;
  purpose: string;
  algorithm: string | null;
  trained_at: string | null;
  n_samples: number;
  metrics: Record<string, unknown>;
  note: string;
  uses_demo_data: boolean;
  duration_ms: number;
  runs: { at: string; n_samples: number; duration_ms: number }[];
}

export interface SearchCompare {
  query: string;
  normalized: string;
  methods: {
    key: string;
    title: string;
    note: string;
    results: { name: string; icon: string; category: string; score: number }[];
  }[];
}

// ------------------------------------------------------------------ plans and billing (P9.4 to P9.6; screens P9.10)

export type PlanCode = "free" | "growth" | "pro" | "business" | "partner_seat";
export type PlanPeriod = "year" | "month";
export type SubscriptionStatus = "advance" | "active" | "lapsed" | "cancelled" | "refunded";
export type SubscriptionSource = "shop_paid" | "advance" | "sponsored" | "guarantee_credit";

/** What a plan allows (backend/app/models/billing.py, Entitlements). The freshness tools are never here. */
export interface PlanEntitlements {
  demand_report: "teaser" | "full";
  low_stock_alerts: boolean;
  logins: number;
  roi_statement: boolean;
  priority_support: boolean;
  boost_discount_pct: number;
  outcome_guarantee: boolean;
  pro_insights: boolean;
  outlets: number;
}

/** One plan in one catalogue version (plans.plan_out). Prices are in paise. */
export interface PlanVersion {
  id: number;
  code: PlanCode;
  version: number;
  name: string;
  price_year_paise: number | null;
  price_month_paise: number | null;
  founding_year_paise: number | null;
  on_sale: boolean;
  entitlements: PlanEntitlements;
  effective_from: string | null;
  published_at: string | null;
  reason: string | null;
}

/** plans.subscription_out. */
export interface PlanSubscription {
  id: number;
  account_type: string;
  account_id: number;
  shop_ids: number[];
  plan_code: PlanCode;
  plan_version: number;
  period: PlanPeriod;
  price_paise: number;
  founding: boolean;
  founding_slot: number | null;
  source: SubscriptionSource;
  status: SubscriptionStatus;
  starts_at: string | null;
  ends_at: string | null;
  paid_until: string | null;
  paid_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** GET /api/admin/subscriptions: a subscription with the names of the shops it covers. */
export interface AdminSubscription extends PlanSubscription {
  shops: { id: number; name: string | null }[];
}

export interface GuaranteeProgress {
  applies: boolean;
  needed: number;
  quarter_start?: string;
  quarter_end?: string;
  customer_requests?: number;
  met?: boolean;
  credit_months?: number;
}

export interface FoundingCounter {
  cap: number;
  taken: number;
  remaining: number;
  cities: string[];
}

/** GET /api/owner/plan (plans.owner_plan). */
export interface OwnerPlan {
  plan: {
    code: PlanCode;
    name: string;
    version: number;
    entitlements: PlanEntitlements;
    subscription_id: number | null;
    /** "free" when no subscription is active (none, an advance not started yet, lapsed, cancelled or refunded). */
    status: "free" | SubscriptionStatus;
    source: SubscriptionSource | null;
    starts_at: string | null;
    ends_at: string | null;
  };
  subscription: PlanSubscription | null;
  advance: PlanSubscription | null;
  founding: {
    eligible: boolean;
    in_city: boolean;
    already_claimed: boolean;
    remaining: number;
    cap: number;
    price_year_paise: number | null;
  };
  guarantee: GuaranteeProgress;
  /** YYYY-MM-DD: before it, a shop can only pay a refundable advance. */
  billing_starts_on: string;
  on_sale: PlanVersion[];
}

export interface BillingAccount {
  id: number;
  kind: "shop_owner" | "distributor" | "lender";
  legal_name: string;
  address: string;
  state_code: string;
  gstin: string | null;
  contact_user_id: number;
}

export type GstMode = "unregistered" | "registered";

/** A payment request (billing.request_out): pay_to is what the UPI payment carries. */
export interface PaymentRequest {
  id: number;
  number: string;
  shop_id: number;
  purpose: "plan" | "boost";
  plan_code: PlanCode;
  plan_version: number;
  period: PlanPeriod;
  kind: "advance" | "new" | "renewal";
  founding: boolean;
  amount_paise: number;
  gst_mode: GstMode;
  gst_paise: number;
  total_paise: number;
  status: "open" | "paid" | "cancelled" | "refunded";
  created_at: string | null;
  payment_id: number | null;
  created_by: string;
  reason: string | null;
  /** NearShop's UPI ID and payee name (settings), the amount in rupees with two decimals, and the request number. */
  pay_to: { upi_id: string; payee_name: string; amount: string; note: string };
}

export type BillingDocumentType = "receipt" | "tax_invoice" | "refund_receipt" | "credit_note";

export interface BillingDocumentBrief {
  id: number;
  number: string;
  type: BillingDocumentType;
  issued_at: string | null;
  total_paise: number;
  draft: boolean;
}

export interface BillingActor {
  type: string;
  id?: number | null;
}

/** billing.payment_out. */
export interface BillingPayment {
  id: number;
  request_id: number;
  request_number: string;
  shop_id: number;
  credit_id: number;
  upi_reference: string;
  amount_paise: number;
  paid_at: string | null;
  matched_at: string | null;
  matched_by: BillingActor;
  match: "statement" | "manual" | "pilot_ledger" | "autopay" | "reconciliation";
  reason: string;
  subscription_id: number;
  document_id: number | null;
  status: "matched" | "refunded";
  refund: { at: string; by: BillingActor; reason: string; bank_reference: string; document_id: number } | null;
  settled_at: string | null;
  settlement_id: string | null;
}

/** The latest UPI Autopay mandate (gateway.mandate_out). checkout_url is set while it waits for approval. */
export interface AutopayMandate {
  id: number;
  status: "created" | "authorised" | "halted" | "cancelled";
  plan_code: PlanCode;
  plan_version: number;
  amount_paise: number;
  gst_paise: number;
  total_paise: number;
  checkout_url: string | null;
  subscription_id: number | null;
  debits: number;
  failed_debits: number;
  authorised_at: string | null;
  last_debit_at: string | null;
  halted_at: string | null;
  cancelled_at: string | null;
  cancelled_by: string | null;
  created_at: string | null;
}

/** GET /api/owner/billing. */
export interface OwnerBilling {
  /** Whether NearShop's UPI ID and payee name are set: until then no payment request can be made. */
  configured: boolean;
  account: BillingAccount | null;
  gst_mode: GstMode;
  requests: PaymentRequest[];
  payments: (BillingPayment & { documents: BillingDocumentBrief[] })[];
  autopay: { available: boolean; price_month_paise: number | null; mandate: AutopayMandate | null };
}

export interface BillingParty {
  legal_name: string;
  address: string;
  state_code: string;
  gstin: string | null;
}

/** One issued document, an immutable snapshot (billing.document_out). */
export interface BillingDocument {
  id: number;
  number: string;
  financial_year: string;
  seq: number;
  type: BillingDocumentType;
  issued_at: string | null;
  payment_date: string | null;
  format: string;
  /** "Draft format, not for use" until the CA approves the format (B22). */
  draft_line: string | null;
  wording: string;
  supplier: BillingParty;
  recipient: BillingParty;
  place_of_supply: string;
  description: string;
  sac: string | null;
  taxable_paise: number;
  gst_rate_pct: number;
  cgst_paise: number;
  sgst_paise: number;
  igst_paise: number;
  total_paise: number;
  payment_id: number;
  request_number: string;
  upi_reference: string;
  original_document_id: number | null;
  original_number: string | null;
  refund_reference: string | null;
  content_hash: string;
  /** Whether the stored document is exactly as issued. */
  intact: boolean;
}

/** billing.credit_out: a credit from a bank statement, matched by hand, from the pilot ledger or from the gateway. */
export interface BankCredit {
  id: number;
  upi_reference: string;
  credited_on: string;
  amount_paise: number;
  narration: string;
  source: "statement" | "manual" | "pilot_ledger" | "autopay" | "reconciliation";
  statement_id: number | null;
  request_number: string | null;
  status: "unmatched" | "matched" | "returned" | "closed";
  note: string | null;
  payment_id: number | null;
  alerted_at: string | null;
  returned: { at: string; by: BillingActor; reason: string; bank_reference: string } | null;
  closed: { at: string; by: BillingActor; reason: string; reference: string } | null;
  created_at: string | null;
}

/** POST /api/admin/billing/statements: what one statement upload did. */
export interface StatementImport {
  statement_id: number;
  credits: number;
  matched: number;
  unmatched: number;
  duplicates: number;
  debits_skipped: number;
  unreadable: number;
  rows: {
    upi_reference: string;
    amount_paise: number;
    credited_on: string;
    result: "matched" | "unmatched" | "duplicate";
    credit_id?: number | null;
    request_number?: string;
    reason?: string;
    payment_id?: number;
    status?: string | null;
  }[];
}
