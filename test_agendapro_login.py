import os
import time
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://app.agendapro.com/sign_in"

USUARIO = os.environ["AGENDAPRO_USER"]
PASSWORD = os.environ["AGENDAPRO_PASSWORD"]

COOKIES_CLAVE = {
    "ap_cognito_authorization",
    "_agendapro_session",
    "cognito_refresh_token",
    "cognito_last_auth",
}

# Para esta prueba usamos solo Saavedra
LOCATION_ID = "352403"


def main():
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
            print("Abriendo AgendaPro...")

            page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            print("Pagina de login cargada")

            page.get_by_placeholder("user@example.com").fill(USUARIO)
            page.get_by_placeholder("Enter your password").fill(PASSWORD)
            page.get_by_role("button", name="Log in").click()

            print("Login enviado. Esperando autenticacion...")

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

            print("URL final:", page.url)
            print(
                "Cookies encontradas:",
                ", ".join(sorted(encontradas.keys()))
                if encontradas
                else "ninguna"
            )

            if "ap_cognito_authorization" not in encontradas:
                raise RuntimeError(
                    "AgendaPro no genero la cookie ap_cognito_authorization"
                )

            print("LOGIN OK")

            # --------------------------------------------------
            # PRUEBA REAL CONTRA EL ENDPOINT QUE USA QLIK
            # --------------------------------------------------

            cookie_header = "; ".join(
                f"{nombre}={valor}"
                for nombre, valor in encontradas.items()
            )

            hoy = datetime.now().strftime("%d-%m-%Y")

            url = (
                "https://agendapro.com/api/views/admin/v1/payments"
                f"?from={hoy}"
                f"&to={hoy}"
                "&per_page=10"
                f"&location_ids={LOCATION_ID}"
                "&page=1"
            )

            print("Probando endpoint payments...")
            print("Fecha:", hoy)
            print("Local:", LOCATION_ID)

            response = requests.get(
                url,
                headers={
                    "Cookie": cookie_header,
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                },
                timeout=60,
            )

            print("HTTP status:", response.status_code)

            if response.status_code != 200:
                print("Respuesta:")
                print(response.text[:2000])
                raise RuntimeError(
                    f"El endpoint payments devolvio HTTP {response.status_code}"
                )

            data = response.json()

            print("API OK")
            print("Tipo de respuesta:", type(data).__name__)

            if isinstance(data, dict):
                print("Claves raiz:", ", ".join(data.keys()))

                pages = data.get("pages")
                payments = data.get("payments")

                print("Paginas:", pages)

                if isinstance(payments, list):
                    print("Cantidad de pagos en pagina 1:", len(payments))
                else:
                    print("payments no es una lista o no esta presente")

            print("PRUEBA COMPLETA OK")

        except Exception:
            print("FALLO LA PRUEBA")
            print("URL actual:", page.url)

            try:
                print("Titulo:", page.title())
            except Exception:
                pass

            cookies = context.cookies()
            print(
                "Nombres de cookies presentes:",
                ", ".join(sorted(c["name"] for c in cookies))
                if cookies
                else "ninguna"
            )

            try:
                page.screenshot(
                    path="agendapro-login-error.png",
                    full_page=True
                )
                print("Screenshot guardado: agendapro-login-error.png")
            except Exception as e:
                print("No se pudo guardar screenshot:", e)

            try:
                with open(
                    "agendapro-login-error.html",
                    "w",
                    encoding="utf-8"
                ) as f:
                    f.write(page.content())

                print("HTML guardado: agendapro-login-error.html")
            except Exception as e:
                print("No se pudo guardar HTML:", e)

            raise

        finally:
            browser.close()


if __name__ == "__main__":
    main()
