# 时延分析 · AD P99

面向同一车辆两个时段的时延与帧率对比工具。既可通过飞书/CC Connect 发起任务、向阿斯加德获取结果，也可在已获准的情况下直接分析两份结果 Sheets。

仓库包含任务派发、结果关联、AD/PCM/CPU 分析、报告生成和来源会话交付源码。实际服务使用 **Windows x64、CPython 3.13**，并需要独立的飞书身份、阿斯加德接入和相应数据权限。

## 输入与输出

| 入口 | 输入 | 处理与交付 |
| --- | --- | --- |
| 会话任务 | 同一 VIN 的 Base/PCM 更新两个完整时窗及真实消息上下文 | 派发查询、收集对应 Sheets/日志、分析并回原话题交付报告 |
| 已有结果表 | 两份 Sheets 链接、各自 VIN/时间元数据及明确的读取身份 | 按显式参数执行表格与性能对比 |

主要分析项包括 AD P99、帧率、PCM P99/帧率，以及两个时窗各自日志中的系统和 PCM CPU。显式 Base/PCM 更新角色优先；未标注时按输入顺序，不按时间先后重排。缺失项保留来源与原因。

## 业务流程

1. 接入层保存真实入站上下文，公共路由校验车辆、双时窗和时区。
2. 独立 User 身份向阿斯加德发送版本与日志查询，保存实际派发消息 ID。
3. 按来源、机器人身份、VIN、精确时间和发送边界核对结果，避免串用其他任务的数据。
4. 分析结果后由独立 Bot 创建报告、治理权限并交付原话题。

本地 `job_key` 表示排队，实际 `message_id` 表示已派发，完成状态还要求送达凭据。未知发送结果先核对原记录，交付失败不重发日志查询。

## 获取源码与校验

```powershell
git clone https://github.com/Inner-vic/ad-p99-analysis.git
cd ad-p99-analysis
python -B -X utf8 tools/check_repository.py
```

校验不启动派发。业务依赖以 [requirements.txt](workspace/.agents/skills/ad-p99-analysis/requirements.txt) 为准，使用独立解释器安装。

## 新环境准备

下面的解释器和目录均为示例，替换为自己的 CPython 3.13 x64 和尚不存在的目标目录：

```powershell
python -B -X utf8 tools/prepare_workspace.py --workspace "D:/workspaces/ad-p99" --base-python "C:/Path/To/Python313/python.exe"
```

工具复制源码、创建 venv 和私有配置样例，不安装依赖、授权或启动任务。之后按[部署与配置](docs/deployment.md)完成：

- 固定依赖、独立 CLI home 及 User/Bot profile。
- 飞书应用 scope、资源权限、首次 OAuth 和本用户的加密凭据副本。
- 阿斯加德来源、报告目录及车云配置；补充 PCM/CPU 时需在进程启动前设置 `AD_P99_SETTINGS_PATH`。
- CC Connect 真实上下文接入、授权维护和首次完整任务验收。

首次接入与续期步骤见[飞书与阿斯加德配置](docs/lark-asgard-integration.md)。新机器使用本人的授权，不复制其他用户的凭据。

## 开发入口与源码

| 路径 | 内容 |
| --- | --- |
| `workspace/.agents/skills/ad-p99-analysis/scripts/asgard_worker.py` | 任务启动、状态、收卡、分析与交付 |
| `workspace/.agents/skills/ad-p99-analysis/scripts/ad_p99.py` | AD 结果表解析与对比 |
| `workspace/.agents/skills/ad-p99-analysis/scripts/performance_comparison.py` | 系统、PCM 性能对比 |
| `workspace/.agents/skills/chassis-business-routing/` | 公共意图和时间解析 |
| `workspace/deployment/` | 凭据锁、隔离授权及续期维护 |
| `tools/`、`examples/` | 检查、候选准备及占位配置 |

在准备好的独立解释器中，可用上述业务脚本的 `--help` 查看显式参数。真实启动必须经过路由和来源验证；恢复按精确任务记录执行，不使用“最近任务”。详细契约见[上游接入与恢复](workspace/.agents/skills/ad-p99-analysis/references/upstream-integration.md)。

## 测试与维护

安装依赖后，从仓库根目录运行离线测试，例如：

```powershell
& "D:/workspaces/ad-p99/runtime/ad-p99-python/Scripts/python.exe" -B -X utf8 tools/run_tests.py
```

GitHub 的 [Source checks](.github/workflows/source-checks.yml) 检查源码与完整性清单，不执行真实飞书派发。离线验证范围见[发布说明](PUBLICATION.md)，授权失败、状态和恢复处理见[运维说明](docs/operations.md)。

本项目与走查测试、日志分析分别维护运行环境和任务数据。凭据、真实 VIN/会话、日志、报告及运行状态不进入源码仓库。
