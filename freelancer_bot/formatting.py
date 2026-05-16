from __future__ import annotations

import html
import re
from datetime import datetime

from .sources import Source
from .storage import LeadRecord


CONTACT_RE = re.compile(
    r"(?P<username>@[A-Za-z0-9_]{5,32})|(?P<email>[\w.+-]+@[\w-]+\.[\w.-]+)|(?P<url>https?://\S+)"
)


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

    link_html = (
        f'<a href="{html.escape(lead.link)}">Открыть пост в Telegram</a>'
    )

    lines = [
        f'\U0001f4cc <b>Лид</b> [score {lead.score}] \u2014 <i>{html.escape(source.title)}</i>',
        f'\U0001f4c5 {html.escape(date_text)}',
        f'\U0001f4de {contact_line}',
        f'\U0001f3f7 {keywords}',
        f'\U0001f517 {link_html}',
    ]
    return "\n".join(lines)
