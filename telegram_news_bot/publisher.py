"""
Telegram publishing logic.
Formats the article into the required template and sends it to the channel.
"""
import logging

import telebot
from telebot.types import InputMediaPhoto

from .models import Article

logger = logging.getLogger(__name__)


def _build_caption(article: Article) -> str:
    """
    Format:
      🎮 Заголовок

      Краткое описание...

      📅 ДД.ММ.ГГГГ ЧЧ:ММ
      🔗 Читать полностью → ссылка
    """
    parts = [
        f"🎮 <b>{article.title}</b>",
        "",
    ]
    if article.description:
        parts.append(article.description)
        parts.append("")
    parts.append(f"📅 {article.format_date()}")
    parts.append(f'🔗 Читать полностью → <a href="{article.url}">goha.ru</a>')
    return "\n".join(parts)


def publish_article(bot: telebot.TeleBot, channel_id: str, article: Article) -> bool:
    """Send one article to the channel. Returns True on success."""
    caption = _build_caption(article)

    try:
        if article.image_url:
            bot.send_photo(
                chat_id=channel_id,
                photo=article.image_url,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            bot.send_message(
                chat_id=channel_id,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
        logger.info("Published: %s", article.title)
        return True
    except telebot.apihelper.ApiTelegramException as exc:
        # If image URL is bad, retry as text
        if "PHOTO_INVALID_DIMENSIONS" in str(exc) or "wrong file identifier" in str(exc).lower():
            logger.warning("Image failed (%s), retrying as text-only post", exc)
            try:
                bot.send_message(
                    chat_id=channel_id,
                    text=caption,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                return True
            except telebot.apihelper.ApiTelegramException as exc2:
                logger.error("Text post also failed: %s", exc2)
                return False
        logger.error("Telegram error publishing '%s': %s", article.title, exc)
        return False
