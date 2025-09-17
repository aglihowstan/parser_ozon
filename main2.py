# bot_ozon.py

import os
import re
import time
import json
import random
import logging
import traceback
import html
import asyncio

from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Загрузка конфигурации / секретов ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в переменных окружения.")

# Можно добавить переменные: HEADLESS (да/нет), TIMEOUT, USER_AGENTS_LIST и т.п.
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

# --- Утилиты ---
def normalize_spaces(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip() if s else s

def is_ozon_product_url(url: str) -> bool:
    # Поддержка ozon.ru, ozon.by, и возможных вариантов www.
    return bool(re.match(r'^https?://(www\.)?(ozon\.ru|ozon\.by)/product/', url))

# --- Настройка драйвера ---
def setup_driver(headless: bool = True):
    chrome_options = Options()
    if headless:
        # Note: "new" headless mode может быть не поддержан в некоторых версиях ChromeDriver
        chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-extensions')
    chrome_options.add_argument('--disable-infobars')
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)

    # User agent ротация
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36'
    ]
    ua = random.choice(user_agents)
    chrome_options.add_argument(f'--user-agent={ua}')

    try:
        driver = webdriver.Chrome(options=chrome_options)
    except WebDriverException as e:
        logger.error(f"Не удалось инициализировать ChromeDriver: {e}")
        raise

    # Маскировка navigator.webdriver
    try:
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    except Exception as e:
        logger.debug(f"Не удалось скрыть webdriver property: {e}")

    return driver

# --- Обработка куки ---
def accept_cookies(driver, timeout: int = 6):
    logger.info("Пытаемся принять куки (если есть)...")
    cookie_xpaths = [
        "//button[contains(., 'Принять')]",
        "//button[contains(., 'Согласен')]",
        "//button[contains(., 'Accept')]",
        "//button[contains(@class,'cookie') or contains(@id,'cookie')]",
        "//div[contains(@class,'cookie')]//button"
    ]
    for xp in cookie_xpaths:
        try:
            btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xp)))
            if btn and btn.is_displayed():
                try:
                    driver.execute_script("arguments[0].click();", btn)
                except Exception:
                    btn.click()
                logger.info(f"Нажали куки-кнопку (xpath: {xp})")
                time.sleep(1)
                return True
        except Exception:
            continue
    logger.info("Кнопка куки не найдена.")
    return False

# --- Извлечение информации о товаре ---
def extract_product_info_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    result = {
        "title": None,
        "price": None,
        "currency": None,
        "image": None,
        "description": None,
        "rating": None,
        "ratingCount": None
    }

    # JSON-LD парсинг
    try:
        scripts = soup.find_all("script", type="application/ld+json")
        for s in scripts:
            try:
                j = json.loads(s.string or "{}")
            except Exception:
                continue
            items = j if isinstance(j, list) else [j]
            for it in items:
                if not isinstance(it, dict):
                    continue
                t = it.get("@type", "").lower()
                if t == "product" or "offers" in it:
                    if not result["title"] and it.get("name"):
                        result["title"] = normalize_spaces(it.get("name"))
                    img = it.get("image")
                    if img:
                        if isinstance(img, list):
                            result["image"] = img[0]
                        else:
                            result["image"] = img
                    if not result["description"] and it.get("description"):
                        result["description"] = normalize_spaces(it.get("description"))
                    offers = it.get("offers") or {}
                    if isinstance(offers, dict):
                        price = offers.get("price")
                        currency = offers.get("priceCurrency")
                        if price:
                            result["price"] = str(price)
                        if currency:
                            result["currency"] = currency
                    agg = it.get("aggregateRating")
                    if agg and isinstance(agg, dict):
                        result["rating"] = agg.get("ratingValue") or result["rating"]
                        result["ratingCount"] = agg.get("reviewCount") or result["ratingCount"]
    except Exception as e:
        logger.debug("Ошибка при JSON-LD парсинге", exc_info=True)

    # meta-теги
    if not result["image"]:
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            result["image"] = og_img["content"]
    if not result["description"]:
        og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        if og_desc and og_desc.get("content"):
            result["description"] = normalize_spaces(og_desc["content"])

    # цена из meta
    if not result["price"]:
        meta_price = soup.find("meta", attrs={"property": re.compile(r"product:price:amount", re.I)})
        if meta_price and meta_price.get("content"):
            result["price"] = meta_price["content"]
            meta_cur = soup.find("meta", attrs={"property": re.compile(r"product:price:currency", re.I)})
            if meta_cur and meta_cur.get("content"):
                result["currency"] = meta_cur["content"]

    # запасной вариант цены
    if not result["price"]:
        price_el = soup.find(attrs={"data-widget": re.compile(r".*price.*", re.I)})
        if not price_el:
            price_el = soup.find("div", class_=re.compile(r".*(price|cost).*", re.I))
        if price_el:
            txt = normalize_spaces(price_el.get_text(" ", strip=True))
            m = re.search(r"(\d{1,3}(?:[ \xa0]\d{3})*(?:[.,]\d+)?)[\s\xa0]*(₽|руб|RUB|\$|€)?", txt)
            if m:
                # Убираем пробелы и неразрывные пробелы
                price_raw = m.group(1).replace("\xa0", "").replace(" ", "")
                result["price"] = price_raw
                if m.group(2):
                    result["currency"] = m.group(2)

    # название товара запасное
    if not result["title"]:
        t = soup.find("h1")
        if t:
            result["title"] = normalize_spaces(t.get_text(" ", strip=True))

    # рейтинг запасной
    if not result["rating"]:
        rating_el = soup.find(attrs={"aria-label": re.compile(r".*(звез|star|рейтинг).*", re.I)})
        if rating_el and rating_el.has_attr("aria-label"):
            result["rating"] = normalize_spaces(rating_el["aria-label"])
        else:
            m = soup.find(string=re.compile(r"(\d[.,]?\d?)\s*(из\s*5|/5|★|звезд)", re.I))
            if m:
                mm = re.search(r"(\d[.,]?\d?)", m)
                result["rating"] = mm.group(1) if mm else None

    # картинка запасная
    if not result["image"]:
        imgs = soup.find_all("img")
        big_img = None
        max_len = 0
        for im in imgs:
            src = im.get("src") or im.get("data-src") or im.get("data-lazy")
            if src:
                if len(src) > max_len:
                    max_len = len(src)
                    big_img = src
        result["image"] = big_img

    # очистка строк
    for k, v in result.items():
        if isinstance(v, str):
            result[k] = normalize_spaces(v)

    return result

# --- Синхронный парсинг (для вызова из async) ---
def parse_product_sync(url: str, headless: bool = True):
    driver = setup_driver(headless=headless)
    try:
        logger.info(f"Открываем URL: {url}")
        driver.get(url)

        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)  # небольшая задержка для JS

        accept_cookies(driver)

        # Прокрутка для загрузки элементов, подгрузки ленивых секций
        driver.execute_script("window.scrollTo(0, 400);")
        time.sleep(1)

        html_source = driver.page_source

        product = extract_product_info_from_html(html_source)
        logger.info(f"Parsed: title={product.get('title')}, price={product.get('price')}")

        return product

    except Exception as e:
        logger.error(f"Ошибка при парсинге: {e}")
        logger.error(traceback.format_exc())
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass

# --- Handlers Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне ссылку на товар с Ozon (.ru или .by), и я покажу информацию о нём."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    if not is_ozon_product_url(url):
        await update.message.reply_text("Пожалуйста, отправьте корректную ссылку на товар Ozon (ozon.ru или ozon.by).")
        return

    await update.message.reply_text("Парсим информацию о товаре, подождите...")

    try:
        # Запускаем синхронную функцию парсинга в фоне
        product = await asyncio.to_thread(parse_product_sync, url, HEADLESS)

        if not product:
            await update.message.reply_text("Не удалось распарсить товар — попробуйте позже.")
            return

        # Формируем сообщение
        title = html.escape(product.get("title") or "—")
        price = html.escape(str(product.get("price") or "—"))
        currency = html.escape(str(product.get("currency") or ""))
        rating = html.escape(str(product.get("rating") or "—"))
        ratingCount = html.escape(str(product.get("ratingCount") or "—"))
        description = html.escape(product.get("description") or "—")

        caption = (
            f"🏷 <b>{title}</b>\n"
            f"💰 Цена: {price} {currency}\n"
            f"⭐ Рейтинг: {rating} ({ratingCount} оценок)\n\n"
            f"📝 Описание: {description[:300]}..."
        )

        # Если есть картинка — отправляем фото, иначе просто текст
        if product.get("image"):
            await update.message.reply_photo(
                photo=product["image"],
                caption=caption,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(caption, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Ошибка в обработчике сообщения: {e}")
        logger.error(traceback.format_exc())
        await update.message.reply_text("Произошла ошибка при обработке — попробуйте ещё раз через некоторое время.")

# --- Главная функция ---
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен.")
    application.run_polling()

if __name__ == "__main__":
    main()



