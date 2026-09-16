import os
import time
import json
from decimal import Decimal, InvalidOperation
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://app.agendapro.com/sign_in"
AGENDAPRO_USER = os.environ["AGENDAPRO_USER"]
AGENDAPRO_PASSWORD = os.environ["AGENDAPRO_PASSWORD"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]

ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
UTC = timezone.utc
ROLLING_DAYS = 14
PER_PAGE = 100

LOCATIONS = [
    311450,
    441684,
    438946,
    433116,
    297091,
    433118,
    355874,
    182223,
    352403,
    352405,
    42969,
    25626,
    80886,
    106380,
    297090,
]



SALE_COMPARE_FIELDS = [
    "internal_id", "payment_id", "cart_id", "status", "paid_at_source",
    "business_date", "total_amount", "paid_amount", "pending_amount",
    "giftcard_amount", "sale_type", "client_id", "client_first_name",
    "client_last_name", "client_email", "client_identification_number",
    "location_id", "location_name", "document_status", "document_url", "note",
]

ITEM_COMPARE_FIELDS = [
    "booking_id", "source_item_id", "sale_id", "payment_id", "receipt_id",
    "client_id", "service_id", "service_name", "service_provider_id",
    "service_provider_name", "session_number", "price", "list_price",
    "discount", "item_type", "product_name", "quantity", "seller_id",
    "seller_type", "item_name", "subtotal", "total",
]

TRANSACTION_COMPARE_FIELDS = [
    "transaction_id", "sale_id", "sale_internal_id", "paid_at_source",
    "business_date", "amount", "tip", "external_reference",
    "payment_method_name", "payment_method_key", "payment_method_type",
    "transaction_type", "installments", "settled_status",
]

ITEM_KEYS = ITEM_COMPARE_FIELDS + ["synced_at"]
CANCELLED_STATUSES = {"cancelled", "canceled", "void", "voided", "refunded"}

COOKIES_CLAVE = {
    "ap_cognito_authorization",
    "_agendapro_session",
    "cognito_refresh_token",
    "cognito_last_auth",
}


def log(msg):
    print(msg, flush=True)


def num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except Exception:
        return None


def now_utc_iso():
    return datetime.now(UTC).isoformat()


def business_date_from_utc(value):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(ARG_TZ).date().isoformat()


def supabase_headers(prefer=None):
    h = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def supabase_insert(table, payload, return_representation=True):
    prefer = "return=representation" if return_representation else "return=minimal"
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=supabase_headers(prefer),
        json=payload,
        timeout=90,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase INSERT {table}: HTTP {r.status_code} - {r.text[:2000]}")
    return r.json() if return_representation else None


def supabase_upsert(table, rows, on_conflict):
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        json=rows,
        timeout=180,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase UPSERT {table}: HTTP {r.status_code} - {r.text[:3000]}")


def supabase_update(table, filters, values):
    qs = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        headers=supabase_headers("return=minimal"),
        json=values,
        timeout=90,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Supabase UPDATE {table}: HTTP {r.status_code} - {r.text[:2000]}")


def supabase_select(table, params=None):
    q = {"select": "*"}
    if params:
        q.update(params)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=supabase_headers(),
        params=q,
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Supabase SELECT {table}: HTTP {r.status_code} - {r.text[:2000]}")
    return r.json()


def supabase_delete_by_sale_ids(table, sale_ids):
    ids = sorted({int(x) for x in sale_ids if x is not None})
    if not ids:
        return
    for pos in range(0, len(ids), 100):
        batch = ids[pos:pos + 100]
        r = requests.delete(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supabase_headers("return=minimal"),
            params={"sale_id": f"in.({','.join(str(x) for x in batch)})"},
            timeout=120,
        )
        if r.status_code not in (200, 204):
            raise RuntimeError(f"Supabase DELETE {table}: HTTP {r.status_code} - {r.text[:2000]}")


def supabase_insert_rows(table, rows):
    if not rows:
        return
    for pos in range(0, len(rows), 250):
        supabase_insert(table, rows[pos:pos + 250], return_representation=False)


def supabase_rows_by_sale_ids(table, sale_ids):
    ids = sorted({int(x) for x in sale_ids if x is not None})
    out = []
    for pos in range(0, len(ids), 100):
        batch = ids[pos:pos + 100]
        out.extend(supabase_select(
            table,
            {"sale_id": f"in.({','.join(str(x) for x in batch)})"}
        ))
    return out


def normalize_number(value):
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
        if d == 0:
            return "0"
        return format(d.normalize(), "f")
    except (InvalidOperation, ValueError, TypeError):
        return value


def canonical_row(row, fields):
    numeric_fields = {
        "total_amount", "paid_amount", "pending_amount", "giftcard_amount",
        "price", "list_price", "discount", "quantity", "subtotal", "total",
        "amount", "tip",
    }
    result = {}
    for field in fields:
        value = row.get(field)
        if field in numeric_fields:
            value = normalize_number(value)
        result[field] = value
    return result


def canonical_rows(rows, fields):
    values = [canonical_row(r, fields) for r in rows]
    return sorted(values, key=lambda x: json.dumps(x, sort_keys=True, default=str, ensure_ascii=False))


def log_change(sync_run_id, sale_id, location_id, business_date, change_type, old_value, new_value):
    supabase_insert(
        "agenda_change_log",
        {
            "sync_run_id": sync_run_id,
            "sale_id": sale_id,
            "location_id": location_id,
            "business_date": business_date,
            "change_type": change_type,
            "old_value": old_value,
            "new_value": new_value,
        },
        return_representation=False,
    )


def is_cancelled_status(status):
    return (status or "").strip().lower() in CANCELLED_STATUSES


def login_agendapro():
    log("Abriendo AgendaPro...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.get_by_placeholder("user@example.com").fill(AGENDAPRO_USER)
            page.get_by_placeholder("Enter your password").fill(AGENDAPRO_PASSWORD)
            page.get_by_role("button", name="Log in").click()

            deadline = time.time() + 60
            found = {}
            while time.time() < deadline:
                cookies = context.cookies()
                found = {
                    c["name"]: c["value"]
                    for c in cookies
                    if c["name"] in COOKIES_CLAVE
                }
                if "ap_cognito_authorization" in found:
                    break
                page.wait_for_timeout(1000)

            log(f"URL final login: {page.url}")
            log("Cookies obtenidas: " + ", ".join(sorted(found.keys())))
            if "ap_cognito_authorization" not in found:
                raise RuntimeError("No se obtuvo ap_cognito_authorization")
            return "; ".join(f"{k}={v}" for k, v in found.items())
        finally:
            browser.close()


def agenda_get(url, cookie_header):
    r = requests.get(
        url,
        headers={
            "Cookie": cookie_header,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"AgendaPro HTTP {r.status_code}: {r.text[:2000]}")
    return r.json()


def fetch_v1_payments(cookie_header, location_id, day):
    day_dmy = day.strftime("%d-%m-%Y")
    all_rows = []
    page = 1
    while True:
        url = (
            "https://agendapro.com/api/views/admin/v1/payments"
            f"?from={day_dmy}&to={day_dmy}&per_page={PER_PAGE}"
            f"&location_ids={location_id}&page={page}"
        )
        data = agenda_get(url, cookie_header)
        rows = data.get("payments") or []
        all_rows.extend(rows)
        pages = int(data.get("pages") or 1)
        if page >= pages:
            break
        page += 1
    return all_rows


def fetch_v2_sales(cookie_header, location_id, day):
    date_iso = day.strftime("%Y-%m-%d")
    all_rows = []
    page = 1
    while True:
        url = (
            "https://agendapro.com/api/views/admin/v2/sales/sale"
            f"?per_page={PER_PAGE}&page={page}"
            f"&end_date={date_iso}T23:59:59-03:00"
            f"&start_date={date_iso}T00:00:00-03:00"
            f"&location_id[]={location_id}"
        )
        data = agenda_get(url, cookie_header)
        rows = data.get("data") or []
        all_rows.extend(rows)
        pages = int((data.get("pagination") or {}).get("total_pages") or 1)
        if page >= pages:
            break
        page += 1
    return all_rows


def fetch_v2_transactions(cookie_header, location_id, day):
    date_iso = day.strftime("%Y-%m-%d")
    all_rows = []
    page = 1
    while True:
        url = (
            "https://agendapro.com/api/views/admin/v2/sales/transaction"
            f"?end_date={date_iso}T23:59:59-03:00"
            f"&start_date={date_iso}T00:00:00-03:00"
            f"&location_id[]={location_id}"
            "&sale_id=&external_reference="
            f"&page={page}&per_page={PER_PAGE}"
        )
        data = agenda_get(url, cookie_header)
        rows = data.get("data") or []
        all_rows.extend(rows)
        pages = int((data.get("pagination") or {}).get("total_pages") or 1)
        if page >= pages:
            break
        page += 1
    return all_rows


def fetch_v2_sale_detail(cookie_header, sale_id):
    url = f"https://agendapro.com/api/views/admin/v2/sales/sale/{sale_id}"
    r = requests.get(
        url,
        headers={
            "Cookie": cookie_header,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=120,
    )
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(
            f"AgendaPro detalle sale {sale_id}: HTTP {r.status_code}: {r.text[:2000]}"
        )
    return r.json()


def build_detail_sale(detail):
    return {
        "sale_id": int(detail["id"]),
        "internal_id": detail.get("internal_id"),
        "payment_id": None,
        "cart_id": detail.get("cart_id"),
        "status": detail.get("status"),
        "paid_at_source": detail.get("paid_at"),
        "business_date": business_date_from_utc(detail.get("paid_at")),
        "total_amount": num(detail.get("total_amount")),
        "paid_amount": num(detail.get("paid_amount")),
        "pending_amount": num(detail.get("pending_amount")),
        "giftcard_amount": num(detail.get("giftcard_amount")) or 0,
        "sale_type": "giftcard_sale" if any(
            (i.get("item_type") or "").lower() == "giftcard"
            for i in (detail.get("items") or [])
        ) else "special_sale",
        "client_id": detail.get("client_id"),
        "client_first_name": detail.get("client_first_name"),
        "client_last_name": detail.get("client_last_name"),
        "client_email": detail.get("client_email"),
        "client_identification_number": None,
        "location_id": detail.get("location_id"),
        "location_name": detail.get("location_name"),
        "document_status": None,
        "document_url": None,
        "note": detail.get("note"),
        "synced_at": now_utc_iso(),
    }


def build_detail_items(detail):
    rows = []
    sale_id = int(detail["id"])
    for idx, item in enumerate(detail.get("items") or []):
        item_type = (item.get("item_type") or "special").lower()
        # AgendaPro no entrega ID del item en este endpoint.
        # Generamos un ID tecnico deterministico negativo, estable por venta y posicion.
        source_item_id = -(sale_id * 100 + idx + 1)
        rows.append({
            "booking_id": None,
            "source_item_id": source_item_id,
            "sale_id": sale_id,
            "payment_id": None,
            "receipt_id": None,
            "client_id": detail.get("client_id"),
            "service_id": None,
            "service_name": None,
            "service_provider_id": None,
            "service_provider_name": None,
            "session_number": None,
            "price": num(item.get("total")),
            "list_price": num(item.get("subtotal")),
            "discount": None,
            "item_type": item_type,
            "product_name": None,
            "quantity": num(item.get("quantity")) or 1,
            "seller_id": None,
            "seller_type": None,
            "item_name": item.get("name"),
            "subtotal": num(item.get("subtotal")),
            "total": num(item.get("total")),
            "synced_at": now_utc_iso(),
        })
    return rows


def build_sales(sales, payments):
    payment_by_id = {
        int(p["id"]): p
        for p in payments
        if p.get("id") is not None
    }
    out = []
    missing = []
    for sale in sales:
        pid = sale.get("payment_id")
        payment = payment_by_id.get(int(pid)) if pid is not None else None
        if payment is None:
            missing.append(pid)
        client = (payment or {}).get("client") or {}
        out.append({
            "sale_id": int(sale["id"]),
            "internal_id": sale.get("internal_id"),
            "payment_id": pid,
            "cart_id": sale.get("cart_id"),
            "status": sale.get("status"),
            "paid_at_source": sale.get("paid_at"),
            "business_date": business_date_from_utc(sale.get("paid_at")),
            "total_amount": num(sale.get("total_amount")),
            "paid_amount": num(sale.get("paid_amount")),
            "pending_amount": num(sale.get("pending_amount")),
            "giftcard_amount": num(sale.get("giftcard_amount")) or 0,
            "sale_type": "regular_sale",
            "client_id": client.get("id"),
            "client_first_name": client.get("first_name") or sale.get("client_first_name"),
            "client_last_name": client.get("last_name") or sale.get("client_last_name"),
            "client_email": client.get("email") or sale.get("client_email"),
            "client_identification_number": client.get("identification_number"),
            "location_id": sale.get("location_id"),
            "location_name": sale.get("location_name"),
            "document_status": sale.get("document_status"),
            "document_url": sale.get("document_url"),
            "note": sale.get("note"),
            "synced_at": now_utc_iso(),
        })
    return out, missing


def build_items(sales, payments):
    sale_by_payment = {
        int(s["payment_id"]): s
        for s in sales
        if s.get("payment_id") is not None
    }
    rows = []
    payments_without_sale = []
    memberships = 0
    giftcards = 0

    for payment in payments:
        payment_id = int(payment["id"])
        sale = sale_by_payment.get(payment_id)
        if not sale:
            payments_without_sale.append(payment_id)
            continue
        sale_id = int(sale["id"])
        client_id = (payment.get("client") or {}).get("id")

        receipt_booking_info = {}
        receipt_mock_info = {}
        receipt_product_info = {}

        for receipt in payment.get("receipts") or []:
            receipt_id = receipt.get("id")

            for booking in receipt.get("bookings") or []:
                bid = booking.get("id")
                if bid is None:
                    continue
                receipt_booking_info[int(bid)] = {
                    "receipt_id": receipt_id,
                    "client_id": booking.get("client_id"),
                    "price": booking.get("price"),
                    "list_price": booking.get("list_price"),
                    "discount": booking.get("discount"),
                    "service": booking.get("service") or {},
                }

            for mock in receipt.get("mock_bookings") or []:
                mid = mock.get("id")
                if mid is None:
                    continue
                receipt_mock_info[int(mid)] = {
                    "receipt_id": receipt_id,
                    "client_id": mock.get("client_id"),
                    "price": mock.get("price"),
                    "list_price": mock.get("list_price"),
                    "discount": mock.get("discount"),
                    "service": mock.get("service") or {},
                    "service_provider": mock.get("service_provider") or {},
                }

            for product in receipt.get("payment_products") or []:
                pid = product.get("id")
                if pid is None:
                    continue
                receipt_product_info[int(pid)] = {
                    "receipt_id": receipt_id,
                    **product,
                }

            memberships += len(receipt.get("payment_memberships") or [])
            giftcards += len(receipt.get("payment_giftcards") or [])

        for booking in payment.get("bookings") or []:
            bid = booking.get("id")
            if bid is None:
                continue
            bid = int(bid)
            r = receipt_booking_info.get(bid, {})
            service = booking.get("service") or r.get("service") or {}
            provider = booking.get("service_provider") or {}
            rows.append({
                "booking_id": bid,
                "source_item_id": bid,
                "sale_id": sale_id,
                "payment_id": payment_id,
                "receipt_id": r.get("receipt_id"),
                "client_id": r.get("client_id") or client_id,
                "service_id": service.get("id"),
                "service_name": service.get("name"),
                "service_provider_id": provider.get("id"),
                "service_provider_name": provider.get("public_name"),
                "session_number": booking.get("session_number"),
                "price": num(booking.get("price") if booking.get("price") is not None else r.get("price")),
                "list_price": num(r.get("list_price")),
                "discount": num(r.get("discount")),
                "item_type": "booking",
                "product_name": None,
                "quantity": None,
                "seller_id": None,
                "seller_type": None,
                "synced_at": now_utc_iso(),
            })

        for mock in payment.get("mock_bookings") or []:
            mid = mock.get("id")
            if mid is None:
                continue
            mid = int(mid)
            r = receipt_mock_info.get(mid, {})
            service = mock.get("service") or r.get("service") or {}
            provider = mock.get("service_provider") or r.get("service_provider") or {}
            rows.append({
                "booking_id": None,
                "source_item_id": mid,
                "sale_id": sale_id,
                "payment_id": payment_id,
                "receipt_id": r.get("receipt_id") or mock.get("receipt_id"),
                "client_id": mock.get("client_id") or r.get("client_id") or client_id,
                "service_id": service.get("id") or mock.get("service_id"),
                "service_name": service.get("name"),
                "service_provider_id": provider.get("id") or mock.get("service_provider_id"),
                "service_provider_name": provider.get("public_name"),
                "session_number": None,
                "price": num(mock.get("price") if mock.get("price") is not None else r.get("price")),
                "list_price": num(mock.get("list_price") if mock.get("list_price") is not None else r.get("list_price")),
                "discount": num(mock.get("discount") if mock.get("discount") is not None else r.get("discount")),
                "item_type": "mock_booking",
                "product_name": None,
                "quantity": None,
                "seller_id": None,
                "seller_type": None,
                "synced_at": now_utc_iso(),
            })

        products = payment.get("products") or []
        if not products and receipt_product_info:
            products = list(receipt_product_info.values())

        for product in products:
            pid = product.get("id")
            if pid is None:
                continue
            pid = int(pid)
            r = receipt_product_info.get(pid, {})
            rows.append({
                "booking_id": None,
                "source_item_id": pid,
                "sale_id": sale_id,
                "payment_id": payment_id,
                "receipt_id": r.get("receipt_id"),
                "client_id": client_id,
                "service_id": None,
                "service_name": None,
                "service_provider_id": None,
                "service_provider_name": None,
                "session_number": None,
                "price": num(product.get("price") if product.get("price") is not None else product.get("price_product") or r.get("price")),
                "list_price": num(product.get("list_price") if product.get("list_price") is not None else product.get("list_price_product") or r.get("list_price")),
                "discount": num(product.get("discount") if product.get("discount") is not None else product.get("discount_product") or r.get("discount")),
                "item_type": "product",
                "product_name": product.get("product") or product.get("name") or r.get("product"),
                "quantity": num(product.get("quantity") if product.get("quantity") is not None else r.get("quantity")),
                "seller_id": product.get("seller_id") if product.get("seller_id") is not None else r.get("seller_id"),
                "seller_type": product.get("seller_type") or r.get("seller_type"),
                "synced_at": now_utc_iso(),
            })

    return rows, payments_without_sale, memberships, giftcards


def build_transactions(transactions):
    rows = []
    for t in transactions:
        rows.append({
            "transaction_id": int(t["id"]),
            "sale_id": int(t["sale_id"]),
            "sale_internal_id": t.get("sale_internal_id"),
            "paid_at_source": t.get("paid_at"),
            "business_date": business_date_from_utc(t.get("paid_at")),
            "amount": num(t.get("amount")),
            "tip": num(t.get("tip")) or 0,
            "external_reference": t.get("external_reference"),
            "payment_method_name": t.get("payment_method_name"),
            "payment_method_key": t.get("payment_method_internal_key"),
            "payment_method_type": t.get("payment_method_type"),
            "transaction_type": t.get("transaction_type"),
            "installments": t.get("installments"),
            "settled_status": t.get("settled_status"),
            "synced_at": now_utc_iso(),
        })
    return rows


def run_one_day(cookie_header, location_id, day, sync_run_id):
    payments = fetch_v1_payments(cookie_header, location_id, day)
    sales = fetch_v2_sales(cookie_header, location_id, day)
    transactions = fetch_v2_transactions(cookie_header, location_id, day)

    sale_rows, missing_payments = build_sales(sales, payments)
    item_rows, payments_without_sale, memberships, giftcards = build_items(sales, payments)
    transaction_rows = build_transactions(transactions)

    sale_ids = {r["sale_id"] for r in sale_rows}
    orphan_transactions = sorted({r["sale_id"] for r in transaction_rows} - sale_ids)

    resolved_special_sales = []
    unresolved_transactions = []
    special_item_rows = []

    for orphan_sale_id in orphan_transactions:
        detail = fetch_v2_sale_detail(cookie_header, orphan_sale_id)
        if not detail:
            unresolved_transactions.append(orphan_sale_id)
            continue

        detail_location = detail.get("location_id")
        if detail_location is not None and int(detail_location) != int(location_id):
            unresolved_transactions.append(orphan_sale_id)
            log(
                f"WARNING sale especial {orphan_sale_id}: local detalle={detail_location}, "
                f"local esperado={location_id}"
            )
            continue

        detail_sale = build_detail_sale(detail)
        sale_rows.append(detail_sale)
        detail_items = build_detail_items(detail)
        item_rows.extend(detail_items)
        special_item_rows.extend(detail_items)
        resolved_special_sales.append({
            "sale_id": orphan_sale_id,
            "sale_type": detail_sale["sale_type"],
            "items": len(detail_items),
        })

        log(
            f"Venta especial resuelta: sale_id={orphan_sale_id} "
            f"tipo={detail_sale['sale_type']} items={len(detail_items)}"
        )

    if unresolved_transactions:
        raise RuntimeError(
            f"Local {location_id} {day.isoformat()}: transacciones no resueltas "
            f"{unresolved_transactions[:20]}"
        )

    # PostgREST exige exactamente las mismas claves para todos los objetos del array.
    item_rows = [{key: row.get(key) for key in ITEM_KEYS} for row in item_rows]

    incoming_ids = {int(r["sale_id"]) for r in sale_rows}
    now_iso = now_utc_iso()

    # Estado previo de las ventas que llegaron hoy, aunque antes estuvieran en otra fecha.
    previous_sales = supabase_rows_by_sale_ids("agenda_sales", incoming_ids)
    previous_by_id = {int(r["sale_id"]): r for r in previous_sales}

    # Estado previo de la fecha/local para detectar ventas que dejaron de aparecer.
    existing_day_rows = supabase_select(
        "agenda_sales",
        {
            "location_id": f"eq.{location_id}",
            "business_date": f"eq.{day.isoformat()}",
        },
    )
    existing_active_day_ids = {
        int(r["sale_id"])
        for r in existing_day_rows
        if r.get("source_active") is not False
    }

    # Hijos previos: se usan para detectar cambios antes de reemplazarlos.
    previous_items = supabase_rows_by_sale_ids("agenda_sale_items", incoming_ids)
    previous_transactions = supabase_rows_by_sale_ids("agenda_payment_transactions", incoming_ids)

    prev_items_by_sale = {}
    for r in previous_items:
        prev_items_by_sale.setdefault(int(r["sale_id"]), []).append(r)
    prev_tx_by_sale = {}
    for r in previous_transactions:
        prev_tx_by_sale.setdefault(int(r["sale_id"]), []).append(r)

    new_items_by_sale = {}
    for r in item_rows:
        new_items_by_sale.setdefault(int(r["sale_id"]), []).append(r)
    new_tx_by_sale = {}
    for r in transaction_rows:
        new_tx_by_sale.setdefault(int(r["sale_id"]), []).append(r)

    changes_detected = 0

    # Auditoria de cabecera, items y transacciones.
    for row in sale_rows:
        sid = int(row["sale_id"])
        old = previous_by_id.get(sid)
        if old:
            old_sale = canonical_row(old, SALE_COMPARE_FIELDS)
            new_sale = canonical_row(row, SALE_COMPARE_FIELDS)
            if old_sale != new_sale:
                change_type = "sale_date_changed" if old.get("business_date") != row.get("business_date") else "sale_updated"
                log_change(
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    change_type, old_sale, new_sale,
                )
                changes_detected += 1

            old_items = canonical_rows(prev_items_by_sale.get(sid, []), ITEM_COMPARE_FIELDS)
            new_items = canonical_rows(new_items_by_sale.get(sid, []), ITEM_COMPARE_FIELDS)
            if old_items != new_items:
                log_change(
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    "items_changed", old_items, new_items,
                )
                changes_detected += 1

            old_tx = canonical_rows(prev_tx_by_sale.get(sid, []), TRANSACTION_COMPARE_FIELDS)
            new_tx = canonical_rows(new_tx_by_sale.get(sid, []), TRANSACTION_COMPARE_FIELDS)
            if old_tx != new_tx:
                log_change(
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    "transactions_changed", old_tx, new_tx,
                )
                changes_detected += 1

            if old.get("source_active") is False:
                log_change(
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    "sale_reactivated", {"source_active": False}, {"source_active": True},
                )
                changes_detected += 1

        row["source_active"] = not is_cancelled_status(row.get("status"))
        row["last_seen_at"] = now_iso
        row["source_missing_since"] = None

    # Reemplazo completo de hijos de cada venta que vino de AgendaPro.
    # Esto elimina servicios/transacciones viejos si fueron corregidos o borrados.
    supabase_upsert("agenda_sales", sale_rows, "sale_id")
    supabase_delete_by_sale_ids("agenda_sale_items", incoming_ids)
    supabase_delete_by_sale_ids("agenda_payment_transactions", incoming_ids)
    supabase_insert_rows("agenda_sale_items", item_rows)
    supabase_insert_rows("agenda_payment_transactions", transaction_rows)

    # Ventas que estaban activas en esta fecha/local y ya no aparecen en el listado actual.
    missing_ids = sorted(existing_active_day_ids - incoming_ids)
    missing_marked = 0
    missing_resolved = 0

    for sid in missing_ids:
        old = next((r for r in existing_day_rows if int(r["sale_id"]) == sid), None)
        detail = fetch_v2_sale_detail(cookie_header, sid)

        if detail:
            detail_sale = build_detail_sale(detail)
            detail_sale["source_active"] = not is_cancelled_status(detail_sale.get("status"))
            detail_sale["last_seen_at"] = now_iso
            detail_sale["source_missing_since"] = None

            old_date = old.get("business_date") if old else None
            new_date = detail_sale.get("business_date")
            old_status = old.get("status") if old else None
            new_status = detail_sale.get("status")

            if old_date != new_date:
                change_type = "sale_date_changed"
            elif is_cancelled_status(new_status):
                change_type = "sale_cancelled"
            else:
                change_type = "sale_updated"

            log_change(
                sync_run_id, sid, detail_sale.get("location_id") or location_id,
                new_date or day.isoformat(), change_type,
                canonical_row(old or {}, SALE_COMPARE_FIELDS),
                canonical_row(detail_sale, SALE_COMPARE_FIELDS),
            )
            supabase_upsert("agenda_sales", [detail_sale], "sale_id")
            changes_detected += 1
            missing_resolved += 1
            log(f"Venta ausente resuelta por detalle: sale_id={sid} tipo={change_type}")
        else:
            first_missing = (old or {}).get("source_missing_since") or now_iso
            supabase_update(
                "agenda_sales",
                {"sale_id": sid},
                {
                    "source_active": False,
                    "source_missing_since": first_missing,
                },
            )
            log_change(
                sync_run_id, sid, location_id, day.isoformat(), "sale_missing",
                canonical_row(old or {}, SALE_COMPARE_FIELDS),
                {"source_active": False, "source_missing_since": first_missing},
            )
            changes_detected += 1
            missing_marked += 1
            log(f"Venta ya no disponible en AgendaPro: sale_id={sid}")

    return {
        "payments": len(payments),
        "sales": len(sales) + len(resolved_special_sales),
        "regular_sales": len(sales),
        "special_sales": len(resolved_special_sales),
        "transactions": len(transactions),
        "items": len(item_rows),
        "missing_payments": len(missing_payments),
        "payments_without_sale": len(payments_without_sale),
        "orphan_transactions": len(orphan_transactions),
        "resolved_special_sales": len(resolved_special_sales),
        "memberships": memberships,
        "giftcards": giftcards + sum(
            1 for r in special_item_rows if r["item_type"] == "giftcard"
        ),
        "mock_bookings": sum(1 for r in item_rows if r["item_type"] == "mock_booking"),
        "products": sum(1 for r in item_rows if r["item_type"] == "product"),
        "changes_detected": changes_detected,
        "missing_marked": missing_marked,
        "missing_resolved": missing_resolved,
    }


def main():
    today_arg = datetime.now(ARG_TZ).date()
    start_day = today_arg - timedelta(days=ROLLING_DAYS - 1)
    end_day = today_arg

    run = supabase_insert(
        "agenda_sync_runs",
        {
            "location_id": None,
            "date_from": start_day.isoformat(),
            "date_to": end_day.isoformat(),
            "status": "running",
            "metadata": {
                "mode": "manual_multilocal_reconciliation",
                "source": "github-actions",
                "locations": LOCATIONS,
            },
        },
    )
    sync_run_id = run[0]["id"]

    totals = {
        "sales": 0,
        "items": 0,
        "transactions": 0,
        "mock_bookings": 0,
        "products": 0,
        "memberships": 0,
        "giftcards": 0,
        "special_sales": 0,
        "days_processed": 0,
        "warnings": [],
        "changes_detected": 0,
        "missing_marked": 0,
        "missing_resolved": 0,
    }

    try:
        cookie_header = login_agendapro()
        log("\n========================================")
        log("SYNC MANUAL AGENDA PRO -> NIKI OS")
        log(f"Desde: {start_day}")
        log(f"Hasta: {end_day}")
        log(f"Locales: {len(LOCATIONS)}")
        log("========================================\n")

        for location_id in LOCATIONS:
            log(f"--- Local {location_id} ---")
            day = start_day
            while day <= end_day:
                summary = run_one_day(cookie_header, location_id, day, sync_run_id)
                totals["sales"] += summary["sales"]
                totals["items"] += summary["items"]
                totals["transactions"] += summary["transactions"]
                totals["mock_bookings"] += summary["mock_bookings"]
                totals["products"] += summary["products"]
                totals["memberships"] += summary["memberships"]
                totals["giftcards"] += summary["giftcards"]
                totals["special_sales"] += summary["special_sales"]
                totals["days_processed"] += 1
                totals["changes_detected"] += summary["changes_detected"]
                totals["missing_marked"] += summary["missing_marked"]
                totals["missing_resolved"] += summary["missing_resolved"]

                warn_parts = []
                if summary["missing_payments"]:
                    warn_parts.append(f"sales_sin_payment={summary['missing_payments']}")
                if summary["payments_without_sale"]:
                    warn_parts.append(f"payments_sin_sale={summary['payments_without_sale']}")
                if summary["memberships"]:
                    warn_parts.append(f"memberships={summary['memberships']}")
                if summary["giftcards"]:
                    warn_parts.append(f"giftcards={summary['giftcards']}")
                if summary["special_sales"]:
                    warn_parts.append(f"ventas_especiales={summary['special_sales']}")
                if warn_parts:
                    totals["warnings"].append(
                        f"{location_id} {day.isoformat()}: " + ", ".join(warn_parts)
                    )

                log(
                    f"{day.isoformat()} | sales={summary['sales']} | items={summary['items']} "
                    f"| trans={summary['transactions']} | mock={summary['mock_bookings']} "
                    f"| products={summary['products']} | special={summary['special_sales']} "
                    f"| changes={summary['changes_detected']} | missing={summary['missing_marked']}"
                )
                day += timedelta(days=1)

        metadata = {
            "mode": "manual_multilocal_reconciliation",
            "source": "github-actions",
            "locations": LOCATIONS,
            "days_processed": totals["days_processed"],
            "mock_bookings": totals["mock_bookings"],
            "products": totals["products"],
            "memberships_detected_not_loaded": totals["memberships"],
            "giftcards_loaded_or_detected": totals["giftcards"],
            "special_sales_resolved": totals["special_sales"],
            "changes_detected": totals["changes_detected"],
            "sales_marked_missing": totals["missing_marked"],
            "missing_sales_resolved_by_detail": totals["missing_resolved"],
            "warnings": totals["warnings"][:100],
        }

        supabase_update(
            "agenda_sync_runs",
            {"id": sync_run_id},
            {
                "finished_at": now_utc_iso(),
                "status": "success",
                "sales_read": totals["sales"],
                "items_read": totals["items"],
                "transactions_read": totals["transactions"],
                "sales_upserted": totals["sales"],
                "items_upserted": totals["items"],
                "transactions_upserted": totals["transactions"],
                "metadata": metadata,
            },
        )

        log("\n========================================")
        log("SYNC MANUAL OK")
        log(f"Ventas:        {totals['sales']}")
        log(f"Items:         {totals['items']}")
        log(f"Transacciones: {totals['transactions']}")
        log(f"Mock bookings: {totals['mock_bookings']}")
        log(f"Productos:     {totals['products']}")
        log(f"Memberships detectados: {totals['memberships']}")
        log(f"Giftcards cargadas/detectadas: {totals['giftcards']}")
        log(f"Ventas especiales resueltas:  {totals['special_sales']}")
        log(f"Cambios detectados: {totals['changes_detected']}")
        log(f"Ventas marcadas ausentes: {totals['missing_marked']}")
        log(f"Ausentes resueltas por detalle: {totals['missing_resolved']}")
        log(f"Warnings:      {len(totals['warnings'])}")
        log("========================================")

    except Exception as exc:
        try:
            supabase_update(
                "agenda_sync_runs",
                {"id": sync_run_id},
                {
                    "finished_at": now_utc_iso(),
                    "status": "error",
                    "error_message": str(exc)[:3000],
                    "metadata": {
                        "mode": "manual_multilocal_reconciliation",
                        "source": "github-actions",
                        "locations": LOCATIONS,
                    },
                },
            )
        except Exception as log_exc:
            log(f"No se pudo registrar error en agenda_sync_runs: {log_exc}")
        raise


if __name__ == "__main__":
    main()
