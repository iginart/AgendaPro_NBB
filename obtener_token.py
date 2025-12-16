from playwright.sync_api import sync_playwright
import json
import os
import time

# =========================
# CONFIG
# =========================

LOGIN_URL = "https://app.agendapro.com/sign_in"

USUARIO = os.environ.get("AGENDAPRO_USER")
PASSWORD = os.environ.get("AGENDAPRO_PASS")

if not USUARIO or not PASSWORD:
    raise Exception("❌ Faltan AGENDAPRO_USER / AGENDAPRO_PASS en variables de entorno")


# =========================
# MAIN
# =========================

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # LOGIN (idéntico al que funciona)
        page.goto(LOGIN_URL)
        page.get_by_role("textbox", name="usuario@correo.com").fill(USUARIO)
        page.get_by_role("textbox", name="Ingresa tu contraseña").fill(PASSWORD)
        page.get_by_role("button", name="Ingresar").click()

        # esperar sesión
        page.wait_for_timeout(4000)

        # Obtener cookies
        cookies = context.cookies()

        token = None

        for c in cookies:
            if c["name"] == "ap_cognito_authorization":
                token = c["value"]
                break

        if not token:
            raise Exception("❌ No se encontró ap_cognito_authorization")

        # Guardar token
        with open("token.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "authorization": token
                },
                f,
                indent=2
            )

        print("✅ Token generado correctamente")
        browser.close()


if __name__ == "__main__":
    main()
