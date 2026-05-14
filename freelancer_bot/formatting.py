from __future__ import annotations

import html
import re
from datetime import datetime

from .sources import Source
from .storage import LeadRecord


CONTACT_RE = re.compile(
    r"(?P<username>@[A-Za-z0-9_]{5,32})|(?P<email>[\w.+-]+@[\w-]+\.[\w.-]+)|(?P<url>https?://\S+)"
)


def truncate(text: str, limit: int = 800) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "..."


def extract_contacts(text: str) -> tuple[str, ...]:
    contacts: list[str] = []
    for match in CONTACT_RE.finditer(text):
        value = match.group(0).rstrip(".,;)")
        if value not in contacts:
            contacts.append(value)
    return tuple(contacts[:8])


def format_lead(source: Source, lead: LeadRecord) -> str:
    contacts = extract_contacts(lead.text)
    contact_line = ", ".join(html.escape(item) for item in contacts) if contacts else "—"
    keywords = ", ".join(html.escape(item) for item in lead.keywords[:6])

    date_text = lead.message_date
    try:
        date_text = datetime.fromisoformat(lead.message_date).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        pass

    excerpt = html.escape(truncate(lead.text))

    link_html = (
        f'\n<a href="{html.escape(lead.link)}">\u041f\u043e\u0441\u043c\u043e\u0442\u0440\u0435\u0442\u044c \u0432 Telegram</a>'
    )

    return (
        f'<b>\u041b\u0438\u0434</b> [score {lead.score}]  \u2014  <i>{html.escape(source.title)}</i>\n'
        f'\u250c {excerpt}\n'
        f'\u2514 {html.escape(date_text)}  \u2022  {keywords}'
        f'{link_html}'
    )
