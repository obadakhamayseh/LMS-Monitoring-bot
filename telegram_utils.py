"""Shared Telegram HTML formatting, safe message splitting, and rate-limit-aware senders (H12)."""

from __future__ import annotations

import asyncio
import html
import inspect
import logging
import re
from typing import Any, Awaitable, Callable, Optional, Union
from urllib.parse import urlsplit

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

logger = logging.getLogger(__name__)

# Telegram Bot API supports up to 4096 UTF-8 characters per message; 4000 leaves safe headroom.
DEFAULT_MAX_LEN = 4000

# Inline/block formatting tags supported by Telegram's HTML parse mode.
_TRACKED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "a",
        "blockquote",
        "tg-spoiler",
    }
)

_TOKEN_RE = re.compile(
    r"(<[^>]+>|&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);|\n|[ \t]+|[^\n \t<&]+|.)"
)
_OPEN_TAG_RE = re.compile(r"^<\s*([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>$")
_CLOSE_TAG_RE = re.compile(r"^<\s*/\s*([a-zA-Z][a-zA-Z0-9-]*)\s*>$")
_SELF_CLOSING_RE = re.compile(r"/\s*>$")

ForbiddenCallback = Callable[[int], Union[None, Awaitable[None]]]


def escape_html(text: Any) -> str:
    """Safely escape any value for Telegram HTML parse mode."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def html_link(text: Any, url: Optional[str]) -> str:
    """Render an HTML <a> tag if url is a valid http(s) URL, otherwise escaped plain text."""
    label = escape_html(text)
    raw_url = (url or "").strip()
    if raw_url:
        try:
            scheme = urlsplit(raw_url).scheme.lower()
        except ValueError:
            scheme = ""
        if scheme in ("http", "https"):
            return f'<a href="{escape_html(raw_url)}">{label}</a>'
    return label


def _closing_tags(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _opening_tags(stack: list[tuple[str, str]]) -> str:
    return "".join(raw for _, raw in stack)


def _apply_tag(stack: list[tuple[str, str]], token: str) -> list[tuple[str, str]]:
    if not token.startswith("<") or not token.endswith(">") or _SELF_CLOSING_RE.search(token):
        return stack
    m_close = _CLOSE_TAG_RE.match(token)
    if m_close:
        tname = m_close.group(1).lower()
        if tname in _TRACKED_TAGS:
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == tname:
                    return stack[:idx] + stack[idx + 1 :]
        return stack
    m_open = _OPEN_TAG_RE.match(token)
    if m_open:
        tname = m_open.group(1).lower()
        if tname in _TRACKED_TAGS:
            return stack + [(tname, token)]
    return stack


def _is_atomic_markup(token: str) -> bool:
    if token.startswith("<") and token.endswith(">"):
        return True
    if token.startswith("&") and token.endswith(";") and len(token) >= 3:
        return True
    return False


def split_html_message(text: str, max_len: int = DEFAULT_MAX_LEN) -> list[str]:
    """Split a long Telegram HTML message into valid chunks <= max_len without truncation.

    - Prefers paragraph (\\n\\n), then line (\\n), then whitespace boundaries.
    - Never splits inside an HTML <...> tag or &...; entity.
    - Closes open formatting tags at the end of a chunk and reopens them in the next chunk.
    """
    if not text:
        return [""]
    if max_len < 16:
        max_len = 16
    if len(text) <= max_len:
        return [text]

    tokens = _TOKEN_RE.findall(text)
    chunks: list[str] = []

    buf: list[str] = []
    # Track the tag stack state AFTER each token in buf, parallel to buf.
    buf_stacks: list[list[tuple[str, str]]] = []
    buf_len = 0
    prefix_len = 0  # number of reopened opening-tag tokens at start of buf
    open_stack: list[tuple[str, str]] = []

    def _reset_with_stack(stack: list[tuple[str, str]]) -> None:
        nonlocal buf, buf_stacks, buf_len, prefix_len, open_stack
        open_stack = list(stack)
        buf = [raw for _, raw in open_stack]
        buf_stacks = [open_stack[: i + 1] for i in range(len(open_stack))]
        buf_len = sum(len(tok) for tok in buf)
        prefix_len = len(buf)

    def _find_best_cut() -> Optional[int]:
        """Return index in buf to split at (excluding the break token if newline/space), or None."""
        if len(buf) <= prefix_len:
            return None

        # 1. Paragraph boundary (\n\n) if it captures a reasonable portion of the chunk
        min_para_chars = max(1, int(max_len * 0.35))
        running = 0
        best_para = None
        best_line = None
        best_space = None
        for i, tok in enumerate(buf):
            running += len(tok)
            if i < prefix_len:
                continue
            if tok == "\n":
                if i > prefix_len and buf[i - 1] == "\n" and running >= min_para_chars:
                    best_para = i
                best_line = i
            elif tok.strip() == "" and i > prefix_len:
                best_space = i

        if best_para is not None and best_para > prefix_len:
            return best_para
        if best_line is not None and best_line > prefix_len:
            return best_line
        if best_space is not None and best_space > prefix_len:
            return best_space
        return None

    def _emit_chunk(head_tokens: list[str], end_stack: list[tuple[str, str]]) -> None:
        chunk_str = "".join(head_tokens) + _closing_tags(end_stack)
        if chunk_str:
            chunks.append(chunk_str)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        next_stack = _apply_tag(open_stack, tok)
        closing_needed = len(_closing_tags(next_stack))

        if buf_len + len(tok) + closing_needed <= max_len:
            buf.append(tok)
            buf_stacks.append(next_stack)
            buf_len += len(tok)
            open_stack = next_stack
            i += 1
            continue

        # Current token doesn't fit. Try flushing at the best boundary inside buf.
        if len(buf) > prefix_len:
            cut = _find_best_cut()
            if cut is not None:
                # Split BEFORE the boundary token at `cut` (drop a single separator newline/space)
                head = buf[:cut]
                cut_stack = buf_stacks[cut - 1] if cut > 0 else []
                tail = buf[cut + 1 :]  # drop separator token itself
                _emit_chunk(head, cut_stack)
                _reset_with_stack(cut_stack)
                # Re-queue tail before current token
                if tail:
                    tokens = tail + tokens[i:]
                    i = 0
                continue
            else:
                # Flush entire buffer before current token
                _emit_chunk(buf, open_stack)
                _reset_with_stack(open_stack)
                continue

        # Buffer only has reopened prefix tags and a single token still exceeds max_len.
        if not _is_atomic_markup(tok) and len(tok) > 1:
            avail = max_len - buf_len - len(_closing_tags(open_stack))
            if avail < 1:
                # Even reopened tags fill max_len; take at least 1 char to guarantee progress.
                avail = 1
            part = tok[:avail]
            rem = tok[avail:]
            buf.append(part)
            buf_stacks.append(open_stack)
            buf_len += len(part)
            _emit_chunk(buf, open_stack)
            _reset_with_stack(open_stack)
            tokens[i] = rem
            continue

        # Indivisible HTML tag or entity larger than remaining room: include intact to never corrupt markup.
        buf.append(tok)
        buf_stacks.append(next_stack)
        buf_len += len(tok)
        open_stack = next_stack
        i += 1

    if len(buf) > prefix_len or (buf and not chunks):
        _emit_chunk(buf, open_stack)

    return chunks if chunks else [""]


async def _invoke_forbidden_cb(
    on_forbidden: Optional[ForbiddenCallback], chat_id: int
) -> None:
    if on_forbidden is None:
        return
    try:
        res = on_forbidden(chat_id)
        if inspect.isawaitable(res):
            await res
    except Exception as cb_err:
        logger.warning("on_forbidden callback failed for chat %s: %s", chat_id, cb_err)


def _is_not_modified_error(exc: BadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


def _is_parse_entities_error(exc: BadRequest) -> bool:
    msg = str(exc).lower()
    return "can't parse entities" in msg or "cant parse entities" in msg or "unsupported start tag" in msg


async def _call_with_retries(
    send_fn: Callable[[Optional[ParseMode]], Awaitable[Any]],
    *,
    chat_id: int,
    on_forbidden: Optional[ForbiddenCallback] = None,
    max_retries: int = 3,
    ignore_not_modified: bool = False,
) -> bool:
    """Execute a Telegram send/edit callable with RetryAfter backoff, Forbidden hook, and HTML fallback."""
    attempt = 0
    while True:
        try:
            await send_fn(ParseMode.HTML)
            return True
        except RetryAfter as exc:
            if attempt >= max_retries:
                logger.error(
                    "Telegram RetryAfter exhausted after %d retries for chat %s: %s",
                    max_retries,
                    chat_id,
                    exc,
                )
                return False
            raw_retry = exc.retry_after
            retry_secs = (
                raw_retry.total_seconds()
                if hasattr(raw_retry, "total_seconds")
                else float(raw_retry)
            )
            delay = retry_secs + 0.5
            logger.warning(
                "Telegram rate limit (RetryAfter=%ss) for chat %s; sleeping %.1fs (attempt %d/%d)",
                exc.retry_after,
                chat_id,
                delay,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(delay)
            attempt += 1
        except Forbidden as exc:
            logger.warning("Telegram Forbidden for chat %s (bot blocked/kicked): %s", chat_id, exc)
            await _invoke_forbidden_cb(on_forbidden, chat_id)
            return False
        except BadRequest as exc:
            if ignore_not_modified and _is_not_modified_error(exc):
                return True
            if _is_parse_entities_error(exc):
                logger.warning("HTML parse error for chat %s (%s); retrying plain text.", chat_id, exc)
                try:
                    await send_fn(None)
                    return True
                except Forbidden as f_exc:
                    logger.warning("Telegram Forbidden on plain fallback for chat %s: %s", chat_id, f_exc)
                    await _invoke_forbidden_cb(on_forbidden, chat_id)
                    return False
                except Exception as plain_err:
                    logger.error("Plain-text fallback also failed for chat %s: %s", chat_id, plain_err)
                    return False
            logger.error("Telegram BadRequest for chat %s: %s", chat_id, exc)
            return False
        except TelegramError as exc:
            logger.error("Telegram error for chat %s: %s", chat_id, exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error sending Telegram message to chat %s: %s", chat_id, exc)
            return False


async def send_html_message(
    bot: Any,
    chat_id: int,
    text: str,
    *,
    reply_markup: Any = None,
    disable_web_page_preview: bool = True,
    on_forbidden: Optional[ForbiddenCallback] = None,
    max_retries: int = 3,
    max_len: int = DEFAULT_MAX_LEN,
) -> bool:
    """Send an HTML message via bot.send_message, safely splitting long text and handling RetryAfter/Forbidden.

    Returns True iff all chunks were delivered successfully.
    """
    chunks = split_html_message(text, max_len=max_len)
    last_idx = len(chunks) - 1
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == last_idx else None

        async def _send_chunk(parse_mode: Optional[ParseMode], _chunk: str = chunk, _markup: Any = markup):
            return await bot.send_message(
                chat_id=chat_id,
                text=_chunk,
                parse_mode=parse_mode,
                reply_markup=_markup,
                disable_web_page_preview=disable_web_page_preview,
            )

        ok = await _call_with_retries(
            _send_chunk,
            chat_id=chat_id,
            on_forbidden=on_forbidden,
            max_retries=max_retries,
        )
        if not ok:
            return False
    return True


async def safe_edit_or_reply_html(
    obj: Any,
    text: str,
    *,
    reply_markup: Any = None,
    edit: bool = False,
    disable_web_page_preview: bool = True,
    on_forbidden: Optional[ForbiddenCallback] = None,
    max_retries: int = 3,
    max_len: int = DEFAULT_MAX_LEN,
) -> bool:
    """Edit or reply to a Telegram Message with HTML, safely splitting across multiple messages if long."""
    chat_id = getattr(obj, "chat_id", None) or getattr(getattr(obj, "chat", None), "id", 0)
    chunks = split_html_message(text, max_len=max_len)
    last_idx = len(chunks) - 1

    for idx, chunk in enumerate(chunks):
        use_edit = edit and idx == 0
        markup = reply_markup if idx == last_idx else None

        async def _send_chunk(
            parse_mode: Optional[ParseMode],
            _chunk: str = chunk,
            _markup: Any = markup,
            _edit: bool = use_edit,
        ):
            kw = dict(
                text=_chunk,
                parse_mode=parse_mode,
                reply_markup=_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
            return await obj.edit_text(**kw) if _edit else await obj.reply_text(**kw)

        ok = await _call_with_retries(
            _send_chunk,
            chat_id=chat_id,
            on_forbidden=on_forbidden,
            max_retries=max_retries,
            ignore_not_modified=use_edit,
        )
        if not ok:
            return False
    return True


async def safe_edit_message_text_html(
    bot: Any,
    chat_id: int,
    message_id: Optional[int],
    text: str,
    *,
    reply_markup: Any = None,
    disable_web_page_preview: bool = True,
    on_forbidden: Optional[ForbiddenCallback] = None,
    max_retries: int = 3,
    max_len: int = DEFAULT_MAX_LEN,
) -> bool:
    """Edit a message by chat_id/message_id with HTML, sending overflow chunks via send_html_message."""
    chunks = split_html_message(text, max_len=max_len)
    last_idx = len(chunks) - 1

    first_markup = reply_markup if last_idx == 0 else None

    async def _edit_first(parse_mode: Optional[ParseMode]):
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=chunks[0],
            parse_mode=parse_mode,
            reply_markup=first_markup,
            disable_web_page_preview=disable_web_page_preview,
        )

    ok = await _call_with_retries(
        _edit_first,
        chat_id=chat_id,
        on_forbidden=on_forbidden,
        max_retries=max_retries,
        ignore_not_modified=True,
    )
    if not ok:
        return False

    for idx in range(1, len(chunks)):
        markup = reply_markup if idx == last_idx else None
        sent = await send_html_message(
            bot,
            chat_id,
            chunks[idx],
            reply_markup=markup,
            disable_web_page_preview=disable_web_page_preview,
            on_forbidden=on_forbidden,
            max_retries=max_retries,
            max_len=max_len,
        )
        if not sent:
            return False
    return True
