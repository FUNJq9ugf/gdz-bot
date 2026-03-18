from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from dataclasses import dataclass
from html import escape

from dotenv import load_dotenv
from telegram import InputFile, InputMediaPhoto, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from scraper import FourBookScraper, ScraperError, SolutionImage, SolutionResult, TaskEntry


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+|4book\.org/\S+", re.IGNORECASE)
DEFAULT_BOOK_URL = (
    "https://4book.org/gdz-reshebniki-ukraina/10-klas/"
    "reshebnik-algebra-10-klas-merzlyak-2018-gdz"
)
UKR_MOVA_BOOK_URL = "https://4book.org/gdz-reshebniki-ukraina/10-klas/gdz-ukrayinska-mova-10-klas-avramenko-2018"
BOOK_ALIASES = {
    "algebra": "Алгебра 10 клас",
    "mova": "Українська мова 10 клас",
}

BOOK_ALIASES = {
    "algebra": "Алгебра",
    "mova": "Українська мова",
}
SUBJECT_BUTTON_TO_KEY = {value: key for key, value in BOOK_ALIASES.items()}


@dataclass(slots=True)
class BotConfig:
    token: str
    book_url: str
    ukr_mova_book_url: str
    webhook_url: str
    port: int
    webhook_path: str


class TaskResolver:
    def __init__(self, scraper: FourBookScraper, book_url: str) -> None:
        self.scraper = scraper
        self.book_url = book_url
        self._index: dict[str, TaskEntry] = {}
        self._lock = asyncio.Lock()

    async def ensure_index(self, force: bool = False) -> int:
        async with self._lock:
            if self._index and not force:
                return self.unique_count

            self._index = await asyncio.to_thread(self.scraper.build_task_index, self.book_url)
            return self.unique_count

    async def find(self, query: str) -> TaskEntry | None:
        await self.ensure_index()
        normalized = self.scraper.normalize_task_label(query)
        keys = {
            normalized,
            normalized.replace(" ", ""),
            normalized.replace("(", "").replace(")", ""),
        }
        for key in keys:
            entry = self._index.get(key)
            if entry:
                return entry
        return None

    async def find_many(self, query: str) -> list[TaskEntry]:
        await self.ensure_index()
        normalized = self.scraper.normalize_task_label(query)
        bounds = parse_task_range(normalized)
        if not bounds:
            entry = await self.find(query)
            return [entry] if entry else []

        start, end = bounds
        matches: list[tuple[tuple[int, ...], TaskEntry]] = []
        seen_urls: set[str] = set()
        for entry in self._index.values():
            if entry.page_url in seen_urls:
                continue
            entry_start, entry_end = entry_task_bounds(entry.label)
            if entry_start is None or entry_end is None:
                continue
            if entry_end < start or entry_start > end:
                continue
            seen_urls.add(entry.page_url)
            matches.append((entry_start, entry))

        matches.sort(key=lambda item: item[0])
        return [entry for _, entry in matches]

    async def find_many_by_page(self, query: str) -> list[TaskEntry]:
        await self.ensure_index()
        page_number = extract_page_number(query)
        if page_number is None:
            return []

        matches: list[TaskEntry] = []
        seen_urls: set[str] = set()
        for entry in self._index.values():
            if entry.page_url in seen_urls:
                continue
            entry_page = extract_page_number(entry.label)
            if entry_page != page_number:
                continue
            seen_urls.add(entry.page_url)
            matches.append(entry)

        matches.sort(key=lambda item: item.label)
        return matches

    @property
    def unique_count(self) -> int:
        return len({entry.page_url for entry in self._index.values()})


def load_config() -> BotConfig:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("В файле .env не найден BOT_TOKEN.")

    book_url = os.getenv("BOOK_URL", DEFAULT_BOOK_URL).strip() or DEFAULT_BOOK_URL
    ukr_mova_book_url = os.getenv("BOOK_URL_UKR_MOVA", UKR_MOVA_BOOK_URL).strip() or UKR_MOVA_BOOK_URL
    webhook_url = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
    port = int(os.getenv("PORT", "8000"))
    webhook_path = os.getenv("WEBHOOK_PATH", token).strip().strip("/")
    return BotConfig(
        token=token,
        book_url=book_url,
        ukr_mova_book_url=ukr_mova_book_url,
        webhook_url=webhook_url,
        port=port,
        webhook_path=webhook_path,
    )


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text)
    return match.group(0) if match else None


def extract_page_number(text: str) -> int | None:
    cleaned = text.strip().lower()
    match = re.search(r"(?:стор\.?|стр\.?|с\.?)\s*(\d+)", cleaned)
    if match:
        return int(match.group(1))
    if re.fullmatch(r"\d{1,4}", cleaned):
        return int(cleaned)
    return None


def get_active_book_key(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("book_key", "algebra")


def get_active_resolver(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, TaskResolver]:
    book_key = get_active_book_key(context)
    resolvers: dict[str, TaskResolver] = context.application.bot_data["resolvers"]
    return book_key, resolvers[book_key]


def build_subject_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BOOK_ALIASES["algebra"], BOOK_ALIASES["mova"]]],
        resize_keyboard=True,
    )


async def set_book(update: Update, context: ContextTypes.DEFAULT_TYPE, book_key: str) -> None:
    context.user_data["book_key"] = book_key
    if update.message:
        await update.message.reply_text(f"Выбрана книга: {BOOK_ALIASES[book_key]}")


def parse_task_number(value: str) -> tuple[int, ...] | None:
    cleaned = value.strip()
    if not re.fullmatch(r"\d+(?:\.\d+)+", cleaned):
        return None
    return tuple(int(part) for part in cleaned.split("."))


def parse_task_range(value: str) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)+)-(\d+(?:\.\d+)+)(?:\s*\([^)]+\))?", value.strip())
    if not match:
        return None

    start = parse_task_number(match.group(1))
    end = parse_task_number(match.group(2))
    if not start or not end:
        return None
    return (start, end) if start <= end else (end, start)


def entry_task_bounds(label: str) -> tuple[tuple[int, ...] | None, tuple[int, ...] | None]:
    normalized = FourBookScraper.normalize_task_label(label)
    bounds = parse_task_range(normalized)
    if bounds:
        return bounds

    single = parse_task_number(normalized)
    if single:
        return single, single
    return None, None


def build_caption(title: str, source_url: str, total: int, task_label: str | None = None) -> str:
    safe_title = escape(title)
    safe_url = escape(source_url)
    lines = [f"<b>{safe_title}</b>"]
    if task_label:
        lines.append(f"Задание: <code>{escape(task_label)}</code>")
    lines.append(f"Найдено изображений: {total}")
    lines.append(f"<a href=\"{safe_url}\">Открыть страницу решения</a>")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Отправь номер задания, например `1.1-1.2`, `1.2` или `2.12 (1-2)`.\n"
        "Я найду нужную страницу в ГДЗ на 4book.org и пришлю решение картинками.\n"
        "Можно также отправить прямую ссылку на страницу 4book.org.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reload_index(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    resolver: TaskResolver = context.application.bot_data["resolver"]
    await update.message.reply_text("Обновляю список заданий...")
    try:
        count = await resolver.ensure_index(force=True)
    except Exception as exc:
        LOGGER.exception("Failed to rebuild task index")
        await update.message.reply_text(f"Не удалось обновить список заданий: {exc}")
        return

    await update.message.reply_text(f"Готово. Загружено {count} страниц с решениями.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        await update.message.reply_text("Отправь номер задания или ссылку на 4book.org.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    scraper: FourBookScraper = context.application.bot_data["scraper"]
    resolver: TaskResolver = context.application.bot_data["resolver"]

    url = extract_url(text)
    task_label: str | None = None

    if url:
        source = url
    else:
        try:
            entry = await resolver.find(text)
        except Exception as exc:
            LOGGER.exception("Failed to load task index")
            await update.message.reply_text(f"Не удалось загрузить список заданий: {exc}")
            return

        if not entry:
            await update.message.reply_text(
                "Я не нашёл такое задание.\n"
                "Попробуй формат вроде `1.1-1.2`, `1.2`, `2.3`, `2.12 (1-2)` или отправь полную ссылку 4book.org.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        source = entry.page_url
        task_label = entry.label

    try:
        result = await asyncio.to_thread(scraper.fetch_solution, source)
    except ScraperError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception as exc:
        LOGGER.exception("Failed to parse solution")
        await update.message.reply_text(f"Не удалось обработать запрос: {exc}")
        return

    try:
        files = await asyncio.gather(
            *[asyncio.to_thread(scraper.download_image, image) for image in result.images]
        )
    except Exception as exc:
        LOGGER.exception("Failed to download images")
        await update.message.reply_text(f"Решение найдено, но не удалось скачать изображения: {exc}")
        return

    media_groups = build_media_groups(result, task_label, files)
    try:
        for group in media_groups:
            await update.message.reply_media_group(media=group)
    except BadRequest as exc:
        LOGGER.exception("Failed to send media group")
        await send_images_one_by_one(update, result, task_label, files)


def build_media_groups(
    result: SolutionResult,
    task_label: str | None,
    files: list[bytes],
) -> list[list[InputMediaPhoto]]:
    groups: list[list[InputMediaPhoto]] = []
    current_group: list[InputMediaPhoto] = []

    for index, (image, content) in enumerate(zip(result.images, files, strict=True), start=1):
        caption = None
        parse_mode = None
        if index == 1:
            caption = build_caption(result.title, result.source_url, len(result.images), task_label)
            parse_mode = ParseMode.HTML

        current_group.append(
            InputMediaPhoto(
                media=InputFile(content, filename=image.filename, attach=True),
                caption=caption,
                parse_mode=parse_mode,
            )
        )

        if len(current_group) == 10:
            groups.append(current_group)
            current_group = []

    if current_group:
        groups.append(current_group)

    return groups


def build_caption_ua(title: str, source_url: str, total: int, task_label: str | None = None) -> str:
    safe_title = escape(title)
    safe_url = escape(source_url)
    lines = [f"<b>{safe_title}</b>"]
    if task_label:
        lines.append(f"Завдання: <code>{escape(task_label)}</code>")
    lines.append(f"Знайдено зображень: {total}")
    lines.append(f"<a href=\"{safe_url}\">Відкрити сторінку розв'язання</a>")
    return "\n".join(lines)


def build_media_groups_ua(
    result: SolutionResult,
    task_label: str | None,
    files: list[bytes],
) -> list[list[InputMediaPhoto]]:
    groups: list[list[InputMediaPhoto]] = []
    current_group: list[InputMediaPhoto] = []

    for index, (image, content) in enumerate(zip(result.images, files, strict=True), start=1):
        caption = None
        parse_mode = None
        if index == 1:
            caption = build_caption_ua(result.title, result.source_url, len(result.images), task_label)
            parse_mode = ParseMode.HTML

        current_group.append(
            InputMediaPhoto(
                media=InputFile(content, filename=image.filename, attach=True),
                caption=caption,
                parse_mode=parse_mode,
            )
        )

        if len(current_group) == 10:
            groups.append(current_group)
            current_group = []

    if current_group:
        groups.append(current_group)

    return groups


async def send_images_one_by_one(
    update: Update,
    result: SolutionResult,
    task_label: str | None,
    files: list[bytes],
) -> None:
    if not update.message:
        return

    caption = build_caption(result.title, result.source_url, len(result.images), task_label)
    for index, (image, content) in enumerate(zip(result.images, files, strict=True), start=1):
        stream = io.BytesIO(content)
        stream.name = image.filename
        await update.message.reply_photo(
            photo=stream,
            caption=caption if index == 1 else None,
            parse_mode=ParseMode.HTML if index == 1 else None,
        )


async def send_images_one_by_one_ua(
    update: Update,
    result: SolutionResult,
    task_label: str | None,
    files: list[bytes],
) -> None:
    if not update.message:
        return

    caption = build_caption_ua(result.title, result.source_url, len(result.images), task_label)
    for index, (image, content) in enumerate(zip(result.images, files, strict=True), start=1):
        stream = io.BytesIO(content)
        stream.name = image.filename
        await update.message.reply_photo(
            photo=stream,
            caption=caption if index == 1 else None,
            parse_mode=ParseMode.HTML if index == 1 else None,
        )


async def start_multi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    current_book = BOOK_ALIASES[get_active_book_key(context)]
    await update.message.reply_text(
        f"Текущая книга: {current_book}\n"
        "Команды переключения: /algebra и /mova\n"
        "После выбора просто отправляй номер задания."
    )


async def select_algebra(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_book(update, context, "algebra")


async def select_mova(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_book(update, context, "mova")


async def reload_index_multi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    book_key, resolver = get_active_resolver(context)
    await update.message.reply_text(f"Обновляю индекс: {BOOK_ALIASES[book_key]}")
    try:
        count = await resolver.ensure_index(force=True)
    except Exception as exc:
        LOGGER.exception("Failed to rebuild task index")
        await update.message.reply_text(f"Не удалось обновить индекс: {exc}")
        return
    await update.message.reply_text(f"Готово. Загружено {count} страниц.")


async def handle_message_multi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        await update.message.reply_text("Отправь номер задания или ссылку на 4book.org.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    scraper: FourBookScraper = context.application.bot_data["scraper"]
    book_key, resolver = get_active_resolver(context)

    url = extract_url(text)
    task_label: str | None = None

    if url:
        sources = [url]
    else:
        try:
            entries = await resolver.find_many(text)
        except Exception as exc:
            LOGGER.exception("Failed to load task index")
            await update.message.reply_text(f"Не удалось загрузить индекс {BOOK_ALIASES[book_key]}: {exc}")
            return

        if not entries:
            await update.message.reply_text(
                f"Я не нашёл это задание в книге {BOOK_ALIASES[book_key]}.\n"
                "Сменить книгу: /algebra или /mova"
            )
            return

        sources = [entry.page_url for entry in entries]
        task_label = text if len(entries) > 1 else entries[0].label

    for source in sources:
        caption_label = task_label if len(sources) == 1 else None
        try:
            result = await asyncio.to_thread(scraper.fetch_solution, source)
        except ScraperError as exc:
            await update.message.reply_text(str(exc))
            continue
        except Exception as exc:
            LOGGER.exception("Failed to parse solution")
            await update.message.reply_text(f"Не удалось обработать запрос: {exc}")
            continue

        try:
            files = await asyncio.gather(
                *[asyncio.to_thread(scraper.download_image, image) for image in result.images]
            )
        except Exception as exc:
            LOGGER.exception("Failed to download images")
            await update.message.reply_text(f"Решение найдено, но не удалось скачать изображения: {exc}")
            continue

        media_groups = build_media_groups_ua(result, caption_label, files)
        try:
            for group in media_groups:
                await update.message.reply_media_group(media=group)
        except BadRequest:
            LOGGER.exception("Failed to send media group")
            await send_images_one_by_one_ua(update, result, caption_label, files)


async def set_book_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, book_key: str) -> None:
    context.user_data["book_key"] = book_key
    if update.message:
        await update.message.reply_text(
            f"Обрано предмет: {BOOK_ALIASES[book_key]}",
            reply_markup=build_subject_keyboard(),
        )


async def start_ua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    current_book = BOOK_ALIASES[get_active_book_key(context)]
    await update.message.reply_text(
        f"Вітаю. Поточний предмет: {current_book}\n"
        "Оберіть предмет кнопкою нижче або командами /algebra і /mova.\n"
        "Після цього просто надішліть номер вправи.",
        reply_markup=build_subject_keyboard(),
    )


async def select_algebra_ua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_book_prompt(update, context, "algebra")


async def select_mova_ua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_book_prompt(update, context, "mova")


async def reload_index_ua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    book_key, resolver = get_active_resolver(context)
    await update.message.reply_text(f"Оновлюю індекс для предмета: {BOOK_ALIASES[book_key]}")
    try:
        count = await resolver.ensure_index(force=True)
    except Exception as exc:
        LOGGER.exception("Failed to rebuild task index")
        await update.message.reply_text(f"Не вдалося оновити індекс: {exc}")
        return
    await update.message.reply_text(f"Готово. Завантажено {count} сторінок.")


async def handle_message_ua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        await update.message.reply_text("Надішліть номер вправи або посилання на 4book.org.")
        return

    if text in SUBJECT_BUTTON_TO_KEY:
        await set_book_prompt(update, context, SUBJECT_BUTTON_TO_KEY[text])
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    scraper: FourBookScraper = context.application.bot_data["scraper"]
    book_key, resolver = get_active_resolver(context)

    url = extract_url(text)
    task_label: str | None = None

    if url:
        sources = [url]
    else:
        try:
            entries = await resolver.find_many(text)
        except Exception as exc:
            LOGGER.exception("Failed to load task index")
            await update.message.reply_text(f"Не вдалося завантажити індекс {BOOK_ALIASES[book_key]}: {exc}")
            return

        if not entries:
            await update.message.reply_text(
                f"Я не знайшов це завдання у предметі {BOOK_ALIASES[book_key]}.\n"
                "Спробуйте інший номер або перемкніть предмет кнопками нижче.",
                reply_markup=build_subject_keyboard(),
            )
            return

        sources = [entry.page_url for entry in entries]
        task_label = text if len(entries) > 1 else entries[0].label

    for source in sources:
        caption_label = task_label if len(sources) == 1 else None
        try:
            result = await asyncio.to_thread(scraper.fetch_solution, source)
        except ScraperError as exc:
            await update.message.reply_text(str(exc), reply_markup=build_subject_keyboard())
            continue
        except Exception as exc:
            LOGGER.exception("Failed to parse solution")
            await update.message.reply_text(f"Не вдалося опрацювати запит: {exc}")
            continue

        try:
            files = await asyncio.gather(
                *[asyncio.to_thread(scraper.download_image, image) for image in result.images]
            )
        except Exception as exc:
            LOGGER.exception("Failed to download images")
            await update.message.reply_text(f"Розв'язання знайдено, але не вдалося завантажити зображення: {exc}")
            continue

        media_groups = build_media_groups(result, caption_label, files)
        try:
            for group in media_groups:
                await update.message.reply_media_group(media=group)
        except BadRequest:
            LOGGER.exception("Failed to send media group")
            await send_images_one_by_one(update, result, caption_label, files)


async def handle_message_v2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        await update.message.reply_text("Отправь номер задания или ссылку на 4book.org.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    scraper: FourBookScraper = context.application.bot_data["scraper"]
    resolver: TaskResolver = context.application.bot_data["resolver"]

    url = extract_url(text)
    task_label: str | None = None

    if url:
        sources = [url]
    else:
        try:
            entries = await resolver.find_many(text)
        except Exception as exc:
            LOGGER.exception("Failed to load task index")
            await update.message.reply_text(f"Не удалось загрузить список заданий: {exc}")
            return

        if not entries:
            await update.message.reply_text(
                "Я не нашёл такое задание.\n"
                "Попробуй формат вроде `33.4`, `33.4-33.10`, `1.2`, `2.12 (1-2)` или отправь полную ссылку 4book.org.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        sources = [entry.page_url for entry in entries]
        task_label = text if len(entries) > 1 else entries[0].label

    for source in sources:
        caption_label = task_label if len(sources) == 1 else None
        try:
            result = await asyncio.to_thread(scraper.fetch_solution, source)
        except ScraperError as exc:
            await update.message.reply_text(str(exc))
            continue
        except Exception as exc:
            LOGGER.exception("Failed to parse solution")
            await update.message.reply_text(f"Не удалось обработать запрос: {exc}")
            continue

        try:
            files = await asyncio.gather(
                *[asyncio.to_thread(scraper.download_image, image) for image in result.images]
            )
        except Exception as exc:
            LOGGER.exception("Failed to download images")
            await update.message.reply_text(f"Решение найдено, но не удалось скачать изображения: {exc}")
            continue

        media_groups = build_media_groups(result, caption_label, files)
        try:
            for group in media_groups:
                await update.message.reply_media_group(media=group)
        except BadRequest:
            LOGGER.exception("Failed to send media group")
            await send_images_one_by_one(update, result, caption_label, files)


def subject_name(book_key: str) -> str:
    return {
        "algebra": "Алгебра",
        "mova": "Українська мова",
    }[book_key]


def build_subject_keyboard_ua() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["Алгебра", "Українська мова"]], resize_keyboard=True)


async def set_book_prompt_clean(update: Update, context: ContextTypes.DEFAULT_TYPE, book_key: str) -> None:
    context.user_data["book_key"] = book_key
    if not update.message:
        return

    prompt = (
        "Надішліть номер вправи, наприклад 6.7."
        if book_key == "algebra"
        else "Надішліть номер сторінки, наприклад 33."
    )
    await update.message.reply_text(
        f"Обрано предмет: {subject_name(book_key)}.\n{prompt}",
        reply_markup=build_subject_keyboard_ua(),
    )


async def start_clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.setdefault("book_key", "algebra")
    if not update.message:
        return
    current_book = subject_name(get_active_book_key(context))
    await update.message.reply_text(
        "Вітаю.\n"
        f"Поточний предмет: {current_book}.\n"
        "Оберіть предмет кнопкою нижче.\n"
        "Для алгебри надсилайте номер вправи, для української мови — номер сторінки.",
        reply_markup=build_subject_keyboard_ua(),
    )


async def select_algebra_clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_book_prompt_clean(update, context, "algebra")


async def select_mova_clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_book_prompt_clean(update, context, "mova")


async def reload_index_clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    book_key, resolver = get_active_resolver(context)
    await update.message.reply_text(f"Оновлюю індекс для предмета: {subject_name(book_key)}")
    try:
        count = await resolver.ensure_index(force=True)
    except Exception as exc:
        LOGGER.exception("Failed to rebuild task index")
        await update.message.reply_text(f"Не вдалося оновити індекс: {exc}")
        return
    await update.message.reply_text(f"Готово. Завантажено {count} сторінок.")


async def handle_message_clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        await update.message.reply_text(
            "Надішліть номер вправи або сторінки, або посилання на 4book.org.",
            reply_markup=build_subject_keyboard_ua(),
        )
        return

    if text == "Алгебра":
        await set_book_prompt_clean(update, context, "algebra")
        return
    if text == "Українська мова":
        await set_book_prompt_clean(update, context, "mova")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    scraper: FourBookScraper = context.application.bot_data["scraper"]
    book_key, resolver = get_active_resolver(context)

    url = extract_url(text)
    task_label: str | None = None

    if url:
        sources = [url]
    else:
        try:
            if book_key == "mova":
                entries = await resolver.find_many_by_page(text)
            else:
                entries = await resolver.find_many(text)
        except Exception as exc:
            LOGGER.exception("Failed to load task index")
            await update.message.reply_text(
                f"Не вдалося завантажити індекс {subject_name(book_key)}: {exc}",
                reply_markup=build_subject_keyboard_ua(),
            )
            return

        if not entries:
            if book_key == "mova":
                await update.message.reply_text(
                    "Я не знайшов таку сторінку.\nСпробуйте номер сторінки, наприклад 33 або 50.",
                    reply_markup=build_subject_keyboard_ua(),
                )
            else:
                await update.message.reply_text(
                    "Я не знайшов таку вправу.\nСпробуйте інший номер або перемкніть предмет кнопками нижче.",
                    reply_markup=build_subject_keyboard_ua(),
                )
            return

        sources = [entry.page_url for entry in entries]
        if book_key == "mova":
            page_number = extract_page_number(text)
            task_label = f"Стор. {page_number}" if page_number is not None else text
        else:
            task_label = text if len(entries) > 1 else entries[0].label

    for source in sources:
        caption_label = task_label if len(sources) == 1 else None
        try:
            result = await asyncio.to_thread(scraper.fetch_solution, source)
        except ScraperError as exc:
            await update.message.reply_text(str(exc), reply_markup=build_subject_keyboard_ua())
            continue
        except Exception as exc:
            LOGGER.exception("Failed to parse solution")
            await update.message.reply_text(f"Не вдалося опрацювати запит: {exc}")
            continue

        try:
            files = await asyncio.gather(
                *[asyncio.to_thread(scraper.download_image, image) for image in result.images]
            )
        except Exception as exc:
            LOGGER.exception("Failed to download images")
            await update.message.reply_text(f"Розв'язання знайдено, але не вдалося завантажити зображення: {exc}")
            continue

        media_groups = build_media_groups_ua(result, caption_label, files)
        try:
            for group in media_groups:
                await update.message.reply_media_group(media=group)
        except BadRequest:
            LOGGER.exception("Failed to send media group")
            await send_images_one_by_one_ua(update, result, caption_label, files)


async def on_startup(app: Application) -> None:
    resolver: TaskResolver = app.bot_data["resolvers"]["algebra"]
    try:
        count = await resolver.ensure_index()
        LOGGER.info("Exercise index loaded: %s pages", count)
    except Exception:
        LOGGER.exception("Initial index build failed")


def main() -> None:
    config = load_config()
    scraper = FourBookScraper()
    resolvers = {
        "algebra": TaskResolver(scraper=scraper, book_url=config.book_url),
        "mova": TaskResolver(scraper=scraper, book_url=config.ukr_mova_book_url),
    }

    app = Application.builder().token(config.token).job_queue(None).post_init(on_startup).build()
    app.bot_data["scraper"] = scraper
    app.bot_data["resolvers"] = resolvers
    app.bot_data["resolver"] = resolvers["algebra"]

    app.add_handler(CommandHandler("start", start_clean))
    app.add_handler(CommandHandler("reload", reload_index_clean))
    app.add_handler(CommandHandler("algebra", select_algebra_clean))
    app.add_handler(CommandHandler("mova", select_mova_clean))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_clean))
    if config.webhook_url:
        webhook_url = f"{config.webhook_url}/{config.webhook_path}"
        LOGGER.info("Starting webhook mode on port %s", config.port)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.port,
            url_path=config.webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        LOGGER.info("Starting polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
