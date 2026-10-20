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
    availability_text: first(['#availability', '#outOfStock', '#availabilityInsideBuyBox_feature_div']),
    captcha: visible(document.querySelector('#captchacharacters')),
    page_title: document.title,
    body_excerpt: (document.body?.innerText || '').slice(0, 14000)
  };
}"""


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
    if state:
        data["availability"] = state
    return data, "；".join(notes)


async def prepare_us_delivery(page):
    """通过官网的公开配送弹窗固定美国邮编；失败即停止，不猜测价格环境。"""
    response = await page.goto("https://www.amazon.com/", wait_until="domcontentloaded", timeout=45000)
    if response and response.status in (403, 429, 503):
        raise AccessBlocked(f"Amazon HTTP {response.status}，未设置配送地区，本轮停止")
    if response and response.status >= 400:
        raise ObservationError(f"配送地区初始化失败：Amazon HTTP {response.status}")
    if await page.locator("#captchacharacters").count():
        raise AccessBlocked("Amazon 出现验证码，本轮停止；不自动绕过")
    try:
        await page.locator("#nav-global-location-popover-link").click(timeout=15000)
        zipcode = page.locator("#GLUXZipUpdateInput")
        await zipcode.fill(US_ZIP, timeout=15000)
        await page.locator("#GLUXZipUpdate").click(timeout=15000)
        # Apply 后确认按钮异步出现，不能用固定短等待后直接跳过。
        location = clean_text(await page.locator("#nav-global-location-popover-link").inner_text())
        if US_ZIP not in location:
            close = page.locator("#GLUXConfirmClose:visible").first
            await close.wait_for(state="visible", timeout=12000)
            await close.click()
        await page.wait_for_function(
            "zip => [...document.querySelectorAll('#glow-ingress-block, #nav-global-location-popover-link')]"
            ".some(el => el.innerText.includes(zip))", arg=US_ZIP, timeout=12000)
    except Exception as exc:
        raise ObservationError(f"未能确认美国配送邮编 {US_ZIP}，本轮停止，未更新价格：{str(exc)[:200]}") from exc


async def collect_page(page, asin):
    response = await page.goto(product_url(asin), wait_until="domcontentloaded", timeout=45000)
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

                async def restrict_navigation(route):
                    request = route.request
                    if request.is_navigation_request() and request.frame == page.main_frame:
                        u = urlsplit(request.url)
                        if u.scheme != "https" or u.hostname not in ("www.amazon.com", "amazon.com"):
                            await route.abort()
                            return
                    await route.continue_()

                await page.route("**/*", restrict_navigation)
                job["detail"] = f"初始化美国配送邮编 {US_ZIP}…"
                job["log"].append(f"[i] 固定匿名观察环境：Amazon US / 邮编 {US_ZIP}；不登录、不计算到手价")
                await prepare_us_delivery(page)
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
