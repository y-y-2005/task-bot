"""
manabaから課題一覧をスクレイピングする
- ログイン失敗時は ValueError("login_failed") を送出
- それ以外の例外は呼び出し元に伝播させる
"""

import re
from datetime import date
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

LOGIN_URL  = "https://cit.manaba.jp/ct/login"
REPORT_URL = "https://cit.manaba.jp/ct/home_report"


def _parse_deadline(text: str) -> str:
    """日付文字列から YYYY-MM-DD を抽出する。見つからなければ空文字。"""
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _is_urgent(deadline_str: str) -> bool:
    if not deadline_str:
        return False
    try:
        d    = date.fromisoformat(deadline_str)
        diff = (d - date.today()).days
        return diff <= 3
    except ValueError:
        return False


def _build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def get_manaba_tasks(user_id: str, password: str) -> list[dict]:
    """
    manabaにログインして課題一覧を返す。

    Returns:
        [{"title": str, "deadline": "YYYY-MM-DD"|"", "channel": 科目名,
          "source": "manaba", "urgent": bool}, ...]

    Raises:
        ValueError("login_failed"): 認証失敗
        Exception: その他のエラー（ネットワーク障害など）
    """
    driver = _build_driver()
    wait   = WebDriverWait(driver, 15)

    try:
        # ── ログイン ──────────────────────────────────────────
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.NAME, "userid")))

        driver.find_element(By.NAME, "userid").send_keys(user_id)
        driver.find_element(By.NAME, "password").send_keys(password)
        driver.find_element(
            By.CSS_SELECTOR, "input[type='submit'], button[type='submit']"
        ).click()

        # URL が変わらなければタイムアウト → ログイン失敗と見なす
        try:
            wait.until(EC.url_changes(LOGIN_URL))
        except TimeoutException:
            raise ValueError("login_failed")

        if "login" in driver.current_url:
            raise ValueError("login_failed")

        # ── 課題一覧ページ ────────────────────────────────────
        driver.get(REPORT_URL)

        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "table.stdlist, .list-report")
                )
            )
        except TimeoutException:
            # ページが取れても課題ゼロの場合はテーブルなし
            return []

        tasks = []
        rows  = driver.find_elements(
            By.CSS_SELECTOR, "table.stdlist tbody tr, table.stdlist tr"
        )

        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 2:
                continue

            course   = cells[0].text.strip()
            title    = cells[1].text.strip()
            deadline = ""

            # 3列目以降から日付を探す
            for cell in cells[2:]:
                parsed = _parse_deadline(cell.text)
                if parsed:
                    deadline = parsed
                    break

            if not title or not course:
                continue

            tasks.append({
                "title":    title,
                "deadline": deadline,
                "channel":  course,
                "source":   "manaba",
                "urgent":   _is_urgent(deadline),
            })

        return tasks

    finally:
        driver.quit()
