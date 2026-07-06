from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .config import COMMAND_PREFIX, command_usage
from .core import TriGuessService, split_command_args

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


@register("tri_guess", "nipz", "QQ 群三态竞猜记分小游戏", "1.0.6")
class TriGuessPlugin(Star):
    def __init__(self, context: Any):
        super().__init__(context)
        base_dir = Path(__file__).resolve().parent
        self.service = TriGuessService(base_dir / "data" / "tri_guess.sqlite3")

    @filter.command(COMMAND_PREFIX)
    async def guess(self, event: Any):
        if self._is_group_event(event) and not self._is_explicitly_at_bot(event):
            return

        args = split_command_args(self._message_text(event), COMMAND_PREFIX)
        subcommand, _, rest = args.partition(" ")
        subcommand = subcommand.lower().strip()
        rest = rest.strip()

        if subcommand in {"", "help"}:
            yield self._reply(event, self.service.help())
            return

        if not self._is_group_event(event):
            yield self._reply(event, "本功能仅支持群聊使用。")
            return

        if subcommand in {"start", "start_guess"}:
            yield self._reply(event, self.service.start_guess(self._group_feature_id(event), self._user_id(event), rest))
            return

        if subcommand == "bet":
            yield self._reply(event, self.service.bet(self._group_feature_id(event), self._user_id(event), rest))
            return

        if subcommand == "settle":
            yield self._reply(event, self.service.settle(self._group_feature_id(event), rest))
            return

        if subcommand == "cancel":
            yield self._reply(event, self.service.cancel(self._group_feature_id(event)))
            return

        if subcommand == "current":
            yield self._reply(event, self.service.current(self._group_feature_id(event), self._user_id(event)))
            return

        if subcommand == "score":
            yield self._reply(event, self.service.score(self._group_feature_id(event), self._user_id(event)))
            return

        if subcommand == "history":
            yield self._reply(event, self.service.history(self._group_feature_id(event), self._user_id(event)))
            return

        yield self._reply(event, f"未知子命令，请使用 {command_usage('help')} 查看帮助。")

    def _plain(self, event: Any, text: str) -> Any:
        plain_result = getattr(event, "plain_result", None)
        if callable(plain_result):
            return plain_result(text)
        return text

    def _reply(self, event: Any, text: str) -> Any:
        for method in ("reply_result", "quote_result"):
            func = getattr(event, method, None)
            if callable(func):
                try:
                    return func(text)
                except TypeError:
                    continue
        chain = self._reply_chain(event, text)
        chain_result = getattr(event, "chain_result", None)
        if chain and callable(chain_result):
            try:
                return chain_result(chain)
            except Exception:
                pass
        return self._plain(event, text)

    def _reply_chain(self, event: Any, text: str) -> list[Any] | None:
        message_id = self._message_id(event)
        if not message_id:
            return None
        try:
            components = importlib.import_module("astrbot.api.message_components")
        except Exception:
            return None
        plain_cls = getattr(components, "Plain", None)
        reply_cls = getattr(components, "Reply", None)
        if not plain_cls or not reply_cls:
            return None
        reply = None
        for kwargs in ({"id": message_id}, {"message_id": message_id}, {"msg_id": message_id}):
            try:
                reply = reply_cls(**kwargs)
                break
            except TypeError:
                continue
        if reply is None:
            try:
                reply = reply_cls(message_id)
            except TypeError:
                return None
        return [reply, plain_cls(text)]

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

    def _group_id(self, event: Any) -> str:
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
