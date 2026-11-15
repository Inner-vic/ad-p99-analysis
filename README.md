# 时延分析（AD P99）

智能底盘双时段时延对比的独立交接存档，包含 **lark-cli → 阿斯加德 Bot → 结果 Sheets → AD/PCM/CPU 分析 → 原话题报告** 的完整业务代码，以及双 Sheets 直接分析入口。归档版本 `2026.11.04.archive1`，业务脚本来自当前部署，11 个生产模块逐字节保留；这次整理不改动线上部署。

## 数据流与角色

1. CC Connect 提供真实入站正文和原始上下文，公共路由校验同 VIN 双时段与北京时间。
2. 固定开发者 User 的隔离 lark-cli profile 在来源消息线程 @ 阿斯加德，发送版本查询与两条独立 `fsdlog`。
3. 按本任务消息 ID、机器人身份、VIN、精确时窗和发送边界轮询收卡，读取两份结果 Sheets。显式 Base/PCM 更新角色优先；未标注时按输入顺序，不按时间先后重排。
4. 提取 AD P99、帧率与两个时窗各自 `log_fsdA_service` 的系统 CPU、PCM P99、PCM 帧率和 PCM CPU；PCM 按 PID、Pre/Post、ctx 与完整十帧状态窗口计算。
5. 独立 Bot 创建报告、收紧权限、授权来源群查看、转所有权给原请求人；固定 User 设置 L3，撤销临时权限后 Bot 回原话题。有真实送达凭据才完成。

## 包含与排除

`workspace/.agents/skills/ad-p99-analysis/` 是完整业务；`workspace/deployment/` 包含凭据锁、加密副本、OAuth 与续期维护；公共时间路由和 CC Connect 上下文解析器只带必需文件，不捆绑 Uroad/走查执行服务。`tools/` 提供离线校验、测试、新目录准备和飞书接入工具；`examples/` 只含占位配置。无凭据、token、真实 VIN/会话、任务状态、原始日志、业务报告、缓存、旧版本副本或第三方二进制。

## 新环境部署

使用 Windows x64、独立 CPython 3.13 和组织获准的 lark-cli（本次核对 1.0.97）。Python 包以 Skill 的 requirements 为准，另固定间接依赖 `et-xmlfile==2.0.0`，通过组织包源安装。

```powershell
python -B -X utf8 tools/check_repository.py
python tools/prepare_workspace.py --workspace $NewWorkspace --base-python $Python313
# 用返回的独立解释器安装 requirements；不启动任务或自动授权。
& $HostPython -I -m pip install --no-deps --only-binary=:all: -r workspace/.agents/skills/ad-p99-analysis/requirements.txt
& $HostPython -B -X utf8 tools/run_tests.py
```

按 [部署与配置](docs/deployment.md) 填写私有配置，再按 [lark-cli 与阿斯加德接入](docs/lark-asgard-integration.md) 初始化固定 User/Bot、scope、资源权限、首次 OAuth/凭据副本及维护服务。必须接入真实 CC Connect 上下文；不能手造群/用户/消息字段来启动。

## 启动与恢复

在已部署工作区运行；变量必须来自同一获准入站请求。先运行公共 router，再将其验证后的 `normalized.canonical_text` 传给 start。存档、代码维护或历史状态追问不创建业务任务。

```powershell
& $HostPython .agents/skills/ad-p99-analysis/scripts/asgard_worker.py start --text $CanonicalText --ccconnect-context $OriginalContext --settings $PrivateSettings
& $HostPython .agents/skills/ad-p99-analysis/scripts/asgard_worker.py status --record $ExactRecord --settings $PrivateSettings --ccconnect-context $OriginalContext
# 已有 comparison_key 时只恢复收卡/分析/治理/交付，不重发 fsdlog。
& $HostPython .agents/skills/ad-p99-analysis/scripts/asgard_worker.py resume --record $ExactRecord --settings $PrivateSettings
```

`job_key` 只证明本地排队，上游 `message_id` 才证明已派发。发送结果未知、部分派发、同窗冲突或权限未完成时停止并检查精确记录，不选择最近任务、不创建重复报告。详见 [上游接入与恢复契约](workspace/.agents/skills/ad-p99-analysis/references/upstream-integration.md)。

已有两份 Sheets 可使用 `ad_p99.py` / `performance_comparison.py` 的显式参数入口，先看 `--help`；必须绑定每份表各自 VIN/时间 metadata、明确 user/bot 读取身份，并沿用隔离 CLI home。未授权直接 Sheets 读取不得自动尝试。

## 验证与维护

[验证记录](docs/validation-results.json) 区分离线测试和新环境线上验收。[贡献规范](CONTRIBUTING.md) 要求更新清单并审查依赖；[运维说明](docs/operations.md) 覆盖凭据失效和恢复。CI 使用 Python 3.13 容器执行完整离线回归；公司 Runner 若无法访问 Docker Hub，配置组织批准的内部镜像，不跳过检查来制造通过状态。

## GitLab 自动校验状态

当前仅保留**手动流水线**：在 GitLab 的 Build → Pipelines → Run pipeline 明确启动。Push、合并请求和文档提交不自动执行，避免 Runner 镜像下载失败反复发送 Failed 通知。测试命令和检查步骤完整保留。

公司 Runner 当前无法从 Docker Hub 拉取 Python 镜像，尚未运行到测试；需要维护人员配置获准内部镜像或修复镜像源后再手动验证，成功后另行恢复自动触发。历史 Failed 记录保留，不能当作测试失败或线上业务任务失败；暂停触发不代表 CI 已通过。

公开副本的来源、示例配置与验证范围见 [发布说明](PUBLICATION.md)。
