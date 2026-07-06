from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .config import COMMAND_PREFIX, command_usage
from .core import MENTION_PATTERN, TriGuessService, split_command_args, strip_mention_tokens

try:
    event_api = importlib.import_module("astrbot.api.event")
    star_api = importlib.import_module("astrbot.api.star")
    filter = event_api.filter
    Star = star_api.Star
    register = star_api.register
except Exception:  # pragma: no cover - lets core tests import without AstrBot installed.
    class Star:
        def __init__(self, context: Any | None = None):
            self.context = context

    class _Filter:
        @staticmethod
        def command(*_args: Any, **_kwargs: Any):
            def decorator(func: Any) -> Any:
                return func

            return decorator

    filter = _Filter()  # type: ignore[assignment]

    def register(*_args: Any, **_kwargs: Any):
        def decorator(cls: Any) -> Any:
            return cls

        return decorator


@register("tri_guess", "nipz", "QQ 群三态竞猜记分小游戏", "1.0.12")
class TriGuessPlugin(Star):
    def __init__(self, context: Any):
        super().__init__(context)
        base_dir = Path(__file__).resolve().parent
        self.service = TriGuessService(base_dir / "data" / "tri_guess.sqlite3")

    @filter.command(COMMAND_PREFIX)
    async def guess(self, event: Any):
        if self._is_group_event(event) and not self._is_explicitly_at_bot(event):
            self._stop_event(event)
            return
        self._stop_event(event)

        args = split_command_args(self._message_text(event), COMMAND_PREFIX)
        subcommand, _, rest = args.partition(" ")
        subcommand = subcommand.lower().strip()
        rest = rest.strip()

        if subcommand in {"", "help"}:
            yield self._reply(event, self.service.help(), target_sender=True, quote=True)
            return

        if not self._is_group_event(event):
            yield self._reply(event, "本功能仅支持群聊使用。", target_sender=True, quote=True)
            return

        if subcommand in {"start", "start_guess"}:
            yield self._reply(event, self.service.start_guess(self._group_feature_id(event), self._user_id(event), rest))
            return

        if subcommand == "bet":
            yield self._reply(
                event,
                self.service.bet(
                    self._group_feature_id(event),
                    self._user_id(event),
                    rest,
                    user_label=self._user_label(event),
                ),
                target_sender=True,
                quote=True,
            )
            return

        if subcommand == "settle":
            text = self.service.settle(self._group_feature_id(event), rest)
            yield self._reply(event, text, target_sender=not self._has_mention_token(text))
            return

        if subcommand == "cancel":
            yield self._reply(event, self.service.cancel(self._group_feature_id(event)), target_sender=True)
            return

        if subcommand == "current":
            yield self._reply(event, self.service.current(self._group_feature_id(event), self._user_id(event)), target_sender=True, quote=True)
            return

        if subcommand == "score":
            yield self._reply(event, self.service.score(self._group_feature_id(event), self._user_id(event)), target_sender=True, quote=True)
            return

        if subcommand == "history":
            yield self._reply(event, self.service.history(self._group_feature_id(event), self._user_id(event)), target_sender=True, quote=True)
            return

        yield self._reply(event, f"未知子命令，请使用 {command_usage('help')} 查看帮助。", target_sender=True, quote=True)

    def _plain(self, event: Any, text: str) -> Any:
        plain_result = getattr(event, "plain_result", None)
        if callable(plain_result):
            return plain_result(text)
        return text

    def _reply(self, event: Any, text: str, target_sender: bool = False, quote: bool = False) -> Any:
        fallback_text = self._fallback_reply_text(event, text, target_sender)
        if quote:
            native = self._native_quote_result(event, fallback_text)
            if native is not None:
                return native
        chain = self._reply_chain(event, text, target_sender=target_sender, quote=quote)
        chain_result = getattr(event, "chain_result", None)
        if chain and callable(chain_result):
            try:
                result = chain_result(chain)
                use_markdown = getattr(result, "use_markdown", None)
                if callable(use_markdown):
                    use_markdown(False)
                return result
            except Exception:
                pass
        return self._plain(event, fallback_text)

    def _native_quote_result(self, event: Any, text: str) -> Any | None:
        for method in ("reply_result", "quote_result"):
            func = getattr(event, method, None)
            if callable(func):
                try:
                    return func(text)
                except TypeError:
                    continue
        return None

    def _reply_chain(self, event: Any, text: str, target_sender: bool = False, quote: bool = False) -> list[Any] | None:
        try:
            components = importlib.import_module("astrbot.api.message_components")
        except Exception:
            return None
        plain_cls = getattr(components, "Plain", None)
        reply_cls = getattr(components, "Reply", None)
        at_cls = getattr(components, "At", None)
        if not plain_cls:
            return None
        if self._is_qq_official_event(event):
            text = self._qq_official_text(event, text, target_sender)
            return [plain_cls(text)] if text else None
        chain: list[Any] = []
        message_id = self._message_id(event) if quote else ""
        if message_id and reply_cls:
            reply = self._make_reply_component(reply_cls, message_id)
            if reply is not None:
                chain.append(reply)
        if target_sender and at_cls:
            at = self._make_at_component(at_cls, self._user_id(event), self._user_label(event))
            if at is not None:
                chain.extend([at, plain_cls("\n")])
        has_inline_mention = False
        for part in self._message_parts(text):
            if part["type"] == "text":
                if part["text"]:
                    chain.append(plain_cls(part["text"]))
            elif at_cls:
                at = self._make_at_component(at_cls, part["user_id"], part["label"])
                if at is not None:
                    has_inline_mention = True
                    chain.append(at)
                elif part["label"]:
                    chain.append(plain_cls(f"@{part['label']}"))
        if not chain or (len(chain) == 1 and not message_id and not target_sender and not has_inline_mention):
            return None
        return chain

    def _message_parts(self, text: str) -> list[dict[str, str]]:
        parts: list[dict[str, str]] = []
        cursor = 0
        for match in MENTION_PATTERN.finditer(text):
            if match.start() > cursor:
                parts.append({"type": "text", "text": text[cursor : match.start()]})
            parts.append({"type": "at", "user_id": match.group(1), "label": match.group(2)})
            cursor = match.end()
        if cursor < len(text):
            parts.append({"type": "text", "text": text[cursor:]})
        if not parts:
            parts.append({"type": "text", "text": text})
        return parts

    def _make_reply_component(self, reply_cls: Any, message_id: str) -> Any | None:
        for kwargs in ({"id": message_id}, {"message_id": message_id}, {"msg_id": message_id}):
            try:
                return reply_cls(**kwargs)
            except TypeError:
                continue
        try:
            return reply_cls(message_id)
        except TypeError:
            return None

    def _make_at_component(self, at_cls: Any, user_id: str, label: str = "") -> Any | None:
        for kwargs in ({"qq": user_id, "name": label}, {"qq": user_id}, {"user_id": user_id}, {"id": user_id}):
            try:
                return at_cls(**kwargs)
            except TypeError:
                continue
        return None

    def _fallback_reply_text(self, event: Any, text: str, target_sender: bool) -> str:
        rendered = strip_mention_tokens(text)
        if not target_sender:
            return rendered
        return f"@{self._user_label(event)}\n{rendered}"

    def _has_mention_token(self, text: str) -> bool:
        return MENTION_PATTERN.search(text) is not None

    def _qq_official_text(self, event: Any, text: str, target_sender: bool) -> str:
        parts: list[str] = []
        if target_sender:
            parts.append(f"@{self._user_label(event)}\n")
        for part in self._message_parts(text):
            if part["type"] == "text":
                parts.append(part["text"])
            else:
                parts.append(f"@{part['label'] or part['user_id']}")
        return "".join(parts)

    def _is_qq_official_event(self, event: Any) -> bool:
        if self._is_onebot_event(event):
            return False
        platform = self._first_event_value(
            event,
            methods=("get_platform_name",),
            attrs=("platform_name",),
        )
        return platform == "qq_official"

    def _is_onebot_event(self, event: Any) -> bool:
        platform = self._first_event_value(
            event,
            methods=("get_platform_name", "get_adapter_name"),
            attrs=("platform_name", "adapter_name"),
        ).lower()
        if platform in {"aiocqhttp", "onebot", "onebot11", "napcat", "napcatqq"}:
            return True
        raw = self._raw_message_object(event)
        if raw is None:
            return False
        raw_type = f"{raw.__class__.__module__}.{raw.__class__.__name__}".lower()
        if "aiocqhttp" in raw_type or "onebot" in raw_type or "napcat" in raw_type:
            return True
        post_type = self._object_value(raw, "post_type")
        message_type = self._object_value(raw, "message_type")
        user_id = self._object_value(raw, "user_id")
        group_id = self._object_value(raw, "group_id")
        if (post_type or message_type) and user_id:
            return True
        return bool(user_id and group_id and str(user_id).isdigit())

    def _message_text(self, event: Any) -> str:
        value = getattr(event, "message_str", None)
        if isinstance(value, str):
            return value
        func = getattr(event, "get_message_str", None)
        if callable(func):
            value = func()
            if isinstance(value, str):
                return value
        return ""

    def _is_explicitly_at_bot(self, event: Any) -> bool:
        for method in ("is_at_bot", "is_at"):
            func = getattr(event, method, None)
            if callable(func):
                try:
                    result = func()
                except TypeError:
                    continue
                if result is not None:
                    return bool(result)
        for text in self._raw_text_candidates(event):
            if "[At:" in text or "[at:" in text.lower():
                return True
        for component in self._message_components(event):
            name = component.__class__.__name__.lower()
            if name == "at" or name.endswith(".at"):
                return True
            ctype = getattr(component, "type", "")
            if str(ctype).lower() == "at":
                return True
        return False

    def _raw_text_candidates(self, event: Any) -> list[str]:
        candidates: list[str] = []
        for attr in ("raw_message", "message_text", "message_str"):
            value = getattr(event, attr, None)
            if isinstance(value, str):
                candidates.append(value)
        for method in ("get_message_str", "get_message_text", "get_plain_text"):
            func = getattr(event, method, None)
            if callable(func):
                try:
                    value = func()
                except TypeError:
                    continue
                if isinstance(value, str):
                    candidates.append(value)
        return candidates

    def _message_components(self, event: Any) -> list[Any]:
        for attr in ("message_chain", "message", "messages"):
            value = getattr(event, attr, None)
            if isinstance(value, list):
                return value
        message_obj = getattr(event, "message_obj", None)
        if message_obj:
            for attr in ("message", "message_chain", "messages"):
                value = getattr(message_obj, attr, None)
                if isinstance(value, list):
                    return value
        return []

    def _message_id(self, event: Any) -> str:
        onebot_message_id = self._onebot_message_id(event)
        if onebot_message_id:
            return onebot_message_id
        value = self._first_event_value(
            event,
            methods=("get_message_id", "get_msg_id"),
            attrs=("message_id", "msg_id", "id"),
        )
        if value:
            return value
        message_obj = getattr(event, "message_obj", None)
        if message_obj:
            return self._first_event_value(
                message_obj,
                methods=("get_message_id", "get_msg_id"),
                attrs=("message_id", "msg_id", "id"),
            )
        return ""

    def _stop_event(self, event: Any) -> None:
        func = getattr(event, "stop_event", None)
        if callable(func):
            func()

    def _group_id(self, event: Any) -> str:
        onebot_group_id = self._onebot_group_id(event)
        if onebot_group_id:
            return onebot_group_id
        for method in ("get_group_id", "get_session_id"):
            func = getattr(event, method, None)
            if callable(func):
                value = func()
                if value:
                    return str(value)
        for attr in ("group_id", "session_id"):
            value = getattr(event, attr, None)
            if value:
                return str(value)
        return "unknown_group"

    def _group_feature_id(self, event: Any) -> str:
        platform = self._first_event_value(
            event,
            methods=("get_platform_name", "get_platform_id", "get_adapter_name", "get_adapter_id"),
            attrs=("platform_name", "platform_id", "adapter_name", "adapter_id"),
        )
        group_id = self._group_id(event)
        session_id = self._first_event_value(
            event,
            methods=("get_session_id",),
            attrs=("session_id",),
        )
        parts = [platform or "default", group_id]
        if session_id and session_id != group_id:
            parts.append(session_id)
        return ":".join(self._normalize_feature_part(part) for part in parts)

    def _user_id(self, event: Any) -> str:
        onebot_user_id = self._onebot_sender_id(event)
        if onebot_user_id:
            return onebot_user_id
        for method in ("get_sender_id", "get_user_id"):
            func = getattr(event, method, None)
            if callable(func):
                value = func()
                if value:
                    return str(value)
        for attr in ("sender_id", "user_id"):
            value = getattr(event, attr, None)
            if value:
                return str(value)
        return "unknown_user"

    def _onebot_sender_id(self, event: Any) -> str:
        raw = self._raw_message_object(event)
        if raw is None:
            return ""
        for key in ("user_id", "sender_id"):
            value = self._object_value(raw, key)
            if value and str(value).isdigit():
                return str(value)
        sender = self._object_value(raw, "sender")
        if sender:
            for key in ("user_id", "id"):
                value = self._object_value(sender, key)
                if value and str(value).isdigit():
                    return str(value)
        return ""

    def _user_label(self, event: Any) -> str:
        value = self._first_event_value(
            event,
            methods=("get_sender_name", "get_user_name", "get_sender_nickname", "get_nickname"),
            attrs=("sender_name", "user_name", "sender_nickname", "nickname", "card"),
        )
        if value:
            return value
        sender = getattr(event, "sender", None)
        if sender:
            value = self._first_event_value(
                sender,
                methods=("get_name", "get_nickname"),
                attrs=("name", "nickname", "card", "display_name"),
            )
            if value:
                return value
        message_obj = getattr(event, "message_obj", None)
        if message_obj:
            value = self._first_event_value(
                message_obj,
                methods=("get_sender_name", "get_user_name", "get_sender_nickname", "get_nickname"),
                attrs=("sender_name", "user_name", "sender_nickname", "nickname", "card"),
            )
            if value:
                return value
        return self._user_id(event)

    def _first_event_value(self, event: Any, methods: tuple[str, ...], attrs: tuple[str, ...]) -> str:
        for method in methods:
            func = getattr(event, method, None)
            if callable(func):
                try:
                    value = func()
                except TypeError:
                    continue
                if value:
                    return str(value)
        for attr in attrs:
            value = getattr(event, attr, None)
            if value:
                return str(value)
        return ""

    def _raw_message_object(self, event: Any) -> Any | None:
        raw = getattr(event, "raw_message", None)
        if raw is not None and not isinstance(raw, str):
            return raw
        message_obj = getattr(event, "message_obj", None)
        if message_obj:
            raw = getattr(message_obj, "raw_message", None)
            if raw is not None:
                return raw
        return None

    def _object_value(self, obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        get = getattr(obj, "get", None)
        if callable(get):
            try:
                return get(key)
            except TypeError:
                pass
        return getattr(obj, key, None)

    def _normalize_feature_part(self, value: str) -> str:
        return value.strip().replace(":", "_").replace("/", "_") or "unknown"

    def _is_group_event(self, event: Any) -> bool:
        for method in ("get_group_id",):
            func = getattr(event, method, None)
            if callable(func) and func():
                return True
        for attr in ("group_id",):
            if getattr(event, attr, None):
                return True
        session_id = getattr(event, "session_id", None)
        return bool(session_id and "group" in str(session_id).lower())

    def _onebot_group_id(self, event: Any) -> str:
        raw = self._raw_message_object(event)
        if raw is None:
            return ""
        value = self._object_value(raw, "group_id")
        return str(value) if value and str(value).isdigit() else ""

    def _onebot_message_id(self, event: Any) -> str:
        raw = self._raw_message_object(event)
        if raw is None:
            return ""
        for key in ("message_id", "msg_id"):
            value = self._object_value(raw, key)
            if value:
                return str(value)
        return ""
