#!/usr/bin/env python3


from __future__ import annotations

import argparse
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



ASIN_PATTERN = re.compile(r"\b([A-Z0-9]{10})\b", re.IGNORECASE)

RATING_PATTERN = re.compile(
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

REVIEW_BLOCK_SELECTORS = [
    'div[data-hook="review"]',
    'div[id^="customer_review-"]',
    'li[data-hook="review"]',
]

TITLE_SELECTORS = [
    '[data-hook="review-title"] span:not(.a-icon-alt)',
    '[data-hook="review-title"] span',
    '[data-hook="review-title"]',
    ".review-title-content span",
    ".review-title-content",
    "a.review-title span",
    "a.review-title",
    ".review-title",
]

BODY_SELECTORS = [
    '[data-hook="review-body"] span',
    '[data-hook="review-body"]',
    ".review-text-content span",
    ".review-text-content",
    ".review-text span",
    ".review-text",
]

REVIEWER_SELECTORS = [
    ".a-profile-name",
    '[data-hook="genome-widget"] .a-profile-name',
]

RATING_SELECTORS = [
    '[data-hook="review-star-rating"]',
    '[data-hook="cmps-review-star-rating"]',
    ".review-rating",
]

DATE_SELECTORS = [
    '[data-hook="review-date"]',
    ".review-date",
]

HELPFUL_SELECTORS = [
    '[data-hook="helpful-vote-statement"]',
    ".cr-vote-text",
]

VARIANT_SELECTORS = [
    '[data-hook="format-strip"]',
    ".review-format-strip",
]

VERIFIED_SELECTORS = [
    '[data-hook="avp-badge"]',
    ".avp-badge",
]

IGNORED_TEXT_PATTERNS = (
    "verified purchase",
    "已验证购买",
    "helpful",
    "有帮助",
    "report",
    "报告",
    "people found this helpful",
    "person found this helpful",
)


# ============================================================
# 基础函数
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None

    value = " ".join(value.split())
    return value if value else None


def extract_asin(value: str) -> str:
    """
    支持：
    - B008JGIZGS
    - https://www.amazon.com/.../dp/B008JGIZGS/
    """
    value = value.strip()

    if re.fullmatch(r"[A-Z0-9]{10}", value, re.IGNORECASE):
        return value.upper()

    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            "请输入 10 位 ASIN，或者完整 Amazon 商品 URL。"
        )

    path_match = re.search(
        r"/(?:dp|gp/product|product-reviews)/"
        r"([A-Z0-9]{10})(?:[/?]|$)",
        parsed.path,
        re.IGNORECASE,
    )

    if path_match:
        return path_match.group(1).upper()

    fallback = ASIN_PATTERN.search(value)

    if fallback:
        return fallback.group(1).upper()

    raise ValueError("无法从输入中找到有效的 10 位 ASIN。")


def element_text(element: Tag | None) -> str | None:
    if element is None:
        return None

    return clean_text(element.get_text(" ", strip=True))


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

    if 0 <= value <= 5:
        return value

    return None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()



def classify_response(
    response: requests.Response,
    html: str,
) -> str:
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


# ============================================================
# Selector 诊断
# ============================================================

def count_selectors(
    blocks: list[Tag],
    selectors: list[str],
) -> dict[str, int]:
    """
    统计每个 selector 在多少个 review block 中命中。
    """
    result = {}

    for selector in selectors:
        count = 0

        for block in blocks:
            if block.select_one(selector) is not None:
                count += 1

        result[selector] = count

    return result


def first_text(
    root: Tag,
    selectors: list[str],
) -> tuple[str | None, str | None]:
    """
    返回：
    (提取到的文本, 实际命中的 selector)
    """
    for selector in selectors:
        element = root.select_one(selector)
        text = element_text(element)

        if text:
            return text, selector

    return None, None


# ============================================================
# 标题提取
# ============================================================

def is_rating_text(text: str) -> bool:
    return RATING_PATTERN.search(text) is not None


def extract_title(
    block: Tag,
) -> tuple[str | None, str | None]:
    """
    标题节点有时同时包含星级文字，因此需要过滤。
    """

    for selector in TITLE_SELECTORS:
        elements = block.select(selector)

        for element in elements:
            text = element_text(element)

            if not text:
                continue

            if is_rating_text(text):
                # 如果文本同时包含评分和标题，尝试去掉评分部分
                cleaned = RATING_PATTERN.sub("", text)
                cleaned = clean_text(cleaned)

                if cleaned:
                    return cleaned, selector

                continue

            if 2 <= len(text) <= 300:
                return text, selector

    return None, None


# ============================================================
# 正文提取
# ============================================================

def extract_body(
    block: Tag,
) -> tuple[str | None, str | None]:
    """
    正文通常是较长文本。
    在同一 selector 命中多个 span 时，取最长的合理文本。
    """

    for selector in BODY_SELECTORS:
        elements = block.select(selector)

        candidates = []

        for element in elements:
            text = element_text(element)

            if not text:
                continue

            if len(text) >= 15:
                candidates.append(text)

        if candidates:
            return max(candidates, key=len), selector

    return None, None


# ============================================================
# 保守的 fallback
# ============================================================

def should_ignore_string(
    text: str,
    known_values: set[str],
) -> bool:
    lowered = text.lower()

    if text in known_values:
        return True

    if is_rating_text(text):
        return True

    if any(pattern in lowered for pattern in IGNORED_TEXT_PATTERNS):
        return True

    if len(text) <= 2:
        return True

    return False


def heuristic_title_and_body(
    block: Tag,
    known_values: list[str | None],
    existing_title: str | None,
    existing_body: str | None,
) -> tuple[str | None, str | None]:
    """
    如果标准 selector 全部失败，则检查 review block 内的可见文本。

    这是诊断性 fallback：
    - 最长的较长文本作为正文候选
    - 较短且非日期/评分的文本作为标题候选
    """

    known = {
        value
        for value in known_values
        if value is not None
    }

    strings = []

    for raw in block.stripped_strings:
        text = clean_text(raw)

        if not text:
            continue

        if should_ignore_string(text, known):
            continue

        if text not in strings:
            strings.append(text)

    title = existing_title
    body = existing_body

    if body is None:
        body_candidates = [
            text
            for text in strings
            if len(text) >= 40
        ]

        if body_candidates:
            body = max(body_candidates, key=len)

    if title is None:
        title_candidates = [
            text
            for text in strings
            if 4 <= len(text) <= 180
            and text != body
            and not re.search(
                r"\b(?:19|20)\d{2}\b",
                text,
            )
        ]

        if title_candidates:
            title = title_candidates[0]

    return title, body


# ============================================================
# 商品信息
# ============================================================

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
        or soup.select_one('[data-hook="rating-out-of-text"]')
    )

    overall_rating_raw = None

    if rating_element is not None:
        overall_rating_raw = (
            clean_text(rating_element.get("title"))
            or element_text(rating_element)
        )

    rating_count = element_text(
        soup.select_one("#acrCustomerReviewText")
    )

    price = element_text(
        soup.select_one(".a-price .a-offscreen")
    )

    brand = element_text(
        soup.select_one("#bylineInfo")
    )

    return {
        "source": "amazon",
        "asin": asin,
        "source_url": source_url,
        "product_title": product_title,
        "overall_rating_raw": overall_rating_raw,
        "overall_rating": parse_rating(overall_rating_raw),
        "rating_count_raw": rating_count,
        "price_raw": price,
        "brand_raw": brand,
        "collected_at_utc": utc_now(),
    }


# ============================================================
# 评论解析
# ============================================================

def find_review_blocks(
    soup: BeautifulSoup,
) -> tuple[list[Tag], str | None]:
    """
    使用第一套能找到内容的 selector，防止重复匹配。
    """

    for selector in REVIEW_BLOCK_SELECTORS:
        blocks = soup.select(selector)

        if blocks:
            return blocks, selector

    return [], None


def parse_review_block(
    block: Tag,
    asin: str,
    source_url: str,
    index: int,
) -> dict:
    review_id = clean_text(block.get("id"))

    reviewer_name, reviewer_selector = first_text(
        block,
        REVIEWER_SELECTORS,
    )

    rating_raw, rating_selector = first_text(
        block,
        RATING_SELECTORS,
    )

    rating = parse_rating(rating_raw)

    review_date_raw, date_selector = first_text(
        block,
        DATE_SELECTORS,
    )

    helpful_votes_raw, helpful_selector = first_text(
        block,
        HELPFUL_SELECTORS,
    )

    variant_raw, variant_selector = first_text(
        block,
        VARIANT_SELECTORS,
    )

    verified_purchase = any(
        block.select_one(selector) is not None
        for selector in VERIFIED_SELECTORS
    )

    title, title_selector = extract_title(block)
    review_text, body_selector = extract_body(block)

    fallback_used = False

    if title is None or review_text is None:
        fallback_title, fallback_body = heuristic_title_and_body(
            block=block,
            known_values=[
                reviewer_name,
                rating_raw,
                review_date_raw,
                helpful_votes_raw,
                variant_raw,
            ],
            existing_title=title,
            existing_body=review_text,
        )

        if title is None and fallback_title is not None:
            title = fallback_title
            title_selector = "heuristic_visible_text"
            fallback_used = True

        if review_text is None and fallback_body is not None:
            review_text = fallback_body
            body_selector = "heuristic_longest_text"
            fallback_used = True

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

    return {
        "source": "amazon",
        "asin": asin,
        "block_index": index,
        "review_id": review_id,
        "reviewer_name": reviewer_name,
        "rating": rating,
        "rating_raw": rating_raw,
        "title": title,
        "review_text": review_text,
        "review_date_raw": review_date_raw,
        "verified_purchase": verified_purchase,
        "helpful_votes_raw": helpful_votes_raw,
        "variant_raw": variant_raw,
        "source_url": source_url,
        "collected_at_utc": utc_now(),
        "dedup_key": sha256_text(dedup_source),
        "fallback_used": fallback_used,
        "selector_used": {
            "reviewer": reviewer_selector,
            "rating": rating_selector,
            "title": title_selector,
            "body": body_selector,
            "date": date_selector,
            "helpful": helpful_selector,
            "variant": variant_selector,
        },
    }


# ============================================================
# 汇总
# ============================================================

def field_completeness(
    reviews: list[dict],
    field: str,
) -> float:
    if not reviews:
        return 0.0

    present = sum(
        review.get(field) is not None
        and review.get(field) != ""
        for review in reviews
    )

    return round(present / len(reviews), 4)


def build_summary(
    response: requests.Response,
    result: str,
    blocks: list[Tag],
    reviews: list[dict],
    review_block_selector: str | None,
    selector_diagnostics: dict,
) -> dict:
    dedup_keys = [
        review["dedup_key"]
        for review in reviews
    ]

    unique_count = len(set(dedup_keys))
    duplicate_count = len(reviews) - unique_count

    return {
        "generated_at_utc": utc_now(),
        "http_status": response.status_code,
        "final_url": response.url,
        "result": result,
        "response_bytes": len(response.content),
        "review_block_selector": review_block_selector,
        "review_block_count": len(blocks),
        "parsed_review_count": len(reviews),
        "unique_review_count": unique_count,
        "duplicate_review_count": duplicate_count,
        "field_completeness": {
            "review_id": field_completeness(
                reviews,
                "review_id",
            ),
            "reviewer_name": field_completeness(
                reviews,
                "reviewer_name",
            ),
            "rating": field_completeness(
                reviews,
                "rating",
            ),
            "title": field_completeness(
                reviews,
                "title",
            ),
            "review_text": field_completeness(
                reviews,
                "review_text",
            ),
            "review_date_raw": field_completeness(
                reviews,
                "review_date_raw",
            ),
            "helpful_votes_raw": field_completeness(
                reviews,
                "helpful_votes_raw",
            ),
            "variant_raw": field_completeness(
                reviews,
                "variant_raw",
            ),
        },
        "fallback_review_count": sum(
            review["fallback_used"]
            for review in reviews
        ),
        "selector_diagnostics": selector_diagnostics,
    }


# ============================================================
# 参数
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose Amazon product-page review markup "
            "and extract embedded reviews."
        )
    )

    parser.add_argument(
        "--product",
        required=True,
        help="Amazon product URL or 10-character ASIN.",
    )

    parser.add_argument(
        "--output-dir",
        default="amazon_product_diagnostic",
        help="Output directory.",
    )

    parser.add_argument(
        "--save-blocks",
        type=int,
        default=3,
        help="Number of review blocks to save as HTML.",
    )

    return parser.parse_args()


# ============================================================
# 主程序
# ============================================================

def main() -> int:
    args = parse_args()

    try:
        asin = extract_asin(args.product)
    except ValueError as error:
        print(f"Input error: {error}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    product_url = f"https://www.amazon.com/dp/{asin}/"

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
                "application/xml;q=0.9,image/avif,"
                "image/webp,*/*;q=0.8"
            ),
            "Connection": "keep-alive",
        }
    )

    print(f"ASIN:       {asin}")
    print(f"Product URL: {product_url}")
    print(f"Output:     {output_dir.resolve()}")

    start = time.perf_counter()

    try:
        response = session.get(
            product_url,
            timeout=(10, 30),
            allow_redirects=True,
        )
    except requests.RequestException as error:
        print(f"Request failed: {error}", file=sys.stderr)
        return 1

    elapsed_ms = int(
        (time.perf_counter() - start) * 1000
    )

    html = response.text
    result = classify_response(response, html)

    print("\nRequest result")
    print("-" * 60)
    print(f"HTTP status:   {response.status_code}")
    print(f"Final URL:     {response.url}")
    print(f"Elapsed:       {elapsed_ms} ms")
    print(f"Response size: {len(response.content)} bytes")
    print(f"Classification:{result}")

    # 无论成功与否，都保存完整页面
    page_path = output_dir / "product_page.html"
    page_path.write_text(
        html,
        encoding="utf-8",
    )

    print(f"Full HTML saved: {page_path.resolve()}")

    if result != "reachable":
        summary = {
            "generated_at_utc": utc_now(),
            "http_status": response.status_code,
            "final_url": response.url,
            "result": result,
            "elapsed_ms": elapsed_ms,
            "response_bytes": len(response.content),
            "parsed_review_count": 0,
        }

        with (
            output_dir / "summary.json"
        ).open("w", encoding="utf-8") as file:
            json.dump(
                summary,
                file,
                ensure_ascii=False,
                indent=2,
            )

        print("Page was restricted. Stopping.")
        return 0

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    product_metadata = extract_product_metadata(
        soup=soup,
        asin=asin,
        source_url=response.url,
    )

    blocks, review_block_selector = find_review_blocks(
        soup
    )

    print("\nHTML structure")
    print("-" * 60)
    print(
        f"Review block selector: {review_block_selector}"
    )
    print(f"Review blocks found:   {len(blocks)}")

    # 保存前几个完整 review block
    for index, block in enumerate(
        blocks[: args.save_blocks],
        start=1,
    ):
        block_path = (
            output_dir
            / f"review_block_{index}.html"
        )

        block_path.write_text(
            block.prettify(),
            encoding="utf-8",
        )

        print(
            f"Review block {index} saved: "
            f"{block_path.resolve()}"
        )

    selector_diagnostics = {
        "review_blocks": {
            selector: len(soup.select(selector))
            for selector in REVIEW_BLOCK_SELECTORS
        },
        "title_selectors": count_selectors(
            blocks,
            TITLE_SELECTORS,
        ),
        "body_selectors": count_selectors(
            blocks,
            BODY_SELECTORS,
        ),
        "reviewer_selectors": count_selectors(
            blocks,
            REVIEWER_SELECTORS,
        ),
        "rating_selectors": count_selectors(
            blocks,
            RATING_SELECTORS,
        ),
        "date_selectors": count_selectors(
            blocks,
            DATE_SELECTORS,
        ),
        "variant_selectors": count_selectors(
            blocks,
            VARIANT_SELECTORS,
        ),
    }

    reviews = []

    for index, block in enumerate(
        blocks,
        start=1,
    ):
        review = parse_review_block(
            block=block,
            asin=asin,
            source_url=response.url,
            index=index,
        )

        reviews.append(review)

    # 保存商品信息
    with (
        output_dir / "product_metadata.json"
    ).open("w", encoding="utf-8") as file:
        json.dump(
            product_metadata,
            file,
            ensure_ascii=False,
            indent=2,
        )

    # 保存评论
    with (
        output_dir / "reviews_diagnostic.jsonl"
    ).open("w", encoding="utf-8") as file:
        for review in reviews:
            file.write(
                json.dumps(
                    review,
                    ensure_ascii=False,
                )
                + "\n"
            )

    # 保存 selector 诊断
    with (
        output_dir / "selector_diagnostics.json"
    ).open("w", encoding="utf-8") as file:
        json.dump(
            selector_diagnostics,
            file,
            ensure_ascii=False,
            indent=2,
        )

    summary = build_summary(
        response=response,
        result=result,
        blocks=blocks,
        reviews=reviews,
        review_block_selector=review_block_selector,
        selector_diagnostics=selector_diagnostics,
    )

    summary["elapsed_ms"] = elapsed_ms

    with (
        output_dir / "summary.json"
    ).open("w", encoding="utf-8") as file:
        json.dump(
            summary,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("\nSummary")
    print("-" * 60)
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        )
    )

    if reviews:
        print("\nFirst review")
        print("-" * 60)
        print(
            json.dumps(
                reviews[0],
                ensure_ascii=False,
                indent=2,
            )
        )

    print("\nGenerated files")
    print("-" * 60)
    print("product_page.html")
    print("product_metadata.json")
    print("reviews_diagnostic.jsonl")
    print("selector_diagnostics.json")
    print("summary.json")

    if blocks:
        print(
            f"review_block_1.html through "
            f"review_block_{min(len(blocks), args.save_blocks)}.html"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())