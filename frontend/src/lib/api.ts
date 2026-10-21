// Typed client for the FastAPI backend (proxied at /api by Next.js).

export type Dimension = {
  score: number | null;
  label: string;
  confidence: number;
  evidence_counts?: Record<string, number>;
  notes?: string[];
};

export type Quality = {
  status: "Unverified" | "Limited evidence" | "Evidence-backed";
  overall_confidence: number;
  quality_score: number | null;
  dimensions: {
    design_compatibility: Dimension;
    construction: Dimension;
    material: Dimension;
    seller_reliability: Dimension;
    value: Dimension;
  };
};

export type LandedCost = {
  goods_gbp: number;
  domestic_shipping_gbp: number;
  agent_fee_gbp: number;
  international_shipping_gbp: number;
  estimated_weight_kg: number;
  duty_gbp: number;
  vat_gbp: number;
  handling_gbp: number;
  total_gbp: number;
  assumptions: string;
};

export type Recommendation = {
  score: number;
  design_score: number;
  components: Record<string, number>;
  contributions: Record<string, number>;
  reasons: string[];
  cautions: string[];
  nearest_positive: { label: string; product_id: number | null; kind: string } | null;
  explore: boolean;
};

export type SelectiveMode = "balanced" | "high" | "extreme";
export type GateState = "calibrated" | "uncalibrated" | "pending" | "unavailable" | "off";
export const MODES: SelectiveMode[] = ["balanced", "high", "extreme"];
export const MODE_LABEL: Record<SelectiveMode, string> = { balanced: "Balanced", high: "High confidence", extreme: "Extremely selective" };

/** Per piece, from the server's gate. ``estimate``/``bound`` exist only when your decisions have been calibrated. */
export type Confidence = {
  state: GateState;
  mode: SelectiveMode;
  passes: SelectiveMode[];
  excluded: string | null;
  estimate?: number;
  bound?: number | null;
  target?: number;
  level?: number;
  learning?: boolean;
};

export type CalibrationSummary = { id: number; created_at: string; labels: number; approvals: number; map: string; auc: number | null; brier: number; ece: number; base_rate: number };
export type LabelProgress = { labels: number; approvals: number; non_approvals: number; needed: number; needed_each: number; sufficient: boolean };

/** What the gate did for one feed, and why the feed is what it is. */
export type Gate = {
  policy: "selective" | "learning" | "ranked";
  mode: SelectiveMode;
  mode_label: string;
  state: GateState;
  target: number | null;
  confidence: number | null;
  targets_validated: boolean;
  shown: number;
  candidates: number;
  eligible: Record<SelectiveMode, number>;
  qualified: Record<SelectiveMode, number> | null;
  excluded: Record<string, { count: number; label: string }>;
  calibration: CalibrationSummary | null;
  progress: LabelProgress | null;
  message: string | null;
  sampling?: { method: string; pool: number; expected: number };
};

export type FeedResponse = { feed_id: string | null; items: Product[]; total_candidates: number; learning_strength: number; decisions: number; gate: Gate };

export type ObservedRate = { rated: number; approved: number; rate: number | null; interval: [number, number] | null };
export type ReliabilityBin = { lo: number; hi: number; n: number; mean_estimate: number; observed: number; interval: [number, number] };
export type SelectiveStatus = {
  enabled: boolean;
  mode: SelectiveMode;
  modes: { mode: SelectiveMode; label: string; target: number; confidence: number }[];
  targets_validated: boolean;
  calibration: LabelProgress & {
    state: "calibrated" | "pending" | "insufficient";
    calibration?: { id: number; created_at: string; n_labels: number; n_positive: number; auc_out_of_fold: number | null; map: string; brier: number; ece: number; base_rate: number };
  };
  performance: {
    served_in_mode: Record<SelectiveMode, ObservedRate>;
    passed_gate: Record<SelectiveMode, ObservedRate>;
    learning_picks: ObservedRate;
    uncalibrated_ranking: ObservedRate;
    min_rated: number;
    note: string;
  };
  reliability?: ReliabilityBin[];
  history: { id: number; created_at: string; labels: number; approvals: number; active: boolean; map: string; brier: number; ece: number; current_model: boolean }[];
};

export type Evidence = {
  id: number;
  kind: string;
  kind_label: string;
  aspect: string;
  claim: string;
  polarity: number;
  strength: number;
  source_url: string | null;
  source_title: string | null;
  is_affiliate: boolean;
  captured_at: string | null;
  created_by: string;
  scope?: "seller";
};

export type ProductImage = {
  id: number;
  src: string | null;
  remote_url: string | null;
  status: string;
  role?: string | null;
  selected?: boolean;
};

export type Product = {
  id: number;
  title: string;
  title_original: string;
  seller: { id: number; name: string; platform: string; reputation: string } | null;
  seller_item_code: string | null;
  brand: string | null;
  category: string | null;
  colours: string[];
  materials: { fibre: string; pct: number | null }[];
  material_text: string | null;
  gsm: number | null;
  sizes: string[];
  seasons: string[];
  fit_tags: string[];
  logo_level: string;
  price: number | null;
  currency: string;
  price_gbp: number | null;
  landed_cost: LandedCost | null;
  source_url: string | null;
  links: Record<string, string>;
  images: ProductImage[];
  /** Size charts found in the gallery: shown for sizing, never used for visual taste. */
  sizing_images?: ProductImage[];
  /** discovered (catalogue listing only) · partial · analysed · deep */
  analysis_state?: "discovered" | "partial" | "analysed" | "deep";
  status: string;
  data_origin: "scraped" | "manual" | "csv" | "json" | "bookmarklet" | "seed";
  completeness: "complete" | "partial" | "seed_unverified";
  is_new: boolean;
  first_seen_at: string | null;
  last_seen_at: string | null;
  group_id: number | null;
  quality: Quality;
  pros: string[];
  cons: string[];
  saved: { list: string; priority: number; note: string | null; target_size?: string | null; added_at?: string } | null;
  recommendation?: Recommendation;
  /** The selective gate's view of this piece (feeds and the product page). Numbers only with a calibration. */
  confidence?: Confidence;
  /** Discover feed only: other listings of the same garment (one family is shown once). */
  family?: { size: number; offers: { product_id: number; seller: string | null; price: number | null; currency: string }[] };
  // detail only
  description?: string | null;
  description_original?: string | null;
  translation_method?: string | null;
  evidence?: Evidence[];
  observations?: { observed_at: string; price: number | null; sizes: string[]; status: string; change: string | null; source_url: string }[];
  missing?: string[];
};

export type Seller = {
  id: number;
  name: string;
  slug: string;
  platform: string;
  base_url: string | null;
  status: "active" | "paused";
  priority: number;
  notes: string | null;
  scan_interval_hours: number;
  reputation_status: string;
  evidence_count: number;
  products: { active: number; unavailable: number; scraped: number; manual: number; seed: number; new: number; images_downloaded: number };
  last_scan_at: string | null;
  last_scan_status: string | null;
  last_scan_message: string | null;
  last_run: { pages_fetched: number; products_seen: number; new: number; updated: number; errors: string[]; status: string } | null;
};

export type Job = {
  id: number;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "partial" | "cancelled";
  params: Record<string, unknown>;
  progress: number;
  message: string | null;
  result: Record<string, unknown>;
  log: string[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  control?: "pause" | "cancel" | null;
  stats?: ScanStats | null;
};

export type ScanSellerProgress = {
  id: number;
  name: string;
  pages: number;
  total_pages: number | null;
  discovery_done: boolean;
  catalogue_complete: boolean;
  status: string | null;
  discovered: number;
  new: number;
  changed: number;
  unchanged: number;
};

export type ScanStats = {
  stage: string;
  discovered: number;
  new: number;
  changed: number;
  unchanged_skipped: number;
  details_fetched: number;
  details_not_modified: number;
  images_downloaded: number;
  image_bytes: number;
  images_reused: number;
  size_charts_excluded: number;
  text_graphics_excluded: number;
  repeated_excluded: number;
  products_analysed: number;
  duplicates_found: number;
  similar_found: number;
  deep_analysed: number;
  removed: number;
  verified_present: number;
  errors: number;
  error_samples: string[];
  elapsed_s?: number;
  pending_details?: number;
  pending_images?: number;
  pending_products?: number;
  in_flight?: number;
  requests?: number;
  throughput_per_min?: number;
  eta_seconds?: [number, number] | null;
  discovery_complete?: boolean;
  sellers?: ScanSellerProgress[];
};

export type ScanStatus = {
  active: Job[];
  last: Job | null;
  pending_analysis: number;
  sellers: {
    id: number;
    name: string;
    products: number;
    states: { discovered: number; partial: number; analysed: number; deep: number };
    last_scan_at: string | null;
    last_status: string | null;
    catalogue_complete: boolean | null;
    pages: number | null;
  }[];
};

export type Comparison = {
  product: Product;
  alternatives: { relation: string; relation_label: string; score: number; signals: Record<string, unknown>; user_confirmed: boolean | null; product: Product }[];
  verdict: { recommendation: string; notes: string[] };
};

export type WardrobeItem = {
  id: number;
  name: string;
  category: string | null;
  brand: string | null;
  colours: string[];
  materials: { fibre: string; pct: number | null }[];
  seasons: string[];
  notes: string | null;
  rating: number | null;
  source_url: string | null;
  product_id: number | null;
  image: string | null;
  added_at: string;
};

export type OutfitPiece = { id: number; name: string; category: string | null; colours: string[]; kind: "owned" | "candidate"; image: string | null };

export type WardrobeResponse = {
  items: WardrobeItem[];
  analysis: {
    count: number;
    by_category: Record<string, number>;
    by_group: Record<string, number>;
    gaps: { season: string; category: string; colours: string[]; have: number; want: number }[];
    redundancy: { category: string; colour_family: string; count: number; advice: string }[];
    outfits: { pieces: OutfitPiece[]; score: number; uses_candidates: number }[];
  };
  budget: { monthly_gbp: number | null; season_gbp: number | null };
};

export type Facets = {
  brands: { value: string; count: number }[];
  categories: { value: string; count: number }[];
  materials: { value: string; count: number }[];
  sizes: { value: string; count: number }[];
  seasons: { value: string; count: number }[];
  sellers: { value: number; label: string; count: number }[];
  price_range: [number | null, number | null];
  total: number;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/unlock")) {
    const body = await res.clone().json().catch(() => ({}));
    if (body.locked) {
      window.location.href = "/unlock";
      throw new ApiError(401, "Locked");
    }
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map((d: { msg: string }) => d.msg).join("; ") : body.error ?? msg;
    } catch {
      /* not JSON */
    }
    if (res.status === 500 || res.status === 502 || res.status === 504) msg = `The backend is not responding (${res.status}). Is it running?`;
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const fetcher = <T,>(url: string): Promise<T> => fetch(url).then((r) => handle<T>(r));

export const api = {
  get: <T,>(url: string) => fetch(url).then((r) => handle<T>(r)),
  post: <T,>(url: string, body?: unknown) =>
    fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) }).then((r) => handle<T>(r)),
  put: <T,>(url: string, body: unknown) =>
    fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => handle<T>(r)),
  patch: <T,>(url: string, body: unknown) =>
    fetch(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => handle<T>(r)),
  del: <T,>(url: string) => fetch(url, { method: "DELETE" }).then((r) => handle<T>(r)),
  form: <T,>(url: string, data: FormData) => fetch(url, { method: "POST", body: data }).then((r) => handle<T>(r)),
};

export function qs(params: Record<string, string | number | boolean | string[] | number[] | null | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, String(x)));
    else sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const REASONS: { value: string; label: string; hint: string }[] = [
  { value: "silhouette", label: "Dislike the silhouette", hint: "Learns about cut/fit" },
  { value: "colour", label: "Dislike the colour", hint: "Only this colour is down-weighted" },
  { value: "logo", label: "Dislike the logo", hint: "Learns about branding visibility" },
  { value: "fabric", label: "Fabric doesn't appeal", hint: "Learns about materials" },
  { value: "not_my_style", label: "Not my style", hint: "A general taste rejection" },
  { value: "construction", label: "Poor apparent quality", hint: "Raises quality strictness — not a taste dislike" },
  { value: "too_expensive", label: "Too expensive for the quality", hint: "Sets a value ceiling — not a taste dislike" },
  { value: "already_own", label: "Already own something similar", hint: "Avoids redundancy — not a taste dislike" },
  { value: "too_repetitive", label: "Too repetitive", hint: "Varies the feed — not a taste dislike" },
  { value: "wrong_category", label: "Wrong category (listing error)", hint: "A data error — nothing is learned" },
  { value: "specific_item", label: "Don't like this specific item", hint: "Hides near-identical listings only" },
];
