# Changelog

## 0.3.0 (2026-08-25)

- 新增自动观察群约定：Planner 识别群里「已敲定」的未来约定（训练/比赛/聚餐）后静默登记，到点主动提醒一次
- 类型化提醒策略：比赛提前一天 20:00 打气、训练提前 60 分钟催促、聚餐提前 30 分钟嘴馋（可配），其余活动用默认提前量
- 新增工具 `observe_group_event(title, time_expression, event_type, location?)`
- 两级触发防误判：仅登记时间可解析为未来 + 活动明确的约定；随口闲聊/未定提议不登记
- 去重/改期：同群同名约定只登记一次，改时间自动更新；取消沿用 cancel_reminder
- 限流：每群每小时主动播报上限（默认 2 条，超出改兜底直发，保证必达但不刷屏）
- 触发仍走 `maisaka.proactive.trigger`（宿主人设口吻，不硬编码语气）

## 0.2.0 (2026-08-25)

- 审核修复：config.toml 改为模板 config.example.toml（仓库不再直接提交配置文件，避免更新冲突），config.toml 加入 .gitignore
- README 新增「能力与隐私说明」：明确 message.get_recent 仅用于判断 bot 自身发言状态（兜底验证），不记录/存储/上传聊天内容；说明 maisaka.proactive.trigger 为 MaiBot 宿主公开能力且实测稳定
- 版本号统一：manifest version / config_version / 代码默认值统一为 0.2.0

## 0.1.1 (2026-08-23)

- 时间解析新增规则快路径：相对时间（N分钟后/N秒后）、绝对时间、今明后天、周X 等常用格式零 LLM 成本秒解析，LLM 仅兜底模糊表达
- LLM 调用失败输出诊断日志
- 工具返回文案提示 Planner 勿重复重试

## 0.1.0 (2026-08-23)

- 首个版本：set_reminder / cancel_reminder / list_reminders 三个工具
- 到点送达走 maisaka.proactive.trigger（口吻与本体一致），120 秒未确认兜底直发
- 兜底文案从宿主配置读取 personality 注入，插件零口吻耦合
- JSON 持久化，重启不丢；支持多档提前提醒
