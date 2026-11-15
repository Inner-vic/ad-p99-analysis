"""Cloud preflight: local dependencies first, optional explicit metadata-only access check."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ad_p99 import InputError, compact
from lark_source import LarkReader, cli_executable, require_complete, sheet_title


def check_environment(executable=None):
    checks = []
    checks.append({"check": "python", "ok": sys.version_info >= (3, 9),
                   "version": ".".join(map(str, sys.version_info[:3]))})
    try:
        import openpyxl
        checks.append({"check": "openpyxl", "ok": openpyxl.__version__ == "3.1.5",
                       "version": openpyxl.__version__})
    except ImportError:
        checks.append({"check": "openpyxl", "ok": False, "action": "由部署人员按 requirements.txt 准备依赖"})
    try:
        import zstandard
        version = getattr(zstandard, "__version__", None)
        checks.append({"check": "zstandard", "ok": version == "0.25.0", "version": version})
    except ImportError:
        checks.append({"check": "zstandard", "ok": False, "action": "由部署人员按 requirements.txt 准备依赖"})
    try:
        binary = cli_executable(executable)
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        response = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                  encoding="utf-8", timeout=15, env=env,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        match = re.search(r"\b\d+\.\d+\.\d+\b", response.stdout)
        checks.append({"check": "lark_cli", "ok": response.returncode == 0 and match is not None,
                       "version": match.group(0) if match else None})
    except (InputError, OSError, subprocess.TimeoutExpired):
        checks.append({"check": "lark_cli", "ok": False, "action": "由部署人员安装公司批准的 CLI 或指定路径"})
    return {"status": "ok" if all(item["ok"] for item in checks) else "failed",
            "checks": checks, "document_access": "not_checked"}


def check_access(url, identity, executable=None, reader=None):
    reader = reader or LarkReader(url, identity=identity, executable=executable)
    revision = reader.call("+revision-get").get("revision")
    if type(revision) not in (str, int) or revision == "":
        raise InputError("未取得有效版本号；请按实际 CLI 版本核对返回结构。")
    info = reader.call("+workbook-info")
    require_complete(info)
    if info.get("warning_message"):
        raise InputError("工作簿清单返回警告，需在云端核对。")
    sheets = info.get("sheets")
    if sheets is None and isinstance(info.get("workbook"), dict):
        sheets = info["workbook"].get("sheets")
    if not isinstance(sheets, list) or not all(isinstance(item, dict) for item in sheets):
        raise InputError("工作簿清单结构不受支持。")
    counts = {name: sum(compact(sheet_title(item)) == name for item in sheets)
              for name in ("vla", "vlaparking")}
    return {"status": "ok" if all(count == 1 for count in counts.values()) else "failed",
            "identity": identity, "target_sheet_counts": counts, "document_access": "metadata_readable",
            "cell_access": "not_checked"}


def check_log_source_config(settings_path=None):
    from cloud_log_client import environment_config, load_settings
    load_settings(Path(settings_path) if settings_path else None)
    configured = []
    for environment in ("prod", "test"):
        config = environment_config(environment)
        configured.append({
            "environment": environment,
            "request_host_configured": bool(config["origin"]),
            "https_download_allowlist_count": len(config["trustedDownloadHosts"]["https"]),
        })
    return {"status": "ok", "environments": configured, "username_configured": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description="云端预检；默认不访问飞书文档、不登录、不下载。")
    parser.add_argument("--lark-cli")
    parser.add_argument("--sheet-url", help="仅在公司云电脑提供：核对工作表清单，不读取单元格")
    parser.add_argument("--identity", choices=("user", "bot"))
    parser.add_argument("--check-log-source-config", action="store_true",
                        help="检查 AD 的 settings.json、车云端点及下载白名单；不发起网络请求")
    parser.add_argument("--settings", type=Path, help="AD Skill 本地 settings.json 路径")
    args = parser.parse_args(argv)
    if args.sheet_url and not args.identity:
        parser.error("访问检查必须显式指定 --identity。")
    result = check_environment(args.lark_cli)
    if result["status"] == "ok" and args.check_log_source_config:
        try:
            result["log_source"] = check_log_source_config(args.settings)
        except (OSError, RuntimeError, ValueError) as exc:
            result.update(status="failed", log_source={"status": "failed"}, error=str(exc))
    if result["status"] == "ok" and args.sheet_url:
        try:
            result["access"] = check_access(args.sheet_url, args.identity, args.lark_cli)
            result["document_access"] = result["access"]["document_access"]
            result["status"] = result["access"]["status"]
        except InputError as exc:
            result.update(status="failed", document_access="failed", error=str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
