import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright


# ==========================================================
# CONFIGURACION PILOTO
# ==========================================================

LOGIN_URL = "https://app.agendapro.com/sign_in"

AGENDAPRO_USER = os.environ["AGENDAPRO_USER"]
AGENDAPRO_PASSWORD = os.environ["AGENDAPRO_PASSWORD"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]

LOCATION_ID = 352403
LOCATION_NAME = "NIKI Beauty Bar Saavedra"

PILOT_DATE = "2026-09-12"
PILOT_DATE_DMY = "12-09-2026"

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

COOKIES_CLAVE = {
    "ap_cognito_authorization",
    "_agendapro_session",
    "cognito_refresh_token",
    "cognito_last_auth",
}


# ==========================================================
# HELPERS
# ==========================================================

def log(message):
    print(message, flush=True)


def numeric(value):
    if value is None or value == "":
        return None
    return float(value)


def business_date_from_utc(value):
    """
    AgendaPro v2 devuelve paid_at como UTC real.
    Ej:
      2026-09-12T23:01:00.000Z
    corresponde a:
      2026-09-12 20:01 Argentina
    """
    if not value:
        return None

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(ARGENTINA_TZ).date().isoformat()


def supabase_headers(prefer=None):
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    return headers


def supabase_insert(table, payload, return_representation=True):
    prefer = "return=representation" if return_representation else "return=minimal"

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=supabase_headers(prefer),
        json=payload,
        timeout=60,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Supabase INSERT {table}: "
            f"HTTP {response.status_code} - {response.text[:2000]}"
        )

    if return_representation:
        return response.json()

    return None


def supabase_upsert(table, rows, on_conflict):
    if not rows:
        return

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        headers=supabase_headers(
            "resolution=merge-duplicates,return=minimal"
        ),
        json=rows,
        timeout=120,
    )

    if response.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"Supabase UPSERT {table}: "
            f"HTTP {response.status_code} - {response.text[:3000]}"
        )


def supabase_update(table, filters, values):
    filter_text = "&".join(
        f"{field}=eq.{value}"
        for field, value in filters.items()
    )

    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{filter_text}",
        headers=supabase_headers("return=minimal"),
        json=values,
        timeout=60,
    )

    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Supabase UPDATE {table}: "
            f"HTTP {response.status_code} - {response.text[:2000]}"
        )


# ==========================================================
# LOGIN AGENDAPRO
# ==========================================================

def login_agendapro():
    log("Abriendo AgendaPro...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.get_by_placeholder(
                "user@example.com"
            ).fill(AGENDAPRO_USER)

            page.get_by_placeholder(
                "Enter your password"
            ).fill(AGENDAPRO_PASSWORD)

            page.get_by_role(
                "button",
                name="Log in"
            ).click()

            limite = time.time() + 60
            encontradas = {}

            while time.time() < limite:
                cookies = context.cookies()

                encontradas = {
                    c["name"]: c["value"]
                    for c in cookies
                    if c["name"] in COOKIES_CLAVE
                }

                if "ap_cognito_authorization" in encontradas:
                    break

                page.wait_for_timeout(1000)

            log(f"URL final login: {page.url}")
            log(
                "Cookies obtenidas: "
                + ", ".join(sorted(encontradas.keys()))
            )

            if "ap_cognito_authorization" not in encontradas:
                raise RuntimeError(
                    "No se obtuvo ap_cognito_authorization"
                )

            return "; ".join(
                f"{nombre}={valor}"
                for nombre, valor in encontradas.items()
            )

        finally:
            browser.close()


# ==========================================================
# CONSULTAS AGENDAPRO
# ==========================================================

def agenda_get(url, cookie_header):
    response = requests.get(
        url,
        headers={
            "Cookie": cookie_header,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=90,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"AgendaPro HTTP {response.status_code}: "
            f"{response.text[:2000]}"
        )

    return response.json()


def fetch_payments(cookie_header):
    all_rows = []
    page = 1

    while True:
        url = (
            "https://agendapro.com/api/views/admin/v1/payments"
            f"?from={PILOT_DATE_DMY}"
            f"&to={PILOT_DATE_DMY}"
            "&per_page=100"
            f"&location_ids={LOCATION_ID}"
            f"&page={page}"
        )

        data = agenda_get(url, cookie_header)
        rows = data.get("payments") or []

        all_rows.extend(rows)

        total_pages = int(data.get("pages") or 1)

        log(
            f"payments pagina {page}/{total_pages}: "
            f"{len(rows)} registros"
        )

        if page >= total_pages:
            break

        page += 1

    return all_rows


def fetch_sales(cookie_header):
    all_rows = []
    page = 1

    while True:
        url = (
            "https://agendapro.com/api/views/admin/v2/sales/sale"
            "?per_page=100"
            f"&page={page}"
            f"&end_date={PILOT_DATE}T23:59:59-03:00"
            f"&start_date={PILOT_DATE}T00:00:00-03:00"
            f"&location_id[]={LOCATION_ID}"
        )

        data = agenda_get(url, cookie_header)

        rows = data.get("data") or []
        pagination = data.get("pagination") or {}

        all_rows.extend(rows)

        total_pages = int(
            pagination.get("total_pages") or 1
        )

        log(
            f"sales pagina {page}/{total_pages}: "
            f"{len(rows)} registros"
        )

        if page >= total_pages:
            break

        page += 1

    return all_rows


def fetch_transactions(cookie_header):
    all_rows = []
    page = 1

    while True:
        url = (
            "https://agendapro.com/api/views/admin/v2/sales/transaction"
            f"?end_date={PILOT_DATE}T23:59:59-03:00"
            f"&start_date={PILOT_DATE}T00:00:00-03:00"
            f"&location_id[]={LOCATION_ID}"
            "&sale_id="
            "&external_reference="
            f"&page={page}"
            "&per_page=100"
        )

        data = agenda_get(url, cookie_header)

        rows = data.get("data") or []
        pagination = data.get("pagination") or {}

        all_rows.extend(rows)

        total_pages = int(
            pagination.get("total_pages") or 1
        )

        log(
            f"transactions pagina {page}/{total_pages}: "
            f"{len(rows)} registros"
        )

        if page >= total_pages:
            break

        page += 1

    return all_rows


# ==========================================================
# TRANSFORMACION
# ==========================================================

def build_sales(sales, payments):
    payment_by_id = {
        int(p["id"]): p
        for p in payments
        if p.get("id") is not None
    }

    rows = []
    missing_payments = []

    for sale in sales:
        payment_id = sale.get("payment_id")

        payment = (
            payment_by_id.get(int(payment_id))
            if payment_id is not None
            else None
        )

        if payment is None:
            missing_payments.append(payment_id)

        client = (
            (payment or {}).get("client")
            or {}
        )

        rows.append({
            "sale_id": int(sale["id"]),
            "internal_id": sale.get("internal_id"),
            "payment_id": payment_id,
            "cart_id": sale.get("cart_id"),
            "status": sale.get("status"),
            "paid_at_source": sale.get("paid_at"),
            "business_date": business_date_from_utc(
                sale.get("paid_at")
            ),
            "total_amount": numeric(
                sale.get("total_amount")
            ),
            "paid_amount": numeric(
                sale.get("paid_amount")
            ),
            "pending_amount": numeric(
                sale.get("pending_amount")
            ),
            "client_id": client.get("id"),
            "client_first_name": (
                client.get("first_name")
                or sale.get("client_first_name")
            ),
            "client_last_name": (
                client.get("last_name")
                or sale.get("client_last_name")
            ),
            "client_email": client.get("email"),
            "client_identification_number": (
                client.get("identification_number")
            ),
            "location_id": sale.get(
                "location_id",
                LOCATION_ID
            ),
            "location_name": sale.get(
                "location_name",
                LOCATION_NAME
            ),
            "document_status": sale.get(
                "document_status"
            ),
            "document_url": sale.get(
                "document_url"
            ),
            "note": sale.get("note"),
            "synced_at": datetime.now(
                ZoneInfo("UTC")
            ).isoformat(),
        })

    return rows, missing_payments


def build_items(sales, payments):
    sale_by_payment_id = {
        int(s["payment_id"]): s
        for s in sales
        if s.get("payment_id") is not None
    }

    rows = []
    payments_without_sale = []
    mock_count = 0

    for payment in payments:
        payment_id = int(payment["id"])

        sale = sale_by_payment_id.get(payment_id)

        if not sale:
            payments_without_sale.append(payment_id)
            continue

        sale_id = int(sale["id"])

        receipt_booking_info = {}

        for receipt in payment.get("receipts") or []:
            receipt_id = receipt.get("id")

            for booking in receipt.get("bookings") or []:
                booking_id = booking.get("id")

                if booking_id is None:
                    continue

                receipt_booking_info[int(booking_id)] = {
                    "receipt_id": receipt_id,
                    "client_id": booking.get("client_id"),
                    "price": booking.get("price"),
                    "list_price": booking.get("list_price"),
                    "discount": booking.get("discount"),
                    "service": booking.get("service") or {},
                }

            mock_count += len(
                receipt.get("mock_bookings") or []
            )

        for booking in payment.get("bookings") or []:
            booking_id = booking.get("id")

            if booking_id is None:
                continue

            booking_id = int(booking_id)

            receipt_info = receipt_booking_info.get(
                booking_id,
                {}
            )

            service = (
                booking.get("service")
                or receipt_info.get("service")
                or {}
            )

            provider = (
                booking.get("service_provider")
                or {}
            )

            rows.append({
                "booking_id": booking_id,
                "sale_id": sale_id,
                "payment_id": payment_id,
                "receipt_id": receipt_info.get(
                    "receipt_id"
                ),
                "client_id": (
                    receipt_info.get("client_id")
                    or (payment.get("client") or {}).get("id")
                ),
                "service_id": service.get("id"),
                "service_name": service.get("name"),
                "service_provider_id": provider.get("id"),
                "service_provider_name": provider.get(
                    "public_name"
                ),
                "session_number": booking.get(
                    "session_number"
                ),
                "price": numeric(
                    booking.get("price")
                    if booking.get("price") is not None
                    else receipt_info.get("price")
                ),
                "list_price": numeric(
                    receipt_info.get("list_price")
                ),
                "discount": numeric(
                    receipt_info.get("discount")
                ),
                "item_type": "booking",
                "synced_at": datetime.now(
                    ZoneInfo("UTC")
                ).isoformat(),
            })

    return rows, payments_without_sale, mock_count


def build_transactions(transactions):
    rows = []

    for transaction in transactions:
        rows.append({
            "transaction_id": int(transaction["id"]),
            "sale_id": int(transaction["sale_id"]),
            "sale_internal_id": transaction.get(
                "sale_internal_id"
            ),
            "paid_at_source": transaction.get(
                "paid_at"
            ),
            "business_date": business_date_from_utc(
                transaction.get("paid_at")
            ),
            "amount": numeric(
                transaction.get("amount")
            ),
            "tip": numeric(
                transaction.get("tip")
            ) or 0,
            "external_reference": transaction.get(
                "external_reference"
            ),
            "payment_method_name": transaction.get(
                "payment_method_name"
            ),
            "payment_method_key": transaction.get(
                "payment_method_internal_key"
            ),
            "payment_method_type": transaction.get(
                "payment_method_type"
            ),
            "transaction_type": transaction.get(
                "transaction_type"
            ),
            "installments": transaction.get(
                "installments"
            ),
            "settled_status": transaction.get(
                "settled_status"
            ),
            "synced_at": datetime.now(
                ZoneInfo("UTC")
            ).isoformat(),
        })

    return rows


# ==========================================================
# MAIN
# ==========================================================

def main():
    sync_run_id = None

    try:
        log("========================================")
        log("PILOTO AGENDA PRO -> NIKI OS")
        log(f"Local: {LOCATION_NAME} ({LOCATION_ID})")
        log(f"Fecha: {PILOT_DATE}")
        log("========================================")

        run = supabase_insert(
            "agenda_sync_runs",
            {
                "location_id": LOCATION_ID,
                "date_from": PILOT_DATE,
                "date_to": PILOT_DATE,
                "status": "running",
                "metadata": {
                    "mode": "pilot",
                    "source": "github-actions",
                },
            },
        )

        sync_run_id = run[0]["id"]

        log(f"Sync run creado: {sync_run_id}")

        cookie_header = login_agendapro()

        log("")
        log("Descargando AgendaPro...")

        payments = fetch_payments(cookie_header)
        sales = fetch_sales(cookie_header)
        transactions = fetch_transactions(cookie_header)

        log("")
        log("Resumen origen:")
        log(f"Payments:     {len(payments)}")
        log(f"Sales:        {len(sales)}")
        log(f"Transactions: {len(transactions)}")

        sale_rows, missing_payments = build_sales(
            sales,
            payments
        )

        (
            item_rows,
            payments_without_sale,
            mock_count,
        ) = build_items(
            sales,
            payments
        )

        transaction_rows = build_transactions(
            transactions
        )

        sale_ids = {
            row["sale_id"]
            for row in sale_rows
        }

        transaction_sale_ids = {
            row["sale_id"]
            for row in transaction_rows
        }

        orphan_transactions = sorted(
            transaction_sale_ids - sale_ids
        )

        log("")
        log("Validacion relaciones:")
        log(
            "Sales sin payment v1 relacionado: "
            f"{len(missing_payments)}"
        )
        log(
            "Payments v1 sin sale v2 relacionado: "
            f"{len(payments_without_sale)}"
        )
        log(
            "Transactions sin sale relacionada: "
            f"{len(orphan_transactions)}"
        )
        log(
            "Mock bookings detectados "
            f"(no cargados en este piloto): {mock_count}"
        )

        if missing_payments:
            log(
                "Payment IDs faltantes: "
                + str(missing_payments[:20])
            )

        if payments_without_sale:
            log(
                "Payments sin sale: "
                + str(payments_without_sale[:20])
            )

        if orphan_transactions:
            log(
                "Sale IDs huerfanos en transactions: "
                + str(orphan_transactions[:20])
            )

        if orphan_transactions:
            raise RuntimeError(
                "Hay transacciones cuya venta no fue "
                "recuperada. Se cancela la carga piloto."
            )

        log("")
        log("Grabando en Supabase...")

        # Primero padres
        supabase_upsert(
            "agenda_sales",
            sale_rows,
            "sale_id"
        )

        # Luego hijos
        supabase_upsert(
            "agenda_sale_items",
            item_rows,
            "booking_id"
        )

        supabase_upsert(
            "agenda_payment_transactions",
            transaction_rows,
            "transaction_id"
        )

        supabase_update(
            "agenda_sync_runs",
            {"id": sync_run_id},
            {
                "finished_at": datetime.now(
                    ZoneInfo("UTC")
                ).isoformat(),
                "status": "success",
                "sales_read": len(sales),
                "items_read": len(item_rows),
                "transactions_read": len(transactions),
                "sales_upserted": len(sale_rows),
                "items_upserted": len(item_rows),
                "transactions_upserted": len(
                    transaction_rows
                ),
                "metadata": {
                    "mode": "pilot",
                    "source": "github-actions",
                    "payments_v1_read": len(payments),
                    "sales_without_payment": len(
                        missing_payments
                    ),
                    "payments_without_sale": len(
                        payments_without_sale
                    ),
                    "orphan_transactions": len(
                        orphan_transactions
                    ),
                    "mock_bookings_not_loaded": mock_count,
                },
            },
        )

        log("")
        log("========================================")
        log("CARGA PILOTO OK")
        log(f"Ventas cargadas:       {len(sale_rows)}")
        log(f"Items cargados:        {len(item_rows)}")
        log(
            f"Transacciones cargadas: "
            f"{len(transaction_rows)}"
        )
        log("========================================")

    except Exception as exc:
        log("")
        log(f"ERROR: {exc}")

        if sync_run_id is not None:
            try:
                supabase_update(
                    "agenda_sync_runs",
                    {"id": sync_run_id},
                    {
                        "finished_at": datetime.now(
                            ZoneInfo("UTC")
                        ).isoformat(),
                        "status": "error",
                        "error_message": str(exc)[:3000],
                    },
                )
            except Exception as log_exc:
                log(
                    "No se pudo actualizar agenda_sync_runs: "
                    f"{log_exc}"
                )

        raise


if __name__ == "__main__":
    main()
