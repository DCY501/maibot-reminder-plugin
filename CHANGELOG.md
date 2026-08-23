# Changelog

## 0.1.1 (2026-08-23)

- 时间解析新增规则快路径：相对时间（N分钟后/N秒后）、绝对时间、今明后天、周X 等常用格式零 LLM 成本秒解析，LLM 仅兜底模糊表达
- LLM 调用失败输出诊断日志
- 工具返回文案提示 Planner 勿重复重试

## 0.1.0 (2026-08-23)

- 首个版本：set_reminder / cancel_reminder / list_reminders 三个工具
- 到点送达走 maisaka.proactive.trigger（口吻与本体一致），120 秒未确认兜底直发
- 兜底文案从宿主配置读取 personality 注入，插件零口吻耦合
- JSON 持久化，重启不丢；支持多档提前提醒
