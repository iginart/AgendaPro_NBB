import os
from playwright.sync_api import sync_playwright
import json
import time

# =========================
# CONFIGURACIÓN
# =========================

LOGIN_URL = "https://app.agendapro.com/sign_in"
REPORT_URL = "https://app.agendapro.com/sales_cash"

USUARIO = os.environ.get("AGENDAPRO_USER")
PASSWORD = os.environ.get("AGENDAPRO_PASS")

if not USUARIO or not PASSWORD:
    raise Exception("Faltan variables de entorno AGENDAPRO_USER / AGENDAPRO_PASS")


FECHA_INICIO = "01-11-2025"
FECHA_FIN = "30-11-2025"

# =========================
# UTIL: MESES
# =========================

MESES = {
    "01": "Enero", "02": "Febrero", "03": "Marzo", "04": "Abril",
    "05": "Mayo", "06": "Junio", "07": "Julio", "08": "Agosto",
    "09": "Septiembre", "10": "Octubre", "11": "Noviembre", "12": "Diciembre"
}

# =========================
# FECHAS
# =========================

def seleccionar_rango_fechas(page, inicio, fin):
    d1, m1, y1 = inicio.split("-")
    d2, m2, y2 = fin.split("-")

    campo = page.locator("input.flatpickr-input")
    campo.click()
    page.wait_for_timeout(300)

    # Inicio
    page.locator("select.flatpickr-monthDropdown-months").select_option(str(int(m1) - 1))
    page.locator("input.cur-year").fill(y1)
    page.wait_for_timeout(200)
    page.locator(f"[aria-label='{MESES[m1]} {int(d1)}, {y1}']").click()

    # Fin
    page.locator("select.flatpickr-monthDropdown-months").select_option(str(int(m2) - 1))
    page.locator("input.cur-year").fill(y2)
    page.wait_for_timeout(200)
    page.locator(f"[aria-label='{MESES[m2]} {int(d2)}, {y2}']").click()

    page.wait_for_timeout(500)

# =========================
# MAIN
# =========================

def main():
    api_responses = []

    def intercept_response(response):
        if "sales_cash_content_expenses" in response.url:
            try:
                api_responses.append(response.json())
            except:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Escuchar llamadas de red
        page.on("response", intercept_response)

        # LOGIN
        page.goto(LOGIN_URL)
        page.goto("https://app.agendapro.com/sign_in", wait_until="networkidle")

        page.wait_for_selector("input[type='email']", timeout=15000)
        page.fill("input[type='email']", USUARIO)

        page.wait_for_selector("input[type='password']", timeout=15000)
        page.fill("input[type='password']", PASSWORD)

        page.click("button[type='submit']")
        page.wait_for_timeout(3000)

        page.wait_for_timeout(2000)

        # REPORTE
        page.goto(REPORT_URL)
        page.wait_for_timeout(1500)

        # FECHAS
        seleccionar_rango_fechas(page, FECHA_INICIO, FECHA_FIN)

        # BUSCAR
        page.get_by_test_id("search-button").click()
        page.wait_for_timeout(4000)

        # EGRESOS
        page.locator("[data-cy='Egresos']").click()
        page.wait_for_timeout(3000)

        # Esperar que carguen todas las páginas (muy importante)
        time.sleep(6)

        browser.close()

    # =========================
    # GUARDAR JSON
    # =========================

    with open("gastos_api.json", "w", encoding="utf-8") as f:
        json.dump(api_responses, f, ensure_ascii=False, indent=2)

    print(f"✔ JSON capturado: {len(api_responses)} páginas")
    print("✔ Archivo generado: gastos_api.json")

if __name__ == "__main__":
    main()
