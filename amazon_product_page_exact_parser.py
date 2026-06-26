#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag


ASIN_RE = re.compile(r"\b([A-Z0-9]{10})\b", re.IGNORECASE)
RATING_RE = re.compile(
    r"([0-5](?:\.\d+)?)\s+out\s+of\s+5",
    re.IGNORECASE,
)

BLOCK_MARKERS = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "robot check",
    "captcha",
    "automated access to amazon data",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None

    value = " ".join(value.split())
    return value or None


def element_text(element: Tag | None) -> str | None:
    if element is None:
        return None

    return clean_text(element.get_text(" ", strip=True))


def extract_asin(value: str) -> str:
    value = value.strip()

    if re.fullmatch(r"[A-Z0-9]{10}", value, re.IGNORECASE):
        return value.upper()

    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            "Input must be a 10-character ASIN or Amazon product URL."
        )

    match = re.search(
        r"/(?:dp|gp/product|product-reviews)/"
        r"([A-Z0-9]{10})(?:[/?]|$)",
        parsed.path,
        re.IGNORECASE,
    )

    if match:
        return match.group(1).upper()

    fallback = ASIN_RE.search(value)

    if fallback:
        return fallback.group(1).upper()

    raise ValueError("Could not extract ASIN from input.")


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


def classify_response(
    response: requests.Response,
    html: str,
) -> str:
    lower = html.lower()
    final_path = urlparse(response.url).path.lower()

    if response.status_code == 429:
        return "rate_limited"

    if response.status_code in {401, 403}:
        return "access_denied"

    if response.status_code >= 500:
        return "server_or_block_error"

    if "/ap/signin" in final_path:
        return "login_required"

    if any(marker in lower for marker in BLOCK_MARKERS):
        return "captcha_or_bot_block"

    if response.status_code != 200:
        return f"http_{response.status_code}"

    return "reachable"


def extract_full_review_text(
    block: Tag,
) -> tuple[str | None, str | None]:
    """
    商品页真实结构：

    div[data-hook="reviewRichContentContainer"]
        p
            span
    """

    rich_container = block.select_one(
        '[data-hook="reviewRichContentContainer"]'
    )

    if rich_container is None:
        return None, None

    paragraphs: list[str] = []

    for paragraph in rich_container.select("p"):
        text = element_text(paragraph)

        if text:
            paragraphs.append(text)

    if paragraphs:
        return " ".join(paragraphs), clean_text(
            rich_container.get("lang")
        )

    # 有些评论可能没有 p 标签
    return element_text(rich_container), clean_text(
        rich_container.get("lang")
    )


def extract_review(
    block: Tag,
    asin: str,
    source_url: str,
    index: int,
) -> dict:
    review_id = clean_text(block.get("id"))

    reviewer_name = element_text(
        block.select_one(".a-profile-name")
    )

    rating_raw = element_text(
        block.select_one('[data-hook="review-star-rating"]')
    )

    rating = parse_rating(rating_raw)

    # 正确 selector：reviewTitle
    title_element = block.select_one(
        '[data-hook="reviewTitle"]'
    )
    title = element_text(title_element)
    title_language = (
        clean_text(title_element.get("lang"))
        if title_element
        else None
    )

    # 正确正文 selector
    review_text, review_language = extract_full_review_text(
        block
    )

    review_date_raw = element_text(
        block.select_one('[data-hook="review-date"]')
    )

    variant_raw = element_text(
        block.select_one('[data-hook="format-strip"]')
    )

    verified_purchase = (
        block.select_one('[data-hook="avp-badge"]')
        is not None
    )

    helpful_votes_raw = element_text(
        block.select_one(
            '[data-hook="helpful-vote-statement"]'
        )
    )

    if review_id:
        dedup_source = f"amazon|{asin}|{review_id}"
    else:
        dedup_source = "|".join(
            [
                "amazon",
                asin,
                reviewer_name or "",
                review_date_raw or "",
                review_text or "",
            ]
        )

    dedup_key = hashlib.sha256(
        dedup_source.encode("utf-8")
    ).hexdigest()

    return {
        "source": "amazon",
        "asin": asin,
        "block_index": index,
        "review_id": review_id,
        "reviewer_name": reviewer_name,
        "rating": rating,
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
        "dedup_key": dedup_key,
    }


def extract_product_metadata(
    soup: BeautifulSoup,
    asin: str,
    source_url: str,
) -> dict:
    product_title = element_text(
        soup.select_one("#productTitle")
    )

    rating_element = (
        soup.select_one("#acrPopover")
        or soup.select_one(
            '[data-hook="rating-out-of-text"]'
        )
    )

    overall_rating_raw = None

    if rating_element:
        overall_rating_raw = (
            clean_text(rating_element.get("title"))
            or element_text(rating_element)
        )

    rating_count_raw = element_text(
        soup.select_one("#acrCustomerReviewText")
    )

    price_raw = element_text(
        soup.select_one(".a-price .a-offscreen")
    )

    brand_raw = element_text(
        soup.select_one("#bylineInfo")
    )

    return {
        "source": "amazon",
        "asin": asin,
        "source_url": source_url,
        "product_title": product_title,
        "overall_rating_raw": overall_rating_raw,
        "overall_rating": parse_rating(
            overall_rating_raw
        ),
        "rating_count_raw": rating_count_raw,
        "price_raw": price_raw,
        "brand_raw": brand_raw,
        "collected_at_utc": utc_now(),
    }


def completeness(
    reviews: list[dict],
    field: str,
) -> float:
    if not reviews:
        return 0.0

    count = sum(
        review.get(field) is not None
        and review.get(field) != ""
        for review in reviews
    )

    return round(count / len(reviews), 4)


def save_json(
    path: Path,
    value,
) -> None:
    with path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            value,
            file,
            ensure_ascii=False,
            indent=2,
        )


def save_jsonl(
    path: Path,
    reviews: list[dict],
) -> None:
    with path.open(
        "w",
        encoding="utf-8",
    ) as file:
        for review in reviews:
            file.write(
                json.dumps(
                    review,
                    ensure_ascii=False,
                )
                + "\n"
            )


def save_csv(
    path: Path,
    reviews: list[dict],
) -> None:
    if not reviews:
        return

    fields = [
        key
        for key in reviews[0].keys()
    ]

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields,
        )
        writer.writeheader()
        writer.writerows(reviews)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--product",
        required=True,
        help="Amazon product URL or ASIN.",
    )

    parser.add_argument(
        "--output-dir",
        default="amazon_exact_parser_output",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        asin = extract_asin(args.product)
    except ValueError as error:
        print(f"Input error: {error}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    url = f"https://www.amazon.com/dp/{asin}/"

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
        }
    )

    print(f"Requesting: {url}")

    start = time.perf_counter()

    try:
        response = session.get(
            url,
            timeout=(10, 30),
            allow_redirects=True,
        )
    except requests.RequestException as error:
        print(f"Request failed: {error}")
        return 1

    elapsed_ms = int(
        (time.perf_counter() - start) * 1000
    )

    html = response.text
    result = classify_response(
        response,
        html,
    )

    print(f"HTTP status: {response.status_code}")
    print(f"Result:      {result}")
    print(f"Elapsed:     {elapsed_ms} ms")
    print(f"Final URL:   {response.url}")

    (output_dir / "product_page.html").write_text(
        html,
        encoding="utf-8",
    )

    if result != "reachable":
        save_json(
            output_dir / "summary.json",
            {
                "result": result,
                "http_status": response.status_code,
                "final_url": response.url,
                "elapsed_ms": elapsed_ms,
                "parsed_review_count": 0,
            },
        )
        return 0

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    blocks = soup.select(
        'div[data-hook="review"]'
    )

    reviews = [
        extract_review(
            block=block,
            asin=asin,
            source_url=response.url,
            index=index,
        )
        for index, block in enumerate(
            blocks,
            start=1,
        )
    ]

    product_metadata = extract_product_metadata(
        soup=soup,
        asin=asin,
        source_url=response.url,
    )

    duplicate_count = (
        len(reviews)
        - len(
            {
                review["dedup_key"]
                for review in reviews
            }
        )
    )

    summary = {
        "generated_at_utc": utc_now(),
        "result": result,
        "http_status": response.status_code,
        "final_url": response.url,
        "elapsed_ms": elapsed_ms,
        "response_bytes": len(
            response.content
        ),
        "review_block_count": len(blocks),
        "parsed_review_count": len(reviews),
        "duplicate_review_count": duplicate_count,
        "field_completeness": {
            "review_id": completeness(
                reviews,
                "review_id",
            ),
            "reviewer_name": completeness(
                reviews,
                "reviewer_name",
            ),
            "rating": completeness(
                reviews,
                "rating",
            ),
            "title": completeness(
                reviews,
                "title",
            ),
            "review_text": completeness(
                reviews,
                "review_text",
            ),
            "review_date_raw": completeness(
                reviews,
                "review_date_raw",
            ),
            "variant_raw": completeness(
                reviews,
                "variant_raw",
            ),
            "review_language": completeness(
                reviews,
                "review_language",
            ),
        },
        "language_counts": {},
    }

    for review in reviews:
        language = (
            review["review_language"]
            or "unknown"
        )

        summary["language_counts"][language] = (
            summary["language_counts"].get(
                language,
                0,
            )
            + 1
        )

    save_json(
        output_dir / "product_metadata.json",
        product_metadata,
    )

    save_jsonl(
        output_dir / "reviews_exact.jsonl",
        reviews,
    )

    save_csv(
        output_dir / "reviews_exact.csv",
        reviews,
    )

    save_json(
        output_dir / "summary.json",
        summary,
    )

    print("\nSummary")
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        )
    )

    if reviews:
        print("\nFirst review")
        print(
            json.dumps(
                reviews[0],
                ensure_ascii=False,
                indent=2,
            )
        )

    print(
        f"\nFiles saved to: "
        f"{output_dir.resolve()}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())