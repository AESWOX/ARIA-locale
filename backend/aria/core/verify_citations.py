"""S5: проверка цитат и ссылок без LLM (подтверждено / не подтверждено / недоступно)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger("local_agent.verify_citations")

MAX_PAGES = 5
_URL_RE = re.compile(r"https?://[^\s<>\"'«»]+")
_QUOTE_RE = re.compile(r"«([^»]{3,300})»|\"([^\"]{3,300})\"")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_TRIM = ".,;:!?)]}>»\"'"
_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has", "have",
    "и", "в", "во", "на", "по", "с", "со", "что", "это", "как", "для", "или", "не", "из", "от", "до",
})


@dataclass(slots=True)
class Citation:
    url: str
    claim: str
    status: str
    detail: str = ""


def _trim_url(url: str) -> str:
    return url.rstrip(_TRIM)


def normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w-]{2,}", normalize(text)) if t not in _STOP]


def extract_citations(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        urls = [_trim_url(m.group(0)) for m in _URL_RE.finditer(sentence)]
        if not urls:
            continue
        claim = ""
        for match in _QUOTE_RE.finditer(sentence):
            claim = (match.group(1) or match.group(2) or "").strip()
            if claim:
                break
        if not claim:
            number = re.search(r"\d+(?:[.,]\d+)?", sentence)
            claim = number.group(0) if number else ""
        if not claim:
            continue
        for url in urls:
            if not url or url in seen or not urlsplit(url).netloc:
                continue
            seen.add(url)
            pairs.append((url, claim))
    return pairs


async def _default_fetch(url: str) -> str | None:
    from aria.tools import page_cache
    from aria.tools.handlers.web import web_fetch

    cached = page_cache.get(url)
    if cached is not None:
        return str(cached.get("content") or "")
    result = await web_fetch({"url": url, "max_chars": 50_000})
    if result.get("error"):
        return None
    return str(result.get("content") or "")


async def verify_citation(url: str, claim: str, fetch=None) -> Citation:
    fetcher = fetch or _default_fetch
    try:
        page = await fetcher(url)
    except Exception:  # noqa: BLE001 — недоступность страницы не валит проверку
        return Citation(url=url, claim=claim, status="unavailable", detail="fetch error")
    if not page:
        return Citation(url=url, claim=claim, status="unavailable", detail="empty page")
    page_norm = normalize(page)
    claim_norm = normalize(claim)
    if claim_norm and claim_norm in page_norm:
        return Citation(url=url, claim=claim, status="confirmed")
    tokens = _tokens(claim)
    if len(tokens) < 3:
        return Citation(url=url, claim=claim, status="not_confirmed", detail="short claim not found")
    present = sum(1 for token in tokens if token in page_norm)
    ratio = present / len(tokens)
    if ratio >= 0.9:
        return Citation(url=url, claim=claim, status="confirmed", detail=f"overlap {ratio:.2f}")
    return Citation(url=url, claim=claim, status="not_confirmed", detail=f"overlap {ratio:.2f}")


async def verify_answer(answer: str, fetch=None) -> list[Citation]:
    pairs = extract_citations(answer)
    pages: list[str] = []
    results: list[Citation] = []
    for url, claim in pairs:
        if url not in pages:
            if len(pages) >= MAX_PAGES:
                continue
            pages.append(url)
        results.append(await verify_citation(url, claim, fetch=fetch))
    return results


def summary(citations: list[Citation]) -> str:
    confirmed = sum(1 for c in citations if c.status == "confirmed")
    failed = [c for c in citations if c.status == "not_confirmed"]
    unavailable = sum(1 for c in citations if c.status == "unavailable")
    line = (
        f"Проверка цитат: подтверждено {confirmed}, не подтверждено {len(failed)}, "
        f"недоступно {unavailable}."
    )
    if failed:
        line += " Не подтверждены: " + ", ".join(c.url for c in failed[:5])
    return line


async def maybe_verify_research_answer(db, session, task, fetch=None) -> str | None:
    """Дописать сводку проверки для research-задачи. Ошибки — только WARNING."""
    try:
        if str(getattr(task, "role", "")) != "research":
            return None
        from sqlalchemy import select

        from aria.db import models as m
        from aria.db import repository as repo

        messages = list(
            db.execute(
                select(m.Message)
                .where(m.Message.session_id == session.id)
                .order_by(m.Message.seq_no.desc())
                .limit(100)
            ).scalars()
        )
        answer = ""
        for message in messages:
            if message.role == "assistant" and "http" in (message.content or ""):
                answer = message.content or ""
                break
        if not answer:
            return None
        citations = await verify_answer(answer, fetch=fetch)
        if not citations:
            return None
        text = summary(citations)
        repo.append_message(db, session, role="assistant", content=text)
        return text
    except Exception:  # noqa: BLE001 — проверка цитат не должна валить аудит
        logger.warning("citation verification failed", exc_info=True)
        return None
