# 阿斯加德上游接入与故障恢复

本说明只在阿斯加德自动入口的部署、诊断或恢复时读取。普通双 Sheets 对比不需要加载。

## 已验证的完整链路

1. `chassis-business-routing` 将同一 VIN 的两个不重叠时段规范化为 Baseline 与替换后。
2. `asgard_worker.py start` 以来源 `message_id` 建立幂等 runtime 记录，并在任何派发前检查两种身份：
   - `lark_profile`：固定开发者用户 OAuth，只用于读取来源会话、向阿斯加德发送版本查询与两条独立 `fsdlog`、读取结果表和设置 L3；来源群内任意用户均可触发任务，该身份可以与请求人不同。
   - `report_lark_profile`：独立 bot-only profile，只用于创建报告、设置协作者和回传链接。
3. 三条上游消息都记录真实 `message_id`。结果只能在来源群内按阿斯加德身份、VIN、精确时段和派发时间边界关联；卡片到达顺序不决定角色。
4. 两张结果表收齐后只读提取 AD 指标，并从两个时段各自的 `log_fsdA_service` 提取系统 CPU、PCM P99、PCM 帧率和 PCM CPU。
5. 车辆标识来自本任务版本查询话题内“相关制品信息”首项。Bot 在配置文件夹中创建报告，将协作者管理收紧为仅 `full_access`，授予来源群 `view`，保持原目录并把所有权转给原请求者，Bot 保留 `full_access`。新任务记录带 `report_secure_label_required=true`；固定开发者与所有者不同时，Bot 仅向记录中已固定的 `dispatch_user_open_id` 临时授予 `full_access`，由该用户设置企业密级 `L3` 后立即撤销直接权限，再由 Bot 回传原话题。临时授权、L3 或撤销任一步未成功都不得交付；恢复时根据逐步状态补齐，不重复创建报告。上线该规则前未带标记的历史报告不自动追补密级。其他人看到报告后可发起飞书原生编辑权限申请，由作为所有者的原请求者后续审核；创建时不预授权未知申请人。

密级增量授权只申请 `docs:secure_label:readonly` 与 `docs:secure_label:write_only`，显式使用用户 profile 且不得带 `--recommend`，避免企业禁用的无关推荐权限导致整次授权失败。企业实际标签可为“敏感（L3）”等本地化名称；按独立等级代码唯一匹配，不能用模糊的第一个结果代替。

用户身份与 Bot 身份可以并存，但必须分属两个显式 profile。不要把 Bot profile 登录为用户，也不要让用户 profile 代替报告所有者。

## 授权判定

- 始终传 `--profile`；不得读取、登录或覆盖默认 profile。
- 固定开发者 `lark_profile` 必须使用 `strict-mode user`，所有上游与密级命令显式传 `--as user`；`report_lark_profile` 继续保持 bot-only，不得修改其严格模式。
- 本地状态显示正确 openId、token 有效且距过期超过 15 分钟时，不执行联网验证。
- 临近过期或状态不完整时执行 `auth status --profile <user-profile> --verify --json`。首次状态异常会短暂等待后复核一次，避免把瞬时 CLI 状态误报为重新授权。
- 首次预检得到的开发者 openId 写入任务记录；worker 或安全重试发现同一 profile 已变成其他 openId 时立即拒绝，不能在任务中途切换派发身份。
- 连续验证后仍 missing/invalid 才要求该开发者重新 OAuth。`20140: The auth method is not supported` 属于应用配置问题，应由管理员检查 OAuth/Device Flow、App ID/Secret、已发布权限和企业安装状态。
- Bot 不需要用户 OAuth；其不可用时必须在派发前失败。

不要把网页登录受条件访问拦截等同于现有 token 已失效。先检查 CLI 的显式 profile 状态；只有确需 OAuth 时才让本人操作授权页面，且不得把 token、App Secret 或 device code 写入聊天。

## 下载可靠性

`cloud_log_client.download_to_file` 对瞬时网络故障最多尝试三次，每次都从头下载到同目录 `.part` 文件。以下错误可重试：

- `http.client.IncompleteRead`
- socket timeout
- connection reset / broken pipe
- `URLError` 映射的网络连接失败
- 响应正常结束但实际字节数与 `Content-Length` 不一致

临时文件在每次失败后删除，只有完整文件完成 `fsync` 后才原子替换目标。认证失败、HTTP 4xx、重定向、下载 host 不在白名单、超限或无效响应不重试。三次仍失败时保留 runtime 失败状态，后续使用 `resume`，不重新派发上游任务。

## 幂等恢复矩阵

| runtime / job 状态 | 恢复动作 | 禁止事项 |
|---|---|---|
| 无 `comparison_key`，且确认没有上游 `message_id` | 对原 runtime 记录重新运行 `worker` | 不使用 `resume`，不新建来源消息记录 |
| 已有任一上游 `message_id` 但状态不完整 | 先检查 job 状态；无法证明安全时停止并人工核对 | 不整单重派 |
| 已有 `comparison_key`，报告尚未创建 | 对原 runtime 记录运行 `resume` | 不再发送版本查询或 `fsdlog` |
| 已有 `report_url`，权限或回传未完成 | 运行 `resume`，复用原报告并补齐权限收紧、群查看、所有权转移；仅当记录带 `report_secure_label_required=true` 时补 L3，然后回传 | 不创建第二份报告，不预授权未知申请人，不修改无 L3 标记的历史报告 |
| `status=completed` | 内部复用已有 `report_url`；按对外会话交互判断是否需要用户可见答复 | 不做外部写操作，不复述本轮已成功投递的报告；仅 URL 不作为送达证明 |

示例恢复命令必须显式提供原记录与受保护配置：

```powershell
python .agents/skills/ad-p99-analysis/scripts/asgard_worker.py resume `
  --record '<原 runtime JSON>' `
  --settings '<受保护的 ad-p99-asgard.settings.json>'
```

运行前检查 record 的 `source_message_id`、`sender_id`、`source_chat_id` 和 `comparison_key` 与当前请求一致。不得按目录修改时间选择“最近”的记录。

## 验收

完成修复后至少验证：

1. AD P99 全量单元测试通过。
2. runtime 最终为 `completed`，有且只有一个 `report_url`。
3. 上游 job 的三类消息 ID 没有因恢复而增加。
4. 报告车辆标识来自版本卡片，不是 VIN 后四位。
5. `share_entity=only_full_access`，来源群具有 `view`，原请求者是所有者，Bot 保留 `full_access`，报告密级为 `L3`，链接由 Bot 回传原话题。

任何真实 token、原始单元格、临时下载 URL 和内部凭据都不得写入测试输出、文档或聊天。
