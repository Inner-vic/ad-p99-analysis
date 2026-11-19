# 时延分析业务入口

CC Connect 入站第一行 request_context_v=1 仅保存为原始 context_header，其余正文运行 .agents/skills/chassis-business-routing/scripts/intent_router.py。维护、存档、GitLab 上传、历史失败解释和仅状态追问不创建车辆任务，关键词命中不等于执行授权。

本独立工作区只执行 ad-p99 / ad-p99-asgard；其他业务交各自独立项目，不回退。需要字段时只追问缺失值。阿斯加德入口使用 router 验证后的 canonical_text 和原始 context_header，显式 --settings 指向本部署私有配置；启动环境须设置 AD_P99_SETTINGS_PATH 指向车云配置。不得伪造来源群、用户、消息或按最近任务关联。

start 真实 job_key 只确认本地排队，上游 message_id 才确认派发。固定开发者 User 向阿斯加德发送，独立 Bot 创建和治理报告后回原话题。completed 必须有真实来源绑定的送达凭据；本轮后台已投递不重复通知。状态用精确 record 和原始上下文，只读 status，不调用 start/resume。恢复先读 Skill 及 upstream-integration 契约。
