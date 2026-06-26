#!/usr/bin/env python3
"""
Amazon live review feasibility probe.

This script:
- Accepts either an Amazon product URL or an ASIN.
- Converts the product URL to a review-page URL.
- Uses requests + BeautifulSoup only.
- Stops when CAPTCHA, login, 403, 429, or 5xx restrictions are detected.
- Extracts a small sample of review fields.
- Saves access_log.csv, reviews_sample.jsonl, summary.json, and debug HTML when needed.

It does not use proxies, CAPTCHA bypass, stealth browsers, or account automation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


ASIN_PATTERN = re.compile(r"\b([A-Z0-9]{10})\b", re.IGNORECASE)
RATING_PATTERN = re.compile(r"([0-5](?:\.\d+)?)\s+out\s+of\s+5", re.IGNORECASE)

BLOCK_MARKERS = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "robot check",
    "captcha",
    "automated access to amazon data",
    "to discuss automated access to amazon data",
)


@dataclass
class AccessRecord:
    requested_at_utc: str
    asin: str
    page_number: int
    requested_url: str
    final_url: str
    http_status: int | None
    elapsed_ms: int | None
    response_bytes: int
    redirected: bool
    result: str
    parsed_review_count: int
    html_sha256: str | None
    error: str | None


@dataclass
class ReviewRecord:
    source: str
    asin: str
    page_number: int
    review_id: str | None
    reviewer_name: str | None
    rating: float | None
    rating_raw: str | None
    title: str | None
    review_text: str | None
    review_date_raw: str | None
    verified_purchase: bool
    helpful_votes_raw: str | None
    source_url: str
    collected_at_utc: str
    dedup_key: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def element_text(element) -> str | None:
    if element is None:
        return None
    return clean_text(element.get_text(" ", strip=True))


def extract_asin(value: str) -> str:
    value = value.strip()

    if re.fullmatch(r"[A-Z0-9]{10}", value, re.IGNORECASE):
        return value.upper()

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Input must be a 10-character ASIN or an Amazon product URL.")

    path_match = re.search(
        r"/(?:dp|gp/product|product-reviews)/([A-Z0-9]{10})(?:[/?]|$)",
        parsed.path,
        re.IGNORECASE,
    )
    if path_match:
        return path_match.group(1).upper()

    fallback = ASIN_PATTERN.search(value)
    if fallback:
        return fallback.group(1).upper()

    raise ValueError("Could not find a 10-character ASIN in the supplied value.")


def parse_rating(raw: str | None) -> float | None:
    if not raw:
        return None

    match = RATING_PATTERN.search(raw)
    if not match:
        return None

    try:
        value = float(match.group(1))
    except ValueError:
        return None

    return value if 0 <= value <= 5 else None


def make_dedup_key(
    asin: str,
    review_id: str | None,
    reviewer_name: str | None,
    review_date_raw: str | None,
    review_text: str | None,
) -> str:
    if review_id:
        raw = f"amazon|{asin}|{review_id}"
    else:
        raw = "|".join(
            [
                "amazon",
                asin,
                reviewer_name or "",
                review_date_raw or "",
                review_text or "",
            ]
        )

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def parse_reviews(
    html: str,
    asin: str,
    page_number: int,
    source_url: str,
) -> list[ReviewRecord]:
    soup = BeautifulSoup(html, "html.parser")
    review_blocks = soup.select('div[data-hook="review"]')
    collected_at = utc_now()
    reviews: list[ReviewRecord] = []

    for block in review_blocks:
        review_id = clean_text(block.get("id"))
        reviewer_name = element_text(block.select_one(".a-profile-name"))

        rating_element = (
            block.select_one('[data-hook="review-star-rating"]')
            or block.select_one('[data-hook="cmps-review-star-rating"]')
        )
        rating_raw = element_text(rating_element)
        rating = parse_rating(rating_raw)

        title = element_text(block.select_one('[data-hook="review-title"]'))
        review_text = element_text(block.select_one('[data-hook="review-body"]'))
        review_date_raw = element_text(block.select_one('[data-hook="review-date"]'))

        verified_purchase = block.select_one('[data-hook="avp-badge"]') is not None
        helpful_votes_raw = element_text(
            block.select_one('[data-hook="helpful-vote-statement"]')
        )

        dedup_key = make_dedup_key(
            asin=asin,
            review_id=review_id,
            reviewer_name=reviewer_name,
            review_date_raw=review_date_raw,
            review_text=review_text,
        )

        reviews.append(
            ReviewRecord(
                source="amazon",
                asin=asin,
                page_number=page_number,
                review_id=review_id,
                reviewer_name=reviewer_name,
                rating=rating,
                rating_raw=rating_raw,
                title=title,
                review_text=review_text,
                review_date_raw=review_date_raw,
                verified_purchase=verified_purchase,
                helpful_votes_raw=helpful_votes_raw,
                source_url=source_url,
                collected_at_utc=collected_at,
                dedup_key=dedup_key,
            )
        )

    return reviews


def remove_duplicates(reviews: Iterable[ReviewRecord]) -> list[ReviewRecord]:
    unique: list[ReviewRecord] = []
    seen: set[str] = set()

    for review in reviews:
        if review.dedup_key in seen:
            continue
        seen.add(review.dedup_key)
        unique.append(review)

    return unique


def completeness(reviews: list[ReviewRecord], field: str) -> float:
    if not reviews:
        return 0.0

    present = 0
    for review in reviews:
        value = getattr(review, field)
        if value is not None and value != "":
            present += 1

    return round(present / len(reviews), 4)


def write_access_csv(path: Path, rows: list[AccessRecord]) -> None:
    if not rows:
        return

    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def write_reviews_jsonl(path: Path, rows: list[ReviewRecord]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def build_summary(
    access_records: list[AccessRecord],
    raw_reviews: list[ReviewRecord],
    unique_reviews: list[ReviewRecord],
) -> dict:
    blocked_results = {
        "captcha_or_bot_block",
        "rate_limited",
        "access_denied",
        "login_required",
        "server_or_block_error",
    }

    total_requests = len(access_records)
    reachable_requests = sum(
        record.result in {"reachable", "reachable_but_no_reviews_parsed"}
        for record in access_records
    )
    blocked_requests = sum(
        record.result in blocked_results for record in access_records
    )

    duplicate_count = len(raw_reviews) - len(unique_reviews)

    return {
        "generated_at_utc": utc_now(),
        "total_requests": total_requests,
        "reachable_requests": reachable_requests,
        "blocked_or_restricted_requests": blocked_requests,
        "request_success_rate": (
            round(reachable_requests / total_requests, 4)
            if total_requests
            else 0.0
        ),
        "raw_review_count": len(raw_reviews),
        "unique_review_count": len(unique_reviews),
        "duplicate_review_count": duplicate_count,
        "duplicate_rate": (
            round(duplicate_count / len(raw_reviews), 4)
            if raw_reviews
            else 0.0
        ),
        "field_completeness": {
            "review_id": completeness(unique_reviews, "review_id"),
            "reviewer_name": completeness(unique_reviews, "reviewer_name"),
            "rating": completeness(unique_reviews, "rating"),
            "title": completeness(unique_reviews, "title"),
            "review_text": completeness(unique_reviews, "review_text"),
            "review_date_raw": completeness(unique_reviews, "review_date_raw"),
            "helpful_votes_raw": completeness(unique_reviews, "helpful_votes_raw"),
        },
        "result_counts": {
            result: sum(record.result == result for record in access_records)
            for result in sorted({record.result for record in access_records})
        },
        "note": (
            "This report measures technical accessibility under the tested conditions. "
            "A successful response does not by itself establish production suitability."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Small-scale Amazon live review accessibility probe."
    )
    parser.add_argument(
        "--product",
        required=True,
        help="Amazon product URL or 10-character ASIN.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        choices=[1, 2],
        help="Number of review pages to test. Allowed values: 1 or 2.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=20.0,
        help="Seconds between page requests. Minimum: 15 seconds.",
    )
    parser.add_argument(
        "--output-dir",
        default="amazon_probe_output",
        help="Output directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.delay < 15:
        print("--delay must be at least 15 seconds.", file=sys.stderr)
        return 2

    try:
        asin = extract_asin(args.product)
    except ValueError as error:
        print(f"Input error: {error}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Connection": "keep-alive",
        }
    )

    access_records: list[AccessRecord] = []
    raw_reviews: list[ReviewRecord] = []

    print(f"ASIN: {asin}")
    print(f"Pages to test: {args.pages}")
    print(f"Output directory: {output_dir.resolve()}")

    for page_number in range(1, args.pages + 1):
        review_url = (
            f"https://www.amazon.com/dp/{asin}/"
            f"?sortBy=recent&pageNumber={page_number}"
        )

        print("\n" + "=" * 70)
        print(f"Requesting page {page_number}")
        print(review_url)

        requested_at = utc_now()
        start_time = time.perf_counter()

        try:
            response = session.get(
                review_url,
                timeout=(10, 30),
                allow_redirects=True,
            )

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            html = response.text
            result = classify_response(response, html)

            parsed_reviews: list[ReviewRecord] = []

            if result == "reachable":
                parsed_reviews = parse_reviews(
                    html=html,
                    asin=asin,
                    page_number=page_number,
                    source_url=response.url,
                )

                if not parsed_reviews:
                    result = "reachable_but_no_reviews_parsed"

                    debug_path = output_dir / f"debug_page_{page_number}.html"
                    debug_path.write_text(html, encoding="utf-8")
                    print(f"Debug HTML saved to: {debug_path.resolve()}")

            raw_reviews.extend(parsed_reviews)

            access_record = AccessRecord(
                requested_at_utc=requested_at,
                asin=asin,
                page_number=page_number,
                requested_url=review_url,
                final_url=response.url,
                http_status=response.status_code,
                elapsed_ms=elapsed_ms,
                response_bytes=len(response.content),
                redirected=response.url != review_url,
                result=result,
                parsed_review_count=len(parsed_reviews),
                html_sha256=hashlib.sha256(response.content).hexdigest(),
                error=None,
            )
            access_records.append(access_record)

            print(f"HTTP status:   {response.status_code}")
            print(f"Final URL:     {response.url}")
            print(f"Elapsed:       {elapsed_ms} ms")
            print(f"Response size: {len(response.content)} bytes")
            print(f"Result:        {result}")
            print(f"Reviews:       {len(parsed_reviews)}")

            if result in {
                "captcha_or_bot_block",
                "rate_limited",
                "access_denied",
                "login_required",
                "server_or_block_error",
            }:
                print("Access restriction detected. The probe will stop.")
                break

        except requests.RequestException as error:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)

            access_records.append(
                AccessRecord(
                    requested_at_utc=requested_at,
                    asin=asin,
                    page_number=page_number,
                    requested_url=review_url,
                    final_url=review_url,
                    http_status=None,
                    elapsed_ms=elapsed_ms,
                    response_bytes=0,
                    redirected=False,
                    result="network_error",
                    parsed_review_count=0,
                    html_sha256=None,
                    error=f"{type(error).__name__}: {error}",
                )
            )

            print(f"Request error: {error}")
            break

        if page_number < args.pages:
            print(f"Waiting {args.delay:.0f} seconds...")
            time.sleep(args.delay)

    unique_reviews = remove_duplicates(raw_reviews)

    write_access_csv(output_dir / "access_log.csv", access_records)
    write_reviews_jsonl(output_dir / "reviews_sample.jsonl", unique_reviews)

    summary = build_summary(
        access_records=access_records,
        raw_reviews=raw_reviews,
        unique_reviews=unique_reviews,
    )

    with (output_dir / "summary.json").open("w", encoding="utf-8") as file:
        json.dump(summary, file, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nFiles saved in: {output_dir.resolve()}")

    if unique_reviews:
        print("\nFirst parsed review:")
        print(json.dumps(asdict(unique_reviews[0]), ensure_ascii=False, indent=2))
        
        
    

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
