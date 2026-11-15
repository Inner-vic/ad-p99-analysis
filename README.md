# 时延分析 · AD P99

[实习项目总览](https://github.com/Inner-vic/internship-projects) · [使用与验证](docs/project-guide.md)

关联上游查询、双时窗结果表与性能日志，形成可追溯的时延比较。

## 解决的问题

时延比较依赖外部查询机器人返回结果。卡片到达顺序、网络重试、发送结果未知或报告权限失败，都可能让“重试一次”变成重复派发，甚至把另一项任务的数据当成当前结果。

项目围绕精确来源与任务状态，连接上游消息、两份结果表、对应窗口日志和最终报告。

## 实现了什么

| 入口 | 能力 |
| --- | --- |
| 会话任务 | 解析同车双时窗，向既有阿斯加德查询机器人派发请求，核对并收取对应结果 |
| 已有 Sheets | 在已获准的身份与明确元数据下直接分析两份结果表 |
| 联合分析 | 对比 AD P99、帧率、PCM 指标和各自窗口的系统/PCM CPU |
| 报告交付 | 独立 Bot 创建、治理并回传报告，保存真实来源送达凭据 |

## 实现架构

![时延分析的主要模块、执行顺序和关键边界](https://raw.githubusercontent.com/Inner-vic/internship-projects/main/assets/architecture/ad-p99-analysis.svg)

图示为会话任务入口；已有两份 Sheets 的直接分析入口从显式元数据和读取身份开始，复用相应分析能力。阿斯加德属于外部服务，本仓库实现与其衔接的编排与结果处理。

## 关键工程设计

### 1. 区分请求人、派发人和报告创建者

固定 User profile 负责上游交互，独立 Bot profile 负责报告资产。来源上下文、请求人和实际派发身份分别记录，避免默认身份或错误 profile 改写任务含义。

### 2. 用真实消息定位结果

本地任务键只说明已受理，上游 `message_id` 才证明已经派发。结果还需核对来源群、机器人、VIN、精确时窗和发送边界，不能根据卡片先后或最后一份表决定角色。

### 3. 分阶段保存外部副作用

派发、收卡、分析、报告创建、权限和回传各自有可恢复记录。已有上游消息时不整单重派，已有报告时补齐治理与发送，从而把恢复范围限制在尚未完成的步骤。

## 实现与验证范围

仓库包含上游衔接、AD 表格分析、性能日志处理和授权隔离实现。离线测试不能代替外部平台授权与真实交付验收；公开验证范围见[发布说明](https://github.com/Inner-vic/ad-p99-analysis/blob/main/PUBLICATION.md)，外部交互契约见[上游接入设计](workspace/.agents/skills/ad-p99-analysis/references/upstream-integration.md)。

## 源码导读

| 源码 | 关注的实现 |
| --- | --- |
| [workspace/.agents/skills/ad-p99-analysis/scripts/asgard_worker.py](workspace/.agents/skills/ad-p99-analysis/scripts/asgard_worker.py) | 派发、收卡与恢复状态 |
| [workspace/.agents/skills/ad-p99-analysis/scripts/ad_p99.py](workspace/.agents/skills/ad-p99-analysis/scripts/ad_p99.py) | AD 结果表解析 |
| [workspace/.agents/skills/ad-p99-analysis/scripts/performance_comparison.py](workspace/.agents/skills/ad-p99-analysis/scripts/performance_comparison.py) | 系统与 PCM 对比 |
| [workspace/.agents/skills/ad-p99-analysis/scripts/performance_log_source.py](workspace/.agents/skills/ad-p99-analysis/scripts/performance_log_source.py) | 窗口性能日志来源 |

## 项目资料

使用步骤、依赖和本地验证见[使用与验证](docs/project-guide.md)；源码来源与验证范围见[发布说明](https://github.com/Inner-vic/ad-p99-analysis/blob/main/PUBLICATION.md)。本页用于介绍实习阶段的实现与设计，具体结论以所链接的代码和证据为准。

[日志分析](https://github.com/Inner-vic/uroad-log-analyzer)和[走查测试](https://github.com/Inner-vic/walkthrough-test)提供相关车辆分析实现；外部阿斯加德服务不在这些仓库中。
