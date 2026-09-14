import os
import time
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
                    "AgendaPro no genero la cookie "
                    "ap_cognito_authorization"
                )

            print("LOGIN OK")

        except Exception:
            print("FALLO EL LOGIN")
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
                print(
                    "Screenshot guardado: "
                    "agendapro-login-error.png"
                )
            except Exception as e:
                print("No se pudo guardar screenshot:", e)

            try:
                with open(
                    "agendapro-login-error.html",
                    "w",
                    encoding="utf-8"
                ) as f:
                    f.write(page.content())

                print(
                    "HTML guardado: "
                    "agendapro-login-error.html"
                )
            except Exception as e:
                print("No se pudo guardar HTML:", e)

            raise

        finally:
            browser.close()


if __name__ == "__main__":
    main()
