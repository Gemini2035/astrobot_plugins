from __future__ import annotations

import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from .config import command_usage


RESULT_LABELS = {
    "win": "赢",
    "draw": "平",
    "lose": "输",
}

RESULT_ALIASES = {
    "win": "win",
    "赢": "win",
    "draw": "draw",
    "平": "draw",
    "lose": "lose",
    "输": "lose",
}

STATUS_LABELS = {
    "open": "开放中",
    "closed": "已停止参与，等待结算",
    "settled": "已结算",
    "cancelled": "已取消",
}

RECORD_STATUS_LABELS = {
    "pending": "待结算",
    "won": "命中",
    "lost": "未命中",
    "refunded": "已退还",
}

SUPPORTED_RESULTS = tuple(RESULT_LABELS.keys())
SUPPORTED_CATEGORIES = ("apex", "other")
SAFETY_MESSAGE = "本功能仅用于群内娱乐记分，不支持分数购买、兑换、转账或线下交易。"
SENSITIVE_WORDS = (
    "彩票",
    "博彩",
    "赌博",
    "充值",
    "提现",
    "兑奖",
    "下注",
    "投注",
    "赔率",
    "返现",
    "现金",
    "庄家",
    "抽水",
)
MENTION_PATTERN = re.compile(r"\[\[tri_guess_at:([^|\]]+)\|([^\]]*)\]\]")


@dataclass(frozen=True)
class GuessConfig:
    default_score: int = 100
    min_stake: int = 1
    default_odds_win: Decimal = Decimal("1.8")
    default_odds_draw: Decimal = Decimal("3.0")
    default_odds_lose: Decimal = Decimal("1.8")
    default_category: str = "other"
    default_bet_duration_minutes: int = 5
    score_supplement_time: time = time(4, 0)
    history_limit: int = 10


def now_local() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def parse_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("必须是数字") from exc
    if not number.is_finite():
        raise ValueError("不能是 NaN 或 Infinity")
    return number


def round_half_up(value: Decimal | int | float | str) -> int:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_decimal(value: Any) -> str:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    normalized = number.normalize()
    text = format(normalized, "f")
    return text if "." in text else str(int(normalized))


def contains_sensitive(text: str) -> bool:
    return any(word in text for word in SENSITIVE_WORDS)


def normalize_result(value: str) -> str:
    return RESULT_ALIASES.get(value.strip().lower(), "")


def mention_token(user_id: str, label: str | None = None) -> str:
    clean_user_id = str(user_id).replace("|", "_").replace("]", "_")
    clean_label = str(label or user_id).replace("]", "_")
    return f"[[tri_guess_at:{clean_user_id}|{clean_label}]]"


def strip_mention_tokens(text: str) -> str:
    return MENTION_PATTERN.sub(lambda match: f"@{match.group(2) or match.group(1)}", text)


def split_command_args(raw: str, command: str) -> str:
    text = raw.strip()
    command_text = strip_leading_mentions(text)
    marker = f"/{command}"
    index = command_text.find(marker)
    if index >= 0:
        return command_text[index + len(marker) :].strip()
    if command_text == command:
        return ""
    prefix = f"{command} "
    if command_text.startswith(prefix):
        return command_text[len(prefix) :].strip()
    return text


def strip_leading_mentions(text: str) -> str:
    cleaned = text.strip()
    while cleaned.startswith("[At:"):
        end = cleaned.find("]")
        if end < 0:
            break
        cleaned = cleaned[end + 1 :].strip()
    while cleaned.startswith("@"):
        head, sep, rest = cleaned.partition(" ")
        if not sep:
            break
        cleaned = rest.strip()
    return cleaned


def extract_options(text: str) -> tuple[str, dict[str, str]]:
    options: dict[str, str] = {}
    words: list[str] = []
    for token in text.split():
        if "=" in token:
            key, value = token.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in {"category", "win", "draw", "lose"}:
                options[key] = value
                continue
        words.append(token)
    return " ".join(words).strip(), options


class TriGuessService:
    def __init__(self, db_path: str | Path, config: GuessConfig | None = None):
        self.db_path = Path(db_path)
        self.config = config or GuessConfig()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def start_guess(self, group_id: str, user_id: str, raw_args: str, at: datetime | None = None) -> str:
        if contains_sensitive(raw_args):
            return SAFETY_MESSAGE
        current_time = at or now_local()
        title, options = extract_options(raw_args)
        if not title:
            title = f"event_{current_time.strftime('%Y%m%d%H%M%S')}"

        category = options.get("category", self.config.default_category).lower()
        if category not in SUPPORTED_CATEGORIES:
            return "创建失败：不支持的分类。当前支持：apex / other"

        odds = {
            "win": self.config.default_odds_win,
            "draw": self.config.default_odds_draw,
            "lose": self.config.default_odds_lose,
        }
        for result in SUPPORTED_RESULTS:
            if result in options:
                try:
                    parsed = parse_decimal(options[result])
                except ValueError:
                    return f"创建失败：{result} 倍数必须是有效数字"
                if parsed <= 0:
                    return f"创建失败：{result} 倍数必须大于 0"
                odds[result] = parsed

        with self._lock, self._connect() as conn:
            self._run_due_tasks(conn, group_id, current_time)
            self._lazy_close(conn, group_id, current_time)
            if self._get_active_event(conn, group_id):
                return "创建失败：当前群已有未结束事件，请先结算或取消"
            bet_close_at = current_time + timedelta(minutes=self.config.default_bet_duration_minutes)
            conn.execute(
                """
                INSERT INTO current_event (
                    group_id, title, category, status, odds_win, odds_draw, odds_lose,
                    bet_close_at, score_date, created_by, created_at
                )
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    title,
                    category,
                    str(odds["win"]),
                    str(odds["draw"]),
                    str(odds["lose"]),
                    self._dt(bet_close_at),
                    current_time.date().isoformat(),
                    user_id,
                    self._dt(current_time),
                ),
            )

        return (
            "当前事件已创建\n\n"
            f"标题：{title}\n"
            f"分类：{category}\n"
            f"参与截止：{self.config.default_bet_duration_minutes} 分钟后\n\n"
            f"可选结果：赢/输/平，倍率 {fmt_decimal(odds['win'])}/{fmt_decimal(odds['lose'])}/{fmt_decimal(odds['draw'])}\n\n"
            f"参与方式：{command_usage('bet', '赢/输/平', '你的下注')}（支持all快速投入当前全部点数）"
        )

    def bet(self, group_id: str, user_id: str, raw_args: str, at: datetime | None = None, user_label: str | None = None) -> str:
        if contains_sensitive(raw_args):
            return SAFETY_MESSAGE
        current_time = at or now_local()
        parts = raw_args.split()
        if len(parts) != 2:
            return f"竞猜失败：格式应为 {command_usage('bet', '赢', '30')}，也可用 all"
        choice = normalize_result(parts[0])
        if choice not in SUPPORTED_RESULTS:
            return "竞猜失败：结果只能是 赢 / 输 / 平"
        stake_input = parts[1].strip()
        is_all = stake_input.lower() == "all"
        input_score: Decimal | str
        if is_all:
            input_score = "all"
            stake = 0
        else:
            try:
                input_score = parse_decimal(stake_input)
            except ValueError:
                return "竞猜失败：积分必须是有效数字或 all"
            stake = round_half_up(input_score)
            if stake < self.config.min_stake:
                return "竞猜失败：实际投入不能小于 1"

        with self._lock, self._connect() as conn:
            self._run_due_tasks(conn, group_id, current_time)
            self._lazy_close(conn, group_id, current_time)
            event = self._get_active_event(conn, group_id)
            if not event or event["status"] != "open":
                return "竞猜失败：当前没有开放中的事件"
            if current_time >= self._parse_dt(event["bet_close_at"]):
                self._lazy_close(conn, group_id, current_time)
                return "竞猜失败：当前事件已停止参与，等待结算"
            if self._get_record(conn, int(event["id"]), user_id):
                return "竞猜失败：你已参与当前事件，不能重复参与"
            score = self._ensure_score(conn, group_id, user_id, current_time)
            available_score = int(score["available_score"])
            balance_note = ""
            if is_all:
                stake = available_score
            elif stake > available_score:
                stake = available_score
                balance_note = "当前可用积分不足，已按剩余全部积分投入。\n"
            if stake < self.config.min_stake:
                return "竞猜失败：你的当前可用积分不足，实际投入不能小于 1"
            odds = Decimal(str(event[f"odds_{choice}"]))
            new_balance = available_score - stake
            conn.execute(
                """
                UPDATE user_score
                SET available_score = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (new_balance, self._dt(current_time), group_id, user_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO guess_record (
                    event_id, group_id, user_id, user_label, score_date, choice, input_score, stake,
                    odds, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    int(event["id"]),
                    group_id,
                    user_id,
                    user_label or user_id,
                    current_time.date().isoformat(),
                    choice,
                    str(input_score),
                    stake,
                    str(odds),
                    self._dt(current_time),
                ),
            )
            self._log_score(conn, group_id, user_id, -stake, new_balance, "bet_stake", int(event["id"]), cursor.lastrowid, current_time)

        return (
            f"{balance_note}"
            "竞猜成功\n"
            f"选择：{RESULT_LABELS[choice]}\n"
            f"实际投入：{stake}\n"
            f"倍率：{fmt_decimal(odds)}\n"
            f"剩余积分：{new_balance}"
        )

    def settle(self, group_id: str, raw_args: str, at: datetime | None = None) -> str:
        if contains_sensitive(raw_args):
            return SAFETY_MESSAGE
        current_time = at or now_local()
        result = normalize_result(raw_args)
        if result not in SUPPORTED_RESULTS:
            return "结算失败：结果只能是 赢 / 输 / 平"

        with self._lock, self._connect() as conn:
            self._run_due_tasks(conn, group_id, current_time)
            self._lazy_close(conn, group_id, current_time)
            event = self._get_active_event(conn, group_id)
            if not event:
                return "结算失败：当前没有未结束事件"
            records = conn.execute(
                "SELECT * FROM guess_record WHERE event_id = ? AND status = 'pending' ORDER BY created_at ASC, id ASC",
                (int(event["id"]),),
            ).fetchall()
            hit_lines: list[str] = []
            miss_lines: list[str] = []
            for record in records:
                stake = int(record["stake"])
                if record["choice"] == result:
                    expected = Decimal(stake) * Decimal(str(record["odds"]))
                    actual = round_half_up(expected)
                    profit = actual - stake
                    score = self._ensure_score(conn, group_id, record["user_id"], current_time)
                    balance = int(score["available_score"]) + actual
                    conn.execute(
                        "UPDATE user_score SET available_score = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                        (balance, self._dt(current_time), group_id, record["user_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE guess_record
                        SET status = 'won', expected_payout = ?, actual_payout = ?, profit = ?, settled_at = ?
                        WHERE id = ?
                        """,
                        (str(expected), actual, profit, self._dt(current_time), int(record["id"])),
                    )
                    self._log_score(conn, group_id, record["user_id"], actual, balance, "settle_win", int(event["id"]), int(record["id"]), current_time)
                    label = record["user_label"] or record["user_id"]
                    hit_lines.append(f"{mention_token(record['user_id'], label)} 赢 +{profit}（投入 {stake}，实得 {actual}）")
                else:
                    score = self._ensure_score(conn, group_id, record["user_id"], current_time)
                    profit = -stake
                    conn.execute(
                        """
                        UPDATE guess_record
                        SET status = 'lost', expected_payout = '0', actual_payout = 0, profit = ?, settled_at = ?
                        WHERE id = ?
                        """,
                        (profit, self._dt(current_time), int(record["id"])),
                    )
                    self._log_score(
                        conn,
                        group_id,
                        record["user_id"],
                        0,
                        int(score["available_score"]),
                        "settle_lose",
                        int(event["id"]),
                        int(record["id"]),
                        current_time,
                    )
                    label = record["user_label"] or record["user_id"]
                    miss_lines.append(f"{mention_token(record['user_id'], label)} 输 {profit}（投入 {stake}）")
            conn.execute(
                """
                UPDATE current_event
                SET status = 'settled', result = ?, settled_at = ?
                WHERE id = ?
                """,
                (result, self._dt(current_time), int(event["id"])),
            )

        return (
            "当前事件已结算\n\n"
            f"标题：{event['title']}\n"
            f"结果：{RESULT_LABELS[result]}\n\n"
            "输赢情况：\n"
            f"{chr(10).join(hit_lines + miss_lines) if hit_lines or miss_lines else '无参与记录'}"
        )

    def cancel(self, group_id: str, at: datetime | None = None) -> str:
        current_time = at or now_local()
        with self._lock, self._connect() as conn:
            self._run_due_tasks(conn, group_id, current_time)
            self._lazy_close(conn, group_id, current_time)
            event = self._get_active_event(conn, group_id)
            if not event:
                return "取消失败：当前没有未结束事件"
            self._cancel_event(conn, event, current_time)
        return (
            "当前事件已取消\n\n"
            f"标题：{event['title']}\n"
            f"分类：{event['category']}\n"
            "所有已投入积分已退还。"
        )

    def current(self, group_id: str, user_id: str, at: datetime | None = None) -> str:
        current_time = at or now_local()
        with self._lock, self._connect() as conn:
            self._run_due_tasks(conn, group_id, current_time)
            self._lazy_close(conn, group_id, current_time)
            event = self._get_active_event(conn, group_id)
            if not event:
                return "当前没有进行中的事件"
            record = self._get_record(conn, int(event["id"]), user_id)

        bet_info = ""
        if record:
            bet_info = (
                "\n\n你的参与："
                f"{RESULT_LABELS[record['choice']]}\n"
                f"输入积分：{fmt_decimal(record['input_score'])}\n"
                f"实际投入：{record['stake']}"
            )
        return (
            f"当前事件：{event['title']}\n"
            f"分类：{event['category']}\n"
            f"状态：{STATUS_LABELS[event['status']]}\n"
            f"参与截止：{self._parse_dt(event['bet_close_at']).strftime('%H:%M')}\n\n"
            "可选结果：\n"
            f"赢 {fmt_decimal(event['odds_win'])}\n"
            f"平 {fmt_decimal(event['odds_draw'])}\n"
            f"输 {fmt_decimal(event['odds_lose'])}"
            f"{bet_info}"
        )

    def score(
        self,
        group_id: str,
        user_id: str,
        at: datetime | None = None,
        target_user_id: str | None = None,
        target_label: str | None = None,
    ) -> str:
        current_time = at or now_local()
        score_user_id = target_user_id or user_id
        with self._lock, self._connect() as conn:
            self._run_due_tasks(conn, group_id, current_time)
            if score_user_id == user_id:
                score = self._ensure_score(conn, group_id, score_user_id, current_time)
            else:
                score = conn.execute(
                    "SELECT * FROM user_score WHERE group_id = ? AND user_id = ?",
                    (group_id, score_user_id),
                ).fetchone()
                if not score:
                    label = target_label or score_user_id
                    return f"{label} 暂无积分记录，首次使用会获得 {self.config.default_score} 分。"
        next_time = self._next_supplement_time(current_time)
        next_label = "今日 04:00" if next_time.date() == current_time.date() else "明日 04:00"
        owner = "你的" if score_user_id == user_id else f"{target_label or score_user_id} 的"
        return (
            f"{owner}当前可用积分：{score['available_score']}\n"
            f"每日基础分：{self.config.default_score}\n"
            f"下次补充时间：{next_label}\n\n"
            f"说明：低于 {self.config.default_score} 时会补充到 {self.config.default_score}，"
            f"高于或等于 {self.config.default_score} 时不变化。"
        )

    def history(self, group_id: str, user_id: str, at: datetime | None = None) -> str:
        current_time = at or now_local()
        with self._lock, self._connect() as conn:
            self._run_due_tasks(conn, group_id, current_time)
            rows = conn.execute(
                """
                SELECT r.*, e.title, e.category, e.result
                FROM guess_record r
                JOIN current_event e ON e.id = r.event_id
                WHERE r.group_id = ? AND r.user_id = ?
                ORDER BY r.created_at DESC, r.id DESC
                LIMIT ?
                """,
                (group_id, user_id, self.config.history_limit),
            ).fetchall()
        if not rows:
            return "最近记录：\n\n暂无记录"
        blocks = ["最近记录："]
        for row in rows:
            lines = [
                "",
                str(row["title"]),
                f"分类：{row['category']}",
                f"选择：{RESULT_LABELS[row['choice']]}",
                f"输入积分：{fmt_decimal(row['input_score'])}",
                f"实际投入：{row['stake']}",
                f"倍数：{fmt_decimal(row['odds'])}",
                f"状态：{RECORD_STATUS_LABELS[row['status']]}",
            ]
            if row["result"]:
                lines.extend(
                    [
                        f"结果：{RESULT_LABELS[row['result']]}",
                        f"应得：{fmt_decimal(row['expected_payout'] or '0')}",
                        f"实得：{row['actual_payout'] or 0}",
                        f"净收益：{int(row['profit'] or 0):+d}",
                    ]
                )
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def help(self) -> str:
        return (
            "三态竞猜记分帮助\n\n"
            "所有人可用：\n"
            f"{command_usage('start', '标题')}\n"
            "创建当前事件，默认分类 other，默认 5 分钟后停止参与。\n\n"
            f"{command_usage('start', '标题', 'category=apex', 'win=1.8', 'draw=3', 'lose=1.8')}\n"
            "创建当前事件，并设置分类和倍数。\n"
            "分类支持：apex / other。\n"
            "分类大小写不敏感。\n\n"
            f"{command_usage('bet', '赢/输/平', '30')}\n"
            f"{command_usage('bet', '赢', 'all')}\n"
            "参与当前开放中的事件。输入积分会四舍五入，all 会投入当前全部可用积分。\n\n"
            f"{command_usage('current')}\n"
            "查看当前事件、分类、状态、倍数和自己的参与情况。\n\n"
            f"{command_usage('score')}\n"
            f"{command_usage('score', '@用户')}\n"
            "查看自己或指定用户的当前可用积分和每日 04:00 补充规则。\n\n"
            # History is temporarily hidden from help while the command is disabled.
            # f"{command_usage('history')}\n"
            # "查看最近参与记录。\n\n"
            f"{command_usage('help')}\n"
            "查看本帮助。\n\n"
            "结算与取消：\n"
            f"{command_usage('settle', '赢/输/平')}\n"
            "结算当前事件。\n\n"
            f"{command_usage('cancel')}\n"
            "取消当前事件并退还已投入积分。"
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS current_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    odds_win TEXT NOT NULL,
                    odds_draw TEXT NOT NULL,
                    odds_lose TEXT NOT NULL,
                    bet_close_at TEXT NOT NULL,
                    score_date TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    settled_at TEXT
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_current_event_one_active
                ON current_event(group_id)
                WHERE status IN ('open', 'closed');

                CREATE TABLE IF NOT EXISTS user_score (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    available_score INTEGER NOT NULL,
                    default_score INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS guess_record (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_label TEXT,
                    score_date TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    input_score TEXT NOT NULL,
                    stake INTEGER NOT NULL,
                    odds TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expected_payout TEXT,
                    actual_payout INTEGER,
                    profit INTEGER,
                    created_at TEXT NOT NULL,
                    settled_at TEXT,
                    UNIQUE(event_id, user_id),
                    FOREIGN KEY(event_id) REFERENCES current_event(id)
                );

                CREATE TABLE IF NOT EXISTS score_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    change INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    ref_event_id INTEGER,
                    ref_record_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_supplement_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    default_score INTEGER NOT NULL,
                    affected_users_count INTEGER NOT NULL,
                    total_supplement INTEGER NOT NULL,
                    UNIQUE(group_id, run_at)
                );
                """
            )
            self._migrate_db(conn)

    def _migrate_db(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(guess_record)").fetchall()
        }
        if "user_label" not in columns:
            conn.execute("ALTER TABLE guess_record ADD COLUMN user_label TEXT")

    def _ensure_score(self, conn: sqlite3.Connection, group_id: str, user_id: str, at: datetime) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM user_score WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
        if row:
            return row
        conn.execute(
            """
            INSERT INTO user_score (group_id, user_id, available_score, default_score, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (group_id, user_id, self.config.default_score, self.config.default_score, self._dt(at), self._dt(at)),
        )
        self._log_score(conn, group_id, user_id, self.config.default_score, self.config.default_score, "initial_grant", None, None, at)
        return conn.execute(
            "SELECT * FROM user_score WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()

    def _get_active_event(self, conn: sqlite3.Connection, group_id: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM current_event
            WHERE group_id = ? AND status IN ('open', 'closed')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()

    def _get_record(self, conn: sqlite3.Connection, event_id: int, user_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM guess_record WHERE event_id = ? AND user_id = ?",
            (event_id, user_id),
        ).fetchone()

    def _lazy_close(self, conn: sqlite3.Connection, group_id: str, at: datetime) -> None:
        rows = conn.execute(
            """
            SELECT * FROM current_event
            WHERE group_id = ? AND status = 'open' AND bet_close_at <= ?
            """,
            (group_id, self._dt(at)),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE current_event SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'open'",
                (self._dt(at), int(row["id"])),
            )

    def _cancel_event(self, conn: sqlite3.Connection, event: sqlite3.Row, at: datetime) -> None:
        records = conn.execute(
            "SELECT * FROM guess_record WHERE event_id = ? AND status = 'pending'",
            (int(event["id"]),),
        ).fetchall()
        for record in records:
            score = self._ensure_score(conn, record["group_id"], record["user_id"], at)
            balance = int(score["available_score"]) + int(record["stake"])
            conn.execute(
                "UPDATE user_score SET available_score = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (balance, self._dt(at), record["group_id"], record["user_id"]),
            )
            conn.execute(
                """
                UPDATE guess_record
                SET status = 'refunded', expected_payout = '0', actual_payout = ?, profit = 0, settled_at = ?
                WHERE id = ?
                """,
                (int(record["stake"]), self._dt(at), int(record["id"])),
            )
            self._log_score(conn, record["group_id"], record["user_id"], int(record["stake"]), balance, "refund", int(event["id"]), int(record["id"]), at)
        conn.execute(
            "UPDATE current_event SET status = 'cancelled', settled_at = ? WHERE id = ?",
            (self._dt(at), int(event["id"])),
        )

    def _run_due_tasks(self, conn: sqlite3.Connection, group_id: str, at: datetime) -> None:
        if at.time() < self.config.score_supplement_time:
            return
        run_key = at.date().isoformat()
        existing = conn.execute(
            "SELECT id FROM daily_supplement_log WHERE group_id = ? AND run_at = ?",
            (group_id, run_key),
        ).fetchone()
        if existing:
            return
        active_events = conn.execute(
            "SELECT * FROM current_event WHERE group_id = ? AND status IN ('open', 'closed')",
            (group_id,),
        ).fetchall()
        for event in active_events:
            self._cancel_event(conn, event, at)
        users = conn.execute("SELECT * FROM user_score WHERE group_id = ?", (group_id,)).fetchall()
        affected = 0
        total = 0
        for user in users:
            available = int(user["available_score"])
            if available >= self.config.default_score:
                continue
            supplement = self.config.default_score - available
            affected += 1
            total += supplement
            conn.execute(
                "UPDATE user_score SET available_score = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
                (self.config.default_score, self._dt(at), group_id, user["user_id"]),
            )
            self._log_score(conn, group_id, user["user_id"], supplement, self.config.default_score, "daily_supplement", None, None, at)
        conn.execute(
            """
            INSERT INTO daily_supplement_log (group_id, run_at, default_score, affected_users_count, total_supplement)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, run_key, self.config.default_score, affected, total),
        )

    def _log_score(
        self,
        conn: sqlite3.Connection,
        group_id: str,
        user_id: str,
        change: int,
        balance_after: int,
        reason: str,
        ref_event_id: int | None,
        ref_record_id: int | None,
        at: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO score_log (group_id, user_id, change, balance_after, reason, ref_event_id, ref_record_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (group_id, user_id, change, balance_after, reason, ref_event_id, ref_record_id, self._dt(at)),
        )

    def _next_supplement_time(self, at: datetime) -> datetime:
        candidate = datetime.combine(at.date(), self.config.score_supplement_time)
        if at >= candidate:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _dt(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
