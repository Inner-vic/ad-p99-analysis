---
name: ad-p99-analysis
description: 执行智能底盘 AD P99 时延对比：可基于两份飞书 Sheets 直接对比，或由飞书自然语言双时段请求通过阿斯加德生成结果后再对比。
---

# AD P99 时延分析

由智能底盘业务入口路由。用户无需记忆 Skill 名称或 `fsdlog` 格式。

对外回复先读 [对外会话交互](../chassis-business-routing/references/conversation-interaction.md)。不重复接收回执；后台报告已确认投递时不另发相同结果。原有话题、派发与报告权限策略不变。

## 两种入口

两个入口共用同一角色规则：显式标注 Base／Baseline 或 PCM 更新时按标注绑定；未标注时按用户消息中的输入顺序，第一组为 Base，第二组为 PCM 更新。时间早晚不决定角色；Sheets 与其对应时间窗不得分开重排。

中文时间与日期省略先经业务路由的 [自然语言理解与程序校验](../chassis-business-routing/references/semantic-input.md)；AI 只能提取有原文依据的候选，不能补造 VIN 或时段。阿斯加德仍使用路由已验证的 `canonical_text`，上下文头保持原样；不更改旧任务、派发或报告权限流程。

- **阿斯加德双时段入口**：用户给出“时延分析”、同一 17 位 VIN 和两个不重叠时段，但尚未有结果表。先经 `chassis-business-routing` 规范化；使用其 `canonical_text` 和 CC Connect 原始请求上下文启动 `scripts/asgard_worker.py start`。脚本按来源 `message_id` 幂等，以配置中获授权的固定飞书**开发者用户身份**分别发送两条上游 `fsdlog` 并收集结果；来源群内任意用户均可发起请求，触发者身份不替换该开发者身份。报告创建、权限授予和最终回传使用独立的 **bot-only profile**。任务以来源消息为话题锚点，向阿斯加德的请求和最终报告均留在该话题（普通群聊则使用该消息的回复线程）。若阿斯加德自身把结果卡片发布到群主会话，worker 仅在来源群内按阿斯加德身份、精确 VIN/时段和本任务派发边界关联卡片，绝不按最近卡片匹配。
- **双 Sheets 入口**：用户已给出两份最终飞书 Sheets URL、同一 VIN 和两个不重叠时段。读取身份必须由路由结果或用户明确选择 `user`/`bot`。

## 上下文与隔离

阿斯加德入口必须将机器人消息首行的 `request_context_v=1` 原样传给 `--ccconnect-context`。该上下文提供真实 `chat_id`、`sender_id` 和 `message_id`；缺失或手工冲突时拒绝创建任务。不得用共享目录的“最近结果”匹配其他用户任务。

生产配置必须启用 `project_isolation_required=true`，并为 Lark CLI 指定不复用 Windows 用户主目录的项目专属 `lark_home`。用户与报告 Bot profile 必须使用 `project_id` 命名空间；运行前同时校验固定 App ID 和派发用户 openId。所有直接或间接调用 Lark CLI 的子进程都必须继承该 `lark_home`，禁止回退到用户全局 `~/.lark-cli`。

派发前必须使用配置中的显式 `lark_profile` 做授权预检。该 profile 是固定开发者用户身份，可以与请求人不同；预检得到的 user openId 必须写入任务记录，worker 与首次安全重试必须保持同一 openId，禁止任务中途切换派发身份。有效 token 距过期超过 15 分钟时只读取本地状态，不发起 OAuth 验证；临近过期、已过期或状态不完整时才执行 `auth status --verify`。若首次上游发送仍返回 `need_user_authorization`，仅在尚无任何上游 `message_id` 时强制刷新并使用同一幂等键重试一次；已有上游消息后不得整单自动重派。只有 refresh token 也不可用时才要求该开发者重新授权，`20140` 转应用管理员处理。

一次 `--verify` 返回的瞬时不完整状态不能直接判定为需要重新授权；worker 会短暂等待后复核一次。只有同一显式 profile 连续校验失败，才提示本人 OAuth。不得为了预防性检查在每个任务前启动登录，也不得使用或修改默认 profile。

报告必须由配置中的 `report_lark_profile` 以 Bot 身份创建在飞书云空间；该 profile 必须独立于阿斯加德用户 profile，且不得带可用用户登录态。配置 `report_parent_token` 时写入该 Bot 有权管理的飞书文件夹；未配置时写入 Bot 的 `my_library`，本地运行目录只保存临时 XML 和任务状态。创建后必须保持原目录，将所有权转给原请求者，并让 Bot 保留 `full_access`；飞书原生权限申请只通知所有者，不能仅把请求者设为 `full_access` 后仍让 Bot 持有所有权。

后台任务完成后，由 Bot 将协作者管理收紧为 `share_entity=only_full_access`，给来源群 `openchat + view`，再把所有权转给原始请求上下文中的 `sender_id`，Bot 保留 `full_access`。随后必须用固定开发者 `lark_profile` 将报告密级设为企业标签 `L3`。当固定开发者与报告所有者不是同一用户时，Bot 只能给任务记录中已固定的 `dispatch_user_open_id` 临时授予 `full_access`，设置 L3 后立即撤销该直接权限；授权、密级和撤销必须分别持久化，撤销未成功时不得交付。新任务记录必须带 `report_secure_label_required=true`，L3 未成功时不得回传报告；上线该规则前的历史报告没有此标记，恢复流程不得自动追补密级。原请求者作为所有者接收其他人看到报告后发起的飞书原生编辑权限申请；不得在报告创建时预先授权未知申请人。权限、所有权和密级步骤必须逐项持久化，全部成功后才由 Bot 回传链接；重试不得创建重复报告或扩大到其他群/用户。

L3 只需要用户 scope `docs:secure_label:readonly` 和 `docs:secure_label:write_only`。缺失时只能对显式 `lark_profile` 做这两个 scope 的增量授权；禁止带 `--recommend`，避免把企业禁用的“导出云文档”等无关权限加入授权请求。授权一次后沿用 token/refresh token，不得每次任务强制登录。企业标签可显示为“敏感（L3）”等本地化名称；按其中独立的等级代码唯一匹配，存在零个或多个候选时均停止，不得猜测。

报告名称中的车辆标识必须来自本任务话题内、由已配置阿斯加德身份返回的车辆版本卡片之“相关制品信息”首项；必须通过本任务的车辆版本查询消息和 VIN 校验。不得使用 VIN 后四位、其他话题回复或“最近一条”版本信息代替。话题内缺失、冲突或格式异常时停止报告创建并明确报错。

## 失败恢复

用户只问进度时，使用 `asgard_worker.py status --record <精确记录> --settings <固定配置> --ccconnect-context <当前原始首行>`，不执行 start/resume、授权预检、取数或分析。入口核对原请求人和来源会话，分别说明排队、等待两段结果、报告治理、发送及送达；历史 completed 缺少真实回执时仍明确未确认送达。没有精确绑定时先补任务引用，不按最近记录猜测。

启动前先验证请求与原始传输上下文；手工聊天/用户/消息字段不能替代首行。相同来源消息仅在 VIN、双窗命令、请求人和会话均一致时复用，业务输入校验在授权预检之前。worker/resume 使用任务级操作系统互斥，进程退出自动释放；竞争调用不得覆盖正在执行的任务状态。

comparison_key 在第一条上游请求前持久化，每次外部派发先保存意图、后保存消息回执。已有 comparison_key 或派发意图不得再运行 worker 整单重派；双窗派发不完整时保留实际消息证据，需核查原请求。报告创建和发送也各自保存意图，结果未知时停止重试，不能盲目创建第二份报告或重复通知。报告完成必须有真实 Bot 输出消息 ID 及来源绑定凭据。

状态判断与恢复必须基于来源 `message_id` 对应的 runtime 记录及其 `comparison_key`，不得凭聊天中的“最近任务”判断。原始性能日志下载对 `IncompleteRead`、超时和连接重置执行最多三次整文件重试；每次失败清理 `.part`，只有内容长度校验通过后才原子替换正式文件。认证失败、HTTP 4xx、重定向、白名单或数据格式错误不属于网络重试。

- 尚无 `comparison_key` 且确认没有任何上游 `message_id`：重新运行该 runtime 记录的 `worker`，不能 `resume`。
- 已有 `comparison_key`：使用 `asgard_worker.py resume --record ... --settings ...`，只收集既有卡片、重新分析并继续报告步骤，不得重发 `fsdlog`。
- 已有 `report_url`：恢复时复用原报告，只补未完成的协作者管理收紧、群查看、所有权转移，以及新任务记录明确要求的 L3 密级或回传步骤，不得新建第二份报告；历史记录未带 `report_secure_label_required=true` 时不得补设密级。
- `status=completed`：内部复用已有报告，不再执行任何外部写操作；本轮已确认报告送达时不追加相同终答，用户另行明确索要链接时再正常回复。旧记录仅有 URL 而缺少交付证据时，先核对，不能据此宣称已送达或自动补发。

收卡时先验证归属再处理成功或失败：其他请求的显式 reply 不允许通过文本匹配回流，本时段派发前的旧卡片、冲突 VIN/时段、多份不同 URL 及不完整消息列表均不选用。同群本地已知同 VIN/时窗多任务时只接明确 reply；旧时延保留单任务无关联卡片兼容，人工或其他服务的相同时窗派发仍需上游提供明确关联才能完全证明归属。New 继续严格关联，不受此兼容影响。

需要诊断或恢复阿斯加德入口时，先读 [上游接入与故障恢复](references/upstream-integration.md)。

## 直接 Sheets 运行

在本 Skill 根目录运行 `scripts/ad_p99.py`，并显式提供 Baseline/替换后 URL、各自包含 VIN/起止时间的 metadata、读取身份和独立输出目录。性能日志查询使用受保护的配置；不要将凭据、原始单元格或临时下载链接写入聊天。

综合报告固定展示六组指标：`P99 Infer Delay`、`Frame Rate`、`Sys CPU Usage`、`PCM P99 Infer Delay`、`PCM Frame Rate`、`PCM CPU Usage`，每组均含 `base/PCM更新`。后四项来自两个任务时间窗各自的 `log_fsdA_service`；其中 PCM P99 必须按 PID、Pre/Post 帧关系与 ctx 验证计算，PCM Frame Rate 必须按完整十帧同状态窗口归属。解析严格使用 `[start, end)`，保留有效样本数和丢弃诊断，不得用最近状态或单模型推理耗时替代。
