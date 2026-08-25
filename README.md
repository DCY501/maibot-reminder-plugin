# 自然语言提醒插件（MaiBot 1.2.x）

通过自然对话设置定时提醒：**"明晚7点提醒我训练"**、**"10分钟后叫我关火"**、**"周六3点比赛，提前2小时提醒我"**。

## 特点

- **零人设耦合**：插件不自带任何口吻。设置回执由 Planner 用宿主人设说出；到点提醒走 `maisaka.proactive.trigger` 由主链路生成——装在谁的 bot 上，提醒就是谁的声音。LLM 兜底文案也会从宿主配置读取 personality 注入。
- **必达设计**：主链路入队后 120 秒（可配）未确认 bot 发过言，自动兜底直发；兜底也失败时退化为朴素事实文案。
- **持久化**：提醒存于插件数据目录 JSON，重启后未到点的任务继续执行。
- **提前提醒**：支持"N 分钟前"多档提前推送（默认关闭，配置开启）。
- **零第三方依赖**：纯 SDK + 标准库；时间解析规则快路径优先（相对时间/绝对时间/今明后/周X），LLM 仅兜底模糊表达。

## 工具

| 工具 | 说明 |
|---|---|
| `set_reminder(title, time_expression, advance_minutes?)` | Planner 识别用户提醒意图时调用 |
| `cancel_reminder(title?)` | 取消挂起的提醒（不特指则取消最近一个） |
| `list_reminders()` | 列出当前聊天挂起的提醒 |

## 配置（config.example.toml）

仓库只提供模板 `config.example.toml`，请复制为 `config.toml` 后按需修改（`config.toml` 已在 .gitignore 中，不会被提交）：

```toml
[schedule]
scan_interval_seconds = 20        # 到点扫描间隔
default_advance_minutes = []      # 默认提前提醒（分钟），如 [120]
[delivery]
use_proactive = true              # 到点走主链路生成（口吻一致）
fallback_after_seconds = 120      # 未确认发言则兜底直发
```

## 能力与隐私说明

- **`message.get_recent`**：仅用于"兜底验证"——`_bot_spoke_since` 在提醒入队后拉取最近 10 条消息，判断 **bot 自己**是否已发过言（据此决定是否需要兜底直发）。**只用于判断 bot 自身账号的发言状态，不记录、不存储、不上传任何聊天内容**。
- **`maisaka.proactive.trigger`**：MaiBot 宿主通过 plugin_runtime 向插件公开的正式能力（capabilities 注册项），用于请求主链路主动发起一轮对话（到点提醒由主链路以宿主人设生成，插件不硬编码口吻）。已在 MaiBot 1.2.x 实测稳定，非私有/未承诺实现。


## 安装

**方式一（推荐）**：MaiBot WebUI → 插件管理 → 安装插件，粘贴仓库地址：

```
https://github.com/DCY501/maibot-reminder-plugin
```

**方式二（手动）**：clone 到你的 MaiBot `plugins/` 目录：

```bash
git clone https://github.com/DCY501/maibot-reminder-plugin.git
```

装好即默认启用；说一句"10分钟后提醒我拿快递"即可体验。

## 许可

MIT（见 [LICENSE](LICENSE)）。变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 已知限制

- MaiBot 1.2.x 的 ON_MESSAGE 事件尚未接线（主链路 emit 被注释），故触发完全依赖 Planner 的工具调用——这同时意味着设置提醒的意图识别质量与 planner 模型能力相关。
- 群聊内"提前提醒"的提前量以设置时刻计算，不做时区换算（本地时间）。

## 兼容性

- MaiBot >= 1.2.0（manifest v2 / maibot-plugin-sdk 2.x）
- capabilities：`send.text` `llm.generate` `config.get` `message.get_recent` `maisaka.proactive.trigger`

