from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


MSK = timezone(timedelta(hours=3))

def msk_now() -> str:
    return datetime.now(MSK).isoformat()


@dataclass(frozen=True)
class LeadRecord:
    source: str
    message_id: int
    link: str
    text: str
    score: int
    keywords: tuple[str, ...]
    message_date: str


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                text TEXT NOT NULL,
                score INTEGER NOT NULL,
                keywords_json TEXT NOT NULL,
                message_date TEXT NOT NULL,
                notified_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source, message_id)
            );

            CREATE TABLE IF NOT EXISTS kwork_offers (
                project_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price INTEGER NOT NULL,
                offer_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add_subscriber(self, chat_id: int) -> None:
        self._conn.execute(
            """
            INSERT INTO subscribers(chat_id, created_at)
            VALUES(?, ?)
            ON CONFLICT(chat_id) DO NOTHING
            """,
            (chat_id, msk_now()),
        )
        self._conn.commit()

    def record_or_should_retry(self, lead: LeadRecord) -> bool:
        """
        Записывает новый лид в БД.
        Если лид уже существует (даже без notified_at) — не отправляем повторно.
        """
        existing = self._conn.execute(
            "SELECT 1 FROM leads WHERE source = ? AND message_id = ?",
            (lead.source, lead.message_id),
        ).fetchone()
        if existing:
            return False  # Уже есть в БД — не отправляем снова

        self._conn.execute(
            """
            INSERT INTO leads(
                source, message_id, link, text, score, keywords_json,
                message_date, notified_at, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                lead.source,
                lead.message_id,
                lead.link,
                lead.text,
                lead.score,
                json.dumps(list(lead.keywords), ensure_ascii=False),
                lead.message_date,
                msk_now(),
            ),
        )
        self._conn.commit()
        return True

    def mark_notified(self, source: str, message_id: int) -> None:
        self._conn.execute(
            "UPDATE leads SET notified_at = ? WHERE source = ? AND message_id = ?",
            (msk_now(), source, message_id),
        )
        self._conn.commit()

    def save_kwork_offer_draft(
        self,
        project_id: str,
        title: str,
        description: str,
        price: int,
        offer_text: str,
    ) -> None:
        """Save or update a Kwork offer draft."""
        self._conn.execute(
            """
            INSERT INTO kwork_offers(project_id, title, description, price, offer_text, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                price=excluded.price,
                offer_text=excluded.offer_text,
                created_at=excluded.created_at
            """,
            (project_id, title, description, price, offer_text, msk_now()),
        )
        self._conn.commit()

    def get_kwork_offer_draft(self, project_id: str) -> Optional[dict]:
        """Get a saved Kwork offer draft by project ID."""
        row = self._conn.execute(
            "SELECT * FROM kwork_offers WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "project_id": row["project_id"],
            "title": row["title"],
            "description": row["description"],
            "price": int(row["price"]),
            "offer_text": row["offer_text"],
        }

    def delete_kwork_offer_draft(self, project_id: str) -> None:
        """Delete a Kwork offer draft after it's been sent."""
        self._conn.execute(
            "DELETE FROM kwork_offers WHERE project_id = ?",
            (project_id,),
        )
        self._conn.commit()

    def stats(self) -> dict[str, int]:
        lead_count = self._conn.execute("SELECT COUNT(*) AS count FROM leads").fetchone()["count"]
        pending_count = self._conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE notified_at IS NULL"
        ).fetchone()["count"]
        subscriber_count = self._conn.execute("SELECT COUNT(*) AS count FROM subscribers").fetchone()[
            "count"
        ]
        kwork_drafts_count = self._conn.execute(
            "SELECT COUNT(*) AS count FROM kwork_offers"
        ).fetchone()["count"]
        return {
            "leads": int(lead_count),
            "pending": int(pending_count),
            "subscribers": int(subscriber_count),
            "kwork_drafts": int(kwork_drafts_count),
        }

