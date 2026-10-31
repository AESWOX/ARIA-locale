"""T1: парсер публичной веб-версии t.me (только текст, никаких действий)."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_VOID = frozenset({"meta", "link", "br", "hr", "img", "input", "source", "area", "base", "col", "embed", "param", "track", "wbr"})


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _TelegramParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._current: dict | None = None
        self._depth = 0
        self._nested = 0
        self._text_depth = 0
        self._views_depth = 0
        self._views_parts: list[str] = []

    def _start_post(self, data_post: str) -> None:
        channel, _, message_id = data_post.partition("/")
        self._current = {
            "channel": channel,
            "message_id": message_id,
            "date": "",
            "text_parts": [],
            "links": [],
            "views": "",
            "source_trust": "untrusted",
            "url": f"https://t.me/{channel}/{message_id}",
        }
        self._depth = 1
        self._nested = 0
        self._text_depth = 0
        self._views_depth = 0
        self._views_parts = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_d = {str(k): str(v or "") for k, v in attrs}
        classes = set(attrs_d.get("class", "").split())
        if self._current is None:
            if tag == "div" and "tgme_widget_message" in classes and attrs_d.get("data-post"):
                self._start_post(attrs_d["data-post"])
            return
        if tag not in _VOID:
            self._depth += 1
        if self._nested:
            if tag == "div" and "tgme_widget_message" in classes and attrs_d.get("data-post"):
                self._nested += 1
            return
        if tag == "div" and "tgme_widget_message" in classes and attrs_d.get("data-post"):
            self._nested = 1
            return
        if self._text_depth:
            if tag not in _VOID:
                self._text_depth += 1
            if tag == "a" and attrs_d.get("href"):
                self._current["links"].append(attrs_d["href"])
            if tag == "br":
                self._current["text_parts"].append("\n")
            return
        if tag == "div" and "tgme_widget_message_text" in classes:
            self._text_depth = 1
            return
        if tag == "time" and attrs_d.get("datetime"):
            self._current["date"] = attrs_d["datetime"]
        if tag == "a" and attrs_d.get("href"):
            self._current["links"].append(attrs_d["href"])
        if tag in ("div", "span") and "tgme_widget_message_views" in classes:
            self._views_depth = 1

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag not in _VOID:
            self._depth -= 1
        if self._nested:
            if tag == "div":
                self._nested = max(0, self._nested - 1)
        elif self._text_depth:
            self._text_depth = max(0, self._text_depth - 1)
        elif self._views_depth:
            self._views_depth = max(0, self._views_depth - 1)
        if self._depth <= 0:
            self._finalize()

    def handle_data(self, data: str) -> None:
        if self._current is None or self._nested:
            return
        if self._text_depth:
            self._current["text_parts"].append(data)
        elif self._views_depth:
            self._views_parts.append(data)

    def _finalize(self) -> None:
        current = self._current
        self._current = None
        if current is None:
            return
        text = _clean("".join(current.pop("text_parts", [])))
        views = _clean("".join(self._views_parts))
        links = list(dict.fromkeys(current.get("links") or []))
        if not text and not links:
            return
        current["text"] = text
        current["views"] = views
        current["links"] = links
        self.posts.append(current)


def parse_posts(html: str) -> list[dict]:
    parser = _TelegramParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return list(parser.posts)
    return list(parser.posts)
