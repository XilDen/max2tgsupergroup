import logging
from html import escape
from typing import Awaitable, Callable
from urllib.parse import unquote, urlparse

from app.max_client import MAX_FILE_DOWNLOAD_BYTES, MAX_IMAGE_DOWNLOAD_BYTES, MaxClient, MaxMessage
from app.privacy import mask_mapping_values
from app.resolver import ContactResolver
from app.tg_sender import TelegramSender, reply_keyboard

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_ATTACHMENTS_PER_MESSAGE = 10
OVERSIZED_NOTICE = "вырезано так как файл слишком большой"


def _header(sender_label: str, chat_label: str, is_dm: bool, account_label: str = "") -> str:
    account_part = f"👾 <b>{escape(account_label)}</b>" if account_label else ""
    if is_dm:
        return f"{account_part} | {sender_label}" if account_part else sender_label
    if not sender_label:
        return f"{account_part} 💬 <b>{chat_label}</b>" if account_part else f"💬 <b>{chat_label}</b>"
    return f"{account_part} 💬 <b>{chat_label}</b> | {sender_label}" if account_part else f"💬 <b>{chat_label}</b> | {sender_label}"


def _extract_photo_url(attach: dict) -> str | None:
    """Extract the best available URL for a PHOTO attachment."""
    return attach.get("baseUrl") or attach.get("url")


def _extract_file_url(attach: dict) -> str | None:
    """Extract download URL for a FILE attachment (url field takes priority)."""
    url = attach.get("url")
    if url and url.startswith("http"):
        return url
    return None


def _guess_media_kind(filename: str) -> str:
    name_lower = filename.lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "document"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _media_limit_bytes(kind: str) -> int:
    return MAX_IMAGE_DOWNLOAD_BYTES if kind == "photo" else MAX_FILE_DOWNLOAD_BYTES


def _attachment_declared_size(attach: dict) -> int:
    for key in ("size", "fileSize", "bytes", "contentLength", "content_length"):
        value = _safe_int(attach.get(key), default=0)
        if value > 0:
            return value
    return 0


def _is_oversized(attach: dict, kind: str) -> bool:
    declared_size = _attachment_declared_size(attach)
    return declared_size > _media_limit_bytes(kind)


async def _download_limited(client: MaxClient, url: str, kind: str) -> bytes | None:
    return await client.download_file(url, max_bytes=_media_limit_bytes(kind))


async def _send_oversized_notice(
    sender: TelegramSender,
    tg_user_id: int,
    header_text: str,
    reply_markup=None,
) -> None:
    await sender.send(tg_user_id, f"{header_text}\n<i>[{OVERSIZED_NOTICE}]</i>", reply_markup=reply_markup)


def _looks_like_video(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    head = data[:64]
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return True
    if head.startswith(b"RIFF") and b"AVI " in head[:16]:
        return True
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True
    return False


def _guess_filename_from_url(url: str, default: str) -> str:
    try:
        path = unquote(urlparse(url).path or "")
    except Exception:
        return default
    name = path.rsplit("/", 1)[-1]
    return name or default


def _iter_http_urls(value):
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://"):
            yield value
        return
    if isinstance(value, dict):
        preferred_keys = (
            "url", "baseUrl", "downloadUrl", "src", "source", "videoUrl",
            "videoSrc", "mp4Url", "playUrl", "streamUrl", "thumbnail",
        )
        seen = set()
        for key in preferred_keys:
            if key in value:
                for item in _iter_http_urls(value.get(key)):
                    if item not in seen:
                        seen.add(item)
                        yield item
        for nested in value.values():
            for item in _iter_http_urls(nested):
                if item not in seen:
                    seen.add(item)
                    yield item
        return
    if isinstance(value, list):
        seen = set()
        for nested in value:
            for item in _iter_http_urls(nested):
                if item not in seen:
                    seen.add(item)
                    yield item


async def _download_video_with_fallback(attach: dict, client: MaxClient) -> tuple[bytes | None, str]:
    urls = list(_iter_http_urls(attach))
    thumb = attach.get("thumbnail")

    for url in urls:
        if not url or url == thumb:
            continue
        data = await _download_limited(client, url, "video")
        if _looks_like_video(data or b""):
            return data, _guess_filename_from_url(url, "video.mp4")

    return None, "video.mp4"


def _meaningful_attaches(attaches: list) -> list[dict]:
    return [
        a for a in attaches
        if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
    ]


async def _prepare_album_item(attach: dict, client: MaxClient) -> dict | None:
    atype = attach.get("_type", "")

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            return None
        if _is_oversized(attach, "photo"):
            return None
        data = await _download_limited(client, url, "photo")
        if not data:
            return None
        return {"kind": "photo", "data": data, "filename": "photo.jpg"}

    if atype == "VIDEO":
        if _is_oversized(attach, "video"):
            return None
        data, filename = await _download_video_with_fallback(attach, client)
        if data:
            return {"kind": "video", "data": data, "filename": filename}
        thumb = attach.get("thumbnail")
        if not thumb:
            return None
        data = await _download_limited(client, thumb, "photo")
        if not data:
            return None
        return {"kind": "photo", "data": data, "filename": "video_preview.jpg"}

    if atype == "FILE":
        name = attach.get("name", "file")
        token_url = _extract_file_url(attach)
        if not token_url:
            return None
        kind = _guess_media_kind(name)
        if kind not in ("photo", "video"):
            return None
        if _is_oversized(attach, kind):
            return None
        data = await _download_limited(client, token_url, kind)
        if not data:
            return None
        return {"kind": kind, "data": data, "filename": name}

    return None


async def _send_attaches(
    attaches: list[dict],
    text: str,
    header_text: str,
    client: MaxClient,
    sender: TelegramSender,
    tg_user_id: int,
    kb=None,
) -> None:
    meaningful_attaches = _meaningful_attaches(attaches)
    if not meaningful_attaches:
        if text:
            await sender.send(tg_user_id, f"{header_text}\n{escape(text)}", reply_markup=kb)
        else:
            await sender.send(tg_user_id, f"{header_text}\n<i>[без содержимого]</i>", reply_markup=kb)
        return
    skipped_count = max(0, len(meaningful_attaches) - MAX_ATTACHMENTS_PER_MESSAGE)
    if skipped_count:
        log.warning(
            "Message has too many attachments; processing first %d skipped=%d",
            MAX_ATTACHMENTS_PER_MESSAGE,
            skipped_count,
        )
        meaningful_attaches = meaningful_attaches[:MAX_ATTACHMENTS_PER_MESSAGE]

    album_candidates: list[dict] = []
    album_indexes: list[int] = []
    for idx, attach in enumerate(meaningful_attaches):
        prepared = await _prepare_album_item(attach, client)
        if prepared is None:
            continue
        album_candidates.append(prepared)
        album_indexes.append(idx)

    caption = f"{header_text}\n{escape(text)}" if text else header_text
    used_album = False

    album_index_set = set(album_indexes[:10])

    if len(album_candidates) >= 2:
        used_album = await sender.send_media_group(tg_user_id, album_candidates[:10], caption=caption)
        if used_album:
            log.info("Forwarded media group -> TG items=%d", min(len(album_candidates), 10))
            if kb:
                await sender.send(tg_user_id, "↩️ <i>Ответ доступен кнопкой ниже</i>", reply_markup=kb)

    for i, attach in enumerate(meaningful_attaches):
        if used_album and i in album_index_set:
            continue
        cap = header_text
        if not used_album and i == 0 and text:
            cap = caption
        await _send_attach(attach, client, sender, tg_user_id, cap, kb=kb)
        log.debug("Forwarded attach _type=%s -> TG", attach.get("_type"))
    if skipped_count:
        await sender.send(
            tg_user_id,
            f"{header_text}\n<i>[пропущено вложений сверх лимита: {skipped_count}]</i>",
        )


async def _send_attach(
    attach: dict,
    client: MaxClient,
    sender: TelegramSender,
    tg_user_id: int,
    header_text: str,
    kb=None,
) -> bool:
    """Process and send a single attachment. Returns True if handled."""
    atype = attach.get("_type", "")
    log.debug("Processing attach _type=%s keys=%s", atype, list(attach.keys()))

    if atype == "CONTROL" or atype == "WIDGET" or atype == "INLINE_KEYBOARD":
        return False

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            log.warning("PHOTO attach has no URL; keys=%s", list(attach.keys()))
            return False
        if _is_oversized(attach, "photo"):
            await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
            return True
        data = await _download_limited(client, url, "photo")
        if data:
            await sender.send_photo(tg_user_id, data, caption=header_text, reply_markup=kb)
            return True
        await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
        return True

    if atype == "VIDEO":
        if _is_oversized(attach, "video"):
            await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
            return True
        data, filename = await _download_video_with_fallback(attach, client)
        if data:
            sent = await sender.send_video(tg_user_id, data, caption=header_text, filename=filename, reply_markup=kb)
            if sent:
                return True
            log.warning("Failed to send VIDEO as video, falling back to preview")
        thumb = attach.get("thumbnail")
        if thumb:
            data = await _download_limited(client, thumb, "photo")
            if data:
                await sender.send_photo(
                    tg_user_id, data, caption=f"{header_text}\n<i>[видео — превью]</i>", reply_markup=kb
                )
                return True
        await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
        return True

    if atype == "FILE":
        name = attach.get("name", "file")
        size = _attachment_declared_size(attach)
        token_url = _extract_file_url(attach)
        if token_url:
            kind = _guess_media_kind(name)
            if _is_oversized(attach, kind):
                await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
                return True
            data = await _download_limited(client, token_url, kind)
            if data:
                if kind == "photo":
                    await sender.send_photo(tg_user_id, data, caption=header_text, filename=name, reply_markup=kb)
                elif kind == "video":
                    sent = await sender.send_video(tg_user_id, data, caption=header_text, filename=name, reply_markup=kb)
                    if not sent:
                        await sender.send_document(tg_user_id, data, caption=header_text, filename=name, reply_markup=kb)
                else:
                    await sender.send_document(tg_user_id, data, caption=header_text, filename=name, reply_markup=kb)
                return True
            await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
            return True
        size_str = f" ({_human_size(size)})" if size else ""
        await sender.send(tg_user_id, f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}", reply_markup=kb)
        return True

    if atype == "AUDIO":
        url = attach.get("url")
        if url:
            if _is_oversized(attach, "document"):
                await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
                return True
            data = await _download_limited(client, url, "document")
            if data:
                await sender.send_voice(tg_user_id, data, caption=header_text, reply_markup=kb)
                return True
            await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
            return True
        await sender.send(tg_user_id, f"{header_text}\n<i>[аудио]</i>", reply_markup=kb)
        return True

    if atype == "STICKER":
        url = attach.get("url")
        if url:
            if _is_oversized(attach, "photo"):
                await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
                return True
            data = await _download_limited(client, url, "photo")
            if data:
                await sender.send_sticker(tg_user_id, data, reply_markup=kb)
                return True
            await _send_oversized_notice(sender, tg_user_id, header_text, reply_markup=kb)
            return True
        await sender.send(tg_user_id, f"{header_text}\n<i>[стикер]</i>", reply_markup=kb)
        return True

    if atype == "SHARE":
        share_url = attach.get("url", "")
        title = attach.get("title", "")
        desc = attach.get("description", "")
        parts = [header_text]
        if title:
            parts.append(f"🔗 <b>{escape(title)}</b>")
        if share_url:
            parts.append(escape(share_url))
        if desc:
            parts.append(f"<i>{escape(desc[:200])}</i>")
        await sender.send(tg_user_id, "\n".join(parts), reply_markup=kb)
        return True

    if atype == "LOCATION":
        lat = attach.get("lat") or attach.get("latitude")
        lon = attach.get("lon") or attach.get("lng") or attach.get("longitude")
        if lat and lon:
            await sender.send(tg_user_id, f"{header_text}\n📍 {lat}, {lon}", reply_markup=kb)
        else:
            await sender.send(tg_user_id, f"{header_text}\n<i>[геолокация]</i>", reply_markup=kb)
        return True

    if atype == "CONTACT":
        name = attach.get("name", "")
        phone = attach.get("phone", "")
        text = f"{header_text}\n👤 {escape(name)}"
        if phone:
            text += f" — {escape(phone)}"
        await sender.send(tg_user_id, text, reply_markup=kb)
        return True

    log.info("Unknown attach type %s, sending as info", atype)
    await sender.send(tg_user_id, f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>", reply_markup=kb)
    return True


async def _handle_linked_message(
    link: dict,
    link_type: str,
    header_text: str,
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    tg_user_id: int,
    kb=None,
) -> None:
    """Handle FORWARD or REPLY link inside a message."""
    inner = link.get("message") or link
    fwd_sender_id = inner.get("sender") or link.get("sender")
    fwd_text = inner.get("text", "") or link.get("text", "")
    fwd_attaches = inner.get("attaches") or link.get("attaches") or []

    fwd_sender_label = ""
    if fwd_sender_id:
        fwd_sender_label = escape(await resolver.resolve_user(fwd_sender_id))

    if link_type == "FORWARD":
        prefix = "↩️ <b>Переслано</b>"
        if fwd_sender_label:
            prefix = f"↩️ <b>Переслано от {fwd_sender_label}</b>"
    else:
        prefix = "↩ <b>Ответ</b>"
        if fwd_sender_label:
            prefix = f"↩ <b>Ответ на {fwd_sender_label}</b>"

    full_header = f"{header_text}\n{prefix}"

    await _send_attaches(
        attaches=fwd_attaches,
        text=fwd_text,
        header_text=full_header,
        client=client,
        sender=sender,
        tg_user_id=tg_user_id,
        kb=kb,
    )


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def create_max_client(
    account_id: int,
    tg_user_id: int,
    max_token: str,
    max_device_id: str,
    sender: TelegramSender,
    stats_callback: Callable[[str], Awaitable[None]] | None = None,
    account_label: str = "",
    debug: bool = False, reply_enabled: bool = False,
) -> MaxClient:
    client = MaxClient(token=max_token, device_id=max_device_id, debug=debug, account_id=account_id)
    resolver = ContactResolver(client=client)

    _recent_messages = []

    @client.on_ready
    async def handle_ready(snapshot: dict):
        participant_ids = resolver.load_snapshot(snapshot)

        if participant_ids:
            log.info("Batch-resolving participants account=%s count=%d", account_id, len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info(
                "Resolver cache account=%s chats=%d users=%d",
                account_id,
                len(resolver.chats),
                len(resolver.users),
            )
            log.debug("Known chats masked account=%s: %s", account_id, mask_mapping_values(resolver.chats))
            log.debug("Known users masked account=%s: %s", account_id, mask_mapping_values(resolver.users))

    @client.on_message
    async def handle_message(msg: MaxMessage):
        msg_sig = msg.message_id or f"{msg.chat_id}_{msg.timestamp}_{hash(msg.text)}"
        if msg_sig in _recent_messages:
            log.info("Skipped duplicate message account=%s sig=%s", account_id, msg_sig)
            return
        _recent_messages.append(msg_sig)
        if len(_recent_messages) > 1000:
            _recent_messages.pop(0)

        log.info(
            "New message account=%s chat=%s sender=%s is_self=%s attaches=%d",
            account_id,
            msg.chat_id,
            msg.sender_id,
            msg.is_self,
            len(msg.attaches),
        )

        if msg.is_self:
            return

        resolver.update_chat_from_event(msg.raw, msg.chat_id)
        await resolver.ensure_chat_meta(msg.chat_id)
        is_channel = resolver.is_channel(msg.chat_id)
        is_dm = resolver.is_dm(msg.chat_id)
        sender_name = await resolver.resolve_user(msg.sender_id)
        sender_missing = not sender_name or sender_name == "None" or sender_name == "Неизвестный"
        if is_channel and sender_missing:
            sender_label = ""
        else:
            sender_label = escape(sender_name if not sender_missing else "Неизвестный")
        chat_label = escape(resolver.chat_name(msg.chat_id))
        header_text = _header(sender_label, chat_label, is_dm, account_label=account_label)
        kb = reply_keyboard(account_id, msg.chat_id, is_dm) if reply_enabled and not is_channel else None
        if reply_enabled and is_channel:
            log.debug(
                "Reply button hidden for channel account=%s chat=%s type=%s",
                account_id,
                msg.chat_id,
                resolver.chat_type(msg.chat_id),
            )

        if stats_callback:
            if is_channel:
                incoming_metric = "forward_channel"
            else:
                incoming_metric = "forward_dm" if is_dm else "forward_group"
            try:
                await stats_callback(incoming_metric)
            except Exception:
                log.exception("Failed to write report metric=%s", incoming_metric)

        link = msg.link
        link_type = link.get("type") if isinstance(link, dict) else None

        if link_type in ("FORWARD", "REPLY"):
            await _handle_linked_message(link, link_type, header_text, client, sender, resolver, tg_user_id, kb=kb)
            if msg.text:
                await sender.send(tg_user_id, f"{header_text}\n{escape(msg.text)}", reply_markup=kb)
            log.info("Forwarded link type=%s -> TG", link_type)
            return

        if _meaningful_attaches(msg.attaches):
            await _send_attaches(
                attaches=msg.attaches,
                text=msg.text,
                header_text=header_text,
                client=client,
                sender=sender,
                tg_user_id=tg_user_id,
                kb=kb,
            )
        else:
            body = escape(msg.text) if msg.text else "<i>[нетекстовое сообщение]</i>"
            await sender.send(tg_user_id, f"{header_text}\n{body}", reply_markup=kb)
            log.info("Forwarded text -> TG")

    return client
