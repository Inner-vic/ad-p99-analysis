# 部署、配置和接入

`tools/prepare_workspace.py` 仅向不存在的独立目录复制 workspace、创建 CPython 3.13 x64 venv 和私有配置样例；不安装依赖、不授权、不注册服务、不派发任务。保留 `.agents/skills/` 与 `deployment/` 的相对结构，动态 import 依赖已一并打包。实际目录可变，不需要原机器目录或用户主目录。

## 配置

| 文件/字段 | 配置要求 |
| --- | --- |
| `private/asgard.settings.json` | 从样例填写 CLI、专属 lark_home、project_id、固定 App/User、user/bot profile、`asgard_open_id`、`asgard_platform`、独立 job/runtime 目录及 Bot 可管理的飞书报告文件夹 |
| `deployment/cc-connect-lark-isolation.json` | 与 settings 逐字段一致，绑定真实 CLI 版本和独立 Python，基础 scope 加业务所需 scope；不能照抄另一应用看到的 open ID |
| `private/cloud.settings.json` | 车云获准 username；补充 PCM/CPU 需要，不是上游阿斯加德授权 |
| `AD_P99_SETTINGS_PATH` | 必须在**启动业务进程之前**指向上述车云配置绝对路径；worker 会改变后续 CLI home，不能依赖默认用户主目录碰巧找到配置 |
| `AD_P99_PROD_ORIGIN/TEST_ORIGIN` 与相应 `DOWNLOAD_HTTPS_HOSTS/DOWNLOAD_HTTP_HOSTS` | 默认组织端点在代码中；只有经批准变更时设置，origin 和下载 host 分别核验，不能放宽白名单 |

准备步骤：校验文件 → 独立环境与固定依赖 → 私有配置 → 飞书应用及资源权限 → 固定 User 首次 OAuth 和 DPAPI 副本 → 授权只读检查/按需续期 → 离线回归 → 接入 CC Connect → 获准首单线上验收。详见 [飞书接入](lark-asgard-integration.md)。

时延 start 自身派生后台 worker，不依赖走查/Uroad 调度器；如由服务宿主接入，应继承同一受控环境与 Windows 登录用户。维护服务定期运行 `deployment/refresh-feishu-user-auth.ps1 -ManifestPath <私有清单>`，使用当前交互账户、有限重试，禁止双安装争用同一 profile。部署包不覆盖本机计划任务；组织服务管理系统或 `tools/Install-AuthMaintenance.ps1` 可注册一个项目专属任务，默认 60 分钟、不立即执行。

CC Connect 的入站处理器应读取 `workspace/AGENTS.md` 和公共 router；将传输首行原样保存并传给 worker，正文单独解析。只在 start 真实返回 job_key 后确认排队；上游回执另行确认。结果表和最终报告均绑定原始消息，不将触发者当作固定派发身份。

## 迁移与回退

先停止新接单、等待现有所属进程结束，保留精确任务状态和消息/报告回执。用版本目录部署，验证后切换宿主。回退只恢复已明确备份的代码/配置/服务 action；已有任务按原版本显式恢复，不整体重派、不删除加密副本、不迁移别人的 DPAPI 数据。新机器/新职责人必须自行获准并重新授权，不能复制旧用户凭据。
