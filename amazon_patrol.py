# -*- coding: utf-8 -*-
"""Amazon US 价格巡查 MVP：手动、串行、独立匿名会话，不计算附条件到手价。"""
import asyncio
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

import local_db

MAX_ASINS = 20
US_ZIP = "10001"  # MVP 固定同一美国配送地区；不引入地区配置/账号系统。
AMAZON_US_HOSTS = {"amazon.com", "www.amazon.com"}
AMAZON_JP_BOOTSTRAP_HOSTS = {"amazon.co.jp", "www.amazon.co.jp"}
AMAZON_CHALLENGE_RE = re.compile(r"robot check|enter the characters you see below|automated access to amazon", re.I)
ASIN_RE = re.compile(r"[A-Z0-9]{10}")


class ObservationError(RuntimeError):
    pass


class AccessBlocked(ObservationError):
    pass


def parse_asins(raw):
    if isinstance(raw, str):
        values = re.split(r"[,，;；\s]+", raw)
    elif isinstance(raw, list) and all(isinstance(a, str) for a in raw):
        values = raw
    else:
        raise ValueError("请提供 ASIN 文本或字符串列表")
    asins = []
    for value in values:
        asin = value.strip().upper()
        if not asin:
            continue
        if not ASIN_RE.fullmatch(asin):
            raise ValueError(f"ASIN 格式错误：{asin[:50]}（仅支持10位字母数字，不接受URL）")
        if asin not in asins:
            asins.append(asin)
    if len(asins) > MAX_ASINS:
        raise ValueError(f"MVP 单次最多 {MAX_ASINS} 个 ASIN")
    return asins


def product_url(asin):
    if not ASIN_RE.fullmatch(asin):
        raise ValueError("ASIN 格式错误")
    return f"https://www.amazon.com/dp/{asin}"


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()


def usd_price(text):
    """仅接受明确的美元金额，不把分期、订阅或其它币种当作一次性购买价。"""
    text = clean_text(text)
    if re.search(r"(?:CDN|CAD|CA|AUD|AU|NZ|HK|SG)\s*\$|[€£¥]|/\s*(?:mo|month)\b|per month|monthly|installment|subscribe", text, re.I):
        return None
    match = re.search(r"(?:US\s*\$|\$|USD\s*)(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\d.,])", text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
        return str(amount.quantize(Decimal("0.01"))) if amount > 0 else None
    except InvalidOperation:
        return None


# 只从商品主购买区域取价格；不回退到推荐商品中的通用 .a-price。
EXTRACT_PRODUCT = r"""() => {
  const visible = el => !!el && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  const text = el => (el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
  const first = selectors => {
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el) && text(el)) return text(el);
      }
    }
    return '';
  };
  const all = selectors => [...new Set(selectors.flatMap(selector =>
    [...document.querySelectorAll(selector)].filter(visible).map(text).filter(Boolean)))].join(' | ');
  const present = selectors => selectors.some(s => !!document.querySelector(s));
  const priceSelectors = [
    '#corePriceDisplay_desktop_feature_div .priceToPay',
    '#corePriceDisplay_desktop_feature_div .apexPriceToPay',
    '#corePrice_feature_div .priceToPay',
    '#corePrice_feature_div .apexPriceToPay',
    '#apex_desktop .apexPriceToPay',
    '#corePrice_desktop .apex-price-to-pay-value',
    '#corePrice_desktop .apex-pricetopay-value',
    '#corePriceDisplay_desktop_feature_div .apex-price-to-pay-value',
    '#corePriceDisplay_desktop_feature_div .reinventPricePriceToPayMargin',
    '#corePrice_feature_div .reinventPricePriceToPayMargin',
    '#priceblock_dealprice', '#priceblock_ourprice', '#price_inside_buybox', '#newBuyBoxPrice'
  ];
  const priceValue = el => {
    const price = el.matches('.a-price') ? el : el.querySelector('.a-price');
    if (!price) return text(el);
    const readable = text(price.querySelector('.a-offscreen'));
    if (readable) return readable;
    // 实测新版主价的 a-offscreen 为空，按可见美元符号/整数/小数拼接，不取推荐价。
    const symbol = text(price.querySelector('.a-price-symbol'));
    const whole = text(price.querySelector('.a-price-whole')).replace(/\.$/, '');
    const fraction = text(price.querySelector('.a-price-fraction')) || '00';
    return symbol && whole ? symbol + whole + '.' + fraction : text(price);
  };
  const couponSelectors = ['#couponFeature', '#coupon_feature_div', '#couponText',
    '#promoPriceBlockMessage_feature_div .couponLabelText'];
  const promoSelectors = ['#promotions_feature_div', '#promotion_feature_div',
    '#quickPromoBucketContent', '#applicablePromotionList_feature_div', '#promoPriceBlockMessage_feature_div'];
  const firstPurchasePrice = () => {
    for (const selector of priceSelectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (!visible(el) || !priceValue(el)) continue;
        if (el.closest('[id*="sns" i], [id*="subscription" i], [id*="installment" i]')) continue;
        let parent = el.parentElement, excluded = false;
        for (let depth = 0; parent && depth < 3; depth++, parent = parent.parentElement) {
          const context = text(parent);
          if (/(subscribe\s*(?:&|and)?\s*save|subscription|per month|\/\s*(?:mo|month)\b|monthly payments?|installments?)/i.test(context)) {
            excluded = true; break;
          }
          if (parent.matches('#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #corePrice_desktop, #apex_desktop')) break;
        }
        if (!excluded) return priceValue(el);
      }
    }
    return '';
  };
  return {
    title: first(['#productTitle']),
    page_asin: document.querySelector('#ASIN')?.value ||
      document.querySelector('input[name="ASIN"]')?.value || '',
    price_text: firstPurchasePrice(),
    reference_price_text: first([
      '#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen',
      '#corePrice_feature_div .a-text-price .a-offscreen',
      '#corePrice_desktop .apex-basis-price-value .a-offscreen',
      '#corePrice_desktop .apex-basisprice-value .a-offscreen', '#priceblock_listprice',
      '#listPrice', '#listPriceLegalMessage .a-offscreen'
    ]),
    price_region_seen: present(['#corePriceDisplay_desktop_feature_div', '#corePrice_feature_div', '#corePrice_desktop',
      '#priceblock_ourprice', '#priceblock_dealprice']),
    delivery_location: first(['#glow-ingress-block', '#nav-global-location-popover-link']),
    coupon_text: all(couponSelectors), promotions_text: all(promoSelectors),
    offer_regions_seen: present([...couponSelectors, ...promoSelectors, '#corePriceDisplay_desktop_feature_div', '#corePrice_feature_div']),
    availability_text: first(['#availability', '#outOfStock', '#availabilityInsideBuyBox_feature_div']),
    captcha: visible(document.querySelector('#captchacharacters')),
    page_title: document.title,
    body_excerpt: (document.body?.innerText || '').slice(0, 14000)
  };
}"""


def classify_offers(raw, observed_at=None, price=None, reference_price=None):
    """Classify only explicit visible deal evidence; generic discounts are not Lightning Deals."""
    regions_seen = bool(raw.get("offer_regions_seen") or raw.get("coupon_region_seen") or raw.get("promo_region_seen"))
    if not regions_seen:
        return {"offer_types": ["unknown"], "offer_evidence": [],
                "offer_read_status": "not_read", "offer_observed_at": None}
    coupon = clean_text(raw.get("coupon_text"))
    promotion = clean_text(raw.get("promotions_text"))
    combined = " | ".join(value for value in (coupon, promotion) if value)
    found: list[str] = []
    evidence: list[dict[str, str]] = []

    def add(kind, source, excerpt):
        if kind not in found:
            found.append(kind)
            evidence.append({"type": kind, "source": source, "text": excerpt[:300]})

    if re.search(r"\blightning\s+deal\b", combined, re.I):
        add("lightning_deal", "coupon_or_promotion_region", combined)
    if re.search(r"\blimited[- ]time\s+deal\b", combined, re.I):
        add("limited_time_deal", "coupon_or_promotion_region", combined)
    if re.search(r"\bprime\s+(?:exclusive|member[- ]exclusive)(?:\s+(?:deal|discount|offer))?", combined, re.I):
        add("prime_exclusive", "coupon_or_promotion_region", combined)
    if coupon and not re.search(r"\bno\s+coupons?\b", coupon, re.I):
        add("coupon", "coupon_region", coupon)
    if promotion and re.search(r"\bpromotion\b|\bbuy\s+.{1,80}\s+get\b|\bsave\s+\$", promotion, re.I):
        add("promotion", "promotion_region", promotion)
    try:
        price_value = Decimal(str(price)) if price not in (None, "") else None
        reference_value = Decimal(str(reference_price)) if reference_price not in (None, "") else None
        if price_value is not None and reference_value is not None and reference_value > price_value:
            add("reference_discount", "strikethrough_reference_price",
                f"购买价 {price_value:.2f} USD < 划线参考价 {reference_value:.2f} USD；不构成秒杀证据")
    except (InvalidOperation, ValueError):
        pass
    if not found:
        found.append("unknown")
    return {"offer_types": found, "offer_evidence": evidence,
            "offer_read_status": "observed" if evidence else "read_no_explicit_offer",
            "offer_observed_at": observed_at or __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")}


def normalize_observation(asin, raw, url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("www.amazon.com", "amazon.com"):
        raise ObservationError("页面离开 Amazon US，已跳过")
    blocking_text = f"{raw.get('page_title', '')} {raw.get('body_excerpt', '')}"
    if raw.get("captcha") or re.search(
            r"robot check|enter the characters you see below|sorry, we just need to make sure|automated access to amazon", blocking_text, re.I):
        raise AccessBlocked("Amazon 出现验证码或访问限制，本轮停止；不自动绕过")
    if not clean_text(raw.get("title")):
        raise ObservationError("未识别到商品标题，不能确认这是正常商品页")
    actual = clean_text(raw.get("page_asin")).upper()
    if not actual:
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", parsed.path, re.I)
        actual = match.group(1).upper() if match else ""
    if actual != asin:
        raise ObservationError(f"实际商品 ASIN {actual or '未识别'} 与目标不符，未更新基线")
    price = usd_price(raw.get("price_text"))
    availability = clean_text(raw.get("availability_text")).lower()
    state = None
    if re.search(r"out of stock", availability):
        state = "out_of_stock"
    elif "currently unavailable" in availability or "currently not available" in availability:
        state = "unavailable"
    elif "in stock" in availability:
        state = "available"
    if not price and state not in ("unavailable", "out_of_stock"):
        raise ObservationError("未识别到明确的一次性美元购买价，也未确认缺货；保留原基线")
    data = {"title": clean_text(raw["title"])}
    notes = []
    reference = None
    if price and state not in ("unavailable", "out_of_stock"):
        data.update(price=price, currency="USD")
        reference_text = clean_text(raw.get("reference_price_text"))
        reference = usd_price(reference_text)
        if reference:
            data["reference_price"] = reference
        elif raw.get("price_region_seen") and not reference_text:
            data["reference_price"] = ""
        unknown = []
        for field, label in (("coupon", "Coupon"), ("promotions", "促销")):
            value = clean_text(raw.get(f"{field}_text"))
            if value:
                # 缺失/空容器都不是取消优惠；仅可见原文明确说无优惠时才清空。
                absent = re.search(r"\bno (?:coupons?|promotions?) (?:are )?(?:currently )?available\b", value, re.I)
                data[field] = "" if absent else value
            else:
                unknown.append(label)
        if unknown:
            notes.append("/".join(unknown) + "未读取，保留上次有效值（不代表无优惠）")
    else:
        notes.append("缺货：页面明确提示暂时无货；保留金额为历史价，不代表当前可购买")
    if state not in ("unavailable", "out_of_stock"):
        offers = classify_offers(raw, price=price, reference_price=reference)
        data["offer_read_status"] = offers["offer_read_status"]
        if offers["offer_read_status"] != "not_read":
            data["offer_types"] = offers["offer_types"]
            data["offer_evidence"] = offers["offer_evidence"]
            data["offer_observed_at"] = offers["offer_observed_at"]
        else:
            notes.append("优惠区域未读取；保留上次有效优惠，不能据此判断已取消")
    if state:
        data["availability"] = state
    return data, "；".join(notes)


async def _visible_first(page, selectors):
    """Amazon 弹窗可能在 DOM 中留有隐藏控件；返回首个真正可见的候选。"""
    for selector in selectors:
        matches = page.locator(selector)
        for index in range(await matches.count()):
            candidate = matches.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


LOCATION_ENTRY_SELECTORS = (
    "#nav-global-location-popover-link", "#glow-ingress-block", "#nav-global-location-data-modal-action",
    "[data-csa-c-slot-id='nav-global-location-slot']",
    "button[aria-label*='delivery location' i]", "a[aria-label*='delivery location' i]",
    "button[aria-label*='deliver to' i]", "a[aria-label*='deliver to' i]",
    "button:has-text('Deliver to')", "a:has-text('Deliver to')", "[role='button']:has-text('Deliver to')",
)


async def _wait_for_location_entry(page, timeout_ms=25000):
    """Wait through Amazon's delayed/202 homepage refresh for the visible location control."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while True:
        candidate = await _visible_first(page, LOCATION_ENTRY_SELECTORS)
        if candidate is not None:
            return candidate
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        await page.wait_for_timeout(min(300, max(1, int(remaining * 1000))))


ZIP_INPUT_SELECTORS = (
    "#GLUXZipUpdateInput:visible",
    "#a-popover-content-1 input[type='text']:visible",
    ".a-popover-wrapper:visible input[type='text']",
    "input[aria-label*='zip' i]:visible",
    "input[placeholder*='zip' i]:visible",
)


async def _wait_us_zip_input(page, timeout_ms):
    try:
        await page.wait_for_function("""() => {
          const visible = el => !!el && el.getClientRects().length > 0 &&
            getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
          return [...document.querySelectorAll('#GLUXZipUpdateInput, #a-popover-content-1 input[type="text"], '
            + '.a-popover-wrapper input[type="text"], input[aria-label*="zip" i], input[placeholder*="zip" i]')]
            .some(visible);
        }""", timeout=timeout_ms)
    except Exception:
        return None
    return await _visible_first(page, ZIP_INPUT_SELECTORS)


async def _confirmed_us_zip(page):
    location_node = await _visible_first(page, LOCATION_ENTRY_SELECTORS)
    if location_node is None:
        return False
    try:
        location = clean_text(await location_node.inner_text(timeout=4000))
    except Exception:
        return False
    return bool(re.search(rf"(?<!\d){re.escape(US_ZIP)}(?!\d)", location))


async def _wait_for_us_storefront(page, job):
    hostname = (urlsplit(page.url).hostname or "").casefold()
    if hostname in AMAZON_US_HOSTS:
        return
    if hostname not in AMAZON_JP_BOOTSTRAP_HOSTS:
        raise ObservationError(f"Amazon US 初始化后落在非美国站点（{hostname or '未知'}），本轮未采集价格")
    if not isinstance(job, dict) or job.get("job_source") != "manual":
        raise AccessBlocked("JP 出口将 Amazon.com 重定向到 Amazon.co.jp；自动任务不会跨站采集")
    job["detail"] = "JP 节点将 Amazon US 引到 Amazon.co.jp；请在打开的浏览器中切回 Amazon.com，之后会继续设置邮编 10001。"
    job.setdefault("log", []).append("[i] 等待人工切回 Amazon.com；不会在日本站采集价格")
    try:
        await page.wait_for_function(
            "hosts => hosts.includes((location.hostname || '').toLowerCase())",
            arg=sorted(AMAZON_US_HOSTS), timeout=120000,
        )
    except Exception as exc:
        raise ObservationError("等待切回 Amazon.com 超时；本轮未巡查商品。") from exc
    if (urlsplit(page.url).hostname or "").casefold() not in AMAZON_US_HOSTS:
        raise ObservationError("页面尚未回到 Amazon.com；本轮未采集价格")
    job["log"].append("[+] 已回到 Amazon.com，继续设置美国配送邮编")


async def _wait_for_manual_verification(page, job):
    if not isinstance(job, dict) or job.get("job_source") != "manual":
        raise AccessBlocked("Amazon 要求人工验证；定时巡查已停止，不会绕过验证")
    job["detail"] = "Amazon 要求人工验证；请在打开的浏览器中完成验证，之后会继续选择邮编 10001。"
    job.setdefault("log", []).append("[i] 等待人工完成 Amazon 验证；不会尝试绕过验证码")
    try:
        await page.wait_for_function("""() => {
          const visible = el => !!el && el.getClientRects().length > 0 &&
            getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
          const form = [...document.querySelectorAll('#captchacharacters, input[name*="captcha" i], iframe[src*="captcha" i]')].some(visible);
          const text = `${document.title || ''} ${document.body?.innerText || ''}`;
          return !form && !/robot check|enter the characters you see below|automated access to amazon/i.test(text);
        }""", timeout=180000)
    except Exception as exc:
        raise AccessBlocked("等待人工 Amazon 验证超时；没有访问商品或更新价格") from exc
    if (urlsplit(page.url).hostname or "").casefold() not in AMAZON_US_HOSTS:
        await _wait_for_us_storefront(page, job)
    title = await page.title()
    try:
        body = await page.locator("body").inner_text(timeout=4000)
    except Exception:
        body = ""
    if AMAZON_CHALLENGE_RE.search(f"{title} {body}"):
        raise AccessBlocked("Amazon 人工验证尚未完成；没有更新价格")
    job["log"].append("[+] 人工验证已通过，继续选择美国配送邮编")


async def _wait_for_manual_us_zip(page, job, reason):
    if not isinstance(job, dict) or job.get("job_source") != "manual":
        raise ObservationError(reason)
    job["detail"] = f"{reason} 请在打开的浏览器中选择美国邮编 {US_ZIP}；确认后会自动继续。"
    job.setdefault("log", []).append(f"[i] 等待人工将配送地址设为 ZIP {US_ZIP}，最多 2 分钟")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120
    while loop.time() < deadline:
        if await _confirmed_us_zip(page):
            break
        await page.wait_for_timeout(400)
    else:
        raise ObservationError(f"人工选择邮编 {US_ZIP} 超时；本轮未更新商品价格。")
    if (urlsplit(page.url).hostname or "").casefold() not in AMAZON_US_HOSTS:
        raise ObservationError("人工选择后页面未停留在 Amazon.com；本轮未更新商品价格。")
    if not await _confirmed_us_zip(page):
        raise ObservationError(f"未能确认配送邮编 {US_ZIP}；本轮未更新商品价格。")
    job["log"].append(f"[+] 已人工确认 Amazon.com 配送邮编 {US_ZIP}")


async def prepare_us_delivery(page, job=None):
    """固定美国配送邮编；地域跳转或界面变化时允许手动确认，不绕过验证码。"""
    response = await page.goto("https://www.amazon.com/", wait_until="domcontentloaded", timeout=60000)
    if response and response.status in (403, 429, 503):
        raise AccessBlocked(f"Amazon HTTP {response.status}，未设置配送地区，本轮停止")
    if response and response.status >= 400:
        raise ObservationError(f"配送地区初始化失败：Amazon HTTP {response.status}")
    try:
        # JP 出口可能把 Amazon.com 落到日本站；只等待人工切回美国站，不在日本站采集。
        await _wait_for_us_storefront(page, job)
        body_text = ""
        try:
            body_text = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            pass
        title = await page.title()
        captcha = await _visible_first(page, ("#captchacharacters", "input[name*='captcha' i]"))
        if captcha is not None or AMAZON_CHALLENGE_RE.search(f"{title} {body_text}"):
            await _wait_for_manual_verification(page, job)

        # Amazon 可保留已选邮编却隐藏 ZIP 输入框；先读位置，已符合时不再打开弹窗。
        if await _confirmed_us_zip(page):
            return
        if isinstance(job, dict):
            job["detail"] = "等待 Amazon 美国站加载配送位置控件…"
        location_button = await _wait_for_location_entry(page, timeout_ms=25000)
        if await _confirmed_us_zip(page):
            return
        if location_button is None:
            await _wait_for_manual_us_zip(page, job, "Amazon 页面未显示配送位置入口。")
            return
        await location_button.click(timeout=15000)

        # JP 节点/慢速页面会延迟挂载 GLUX 控件，先等候；部分地域版还需点 Change Address。
        zipcode = await _wait_us_zip_input(page, timeout_ms=8000)
        if zipcode is None and not await _confirmed_us_zip(page):
            change_address = await _visible_first(page, (
                ".glow-toaster-button-submit input[type='submit']",
                ".glow-toaster-button-submit button",
                "button:has-text('Change Address')",
            ))
            if change_address is not None:
                try:
                    await change_address.click(timeout=7000)
                except Exception:
                    # Amazon a-button 的文本层可能被 submit input 覆盖；只点击已定位的 Change Address 控件。
                    await change_address.evaluate("el => el.click()")
                zipcode = await _wait_us_zip_input(page, timeout_ms=18000)
        if zipcode is None:
            if await _confirmed_us_zip(page):
                return
            await _wait_for_manual_us_zip(page, job,
                "Amazon 配送弹窗未显示可见的邮编输入框；请在页面中打开配送位置设置。")
            return

        await zipcode.fill(US_ZIP, timeout=8000)
        apply = await _visible_first(page, (
            "#GLUXZipUpdate:visible",
            "#a-popover-content-1 button[type='submit']:visible",
            "#a-popover-content-1 input[type='submit']:visible",
            ".a-popover-wrapper:visible button:has-text('Apply')",
            ".a-popover-wrapper:visible input[type='submit']",
        ))
        if apply is None:
            await _wait_for_manual_us_zip(page, job, "已填入邮编，但配送弹窗未显示 Apply 按钮。")
            return
        await apply.click(timeout=10000)

        # Apply 后的确认控件会异步出现；优先自动确认，超时则保留浏览器给用户处理。
        try:
            await page.wait_for_function(
                "zip => [...document.querySelectorAll('#glow-ingress-block, #nav-global-location-popover-link')]"
                ".some(el => el.innerText.includes(zip)) || "
                "[...document.querySelectorAll('#GLUXConfirmClose, button[name=glowDoneButton]')]"
                ".some(el => el.getClientRects().length && getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden')",
                arg=US_ZIP, timeout=15000)
        except Exception:
            await _wait_for_manual_us_zip(page, job, "Amazon 尚未确认配送邮编。")
            return
        if not await _confirmed_us_zip(page):
            close = await _visible_first(page, (
                "input#GLUXConfirmClose[type='submit']",
                "#GLUXConfirmClose:visible",
                "input[name='glowDoneButton'][type='button']",
                "#a-popover-content-1 button[name='glowDoneButton']:visible",
                ".a-popover-wrapper:visible button:has-text('Done')",
            ))
            if close is None:
                await _wait_for_manual_us_zip(page, job, "邮编已提交，但 Amazon 未显示可见的确认按钮。")
                return
            try:
                await close.click(timeout=4000)
            except Exception:
                clicked = await page.evaluate("""zip => {
                  const visible = el => !!el && el.getClientRects().length > 0 &&
                    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
                  const body = document.body?.innerText || '';
                  if (!body.includes(zip) || !/You're now shopping for delivery to|selected location to show all products available for the United States/i.test(body)) return false;
                  const submit = [...document.querySelectorAll('input#GLUXConfirmClose[type="submit"]')].find(visible);
                  const done = [...document.querySelectorAll('button[name="glowDoneButton"]')].find(visible);
                  const control = submit || done;
                  if (!control) return false;
                  control.click();
                  return true;
                }""", US_ZIP)
                if not clicked and not await _confirmed_us_zip(page):
                    await _wait_for_manual_us_zip(page, job, "邮编已提交，但确认按钮被遮挡或无法点击。")
                    return
        try:
            await page.wait_for_function(
                "zip => [...document.querySelectorAll('#glow-ingress-block, #nav-global-location-popover-link')]"
                ".some(el => el.innerText.includes(zip))", arg=US_ZIP, timeout=15000)
        except Exception:
            await _wait_for_manual_us_zip(page, job, "等待配送位置更新超时。")
    except AccessBlocked:
        raise
    except Exception as exc:
        if isinstance(exc, ObservationError):
            raise
        raise ObservationError(f"未能确认美国配送邮编 {US_ZIP}，本轮停止，未更新价格：{str(exc)[:200]}") from exc


async def collect_page(page, asin):
    response = await page.goto(product_url(asin), wait_until="domcontentloaded", timeout=45000)
    if (urlsplit(page.url).hostname or "").casefold() not in AMAZON_US_HOSTS:
        raise AccessBlocked("商品页离开 Amazon.com 美国站，停止采集以免记录日本站价格")
    if response and response.status in (403, 429, 503):
        raise AccessBlocked(f"Amazon HTTP {response.status}，本轮停止；稍后人工重试")
    if response and response.status >= 400:
        raise ObservationError(f"Amazon HTTP {response.status}，未更新基线")
    await page.wait_for_timeout(1500)
    raw = await page.evaluate(EXTRACT_PRODUCT)
    if not raw.get("title") and not raw.get("captcha") and "robot check" not in raw.get("page_title", "").lower():
        try:
            await page.wait_for_selector("#productTitle", timeout=8000)
            raw = await page.evaluate(EXTRACT_PRODUCT)
        except Exception:
            pass
    data, note = normalize_observation(asin, raw, page.url)
    if US_ZIP not in clean_text(raw.get("delivery_location")):
        raise ObservationError(f"本页未确认配送邮编 {US_ZIP}，未更新价格")
    return data, note


async def run_patrol(asins, job):
    """由 Web 手动调用。未来定时可复用，但本版不创建调度器/CLI。"""
    job.update(changed=0, baselines=0, unchanged=0, failed=0, results=[])
    browser = None
    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=False)
                context = await browser.new_context(locale="en-US", viewport={"width": 1440, "height": 1000},
                                                    accept_downloads=False)
                page = await context.new_page()

                allow_jp_bootstrap = [True]

                async def restrict_navigation(route):
                    request = route.request
                    if request.is_navigation_request() and request.frame == page.main_frame:
                        u = urlsplit(request.url)
                        host = (u.hostname or "").casefold()
                        us_host = host in AMAZON_US_HOSTS
                        jp_bootstrap = allow_jp_bootstrap[0] and host in AMAZON_JP_BOOTSTRAP_HOSTS
                        if u.scheme != "https" or not (us_host or jp_bootstrap):
                            await route.abort()
                            return
                    await route.continue_()

                await page.route("**/*", restrict_navigation)
                job["detail"] = f"初始化美国配送邮编 {US_ZIP}…"
                job["log"].append(f"[i] 固定匿名观察环境：Amazon US / 邮编 {US_ZIP}；不登录、不计算到手价")
                await prepare_us_delivery(page, job)
                allow_jp_bootstrap[0] = False
                for i, asin in enumerate(asins, 1):
                    job["detail"] = f"{i}/{len(asins)} · {asin}"
                    job["log"].append(f"[i] 巡查 {asin}")
                    blocked = False
                    try:
                        data, note = await collect_page(page, asin)
                        result = local_db.amz_record(asin, data, note=note)
                        result["availability"] = data.get("availability")  # 本次结论，不把历史库存冒充实时状态
                        counter = {"baseline": "baselines", "changed": "changed", "unchanged": "unchanged"}[result["status"]]
                        job[counter] += 1
                        job["log"].append(f"[+] {asin}: {result['status']}" + (f" · {note}" if note else ""))
                    except Exception as exc:
                        message = str(exc)[:500]
                        local_db.amz_record_error(asin, message)
                        result = {"asin": asin, "status": "error", "changes": {}, "message": message,
                                  "availability": None}
                        job["failed"] += 1
                        job["log"].append(f"[!] {asin}: {message}")
                        blocked = isinstance(exc, AccessBlocked)
                    job["results"].append(result)
                    job["done"] = i
                    job["rows"] = job["changed"]
                    if blocked:
                        job["status"] = "error"
                        job["detail"] = f"访问受限，已停止；剩余 {len(asins) - i} 个未访问"
                        break
                    if i < len(asins):
                        await asyncio.sleep(3)
                if job["status"] == "running":
                    job["status"] = "done" if job["failed"] < len(asins) else "error"
                    job["detail"] = "巡查结束（仅保存基线和变化；失败未覆盖旧值）"
            finally:
                if browser:
                    await browser.close()
    except Exception as exc:
        job["status"] = "error"
        job["detail"] = str(exc)[:500]
        job["log"].append(f"[x] 巡查失败: {type(exc).__name__}: {exc}")
    finally:
        if job["status"] == "running":
            job["status"] = "error"
            job["detail"] = "巡查被中断，可人工重新运行"
