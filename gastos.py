import requests
import json
import os
from datetime import datetime, timedelta

# =========================
# CONFIGURACIÓN
# =========================
BASE_URL = "https://agendapro.com"
LOGIN_URL = f"{BASE_URL}/authentication/sign_in"
API_URL = f"{BASE_URL}/api/views/admin/v1/payments/sales_cash_content_expenses"

USERNAME = os.environ.get("AGENDAPRO_USER")
PASSWORD = os.environ.get("AGENDAPRO_PASS")

IDS = "311450,441684,438946,433116,297091,433118,355874,182223,352403,352405,42969,25626,80886,106380,297090"

# Últimos 5 días
to_date = datetime.today()
from_date = to_date - timedelta(days=5)

PARAMS_BASE = {
    "sales_cash": "true",
    "from": from_date.strftime("%d-%m-%Y"),
    "to": to_date.strftime("%d-%m-%Y"),
    "ids": IDS
}

# =========================
# SESIÓN
# =========================
session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json"
})

# =========================
# LOGIN
# =========================
login_payload = {
    "username": USERNAME,
    "password": PASSWORD
}

login_resp = session.post(LOGIN_URL, json=login_payload)

if login_resp.status_code != 200:
    raise Exception("Error al loguearse en AgendaPro")

# =========================
# PRIMERA PÁGINA (para saber cuántas hay)
# =========================
params = PARAMS_BASE.copy()
params["page"] = 1

resp = session.get(API_URL, params=params)

if resp.status_code != 200:
    raise Exception("Error al consultar API")

data = resp.json()
max_pages = data.get("page_number", 1)

all_expenses = data.get("sales_cash_expenses", [])

# =========================
# RESTO DE PÁGINAS
# =========================
for page in range(2, max_pages + 1):
    params["page"] = page
    r = session.get(API_URL, params=params)
    r.raise_for_status()
    page_data = r.json()
    all_expenses.extend(page_data.get("sales_cash_expenses", []))

# =========================
# GUARDAR ARCHIVO
# =========================
output = {
    "fecha_ejecucion": datetime.utcnow().isoformat(),
    "from": PARAMS_BASE["from"],
    "to": PARAMS_BASE["to"],
    "total_registros": len(all_expenses),
    "data": all_expenses
}

with open("gastos.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"Descargados {len(all_expenses)} gastos")
