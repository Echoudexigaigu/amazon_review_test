#!/usr/bin/env python3
"""
Live Amazon multi-ASIN feasibility test.

Purpose
-------
1. Discover current ASIN candidates from Amazon search-result pages.
2. Test 10-20 products across 3-5 categories.
3. Measure accessibility, field completeness, duplicate behavior,
   review-volume coverage, variants, product age, and cross-run updates.
4. Optionally test the dedicated /product-reviews/ endpoint.

Scope
-----
- Low-frequency Requests + BeautifulSoup only.
- No login automation, cookie injection, proxy rotation, CAPTCHA bypass,
  or stealth-browser functionality.
- Stops after CAPTCHA, access denial, rate limiting, or comparable blocking.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup, Tag


CATEGORY_SEARCH_TERMS: dict[str, list[str]] = {
    "Grocery": ["protein shake", "coffee beans", "snack bars"],
    "Electronics": ["wireless mouse", "usb c charger", "bluetooth headphones"],
    "Beauty": ["shampoo", "face moisturizer", "hair dryer"],
    "Home": ["storage organizer", "kitchen scale", "vacuum storage bags"],
    "Office": ["desk organizer", "notebook", "office chair mat"],
}

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
RATING_RE = re.compile(r"([0-5](?:\.\d+)?)\s+out\s+of\s+5", re.IGNORECASE)
INTEGER_RE = re.compile(r"(\d[\d,]*)")
DATE_PATTERNS = ("%B %d, %Y", "%b %d, %Y")

BLOCK_MARKERS = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "robot check",
    "captcha",
    "automated access to amazon data",
)

BLOCKING_RESULTS = {
    "rate_limited",
    "access_denied",
    "server_or_block_error",
    "login_required",
    "captcha_or_bot_block",
}

PRODUCT_REVIEW_BLOCK_SELECTOR = 'div[data-hook="review"]'


@dataclass
class AccessRecord:
    run_id: str
    requested_at_utc: str
    request_type: str
    category: str | None
    asin: str | None
    requested_url: str
    final_url: str | None
    http_status: int | None
    elapsed_ms: int | None
    response_bytes: int | None
    redirected: bool | None
    result: str
    error: str | None


@dataclass
class ProductRecord:
    run_id: str
    asin: str
    category: str
    search_term: str
    source_url: str
    product_title: str | None
    brand_raw: str | None
    price_raw: str | None
    overall_rating: float | None
    overall_rating_raw: str | None
    rating_count: int | None
    rating_count_raw: str | None
    date_first_available_raw: str | None
    date_first_available_iso: str | None
    listing_age_days: int | None
    age_bucket: str
    has_variants: bool
    variant_evidence: str | None
    review_volume_bucket: str
    embedded_review_count: int
    unique_embedded_review_count: int
    embedded_duplicate_count: int
    review_id_completeness: float
    reviewer_name_completeness: float
    rating_completeness: float
    title_completeness: float
    review_text_completeness: float
    review_date_completeness: float
    variant_completeness: float
    language_completeness: float
    collected_at_utc: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def element_text(element: Tag | None) -> str | None:
    if element is None:
        return None
    return clean_text(element.get_text(" ", strip=True))


def parse_rating(value: str | None) -> float | None:
    if not value:
        return None
    match = RATING_RE.search(value)
    if not match:
        return None
    try:
        rating = float(match.group(1))
    except ValueError:
        return None
    return rating if 0 <= rating <= 5 else None


def parse_integer(value: str | None) -> int | None:
    if not value:
        return None
    match = INTEGER_RE.search(value)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_amazon_date(value: str | None) -> date | None:
    if not value:
        return None
    normalized = clean_text(value)
    if not normalized:
        return None
    normalized = re.sub(
        r"^date first available\s*[:\-]?\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    for pattern in DATE_PATTERNS:
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            continue
    return None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def field_completeness(records: list[dict[str, Any]], field: str) -> float:
    if not records:
        return 0.0
    present = sum(
        record.get(field) is not None and record.get(field) != ""
        for record in records
    )
    return round(present / len(records), 4)


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def save_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Connection": "keep-alive",
        }
    )
    return session


def classify_response(response: requests.Response, html: str) -> str:
    html_lower = html.lower()
    final_path = urlparse(response.url).path.lower()
    if response.status_code == 429:
        return "rate_limited"
    if response.status_code in {401, 403}:
        return "access_denied"
    if response.status_code >= 500:
        return "server_or_block_error"
    if "/ap/signin" in final_path:
        return "login_required"
    if any(marker in html_lower for marker in BLOCK_MARKERS):
        return "captcha_or_bot_block"
    if response.status_code != 200:
        return f"http_{response.status_code}"
    return "reachable"


def request_page(
    session: requests.Session,
    *,
    run_id: str,
    request_type: str,
    url: str,
    category: str | None,
    asin: str | None,
    timeout_seconds: int,
) -> tuple[requests.Response | None, str | None, AccessRecord]:
    requested_at = utc_now()
    start = time.perf_counter()
    try:
        response = session.get(url, timeout=(10, timeout_seconds), allow_redirects=True)
    except requests.RequestException as error:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return None, None, AccessRecord(
            run_id=run_id,
            requested_at_utc=requested_at,
            request_type=request_type,
            category=category,
            asin=asin,
            requested_url=url,
            final_url=None,
            http_status=None,
            elapsed_ms=elapsed_ms,
            response_bytes=None,
            redirected=None,
            result="network_error",
            error=str(error),
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    html = response.text
    result = classify_response(response, html)
    return response, html, AccessRecord(
        run_id=run_id,
        requested_at_utc=requested_at,
        request_type=request_type,
        category=category,
        asin=asin,
        requested_url=url,
        final_url=response.url,
        http_status=response.status_code,
        elapsed_ms=elapsed_ms,
        response_bytes=len(response.content),
        redirected=bool(response.history),
        result=result,
        error=None,
    )


def polite_sleep(delay_seconds: float) -> None:
    time.sleep(delay_seconds)


def extract_search_asins(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for node in soup.select("[data-asin]"):
        value = clean_text(node.get("data-asin"))
        if not value:
            continue
        asin = value.upper()
        if ASIN_RE.fullmatch(asin) and asin not in seen:
            seen.add(asin)
            found.append(asin)
    for link in soup.select('a[href*="/dp/"]'):
        href = link.get("href")
        if not href:
            continue
        match = re.search(r"/dp/([A-Z0-9]{10})(?:[/?]|$)", href, flags=re.IGNORECASE)
        if not match:
            continue
        asin = match.group(1).upper()
        if asin not in seen:
            seen.add(asin)
            found.append(asin)
    return found


def discover_candidates(
    session: requests.Session,
    *,
    run_id: str,
    categories: dict[str, list[str]],
    candidates_per_category: int,
    delay_seconds: float,
    timeout_seconds: int,
    rng: random.Random,
    access_records: list[AccessRecord],
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    globally_seen: set[str] = set()
    for category, terms in categories.items():
        terms_copy = list(terms)
        rng.shuffle(terms_copy)
        category_asins: list[tuple[str, str]] = []
        for term in terms_copy:
            search_url = f"https://www.amazon.com/s?k={quote_plus(term)}"
            response, html, access = request_page(
                session,
                run_id=run_id,
                request_type="search_discovery",
                url=search_url,
                category=category,
                asin=None,
                timeout_seconds=timeout_seconds,
            )
            access_records.append(access)
            print(f"[search] {category:12s} | {term:24s} | {access.result}")
            if access.result in BLOCKING_RESULTS:
                raise RuntimeError(f"Stopping after restricted search response: {access.result}")
            if response is None or html is None or access.result != "reachable":
                polite_sleep(delay_seconds)
                continue
            discovered = extract_search_asins(html)
            rng.shuffle(discovered)
            for asin in discovered:
                if asin in globally_seen:
                    continue
                globally_seen.add(asin)
                category_asins.append((asin, term))
                if len(category_asins) >= candidates_per_category:
                    break
            polite_sleep(delay_seconds)
            if len(category_asins) >= candidates_per_category:
                break
        for asin, term in category_asins:
            candidates.append({"asin": asin, "category": category, "search_term": term})
    return candidates


def find_labeled_value(soup: BeautifulSoup, label: str) -> str | None:
    label_lower = label.lower()
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        left = clean_text(cells[0].get_text(" ", strip=True))
        right = clean_text(cells[1].get_text(" ", strip=True))
        if left and label_lower in left.lower():
            return right
    for item in soup.select(
        "#detailBullets_feature_div li, "
        "#productDetails_detailBullets_sections1 tr, "
        "#productDetails_techSpec_section_1 tr, "
        "#productDetails_db_sections tr"
    ):
        text = clean_text(item.get_text(" ", strip=True))
        if not text or label_lower not in text.lower():
            continue
        match = re.search(rf"{re.escape(label)}\s*[:\-]?\s*(.+)$", text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return None


def detect_variants(soup: BeautifulSoup, html: str) -> tuple[bool, str | None]:
    selectors = [
        "#twister",
        "#variation_color_name",
        "#variation_size_name",
        "#variation_style_name",
        "#variation_flavor_name",
        "#variation_number_of_items",
        '[id^="variation_"]',
    ]
    matched = [selector for selector in selectors if soup.select_one(selector) is not None]
    if matched:
        return True, ",".join(matched[:5])
    lower = html.lower()
    markers = ['"dimensionvaluesdisplaydata"', '"variationvalues"', "twisterjsinitializer", "twister-plus"]
    matched_markers = [marker for marker in markers if marker in lower]
    if matched_markers:
        return True, ",".join(matched_markers)
    return False, None


def parse_review_text(block: Tag) -> tuple[str | None, str | None]:
    container = block.select_one('[data-hook="reviewRichContentContainer"]')
    if container is None:
        container = (
            block.select_one('[data-hook="review-body"]')
            or block.select_one('[data-hook="reviewText"]')
            or block.select_one(".review-text-content")
        )
    if container is None:
        return None, None
    paragraphs = [element_text(paragraph) for paragraph in container.select("p")]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    text = " ".join(paragraphs) if paragraphs else element_text(container)
    language = clean_text(container.get("lang"))
    if not language:
        language_node = container.select_one("[lang]")
        if language_node is not None:
            language = clean_text(language_node.get("lang"))
    return text, language


def parse_review(
    block: Tag,
    *,
    asin: str,
    category: str,
    source_url: str,
    run_id: str,
) -> dict[str, Any]:
    review_id = clean_text(block.get("id"))
    reviewer_name = element_text(block.select_one(".a-profile-name"))
    rating_raw = element_text(
        block.select_one('[data-hook="review-star-rating"]')
        or block.select_one('[data-hook="cmps-review-star-rating"]')
        or block.select_one(".review-rating")
    )
    title_node = (
        block.select_one('[data-hook="reviewTitle"]')
        or block.select_one('[data-hook="review-title"]')
        or block.select_one(".review-title")
    )
    title = element_text(title_node)
    title_language = clean_text(title_node.get("lang")) if title_node is not None else None
    review_text, review_language = parse_review_text(block)
    review_date_raw = element_text(
        block.select_one('[data-hook="review-date"]')
        or block.select_one(".review-date")
    )
    variant_raw = element_text(
        block.select_one('[data-hook="format-strip"]')
        or block.select_one('[data-hook="product-variation-attributes"]')
        or block.select_one(".review-format-strip")
    )
    verified_purchase = block.select_one('[data-hook="avp-badge"]') is not None
    helpful_votes_raw = element_text(
        block.select_one('[data-hook="helpful-vote-statement"]')
        or block.select_one(".cr-vote-text")
    )
    if review_id:
        dedup_source = f"amazon|{asin}|{review_id}"
    else:
        dedup_source = "|".join(
            ["amazon", asin, reviewer_name or "", review_date_raw or "", review_text or ""]
        )
    return {
        "run_id": run_id,
        "source": "amazon_public_product_page",
        "category": category,
        "asin": asin,
        "review_id": review_id,
        "reviewer_name": reviewer_name,
        "rating": parse_rating(rating_raw),
        "rating_raw": rating_raw,
        "title": title,
        "title_language": title_language,
        "review_text": review_text,
        "review_language": review_language,
        "review_date_raw": review_date_raw,
        "variant_raw": variant_raw,
        "verified_purchase": verified_purchase,
        "helpful_votes_raw": helpful_votes_raw,
        "source_url": source_url,
        "collected_at_utc": utc_now(),
        "dedup_key": sha256_text(dedup_source),
    }


def classify_review_volume(rating_count: int | None, *, low_max: int, high_min: int) -> str:
    if rating_count is None:
        return "unknown"
    if rating_count <= low_max:
        return "low"
    if rating_count >= high_min:
        return "high"
    return "medium"


def classify_age(listing_date: date | None, *, new_days: int, old_days: int) -> tuple[int | None, str]:
    if listing_date is None:
        return None, "unknown"
    age_days = (date.today() - listing_date).days
    if age_days < 0:
        return age_days, "future_or_invalid"
    if age_days <= new_days:
        return age_days, "new"
    if age_days >= old_days:
        return age_days, "old"
    return age_days, "mature"


def parse_product_page(
    *,
    run_id: str,
    asin: str,
    category: str,
    search_term: str,
    source_url: str,
    html: str,
    low_review_max: int,
    high_review_min: int,
    new_days: int,
    old_days: int,
) -> tuple[ProductRecord, list[dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    product_title = element_text(soup.select_one("#productTitle"))
    brand_raw = element_text(soup.select_one("#bylineInfo"))
    price_raw = element_text(
        soup.select_one(".a-price .a-offscreen")
        or soup.select_one("#priceblock_ourprice")
        or soup.select_one("#priceblock_dealprice")
    )
    rating_node = soup.select_one("#acrPopover") or soup.select_one('[data-hook="rating-out-of-text"]')
    overall_rating_raw = None
    if rating_node is not None:
        overall_rating_raw = clean_text(rating_node.get("title")) or element_text(rating_node)
    rating_count_raw = element_text(
        soup.select_one("#acrCustomerReviewText")
        or soup.select_one('[data-hook="total-review-count"]')
    )
    rating_count = parse_integer(rating_count_raw)
    date_first_available_raw = find_labeled_value(soup, "Date First Available")
    listing_date = parse_amazon_date(date_first_available_raw)
    age_days, age_bucket = classify_age(listing_date, new_days=new_days, old_days=old_days)
    has_variants, variant_evidence = detect_variants(soup, html)
    review_blocks = soup.select(PRODUCT_REVIEW_BLOCK_SELECTOR)
    reviews = [
        parse_review(block, asin=asin, category=category, source_url=source_url, run_id=run_id)
        for block in review_blocks
    ]
    unique_keys = {review["dedup_key"] for review in reviews}
    product = ProductRecord(
        run_id=run_id,
        asin=asin,
        category=category,
        search_term=search_term,
        source_url=source_url,
        product_title=product_title,
        brand_raw=brand_raw,
        price_raw=price_raw,
        overall_rating=parse_rating(overall_rating_raw),
        overall_rating_raw=overall_rating_raw,
        rating_count=rating_count,
        rating_count_raw=rating_count_raw,
        date_first_available_raw=date_first_available_raw,
        date_first_available_iso=listing_date.isoformat() if listing_date else None,
        listing_age_days=age_days,
        age_bucket=age_bucket,
        has_variants=has_variants,
        variant_evidence=variant_evidence,
        review_volume_bucket=classify_review_volume(
            rating_count,
            low_max=low_review_max,
            high_min=high_review_min,
        ),
        embedded_review_count=len(reviews),
        unique_embedded_review_count=len(unique_keys),
        embedded_duplicate_count=len(reviews) - len(unique_keys),
        review_id_completeness=field_completeness(reviews, "review_id"),
        reviewer_name_completeness=field_completeness(reviews, "reviewer_name"),
        rating_completeness=field_completeness(reviews, "rating"),
        title_completeness=field_completeness(reviews, "title"),
        review_text_completeness=field_completeness(reviews, "review_text"),
        review_date_completeness=field_completeness(reviews, "review_date_raw"),
        variant_completeness=field_completeness(reviews, "variant_raw"),
        language_completeness=field_completeness(reviews, "review_language"),
        collected_at_utc=utc_now(),
    )
    return product, reviews


def feature_tags(record: ProductRecord) -> set[str]:
    return {
        f"category:{record.category}",
        f"volume:{record.review_volume_bucket}",
        f"variant:{record.has_variants}",
        f"age:{record.age_bucket}",
    }


def select_balanced_products(
    records: list[ProductRecord],
    *,
    target: int,
    rng: random.Random,
) -> list[ProductRecord]:
    if len(records) <= target:
        return list(records)
    remaining = list(records)
    selected: list[ProductRecord] = []
    covered: set[str] = set()
    category_counts: Counter[str] = Counter()
    required_tags = {
        *(f"category:{category}" for category in CATEGORY_SEARCH_TERMS),
        "volume:low",
        "volume:medium",
        "volume:high",
        "variant:True",
        "variant:False",
        "age:new",
        "age:old",
    }
    while remaining and len(selected) < target:
        scored: list[tuple[float, float, ProductRecord]] = []
        for record in remaining:
            tags = feature_tags(record)
            new_required = len((tags & required_tags) - covered)
            category_balance = 1 / (1 + category_counts[record.category])
            known_quality = sum(
                [
                    bool(record.product_title),
                    record.rating_count is not None,
                    bool(record.date_first_available_iso),
                    record.embedded_review_count > 0,
                ]
            )
            score = new_required * 100 + category_balance * 10 + known_quality
            scored.append((score, rng.random(), record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        chosen = scored[0][2]
        selected.append(chosen)
        remaining.remove(chosen)
        category_counts[chosen.category] += 1
        covered.update(feature_tags(chosen))
    return selected


def find_previous_run_dir(output_root: Path, current_run_dir: Path) -> Path | None:
    run_dirs = [
        path
        for path in output_root.iterdir()
        if path.is_dir()
        and path != current_run_dir
        and (path / "selected_reviews.jsonl").exists()
    ]
    if not run_dirs:
        return None
    return sorted(run_dirs, key=lambda path: path.name, reverse=True)[0]


def compare_with_previous(
    current_reviews: list[dict[str, Any]],
    previous_reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_by_asin: dict[str, set[str]] = defaultdict(set)
    previous_by_asin: dict[str, set[str]] = defaultdict(set)
    for review in current_reviews:
        asin = str(review.get("asin") or "")
        key = str(review.get("review_id") or review.get("dedup_key") or "")
        if asin and key:
            current_by_asin[asin].add(key)
    for review in previous_reviews:
        asin = str(review.get("asin") or "")
        key = str(review.get("review_id") or review.get("dedup_key") or "")
        if asin and key:
            previous_by_asin[asin].add(key)
    comparison: list[dict[str, Any]] = []
    for asin in sorted(set(current_by_asin) | set(previous_by_asin)):
        current = current_by_asin[asin]
        previous = previous_by_asin[asin]
        overlap = current & previous
        new = current - previous
        removed = previous - current
        denominator = len(current | previous)
        comparison.append(
            {
                "asin": asin,
                "current_review_count": len(current),
                "previous_review_count": len(previous),
                "overlap_count": len(overlap),
                "new_review_count": len(new),
                "removed_review_count": len(removed),
                "jaccard_overlap": round(len(overlap) / denominator, 4) if denominator else 0.0,
            }
        )
    return comparison


def summarize_coverage(selected: list[ProductRecord]) -> dict[str, Any]:
    category_counts = Counter(record.category for record in selected)
    volume_counts = Counter(record.review_volume_bucket for record in selected)
    variant_counts = Counter(
        "with_variants" if record.has_variants else "without_variants"
        for record in selected
    )
    age_counts = Counter(record.age_bucket for record in selected)
    required = {
        "categories_covered": len(category_counts),
        "has_low_volume": volume_counts["low"] > 0,
        "has_medium_volume": volume_counts["medium"] > 0,
        "has_high_volume": volume_counts["high"] > 0,
        "has_variant_product": variant_counts["with_variants"] > 0,
        "has_nonvariant_product": variant_counts["without_variants"] > 0,
        "has_new_product": age_counts["new"] > 0,
        "has_old_product": age_counts["old"] > 0,
    }
    return {
        "category_counts": dict(category_counts),
        "review_volume_counts": dict(volume_counts),
        "variant_counts": dict(variant_counts),
        "age_counts": dict(age_counts),
        "coverage_checks": required,
        "all_requested_dimensions_covered": all(required.values()),
    }


def summarize_run(
    *,
    run_id: str,
    candidates: list[dict[str, str]],
    tested_products: list[ProductRecord],
    selected_products: list[ProductRecord],
    selected_reviews: list[dict[str, Any]],
    access_records: list[AccessRecord],
    previous_run: str | None,
    update_comparison: list[dict[str, Any]],
) -> dict[str, Any]:
    access_counts = Counter(record.result for record in access_records)
    unique_review_keys = {review["dedup_key"] for review in selected_reviews}
    elapsed = [record.elapsed_ms for record in access_records if record.elapsed_ms is not None]
    return {
        "run_id": run_id,
        "generated_at_utc": utc_now(),
        "candidate_asin_count": len(candidates),
        "tested_product_count": len(tested_products),
        "selected_product_count": len(selected_products),
        "selected_raw_review_count": len(selected_reviews),
        "selected_unique_review_count": len(unique_review_keys),
        "selected_duplicate_review_count": len(selected_reviews) - len(unique_review_keys),
        "access_result_counts": dict(access_counts),
        "request_count": len(access_records),
        "average_elapsed_ms": round(sum(elapsed) / len(elapsed), 2) if elapsed else None,
        "coverage": summarize_coverage(selected_products),
        "selected_field_completeness": {
            "review_id": field_completeness(selected_reviews, "review_id"),
            "reviewer_name": field_completeness(selected_reviews, "reviewer_name"),
            "rating": field_completeness(selected_reviews, "rating"),
            "title": field_completeness(selected_reviews, "title"),
            "review_text": field_completeness(selected_reviews, "review_text"),
            "review_date_raw": field_completeness(selected_reviews, "review_date_raw"),
            "variant_raw": field_completeness(selected_reviews, "variant_raw"),
            "review_language": field_completeness(selected_reviews, "review_language"),
        },
        "previous_run_directory": previous_run,
        "update_comparison": update_comparison,
        "notes": [
            "Date First Available is treated as listing-age evidence, not a guaranteed original launch date.",
            "Rating count is used as the volume proxy because complete review counts may be unavailable.",
            "Embedded product-page reviews may be selected top reviews and may not represent the full population.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and test a balanced live sample of Amazon product pages."
    )
    parser.add_argument("--target", type=int, default=15)
    parser.add_argument("--candidates-per-category", type=int, default=4)
    parser.add_argument("--delay", type=float, default=15.0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--output-root", default="amazon_batch_feasibility_runs")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--low-review-max", type=int, default=500)
    parser.add_argument("--high-review-min", type=int, default=5000)
    parser.add_argument("--new-days", type=int, default=730)
    parser.add_argument("--old-days", type=int, default=1825)
    parser.add_argument("--test-review-endpoint", action="store_true")
    parser.add_argument("--save-html-on-failure", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 10 <= args.target <= 20:
        print("--target must be between 10 and 20.", file=sys.stderr)
        return 2
    if args.candidates_per_category < 2:
        print("--candidates-per-category must be at least 2.", file=sys.stderr)
        return 2
    if args.delay < 10:
        print("--delay must be at least 10 seconds.", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    run_id = make_run_id()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    debug_dir = run_dir / "debug_html"
    session = build_session()

    access_records: list[AccessRecord] = []
    product_records: list[ProductRecord] = []
    reviews_by_asin: dict[str, list[dict[str, Any]]] = {}

    print(f"Run ID: {run_id}")
    print(f"Output: {run_dir.resolve()}")
    print(f"Target: {args.target}")
    print(f"Delay: {args.delay:.1f} seconds\n")

    try:
        candidates = discover_candidates(
            session,
            run_id=run_id,
            categories=CATEGORY_SEARCH_TERMS,
            candidates_per_category=args.candidates_per_category,
            delay_seconds=args.delay,
            timeout_seconds=args.timeout,
            rng=rng,
            access_records=access_records,
        )
    except RuntimeError as error:
        save_csv(run_dir / "access_log.csv", [asdict(record) for record in access_records])
        save_json(run_dir / "aborted.json", {"run_id": run_id, "aborted_at_utc": utc_now(), "reason": str(error)})
        print(str(error), file=sys.stderr)
        return 1

    save_csv(run_dir / "discovered_candidates.csv", candidates)

    for index, candidate in enumerate(candidates, start=1):
        asin = candidate["asin"]
        category = candidate["category"]
        search_term = candidate["search_term"]
        product_url = f"https://www.amazon.com/dp/{asin}/"
        response, html, access = request_page(
            session,
            run_id=run_id,
            request_type="product_page",
            url=product_url,
            category=category,
            asin=asin,
            timeout_seconds=args.timeout,
        )
        access_records.append(access)
        print(f"[product {index:02d}/{len(candidates):02d}] {category:12s} | {asin} | {access.result}")

        if access.result in BLOCKING_RESULTS:
            if html and args.save_html_on_failure:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"{asin}_{access.result}.html").write_text(html, encoding="utf-8")
            save_csv(run_dir / "access_log.csv", [asdict(record) for record in access_records])
            save_json(
                run_dir / "aborted.json",
                {
                    "run_id": run_id,
                    "aborted_at_utc": utc_now(),
                    "asin": asin,
                    "reason": f"Stopped after restricted response: {access.result}",
                },
            )
            print("Stopping after access restriction.", file=sys.stderr)
            return 1

        if response is None or html is None or access.result != "reachable":
            polite_sleep(args.delay)
            continue

        try:
            product, reviews = parse_product_page(
                run_id=run_id,
                asin=asin,
                category=category,
                search_term=search_term,
                source_url=response.url,
                html=html,
                low_review_max=args.low_review_max,
                high_review_min=args.high_review_min,
                new_days=args.new_days,
                old_days=args.old_days,
            )
        except Exception as error:
            print(f"Parser error for {asin}: {error}", file=sys.stderr)
            if args.save_html_on_failure:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"{asin}_parser_error.html").write_text(html, encoding="utf-8")
            polite_sleep(args.delay)
            continue

        product_records.append(product)
        reviews_by_asin[asin] = reviews
        print(
            " " * 12
            + f"rating_count={str(product.rating_count):8s} "
            + f"volume={product.review_volume_bucket:7s} "
            + f"variants={str(product.has_variants):5s} "
            + f"age={product.age_bucket:7s} "
            + f"reviews={product.embedded_review_count}"
        )

        if args.save_html_on_failure and (
            product.product_title is None or product.review_text_completeness < 0.8
        ):
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{asin}_low_completeness.html").write_text(html, encoding="utf-8")

        polite_sleep(args.delay)

    if not product_records:
        save_csv(run_dir / "access_log.csv", [asdict(record) for record in access_records])
        print("No product pages were parsed.", file=sys.stderr)
        return 1

    selected_products = select_balanced_products(
        product_records,
        target=min(args.target, len(product_records)),
        rng=rng,
    )
    selected_asins = {product.asin for product in selected_products}
    selected_reviews = [
        review
        for asin in selected_asins
        for review in reviews_by_asin.get(asin, [])
    ]

    if args.test_review_endpoint:
        for index, product in enumerate(selected_products, start=1):
            url = (
                "https://www.amazon.com/product-reviews/"
                f"{product.asin}/?sortBy=recent&pageNumber=1"
            )
            _, html, access = request_page(
                session,
                run_id=run_id,
                request_type="dedicated_review_page",
                url=url,
                category=product.category,
                asin=product.asin,
                timeout_seconds=args.timeout,
            )
            access_records.append(access)
            print(
                f"[review endpoint {index:02d}/{len(selected_products):02d}] "
                f"{product.asin} | {access.result}"
            )
            if html and args.save_html_on_failure and access.result != "reachable":
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"{product.asin}_review_endpoint_{access.result}.html").write_text(
                    html,
                    encoding="utf-8",
                )
            if access.result in {
                "captcha_or_bot_block",
                "rate_limited",
                "access_denied",
                "server_or_block_error",
            }:
                print(
                    f"Stopping dedicated-review-endpoint tests after {access.result}.",
                    file=sys.stderr,
                )
                break
            polite_sleep(args.delay)

    previous_run_dir = find_previous_run_dir(output_root, run_dir)
    previous_reviews = (
        load_jsonl(previous_run_dir / "selected_reviews.jsonl")
        if previous_run_dir is not None
        else []
    )
    update_comparison = compare_with_previous(selected_reviews, previous_reviews)

    save_csv(run_dir / "access_log.csv", [asdict(record) for record in access_records])
    save_csv(run_dir / "tested_products.csv", [asdict(record) for record in product_records])
    save_csv(run_dir / "selected_products.csv", [asdict(record) for record in selected_products])
    save_jsonl(run_dir / "selected_reviews.jsonl", selected_reviews)
    save_csv(run_dir / "selected_reviews.csv", selected_reviews)
    save_csv(run_dir / "update_comparison.csv", update_comparison)

    summary = summarize_run(
        run_id=run_id,
        candidates=candidates,
        tested_products=product_records,
        selected_products=selected_products,
        selected_reviews=selected_reviews,
        access_records=access_records,
        previous_run=str(previous_run_dir) if previous_run_dir else None,
        update_comparison=update_comparison,
    )
    save_json(run_dir / "summary.json", summary)

    print("\nSelected sample")
    print("-" * 100)
    for product in selected_products:
        print(
            f"{product.asin} | {product.category:12s} | "
            f"volume={product.review_volume_bucket:7s} | "
            f"variants={str(product.has_variants):5s} | "
            f"age={product.age_bucket:7s} | "
            f"embedded_reviews={product.embedded_review_count:2d} | "
            f"{product.product_title or 'UNKNOWN TITLE'}"
        )

    print("\nCoverage")
    print("-" * 100)
    print(json.dumps(summary["coverage"], ensure_ascii=False, indent=2))

    print("\nGenerated files")
    print("-" * 100)
    for filename in (
        "discovered_candidates.csv",
        "access_log.csv",
        "tested_products.csv",
        "selected_products.csv",
        "selected_reviews.jsonl",
        "selected_reviews.csv",
        "update_comparison.csv",
        "summary.json",
    ):
        print(run_dir / filename)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
