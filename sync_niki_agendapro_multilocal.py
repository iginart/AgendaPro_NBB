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
ROLLING_DAYS = 10
SYNC_MODE = os.getenv("SYNC_MODE", "today").strip().lower()
SYNC_FROM = os.getenv("SYNC_FROM", "").strip()
SYNC_TO = os.getenv("SYNC_TO", "").strip()
HISTORICAL_MAX_DAYS = int(os.getenv("HISTORICAL_MAX_DAYS", "31"))
REFRESH_COMMISSION_SHADOW = os.getenv("REFRESH_COMMISSION_SHADOW", "true").strip().lower() not in {"0", "false", "no", "off"}
PER_PAGE = 100
HTTP = requests.Session()

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
    r = HTTP.post(
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
    r = HTTP.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        json=rows,
        timeout=180,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Supabase UPSERT {table}: HTTP {r.status_code} - {r.text[:3000]}")


def supabase_update(table, filters, values, retries=5):
    qs = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            r = HTTP.patch(
                f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
                headers=supabase_headers("return=minimal"),
                json=values,
                timeout=90,
            )

            if r.status_code in (200, 204):
                return

            body = r.text[:2000]
            transient = (
                r.status_code in (502, 503, 504)
                or "PGRST002" in body
                or "schema cache" in body.lower()
            )

            if transient and attempt < retries:
                wait_seconds = min(2 ** attempt, 15)
                log(
                    f"WARNING: Supabase UPDATE {table} fallo transitorio "
                    f"(intento {attempt}/{retries}). "
                    f"Reintentando en {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                continue

            raise RuntimeError(
                f"Supabase UPDATE {table}: HTTP {r.status_code} - {body}"
            )

        except Exception as exc:
            last_error = exc

            if attempt < retries:
                wait_seconds = min(2 ** attempt, 15)
                log(
                    f"WARNING: Supabase UPDATE {table} fallo "
                    f"(intento {attempt}/{retries}). "
                    f"Reintentando en {wait_seconds}s... Detalle: {exc}"
                )
                time.sleep(wait_seconds)
                continue

            raise

    if last_error:
        raise last_error


def supabase_select(table, params=None):
    q = {"select": "*"}
    if params:
        q.update(params)
    r = HTTP.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=supabase_headers(),
        params=q,
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Supabase SELECT {table}: HTTP {r.status_code} - {r.text[:2000]}")
    return r.json()


def supabase_rpc(function_name, payload=None):
    r = HTTP.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{function_name}",
        headers=supabase_headers("return=representation"),
        json=payload or {},
        timeout=180,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"Supabase RPC {function_name}: HTTP {r.status_code} - {r.text[:3000]}"
        )
    if r.status_code == 204 or not r.text.strip():
        return None
    return r.json()




def refresh_dashboard_materialized_best_effort():
    """
    Refresca la base materializada del Dashboard una sola vez al final de
    today/rolling. En historical/backfill se omite para no recalcular en cada bloque.
    Un timeout no invalida la sincronizacion fuente.
    """
    if SYNC_MODE in {"historical", "backfill"}:
        log("Modo historico/backfill: se omite refresh de mv_agendapro_kpi_local_dia.")
        return {"attempted": False, "ok": None, "result": None, "error": None}

    log("Refrescando materializados del Dashboard...")
    try:
        result = supabase_rpc("refrescar_agendapro_kpi_materializados")
        log(f"Dashboard materializado actualizado: {result}")
        return {"attempted": True, "ok": True, "result": result, "error": None}
    except Exception as exc:
        message = str(exc)
        log(
            "WARNING: no se pudo refrescar el Dashboard materializado. "
            "La sincronizacion fuente continua como exitosa. "
            f"Detalle: {message[:1200]}"
        )
        return {"attempted": True, "ok": False, "result": None, "error": message[:3000]}


def parse_sync_date(value, env_name):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError(
            f"{env_name} invalido: {value!r}. Usar formato YYYY-MM-DD."
        ) from exc


def resolve_sync_window(today_arg):
    if SYNC_MODE == "today":
        return today_arg, today_arg, "today"

    if SYNC_MODE == "rolling":
        start_day = today_arg - timedelta(days=ROLLING_DAYS - 1)
        return start_day, today_arg, f"rolling_{ROLLING_DAYS}_days"

    if SYNC_MODE in {"historical", "backfill"}:
        if not SYNC_FROM or not SYNC_TO:
            raise RuntimeError(
                "Para SYNC_MODE=historical se requieren SYNC_FROM y SYNC_TO "
                "en formato YYYY-MM-DD."
            )

        start_day = parse_sync_date(SYNC_FROM, "SYNC_FROM")
        end_day = parse_sync_date(SYNC_TO, "SYNC_TO")

        if end_day < start_day:
            raise RuntimeError("SYNC_TO no puede ser anterior a SYNC_FROM.")

        total_days = (end_day - start_day).days + 1
        if total_days > HISTORICAL_MAX_DAYS:
            raise RuntimeError(
                f"El backfill historico admite hasta {HISTORICAL_MAX_DAYS} dias por ejecucion. "
                f"Rango solicitado: {total_days} dias. Ejecutar por bloques mensuales."
            )

        if end_day > today_arg:
            raise RuntimeError("SYNC_TO no puede ser posterior a la fecha actual.")

        return start_day, end_day, f"historical_{start_day}_{end_day}"

    raise RuntimeError(
        f"SYNC_MODE invalido: {SYNC_MODE}. Valores permitidos: today, rolling, historical"
    )


def should_refresh_derived():
    """Los historical/backfill cargan fuente; los derivados se refrescan al final."""
    if SYNC_MODE in {"historical", "backfill"}:
        return False
    return REFRESH_COMMISSION_SHADOW


def refresh_commission_shadow_best_effort():
    """El refresh derivado no debe hacer fallar una sync fuente ya guardada."""
    if not should_refresh_derived():
        if SYNC_MODE in {"historical", "backfill"}:
            log("Modo historico/backfill: se omite refresh de comisiones_agendapro_shadow.")
        else:
            log("Refresh de shadow omitido por REFRESH_COMMISSION_SHADOW=false")
        return {"attempted": False, "ok": None, "result": None, "error": None}

    log("Refrescando comisiones_agendapro_shadow...")
    try:
        result = supabase_rpc("refrescar_comisiones_agendapro_shadow")
        log(f"Shadow actualizada: {result}")
        return {"attempted": True, "ok": True, "result": result, "error": None}
    except Exception as exc:
        message = str(exc)
        log(
            "WARNING: no se pudo refrescar comisiones_agendapro_shadow. "
            "La sincronizacion fuente continua como exitosa. "
            f"Detalle: {message[:1200]}"
        )
        return {"attempted": True, "ok": False, "result": None, "error": message[:3000]}


def supabase_delete_by_sale_ids(table, sale_ids):
    ids = sorted({int(x) for x in sale_ids if x is not None})
    if not ids:
        return
    for pos in range(0, len(ids), 100):
        batch = ids[pos:pos + 100]
        r = HTTP.delete(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supabase_headers("return=minimal"),
            params={"sale_id": f"in.({','.join(str(x) for x in batch)})"},
            timeout=120,
        )
        if r.status_code not in (200, 204):
            raise RuntimeError(f"Supabase DELETE {table}: HTTP {r.status_code} - {r.text[:2000]}")


def supabase_insert_rows(table, rows, batch_size=250):
    if not rows:
        return
    for pos in range(0, len(rows), batch_size):
        supabase_insert(table, rows[pos:pos + batch_size], return_representation=False)


def supabase_upsert_rows(table, rows, on_conflict, batch_size=250):
    if not rows:
        return
    for pos in range(0, len(rows), batch_size):
        supabase_upsert(table, rows[pos:pos + batch_size], on_conflict)


def supabase_rows_by_sale_ids(table, sale_ids):
    ids = sorted({int(x) for x in sale_ids if x is not None})
    out = []
    for pos in range(0, len(ids), 200):
        batch = ids[pos:pos + 200]
        out.extend(supabase_select(
            table,
            {"sale_id": f"in.({','.join(str(x) for x in batch)})"}
        ))
    return out


def supabase_select_all(table, params=None, page_size=1000):
    """
    Lee todas las filas de una consulta PostgREST usando Range.
    Evita el limite habitual de 1000 filas.
    """
    out = []
    offset = 0

    while True:
        q = {"select": "*"}
        if params:
            q.update(params)

        headers = supabase_headers()
        headers["Range"] = f"{offset}-{offset + page_size - 1}"

        r = HTTP.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            params=q,
            timeout=120,
        )

        if r.status_code not in (200, 206):
            raise RuntimeError(
                f"Supabase SELECT ALL {table}: HTTP {r.status_code} - {r.text[:2000]}"
            )

        rows = r.json()
        out.extend(rows)

        if len(rows) < page_size:
            break

        offset += page_size

    return out


def purge_sales_window(start_day, end_day):
    """
    Borra del staging AgendaPro todas las ventas del rango y de los locales
    sincronizados, junto con sus items y transacciones.

    Se usa antes de un rolling completo para garantizar que los 10 dias
    se reconstruyan desde cero.
    """
    locations_csv = ",".join(str(x) for x in LOCATIONS)

    log(
        f"Purgando datos existentes desde {start_day} hasta {end_day} "
        f"para {len(LOCATIONS)} locales..."
    )

    rows = supabase_select_all(
        "agenda_sales",
        {
            "select": "sale_id",
            "location_id": f"in.({locations_csv})",
            "and": (
                f"(business_date.gte.{start_day.isoformat()},"
                f"business_date.lte.{end_day.isoformat()})"
            ),
            "order": "sale_id.asc",
        },
    )

    sale_ids = sorted(
        {
            int(r["sale_id"])
            for r in rows
            if r.get("sale_id") is not None
        }
    )

    if not sale_ids:
        log("Purga: no habia ventas existentes en el rango.")
        return 0

    log(f"Purga: {len(sale_ids)} ventas a eliminar.")

    supabase_delete_by_sale_ids("agenda_sale_items", sale_ids)
    supabase_delete_by_sale_ids("agenda_payment_transactions", sale_ids)
    supabase_delete_by_sale_ids("agenda_sales", sale_ids)

    log(f"Purga finalizada: {len(sale_ids)} ventas eliminadas.")
    return len(sale_ids)


def empty_summary():
    return {
        "payments": 0,
        "sales": 0,
        "regular_sales": 0,
        "special_sales": 0,
        "transactions": 0,
        "items": 0,
        "missing_payments": 0,
        "payments_without_sale": 0,
        "orphan_transactions": 0,
        "memberships": 0,
        "giftcards": 0,
        "mock_bookings": 0,
        "products": 0,
        "changes_detected": 0,
        "missing_marked": 0,
        "missing_resolved": 0,
        "changed_item_sales": 0,
        "changed_transaction_sales": 0,
        "new_sales": 0,
    }


def add_summary(total, part):
    for key in total:
        total[key] += int(part.get(key) or 0)
    return total


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


def normalize_integer(value):
    if value in (None, ""):
        return None
    try:
        return str(int(Decimal(str(value))))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def normalize_timestamp(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    # AgendaPro y PostgREST representan el mismo instante con formatos distintos.
    # Para comparar, normalizamos siempre a UTC sin depender de .000Z / +00:00.
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_row(row, fields):
    numeric_fields = {
        "total_amount", "paid_amount", "pending_amount", "giftcard_amount",
        "price", "list_price", "discount", "quantity", "subtotal", "total",
        "amount", "tip",
    }
    integer_fields = {
        "transaction_id", "sale_id", "booking_id", "source_item_id", "payment_id",
        "receipt_id", "client_id", "service_id", "service_provider_id", "seller_id",
        "session_number", "installments", "cart_id", "location_id",
    }
    timestamp_fields = {"paid_at_source"}

    result = {}
    for field in fields:
        value = row.get(field)
        if field in numeric_fields:
            value = normalize_number(value)
        elif field in integer_fields:
            value = normalize_integer(value)
        elif field in timestamp_fields:
            value = normalize_timestamp(value)
        elif value == "":
            value = None
        result[field] = value
    return result


def canonical_rows(rows, fields):
    values = [canonical_row(r, fields) for r in rows]
    return sorted(values, key=lambda x: json.dumps(x, sort_keys=True, default=str, ensure_ascii=False))


def queue_change(change_rows, sync_run_id, sale_id, location_id, business_date,
                 change_type, old_value, new_value):
    change_rows.append({
        "sync_run_id": sync_run_id,
        "sale_id": sale_id,
        "location_id": location_id,
        "business_date": business_date,
        "change_type": change_type,
        "old_value": old_value,
        "new_value": new_value,
    })


def compact_diff_fields(old_row, new_row):
    keys = sorted(set(old_row) | set(new_row))
    return [k for k in keys if old_row.get(k) != new_row.get(k)]


def log_compare_sample(kind, sale_id, old_value, new_value, max_chars=1200):
    """Muestra una muestra compacta si queda algun falso positivo por diagnosticar."""
    try:
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            fields = compact_diff_fields(old_value, new_value)
            log(f"Muestra diferencia {kind} sale_id={sale_id}: campos={fields}")
            return

        old_json = json.dumps(old_value, ensure_ascii=False, sort_keys=True, default=str)
        new_json = json.dumps(new_value, ensure_ascii=False, sort_keys=True, default=str)
        log(
            f"Muestra diferencia {kind} sale_id={sale_id}: "
            f"old={old_json[:max_chars]} | new={new_json[:max_chars]}"
        )
    except Exception:
        pass

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
    r = HTTP.get(
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


def _dedupe_by_id(rows, key="id"):
    out = {}
    for row in rows:
        value = row.get(key)
        if value is not None:
            out[int(value)] = row
    return list(out.values())


def fetch_v1_payments_window(cookie_header, start_day, end_day):
    from_dmy = start_day.strftime("%d-%m-%Y")
    to_dmy = end_day.strftime("%d-%m-%Y")
    locations_csv = ",".join(str(x) for x in LOCATIONS)
    all_rows = []
    page = 1
    while True:
        url = (
            "https://agendapro.com/api/views/admin/v1/payments"
            f"?from={from_dmy}&to={to_dmy}&per_page={PER_PAGE}"
            f"&location_ids={locations_csv}&page={page}"
        )
        data = agenda_get(url, cookie_header)
        rows = data.get("payments") or []
        all_rows.extend(rows)
        pages = int(data.get("pages") or 1)
        log(f"payments pagina {page}/{pages}: {len(rows)}")
        if page >= pages:
            break
        page += 1
    return _dedupe_by_id(all_rows)


def _v2_locations_query():
    return "&".join(f"location_id[]={location_id}" for location_id in LOCATIONS)


def fetch_v2_sales_window(cookie_header, start_day, end_day):
    start_iso = start_day.strftime("%Y-%m-%d")
    end_iso = end_day.strftime("%Y-%m-%d")
    locations_qs = _v2_locations_query()
    all_rows = []
    page = 1
    while True:
        url = (
            "https://agendapro.com/api/views/admin/v2/sales/sale"
            f"?per_page={PER_PAGE}&page={page}"
            f"&end_date={end_iso}T23:59:59-03:00"
            f"&start_date={start_iso}T00:00:00-03:00"
            f"&{locations_qs}"
        )
        data = agenda_get(url, cookie_header)
        rows = data.get("data") or []
        all_rows.extend(rows)
        pages = int((data.get("pagination") or {}).get("total_pages") or 1)
        log(f"sales pagina {page}/{pages}: {len(rows)}")
        if page >= pages:
            break
        page += 1
    return _dedupe_by_id(all_rows)


def fetch_v2_transactions_window(cookie_header, start_day, end_day):
    start_iso = start_day.strftime("%Y-%m-%d")
    end_iso = end_day.strftime("%Y-%m-%d")
    locations_qs = _v2_locations_query()
    all_rows = []
    page = 1
    while True:
        url = (
            "https://agendapro.com/api/views/admin/v2/sales/transaction"
            f"?end_date={end_iso}T23:59:59-03:00"
            f"&start_date={start_iso}T00:00:00-03:00"
            f"&{locations_qs}"
            "&sale_id=&external_reference="
            f"&page={page}&per_page={PER_PAGE}"
        )
        data = agenda_get(url, cookie_header)
        rows = data.get("data") or []
        all_rows.extend(rows)
        pages = int((data.get("pagination") or {}).get("total_pages") or 1)
        log(f"transactions pagina {page}/{pages}: {len(rows)}")
        if page >= pages:
            break
        page += 1
    return _dedupe_by_id(all_rows)


def fetch_v2_sale_detail(cookie_header, sale_id):
    url = f"https://agendapro.com/api/views/admin/v2/sales/sale/{sale_id}"
    r = HTTP.get(
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


def run_window(cookie_header, start_day, end_day, sync_run_id):
    change_log_rows = []
    sample_counts = {"sale": 0, "items": 0, "transactions": 0}
    log("Leyendo AgendaPro en bloque para todos los locales...")
    payments = fetch_v1_payments_window(cookie_header, start_day, end_day)
    sales = fetch_v2_sales_window(cookie_header, start_day, end_day)
    transactions = fetch_v2_transactions_window(cookie_header, start_day, end_day)

    log(
        f"Origen: payments={len(payments)} | sales={len(sales)} | "
        f"transactions={len(transactions)}"
    )

    sale_rows, missing_payments = build_sales(sales, payments)
    item_rows, payments_without_sale, memberships, giftcards = build_items(sales, payments)
    transaction_rows = build_transactions(transactions)

    sale_ids = {int(r["sale_id"]) for r in sale_rows}
    orphan_transactions = sorted({int(r["sale_id"]) for r in transaction_rows} - sale_ids)

    resolved_special_sales = []
    unresolved_transactions = []
    special_item_rows = []

    for orphan_sale_id in orphan_transactions:
        detail = fetch_v2_sale_detail(cookie_header, orphan_sale_id)
        if not detail:
            unresolved_transactions.append(orphan_sale_id)
            continue

        detail_location = detail.get("location_id")
        if detail_location is not None and int(detail_location) not in LOCATIONS:
            unresolved_transactions.append(orphan_sale_id)
            log(
                f"WARNING sale especial {orphan_sale_id}: "
                f"local detalle={detail_location} fuera del conjunto sincronizado"
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
            "Transacciones no resueltas: " + str(unresolved_transactions[:20])
        )

    # PostgREST exige las mismas claves para todos los objetos del array.
    item_rows = [{key: row.get(key) for key in ITEM_KEYS} for row in item_rows]

    # Deduplicacion defensiva por las mismas claves naturales que usa Supabase.
    # agenda_sale_items tiene UNIQUE (item_type, source_item_id). Por eso sale_id
    # NO puede formar parte de la identidad del item: AgendaPro puede reasociar
    # un booking/item existente a otra venta.
    sale_rows = list({int(r["sale_id"]): r for r in sale_rows}.values())
    transaction_rows = list({int(r["transaction_id"]): r for r in transaction_rows}.values())

    sale_by_id = {int(r["sale_id"]): r for r in sale_rows}

    def item_priority(row):
        sale = sale_by_id.get(int(row["sale_id"]), {})

        # Ante una duplicacion entre ventas, preferimos una venta activa.
        active_rank = 0 if is_cancelled_status(sale.get("status")) else 1

        # Si ambas estan activas, preferimos la representacion mas reciente.
        # Los timestamps ISO UTC se pueden ordenar lexicograficamente.
        paid_at_rank = sale.get("paid_at_source") or ""

        return (
            active_rank,
            paid_at_rank,
            int(row["sale_id"]),
        )

    item_unique = {}
    for r in item_rows:
        key = (r.get("item_type"), r.get("source_item_id"))
        previous = item_unique.get(key)

        if previous is None:
            item_unique[key] = r
            continue

        if int(previous["sale_id"]) != int(r["sale_id"]):
            chosen = max((previous, r), key=item_priority)
            log(
                "Item AgendaPro reasociado/doble: "
                f"tipo={key[0]} source_item_id={key[1]} "
                f"sale_1={previous['sale_id']} sale_2={r['sale_id']} "
                f"sale_elegida={chosen['sale_id']}"
            )
            item_unique[key] = chosen
        else:
            # Mismo item dentro de la misma venta: conservar la ultima version.
            item_unique[key] = r

    item_rows = list(item_unique.values())

    incoming_ids = {int(r["sale_id"]) for r in sale_rows}
    now_iso = now_utc_iso()

    # Una sola lectura de cabeceras para toda la ventana y todos los locales.
    locations_csv = ",".join(str(x) for x in LOCATIONS)
    existing_window_rows = supabase_select(
        "agenda_sales",
        {
            "location_id": f"in.({locations_csv})",
            "and": (
                f"(business_date.gte.{start_day.isoformat()},"
                f"business_date.lte.{end_day.isoformat()})"
            ),
        },
    )
    existing_window_by_id = {int(r["sale_id"]): r for r in existing_window_rows}

    # Ventas entrantes que antes pudieron estar fuera de la ventana.
    missing_previous_ids = incoming_ids - set(existing_window_by_id)
    previous_outside = supabase_rows_by_sale_ids("agenda_sales", missing_previous_ids)
    previous_by_id = dict(existing_window_by_id)
    previous_by_id.update({int(r["sale_id"]): r for r in previous_outside})

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
    changed_item_sale_ids = set()
    changed_tx_sale_ids = set()
    new_sale_ids = set()

    for row in sale_rows:
        sid = int(row["sale_id"])
        old = previous_by_id.get(sid)

        if old:
            old_sale = canonical_row(old, SALE_COMPARE_FIELDS)
            new_sale = canonical_row(row, SALE_COMPARE_FIELDS)
            if old_sale != new_sale:
                if sample_counts["sale"] < 3:
                    log_compare_sample("sale", sid, old_sale, new_sale)
                    sample_counts["sale"] += 1
                change_type = (
                    "sale_date_changed"
                    if old.get("business_date") != row.get("business_date")
                    else "sale_updated"
                )
                queue_change(change_log_rows, 
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    change_type, old_sale, new_sale,
                )
                changes_detected += 1

            old_items = canonical_rows(prev_items_by_sale.get(sid, []), ITEM_COMPARE_FIELDS)
            new_items = canonical_rows(new_items_by_sale.get(sid, []), ITEM_COMPARE_FIELDS)
            if old_items != new_items:
                if sample_counts["items"] < 3:
                    log_compare_sample("items", sid, old_items, new_items)
                    sample_counts["items"] += 1
                changed_item_sale_ids.add(sid)
                queue_change(change_log_rows, 
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    "items_changed", old_items, new_items,
                )
                changes_detected += 1

            old_tx = canonical_rows(prev_tx_by_sale.get(sid, []), TRANSACTION_COMPARE_FIELDS)
            new_tx = canonical_rows(new_tx_by_sale.get(sid, []), TRANSACTION_COMPARE_FIELDS)
            if old_tx != new_tx:
                if sample_counts["transactions"] < 3:
                    log_compare_sample("transactions", sid, old_tx, new_tx)
                    sample_counts["transactions"] += 1
                changed_tx_sale_ids.add(sid)
                queue_change(change_log_rows, 
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    "transactions_changed", old_tx, new_tx,
                )
                changes_detected += 1

            if old.get("source_active") is False:
                queue_change(change_log_rows, 
                    sync_run_id, sid, row.get("location_id"), row.get("business_date"),
                    "sale_reactivated", {"source_active": False}, {"source_active": True},
                )
                changes_detected += 1
        else:
            new_sale_ids.add(sid)
            changed_item_sale_ids.add(sid)
            changed_tx_sale_ids.add(sid)

        row["source_active"] = not is_cancelled_status(row.get("status"))
        row["last_seen_at"] = now_iso
        row["source_missing_since"] = None

    # Cabeceras siempre se actualizan para refrescar last_seen_at.
    for pos in range(0, len(sale_rows), 250):
        supabase_upsert("agenda_sales", sale_rows[pos:pos + 250], "sale_id")

    # Hijos solo se reemplazan cuando realmente cambiaron o la venta es nueva.
    # Luego del DELETE usamos UPSERT. Esto permite que un item/transaccion que
    # AgendaPro reasocio a otra sale_id actualice la fila existente en vez de
    # fallar por una restriccion UNIQUE global.
    if changed_item_sale_ids:
        supabase_delete_by_sale_ids("agenda_sale_items", changed_item_sale_ids)
        changed_items = [r for r in item_rows if int(r["sale_id"]) in changed_item_sale_ids]
        supabase_upsert_rows(
            "agenda_sale_items",
            changed_items,
            "item_type,source_item_id",
        )

    if changed_tx_sale_ids:
        supabase_delete_by_sale_ids("agenda_payment_transactions", changed_tx_sale_ids)
        changed_tx = [r for r in transaction_rows if int(r["sale_id"]) in changed_tx_sale_ids]
        supabase_upsert_rows(
            "agenda_payment_transactions",
            changed_tx,
            "transaction_id",
        )

    # Ventas activas que estaban dentro de la ventana pero ya no aparecen en la API actual.
    existing_active_ids = {
        int(r["sale_id"])
        for r in existing_window_rows
        if r.get("source_active") is not False
    }
    missing_ids = sorted(existing_active_ids - incoming_ids)
    missing_marked = 0
    missing_resolved = 0

    for sid in missing_ids:
        old = existing_window_by_id.get(sid)
        detail = fetch_v2_sale_detail(cookie_header, sid)

        if detail:
            detail_sale = build_detail_sale(detail)
            detail_sale["source_active"] = not is_cancelled_status(detail_sale.get("status"))
            detail_sale["last_seen_at"] = now_iso
            detail_sale["source_missing_since"] = None

            old_date = old.get("business_date") if old else None
            new_date = detail_sale.get("business_date")
            new_status = detail_sale.get("status")

            if old_date != new_date:
                change_type = "sale_date_changed"
            elif is_cancelled_status(new_status):
                change_type = "sale_cancelled"
            else:
                change_type = "sale_updated"

            queue_change(change_log_rows, 
                sync_run_id, sid, detail_sale.get("location_id"), new_date or old_date,
                change_type, canonical_row(old or {}, SALE_COMPARE_FIELDS),
                canonical_row(detail_sale, SALE_COMPARE_FIELDS),
            )
            supabase_upsert("agenda_sales", [detail_sale], "sale_id")

            # El detalle puede traer items especiales; si los trae, reemplazarlos.
            # Tambien aqui usamos UPSERT porque source_item_id es global y puede
            # haber quedado previamente asociado a otra venta.
            detail_items = [{key: row.get(key) for key in ITEM_KEYS} for row in build_detail_items(detail)]
            if detail_items:
                detail_unique = {}
                for item in detail_items:
                    detail_key = (item.get("item_type"), item.get("source_item_id"))
                    detail_unique[detail_key] = item
                detail_items = list(detail_unique.values())

                supabase_delete_by_sale_ids("agenda_sale_items", [sid])
                supabase_upsert_rows(
                    "agenda_sale_items",
                    detail_items,
                    "item_type,source_item_id",
                )

            changes_detected += 1
            missing_resolved += 1
            log(f"Venta ausente resuelta por detalle: sale_id={sid} tipo={change_type}")
        else:
            first_missing = (old or {}).get("source_missing_since") or now_iso
            supabase_update(
                "agenda_sales",
                {"sale_id": sid},
                {"source_active": False, "source_missing_since": first_missing},
            )
            queue_change(change_log_rows, 
                sync_run_id, sid, (old or {}).get("location_id"),
                (old or {}).get("business_date"), "sale_missing",
                canonical_row(old or {}, SALE_COMPARE_FIELDS),
                {"source_active": False, "source_missing_since": first_missing},
            )
            changes_detected += 1
            missing_marked += 1
            log(f"Venta ya no disponible en AgendaPro: sale_id={sid}")

    # La auditoria se inserta en lotes; nunca una llamada HTTP por cambio.
    supabase_insert_rows("agenda_change_log", change_log_rows, batch_size=50)

    return {
        "payments": len(payments),
        "sales": len(sale_rows),
        "regular_sales": len(sales),
        "special_sales": len(resolved_special_sales),
        "transactions": len(transaction_rows),
        "items": len(item_rows),
        "missing_payments": len(missing_payments),
        "payments_without_sale": len(payments_without_sale),
        "orphan_transactions": len(orphan_transactions),
        "memberships": memberships,
        "giftcards": giftcards + sum(1 for r in special_item_rows if r["item_type"] == "giftcard"),
        "mock_bookings": sum(1 for r in item_rows if r["item_type"] == "mock_booking"),
        "products": sum(1 for r in item_rows if r["item_type"] == "product"),
        "changes_detected": changes_detected,
        "missing_marked": missing_marked,
        "missing_resolved": missing_resolved,
        "changed_item_sales": len(changed_item_sale_ids),
        "changed_transaction_sales": len(changed_tx_sale_ids),
        "new_sales": len(new_sale_ids),
    }


def main():
    started = time.time()
    today_arg = datetime.now(ARG_TZ).date()
    start_day, end_day, mode_label = resolve_sync_window(today_arg)

    run = supabase_insert(
        "agenda_sync_runs",
        {
            "location_id": None,
            "date_from": start_day.isoformat(),
            "date_to": end_day.isoformat(),
            "status": "running",
            "metadata": {
                "mode": f"reconciliation_v5_{mode_label}",
                "source": "github-actions",
                "locations": LOCATIONS,
                "strategy": "single_window_all_locations_batch_audit",
            },
        },
    )
    sync_run_id = run[0]["id"]

    try:
        cookie_header = login_agendapro()
        log("\n========================================")
        log("SYNC AGENDA PRO -> NIKI OS V5")
        log(f"Modo: {mode_label}")
        log(f"Desde: {start_day}")
        log(f"Hasta: {end_day}")
        log(f"Locales: {len(LOCATIONS)} (en una sola ventana)")
        log("========================================\n")

        if SYNC_MODE == "rolling":
            purged_sales = purge_sales_window(start_day, end_day)

            summary = empty_summary()
            current_day = start_day

            while current_day <= end_day:
                log("\n----------------------------------------")
                log(f"ROLLING DIA: {current_day}")
                log("----------------------------------------")

                day_summary = run_window(
                    cookie_header,
                    current_day,
                    current_day,
                    sync_run_id,
                )
                add_summary(summary, day_summary)
                current_day += timedelta(days=1)

            log(
                f"Rolling reconstruido dia por dia. "
                f"Ventas eliminadas previamente: {purged_sales}"
            )
        else:
            purged_sales = 0
            summary = run_window(
                cookie_header,
                start_day,
                end_day,
                sync_run_id,
            )

        dashboard_refresh_state = refresh_dashboard_materialized_best_effort()

        shadow_state = refresh_commission_shadow_best_effort()
        shadow_refresh = shadow_state["result"]

        elapsed = round(time.time() - started, 1)

        warnings = []
        if summary["missing_payments"]:
            warnings.append(f"sales_sin_payment={summary['missing_payments']}")
        if summary["payments_without_sale"]:
            warnings.append(f"payments_sin_sale={summary['payments_without_sale']}")
        if summary["memberships"]:
            warnings.append(f"memberships={summary['memberships']}")

        metadata = {
            "mode": f"reconciliation_v5_{mode_label}",
            "source": "github-actions",
            "locations": LOCATIONS,
            "strategy": "single_window_all_locations_batch_audit",
            "elapsed_seconds": elapsed,
            "mock_bookings": summary["mock_bookings"],
            "products": summary["products"],
            "memberships_detected_not_loaded": summary["memberships"],
            "giftcards_loaded_or_detected": summary["giftcards"],
            "special_sales_resolved": summary["special_sales"],
            "changes_detected": summary["changes_detected"],
            "sales_marked_missing": summary["missing_marked"],
            "missing_sales_resolved_by_detail": summary["missing_resolved"],
            "new_sales": summary["new_sales"],
            "item_sales_replaced": summary["changed_item_sales"],
            "transaction_sales_replaced": summary["changed_transaction_sales"],
            "dashboard_refresh_attempted": dashboard_refresh_state["attempted"],
            "dashboard_refreshed": dashboard_refresh_state["ok"] is True,
            "dashboard_refresh_result": dashboard_refresh_state["result"],
            "dashboard_refresh_error": dashboard_refresh_state["error"],
            "shadow_refresh_attempted": shadow_state["attempted"],
            "shadow_refreshed": shadow_state["ok"] is True,
            "shadow_refresh_result": shadow_refresh,
            "shadow_refresh_error": shadow_state["error"],
            "sync_from": start_day.isoformat(),
            "sync_to": end_day.isoformat(),
            "rolling_day_by_day": SYNC_MODE == "rolling",
            "rolling_purged_sales": purged_sales,
            "warnings": warnings,
        }

        try:
            supabase_update(
                "agenda_sync_runs",
                {"id": sync_run_id},
                {
                    "finished_at": now_utc_iso(),
                    "status": "success",
                    "sales_read": summary["sales"],
                    "items_read": summary["items"],
                    "transactions_read": summary["transactions"],
                    "sales_upserted": summary["sales"],
                    "items_upserted": summary["changed_item_sales"],
                    "transactions_upserted": summary["changed_transaction_sales"],
                    "metadata": metadata,
                },
            )
        except Exception as exc:
            log(
                "WARNING: la sincronizacion termino correctamente, pero no se pudo "
                "actualizar agenda_sync_runs. La carga fuente queda como exitosa. "
                f"Detalle: {exc}"
            )

        log("\n========================================")
        log("SYNC V5 OK")
        log(f"Ventas leidas:          {summary['sales']}")
        log(f"Items leidos:           {summary['items']}")
        log(f"Transacciones leidas:   {summary['transactions']}")
        log(f"Ventas nuevas:          {summary['new_sales']}")
        log(f"Ventas especiales:      {summary['special_sales']}")
        log(f"Ventas con items cambiados: {summary['changed_item_sales']}")
        log(f"Ventas con pagos cambiados: {summary['changed_transaction_sales']}")
        log(f"Cambios detectados:     {summary['changes_detected']}")
        log(f"Missing marcadas:       {summary['missing_marked']}")
        log(f"Missing resueltas:      {summary['missing_resolved']}")
        log(f"Modo:                   {mode_label}")
        if SYNC_MODE == "rolling":
            log(f"Rolling dia por dia:     SI")
            log(f"Ventas purgadas:         {purged_sales}")
        log(
            "Dashboard refrescado:   "
            + (
                "SI"
                if dashboard_refresh_state["ok"] is True
                else "NO (omitido)"
                if dashboard_refresh_state["attempted"] is False
                else "NO (fallo no bloqueante)"
            )
        )
        log(
            "Shadow refrescada:      "
            + (
                "SI"
                if shadow_state["ok"] is True
                else "NO (omitida)"
                if shadow_state["attempted"] is False
                else "NO (fallo no bloqueante)"
            )
        )
        log(f"Tiempo total:           {elapsed} segundos")
        log("========================================")

    except Exception as exc:
        try:
            supabase_update(
                "agenda_sync_runs",
                {"id": sync_run_id},
                {
                    "finished_at": now_utc_iso(),
                    "status": "error",
                    "metadata": {
                        "mode": f"reconciliation_v5_{mode_label}",
                        "error": str(exc)[:3000],
                    },
                },
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
