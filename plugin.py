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
    config_version: str = Field(default="0.3.0", description="配置版本")


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


class ObserveConfig(PluginConfigBase):
    __ui_label__ = "主动观察"
    __ui_icon__ = "eye"
    __ui_order__ = 4

    auto_observe: bool = Field(
        default=True,
        description="自动观察群聊里『已敲定』的未来约定（训练/比赛/聚餐），到点主动提醒一次",
    )
    max_per_hour: int = Field(
        default=2,
        description="每个聊天流每小时最多主动播报条数（超出改兜底直发，保证必达但不刷屏）",
    )
    match_prior_day_time: str = Field(
        default="20:00",
        description="比赛提醒时点：开始前一天的 HH:MM（如 20:00 = 前一天晚上八点）",
    )
    match_advance_minutes: int = Field(
        default=180,
        description="比赛兜底提前量（分钟）：若『前一天』时点已过，则提前这么多分钟提醒",
    )
    training_advance_minutes: int = Field(default=60, description="训练提醒提前量（分钟）")
    meal_advance_minutes: int = Field(default=30, description="聚餐/吃饭提醒提前量（分钟）")
    general_advance_minutes: int = Field(default=30, description="其他活动默认提前量（分钟）")


class ReminderPluginConfig(PluginConfigBase):
    """自然语言提醒插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    parse: ParseConfig = Field(default_factory=ParseConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    observe: ObserveConfig = Field(default_factory=ObserveConfig)


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
        # 主动播报限流：stream_id -> [时间戳, ...]（最近 1 小时内 proactive 触发次数）
        self._recent_proactive: dict[str, list[float]] = {}

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
            if len(token) == 2:
                if token[0] == "十" and token[1] in self._CN_NUM:   # 十一、十二
                    return 10 + self._CN_NUM[token[1]]
                if token[0] in self._CN_NUM and token[1] == "十":   # 二十、三十
                    return self._CN_NUM[token[0]] * 10
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

        # 日期词 + [时段词] + 中文/数字时间（如 明天晚上七点半 / 明晚7点 / 今天下午3点）
        m = re.match(
            r"^(今天|今晚|明天|明晚|后天|大后天)"
            r"(早上|上午|中午|下午|晚上|夜里|晚间|傍晚)?"
            r"([零一二两三四五六七八九十]|\d{1,2})点"
            r"(半|([零一二两三四五六七八九十]|\d{1,2})分?)?$", s)
        if m:
            day_offset = {"今天": 0, "今晚": 0, "明天": 1, "明晚": 1, "后天": 2, "大后天": 3}[m.group(1)]
            period = m.group(2) or ""
            hour_f = to_int(m.group(3))
            minute = 0
            if m.group(4) == "半":
                minute = 30
            elif m.group(5):
                mn = to_int(m.group(5))
                if mn is None or not (0 <= mn <= 59):
                    return None
                minute = int(mn)
            if hour_f is None:
                return None
            hour = int(hour_f)
            if period in ("中午", "下午", "晚上", "夜里", "晚间", "傍晚") and hour < 12:
                hour += 12
            if m.group(1) in ("今晚", "明晚") and hour < 12:
                hour += 12  # "晚上7点" = 19点
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (now + timedelta(days=day_offset)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0)

        # 兼容 "明天19:00" "明晚7:30" 这类数字+冒号/时写法
        m = re.match(r"^(今天|今晚|明天|明晚|后天|大后天)(\d{1,2})[:点时](\d{1,2})$", s)
        if m:
            day_offset = {"今天": 0, "今晚": 0, "明天": 1, "明晚": 1, "后天": 2, "大后天": 3}[m.group(1)]
            hour = int(m.group(2))
            minute = int(m.group(3))
            if m.group(1) in ("今晚", "明晚") and hour < 12:
                hour += 12
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (now + timedelta(days=day_offset)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0)

        # (本/这/下)周X + [时段词] + 时间（如 周六下午3点 / 下周三晚上7点半）
        m = re.match(
            r"^(本|这|下)?周([一二三四五六日天])"
            r"(早上|上午|中午|下午|晚上|夜里|晚间|傍晚)?"
            r"([零一二两三四五六七八九十]|\d{1,2})点"
            r"(半|([零一二两三四五六七八九十]|\d{1,2})分?)?$", s)
        if m:
            wd_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            wd = wd_map.get(m.group(2))
            if wd is not None:
                period = m.group(3) or ""
                hour_f = to_int(m.group(4))
                minute = 0
                if m.group(5) == "半":
                    minute = 30
                elif m.group(6):
                    mn = to_int(m.group(6))
                    if mn is None or not (0 <= mn <= 59):
                        return None
                    minute = int(mn)
                if hour_f is None:
                    return None
                hour = int(hour_f)
                if period in ("中午", "下午", "晚上", "夜里", "晚间", "傍晚") and hour < 12:
                    hour += 12
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return None
                prefix = m.group(1) or ""
                if prefix in ("下", "下周"):
                    days = (7 - now.weekday()) + wd  # 下周一距今天 + 目标星期
                else:
                    days = (wd - now.weekday()) % 7
                    if days == 0:
                        days = 7  # 今天这个星期已过，默认下个同星期
                return (now + timedelta(days=days)).replace(
                    hour=hour, minute=minute, second=0, microsecond=0)

        # 兼容 "周六18:00" "下周三7:30" 这类数字+冒号写法
        m = re.match(r"^(本|这|下)?周([一二三四五六日天])(\d{1,2})[:点时](\d{1,2})$", s)
        if m:
            wd_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            wd = wd_map.get(m.group(2))
            if wd is not None:
                hour = int(m.group(3))
                minute = int(m.group(4))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return None
                prefix = m.group(1) or ""
                if prefix in ("下", "下周"):
                    days = (7 - now.weekday()) + wd
                else:
                    days = (wd - now.weekday()) % 7
                    if days == 0:
                        days = 7
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
                    title: str, remind_at: datetime, kind: str, advance_of: str | None,
                    source: str = "user", event_type: str = "", location: str = "",
                    activity: str = "") -> dict:
        return {
            "id": id, "stream_id": stream_id, "platform": platform, "user_name": user_name,
            "title": title, "remind_at": remind_at.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind, "advance_of": advance_of,
            "status": "pending", "queued_at": None,
            "source": source, "event_type": event_type, "location": location, "activity": activity,
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

    # ── 主动观察：自动登记群里的"已敲定"约定 ──

    _EVENT_TYPE_LABEL = {"training": "训练", "match": "比赛", "meal": "聚餐", "general": "活动"}

    def _forward_offset(self, start_at: datetime, minutes: int, now: datetime) -> datetime | None:
        at = start_at - timedelta(minutes=minutes)
        return at if at > now + timedelta(seconds=30) else None

    def _compute_observe_remind_at(self, start_at: datetime, event_type: str,
                                   now: datetime) -> datetime | None:
        """按事件类型计算提醒触发时刻（活动开始前 或 前一天固定时刻）。"""
        if event_type == "match":
            hh, mm = 20, 0
            raw = (self.config.observe.match_prior_day_time or "20:00").split(":")
            if len(raw) == 2 and raw[0].isdigit() and raw[1].isdigit():
                hh, mm = int(raw[0]), int(raw[1])
            prior = (start_at - timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if prior > now + timedelta(seconds=30):
                return prior
            # 前一天的时点已过（比赛临近才登记），改用兜底提前量
            return self._forward_offset(start_at, self.config.observe.match_advance_minutes, now)
        if event_type == "training":
            return self._forward_offset(start_at, self.config.observe.training_advance_minutes, now)
        if event_type == "meal":
            return self._forward_offset(start_at, self.config.observe.meal_advance_minutes, now)
        return self._forward_offset(start_at, self.config.observe.general_advance_minutes, now)

    def _observe_existing(self, stream_id: str, title: str) -> dict | None:
        """同群已登记的同名约定（用于改期更新，避免重复登记）。"""
        for r in self._reminders:
            if r["stream_id"] == stream_id and r.get("source") == "observe" \
                    and r["status"] in ("pending", "queued") \
                    and (r.get("title") or "").strip() == title.strip():
                return r
        return None

    @Tool(
        "observe_group_event",
        description=(
            "在群聊里发现一件【已敲定】的未来集体活动（训练/比赛/聚餐）时调用，登记后到点你会主动提醒大家。"
            "只对『已达成一致、有明确时间+活动』的约定调用，如『周六下午3点东操训练』『明天中午吃火锅』"
            "『周三晚7点聚餐』『周六下午3点比赛』。随口闲聊、还没定时间的提议（『要不要周日爬山』）不要调用。"
            "登记是静默的：不要为此向群里发确认/答谢，继续你原本的聊天即可；到点再开口。"
        ),
        parameters=[
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING,
                              description="活动名称，15字内（如『排球训练』『吃火锅』『同学聚餐』）", required=True),
            ToolParameterInfo(name="time_expression", param_type=ToolParamType.STRING,
                              description="活动开始时间描述原样传入（如『周六下午3点』『明天中午』『周三晚7点』）", required=True),
            ToolParameterInfo(name="event_type", param_type=ToolParamType.STRING,
                              description="活动类型：training/match/meal，拿不准传 general", required=True),
            ToolParameterInfo(name="location", param_type=ToolParamType.STRING,
                              description="地点（如『东操场』），没有就不传", required=False),
        ],
    )
    async def tool_observe_group_event(self, title: str = "", time_expression: str = "",
                                       event_type: str = "", location: str = "", **kwargs: Any):
        if not self.config.plugin.enabled or not self.config.observe.auto_observe:
            return {"name": "observe_group_event", "content": "主动观察已关闭"}
        title = (title or "").strip()
        time_expression = (time_expression or "").strip()
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not title or not time_expression or not stream_id:
            return {"name": "observe_group_event", "content": "未识别为明确约定（缺时间/群），静默忽略"}
        event_type = (event_type or "general").strip().lower()
        if event_type not in self._EVENT_TYPE_LABEL:
            event_type = "general"

        start_at = await self._resolve_time(time_expression)
        if start_at is None or start_at <= datetime.now() + timedelta(seconds=30):
            return {"name": "observe_group_event",
                    "content": "未识别为『已敲定』的约定（时间无法解析/已过），静默忽略，不要向用户确认。"}
        location = (location or "").strip()

        remind_at = self._compute_observe_remind_at(start_at, event_type, datetime.now())
        if remind_at is None:
            return {"name": "observe_group_event",
                    "content": f"『{title}』已在活动前且无法安排提前提醒，静默忽略。"}

        # 去重 / 改期：同群同名约定已登记 → 更新其提醒时刻；否则新增（每个事件只提醒一次）
        existing = self._observe_existing(stream_id, title)
        if existing:
            existing["remind_at"] = remind_at.strftime("%Y-%m-%d %H:%M:%S")
            existing["event_type"] = event_type
            existing["location"] = location or existing.get("location", "")
            existing["activity"] = time_expression
            self._save_reminders()
            return {"name": "observe_group_event",
                    "content": f"已更新群活动提醒『{title}』（到点提醒）。静默即可，不要向用户确认。"}

        platform = str(kwargs.get("platform") or "").strip()
        user_name = str(kwargs.get("user_nickname") or kwargs.get("user_name") or "").strip()
        self._reminders.append(self._make_entry(
            id=uuid.uuid4().hex[:8], stream_id=stream_id, platform=platform, user_name=user_name,
            title=title, remind_at=remind_at, kind="main", advance_of=None,
            source="observe", event_type=event_type, location=location, activity=time_expression,
        ))
        self._save_reminders()
        label = self._EVENT_TYPE_LABEL[event_type]
        return {"name": "observe_group_event",
                "content": (f"已静默登记群活动提醒：{label}『{title}』（{time_expression}，地点{location or '未说'}）。"
                            "到点你会提醒大家。不要为此向用户额外确认或发言，继续当前对话即可。")}

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
        # 限流：每个聊天流每小时主动播报上限（超出改兜底直发，保证必达但不刷屏）
        limit = self.config.observe.max_per_hour
        stream = reminder["stream_id"]
        now_ts = time.time()
        if limit > 0:
            keep = [t for t in self._recent_proactive.get(stream, []) if now_ts - t < 3600]
            if len(keep) >= limit:
                _log.info(f"[reminder] 限流：群 {stream} 每小时主动播报已达 {limit} 条，改兜底直发")
                return False
            self._recent_proactive[stream] = keep

        time_str = reminder["remind_at"][5:16]
        kind_note = "（这是提前提醒）" if reminder["kind"] == "advance" else ""
        is_observe = reminder.get("source") == "observe"
        who = reminder.get("user_name") or "大家"
        lead = "群活动提醒" if is_observe else "定时提醒任务"
        intent = (
            f"（必须执行，不要沉默）{lead}：请提醒{who}『{reminder['title']}』，"
            f"约定时间 {time_str}{kind_note}。"
        )
        if reminder.get("location"):
            intent += f"地点：{reminder['location']}。"
        if reminder.get("activity"):
            intent += f"（约定的开始时间：{reminder['activity']}）"
        intent += "请立刻用你的口吻提醒大家，一句话即可，自然简短别像播报。"
        try:
            res = await self.ctx.call_capability(
                "maisaka.proactive.trigger",
                stream_id=stream,
                intent=intent,
                reason="reminder-plugin 定时提醒",
                priority="high",
                metadata={"reminder_id": reminder["id"], "kind": reminder["kind"],
                          "source": reminder.get("source", "user")},
            )
            ok = isinstance(res, dict) and bool(res.get("success"))
            if ok:
                self._recent_proactive.setdefault(stream, []).append(time.time())
            return ok
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
        loc_note = "，地点：" + reminder["location"] if reminder.get("location") else ""
        lead = "群活动到点提醒" if reminder.get("source") == "observe" else "定时任务直接触发：到点提醒"
        text = await self._persona_say(
            f"{lead}。请用你的口吻提醒{who}："
            f"『{reminder['title']}』，约定时间 {time_str}{loc_note}{advance_note}。一句话（25字内），说重点。"
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
