from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import Browser, Page, async_playwright


BASE_LIST_URL = "https://www.g2g.com/categories/pokemon-tcg-pocket-accounts?sort=lowest_price"
OUTPUT_EXCEL = "g2g_pokemon_accounts.xlsx"
PRICE_PREFIX_PATTERN = re.compile(r"(?i)(US\$|USD|\$)\s*([0-9]+(?:\.[0-9]{1,4})?)")
PRICE_SUFFIX_PATTERN = re.compile(r"(?i)([0-9]+(?:\.[0-9]{1,4})?)\s*(USD|US\$)")


@dataclass
class ScraperConfig:
    max_pages: int | None = None
    max_items: int | None = None
    headless: bool = True
    timeout_ms: int = 30_000
    retries: int = 3
    min_delay_sec: float = 0.3
    max_delay_sec: float = 0.9
    expand_view_more_rounds: int = 5
    expand_wait_ms: int = 700
    concurrency: int = 5


@dataclass
class ScoreConfig:
    price_weight: float = 0.4
    content_weight: float = 0.6
    score_ranges: List[Tuple[str, int, int]] = field(
        default_factory=lambda: [
            ("初始號", 0, 39),
            ("中階帳號", 40, 69),
            ("高階帳號", 70, 100),
        ]
    )
    keyword_weights: Dict[str, int] = field(
        default_factory=lambda: {
            "god pack": 30,
            "godpack": 30,
            "crown": 20,
            "gold": 14,
            "immersive": 14,
            "rainbow": 10,
            "full art": 8,
            "alt art": 8,
            "charizard": 8,
            "mewtwo": 6,
            "pikachu": 5,
            "ex": 4,
            "ur": 8,
            "sr": 5,
            "限定": 12,
            "稀有": 8,
            "高稀有": 14,
            "大量資源": 16,
            "大量石": 14,
            "鑽石": 10,
            "pack": 4,
            "抽數": 8,
            "開局": 3,
            "初始": 2,
            "進度": 8,
            "等級": 6,
            "高練度": 12,
            "collection": 8,
            "account level": 7,
            "poke gold": 10,
            "stamina": 4,
        }
    )


@dataclass
class FinanceConfig:
    usd_to_twd_rate: float = 32.0
    fee_rate: float = 0.05


@dataclass
class OfferRecord:
    account_name: str
    seller: str
    price: float
    currency: str
    content: str
    offer_url: str


@dataclass
class ScoredOffer:
    account_name: str
    seller: str
    score: int
    category: str
    offer_url: str
    currency: str
    price_score: float
    content_score: float
    content_full: str
    value_analysis: str
    scraped_at: str
    usd_price: float
    twd_price: float
    fee_twd: float
    total_twd: float


def _parse_price(text: str) -> tuple[float, str]:
    raw = (text or "").replace(",", " ").strip()
    values: List[tuple[float, str]] = []
    for currency, value in PRICE_PREFIX_PATTERN.findall(raw):
        try:
            values.append((float(value), currency.upper()))
        except ValueError:
            pass
    for value, currency in PRICE_SUFFIX_PATTERN.findall(raw):
        try:
            values.append((float(value), currency.upper()))
        except ValueError:
            pass
    if not values:
        return 0.0, ""
    price, currency = max(values, key=lambda x: x[0])
    return price, ("USD" if currency in {"$", "US$"} else currency)


async def _wait_and_sleep(page: Page, config: ScraperConfig) -> None:
    await page.wait_for_timeout(int(random.uniform(config.min_delay_sec, config.max_delay_sec) * 1000))


async def _wait_for_offer_links(page: Page, config: ScraperConfig) -> None:
    try:
        await page.wait_for_selector("a[href*='/offer/']", timeout=config.timeout_ms)
    except Exception:
        return


async def _expand_view_more(page: Page, config: ScraperConfig) -> None:
    selectors = [
        "button:has-text('View more')",
        "a:has-text('View more')",
        "button:has-text('Show more')",
        "a:has-text('Show more')",
        "button:has-text('Expand')",
        "a:has-text('Expand')",
        "button:has-text('查看更多')",
        "a:has-text('查看更多')",
        "button:has-text('展开')",
        "a:has-text('展开')",
        "div[role='button']:has-text('展开')",
        "span[role='button']:has-text('展开')",
    ]
    # 有些商品頁需要多次展開，循環點擊直到沒有可見按鈕
    for _ in range(config.expand_view_more_rounds):
        clicked_any = False
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            if count == 0:
                continue
            max_clicks = min(count, 3)
            for i in range(max_clicks):
                candidate = locator.nth(i)
                try:
                    if not await candidate.is_visible():
                        continue
                    await candidate.scroll_into_view_if_needed()
                    await candidate.click(timeout=2500, force=True)
                    clicked_any = True
                    await page.wait_for_timeout(config.expand_wait_ms)
                except Exception:
                    continue
        if not clicked_any:
            break


async def _goto_with_retry(page: Page, url: str, config: ScraperConfig) -> None:
    last_error: Optional[Exception] = None
    for _ in range(config.retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=config.timeout_ms)
            await page.wait_for_load_state("networkidle", timeout=config.timeout_ms)
            return
        except Exception as exc:
            last_error = exc
            await page.wait_for_timeout(1200)
    if last_error:
        raise last_error


async def _extract_offer_links(page: Page) -> List[str]:
    hrefs = await page.eval_on_selector_all(
        "a[href*='/offer/']",
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
    )
    normalized: List[str] = []
    for href in hrefs:
        if not isinstance(href, str):
            continue
        full_url = urljoin("https://www.g2g.com", href)
        if "/categories/" in full_url and "/offer/" in full_url:
            normalized.append(full_url.split("?")[0])
    return list(dict.fromkeys(normalized))


async def _try_next_page(page: Page, config: ScraperConfig) -> bool:
    for selector in [
        "a[rel='next']",
        "button[aria-label='Next']",
        "a:has-text('Next')",
        "button:has-text('Next')",
        "a:has-text('下一頁')",
        "button:has-text('下一頁')",
    ]:
        locator = page.locator(selector).first
        if await locator.count() == 0 or not await locator.is_visible() or await locator.is_disabled():
            continue
        current_url = page.url
        await locator.click()
        await page.wait_for_load_state("domcontentloaded", timeout=config.timeout_ms)
        await page.wait_for_load_state("networkidle", timeout=config.timeout_ms)
        await _wait_for_offer_links(page, config)
        await _wait_and_sleep(page, config)
        return page.url != current_url
    return False


def _soup_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.extract()
    return " ".join(soup.get_text(separator=" ").split())


async def _extract_offer_detail(page: Page, url: str, config: ScraperConfig) -> OfferRecord:
    await _goto_with_retry(page, url, config)
    await _wait_and_sleep(page, config)
    await _expand_view_more(page, config)

    title = await page.title()
    for selector in ["h1", "[data-testid='offer-title']", ".offer-title", ".product-title"]:
        locator = page.locator(selector).first
        if await locator.count() > 0 and await locator.is_visible():
            text = (await locator.inner_text()).strip()
            if text:
                title = text
                break

    body_text = _soup_visible_text(await page.content())
    price = 0.0
    currency = ""
    for selector in [
        "[data-testid='price']",
        ".price",
        ".product-price",
        "span:has-text('US$')",
        "span:has-text('USD')",
        "span:has-text('$')",
    ]:
        locator = page.locator(selector).first
        if await locator.count() > 0:
            parsed_price, parsed_currency = _parse_price((await locator.inner_text()).strip())
            if parsed_price > 0:
                price, currency = parsed_price, parsed_currency
                break
    if price <= 0:
        price, currency = _parse_price(body_text)

    seller = ""
    for selector in [
        "div.text-body2.ellipsis",
        "[data-testid='seller-name']",
        "a[href*='/users/']",
        ".seller-name",
        ".store-name",
    ]:
        locator = page.locator(selector).first
        if await locator.count() > 0:
            candidate = (await locator.inner_text()).strip()
            if candidate and len(candidate) <= 60:
                seller = candidate
                break
    return OfferRecord(
        account_name=title,
        seller=seller or "未知賣家",
        price=price,
        currency=currency or "USD",
        content=body_text,
        offer_url=url,
    )


async def _fetch_offer(context, url: str, config: ScraperConfig, sem: asyncio.Semaphore) -> Optional[OfferRecord]:
    async with sem:
        try:
            detail_page = await context.new_page()
        except Exception:
            return None
        try:
            return await _extract_offer_detail(detail_page, url, config)
        except Exception:
            return None
        finally:
            await detail_page.close()


async def _crawl(config: ScraperConfig) -> List[OfferRecord]:
    offers: List[OfferRecord] = []
    visited_links: Set[str] = set()
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=config.headless)
        context = await browser.new_context()
        page = await context.new_page()
        await _goto_with_retry(page, BASE_LIST_URL, config)
        await _wait_for_offer_links(page, config)

        page_number = 1
        sem = asyncio.Semaphore(max(1, int(config.concurrency)))
        while True:
            print(f"[列表頁] page={page_number} collected={len(offers)}", flush=True)
            links = await _extract_offer_links(page)
            new_links: List[str] = []
            for link in links:
                if link in visited_links:
                    continue
                visited_links.add(link)
                new_links.append(link)

            tasks = [asyncio.create_task(_fetch_offer(context, link, config, sem)) for link in new_links]
            for fut in asyncio.as_completed(tasks):
                record = await fut
                if record is None:
                    continue
                offers.append(record)
                if len(offers) <= 10 or len(offers) % 20 == 0:
                    print(
                        f"[商品] #{len(offers)} seller={record.seller} usd={record.price} url={record.offer_url}",
                        flush=True,
                    )
                if config.max_items and len(offers) >= config.max_items:
                    await browser.close()
                    return offers
            if config.max_pages and page_number >= config.max_pages:
                break
            if not await _try_next_page(page, config):
                break
            page_number += 1
        await browser.close()
    return offers


def _normalize_price(prices: List[float], value: float) -> float:
    if not prices:
        return 0.0
    low = min(prices)
    high = max(prices)
    if high <= low:
        return 50.0
    return ((value - low) / (high - low)) * 100.0


def _content_score(content: str, config: ScoreConfig) -> float:
    text = (content or "").lower()
    score = 0.0
    for keyword, weight in config.keyword_weights.items():
        if keyword.lower() in text:
            score += float(weight)
    return min(score, 100.0)


def _category(score: int, config: ScoreConfig) -> str:
    for name, min_v, max_v in config.score_ranges:
        if min_v <= score <= max_v:
            return name
    return "未知"


def _top_keyword_hits(content: str, config: ScoreConfig, top_n: int = 3) -> List[tuple[str, int]]:
    text = (content or "").lower()
    hits: List[tuple[str, int]] = []
    for keyword, weight in config.keyword_weights.items():
        if keyword.lower() in text:
            hits.append((keyword, weight))
    hits.sort(key=lambda x: x[1], reverse=True)
    return hits[:top_n]


def _build_value_analysis(
    content: str,
    score: int,
    category: str,
    total_twd: float,
    keyword_hits: List[tuple[str, int]],
) -> str:
    if keyword_hits:
        hit_text = "、".join([f"{k}(+{w})" for k, w in keyword_hits])
    else:
        hit_text = "未命中高價值關鍵字"

    if total_twd <= 40 and score >= 50:
        cp_comment = "低總價且分數不低，性價比佳"
    elif total_twd <= 35 and score < 40:
        cp_comment = "價格便宜但內容偏基礎，適合初始開局"
    elif total_twd > 40 and score >= 70:
        cp_comment = "高分高價，偏向資源完整的進階帳號"
    else:
        cp_comment = "綜合表現中等，建議比對賣家評價後再決定"

    return f"{category}；命中關鍵字：{hit_text}；{cp_comment}"


def score_offers(records: List[OfferRecord], score_cfg: ScoreConfig, finance_cfg: FinanceConfig) -> List[ScoredOffer]:
    prices = [r.price for r in records if r.price > 0]
    now = datetime.now().isoformat(timespec="seconds")
    result: List[ScoredOffer] = []
    for record in records:
        p_score = _normalize_price(prices, record.price) if record.price > 0 else 0.0
        c_score = _content_score(record.content, score_cfg)
        total = (p_score * score_cfg.price_weight) + (c_score * score_cfg.content_weight)
        final_score = max(0, min(100, int(round(total))))
        category = _category(final_score, score_cfg)
        usd_price = round(record.price, 2)
        twd_price = round(usd_price * finance_cfg.usd_to_twd_rate, 2)
        fee_twd = round(twd_price * finance_cfg.fee_rate, 2)
        total_twd = round(twd_price + fee_twd, 2)
        keyword_hits = _top_keyword_hits(record.content, score_cfg)
        analysis = _build_value_analysis(record.content, final_score, category, total_twd, keyword_hits)
        result.append(
            ScoredOffer(
                account_name=record.account_name,
                seller=record.seller,
                score=final_score,
                category=category,
                offer_url=record.offer_url,
                currency=record.currency,
                price_score=round(p_score, 2),
                content_score=round(c_score, 2),
                content_full=record.content,
                value_analysis=analysis,
                scraped_at=now,
                usd_price=usd_price,
                twd_price=twd_price,
                fee_twd=fee_twd,
                total_twd=total_twd,
            )
        )
    return result


def export_to_excel(scored_offers: List[ScoredOffer], output_path: str) -> None:
    rows = []
    for row in scored_offers:
        rows.append(
            {
                "帳號名稱": row.account_name,
                "賣家": row.seller,
                "價格(USD)": row.usd_price,
                "價格(TWD)": row.twd_price,
                "手續費(TWD)": row.fee_twd,
                "總價(TWD)": row.total_twd,
                "分數": row.score,
                "類別": row.category,
                "商品連結": row.offer_url,
                "幣別": row.currency,
                "價格分": row.price_score,
                "內容分": row.content_score,
                "內容全文": row.content_full,
                "帳號價值分析": row.value_analysis,
                "抓取時間": row.scraped_at,
            }
        )
    pd.DataFrame(rows).to_excel(output_path, index=False)


def parse_args() -> argparse.Namespace:
    default_finance = FinanceConfig()
    parser = argparse.ArgumentParser(description="G2G Pokemon TCG Pocket 帳號爬蟲與分級")
    parser.add_argument("--max-pages", type=int, default=None, help="最多抓取頁數")
    parser.add_argument("--max-items", type=int, default=None, help="最多抓取商品數")
    parser.add_argument("--headful", action="store_true", help="顯示瀏覽器視窗")
    parser.add_argument("--output", type=str, default=OUTPUT_EXCEL, help="輸出 Excel 檔名")
    parser.add_argument("--usd-to-twd", type=float, default=default_finance.usd_to_twd_rate, help="美金換算台幣匯率")
    parser.add_argument("--fee-rate", type=float, default=default_finance.fee_rate, help="手續費比率，例如 0.05 表示 5%")
    parser.add_argument("--concurrency", type=int, default=5, help="同時抓取商品詳情的並發數（越大越快也越可能被擋）")
    parser.add_argument("--min-delay", type=float, default=0.2, help="每次請求的最小隨機延遲(秒)")
    parser.add_argument("--max-delay", type=float, default=0.6, help="每次請求的最大隨機延遲(秒)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scraper_config = ScraperConfig(
        max_pages=args.max_pages,
        max_items=args.max_items,
        headless=not args.headful,
        concurrency=max(1, int(args.concurrency)),
        min_delay_sec=float(args.min_delay),
        max_delay_sec=float(args.max_delay),
    )
    score_config = ScoreConfig()
    finance_config = FinanceConfig(usd_to_twd_rate=args.usd_to_twd, fee_rate=args.fee_rate)

    print("開始抓取 G2G 商品...")
    records = asyncio.run(_crawl(scraper_config))
    print(f"抓取完成，取得 {len(records)} 筆商品。")

    print("開始評分與分級...")
    scored = score_offers(records, score_config, finance_config)
    print("評分完成。")

    output_path = os.path.abspath(args.output)
    export_to_excel(scored, output_path)
    print(f"Excel 已輸出：{output_path}")


if __name__ == "__main__":
    main()
