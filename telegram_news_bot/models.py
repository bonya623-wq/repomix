"""
Data models shared across modules.
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Article:
    url: str
    title: str
    description: str
    image_url: str
    published_at: datetime = field(default_factory=datetime.utcnow)

    # Stable unique key — URL without query params
    @property
    def uid(self) -> str:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(self.url)
        return urlunparse(p._replace(query="", fragment=""))

    def format_date(self) -> str:
        return self.published_at.strftime("%d.%m.%Y %H:%M")
