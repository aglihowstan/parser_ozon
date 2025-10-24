import asyncio
import logging
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import json
import re
import random
from urllib.parse import quote
from playwright.async_api import async_playwright
import os
from datetime import datetime
import tempfile
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("ozon_telegram_bot")
TELEGRAM_BOT_TOKEN = "7572687386:AAEfd9tIcg5jpfXJYBxy-06XE1jvliInbV4"
MAX_PRODUCTS = 15
PAGES_TO_PARSE = 2
application = None
user_states = {}


class OzonParser:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None

    async def human_delay(self, min_sec=1, max_sec=3):
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def setup_browser(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=VizDisplayCompositor"
            ],
            slow_mo=50
        )

        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            java_script_enabled=True,
            ignore_https_errors=True
        )

        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)

        self.page = await self.context.new_page()
        self.page.set_default_timeout(15000)
        self.page.set_default_navigation_timeout(20000)

    async def close_browser(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def fetch_product_links(self, query, pages=2):
        try:
            encoded_query = quote(query)
            search_url = f"https://www.ozon.ru/search/?text={encoded_query}&from_global=true"

            logger.info(f"Поиск: {query}")
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            await self.human_delay(2, 3)

            current_url = self.page.url
            if "category" in current_url and "text" not in current_url:
                logger.warning("Перенаправление на категорию")
                search_url = f"https://www.ozon.ru/search/?text={encoded_query}"
                await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                await self.human_delay(2, 3)

            selectors_to_wait = [
                "[data-widget='searchResults']",
                ".widget-search-result-container",
                ".search-container",
                ".tile-root",
                "div[data-widget*='search']",
                ".a0c6"
            ]

            for selector in selectors_to_wait:
                try:
                    await self.page.wait_for_selector(selector, timeout=5000)
                    break
                except:
                    continue

        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            return []

        links = set()
        for p in range(1, pages + 1):
            logger.info(f"Страница {p}/{pages}")

            try:
                await self.page.wait_for_selector("a[href*='/product/']", timeout=10000)

                product_selectors = [
                    "a[href*='/product/']",
                    ".tile-root a[href*='/product/']",
                    "[data-widget*='searchResults'] a[href*='/product/']",
                    "div a[href*='/product/']"
                ]

                elements = []
                for selector in product_selectors:
                    found_elements = await self.page.query_selector_all(selector)
                    elements.extend(found_elements)
                    if found_elements:
                        break

                seen_hrefs = set()
                unique_elements = []
                for e in elements:
                    href = await e.get_attribute("href")
                    if href and href not in seen_hrefs:
                        seen_hrefs.add(href)
                        unique_elements.append(e)

                elements = unique_elements

                for e in elements:
                    href = await e.get_attribute("href")
                    if href and "/product/" in href:
                        full_url = "https://www.ozon.ru" + href.split("?")[0] if href.startswith("/") else href
                        if "product" in full_url.lower() and len(full_url) > 30:
                            links.add(full_url)

                logger.info(f"Найдено ссылок: {len(links)}")

                if p < pages:
                    next_selectors = [
                        "a[aria-label*='Следующая']",
                        "a[data-widget*='paginator-next']",
                        "[data-widget='paginator'] a:last-child",
                        ".paginator .next",
                        "a:has-text('Далее')",
                    ]

                    next_btn = None
                    for selector in next_selectors:
                        next_btn = await self.page.query_selector(selector)
                        if next_btn:
                            break

                    if not next_btn:
                        break

                    await next_btn.scroll_into_view_if_needed()
                    await self.human_delay(1, 2)
                    await next_btn.click()
                    await self.human_delay(3, 4)

            except Exception as e:
                logger.warning(f"Ошибка на странице {p}: {e}")
                continue

        filtered_links = [link for link in links if "ozon.ru" in link and "/product/" in link]
        logger.info(f"Всего товаров: {len(filtered_links)}")
        return filtered_links

    async def parse_price(self, content):
        price = None

        try:
            json_patterns = [
                r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
                r'<script[^>]*data-widget[^>]*>([^<]*)</script>',
            ]

            for pattern in json_patterns:
                matches = re.findall(pattern, content, re.DOTALL)
                for match in matches:
                    try:
                        if isinstance(match, str) and match.startswith('{'):
                            data = json.loads(match)

                            def find_price(obj):
                                if isinstance(obj, dict):
                                    for key, value in obj.items():
                                        if key in ['price', 'currentPrice', 'finalPrice', 'amount'] and value:
                                            if isinstance(value, (int, float)):
                                                return int(value)
                                            elif isinstance(value, str) and value.isdigit():
                                                return int(value)
                                        if isinstance(value, (dict, list)):
                                            result = find_price(value)
                                            if result:
                                                return result
                                elif isinstance(obj, list):
                                    for item in obj:
                                        result = find_price(item)
                                        if result:
                                            return result
                                return None

                            found_price = find_price(data)
                            if found_price:
                                price = found_price
                                break

                    except:
                        continue

            if not price:
                price_selectors = [
                    "[data-widget='webPrice']",
                    "[data-widget='price']",
                    ".price",
                    "[class*='price']",
                    ".c3118",
                    ".yo3",
                ]

                for selector in price_selectors:
                    try:
                        price_elem = await self.page.query_selector(selector)
                        if price_elem:
                            price_text = await price_elem.text_content()
                            if price_text:
                                price_match = re.search(r'(\d[\d\s]*)\s*[₽ррубRUB]', price_text.replace(',', ''))
                                if price_match:
                                    price_str = price_match.group(1).replace(' ', '').replace('\u2009', '').replace(
                                        '\xa0', '')
                                    if price_str.isdigit():
                                        price = int(price_str)
                                        break
                    except:
                        continue

        except Exception as e:
            logger.warning(f"Ошибка парсинга цены: {e}")

        return price

    async def parse_rating_and_reviews(self):
        """Улучшенный парсинг рейтинга и количества отзывов с реальными селекторами Ozon"""
        rating = None
        reviews = None

        try:
            await self.human_delay(2, 3)
            rating_selectors = [
                "span[data-widget='webReviewRating']",
                "div[data-widget='webReviewRating']",
                "[class*='rating']",
                "[class*='Rating']",
                ".a0c8",
                ".a2a0",
                "div > span:has-text('₽') + div span",
                "text=₽ >> xpath=following::span[contains(., '.')]",
            ]

            reviews_selectors = [
                "span[data-widget='webReviewCount']",
                "a[href*='reviews'] span",
                "[class*='review-count']",
                "[class*='reviewCount']",
                ".a0c9",
                ".a2a1",
                "text=₽ >> xpath=following::a[contains(@href, 'reviews')]",
            ]
            for selector in rating_selectors:
                try:
                    rating_elem = await self.page.query_selector(selector)
                    if rating_elem:
                        rating_text = await rating_elem.text_content()
                        if rating_text:
                            logger.info(f"Найден текст рейтинга: '{rating_text}'")
                            rating_match = re.search(r'(\d+\.\d+|\d+)', rating_text.replace(',', '.'))
                            if rating_match:
                                rating_val = rating_match.group(1)
                                try:
                                    rating = float(rating_val)
                                    logger.info(f"Найден рейтинг через селектор {selector}: {rating}")
                                    break
                                except ValueError:
                                    continue
                except Exception as e:
                    continue

            if rating is None:
                try:
                    content = await self.page.content()
                    rating_patterns = [
                        r'"rating":\s*["]?(\d+\.\d+|\d+)["]?',
                        r'"ratingValue":\s*["]?(\d+\.\d+|\d+)["]?',
                        r'"averageRating":\s*["]?(\d+\.\d+|\d+)["]?',
                        r'рейтинг[^"]*?(\d+\.\d+|\d+)',
                        r'rating[^"]*?(\d+\.\d+|\d+)',
                    ]

                    for pattern in rating_patterns:
                        matches = re.findall(pattern, content, re.IGNORECASE)
                        for match in matches:
                            try:
                                rating_candidate = float(match)
                                if 1 <= rating_candidate <= 5:
                                    rating = rating_candidate
                                    logger.info(f"Найден рейтинг через regex: {rating}")
                                    break
                            except ValueError:
                                continue
                        if rating:
                            break
                except Exception as e:
                    logger.warning(f"Ошибка при поиске рейтинга в тексте: {e}")
            for selector in reviews_selectors:
                try:
                    reviews_elem = await self.page.query_selector(selector)
                    if reviews_elem:
                        reviews_text = await reviews_elem.text_content()
                        if reviews_text:
                            logger.info(f"Найден текст отзывов: '{reviews_text}'")
                            patterns = [
                                r'(\d+[\d\s]*)\s*(отзыв|review)',
                                r'(\d+[\d\s]*)',
                            ]

                            for pattern in patterns:
                                reviews_match = re.search(pattern, reviews_text, re.IGNORECASE)
                                if reviews_match:
                                    reviews_str = reviews_match.group(1).replace(' ', '').replace('\u2009', '').replace(
                                        '\xa0', '')
                                    if reviews_str.isdigit():
                                        reviews = int(reviews_str)
                                        logger.info(f"Найдены отзывы через селектор {selector}: {reviews}")
                                        break
                except Exception as e:
                    continue
            if reviews is None:
                try:
                    content = await self.page.content()
                    reviews_patterns = [
                        r'"reviewCount":\s*["]?(\d+)["]?',
                        r'"reviewsCount":\s*["]?(\d+)["]?',
                        r'"review_count":\s*["]?(\d+)["]?',
                        r'отзыв[ов]*[^"]*?(\d+)',
                        r'review[s]*[^"]*?(\d+)',
                    ]

                    for pattern in reviews_patterns:
                        matches = re.findall(pattern, content, re.IGNORECASE)
                        for match in matches:
                            if match.isdigit():
                                reviews_candidate = int(match)
                                if reviews_candidate > 0:
                                    reviews = reviews_candidate
                                    logger.info(f"Найдены отзывы через regex: {reviews}")
                                    break
                        if reviews:
                            break
                except Exception as e:
                    logger.warning(f"Ошибка при поиске отзывов в тексте: {e}")
            if rating is None:
                try:
                    possible_rating_elements = await self.page.query_selector_all("span, div, button")
                    for elem in possible_rating_elements:
                        try:
                            text = await elem.text_content()
                            if text:
                                rating_match = re.search(r'^\d+\.\d+$', text.strip())
                                if rating_match:
                                    rating = float(rating_match.group())
                                    logger.info(f"Найден рейтинг по формату: {rating}")
                                    break
                        except:
                            continue
                except Exception as e:
                    logger.warning(f"Ошибка при дополнительном поиске рейтинга: {e}")

        except Exception as e:
            logger.warning(f"Ошибка парсинга рейтинга и отзывов: {e}")

        return rating, reviews

    async def parse_product(self, url):
        try:
            logger.info(f"Парсим товар: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await self.human_delay(2, 3)
            try:
                await self.page.wait_for_selector("h1", timeout=10000)
            except:
                logger.warning(f"заголовок не найден: {url}")

            content = await self.page.content()
            title_elem = await self.page.query_selector("h1")
            title = await title_elem.text_content() if title_elem else "Неизвестно"

            price = await self.parse_price(content)
            rating, reviews = await self.parse_rating_and_reviews()
            formatted_price = f"{price} ₽" if price is not None else None

            logger.info(
                f"Результат парсинга: {title[:30]}... | Цена: {formatted_price} | Рейтинг: {rating} | Отзывы: {reviews}")

            return {
                "Название": title.strip(),
                "Цена": formatted_price,
                "Рейтинг": rating,
                "Количество отзывов": reviews,
                "Ссылка": url
            }

        except Exception as e:
            logger.warning(f"Ошибка парсинга {url}: {e}")
            return {
                "Название": "Ошибка парсинга",
                "Цена": None,
                "Рейтинг": None,
                "Количество отзывов": None,
                "Ссылка": url
            }

    async def search_products(self, query, pages=2, max_products=15):
        try:
            await self.setup_browser()
            links = await self.fetch_product_links(query, pages)

            if not links:
                return None, "не найдено товаров по вашему запросу"

            results = []
            max_products = min(max_products, len(links))

            for i, url in enumerate(links[:max_products], 1):
                logger.info(f"[{i}/{max_products}] Парсим товар")
                product = await self.parse_product(url)
                results.append(product)

                if i < max_products:
                    await self.human_delay(1, 2)

            df = pd.DataFrame(results)

            total = len(df)
            with_prices = df['Цена'].notna().sum()
            with_ratings = df['Рейтинг'].notna().sum()
            with_reviews = df['Количество отзывов'].notna().sum()

            stats_message = (
                f"📊 Статистика парсинга:\n"
                f"• Всего товаров: {total}\n"
                f"• С ценами: {with_prices}\n"
                f"• С рейтингами: {with_ratings}\n"
                f"• С отзывами: {with_reviews}\n"
                f"• Запрос: '{query}'"
            )

            return df, stats_message

        except Exception as e:
            logger.error(f"Критическая ошибка: {e}")
            return None, f"Произошла ошибка при парсинге: {str(e)}"
        finally:
            await self.close_browser()
ozon_parser = OzonParser()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "Бот для парсинга товаров с Ozon.\n\n"
        "Отправьте мне название товара или категорию, например:\n"
        "• 'ноутбук'\n"
        "• 'крем для лица'\n"
        "• 'игровая мышь'\n"
        "• 'телефон samsung'\n\n"
        "Я найду товары и пришлю вам CSV файл с результатами!\n\n"
        "**в результатах:**\n"
        "• Название товара\n"
        "• Цена в рублях\n"
        "• Рейтинг товара\n"
        "• Количество отзывов\n"
        "• Ссылка на товар\n\n"
        "Парсинг занимает 1-3 минуты..."
    )

    await update.message.reply_text(welcome_message)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    user_message = update.message.text.strip()

    if user_message.startswith('/'):
        return

    if len(user_message) < 2:
        await update.message.reply_text("Пожалуйста, введите более конкретный запрос (минимум 2 символа)")
        return

    try:
        progress_message = await update.message.reply_text(
            f"Ищу товары по запросу: '{user_message}'\n"
            f"Это займет 1-3 минуты..."
        )

        df, stats_message = await ozon_parser.search_products(
            query=user_message,
            pages=PAGES_TO_PARSE,
            max_products=MAX_PRODUCTS
        )

        if df is not None and len(df) > 0:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8-sig') as tmp_file:
                df.to_csv(tmp_file.name, index=False, encoding='utf-8-sig')
                tmp_filename = tmp_file.name

            try:
                await update.message.reply_text(stats_message)

                with open(tmp_filename, 'rb') as file:
                    await update.message.reply_document(
                        document=file,
                        filename=f"ozon_{user_message}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                        caption=f"Файл с результатами по запросу: '{user_message}'"
                    )

                top_products = df.head(3)
                preview_message = "топ 3 товара:\n\n"

                for i, (_, row) in enumerate(top_products.iterrows(), 1):
                    price_str = row['Цена'] if pd.notna(row['Цена']) else "Цена не указана"
                    rating_str = f"{row['Рейтинг']}" if pd.notna(row['Рейтинг']) else " Нет рейтинга"
                    reviews_str = f"{row['Количество отзывов']} отзывов" if pd.notna(
                        row['Количество отзывов']) else "Нет отзывов"

                    preview_message += (
                        f"{i}. {row['Название'][:50]}...\n"
                        f"   {price_str} | {rating_str} | {reviews_str}\n\n"
                    )

                await update.message.reply_text(preview_message)

            finally:
                os.unlink(tmp_filename)

        else:
            await update.message.reply_text(stats_message)

    except Exception as e:
        logger.error(f"Ошибка в обработчике сообщения: {e}")
        await update.message.reply_text("Произошла непредвиденная ошибка. Попробуйте позже.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = (
        "ℹ️ Помощь по использованию бота:\n\n"
        "🔍 **Как использовать:**\n"
        "1. Просто отправьте название товара или категории\n"
        "2. Бот найдет товары на Ozon\n"
        "3. Вы получите CSV файл с результатами\n\n"
        "📊 **Что входит в результаты:**\n"
        "• **Название товара** - полное наименование\n"
        "• **Цена** - в российских рублях (₽)\n"
        "• **Рейтинг** - оценка товара от покупателей\n"
        "• **Количество отзывов** - сколько отзывов оставлено\n"
        "• **Ссылка на товар** - прямая ссылка на Ozon\n\n"
        "⏱️ **Время выполнения:** 1-3 минуты\n"
        "📦 **Количество товаров:** до 15\n\n"
        "💡 **Примеры запросов:**\n"
        "• 'ноутбук asus'\n"
        "• 'крем для рук'\n"
        "• 'смартфон'\n"
        "• 'наушники беспроводные'\n\n"
        "💰 **Цены отображаются в рублях (₽)**"
    )

    await update.message.reply_text(help_message)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")


def signal_handler(sig, frame):
    """Обработчик сигналов для graceful shutdown"""
    logger.info("Получен сигнал завершения...")
    if application:
        application.stop()
    sys.exit(0)


def main():
    """Основная функция запуска бота"""
    global application
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        logger.info("Бот запущен")
        application.run_polling()

    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
    finally:
        logger.info("Бот остановлен")


if __name__ == "__main__":
    main()