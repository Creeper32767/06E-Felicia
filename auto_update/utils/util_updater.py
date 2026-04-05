from functools import lru_cache

import requests
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

__all__ = ["fetch_live_data", "fetch_fallback_data"]

DATA_SOURCE_URL = "https://www.smca.fun/#/"
FALLBACK_DATA_SOURCE_URL = "https://uapis.cn/api/v1/misc/weather?adcode=440300&extended=true&forecast=true"
TARGETS = {
    "t": "分钟气温",
    "th": "最高气温",
    "tl": "最低气温",
    "r_day": "日雨量",
    "r_1h": "1h滑动雨量",
    "p": "海平面气压",
    "time": "更新时间"
}
HTTP_TIMEOUT = 15
PAGE_TIMEOUT = 120


def _create_retry_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_fallback_data(logger):
    try:
        with _create_retry_session() as session:
            response = session.get(FALLBACK_DATA_SOURCE_URL, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            weather_data = response.json()
            return {
                "t": weather_data.get("temperature", ""),
                "th": weather_data.get("temp_max", ""),
                "tl": weather_data.get("temp_min", ""),
                "r_day": "---",
                "r_1h": weather_data.get("precipitation", ""),
                "p": weather_data.get("pressure", ""),
                "time": weather_data.get("report_time", "")
            }
    except Exception as e:
        logger.log_error("autoupdate.fetch_fallback_data", str(e))
    return {key: "---" for key in TARGETS}


def _safe_find_text(element, class_name):
    try:
        return element.find_element(By.CLASS_NAME, class_name).text.strip()
    except Exception:
        return ""


def _extract_live_data(items, logger):
    data = {}
    label_to_key = {label: key for key, label in TARGETS.items()}
    remaining_targets = set(TARGETS.keys())

    for item in items:
        try:
            label = _safe_find_text(item, "stat-label")
            target_key = label_to_key.get(label)
            if not target_key or target_key not in remaining_targets:
                continue

            value = _safe_find_text(item, "stat-value") or _safe_find_text(item, "stat-number")
            if not value:
                continue

            data[target_key] = value
            remaining_targets.discard(target_key)
            if not remaining_targets:
                break
        except Exception as e:
            logger.log_error("autoupdate.extract_live_data", str(e))
    return data


@lru_cache(maxsize=1)
def _get_chromedriver_path():
    path = Path(ChromeDriverManager().install())
    if not path.exists():
        raise FileNotFoundError(f"Chromedriver not found at: {path}")
    return str(path)


def _build_driver(options, logger):
    # Prefer Selenium Manager to avoid external chromedriver cache issues.
    try:
        return webdriver.Chrome(options=options)
    except Exception as e:
        logger.log_error("autoupdate.init_driver.selenium_manager", str(e))

    try:
        return webdriver.Chrome(service=Service(_get_chromedriver_path()), options=options)
    except FileNotFoundError:
        _get_chromedriver_path.cache_clear()
        return webdriver.Chrome(service=Service(_get_chromedriver_path()), options=options)


def fetch_live_data(logger):
    options = Options()
    options.page_load_strategy = "eager"
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = None
    
    try:
        driver = _build_driver(options, logger)
        driver.set_page_load_timeout(PAGE_TIMEOUT)
        driver.set_script_timeout(PAGE_TIMEOUT)
        driver.get(DATA_SOURCE_URL)
        wait = WebDriverWait(driver, PAGE_TIMEOUT)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "stat-item")))

        items = driver.find_elements(By.CLASS_NAME, "stat-item")
        print(f"Found {len(items)} stat items on the page.")
        res = _extract_live_data(items, logger)

        if not res:
            logger.log_error("autoupdate.fetch_smca", "No live data extracted from page")
        return res
    except Exception as e:
        page_snapshot = ""
        try:
            if driver is not None:
                page_snapshot = driver.page_source[:1000]
        except Exception:
            pass
        logger.log_error("autoupdate.fetch_smca", str(e), f"page_snapshot: {page_snapshot}")
        return None
    finally:
        if driver is not None:
            driver.quit()
