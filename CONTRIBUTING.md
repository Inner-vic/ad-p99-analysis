# 维护与提交

本仓库只维护时延分析。走查和 Uroad 是独立 GitLab 项目，不将其运行数据或完整服务复制进来。业务生产模块有 SOURCE-PROVENANCE；修改代码前审查行为与调用边界，更新来源说明、固定依赖、测试和 manifest。

运行 `python tools/update_manifests.py`、`python tools/check_repository.py`、`python tools/run_tests.py`；manifest 更新必须发生在代码审查后，不能用它掩盖非预期文件变更。新依赖固定版本并通过组织制品源准备。CI 环境问题与测试失败分开记录。

凭据、真实 VIN/消息、原始日志、业务报告、cache、.venv、历史备份、安装器和 wheel 不提交。合成输入保留语义一致。线上权限和真实 Bot 回传需要单独获准验收，离线测试不冒充线上验证。默认分支 master；更新前 fetch 远端历史，不将本地导出历史强推覆盖网页提交。
