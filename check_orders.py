#!/usr/bin/env python3
"""Capture Shopee order API responses with a saved Playwright session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error, Page, Response, async_playwright

from login_shopee import DEFAULT_STATE_PATH, DEFAULT_USER_AGENT, chromium_args


ORDER_URLS = [
    "https://shopee.co.id/user/purchase/",
    "https://shopee.co.id/user/purchase?type=3",
    "https://shopee.co.id/user/purchase?type=8",
    "https://shopee.co.id/user/purchase?type=12",
]
DEFAULT_OUTPUT_PATH = "output/shopee-orders.json"
DEFAULT_SCREENSHOT_PATH = "output/shopee-orders.png"
DEFAULT_API_DUMP_PATH = "output/shopee-order-api.json"
DEFAULT_NETWORK_LOG_PATH = "output/shopee-network-log.json"
DEFAULT_SIMPLE_OUTPUT_PATH = "output/shopee-orders-simple.json"
DEFAULT_REPORT_OUTPUT_PATH = "output/shopee-orders-report.md"
DEFAULT_MESSAGE_OUTPUT_PATH = "output/shopee-orders-message.txt"
ORDER_WAIT_MS = 45000
API_URL_HINTS = (
    "order",
    "orders",
    "purchase",
    "checkout",
    "logistics",
    "tracking",
    "shipment",
    "api/v4",
)
ORDER_KEY_HINTS = (
    "order",
    "orderid",
    "order_id",
    "order_sn",
    "ordersn",
    "checkout",
    "purchase",
    "shipment",
    "shipping",
    "logistics",
    "tracking",
    "item",
    "product",
    "shop",
    "seller",
    "status",
)
COMPLETED_STATUS_HINTS = {
    "completed",
    "complete",
    "selesai",
    "diterima",
    "cancelled",
    "canceled",
    "cancel",
    "batal",
    "refund",
    "returned",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Shopee orders using an existing Playwright storage state."
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE_PATH,
        help=f"Playwright storage state path. Defaults to {DEFAULT_STATE_PATH}.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_PATH,
        help=f"Where to save parsed API summary. Defaults to {DEFAULT_OUTPUT_PATH}.",
    )
    parser.add_argument(
        "--api-dump",
        default=DEFAULT_API_DUMP_PATH,
        help=f"Where to save matched raw API payloads. Defaults to {DEFAULT_API_DUMP_PATH}.",
    )
    parser.add_argument(
        "--network-log",
        default=DEFAULT_NETWORK_LOG_PATH,
        help=f"Where to save fetch/xhr request metadata. Defaults to {DEFAULT_NETWORK_LOG_PATH}.",
    )
    parser.add_argument(
        "--simple-output",
        default=DEFAULT_SIMPLE_OUTPUT_PATH,
        help=f"Where to save simplified orders JSON. Defaults to {DEFAULT_SIMPLE_OUTPUT_PATH}.",
    )
    parser.add_argument(
        "--report-output",
        default=DEFAULT_REPORT_OUTPUT_PATH,
        help=f"Where to save active orders report. Defaults to {DEFAULT_REPORT_OUTPUT_PATH}.",
    )
    parser.add_argument(
        "--message-output",
        default=DEFAULT_MESSAGE_OUTPUT_PATH,
        help=f"Where to save WhatsApp-ready order message text. Defaults to {DEFAULT_MESSAGE_OUTPUT_PATH}.",
    )
    parser.add_argument(
        "--print-report",
        action="store_true",
        help="Print only the active orders report. Uses existing output unless --refresh is passed.",
    )
    parser.add_argument(
        "--print-message",
        action="store_true",
        help="Print only WhatsApp-ready order message text. Uses existing output unless --refresh is passed.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force a fresh Shopee API request even when local output already exists.",
    )
    parser.add_argument(
        "--all-orders",
        action="store_true",
        help="Include completed, canceled, and inactive orders in the simplified output.",
    )
    parser.add_argument(
        "--date-from",
        help="Filter simplified orders by visible order date from this value, for example 2026-06-01.",
    )
    parser.add_argument(
        "--date-to",
        help="Filter simplified orders by visible order date through this value, for example 2026-06-28.",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=20,
        help="Order-list limit used when replaying captured Shopee order requests. Defaults to 20.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="Maximum captured order-list pages to replay when date filters or --all-orders are used.",
    )
    parser.add_argument(
        "--screenshot",
        default=DEFAULT_SCREENSHOT_PATH,
        help=f"Where to save a page screenshot. Defaults to {DEFAULT_SCREENSHOT_PATH}.",
    )
    parser.add_argument("--headful", action="store_true", help="Run with a visible browser.")
    parser.add_argument(
        "--allow-http2",
        action="store_true",
        help="Do not pass Chromium's --disable-http2 workaround.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45000,
        help="Default Playwright timeout in milliseconds.",
    )
    return parser.parse_args()


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value).lower())


def compact_payload(value: object, max_chars: int = 1200) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def compact_text(value: str | None, max_chars: int = 2000) -> str | None:
    if value is None or len(value) <= max_chars:
        return value
    return value[:max_chars] + "..."


def url_looks_relevant(url: str) -> bool:
    lowered = url.lower()
    return "shopee.co.id" in lowered and any(hint in lowered for hint in API_URL_HINTS)


def payload_looks_relevant(value: object, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = normalize_key(key)
            if any(hint in normalized for hint in ORDER_KEY_HINTS):
                return True
            if payload_looks_relevant(nested, depth + 1):
                return True
    elif isinstance(value, list):
        return any(payload_looks_relevant(item, depth + 1) for item in value[:50])
    elif isinstance(value, str):
        lowered = value.lower()
        return any(word in lowered for word in ["pesanan", "order", "resi", "kurir"])
    return False


async def goto_first_available(page: Page) -> str:
    errors = []
    for url in ORDER_URLS:
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            if "/login" not in page.url.lower():
                return page.url
            errors.append(f"{url}: redirected to login")
        except Error as exc:
            errors.append(f"{url}: {str(exc).splitlines()[0]}")
    raise RuntimeError("Could not open Shopee orders. " + "; ".join(errors))


async def click_reload(page: Page) -> bool:
    for selector in [
        "button:has-text('Muat Ulang')",
        "button:has-text('Coba Lagi')",
        "button:has-text('Reload')",
        "button:has-text('Try Again')",
        "text=/muat ulang/i",
        "text=/coba lagi/i",
        "text=/try again/i",
    ]:
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=1500)
            await locator.click()
            return True
        except Error:
            continue
    return False


async def wait_for_api_capture(page: Page, captured: list[dict]) -> str:
    deadline = asyncio.get_running_loop().time() + ORDER_WAIT_MS / 1000
    clicked_reload = False
    while asyncio.get_running_loop().time() < deadline:
        if captured:
            return "api_loaded"
        try:
            page_text = normalize_text(await page.locator("body").inner_text(timeout=5000))
        except Error:
            await page.wait_for_timeout(1000)
            continue

        lowered = page_text.lower()
        if any(text in lowered for text in ["belum ada pesanan", "no orders", "tidak ada pesanan"]):
            return "empty"
        if any(text in lowered for text in ["coba lagi", "try again", "terjadi kesalahan"]):
            if not clicked_reload:
                clicked_reload = await click_reload(page)
                if clicked_reload:
                    await page.wait_for_timeout(5000)
                    continue
            return "error"
        await page.wait_for_timeout(2000)
    return "api_loaded" if captured else "timeout"


def find_order_objects(value: object, depth: int = 0) -> list[dict]:
    if depth > 10:
        return []
    matches: list[dict] = []
    if isinstance(value, dict):
        keys = {normalize_key(key) for key in value}
        has_order_id = bool(keys & {"orderid", "order_id", "ordersn", "order_sn", "checkoutid", "checkout_id"})
        has_order_shape = any("order" in key for key in keys) and any(
            key in keys for key in ["items", "itemlist", "item_list", "products", "productlist", "product_list"]
        )
        if has_order_id or has_order_shape:
            matches.append(value)
        for nested in value.values():
            matches.extend(find_order_objects(nested, depth + 1))
    elif isinstance(value, list):
        for item in value[:200]:
            matches.extend(find_order_objects(item, depth + 1))
    return matches


def summarize_api_payloads(captured: list[dict]) -> list[dict]:
    summaries = []
    seen = set()
    for item in captured:
        payload = item["payload"]
        objects = find_order_objects(payload) or [payload]
        for obj in objects[:20]:
            preview = compact_payload(obj)
            key = (item["url"], preview)
            if key in seen:
                continue
            seen.add(key)
            summaries.append(
                {
                    "source_url": item["url"],
                    "status": item["status"],
                    "preview": preview,
                }
            )
    return summaries


def first_value(data: dict, keys: list[str]) -> object:
    for key in keys:
        if key in data and data[key] not in (None, "", []):
            return data[key]
    return None


def deep_first_value(value: object, keys: set[str], depth: int = 0) -> object:
    if depth > 6:
        return None
    if isinstance(value, dict):
        for key, nested in value.items():
            if normalize_key(key) in keys and nested not in (None, "", []):
                return nested
        for nested in value.values():
            found = deep_first_value(nested, keys, depth + 1)
            if found not in (None, "", []):
                return found
    elif isinstance(value, list):
        for item in value[:50]:
            found = deep_first_value(item, keys, depth + 1)
            if found not in (None, "", []):
                return found
    return None


def format_money(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        amount = int(value)
        if amount >= 100000:
            amount = round(amount / 100000)
        return f"Rp{amount:,}".replace(",", ".")
    if isinstance(value, dict):
        for key in ["display", "text", "value", "amount", "price"]:
            nested = value.get(key)
            formatted = format_money(nested)
            if formatted:
                return formatted
    return str(value)


def stringify_status(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (int, float)):
        return str(int(value))
    if isinstance(value, dict):
        for key in ["text", "label", "display", "description", "status", "title", "name"]:
            nested = value.get(key)
            if nested not in (None, ""):
                return stringify_status(nested)
    return normalize_text(value)


def collect_product_dicts(order: dict) -> list[dict]:
    candidates = []
    for key in [
        "item_list",
        "itemlist",
        "items",
        "product_list",
        "productlist",
        "products",
        "parcel_items",
        "order_items",
    ]:
        value = order.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    info_card = order.get("info_card")
    if isinstance(info_card, dict):
        for key in ["order_list_cards", "product_info", "items"]:
            value = info_card.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
    return candidates


def product_name(product: dict) -> str:
    value = first_value(
        product,
        [
            "name",
            "item_name",
            "product_name",
            "title",
            "model_name",
            "display_name",
        ],
    )
    if value:
        return normalize_text(value)
    item_basic = product.get("item_basic")
    if isinstance(item_basic, dict):
        value = first_value(item_basic, ["name", "item_name", "title"])
        if value:
            return normalize_text(value)
    return ""


def format_product(product: dict) -> dict:
    item_basic = product.get("item_basic") if isinstance(product.get("item_basic"), dict) else {}
    merged = {**item_basic, **product}
    price = first_value(
        merged,
        ["price", "item_price", "price_before_discount", "discounted_price", "model_price"],
    )
    quantity = first_value(merged, ["amount", "quantity", "qty", "model_quantity"])
    product_id = first_value(merged, ["itemid", "item_id", "product_id", "shopid", "modelid"])
    return {
        "nama_produk": product_name(merged),
        "harga": format_money(price),
        "quantity": quantity,
        "product_id": str(product_id) if product_id not in (None, "") else None,
    }


def summarize_tracking(order: dict) -> dict:
    tracking = first_value(
        order,
        ["tracking_info", "tracking", "logistics", "shipping", "shipment", "package_tracking_info"],
    )
    if not isinstance(tracking, dict):
        tracking = {}
    latest = deep_first_value(
        tracking,
        {"latest_status", "current_status", "tracking_status", "description", "status_desc", "text"},
    )
    awb = deep_first_value(order, {"tracking_number", "tracking_no", "shipping_traceno", "awb", "resi"})
    courier = deep_first_value(order, {"shipping_carrier", "logistics_channel", "courier", "carrier", "channel_name"})
    eta = deep_first_value(order, {"eta", "estimated_delivery_time", "estimated_delivery_date", "delivery_time"})
    return {
        "available": bool(tracking or latest or awb or courier or eta),
        "current_status": stringify_status(latest),
        "awb": stringify_status(awb),
        "kurir": stringify_status(courier),
        "eta": stringify_status(eta),
    }


def order_identity(order: dict) -> str:
    identity = first_value(
        order,
        ["order_sn", "ordersn", "order_id", "orderid", "checkout_id", "checkoutid"],
    )
    if identity not in (None, ""):
        return str(identity)
    return compact_payload(order, max_chars=200)


def order_status(order: dict) -> str:
    value = first_value(
        order,
        ["status_text", "status", "order_status", "order_status_text", "shipping_status", "status_label"],
    )
    if value in (None, ""):
        value = deep_first_value(order, {"status_text", "order_status_text", "status_label", "title", "text"})
    return stringify_status(value) or "-"


def order_date(order: dict) -> str | None:
    value = first_value(
        order,
        ["create_time", "ctime", "order_time", "pay_time", "payment_time", "date", "created_at"],
    )
    if isinstance(value, (int, float)):
        return str(int(value))
    return stringify_status(value)


def is_active_status(status: str) -> bool:
    lowered = status.lower()
    return not any(hint in lowered for hint in COMPLETED_STATUS_HINTS)


def date_in_filter(order: dict, date_from: str | None, date_to: str | None) -> bool:
    if not date_from and not date_to:
        return True
    value = order.get("tanggal_pesanan") or ""
    if not value:
        return True
    if date_from and value < date_from:
        return False
    if date_to and value > date_to:
        return False
    return True


def extract_simple_order(order: dict) -> dict:
    products = [format_product(product) for product in collect_product_dicts(order)]
    products = [product for product in products if product.get("nama_produk")]
    if not products:
        name = deep_first_value(order, {"item_name", "product_name", "name", "title"})
        if name:
            products = [{"nama_produk": normalize_text(name), "harga": None, "quantity": None, "product_id": None}]

    total = first_value(
        order,
        ["total_amount", "total_price", "amount", "final_total", "order_total", "payment_amount"],
    )
    shop_name = deep_first_value(order, {"shop_name", "seller_name", "username"})
    status = order_status(order)
    return {
        "order_id": order_identity(order),
        "tanggal_pesanan": order_date(order),
        "shop_name": stringify_status(shop_name),
        "produk": products,
        "total_harga": format_money(total),
        "status_pesanan": {"display": status},
        "tracking": summarize_tracking(order),
    }


def build_simple_orders(captured: list[dict], include_completed: bool = False) -> list[dict]:
    simple_orders = []
    seen = set()
    for item in captured:
        for order in find_order_objects(item.get("payload")):
            simple = extract_simple_order(order)
            if not simple.get("produk"):
                continue
            identity = simple["order_id"]
            if identity in seen:
                continue
            seen.add(identity)
            if not include_completed and not is_active_status(simple["status_pesanan"]["display"]):
                continue
            simple_orders.append(simple)
    return simple_orders


def format_eta(tracking: dict) -> str:
    return str(tracking.get("eta") or "-")


def format_latest_shipping_status(order: dict) -> str:
    tracking = order.get("tracking") or {}
    return str(
        tracking.get("current_status")
        or (order.get("status_pesanan") or {}).get("display")
        or "-"
    )


def build_orders_report(simple_orders: list[dict]) -> str:
    if not simple_orders:
        return "Tidak ada pesanan aktif.\n"
    lines = [f"Pesanan: {len(simple_orders)}", ""]
    for index, order in enumerate(simple_orders, start=1):
        product_names = [
            product.get("nama_produk")
            for product in order.get("produk") or []
            if isinstance(product, dict) and product.get("nama_produk")
        ]
        lines.append(f"{index}. {product_names[0] if product_names else '-'}")
        for product_name in product_names[1:]:
            lines.append(f"   + {product_name}")
        lines.append(f"   Status terakhir: {format_latest_shipping_status(order)}")
        lines.append(f"   ETA: {format_eta(order.get('tracking') or {})}")
        lines.append(f"   Total harga: {order.get('total_harga') or '-'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_orders_message(simple_orders: list[dict]) -> str:
    if not simple_orders:
        return "Tidak ada pesanan aktif."
    lines = [f"Update pesanan Shopee ({len(simple_orders)}):"]
    for index, order in enumerate(simple_orders, start=1):
        product_names = [
            product.get("nama_produk")
            for product in order.get("produk") or []
            if isinstance(product, dict) and product.get("nama_produk")
        ]
        product_label = "; ".join(product_names) if product_names else "-"
        lines.append(
            (
                f"{index}. {product_label}\n"
                f"Status: {format_latest_shipping_status(order)}\n"
                f"ETA: {format_eta(order.get('tracking') or {})}\n"
                f"Total: {order.get('total_harga') or '-'}"
            )
        )
    return "\n\n".join(lines).rstrip()


def load_simple_orders(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def filter_simple_orders(simple_orders: list[dict], args: argparse.Namespace) -> list[dict]:
    filtered = simple_orders
    if not args.all_orders:
        filtered = [
            order
            for order in filtered
            if is_active_status((order.get("status_pesanan") or {}).get("display") or "")
        ]
    return [order for order in filtered if date_in_filter(order, args.date_from, args.date_to)]


def append_order_request(request, order_requests: list[dict]) -> None:
    if request.resource_type not in {"fetch", "xhr"}:
        return
    if not url_looks_relevant(request.url):
        return
    lowered = request.url.lower()
    if not any(word in lowered for word in ["order", "purchase"]):
        return
    order_requests.append(
        {
            "url": request.url,
            "method": request.method,
            "post_data": request.post_data,
        }
    )


async def capture_response(
    response: Response,
    captured: list[dict],
    network_log: list[dict],
    order_requests: list[dict],
) -> None:
    request = response.request
    if request.resource_type not in {"fetch", "xhr"}:
        return
    append_order_request(request, order_requests)
    content_type = (await response.header_value("content-type") or "").lower()
    network_log.append(
        {
            "type": "response",
            "url": response.url,
            "status": response.status,
            "method": request.method,
            "resource_type": request.resource_type,
            "content_type": content_type,
            "post_data": compact_text(request.post_data),
        }
    )
    if not url_looks_relevant(response.url):
        return
    if "json" not in content_type and "api/" not in response.url.lower():
        return
    try:
        payload = await response.json()
    except (Error, ValueError):
        return
    if not payload_looks_relevant(payload):
        return
    captured.append(
        {
            "url": response.url,
            "status": response.status,
            "method": request.method,
            "resource_type": request.resource_type,
            "payload": payload,
        }
    )


def capture_request_failure(request, network_log: list[dict], order_requests: list[dict]) -> None:
    if request.resource_type not in {"fetch", "xhr"}:
        return
    network_log.append(
        {
            "type": "request_failed",
            "url": request.url,
            "method": request.method,
            "resource_type": request.resource_type,
            "failure": request.failure,
            "post_data": compact_text(request.post_data),
        }
    )
    append_order_request(request, order_requests)


def mutate_query_url(url: str, page_index: int, page_limit: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    offset = page_index * page_limit
    for key in ["limit", "page_size", "count"]:
        if key in query:
            query[key] = str(page_limit)
    for key in ["offset", "start", "cursor"]:
        if key in query:
            query[key] = str(offset)
    if "page" in query:
        query["page"] = str(page_index + 1)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def mutate_post_data(post_data: str | None, page_index: int, page_limit: int) -> object | str | None:
    if not post_data:
        return post_data
    try:
        payload = json.loads(post_data)
    except ValueError:
        return post_data
    mutated = deepcopy(payload)
    targets = mutated if isinstance(mutated, list) else [mutated]
    for target in targets:
        if not isinstance(target, dict):
            continue
        offset = page_index * page_limit
        for key in ["limit", "page_size", "count"]:
            if key in target:
                target[key] = page_limit
        for key in ["offset", "start", "cursor"]:
            if key in target:
                target[key] = offset
        if "page" in target:
            target["page"] = page_index + 1
    return mutated


async def replay_order_requests(
    api_request,
    order_requests: list[dict],
    captured: list[dict],
    network_log: list[dict],
    args: argparse.Namespace,
) -> None:
    if not (args.all_orders or args.date_from or args.date_to):
        return
    if args.page_limit < 1:
        raise RuntimeError("--page-limit must be at least 1.")
    if args.max_pages < 1:
        raise RuntimeError("--max-pages must be at least 1.")
    if not order_requests:
        return

    seen = set()
    templates = []
    for request in order_requests:
        key = (request["method"], request["url"], request.get("post_data"))
        if key not in seen:
            seen.add(key)
            templates.append(request)

    for template in templates[:3]:
        for page_index in range(args.max_pages):
            url = mutate_query_url(template["url"], page_index, args.page_limit)
            data = mutate_post_data(template.get("post_data"), page_index, args.page_limit)
            try:
                if template["method"].upper() == "POST":
                    response = await api_request.post(
                        url,
                        data=data,
                        headers={
                            "accept": "application/json",
                            "content-type": "application/json",
                            "origin": "https://shopee.co.id",
                            "referer": "https://shopee.co.id/user/purchase/",
                        },
                        timeout=15000,
                    )
                else:
                    response = await api_request.get(
                        url,
                        headers={
                            "accept": "application/json",
                            "referer": "https://shopee.co.id/user/purchase/",
                        },
                        timeout=15000,
                    )
                text = await response.text()
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
                network_log.append(
                    {
                        "type": "direct_order_page",
                        "url": url,
                        "page": page_index + 1,
                        "status": response.status,
                        "ok": response.ok,
                        "content_type": response.headers.get("content-type", ""),
                        "text_preview": text[:2000],
                    }
                )
                if payload is None or not payload_looks_relevant(payload):
                    break
                before = len(captured)
                captured.append(
                    {
                        "url": url,
                        "status": response.status,
                        "method": template["method"],
                        "resource_type": "api_request_context",
                        "payload": payload,
                    }
                )
                if len(find_order_objects(payload)) < args.page_limit or len(captured) == before:
                    break
            except Exception as exc:
                network_log.append(
                    {
                        "type": "direct_order_page_error",
                        "url": url,
                        "failure": str(exc).splitlines()[0],
                    }
                )
                break


async def check_orders(
    args: argparse.Namespace,
) -> tuple[str, str, list[dict], list[dict], str, str, Path, Path, Path, Path, Path, Path, Path, int]:
    state_path = Path(args.state).expanduser().resolve()
    if not state_path.exists():
        raise RuntimeError(f"Storage state not found at {state_path}. Login first with login_shopee.py.")

    output_path = Path(args.output).expanduser().resolve()
    api_dump_path = Path(args.api_dump).expanduser().resolve()
    network_log_path = Path(args.network_log).expanduser().resolve()
    simple_output_path = Path(args.simple_output).expanduser().resolve()
    report_output_path = Path(args.report_output).expanduser().resolve()
    message_output_path = Path(args.message_output).expanduser().resolve()
    screenshot_path = Path(args.screenshot).expanduser().resolve()
    for path in [
        output_path,
        api_dump_path,
        network_log_path,
        simple_output_path,
        report_output_path,
        message_output_path,
        screenshot_path,
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []
    network_log: list[dict] = []
    order_requests: list[dict] = []
    response_tasks: set[asyncio.Task] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not args.headful,
            executable_path=os.environ.get("CHROMIUM_EXECUTABLE_PATH")
            or os.environ.get("PUPPETEER_EXECUTABLE_PATH"),
            args=chromium_args(args),
        )
        try:
            context = await browser.new_context(
                storage_state=str(state_path),
                locale="id-ID",
                timezone_id="Asia/Jakarta",
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1366, "height": 768},
            )
            context.set_default_timeout(args.timeout)
            page = await context.new_page()

            def on_response(response: Response) -> None:
                task = asyncio.create_task(capture_response(response, captured, network_log, order_requests))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            page.on("response", on_response)
            page.on("requestfailed", lambda request: capture_request_failure(request, network_log, order_requests))
            final_url = await goto_first_available(page)
            status = await wait_for_api_capture(page, captured)
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)
            await replay_order_requests(context.request, order_requests, captured, network_log, args)
            if captured:
                status = "api_loaded"
            await page.screenshot(path=str(screenshot_path), full_page=True)

            orders = summarize_api_payloads(captured)
            simple_orders = filter_simple_orders(build_simple_orders(captured, include_completed=True), args)
            report = build_orders_report(simple_orders)
            message = build_orders_message(simple_orders)
            output = {
                "url": final_url,
                "status": status,
                "api_response_count": len(captured),
                "orders": orders,
            }
            output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            api_dump_path.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
            network_log_path.write_text(json.dumps(network_log, ensure_ascii=False, indent=2), encoding="utf-8")
            simple_output_path.write_text(json.dumps(simple_orders, ensure_ascii=False, indent=2), encoding="utf-8")
            report_output_path.write_text(report, encoding="utf-8")
            message_output_path.write_text(message + "\n", encoding="utf-8")
        finally:
            await browser.close()

    return (
        final_url,
        status,
        orders,
        simple_orders,
        report,
        message,
        output_path,
        api_dump_path,
        network_log_path,
        simple_output_path,
        report_output_path,
        message_output_path,
        screenshot_path,
        len(network_log),
    )


def main() -> int:
    args = parse_args()
    report_output_path = Path(args.report_output).expanduser().resolve()
    message_output_path = Path(args.message_output).expanduser().resolve()
    simple_output_path = Path(args.simple_output).expanduser().resolve()
    if (args.print_report or args.print_message) and not args.refresh:
        cached_simple_orders = load_simple_orders(simple_output_path)
        if cached_simple_orders is not None:
            cached_simple_orders = filter_simple_orders(cached_simple_orders, args)
            report = build_orders_report(cached_simple_orders)
            message = build_orders_message(cached_simple_orders)
            report_output_path.parent.mkdir(parents=True, exist_ok=True)
            message_output_path.parent.mkdir(parents=True, exist_ok=True)
            report_output_path.write_text(report, encoding="utf-8")
            message_output_path.write_text(message + "\n", encoding="utf-8")
            print(message if args.print_message else report, end="")
            if args.print_message:
                print()
            return 0
        if args.print_message and message_output_path.exists():
            print(message_output_path.read_text(encoding="utf-8"), end="")
            return 0
        if args.print_report and report_output_path.exists():
            print(report_output_path.read_text(encoding="utf-8"), end="")
            return 0

    try:
        (
            final_url,
            status,
            orders,
            simple_orders,
            report,
            message,
            output_path,
            api_dump_path,
            network_log_path,
            simple_output_path,
            report_output_path,
            message_output_path,
            screenshot_path,
            network_log_count,
        ) = asyncio.run(check_orders(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Check orders failed: {exc}", file=sys.stderr)
        return 1

    if args.print_report:
        print(report, end="")
        return 0
    if args.print_message:
        print(message)
        return 0

    print(f"Opened: {final_url}")
    print(f"Status: {status}")
    print(f"Fetch/XHR network entries: {network_log_count}")
    print(f"API responses matched: {len(orders)} summaries")
    print(f"Simple orders: {len(simple_orders)}")
    print(f"Saved parsed API summary: {output_path}")
    print(f"Saved raw API dump: {api_dump_path}")
    print(f"Saved fetch/xhr network log: {network_log_path}")
    print(f"Saved simple orders: {simple_output_path}")
    print(f"Saved report: {report_output_path}")
    print(f"Saved message: {message_output_path}")
    print(f"Saved screenshot: {screenshot_path}")
    if not orders:
        print("No order API payload found.")
        return 0
    for index, order in enumerate(orders[:10], start=1):
        print(f"API summary {index}:")
        print(f"Source: {order['source_url']}")
        print(f"Status: {order['status']}")
        print(order["preview"][:800])
        if len(order["preview"]) > 800:
            print("...")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
