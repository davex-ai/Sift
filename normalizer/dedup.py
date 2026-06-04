"""
Deduplicator — cross-source product matching and grouping.

The Problem:
    Jumia:  "Samsung Galaxy A15 128GB - Black"
    Konga:  "Samsung A15 Smartphone 128GB"
    Slot:   "Galaxy A15 6/128GB Dual SIM"

These are the same phone. We need to group them, keep the lowest price,
and show all buy links.

Algorithm:
  1. Normalize titles (lowercase, strip junk, extract brand/model/specs)
  2. Within same source: 85%+ similarity → keep first (duplicate)
  3. Cross-source: 85%+ → same product → ProductGroup
  4. Cross-source 60-85% → uncertain → check specs (and optionally LLM)
  5. Represent each unique product as a ProductGroup with N stores
"""

import re
import math
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional
from uuid import uuid4

from scrapers.base import Product

logger = logging.getLogger(__name__)

# ── Stopwords stripped before comparison ──────────────────────
_STOPWORDS = {
    "the", "a", "an", "and", "or", "in", "on", "for", "with", "by",
    "buy", "new", "best", "top", "official", "store", "online",
    "nigeria", "nigerian", "ng", "naira", "free", "delivery",
    "original", "genuine", "authentic", "brand",
    "smartphone", "phone", "mobile",          # too generic for phones
    "laptop", "computer",                     # generic but keep for search
    "product", "item",
}

# ── Common color words ────────────────────────────────────────
_COLORS = {
    "black", "white", "red", "blue", "green", "gold", "silver",
    "pink", "purple", "grey", "gray", "orange", "yellow", "brown",
    "rose", "midnight", "starlight", "space", "sierra",
}


# ══════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════

@dataclass
class SourceEntry:
    """One store's price+link for a product."""
    store: str
    price_ngn: Optional[float]
    price_display: str
    url: str
    availability: str = "unknown"
    rating: Optional[float] = None
    review_count: Optional[int] = None
    seller_name: Optional[str] = None
    condition: Optional[str] = None
    location: Optional[str] = None


@dataclass
class ProductGroup:
    """
    A normalized product with prices from one or more stores.
    This is the unit shown to users.
    """
    id: str = field(default_factory=lambda: str(uuid4())[:8])
    canonical_title: str = ""
    brand: Optional[str] = None
    model: Optional[str] = None
    specs: dict = field(default_factory=dict)    # {ram, storage, color, ...}

    sources: list[SourceEntry] = field(default_factory=list)
    image_url: Optional[str] = None
    condition: Optional[str] = None

    # Aggregated
    lowest_price: Optional[float] = None
    highest_price: Optional[float] = None
    best_store: Optional[str] = None            # store with lowest price
    avg_rating: Optional[float] = None
    total_reviews: int = 0

    score: float = 0.0

    # For price history display
    price_30d_avg: Optional[float] = None
    price_change_pct: Optional[float] = None

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def in_stock_sources(self) -> list[SourceEntry]:
        return [s for s in self.sources if s.availability == "in_stock"]


# ══════════════════════════════════════════════════════════════
# Title normalization
# ══════════════════════════════════════════════════════════════

def normalize_title(title: str) -> str:
    """
    Normalize a product title for comparison.
    'Samsung Galaxy A15 128GB - Black (2024)' → 'samsung galaxy a15 128gb'
    """
    s = title.lower()

    # Remove content in brackets/parens: (2024), [New], - color
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)

    # Remove punctuation except hyphen (for model names like "A-Series")
    s = re.sub(r"[/\\|:;,!?\"']", " ", s)

    # Remove trailing/leading hyphens after spaces
    s = re.sub(r"\s+-\s+", " ", s)

    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Remove stopwords
    words = [w for w in s.split() if w not in _STOPWORDS]

    # Remove pure color words that appear at end (often describe variant, not model)
    while words and words[-1] in _COLORS:
        words.pop()

    return " ".join(words)


def extract_specs(title: str) -> dict:
    """
    Extract structured attributes from a title.
    Returns dict like {ram: '8GB', storage: '128GB', screen: '6.5 inch'}
    """
    s = title.lower()
    specs = {}

    # RAM: "8gb ram", "8 gb ram", "8gb/128gb" (first part is RAM)
    ram_m = re.search(r"(\d+)\s*gb\s*(?:ram|memory)", s)
    if not ram_m:
        # "6/128gb" — first number is RAM
        slash_m = re.search(r"(\d+)\s*/\s*(\d+)\s*gb", s)
        if slash_m:
            specs["ram"] = f"{slash_m.group(1)}GB"
            specs["storage"] = f"{slash_m.group(2)}GB"
    else:
        specs["ram"] = f"{ram_m.group(1)}GB"

    # Storage: standalone "128gb", "256gb", "1tb"
    if "storage" not in specs:
        # TB
        tb_m = re.search(r"(\d+)\s*tb\b", s)
        if tb_m:
            specs["storage"] = f"{tb_m.group(1)}TB"
        else:
            # GB — pick the largest number that looks like storage
            gb_matches = re.findall(r"(\d+)\s*gb", s)
            storage_candidates = [
                int(g) for g in gb_matches
                if int(g) in {16, 32, 64, 128, 256, 512}
            ]
            if storage_candidates:
                specs["storage"] = f"{max(storage_candidates)}GB"

    # Screen size: "6.5 inch", '6.5"', "6.5-inch"
    screen_m = re.search(r"(\d+\.?\d*)\s*(?:inch|\"|-inch|')", s)
    if screen_m:
        specs["screen"] = f"{screen_m.group(1)}\""

    # Color
    for color in _COLORS:
        if re.search(rf"\b{color}\b", s):
            specs["color"] = color
            break

    # SIM
    if "dual sim" in s or "dual-sim" in s:
        specs["sim"] = "dual"
    elif "single sim" in s:
        specs["sim"] = "single"

    return specs


def extract_brand_model(title: str) -> tuple[Optional[str], Optional[str]]:
    """
    Best-effort brand/model extraction.
    'Samsung Galaxy A15 128GB' → ('Samsung', 'Galaxy A15')
    """
    known_brands = {
        "samsung", "apple", "iphone", "xiaomi", "redmi", "poco",
        "tecno", "infinix", "itel", "huawei", "honor", "oppo",
        "vivo", "oneplus", "realme", "nokia", "motorola", "asus",
        "lg", "sony", "panasonic", "qasa", "binatone", "syinix",
        "hisense", "haier", "hp", "dell", "lenovo", "acer", "asus",
        "logitech", "jbl", "sony", "bose", "anker", "oraimo", "tozo",
        "nasco", "midea", "thermocool", "scanfrost", "nexus",
        "xtrapower", "transcend", "sandisk", "seagate", "wd",
        "toshiba", "samsung", "crucial", "kingston",
    }

    title_lower = title.lower()
    words = title_lower.split()

    brand = None
    model_start = 0

    for i, word in enumerate(words):
        if word in known_brands:
            brand = word.capitalize()
            model_start = i + 1
            break

    # Model: take next 2-3 meaningful words
    model_words = words[model_start:model_start + 3]
    # Filter out specs from model
    model_words = [w for w in model_words if not re.match(r"^\d+(gb|tb|mah|mp|w)$", w)]
    model = " ".join(model_words[:2]).title() if model_words else None

    # Special case: Apple products
    iphone_m = re.search(r"(iphone\s+\w+(?:\s+\w+)?)", title_lower)
    if iphone_m:
        brand = "Apple"
        model = iphone_m.group(1).title()

    return brand, model


# ══════════════════════════════════════════════════════════════
# Similarity
# ══════════════════════════════════════════════════════════════

def title_similarity(a: str, b: str) -> float:
    """Similarity ratio between two normalized titles (0-1)."""
    return SequenceMatcher(None, a, b).ratio()


def spec_compatible(specs_a: dict, specs_b: dict) -> bool:
    """
    Are specs compatible? If both have storage and they differ → NOT same product.
    Absent spec = compatible (we just don't know).
    """
    for key in ("ram", "storage"):
        va = specs_a.get(key)
        vb = specs_b.get(key)
        if va and vb and va != vb:
            return False
    return True


def are_same_product(p1: Product, p2: Product, threshold: float = 0.82) -> bool:
    """
    Decide if two products from different stores are the same item.
    Uses title similarity + spec compatibility.
    """
    n1 = normalize_title(p1.title)
    n2 = normalize_title(p2.title)

    sim = title_similarity(n1, n2)

    if sim >= threshold:
        # High confidence — also check specs aren't contradictory
        s1 = extract_specs(p1.title)
        s2 = extract_specs(p2.title)
        return spec_compatible(s1, s2)

    if sim >= 0.60:
        # Medium confidence — require brand+model match
        b1, m1 = extract_brand_model(p1.title)
        b2, m2 = extract_brand_model(p2.title)
        if b1 and b2 and b1.lower() == b2.lower():
            if m1 and m2 and m1.lower() == m2.lower():
                s1 = extract_specs(p1.title)
                s2 = extract_specs(p2.title)
                return spec_compatible(s1, s2)

    return False


# ══════════════════════════════════════════════════════════════
# Deduplicator
# ══════════════════════════════════════════════════════════════

class Deduplicator:
    """
    Takes raw Product list from all scrapers.
    Returns deduplicated ProductGroup list.

    Step 1: Within each source — remove exact/near duplicates (same store)
    Step 2: Cross-source — group same products from different stores
    """

    def __init__(
        self,
        same_source_threshold: float = 0.85,
        cross_source_threshold: float = 0.82,
    ):
        self.same_threshold = same_source_threshold
        self.cross_threshold = cross_source_threshold

    def deduplicate(self, products: list[Product]) -> list[ProductGroup]:
        # Step 1: Deduplicate within each source
        by_source: dict[str, list[Product]] = {}
        for p in products:
            by_source.setdefault(p.source, []).append(p)

        deduped_per_source: list[Product] = []
        for source, source_products in by_source.items():
            deduped = self._dedup_same_source(source_products)
            logger.debug(f"[Dedup] {source}: {len(source_products)} → {len(deduped)}")
            deduped_per_source.extend(deduped)

        # Step 2: Group same products across sources
        groups = self._group_cross_source(deduped_per_source)
        logger.info(f"[Dedup] {len(deduped_per_source)} products → {len(groups)} groups")
        return groups

    def _dedup_same_source(self, products: list[Product]) -> list[Product]:
        """
        Within a single store: remove near-duplicates.
        Keep the product with the lowest price if same normalized title.
        """
        seen: list[tuple[str, Product]] = []  # (normalized_title, product)
        result: list[Product] = []

        for p in products:
            norm = normalize_title(p.title)
            duplicate = False

            for norm_seen, p_seen in seen:
                if title_similarity(norm, norm_seen) >= self.same_threshold:
                    # If new one is cheaper, replace
                    if (
                        p.price_ngn
                        and p_seen.price_ngn
                        and p.price_ngn < p_seen.price_ngn
                    ):
                        result.remove(p_seen)
                        seen.remove((norm_seen, p_seen))
                        break
                    else:
                        duplicate = True
                        break

            if not duplicate:
                seen.append((norm, p))
                result.append(p)

        return result

    def _group_cross_source(self, products: list[Product]) -> list[ProductGroup]:
        """
        Group products from different stores that are the same item.
        Returns list of ProductGroups.
        """
        groups: list[ProductGroup] = []

        for product in products:
            matched_group = None

            for group in groups:
                # Compare against the best product we have in this group
                # (use canonical title as reference)
                ref_product = Product(
                    title=group.canonical_title,
                    source="__ref__",
                    url="",
                )
                if are_same_product(product, ref_product, self.cross_threshold):
                    matched_group = group
                    break

            if matched_group:
                self._merge_into_group(matched_group, product)
            else:
                groups.append(self._new_group(product))

        return groups

    def _new_group(self, product: Product) -> ProductGroup:
        """Create a new ProductGroup from a single product."""
        brand, model = extract_brand_model(product.title)
        specs = extract_specs(product.title)

        from utils.currency import format_ngn
        entry = SourceEntry(
            store=product.source,
            price_ngn=product.price_ngn,
            price_display=format_ngn(product.price_ngn),
            url=product.url,
            availability=product.availability,
            rating=product.rating,
            review_count=product.review_count,
            seller_name=product.seller_name,
            condition=product.condition,
            location=product.location,
        )

        g = ProductGroup(
            canonical_title=product.title,
            brand=brand,
            model=model,
            specs=specs,
            sources=[entry],
            image_url=product.image_url,
            condition=product.condition,
        )
        self._recalculate(g)
        return g

    def _merge_into_group(self, group: ProductGroup, product: Product) -> None:
        """Add a product from a new store into an existing group."""
        # Don't add same store twice
        existing_stores = {s.store for s in group.sources}
        if product.source in existing_stores:
            return

        from utils.currency import format_ngn
        entry = SourceEntry(
            store=product.source,
            price_ngn=product.price_ngn,
            price_display=format_ngn(product.price_ngn),
            url=product.url,
            availability=product.availability,
            rating=product.rating,
            review_count=product.review_count,
            seller_name=product.seller_name,
            condition=product.condition,
            location=product.location,
        )
        group.sources.append(entry)

        # Update canonical title if new one is more detailed
        if len(product.title) > len(group.canonical_title):
            group.canonical_title = product.title
            brand, model = extract_brand_model(product.title)
            if brand:
                group.brand = brand
            if model:
                group.model = model

        # Use image if we don't have one
        if not group.image_url and product.image_url:
            group.image_url = product.image_url

        self._recalculate(group)

    def _recalculate(self, group: ProductGroup) -> None:
        """Recompute aggregate fields after sources change."""
        prices = [s.price_ngn for s in group.sources if s.price_ngn]
        if prices:
            group.lowest_price = min(prices)
            group.highest_price = max(prices)
            cheapest = min(group.sources, key=lambda s: s.price_ngn or float("inf"))
            group.best_store = cheapest.store

        ratings = [(s.rating, s.review_count or 1) for s in group.sources if s.rating]
        if ratings:
            weighted = sum(r * w for r, w in ratings)
            total_w = sum(w for _, w in ratings)
            group.avg_rating = round(weighted / total_w, 1)

        group.total_reviews = sum(s.review_count or 0 for s in group.sources)
