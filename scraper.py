from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk,ru;q=0.9,en;q=0.8",
}

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class ScraperError(Exception):
    """Raised when the scraper cannot parse a supported 4book page."""


@dataclass(slots=True)
class SolutionImage:
    url: str
    filename: str


@dataclass(slots=True)
class SolutionResult:
    source_url: str
    title: str
    images: list[SolutionImage]


@dataclass(slots=True)
class TaskEntry:
    label: str
    normalized_label: str
    page_url: str
    section_title: str


class FourBookScraper:
    def __init__(self, timeout: int = 25) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def fetch_solution(self, raw_url: str) -> SolutionResult:
        url = self._normalize_url(raw_url)
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        if not self._is_supported_url(response.url):
            raise ScraperError("Only 4book.org GDZ pages are supported.")

        title = self._extract_title(soup)
        page_url = response.url

        if self._looks_like_solution_page(page_url):
            images = self._extract_images(soup, page_url)
            if images:
                return SolutionResult(source_url=page_url, title=title, images=images)

        solution_url = self._find_first_solution_page(soup, page_url)
        if not solution_url:
            raise ScraperError(
                "Could not find a page with a concrete exercise. "
                "Send a URL like .../page-1 or a section page."
            )

        solution_response = self.session.get(solution_url, timeout=self.timeout)
        solution_response.raise_for_status()
        solution_soup = BeautifulSoup(solution_response.text, "lxml")
        solution_title = self._extract_title(solution_soup) or title
        images = self._extract_images(solution_soup, solution_response.url)

        if not images:
            raise ScraperError("No solution images were found on the page.")

        return SolutionResult(
            source_url=solution_response.url,
            title=solution_title,
            images=images,
        )

    def download_image(self, image: SolutionImage) -> bytes:
        response = self.session.get(image.url, timeout=self.timeout)
        response.raise_for_status()
        return response.content

    def build_task_index(self, raw_book_url: str) -> dict[str, TaskEntry]:
        book_url = self._normalize_url(raw_book_url)
        response = self.session.get(book_url, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        if not self._is_supported_url(response.url):
            raise ScraperError("Only 4book.org GDZ pages are supported.")

        index: dict[str, TaskEntry] = {}
        visited: set[str] = set()
        pending: list[str] = [response.url]

        while pending:
            current_url = pending.pop(0)
            if current_url in visited:
                continue

            if current_url == response.url:
                current_soup = soup
                visited.add(current_url)
            else:
                current_response = self.session.get(current_url, timeout=self.timeout)
                current_response.raise_for_status()
                current_soup = BeautifulSoup(current_response.text, "lxml")
                current_url = current_response.url
                if current_url in visited:
                    continue
                visited.add(current_url)

            section_title = self._extract_section_title(current_soup)

            for entry in self._extract_task_entries(current_soup, current_url, section_title):
                for key in self._task_keys(entry.label):
                    index.setdefault(
                        key,
                        TaskEntry(
                            label=entry.label,
                            normalized_label=entry.normalized_label,
                            page_url=entry.page_url,
                            section_title=entry.section_title,
                        ),
                    )

            for next_url in self._extract_section_links(current_soup, current_url, response.url):
                if next_url not in visited:
                    pending.append(next_url)

        if not index:
            raise ScraperError("The task index was built, but no exercise pages were found.")

        return index

    def _normalize_url(self, url: str) -> str:
        normalized = url.strip()
        if not normalized:
            raise ScraperError("The URL is empty.")
        if not normalized.startswith(("http://", "https://")):
            normalized = f"https://{normalized}"
        return normalized

    def _is_supported_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.endswith("4book.org") and "/gdz-reshebniki-ukraina/" in parsed.path

    def _extract_section_links(self, soup: BeautifulSoup, page_url: str, book_url: str | None = None) -> list[str]:
        parsed = urlparse(page_url)
        book_parsed = urlparse(book_url or page_url)
        book_path = book_parsed.path.rstrip("/")
        result: list[str] = []
        seen: set[str] = set()

        for link in soup.select("a[href]"):
            href = urljoin(page_url, link.get("href", ""))
            href_parsed = urlparse(href)
            href_path = href_parsed.path.rstrip("/")
            if href_parsed.netloc != book_parsed.netloc:
                continue
            if not href_path.startswith(book_path):
                continue
            if self._looks_like_solution_page(href):
                continue
            if href_path == book_path:
                continue
            text = link.get_text(" ", strip=True)
            if not text:
                continue
            if href in seen:
                continue
            seen.add(href)
            result.append(href)

        return result

    def _looks_like_solution_page(self, url: str) -> bool:
        return bool(re.search(r"/page-\d+/?$", url))

    def _extract_title(self, soup: BeautifulSoup) -> str:
        for selector in ("h1", "meta[property='og:title']", "title"):
            node = soup.select_one(selector)
            if not node:
                continue
            if isinstance(node, Tag) and node.name == "meta":
                content = node.get("content", "").strip()
                if content:
                    return content
            text = node.get_text(" ", strip=True)
            if text:
                return text
        return "4book solution"

    def _extract_section_title(self, soup: BeautifulSoup) -> str:
        header = soup.select_one("h1")
        return header.get_text(" ", strip=True) if header else ""

    def _find_first_solution_page(self, soup: BeautifulSoup, page_url: str) -> str | None:
        candidates = []
        for link in soup.select("a[href]"):
            href = urljoin(page_url, link.get("href", ""))
            if not self._looks_like_solution_page(href):
                continue
            text = link.get_text(" ", strip=True)
            score = 0
            if text:
                score += 1
            if re.search(r"\d", text):
                score += 2
            if "forward" in text.lower() or "next" in text.lower():
                score -= 1
            candidates.append((score, href))

        if not candidates:
            return None

        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][1]

    def _extract_images(self, soup: BeautifulSoup, page_url: str) -> list[SolutionImage]:
        candidates: list[str] = []

        for selector in (
            "meta[property='og:image']",
            "meta[name='twitter:image']",
            "img[src]",
            "img[data-src]",
            "a[href]",
        ):
            for node in soup.select(selector):
                value = (
                    node.get("content")
                    or node.get("data-src")
                    or node.get("src")
                    or node.get("href")
                    or ""
                ).strip()
                if value:
                    candidates.append(urljoin(page_url, value))

        cleaned_urls = self._filter_image_candidates(candidates)
        images = [
            SolutionImage(url=image_url, filename=self._filename_from_url(image_url, index))
            for index, image_url in enumerate(cleaned_urls, start=1)
        ]
        return images

    def _extract_task_entries(
        self,
        soup: BeautifulSoup,
        page_url: str,
        section_title: str,
    ) -> list[TaskEntry]:
        entries: list[TaskEntry] = []
        seen_urls: set[str] = set()

        for link in soup.select("a[href]"):
            href = urljoin(page_url, link.get("href", ""))
            if not self._looks_like_solution_page(href):
                continue
            label = link.get_text(" ", strip=True)
            if not self._looks_like_task_label(label):
                continue
            if href in seen_urls:
                continue

            seen_urls.add(href)
            entries.append(
                TaskEntry(
                    label=label,
                    normalized_label=self.normalize_task_label(label),
                    page_url=href,
                    section_title=section_title,
                )
            )

        return entries

    def _looks_like_task_label(self, text: str) -> bool:
        cleaned = text.strip()
        return bool(cleaned and re.search(r"\d+\.\d+", cleaned))

    def _task_keys(self, label: str) -> set[str]:
        normalized = self.normalize_task_label(label)
        keys = {normalized}

        no_spaces = normalized.replace(" ", "")
        keys.add(no_spaces)
        keys.add(no_spaces.replace("(", "").replace(")", ""))
        keys.add(normalized.replace("(", " ").replace(")", " ").replace("  ", " ").strip())
        keys.update(self._expand_suffix_keys(normalized))
        keys.update(self._expand_range_keys(normalized))

        return {key for key in keys if key}

    def _expand_suffix_keys(self, normalized: str) -> set[str]:
        match = re.fullmatch(r"(?P<base>\d+(?:\.\d+)+)\s*\((?P<suffix>[^)]+)\)", normalized)
        if not match:
            return set()

        base = match.group("base")
        suffix = match.group("suffix").strip()
        keys = {
            base,
            f"{base}({suffix})",
            f"{base} {suffix}",
        }

        range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", suffix)
        if range_match:
            start_value = int(range_match.group(1))
            end_value = int(range_match.group(2))
            if start_value <= end_value and end_value - start_value <= 200:
                keys.update({f"{base} ({value})" for value in range(start_value, end_value + 1)})
                keys.update({f"{base}({value})" for value in range(start_value, end_value + 1)})
                keys.update({f"{base} {value}" for value in range(start_value, end_value + 1)})
        else:
            keys.add(f"{base} ({suffix})")

        return keys

    def _expand_range_keys(self, normalized: str) -> set[str]:
        match = re.fullmatch(
            r"(?P<start>\d+(?:\.\d+)+)\s*-\s*(?P<end>\d+(?:\.\d+)+)(?P<suffix>\s*\([^)]+\))?",
            normalized,
        )
        if not match:
            return set()

        start = match.group("start")
        end = match.group("end")
        suffix = (match.group("suffix") or "").strip()

        keys = {
            normalized,
            f"{start}-{end}",
            f"{start} - {end}",
            start,
            end,
        }

        expanded = self._enumerate_decimal_range(start, end)
        if expanded:
            keys.update(expanded)
            if suffix:
                keys.update({f"{item} {suffix}".strip() for item in expanded})

        if suffix:
            keys.add(f"{start} {suffix}".strip())
            keys.add(f"{end} {suffix}".strip())

        return keys

    def _enumerate_decimal_range(self, start: str, end: str) -> set[str]:
        start_parts = start.split(".")
        end_parts = end.split(".")
        if len(start_parts) != len(end_parts) or start_parts[:-1] != end_parts[:-1]:
            return set()

        prefix = ".".join(start_parts[:-1])
        try:
            start_value = int(start_parts[-1])
            end_value = int(end_parts[-1])
        except ValueError:
            return set()

        if start_value > end_value or end_value - start_value > 200:
            return set()

        if prefix:
            return {f"{prefix}.{value}" for value in range(start_value, end_value + 1)}
        return {str(value) for value in range(start_value, end_value + 1)}

    @staticmethod
    def normalize_task_label(label: str) -> str:
        cleaned = label.lower().strip()
        cleaned = cleaned.replace(",", ".")
        cleaned = cleaned.replace("_", " ")
        cleaned = cleaned.replace("\u2013", "-")
        cleaned = cleaned.replace("\u2014", "-")
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = re.sub(r"\s*-\s*", "-", cleaned)
        cleaned = re.sub(r"\s*\(\s*", " (", cleaned)
        cleaned = re.sub(r"\s*\)\s*", ")", cleaned)

        match = re.search(r"\d+(?:\.\d+)+(?:\s*\([^)]+\))?", cleaned)
        range_match = re.search(r"\d+(?:\.\d+)+\s*-\s*\d+(?:\.\d+)+(?:\s*\([^)]+\))?", cleaned)
        if range_match:
            return range_match.group(0).strip()
        if match:
            return match.group(0).strip()

        fallback = re.search(r"\d[\d\s().-]*", cleaned)
        return fallback.group(0).strip() if fallback else cleaned.strip()

    def _filter_image_candidates(self, urls: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        preferred: list[str] = []

        for url in urls:
            parsed = urlparse(url)
            normalized = parsed._replace(fragment="").geturl()
            lower = normalized.lower()
            path_lower = parsed.path.lower()
            filename_lower = path_lower.rsplit("/", maxsplit=1)[-1]

            if parsed.netloc and not parsed.netloc.endswith("4book.org"):
                continue
            if not path_lower.endswith(IMAGE_EXTENSIONS):
                continue
            if any(
                token in lower
                for token in (
                    "zoom_",
                    "logo",
                    "icon",
                    "sprite",
                    "banner",
                    "ads",
                    "small_inst",
                    "small_face",
                    "small_tel",
                    "small_tik",
                    "main_inst",
                    "main_face",
                    "main_tel",
                    "main_tik",
                )
            ):
                continue

            score = 0
            if "og-image" in lower:
                score += 1
            if any(
                token in lower
                for token in ("gdz", "resheb", "page", "task", "vprava", "vpravi", "uploads", "exercise")
            ):
                score += 2
            if any(token in lower for token in ("cover", "oblozh", "obklad")):
                score -= 2

            if score < 0:
                continue
            if normalized in seen:
                continue

            seen.add(normalized)
            result.append(normalized)
            if filename_lower.startswith(("big_", "orig_", "page-", "page_")):
                preferred.append(normalized)

        return preferred or result

    def _filename_from_url(self, url: str, index: int) -> str:
        path = urlparse(url).path
        last_part = path.rsplit("/", maxsplit=1)[-1]
        if "." not in last_part:
            return f"solution_{index}.jpg"
        return last_part
