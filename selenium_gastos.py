import os
import json
import time
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# =========================
# CONFIG
# =========================
USERNAME = os.environ.get("AGENDAPRO_USER")
PASSWORD = os.environ.get("AGENDAPRO_PASS")

LOGIN_URL = "https://app.agendapro.com/"
API_URL = "https://app.agendapro.com/api/views/admin/v1/payments/sales_cash_content_expenses"

IDS = "311450,441684,438946,433116,297091,433118,355874,182223,352403,352405,42969,25626,80886,106380,297090"

to_date = datetime.today()
from_date = to_date - timedelta(days=5)

# =========================
# SELENIUM OPTIONS
# =========================
chrome_options = Options()
chrome_options.add_argument("--headless=new")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

driver = webdriver.Chrome(options=chrome_options)
wait = WebDriverWait(driver, 20)

# =========================
# LOGIN
# =========================
driver.get(LOGIN_URL)

email_input = wait.until(EC.presence_of_element_located((By.NAME, "username")))
password_input = driver.find_element(By.NAME, "password")

email_input.send_keys(USERNAME)
password_input.send_keys(PASSWORD)
password_input.send_keys(Keys.ENTER)

# Esperar que la app cargue
wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
time.sleep(5)

# =========================
# FETCH API DESDE EL BROWSER
# =========================
script = f"""
return fetch("{API_URL}?sales_cash=true&from={from_date.strftime('%d-%m-%Y')}&to={to_date.strftime('%d-%m-%Y')}&ids={IDS}&page=1")
  .then(r => r.json());
"""

data = driver.execute_script(script)

# =========================
# GUARDAR JSON
# =========================
output = {
    "fecha_ejecucion": datetime.utcnow().isoformat(),
    "data": data
}

with open("gastos.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

driver.quit()

print("Gastos descargados vía Selenium")
