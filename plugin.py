"""自然语言提醒插件（通用版，MaiBot 1.2.x 插件 SDK）

设计原则：插件自己不生成"人设"——所有从 bot 嘴里说出的话都遵从宿主人设：
  1. 触发走 @Tool：Planner 在对话中识别"设提醒"意图后调用工具，工具只返回
     事实结果，确认回执由 Planner 用本体口吻说出（零人设耦合）；
  2. 到点提醒走 maisaka.proactive.trigger，由主链路 Planner+Replyer 生成发送；
  3. 兜底直发通过 llm.generate 生成，prompt 注入从宿主配置读到的 personality；
  4. LLM 失败时退化为朴素事实文案（宁素勿装，绝不硬编码口吻）。

注：MaiBot 1.2.x 的 ON_MESSAGE 事件尚未接线（主链路 emit 被注释），
因此不使用 EventHandler，触发完全依赖 Planner 的工具调用。

用法（自然对话即可，Planner 会调用工具）：
  "明晚7点提醒我训练" / "10分钟后叫我关火" / "周六下午3点比赛，提前2小时提醒"
  "取消提醒" / "我有哪些提醒"
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_log = logging.getLogger("plugin.paixiaozhou.reminder")


# ─────────────────────────────────────────────
# 配置模型（WebUI 可编辑）
# ─────────────────────────────────────────────

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.2.0", description="配置版本")


class ScheduleConfig(PluginConfigBase):
    __ui_label__ = "调度"
    __ui_icon__ = "clock"
    __ui_order__ = 1

    scan_interval_seconds: int = Field(default=20, description="到点扫描间隔（秒）")
    max_pending_per_stream: int = Field(default=20, description="每个聊天流最多挂起的提醒数")
    default_advance_minutes: list[int] = Field(
        default_factory=list,
        description="默认提前提醒量（分钟）；如 [120] 表示到点前 2 小时加推一次",
    )


class ParseConfig(PluginConfigBase):
    __ui_label__ = "解析"
    __ui_icon__ = "brain"
    __ui_order__ = 2

    model_task: str = Field(default="", description="时间解析用的模型任务名（空 = 宿主默认）")


class DeliveryConfig(PluginConfigBase):
    __ui_label__ = "送达"
    __ui_icon__ = "send"
    __ui_order__ = 3

    use_proactive: bool = Field(default=True, description="到点提醒走主链路生成（口吻与本体一致）")
    fallback_after_seconds: int = Field(
        default=120,
        description="主链路入队后这么多秒仍未确认发言则兜底直发（0 = 不验证）",
    )


class ReminderPluginConfig(PluginConfigBase):
    """自然语言提醒插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    parse: ParseConfig = Field(default_factory=ParseConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)


# ─────────────────────────────────────────────
# 插件本体
# ─────────────────────────────────────────────

class NaturalReminderPlugin(MaiBotPlugin):
    """自然语言提醒：三个工具（设/取消/查）+ 到点拟人送达。"""

    config_model = ReminderPluginConfig

    def __init__(self):
        super().__init__()
        self._reminders: list[dict[str, Any]] = []
        self._data_path: Path | None = None
        self._scheduler_task: asyncio.Task | None = None
        # 宿主人设（用于兜底直发的口吻一致）
        self._persona_nickname: str = ""
        self._persona_body: str = ""
        self._persona_reply_style: str = ""
        # bot 账号映射（platform -> account），用于确认 bot 自己发言过
        self._bot_accounts: dict[str, str] = {}

    # ── 生命周期 ──

    async def on_load(self) -> None:
        self._data_path = self.ctx.paths.data_dir / "reminders.json"
        self._load_reminders()
        await self._load_host_persona()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def on_unload(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except BaseException:
                pass
        self._save_reminders()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version

    # ── 宿主人设 / 身份读取 ──

    async def _load_host_persona(self) -> None:
        """从宿主全局配置读取本体人设与账号——插件口吻完全遵从宿主。"""

        async def cfg(key: str) -> Any:
            try:
                res = await self.ctx.call_capability("config.get", key=key)
                if isinstance(res, dict) and res.get("success"):
                    return res.get("value")
            except Exception:
                pass
            return None

        nickname = await cfg("bot.nickname")
        self._persona_nickname = str(nickname).strip() if nickname else ""
        body = await cfg("personality.personality")
        self._persona_body = str(body).strip() if body else ""
        style = await cfg("personality.reply_style")
        self._persona_reply_style = str(style).strip() if style else ""

        qq = await cfg("bot.qq_account")
        if qq:
            self._bot_accounts["qq"] = str(qq)
        platforms = await cfg("bot.platforms")
        if isinstance(platforms, list):
            for item in platforms:
                if isinstance(item, str) and ":" in item:
                    plat, acc = item.split(":", 1)
                    self._bot_accounts[plat.strip()] = acc.strip()

    def _persona_header(self) -> str:
        parts = []
        if self._persona_nickname:
            parts.append(f"你的名字是{self._persona_nickname}。")
        if self._persona_body:
            parts.append(self._persona_body)
        if self._persona_reply_style:
            parts.append(f"说话风格：{self._persona_reply_style}")
        if not parts:
            return "你是一个正在群聊里说话的bot，口吻自然简短。"
        return " ".join(parts)

    # ── 持久化 ──

    def _load_reminders(self) -> None:
        try:
            if self._data_path and self._data_path.exists():
                data = json.loads(self._data_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._reminders = [r for r in data if isinstance(r, dict)]
        except Exception:
            self._reminders = []

    def _save_reminders(self) -> None:
        try:
            if self._data_path:
                self._data_path.parent.mkdir(parents=True, exist_ok=True)
                self._data_path.write_text(
                    json.dumps(self._reminders, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
        except Exception:
            pass

    # ── LLM 辅助 ──

    async def _llm(self, prompt: str, temperature: float = 0.3, max_tokens: int = 300) -> str:
        try:
            model = (self.config.parse.model_task or "").strip()
            res = await self.ctx.llm.generate(
                prompt=prompt, model=model,
                temperature=temperature, max_tokens=max_tokens,
            )
            if isinstance(res, dict) and res.get("success"):
                return str(res.get("response") or "").strip()
            if isinstance(res, dict):
                _log.warning(f"[reminder] llm.generate 未成功: {res.get('error') or res.get('response') or res}")
        except Exception as exc:
            _log.warning(f"[reminder] llm.generate 异常: {type(exc).__name__}: {exc}")
        return ""

    # ── 时间解析：规则快路径 + LLM 兜底 ──

    _CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}

    def _rule_resolve(self, expression: str, now: datetime) -> datetime | None:
        """常用格式的零成本解析：相对时间 / 绝对时间 / 今明后天+时刻。"""
        s = re.sub(r"\s+", "", (expression or "").strip())

        def to_int(token: str) -> float | None:
            if token.isdigit():
                return float(token)
            if token in self._CN_NUM:
                return float(self._CN_NUM[token])
            if len(token) == 3 and token[0] in self._CN_NUM and token[1] == "十" \
                    and token[2] in self._CN_NUM:
                return self._CN_NUM[token[0]] * 10 + self._CN_NUM[token[2]]
            if token == "十":
                return 10.0
            return None

        # N秒/分钟/小时/天后
        m = re.match(r"^(\d+|[零一两二三四五六七八九十半]+)(秒|分钟|分|个小时|小时|天)后$", s)
        if m:
            n = to_int(m.group(1))
            if n is not None:
                unit = m.group(2)
                delta = ({"秒": timedelta(seconds=n), "分": timedelta(minutes=n),
                          "分钟": timedelta(minutes=n), "小时": timedelta(hours=n),
                          "个小时": timedelta(hours=n), "天": timedelta(days=n)})[unit]
                return now + delta

        # 绝对时间：2026-08-25 15:00[:30] / 2026年8月25日15点30分 / 2026-08-25T15:00:00+08:00
        # （s 已去空格，故分隔段允许零个分隔符）
        m = re.match(
            r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日T\s]*(\d{1,2})[:时点](\d{1,2})(?:[:分](\d{1,2}))?",
            s,
        )
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
            except ValueError:
                return None

        # 今天/今晚/明天/明晚/后天/大后天 + HH:MM 或 H点[H半]
        m = re.match(r"^(今天|今晚|明天|明晚|后天|大后天)(\d{1,2})[:点时](\d{1,2})?(半)?$", s)
        if m:
            day_offset = {"今天": 0, "今晚": 0, "明天": 1, "明晚": 1, "后天": 2, "大后天": 3}[m.group(1)]
            hour = int(m.group(2))
            minute = int(m.group(3) or 0)
            if m.group(4):  # "7点半"
                minute = 30
            if m.group(1) in ("今晚", "明晚") and hour < 12:
                hour += 12  # "晚上7点" = 19点
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (now + timedelta(days=day_offset)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0)

        # (本/这/下)周X HH:MM —— "下周"按周一为一周之首计算
        m = re.match(r"^(本|这|下)?周([一二三四五六日天])(\d{1,2})[:点时](\d{1,2})?$", s)
        if m:
            wd_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            wd = wd_map.get(m.group(2))
            if wd is not None:
                hour = int(m.group(3))
                minute = int(m.group(4) or 0)
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return None
                prefix = m.group(1) or ""
                if prefix == "下周":
                    days = (7 - now.weekday()) + wd  # 下周一距今天 + 目标星期
                else:
                    days = (wd - now.weekday()) % 7
                    if days == 0:
                        days = 7  # 今天这个星期已过，默认下个同星期
                return (now + timedelta(days=days)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0)
        return None

    async def _resolve_time(self, expression: str) -> datetime | None:
        """自然语言时间 → 未来时刻。规则快路径优先，LLM 兜底。"""
        now = datetime.now()
        quick = self._rule_resolve(expression, now)
        if quick is not None:
            return quick
        prompt = (
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}（{_WEEKDAYS[now.weekday()]}）。\n"
            f"把时间描述「{expression}」换算成具体的未来本地时刻。\n"
            "只输出 YYYY-MM-DD HH:MM:SS（秒填00）；描述模糊或指向过去则只输出 INVALID。"
        )
        raw = await self._llm(prompt, temperature=0.0, max_tokens=40)
        raw = re.sub(r"\s+", " ", raw).strip()
        match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", raw)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    # ── 工具：设置提醒 ──

    @Tool(
        "set_reminder",
        description=(
            "为用户设置定时提醒，到点由你在聊天里发出提醒。"
            "当用户明确要在未来某时刻被提醒/被叫（如『明晚7点提醒我训练』『10分钟后叫我关火』"
            "『周六3点比赛，提前2小时提醒我』）时调用。"
        ),
        parameters=[
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING,
                              description="提醒内容，20字内（如『去东操场拿球』）", required=True),
            ToolParameterInfo(name="time_expression", param_type=ToolParamType.STRING,
                              description="时间描述原样传入（如『明晚7点』『10分钟后』『2026-08-25 15:00』）", required=True),
            ToolParameterInfo(name="advance_minutes", param_type=ToolParamType.STRING,
                              description="提前提醒量，分钟数逗号分隔（如『120』）；用户没要求提前提醒就不传", required=False),
        ],
    )
    async def tool_set_reminder(self, title: str = "", time_expression: str = "",
                                advance_minutes: str = "", **kwargs: Any):
        if not self.config.plugin.enabled:
            return {"name": "set_reminder", "content": "提醒插件当前已禁用"}
        title = (title or "").strip()
        time_expression = (time_expression or "").strip()
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not title or not time_expression or not stream_id:
            return {"name": "set_reminder", "content": "参数不全：需要提醒内容、时间描述和当前聊天"}

        remind_at = await self._resolve_time(time_expression)
        if remind_at is None or remind_at <= datetime.now() + timedelta(seconds=30):
            return {"name": "set_reminder",
                    "content": (f"时间描述「{time_expression}」无法解析为明确的未来时刻。"
                                "不要用其他格式重试本工具，直接让用户把时间说清楚。")}

        pending = [r for r in self._reminders
                   if r["stream_id"] == stream_id and r["status"] in ("pending", "queued")]
        if len(pending) >= self.config.schedule.max_pending_per_stream:
            return {"name": "set_reminder",
                    "content": f"当前聊天挂起的提醒已有 {len(pending)} 个（上限），请用户先取消一些"}

        advances: list[int] = []
        for part in re.split(r"[，,、\s]+", (advance_minutes or "").strip()):
            if part.isdigit() and 0 < int(part) <= 24 * 60:
                advances.append(int(part))
        if not advances:
            advances = list(self.config.schedule.default_advance_minutes)
        advances = sorted(set(advances), reverse=True)

        user_name = str(kwargs.get("user_nickname") or kwargs.get("user_name") or "").strip()
        platform = str(kwargs.get("platform") or "").strip()
        main_id = uuid.uuid4().hex[:8]
        self._reminders.append(self._make_entry(
            id=main_id, stream_id=stream_id, platform=platform, user_name=user_name,
            title=title, remind_at=remind_at, kind="main", advance_of=None,
        ))
        for a in advances:
            at = remind_at - timedelta(minutes=a)
            if at > datetime.now() + timedelta(seconds=30):
                self._reminders.append(self._make_entry(
                    id=uuid.uuid4().hex[:8], stream_id=stream_id, platform=platform,
                    user_name=user_name, title=title, remind_at=at,
                    kind="advance", advance_of=main_id,
                ))
        self._save_reminders()

        time_str = remind_at.strftime("%m-%d %H:%M")
        advance_note = f"，提前 {advances} 分钟各提醒一次" if advances else ""
        return {"name": "set_reminder",
                "content": f"提醒已设置：{time_str} 提醒『{title}』{advance_note}。请用你的口吻向用户简短确认。"}

    def _make_entry(self, *, id: str, stream_id: str, platform: str, user_name: str,
                    title: str, remind_at: datetime, kind: str, advance_of: str | None) -> dict:
        return {
            "id": id, "stream_id": stream_id, "platform": platform, "user_name": user_name,
            "title": title, "remind_at": remind_at.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind, "advance_of": advance_of,
            "status": "pending", "queued_at": None,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── 工具：取消提醒 ──

    @Tool(
        "cancel_reminder",
        description="取消当前聊天中已设置、还没到点的定时提醒。用户要取消/撤回提醒时调用。",
        parameters=[
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING,
                              description="要取消的提醒内容关键词；用户没特指就传空串（取消最近一个）", required=False),
        ],
    )
    async def tool_cancel_reminder(self, title: str = "", **kwargs: Any):
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not stream_id:
            return {"name": "cancel_reminder", "content": "缺少当前聊天信息"}
        candidates = [r for r in self._reminders
                      if r["stream_id"] == stream_id and r["status"] in ("pending", "queued")]
        if not candidates:
            return {"name": "cancel_reminder", "content": "当前聊天没有挂起的提醒"}
        targets = [r for r in candidates if title and title in r["title"]] or [candidates[-1]]
        main_ids = {r["id"] for r in targets} | {r["advance_of"] for r in targets if r["advance_of"]}
        for r in self._reminders:
            if r["stream_id"] == stream_id and r["status"] in ("pending", "queued") \
                    and (r["id"] in main_ids or r["advance_of"] in main_ids):
                r["status"] = "cancelled"
        self._save_reminders()
        names = "、".join(f"『{r['title']}』({r['remind_at'][5:16]})" for r in targets[:3])
        return {"name": "cancel_reminder", "content": f"已取消提醒：{names}。请用你的口吻简短确认。"}

    # ── 工具：查询提醒 ──

    @Tool(
        "list_reminders",
        description="列出当前聊天中已设置、还没到点的全部定时提醒。用户查看/问自己有哪些提醒时调用。",
        parameters=[],
    )
    async def tool_list_reminders(self, **kwargs: Any):
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not stream_id:
            return {"name": "list_reminders", "content": "缺少当前聊天信息"}
        pending = sorted(
            [r for r in self._reminders
             if r["stream_id"] == stream_id and r["status"] in ("pending", "queued")],
            key=lambda r: r["remind_at"],
        )
        if not pending:
            return {"name": "list_reminders", "content": "当前聊天没有挂起的提醒"}
        lines = [f"- {r['remind_at'][5:16]} {r['title']}" + ("（含提前提醒）" if r["kind"] == "advance" else "")
                 for r in pending[:8]]
        return {"name": "list_reminders",
                "content": "当前挂起的提醒：\n" + "\n".join(lines) + "\n请用你的口吻转述给用户。"}

    # ── 到点送达 ──

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(5, self.config.schedule.scan_interval_seconds))
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning(f"[reminder] 调度异常: {exc}")

    async def _tick(self) -> None:
        now = datetime.now()
        now_ts = time.time()
        fallback_after = self.config.delivery.fallback_after_seconds

        for r in self._reminders:
            if r["status"] == "pending":
                try:
                    at = datetime.fromisoformat(r["remind_at"])
                except ValueError:
                    r["status"] = "cancelled"
                    continue
                if at <= now:
                    await self._deliver(r)
            elif r["status"] == "queued" and fallback_after > 0:
                queued_at = r.get("queued_at") or 0
                if now_ts - queued_at > fallback_after:
                    if await self._bot_spoke_since(r["stream_id"], r.get("platform", ""), queued_at):
                        r["status"] = "sent"
                    else:
                        await self._send_fallback(r)
                        r["status"] = "sent"

        horizon = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        self._reminders = [r for r in self._reminders
                           if r["status"] in ("pending", "queued") or r["remind_at"] >= horizon]
        self._save_reminders()

    async def _deliver(self, reminder: dict) -> None:
        if self.config.delivery.use_proactive:
            ok = await self._trigger_proactive(reminder)
            if ok:
                reminder["status"] = "queued"
                reminder["queued_at"] = time.time()
                return
        await self._send_fallback(reminder)
        reminder["status"] = "sent"

    async def _trigger_proactive(self, reminder: dict) -> bool:
        time_str = reminder["remind_at"][5:16]
        kind_note = "（这是提前提醒）" if reminder["kind"] == "advance" else ""
        who = reminder.get("user_name") or "大家"
        intent = (
            f"定时提醒任务（用户明确设置，必须执行，不要沉默）：请提醒{who}"
            f"『{reminder['title']}』，约定时间 {time_str}{kind_note}。"
            f"请立刻用你的口吻在聊天里发出这句提醒，一句话即可。"
        )
        try:
            res = await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=reminder["stream_id"],
                intent=intent,
                reason="reminder-plugin 定时提醒",
                priority="high",
                metadata={"reminder_id": reminder["id"], "kind": reminder["kind"]},
            )
            return isinstance(res, dict) and bool(res.get("success"))
        except Exception as exc:
            _log.warning(f"[reminder] proactive 触发失败: {exc}")
            return False

    async def _bot_spoke_since(self, stream_id: str, platform: str, since_ts: float) -> bool:
        """确认入队后 bot 自己发过言（判断不了就信任主链路）。"""
        bot_account = self._bot_accounts.get(platform) if platform else None
        if not bot_account:
            return True
        try:
            msgs = await self.ctx.message.get_recent(chat_id=stream_id, limit=10)
            if not isinstance(msgs, list):
                return True
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                try:
                    ts = float(m.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                if ts <= since_ts:
                    continue
                info = m.get("message_info") or {}
                uid = str((info.get("user_info") or {}).get("user_id") or "")
                if uid == bot_account:
                    return True
            return False
        except Exception:
            return True

    async def _send_fallback(self, reminder: dict) -> None:
        time_str = reminder["remind_at"][5:16]
        advance_note = "（提前提醒）" if reminder["kind"] == "advance" else ""
        who = reminder.get("user_name") or "大家"
        text = await self._persona_say(
            f"定时任务直接触发：到点提醒。请用你的口吻提醒{who}："
            f"『{reminder['title']}』，约定时间 {time_str}{advance_note}。一句话（25字内），说重点。"
        )
        await self._reply(
            reminder["stream_id"],
            text or f"提醒：{reminder['title']}（{time_str}）{advance_note}",
        )

    async def _persona_say(self, fact_prompt: str) -> str:
        """让 LLM 以宿主人设口吻说一句话；失败返回空串（由调用方退化为朴素文案）。"""
        prompt = f"{self._persona_header()}\n{fact_prompt}\n只输出要发送的那句话。"
        return await self._llm(prompt, temperature=0.7, max_tokens=80)

    async def _reply(self, stream_id: str, text: str) -> None:
        if not text:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            _log.warning(f"[reminder] 发送失败: {exc}")


def create_plugin() -> NaturalReminderPlugin:
    return NaturalReminderPlugin()
