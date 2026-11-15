"""Background Asgard bridge for the CC Connect AD P99 skill.

Uses a pre-authorized Feishu developer identity only for the upstream Asgard
conversation.  Any member of the source group may request a task; the current
app bot sends the completion notice back to the originating chat.  A source
message id is the idempotency boundary.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(WORKSPACE_ROOT / "deployment"))

from ad_p99 import InputError, parse_comparison_request
from lark_auth_lock import LarkAuthLockTimeout, lark_auth_lock
from asgard_bridge import dispatch
from half_auto import attach_result, load_job, run_job, write_json
from feishu_report import build_report_document
from performance_comparison import load_pair_states

UROAD_CONTEXT_SCRIPTS = HERE.parents[1] / "uroad-cloud-report" / "scripts"
sys.path.insert(0, str(UROAD_CONTEXT_SCRIPTS))
from ccconnect_request_context import RequestContextError, followup_identity_from_context

SHEETS_URL = re.compile(r"https://[^\s<>\"')\]]+\.feishu\.cn/sheets/[A-Za-z0-9]+(?:\?[^\s<>\"')\]]*)?", re.I)
ARTIFACT_INFO = re.compile(r"(?:\*\*)?相关制品信息\s*[:：]\s*(?:\*\*)?\s*([^\n<]+)", re.I)
SAFE_VEHICLE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,31}")
AUTH_REFRESH_MARGIN_SECONDS = 15 * 60
AUTH_VERIFY_ATTEMPTS = 2
AUTH_VERIFY_RETRY_DELAY_SECONDS = 1.0
SECURE_LABEL_SCOPES = frozenset({
    "docs:secure_label:readonly",
    "docs:secure_label:write_only",
})


class UserAuthorizationError(InputError):
    """Raised when a Feishu user token must be refreshed or re-authorized."""


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(4):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 3:
                raise
            time.sleep(0.05 * 2**attempt)


def load_settings(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = (
        "asgard_open_id", "state_dir", "runtime_state_dir", "lark_cli",
        "lark_profile", "report_lark_profile",
    )
    missing = [key for key in required if not isinstance(data.get(key), str) or not data[key].strip()]
    if missing:
        raise InputError("阿斯加德桥接配置缺少: " + ", ".join(missing))
    executable = str(data["lark_cli"]).strip()
    if not Path(executable).is_file() and not shutil.which(executable):
        raise InputError("阿斯加德桥接的 lark_cli 不可执行；请修正本机 CLI 路径后再提交任务。")
    if data["lark_profile"].strip() == data["report_lark_profile"].strip():
        raise InputError("报告必须使用独立的 bot-only report_lark_profile，不能复用阿斯加德用户 profile。")
    if data.get("project_isolation_required") is True:
        project_id = str(data.get("project_id") or "").strip()
        lark_home = str(data.get("lark_home") or "").strip()
        if not project_id or not lark_home:
            raise InputError("项目隔离已启用，但缺少 project_id 或 lark_home。")
        home_path = Path(lark_home).expanduser()
        if not home_path.is_absolute():
            raise InputError("项目隔离的 lark_home 必须是绝对路径。")
        if home_path.resolve() == Path.home().resolve():
            raise InputError("项目隔离的 lark_home 不能复用当前 Windows 用户主目录。")
        if not (home_path / ".lark-cli" / "config.json").is_file():
            raise InputError("项目隔离的 lark_home 尚未初始化 lark-cli 配置。")
        prefix = project_id + "-"
        profiles = (str(data["lark_profile"]).strip(), str(data["report_lark_profile"]).strip())
        if any(not profile.startswith(prefix) for profile in profiles):
            raise InputError("项目隔离的飞书 profile 必须使用 project_id 命名空间。")
    return data


def lark_environment(settings: dict) -> dict[str, str]:
    """Build a subprocess environment pinned to this project's lark-cli home."""
    environment = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    lark_home = str(settings.get("lark_home") or "").strip()
    if lark_home:
        resolved = str(Path(lark_home).expanduser().resolve())
        environment["USERPROFILE"] = resolved
        environment["HOME"] = resolved
    return environment


def activate_lark_environment(settings: dict) -> None:
    """Pin downstream helpers that invoke lark-cli through inherited env vars."""
    lark_home = str(settings.get("lark_home") or "").strip()
    if not lark_home:
        return
    resolved = str(Path(lark_home).expanduser().resolve())
    os.environ["USERPROFILE"] = resolved
    os.environ["HOME"] = resolved


def run_lark(settings: dict, args: list[str], *, timeout: int = 90, profile: str | None = None) -> dict:
    environment = lark_environment(settings)
    command = [settings["lark_cli"], *args]
    selected_profile = str(profile if profile is not None else settings.get("lark_profile") or "").strip()
    if not selected_profile:
        raise InputError("飞书调用必须显式指定 profile，禁止使用默认身份。")
    command.extend(["--profile", selected_profile])
    command.extend(["--format", "json"])
    try:
        with lark_auth_lock(settings, timeout=min(timeout, 60)):
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=environment, cwd=str(WORKSPACE_ROOT))
    except LarkAuthLockTimeout as exc:
        raise InputError(str(exc)) from None
    raw = completed.stdout or completed.stderr
    try:
        payload = json.loads(raw)
    except ValueError:
        raise InputError("飞书命令未返回有效结果。") from None
    if not isinstance(payload, dict):
        raise InputError("飞书命令未返回有效结果。")
    if completed.returncode or payload.get("ok") is not True:
        error = payload.get("error", {})
        message = str(error.get("message") or error.get("type") or "unknown")
        if _authorization_failure(payload) == "reauthorize":
            raise UserAuthorizationError("飞书调用失败: need_user_authorization")
        raise InputError("飞书调用失败: " + message)
    return payload


def _profile_auth_status(settings: dict, profile: str, *, verify: bool = False,
                         timeout: int = 30) -> dict:
    """Return auth status for one explicit profile without changing the active profile."""
    selected_profile = str(profile or "").strip()
    if not selected_profile:
        raise InputError("飞书授权状态检查必须显式指定 profile。")
    command = [settings["lark_cli"], "auth", "status", "--profile", selected_profile]
    if verify:
        command.append("--verify")
    command.append("--json")
    environment = lark_environment(settings)
    try:
        with lark_auth_lock(settings, timeout=min(timeout, 60)):
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                env=environment, cwd=str(WORKSPACE_ROOT),
            )
    except LarkAuthLockTimeout as exc:
        raise InputError(str(exc)) from None
    raw = completed.stdout or completed.stderr
    try:
        payload = json.loads(raw)
    except ValueError:
        raise InputError("飞书授权状态未返回有效结果。") from None
    if not isinstance(payload, dict):
        raise InputError("飞书授权状态未返回有效结果。")
    failure = _authorization_failure(payload)
    if failure == "administrator":
        raise InputError("飞书授权方式不受支持（20140），请应用管理员检查 OAuth 配置。")
    if completed.returncode or payload.get("ok") is False:
        if failure == "reauthorize":
            raise UserAuthorizationError("飞书用户刷新授权不可用，需要本人重新完成 OAuth 授权。")
        raise InputError("飞书授权状态校验暂未完成，请稍后重试或检查网络。")
    return payload


def auth_status(settings: dict, *, verify: bool = False, timeout: int = 30) -> dict:
    """Read one explicit profile; only ``verify=True`` may refresh its token."""
    profile = str(settings.get("lark_profile") or "").strip()
    if not profile:
        raise InputError("阿斯加德桥接必须显式配置 lark_profile，禁止使用默认身份。")
    return _profile_auth_status(settings, profile, verify=verify, timeout=timeout)


def ensure_expected_app(settings: dict, payload: dict) -> None:
    expected = str(settings.get("expected_lark_app_id") or "").strip()
    actual = str((payload or {}).get("appId") or "").strip()
    if expected and actual != expected:
        raise InputError("飞书 profile 的 App ID 与项目隔离清单不一致。")


def ensure_report_bot_authorization(settings: dict) -> dict:
    """Require a dedicated bot-only profile for report ownership and delivery."""
    profile = str(settings.get("report_lark_profile") or "").strip()
    user_profile = str(settings.get("lark_profile") or "").strip()
    if not profile:
        raise InputError("报告发布缺少 report_lark_profile。")
    if profile == user_profile:
        raise InputError("报告必须使用独立的 bot-only report_lark_profile，不能复用阿斯加德用户 profile。")
    payload = _profile_auth_status(settings, profile, verify=False)
    ensure_expected_app(settings, payload)
    identities = payload.get("identities") if isinstance(payload, dict) else None
    bot = identities.get("bot") if isinstance(identities, dict) else None
    user = identities.get("user") if isinstance(identities, dict) else None
    if not isinstance(bot, dict) or bot.get("available") is not True:
        raise InputError("报告 Bot 身份不可用；请为 report_lark_profile 配置获准的 App ID/App Secret 与所需权限。")
    if isinstance(user, dict) and user.get("available") is True:
        raise InputError("report_lark_profile 必须是 bot-only；检测到可用用户身份，可能导致额外 full_access 授权。")
    return {"profile": profile, "available": True}


def _authorization_user(payload: dict) -> dict:
    identities = payload.get("identities") if isinstance(payload, dict) else None
    user = identities.get("user") if isinstance(identities, dict) else None
    return user if isinstance(user, dict) else {}


def _authorization_expiry(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _authorization_failure(payload: dict, now: datetime | None = None) -> str | None:
    """Classify only explicit invalid-refresh evidence, never generic missing state."""
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    fields = [error.get(key) for key in ("code", "type", "message")]
    fields.extend(payload.get(key) for key in ("code", "message"))
    user = _authorization_user(payload)
    fields.extend(user.get(key) for key in ("error", "errorCode", "errorMessage"))
    text = " ".join(str(value) for value in fields if isinstance(value, (str, int))).casefold()
    if "20140" in text or "auth method is not supported" in text:
        return "administrator"
    if "need_user_authorization" in text or "invalid_grant" in text:
        return "reauthorize"
    if any(phrase in text for phrase in (
        "refresh_token_expired", "refresh_token_invalid", "invalid refresh token",
        "refresh token expired", "refresh token has expired", "refresh token revoked",
        "authorization revoked", "authorization has been revoked",
    )):
        return "reauthorize"
    refresh_status = str(user.get("refreshTokenStatus") or user.get("refreshStatus") or "").casefold()
    if refresh_status in {"expired", "revoked", "invalid", "used"}:
        return "reauthorize"
    refresh_expiry = _authorization_expiry(user.get("refreshExpiresAt") or user.get("refreshTokenExpiresAt"))
    if refresh_expiry is not None and refresh_expiry <= (now or datetime.now(timezone.utc)):
        return "reauthorize"
    return None


def _authorization_metadata(user: dict) -> dict:
    expiry = _authorization_expiry(user.get("expiresAt"))
    refresh_expiry = _authorization_expiry(user.get("refreshExpiresAt") or user.get("refreshTokenExpiresAt"))
    return {
        "expires_at": expiry.isoformat() if expiry else None,
        "refresh_expires_at": refresh_expiry.isoformat() if refresh_expiry else None,
    }


def ensure_user_authorization(settings: dict, expected_open_id: str | None = None, *, force_verify: bool = False,
                              now: datetime | None = None) -> dict:
    """Validate the explicit dispatch profile and optionally pin its user identity.

    The authenticated user is the configured developer/dispatcher, not the
    requester from the CC Connect context.  ``expected_open_id`` is used only
    to prevent that explicit profile from changing identity between start and
    worker/retry phases.
    """
    current_time = now or datetime.now(timezone.utc)
    configured_expected = str(settings.get("expected_dispatch_open_id") or "").strip()
    expected = str(expected_open_id or configured_expected).strip()
    if expected_open_id and configured_expected and str(expected_open_id).strip() != configured_expected:
        raise InputError("任务固定的派发用户与项目隔离清单不一致。")
    margin = int(settings.get("auth_refresh_margin_seconds", AUTH_REFRESH_MARGIN_SECONDS))
    if margin < 0:
        raise InputError("auth_refresh_margin_seconds 不能为负数。")

    local = None
    reauthorization_required = False
    if not force_verify:
        try:
            local = auth_status(settings, verify=False)
        except UserAuthorizationError:
            reauthorization_required = True
        except (InputError, subprocess.TimeoutExpired, OSError) as exc:
            if isinstance(exc, InputError) and "20140" in str(exc):
                raise
            # A failed local CLI read is diagnostic uncertainty, not evidence
            # that the developer must log in again.
            pass
    if local is not None and (local.get("appId") or not settings.get("expected_lark_app_id")):
        ensure_expected_app(settings, local)
    local_user = _authorization_user(local or {})
    local_open_id = str(local_user.get("openId") or "").strip()
    if expected and local_open_id and local_open_id != expected:
        raise InputError("cc-connect-chassis 派发 profile 的授权用户已变化，拒绝切换开发者身份。")
    expiry = _authorization_expiry(local_user.get("expiresAt"))
    locally_valid = (
        not force_verify
        and local_user.get("available") is True
        and str(local_user.get("tokenStatus") or "").casefold() == "valid"
        and bool(local_open_id)
        and (not expected or local_open_id == expected)
        and expiry is not None
        and (not settings.get("expected_lark_app_id") or local.get("appId"))
        and expiry.astimezone(timezone.utc) > current_time.astimezone(timezone.utc) + timedelta(seconds=margin)
    )
    if locally_valid:
        return {"refreshed": False, "open_id": local_open_id, **_authorization_metadata(local_user)}

    local_failure = _authorization_failure(local or {}, current_time)
    if local_failure == "administrator":
        raise InputError("飞书授权方式不受支持（20140），请应用管理员检查 OAuth 配置。")
    if local_failure == "reauthorize":
        reauthorization_required = True

    attempts = int(settings.get("auth_verify_attempts", AUTH_VERIFY_ATTEMPTS))
    if attempts < 1 or attempts > 3:
        raise InputError("auth_verify_attempts 必须在 1 到 3 之间。")
    user: dict = {}
    for attempt in range(attempts):
        try:
            verified = auth_status(settings, verify=True)
        except UserAuthorizationError:
            reauthorization_required = True
            verified = {}
        except (InputError, subprocess.TimeoutExpired, OSError) as exc:
            if "20140" in str(exc):
                raise
            verified = {}
        if verified.get("appId") or not settings.get("expected_lark_app_id"):
            ensure_expected_app(settings, verified)
        user = _authorization_user(verified)
        failure = _authorization_failure(verified, current_time)
        if failure == "administrator":
            raise InputError("飞书授权方式不受支持（20140），请应用管理员检查 OAuth 配置。")
        if failure == "reauthorize":
            reauthorization_required = True
        open_id = str(user.get("openId") or "").strip()
        if expected and open_id and open_id != expected:
            raise InputError("cc-connect-chassis 派发 profile 的授权用户已变化，拒绝切换开发者身份。")
        verified_expiry = _authorization_expiry(user.get("expiresAt"))
        if (
            bool(open_id)
            and (not expected or open_id == expected)
            and user.get("available") is True
            and user.get("verified") is True
            and str(user.get("tokenStatus") or "").casefold() == "valid"
            and (not settings.get("expected_lark_app_id") or verified.get("appId"))
            and verified_expiry is not None
            and verified_expiry > current_time
        ):
            return {
                "refreshed": True,
                "open_id": open_id,
                **_authorization_metadata(user),
            }
        if attempt + 1 < attempts:
            time.sleep(AUTH_VERIFY_RETRY_DELAY_SECONDS)
    if reauthorization_required:
        raise UserAuthorizationError(
            "飞书用户刷新授权不可用且连续校验失败，请本人重新完成 cc-connect-chassis OAuth 授权。"
        )
    raise InputError("飞书授权连续校验暂未完成；请稍后重试并检查 CLI 或网络，当前不能判定需要重新 OAuth。")


def ensure_secure_label_authorization(settings: dict) -> None:
    """Require only the two user scopes used to resolve and apply L3.

    Do not suggest ``--recommend`` here: enterprise policy can forbid an
    unrelated recommended scope (for example document export), which would
    make the otherwise valid incremental authorization fail.
    """
    payload = auth_status(settings, verify=False)
    user = _authorization_user(payload)
    granted = {item for item in str(user.get("scope") or "").split() if item}
    missing = sorted(SECURE_LABEL_SCOPES - granted)
    if not missing:
        return
    if settings.get("auth_credential_replica_path"):
        raise UserAuthorizationError(
            "新报告必须设置 L3，但用户 profile 缺少密级权限: " + ", ".join(missing)
            + "。请使用项目受控授权入口 deployment/feishu_project_oauth.py --initiate，"
            + "本人完成授权后执行 --complete；项目清单已包含密级最小 scope。"
        )
    profile = str(settings.get("lark_profile") or "").strip()
    raise UserAuthorizationError(
        "新报告必须设置 L3，但用户 profile 缺少密级权限: "
        + ", ".join(missing)
        + "。请仅增量授权这两个 scope（不要使用 --recommend）："
        + f" lark-cli auth login --profile {profile} --scope \""
        + " ".join(sorted(SECURE_LABEL_SCOPES))
        + "\""
    )


def nested_message_id(value) -> str | None:
    if isinstance(value, dict):
        for key in ("message_id", "messageId"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        for child in value.values():
            found = nested_message_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_message_id(child)
            if found:
                return found
    return None


def send_asgard(settings: dict, source_message_id: str, target_chat_id: str, *,
                thread_id: str | None = None, dispatch_user_open_id: str | None = None):
    chat_id = target_chat_id.removeprefix("chat:")
    first_request_pending = True

    def send_fn(command, **kwargs):
        nonlocal first_request_pending
        role = str(kwargs.get("role") or "request")
        attempt = int(kwargs.get("attempt") or 1)
        key = hashlib.sha256(f"{source_message_id}:{role}:{attempt}:{command}".encode()).hexdigest()[:32]
        content = json.dumps({"text": f'<at user_id="{settings["asgard_open_id"]}">阿斯加德</at> {command}'}, ensure_ascii=False)
        if thread_id:
            args = ["im", "+messages-reply", "--as", "user", "--message-id", thread_id,
                    "--reply-in-thread", "--msg-type", "text", "--content", content,
                    "--idempotency-key", key]
        else:
            args = ["im", "+messages-send", "--as", "user", "--chat-id", chat_id,
                    "--msg-type", "text", "--content", content, "--idempotency-key", key]
        sent_at = datetime.now(timezone.utc).isoformat()
        try:
            result = run_lark(settings, args)
        except UserAuthorizationError:
            if not first_request_pending or role != "vehicle_version" or not dispatch_user_open_id:
                raise
            # The first outbound operation has no upstream message_id yet. A
            # forced refresh plus one retry is safe because the same
            # idempotency key is retained.
            ensure_user_authorization(settings, dispatch_user_open_id, force_verify=True)
            result = run_lark(settings, args)
        message_id = nested_message_id(result)
        if not message_id:
            raise InputError("阿斯加德发送结果缺少 message_id。")
        first_request_pending = False
        return {"message_id": message_id, 'sent_at':sent_at}
    return send_fn


def list_messages(settings: dict, chat_id: str, start: str) -> list[dict]:
    """Read the source group, not a topic: Asgard currently emits cards at group level.

    Result ownership is still decided by configured Asgard identity plus the exact
    VIN/time pair and a post-dispatch time boundary; this function never selects a
    generic "latest" card.
    """
    args = ["im", "+chat-messages-list", "--as", "user", "--chat-id", chat_id.removeprefix("chat:"),
            "--start", start, "--order", "asc", "--page-size", "50",
            '--page-all', '--page-limit', '20', "--no-reactions"]
    result = run_lark(settings, args)
    data = result.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list) or any(not isinstance(m, dict) for m in data['messages']):
        raise InputError('阿斯加德结果列表格式异常，不能当作尚无结果。')
    for node in (result, data, data.get('pagination') or {}):
        if isinstance(node, dict):
            if any(key in node and type(node[key]) is not bool for key in ('has_more', 'hasMore', 'truncated')):
                raise InputError('阿斯加德结果分页状态无效，不能确认列表完整。')
            if any(node.get(key) is True for key in ('has_more', 'hasMore', 'truncated')):
                raise InputError('阿斯加德结果列表尚有未读取分页或截断，不能使用不完整列表确认归属。')
    return data['messages']


def _message_instant(value):
    try:
        if value is None or isinstance(value,bool):
            raise ValueError()
        if isinstance(value,(int,float)) or isinstance(value,str) and re.fullmatch(r'\d+(?:\.\d+)?',value):
            stamp = float(value)
            result = datetime.fromtimestamp(stamp/1000 if stamp >= 100_000_000_000 else stamp,timezone.utc)
        else:
            result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError,ValueError,OverflowError,OSError):
        raise InputError('消息或派发时间无效，无法验证本任务边界。') from None


def _message_creation_instant(settings: dict, message: dict, chat_id: str):
    """Resolve CLI display times through the exact message's raw server stamp.

    Minute-only local display strings have neither zone nor sufficient boundary
    precision. Never guess either; read the original message by ID instead.
    """
    value = message.get('create_time')
    try:
        return _message_instant(value)
    except InputError:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?', value):
            raise
    message_id = message.get('message_id')
    if not isinstance(message_id, str) or re.fullmatch(r'om_[A-Za-z0-9_]+', message_id) is None:
        raise InputError('结果消息缺少有效 ID，不能核验原始时间。')
    payload = run_lark(settings, ['api', 'GET', '/open-apis/im/v1/messages/' + message_id,
                                  '--params', json.dumps({'user_id_type': 'open_id'})])
    data = payload.get('data')
    items = data.get('items') if isinstance(data, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise InputError('结果消息原始时间响应异常，不能验证本任务边界。')
    matches = [item for item in items if item.get('message_id') == message_id]
    if (len(matches) != 1 or matches[0].get('chat_id') != chat_id.removeprefix('chat:')
            or matches[0].get('deleted') is True):
        raise InputError('结果消息原始来源不一致，不能验证本任务边界。')
    stamp = matches[0].get('create_time')
    if not isinstance(stamp, str) or re.fullmatch(r'\d{13}', stamp) is None:
        raise InputError('结果消息缺少原始毫秒时间戳，不能验证本任务边界。')
    return _message_instant(stamp)


def find_result(messages: list[dict], metadata: dict, request_message_id: str, asgard_open_id: str, *, sent_at=None, allow_unbound=True, timestamp_reader=None) -> dict | None:
    expected_start = metadata["start"].replace("T", " ")
    expected_end = metadata["end"].replace("T", " ")
    matches = []
    for message in messages:
        if (message.get("sender") or {}).get("open_bot_id") != asgard_open_id:
            continue
        if message.get('deleted') is True:
            continue
        content = str(message.get("content") or "")
        reply_to = str(message.get("reply_to") or message.get("reply_to_message_id") or "")
        correlated = reply_to == request_message_id
        # An explicit reply to ANOTHER request cannot be rescued by matching
        # text; a failure from an unrelated role/window must not fail this one.
        if reply_to and not correlated:
            continue
        exact_window = metadata['vin'] in content and expected_start in content and expected_end in content
        if correlated:
            vins = set(re.findall(r'(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])', content, re.I))
            times = set(re.findall(r'20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', content))
            times = {value.replace('T',' ') for value in times}
            if (any(value.upper() != metadata['vin'].upper() for value in vins)
                    or (times and not times.issubset({expected_start,expected_end}))):
                raise InputError('阿斯加德关联回复中的 VIN 或时段与当前任务冲突。')
        if not correlated and (not allow_unbound or not exact_window):
            continue
        match = SHEETS_URL.search(content)
        if match is None and '分析失败' not in content:
            continue  # A progress acknowledgement is not a result or failure.
        if sent_at is not None:
            created = (timestamp_reader(message) if timestamp_reader is not None
                       else _message_instant(message.get('create_time')))
            if created < _message_instant(sent_at):
                continue
        if "分析失败" in content:
            raise InputError("阿斯加德返回本时段日志分析失败。")
        if match:
            if re.fullmatch(r'om_[A-Za-z0-9_]+', str(message.get('message_id') or '')) is None:
                raise InputError('结果卡片缺少有效消息ID，不能保存本任务接收凭据。')
            matches.append({"sheet_url": match.group(0), "message_id": message.get("message_id")})
    if len({item['sheet_url'] for item in matches}) > 1:
        raise InputError('本时段出现多份不同结果表，无法可靠确认归属。')
    return matches[0] if matches else None


def _unique_runtime_window(settings, job, metadata, chat_id):
    return _unique_runtime_windows(settings, job, {'window': metadata}, chat_id)['window']


def _unique_runtime_windows(settings, job, windows, chat_id):
    """Unthreaded compatibility must not mix two locally known requests.

    This cannot establish ownership of manually submitted or other-service
    requests; only a server-side reply relationship provides that guarantee.
    """
    runtime = Path(settings['runtime_state_dir'])
    unique = dict.fromkeys(windows, True)
    for path in runtime.glob('*.json'):
        if re.fullmatch(r'[0-9a-f]{24}',path.stem) is None:
            continue
        other = json.loads(path.read_text(encoding='utf-8'))
        if (other.get('source_chat_id') != chat_id or other.get('vin') != job.get('vin', next(iter(windows.values()))['vin'])
                or other.get('comparison_key') == job['comparison_key']
                or other.get('status') in ('completed','cancelled')):
            continue
        if other.get('status') == 'failed' and not (other.get('comparison_key') or other.get('dispatch_intents')):
            continue
        from asgard_bridge import parse_commands
        parsed_windows, _ = parse_commands(other.get('commands'))
        for role, metadata in windows.items():
            if any(window['vin'].upper() == metadata['vin'].upper()
                   and window['start'].replace(' ','T') == metadata['start']
                   and window['end'].replace(' ','T') == metadata['end'] for window in parsed_windows):
                unique[role] = False
    return unique


def vehicle_label_from_topic(payload: dict, *, request_message_id: str, expected_vin: str,
                             expected_asgard_id: str) -> dict | None:
    """Extract the report vehicle label only from the verified task topic."""
    messages = (payload.get("data") or {}).get("messages") or []
    roots = [item for item in messages if item.get("message_id") == request_message_id]
    if len(roots) != 1:
        raise InputError("无法在当前任务话题中唯一定位车辆版本查询消息。")
    root = roots[0]
    root_content = str(root.get("content") or "")
    if expected_vin not in root_content or "查询车辆版本" not in root_content or "CCUFSD" not in root_content:
        raise InputError("当前话题中的车辆版本查询与任务 VIN 不匹配。")

    candidates = []
    for reply in root.get("thread_replies") or []:
        sender = reply.get("sender") or {}
        if sender.get("open_bot_id") != expected_asgard_id:
            continue
        match = ARTIFACT_INFO.search(str(reply.get("content") or ""))
        if not match:
            continue
        raw_value = match.group(1).strip()
        label = re.split(r"[,，]", raw_value, maxsplit=1)[0].strip()
        if SAFE_VEHICLE_LABEL.fullmatch(label) is None:
            raise InputError("话题返回的相关制品标识格式无效。")
        candidates.append({
            "label": label,
            "artifact_info": raw_value,
            "response_message_id": reply.get("message_id"),
            "version_request_message_id": request_message_id,
        })
    labels = {item["label"] for item in candidates}
    if len(labels) > 1:
        raise InputError("当前任务话题返回了多个不同的相关制品标识，无法确定报告名称。")
    return candidates[0] if candidates else None


def wait_for_vehicle_label(settings: dict, job: dict, expected_vin: str) -> dict:
    version_request_id = next((
        item.get("message_id")
        for item in job.get("dispatch", {}).get("sent", [])
        if item.get("role") == "vehicle_version" and item.get("message_id")
    ), None)
    if not version_request_id:
        raise InputError("当前任务缺少车辆版本查询消息，不能生成报告名称。")
    timeout = max(1, int(settings.get("vehicle_version_timeout_seconds", 120)))
    interval = max(1, int(settings.get("poll_backoff_seconds", 2)))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = run_lark(settings, [
            "im", "+messages-mget", "--as", "user",
            "--message-ids", str(version_request_id), "--no-reactions",
        ])
        evidence = vehicle_label_from_topic(
            payload,
            request_message_id=str(version_request_id),
            expected_vin=expected_vin,
            expected_asgard_id=settings["asgard_open_id"],
        )
        if evidence:
            return evidence
        time.sleep(interval)
    raise InputError("当前任务话题未返回“相关制品信息”，不能生成报告名称。")


def wait_for_cards(settings: dict, job: dict, chat_id: str, start: str, *, thread_id: str | None = None, on_received=None) -> dict:
    timeout = int(settings.get("card_timeout_seconds", 900))
    interval = max(1, int(settings.get("poll_backoff_seconds", 2)))
    deadline = time.monotonic() + timeout
    sent = {}
    for role in ('baseline','replacement'):
        candidates = [item for item in job.get('dispatch',{}).get('sent',[]) if item.get('role') == role]
        if len(candidates) != 1 or not candidates[0].get('message_id'):
            raise InputError('上游双时段派发不完整或不唯一；保留已发送凭证，不能重派或关联无请求结果。')
        _message_instant(candidates[0].get('sent_at'))
        sent[role] = candidates[0]
    if sent['baseline']['message_id'] == sent['replacement']['message_id']:
        raise InputError('上游双时段派发不完整；保留已发送凭证，不能重派或关联无请求结果。')
    received = dict(job.get("received") or {})
    if all((received.get(role) or {}).get('sheet_url') for role in ('baseline', 'replacement')):
        return received
    # Server create_time is immutable; keep only this task's in-memory lookup
    # results so polling does not repeatedly read the same raw message.
    precise_times = {}
    def timestamp_reader(message):
        key = message.get('message_id')
        if not isinstance(key, str) or re.fullmatch(r'om_[A-Za-z0-9_]+', key) is None:
            return _message_creation_instant(settings, message, chat_id)
        if key not in precise_times:
            precise_times[key] = _message_creation_instant(settings, message, chat_id)
        return precise_times[key]
    while time.monotonic() < deadline:
        messages = list_messages(settings, chat_id, start)
        unique = _unique_runtime_windows(settings, job,
            {role: job[role + '_metadata'] for role in ('baseline', 'replacement')}, chat_id)
        for role in ("baseline", "replacement"):
            if (received.get(role) or {}).get("sheet_url"):
                continue
            metadata = job[f'{role}_metadata']
            result = find_result(messages, metadata, sent[role]['message_id'], settings["asgard_open_id"],
                                 sent_at=sent[role]['sent_at'], allow_unbound=unique[role],
                                 timestamp_reader=timestamp_reader)
            if result:
                received[role] = result
                if on_received is not None:
                    on_received(role, dict(result))
        if all((received.get(role) or {}).get("sheet_url") for role in ("baseline", "replacement")):
            return received
        time.sleep(interval)
    raise InputError("等待阿斯加德两张结果卡片超时。")


def create_report(settings: dict, job: dict, result: dict) -> str:
    report_profile = str(settings["report_lark_profile"]).strip()
    document = build_report_document(result, baseline_sheet_url=job["received"]["baseline"]["sheet_url"],
                                     replacement_sheet_url=job["received"]["replacement"]["sheet_url"])
    runtime = Path(settings["runtime_state_dir"])
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-8", dir=runtime, delete=False) as stream:
        stream.write(document["xml"])
        content_path = Path(stream.name)
    try:
        try:
            content_arg = "@" + content_path.resolve().relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:
            raise InputError("报告临时文件必须位于工作区内。") from None
        args = ["docs", "+create", "--as", "bot", "--doc-format", "xml", "--content", content_arg]
        parent_token = str(settings.get("report_parent_token") or "").strip()
        if parent_token:
            args.extend(["--parent-token", parent_token])
        else:
            args.extend(["--parent-position", str(settings.get("report_parent_position", "my_library"))])
        response = run_lark(settings, args, timeout=120, profile=report_profile)
    finally:
        content_path.unlink(missing_ok=True)
    data = response.get("data") or {}
    candidates = re.findall(r"https://[^\s\"']+\.feishu\.cn/(?:docx|docs)/[A-Za-z0-9_-]+", json.dumps(data, ensure_ascii=False))
    if candidates:
        return candidates[0]
    token = next((data.get(key) for key in ("document_id", "doc_token", "token") if isinstance(data.get(key), str) and data.get(key)), None)
    if not token:
        raise InputError("报告创建结果缺少文档链接。")
    return f"https://li.feishu.cn/docx/{token}"


def deliver(settings: dict, record: dict, report_url: str) -> dict:
    report_profile = str(settings["report_lark_profile"]).strip()
    key = hashlib.sha256(f"{record['source_message_id']}:report".encode()).hexdigest()[:32]
    # Native rich text keeps rows and link labels intact without exposing
    # literal backslash-n escapes or depending on Markdown rendering.
    content = json.dumps({"zh_cn": {
        "title": "时延分析已完成",
        "content": [
            [{"tag": "text", "text": f"VIN：{record['vin']}"}],
            [{"tag": "a", "text": "查看飞书报告", "href": report_url}],
        ],
    }}, ensure_ascii=False)
    thread_id = record.get("source_thread_id")
    if thread_id:
        args = ["im", "+messages-reply", "--as", "bot", "--message-id", str(thread_id),
                "--reply-in-thread", "--msg-type", "post", "--content", content, "--idempotency-key", key]
    else:
        chat_id = record["source_chat_id"].removeprefix("chat:")
        args = ["im", "+messages-send", "--as", "bot", "--chat-id", chat_id, "--msg-type", "post", "--content", content,
                "--idempotency-key", key]
    payload = run_lark(settings, args, profile=report_profile)
    data = payload.get('data') if isinstance(payload, dict) else None
    candidates = []
    if isinstance(data,dict):
        if data.get('message_id') is not None:
            candidates.append(data)
        if isinstance(data.get('message'),dict):
            candidates.append(data['message'])
        if isinstance(data.get('messages'),list):
            candidates.extend(data['messages'])
    ids = {item.get('message_id') for item in candidates if isinstance(item,dict)}
    message_id = next(iter(ids)) if len(ids) == 1 else None
    excluded = {record['source_message_id'],*(item.get('message_id') for item in (record.get('dispatch_intents') or {}).values())}
    if (not isinstance(message_id, str) or re.fullmatch(r'om_[A-Za-z0-9_]+', message_id) is None
            or message_id in excluded or not isinstance(payload,dict) or payload.get('ok') is not True
            or payload.get('identity') not in (None, 'bot')
            or any(not isinstance(item,dict) or item.get('chat_id') and item['chat_id'].removeprefix('chat:') != str(record.get('source_chat_id') or '').removeprefix('chat:') for item in candidates)):
        raise InputError('报告发送缺少有效 Bot 消息回执，不能确认送达。')
    return {'message_id': message_id, 'source_message_id': record['source_message_id'],
            'source_chat_id': record.get('source_chat_id'), 'report_url': report_url,
            'profile': report_profile, 'identity': 'bot',
            'sent_at': datetime.now(timezone.utc).isoformat()}

def grant_source_chat_view(settings: dict, report_url: str, source_chat_id: str) -> None:
    """Give every member of the originating chat read-only access."""
    chat_id = source_chat_id.removeprefix("chat:")
    if not chat_id:
        raise InputError("报告分享缺少来源 chat_id。")
    run_lark(settings, ["drive", "+member-add", "--as", "bot", "--token", report_url,
                        "--member-id", chat_id, "--member-type", "openchat", "--perm", "view", "--yes"],
             profile=str(settings["report_lark_profile"]).strip())


def restrict_collaborator_management(settings: dict, report_url: str) -> None:
    """Allow only full-access members to add or manage collaborators."""
    match = re.search(r"/docx/([A-Za-z0-9_-]+)", str(report_url))
    if not match:
        raise InputError("报告权限收紧缺少有效的 docx 链接。")
    run_lark(
        settings,
        [
            "drive", "permission.public", "patch",
            "--token", match.group(1), "--type", "docx",
            "--data", json.dumps({"share_entity": "only_full_access"}, ensure_ascii=False),
            "--yes",
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )


def transfer_report_owner(settings: dict, report_url: str, requester_open_id: str) -> None:
    """Transfer ownership to the requester while the report Bot keeps full access."""
    open_id = str(requester_open_id or "").strip()
    if not open_id.startswith("ou_"):
        raise InputError("报告所有权转移缺少有效的请求者 open_id。")
    match = re.search(r"/docx/([A-Za-z0-9_-]+)", str(report_url))
    if not match:
        raise InputError("报告所有权转移缺少有效的 docx 链接。")
    run_lark(
        settings,
        [
            "drive", "permission.members", "transfer_owner",
            "--token", match.group(1), "--type", "docx",
            "--old-owner-perm", "full_access", "--stay-put",
            "--data", json.dumps({"member_id": open_id, "member_type": "openid"}, ensure_ascii=False),
            "--yes",
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )


def grant_secure_label_operator(settings: dict, report_url: str, operator_open_id: str) -> None:
    """Temporarily give the pinned user identity enough access to set L3."""
    open_id = str(operator_open_id or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", open_id):
        raise InputError("报告密级操作缺少有效的固定开发者 open_id。")
    run_lark(
        settings,
        [
            "drive", "+member-add", "--as", "bot", "--token", report_url,
            "--member-id", open_id, "--member-type", "openid",
            "--perm", "full_access", "--yes",
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )


def revoke_secure_label_operator(settings: dict, report_url: str, operator_open_id: str) -> None:
    """Remove the pinned user's temporary direct permission after L3 is set."""
    open_id = str(operator_open_id or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", open_id):
        raise InputError("报告密级操作缺少有效的固定开发者 open_id。")
    match = re.search(r"/docx/([A-Za-z0-9_-]+)", str(report_url))
    if not match:
        raise InputError("撤销报告密级临时权限时缺少有效的 docx 链接。")
    run_lark(
        settings,
        [
            "api", "DELETE",
            f"/open-apis/drive/v1/permissions/{match.group(1)}/members/{open_id}",
            "--params", json.dumps({"type": "docx", "member_type": "openid"}),
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )


def _secure_label_candidates(value) -> list[dict]:
    candidates: list[dict] = []
    if isinstance(value, dict):
        label_id = value.get("id") or value.get("label_id") or value.get("labelId")
        name = value.get("name") or value.get("label_name") or value.get("display_name")
        if label_id is not None and isinstance(name, str) and name.strip():
            candidates.append({"id": str(label_id), "name": name.strip()})
        for child in value.values():
            candidates.extend(_secure_label_candidates(child))
    elif isinstance(value, list):
        for child in value:
            candidates.extend(_secure_label_candidates(child))
    return candidates


def _secure_label_name_matches(name: str, expected: str) -> bool:
    actual = str(name or "").strip()
    configured = str(expected or "").strip()
    if actual.casefold() == configured.casefold():
        return True
    if not re.fullmatch(r"L\d+", configured, re.I):
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(configured)}(?![A-Za-z0-9])", actual, re.I) is not None


def resolve_report_secure_label(settings: dict) -> dict:
    """Resolve the tenant-specific ID for the configured report label."""
    expected_name = str(settings.get("report_secure_label_name") or "L3").strip()
    if not expected_name:
        raise InputError("报告密级名称不能为空。")
    response = run_lark(
        settings,
        ["drive", "+secure-label-list", "--as", "user", "--page-size", "10", "--lang", "zh"],
        profile=str(settings["lark_profile"]).strip(),
    )
    candidates = _secure_label_candidates(response.get("data") or response)
    matches = [item for item in candidates if _secure_label_name_matches(item["name"], expected_name)]
    unique = {(item["id"], item["name"]) for item in matches}
    if len(unique) != 1:
        raise InputError(f"企业密级中无法唯一确定 {expected_name}，请检查密级配置。")
    label_id, name = next(iter(unique))
    return {"id": label_id, "name": name}


def apply_report_secure_label(settings: dict, report_url: str, label: dict) -> None:
    label_id = str((label or {}).get("id") or "").strip()
    label_name = str((label or {}).get("name") or "").strip()
    if not label_id or not _secure_label_name_matches(
        label_name, str(settings.get("report_secure_label_name") or "L3")
    ):
        raise InputError("报告密级信息无效。")
    run_lark(
        settings,
        ["drive", "+secure-label-update", "--as", "user", "--token", report_url, "--label-id", label_id],
        profile=str(settings["lark_profile"]).strip(),
    )


def report_requires_secure_label(record: dict) -> bool:
    """Enforce L3 only for records created after the policy was introduced."""
    return record.get("report_secure_label_required") is True


def configure_report_secure_label(
    settings: dict,
    record_path: Path,
    record: dict,
    report_url: str,
    permissions: dict,
) -> None:
    """Set L3 with least-privilege temporary access when owner and operator differ."""
    if not report_requires_secure_label(record):
        return

    operator_open_id = str(record.get("dispatch_user_open_id") or "").strip()
    owner_open_id = str(record.get("sender_id") or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", operator_open_id):
        raise InputError("任务记录缺少有效的固定开发者 open_id，不能设置报告密级。")
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", owner_open_id):
        raise InputError("任务记录缺少有效的报告所有者 open_id，不能设置报告密级。")

    temporary_access = operator_open_id != owner_open_id
    access = dict(permissions.get("secure_label_operator") or {})
    recorded_operator = str(access.get("open_id") or "").strip()
    if recorded_operator and recorded_operator != operator_open_id:
        raise InputError("报告密级临时权限身份与任务固定派发身份不一致。")

    def persist() -> None:
        record["report_permissions"] = permissions
        atomic_json(record_path, record)

    if permissions.get("secure_label"):
        if temporary_access and access.get("active") is True:
            revoke_secure_label_operator(settings, report_url, operator_open_id)
            access.update({"active": False, "revoked_at": datetime.now(timezone.utc).isoformat()})
            permissions["secure_label_operator"] = access
            persist()
        return

    if temporary_access and access.get("active") is not True:
        grant_secure_label_operator(settings, report_url, operator_open_id)
        access = {
            "open_id": operator_open_id,
            "perm": "full_access",
            "temporary": True,
            "active": True,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        }
        permissions["secure_label_operator"] = access
        persist()

    try:
        label = record.get("report_secure_label") or resolve_report_secure_label(settings)
        try:
            apply_report_secure_label(settings, report_url, label)
        except InputError as exc:
            if not temporary_access or "lacks permission" not in str(exc).casefold():
                raise
            time.sleep(1.0)
            apply_report_secure_label(settings, report_url, label)
        permissions["secure_label"] = {
            "id": label["id"],
            "name": label["name"],
            "configured_at": datetime.now(timezone.utc).isoformat(),
        }
        record["report_secure_label"] = label
        persist()
    finally:
        if temporary_access and access.get("active") is True:
            revoke_secure_label_operator(settings, report_url, operator_open_id)
            access.update({"active": False, "revoked_at": datetime.now(timezone.utc).isoformat()})
            permissions["secure_label_operator"] = access
            persist()


def validate_pair_snapshot(*states, **kwargs) -> bool:
    """Keep the optional reuse guard isolated from the original late-read path."""
    from performance_log_source import validate_pair_snapshot as validate_snapshot
    return validate_snapshot(*states, **kwargs)


def collect_and_analyze_job(record_path: Path, settings: dict, record: dict, job: dict) -> tuple[dict, dict]:
    """Overlap one task's independent CPU/PCM preparation with card collection.

    No shared cache or background state writes: only compact metrics survive
    the performance loader's existing temporary-directory cleanup. Failure in
    collection still joins the preparation thread so raw logs are not left
    behind by an abandoned worker. Existing download timeouts remain in force.
    """
    comparison_key = record["comparison_key"]
    expected_vin = record["vin"]
    if job.get("comparison_key") != comparison_key or job.get("vin") != expected_vin:
        raise InputError("当前任务与性能准备的 comparison_key/VIN 不一致。")
    dispatch_scope = job.get('dispatch') or {}
    if (dispatch_scope.get('chat_id') != record.get('source_chat_id')
            or dispatch_scope.get('sender_id') != record.get('sender_id')
            or dispatch_scope.get('asgard_id') != settings.get('asgard_open_id')):
        raise InputError('当前任务与上游派发的来源会话、请求人或阿斯加德身份不一致。')
    state_args = {
        "baseline_metadata": copy.deepcopy(job["baseline_metadata"]),
        "replacement_metadata": copy.deepcopy(job["replacement_metadata"]),
        "cloud_env": str(settings.get("cpu_log_cloud_env", "auto")),
        "settings_path": settings.get("ad_p99_settings_path"),
        "skip_cpu": False,
    }
    enabled = settings.get("performance_prefetch_enabled", True)
    if not isinstance(enabled, bool):
        raise InputError("performance_prefetch_enabled 必须为布尔值。")
    requests = {
        role: [item.get("message_id") for item in job.get("dispatch", {}).get("sent", [])
               if item.get("role") == role]
        for role in ("baseline", "replacement")
    }
    can_prepare = enabled and all(
        len(ids) == 1 and isinstance(ids[0], str) and re.fullmatch(r"om_[A-Za-z0-9]+", ids[0])
        for ids in requests.values()
    ) and requests["baseline"][0] != requests["replacement"][0]
    timings = {}
    future = None
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ad-p99-performance") if can_prepare else None

    def prepare_states():
        started = time.perf_counter()
        try:
            return load_pair_states(**copy.deepcopy(state_args))
        finally:
            timings["prepare_seconds"] = round(time.perf_counter() - started, 3)

    def load_prepared_states(**kwargs):
        if kwargs != state_args:
            raise InputError("当前任务时段或性能来源配置发生变化，拒绝复用准备结果。")
        started = time.perf_counter()
        try:
            try:
                states = future.result()
            except Exception:
                states = None
            timings["join_seconds"] = round(time.perf_counter() - started, 3)
            def usable_state(state):
                if (not isinstance(state, dict) or state.get("error")
                        or not isinstance(state.get("metrics"), dict) or not state["metrics"]):
                    return False
                if state.get("status") == "ok":
                    return True
                # Missing driving scenarios are a normal partial report, not
                # a failed fetch. Reuse only with proof all selected files were
                # read; lack of files/source evidence still gets the late read.
                coverage = state.get("coverage") or {}
                source = state.get("source") or {}
                if not isinstance(coverage, dict) or not isinstance(source, dict):
                    return False
                files_read = coverage.get("files_read")
                selected = source.get("selected_files")
                return (state.get("status") == "partial" and source.get("kind") == "log_fsdA_service"
                        and type(files_read) is int and type(selected) is int
                        and files_read == selected and selected > 0)

            usable = isinstance(states, (tuple, list)) and len(states) == 2 and all(map(usable_state, states))
            if usable:
                validation_started = time.perf_counter()
                try:
                    usable = validate_pair_snapshot(*states, **state_args) is True
                except Exception:
                    usable = False
                timings["snapshot_validation_seconds"] = round(time.perf_counter() - validation_started, 3)
            timings["reused"] = usable
            # Early queries may run before delayed uploads are available. Only
            # reuse successful preparation; otherwise perform the original
            # later read once, preserving existing partial/error semantics.
            if usable:
                return states
            late_started = time.perf_counter()
            try:
                return load_pair_states(**kwargs)
            finally:
                timings["late_read_seconds"] = round(time.perf_counter() - late_started, 3)
        finally:
            timings["finish_performance_seconds"] = round(time.perf_counter() - started, 3)

    try:
        if executor is not None:
            future = executor.submit(prepare_states)
        started = time.perf_counter()
        def received_card(role, receipt):
            record.setdefault('result_card_receipts', {})[role] = {
                **receipt, 'source_chat_id': record['source_chat_id'],
                'comparison_key': comparison_key,
                'metadata': dict(job[role + '_metadata']),
            }
            atomic_json(record_path, record)
        received = wait_for_cards(settings, job, record["source_chat_id"], record["created_at"],
                                  on_received=received_card)
        timings["cards_wait_seconds"] = round(time.perf_counter() - started, 3)
        vehicle_artifact = wait_for_vehicle_label(settings, job, expected_vin)
        record["vehicle_artifact"] = vehicle_artifact
        atomic_json(record_path, record)
        for role in ("baseline", "replacement"):
            if not (job.get("received", {}).get(role) or {}).get("sheet_url"):
                attach_result(settings["state_dir"], comparison_key, role=role, sheet_url=received[role]["sheet_url"])
        _, job = load_job(settings["state_dir"], comparison_key)
        if (record.get("comparison_key") != comparison_key or record.get("vin") != expected_vin
                or job.get("comparison_key") != comparison_key or job.get("vin") != expected_vin
                or job.get("baseline_metadata") != state_args["baseline_metadata"]
                or job.get("replacement_metadata") != state_args["replacement_metadata"]):
            raise InputError("当前任务绑定或时段发生变化，拒绝合并准备结果。")

        def read_bound_sheet(url, **kwargs):
            from lark_source import LarkReader, read_online
            reader = LarkReader(url, identity=kwargs["identity"], executable=kwargs["executable"], auth_settings=settings)
            return read_online(url, **kwargs, reader=reader)

        started = time.perf_counter()
        result = run_job(settings["state_dir"], comparison_key, identity=str(settings.get("identity", "user")),
                         executable=settings["lark_cli"], cloud_env=state_args["cloud_env"],
                         settings_path=state_args["settings_path"],
                         vehicle_label=vehicle_artifact["label"], read_fn=read_bound_sheet,
                         state_loader=load_prepared_states if future is not None else None)
        timings["analysis_seconds"] = round(time.perf_counter() - started, 3)
        return result, job
    finally:
        original_error = sys.exc_info()[0] is not None
        if executor is not None:
            # cancel() only affects a queued future, not an active download.
            # Join active preparation to preserve the existing raw-log cleanup.
            if future is not None:
                future.cancel()
            executor.shutdown(wait=True)
        record["performance_preparation"] = {"enabled": can_prepare, **timings}
        try:
            atomic_json(record_path, record)
        except Exception:
            # Optional timing persistence must not hide the collection/analysis
            # failure that the existing worker error handler needs to report.
            if not original_error:
                raise


def finish_dispatched_job(record_path: Path, settings: dict, record: dict) -> int:
    """Collect and publish a job that already has both upstream requests."""
    if record.get('status') == 'completed':
        return 0
    report_bot = ensure_report_bot_authorization(settings)
    profile = str(settings.get("lark_profile") or "").strip()
    if profile:
        os.environ["LARK_CLI_PROFILE"] = profile
    comparison_key = str(record.get("comparison_key") or "").strip()
    if not comparison_key:
        raise InputError("恢复任务缺少 comparison_key，不能安全关联阿斯加德结果。")
    report_url = str(record.get("report_url") or "").strip()
    result_path = Path(record.get('result_path') or record_path.with_suffix('.result.json'))
    if not report_url:
        if record.get('report_create_intent'):
            raise InputError('已有报告创建意图但未确认回执，需核查原创建请求；未创建第二份报告。')
        _, job = load_job(settings["state_dir"], comparison_key)
        result, job = collect_and_analyze_job(record_path, settings, record, job)
        atomic_json(result_path, result)
        record['report_create_intent'] = {'pending':True, 'comparison_key':comparison_key}
        atomic_json(record_path, record)
        report_url = create_report(settings, job, result)
        configured_parent_token = str(settings.get("report_parent_token") or "").strip()
        record.update({
            "status": "granting_report_permissions",
            "report_url": report_url,
            'report_create_intent': {'pending':False, 'comparison_key':comparison_key},
            "report_owner": {
                "identity": "bot",
                "profile": report_bot["profile"],
                "parent_token_configured": bool(configured_parent_token),
                "parent_position": None if configured_parent_token else str(
                    settings.get("report_parent_position", "my_library")
                ),
            },
        })
        atomic_json(record_path, record)

    permissions = dict(record.get("report_permissions") or {})
    if not permissions.get("collaborator_management"):
        restrict_collaborator_management(settings, report_url)
        permissions["collaborator_management"] = {
            "share_entity": "only_full_access",
            "configured_at": datetime.now(timezone.utc).isoformat(),
        }
        record["report_permissions"] = permissions
        atomic_json(record_path, record)
    if not permissions.get("source_chat_view"):
        grant_source_chat_view(settings, report_url, record["source_chat_id"])
        permissions["source_chat_view"] = {
            "member_type": "openchat",
            "member_id": record["source_chat_id"].removeprefix("chat:"),
            "perm": "view",
            "granted_at": datetime.now(timezone.utc).isoformat(),
        }
        record["report_permissions"] = permissions
        atomic_json(record_path, record)
    if not permissions.get("owner_transfer"):
        transfer_report_owner(settings, report_url, record["sender_id"])
        permissions["owner_transfer"] = {
            "new_owner_type": "openid",
            "new_owner_id": record["sender_id"],
            "old_owner_perm": "full_access",
            "stay_put": True,
            "transferred_at": datetime.now(timezone.utc).isoformat(),
        }
        record["report_permissions"] = permissions
        record["report_owner"] = {
            "identity": "user",
            "open_id": record["sender_id"],
            "created_by_profile": report_bot["profile"],
            "creator_retained_perm": "full_access",
        }
        atomic_json(record_path, record)
    configure_report_secure_label(settings, record_path, record, report_url, permissions)

    record["status"] = "delivering_report"
    atomic_json(record_path, record)
    receipt = record.get('delivery_receipt')
    if receipt:
        if (receipt.get('report_url') != report_url
                or receipt.get('source_message_id') != record['source_message_id']
                or receipt.get('source_chat_id') != record['source_chat_id']
                or receipt.get('profile') != settings['report_lark_profile']
                or receipt.get('identity') != 'bot'
                or not re.fullmatch(r'om_[A-Za-z0-9_]+', str(receipt.get('message_id') or ''))):
            raise InputError('已有报告交付凭证与当前任务不一致，未重复发送。')
    else:
        if record.get('delivery_intent'):
            raise InputError('已有报告发送意图但回执未确认，需核查原发送请求；未重复发送。')
        record['delivery_intent'] = {'pending':True, 'report_url':report_url,
                                     'source_message_id':record['source_message_id']}
        atomic_json(record_path, record)
        record['delivery_receipt'] = deliver(settings, record, report_url)
        record['delivery_intent']['pending'] = False
        atomic_json(record_path, record)
    record.update({"status": "completed", "report_url": report_url, "result_path": str(result_path),
                   "completed_at": datetime.now(timezone.utc).isoformat()})
    atomic_json(record_path, record)
    return 0


def _run_worker(record_path: Path, settings: dict) -> int:
    activate_lark_environment(settings)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get('status') == 'completed':
        return 0
    if record.get('comparison_key') or record.get('dispatch_intents'):
        raise InputError('任务已有上游派发证据或未确认意图，禁止重派；请核查精确记录后恢复。')
    # Fail before any upstream message is sent if the bot that must own and
    # deliver the final report is unavailable.
    ensure_report_bot_authorization(settings)
    pinned_dispatch_open_id = str(record.get("dispatch_user_open_id") or "").strip() or None
    authorization = ensure_user_authorization(settings, pinned_dispatch_open_id)
    # A failed record may be retried after a deployment repair. Do not leave
    # obsolete failure evidence visible while the new attempt is active.
    record.pop("error", None)
    record.pop("failed_at", None)
    chat_id = record["source_chat_id"]
    record["status"] = "running"
    record["started_at"] = datetime.now(timezone.utc).isoformat()
    record["authorization_preflight"] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "refreshed": authorization["refreshed"],
        "open_id": authorization["open_id"],
        "expires_at": authorization["expires_at"],
    }
    record["dispatch_user_open_id"] = authorization["open_id"]
    atomic_json(record_path, record)
    upstream_send = send_asgard(settings, record["source_message_id"], chat_id,
                          thread_id=record.get("source_thread_id"),
                          dispatch_user_open_id=authorization["open_id"])
    def send_fn(command, **kwargs):
        role = kwargs.get('role')
        record.setdefault('dispatch_intents', {})[role] = {'pending': True, 'command': command}
        atomic_json(record_path, record)
        result = upstream_send(command, **kwargs)
        record['dispatch_intents'][role].update(pending=False, message_id=result['message_id'])
        atomic_json(record_path, record)
        return result
    def prepared(comparison_key):
        record.update(comparison_key=comparison_key, status='dispatching')
        atomic_json(record_path, record)
    dispatched = dispatch(record["commands"], state_dir=settings["state_dir"], send_fn=send_fn, chat_id=chat_id,
                          sender_id=record["sender_id"], asgard_id=settings["asgard_open_id"], on_prepared=prepared)
    record.update({"comparison_key": dispatched["comparison_key"], "status": "waiting_cards"})
    atomic_json(record_path, record)
    return finish_dispatched_job(record_path, settings, record)


def _resume_worker(record_path: Path, settings: dict) -> int:
    """Resume collection after a listener/deployment failure without re-dispatching."""
    activate_lark_environment(settings)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get('status') == 'completed':
        return 0
    if not record.get("comparison_key"):
        raise InputError("该任务尚未完成上游派发，不能使用无重复派发恢复。")
    record.pop("error", None)
    record.pop("failed_at", None)
    record["status"] = "waiting_cards"
    record["resumed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(record_path, record)
    return finish_dispatched_job(record_path, settings, record)


def _runtime_record_path(record_path, settings):
    path = Path(record_path)
    if (path.is_symlink() or path.suffix != '.json'
            or re.fullmatch(r'[0-9a-f]{24}', path.stem) is None
            or path.resolve().parent != Path(settings['runtime_state_dir']).resolve()):
        raise InputError('任务记录必须来自时延链路的精确 runtime。')
    record = json.loads(path.read_text(encoding='utf-8'))
    expected = hashlib.sha256(str(record.get('source_message_id') or '').encode()).hexdigest()[:24]
    if record.get('job_key') != path.stem or expected != path.stem:
        raise InputError('任务记录与来源消息不一致。')
    return path


@contextmanager
def _worker_lock(path):
    # Persistent inode + OS advisory lock: process death releases ownership.
    with path.with_suffix('.worker.lock').open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InputError('该时延任务已有 worker，未重复执行。') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _execute_worker(record_path, settings_path, *, resume):
    settings = load_settings(settings_path)
    path = _runtime_record_path(record_path, settings)
    with _worker_lock(path):
        try:
            return (_resume_worker if resume else _run_worker)(path, settings)
        except Exception as exc:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('status') != 'completed':
                record.update(status='failed', error=str(exc).strip() or type(exc).__name__,
                              failed_at=datetime.now(timezone.utc).isoformat())
                atomic_json(path, record)
            raise


def run_worker(record_path, settings_path):
    return _execute_worker(record_path, settings_path, resume=False)


def resume_worker(record_path, settings_path):
    return _execute_worker(record_path, settings_path, resume=True)


def status_job(args):
    """Read one explicitly bound task; never look for a most-recent result."""
    context=followup_identity_from_context(args.ccconnect_context)
    settings=load_settings(args.settings)
    path=_runtime_record_path(args.record,settings)
    record=json.loads(path.read_text(encoding='utf-8'))
    if record.get('sender_id')!=context['requester_id'] or record.get('source_chat_id')!=context['chat_id']:
        raise InputError('该任务不属于当前请求人的来源会话。')
    status=record.get('status','unknown');receipt=record.get('delivery_receipt') or {}
    delivered=bool(status=='completed' and receipt.get('identity')=='bot'
        and receipt.get('source_message_id')==record['source_message_id']
        and receipt.get('source_chat_id')==record['source_chat_id']
        and receipt.get('profile')==settings['report_lark_profile']
        and receipt.get('report_url')==record.get('report_url')
        and re.fullmatch(r'om_[A-Za-z0-9_]+',str(receipt.get('message_id') or '')))
    messages={'queued':'任务已排队，等待执行服务处理。','running':'正在准备两段分析请求。',
        'dispatching':'正在派发分析请求，尚未确认全部发送。','waiting_cards':'正在等待两段分析结果。',
        'granting_report_permissions':'报告已生成，正在完成访问权限检查。',
        'delivering_report':'报告正在发送，尚未确认送达。'}
    message=messages.get(status,'任务状态需要核查，已有执行记录已保留。')
    if status=='completed':message='报告已返回原话题。' if delivered else '报告已生成，历史送达状态尚未确认。'
    if status=='failed':
        error=str(record.get('error') or '')
        if record.get('report_url'):message='报告已生成，但权限处理或发送未完成；结果已保留。'
        elif '授权' in error:message='飞书授权检查未通过，任务未完成。'
        elif '超时' in error or 'timeout' in error.lower():message='等待或处理数据超时，执行记录已保留。'
        elif any(word in error for word in ('归属','冲突','关联')):message='结果来源或时段关联校验未通过，已停止生成报告。'
        else:message='任务未完成，诊断信息已保留，可基于本任务继续排查。'
    value={'job_key':record['job_key'],'status':status,'message':message,'delivered':delivered}
    if delivered:value['report_url']=record.get('report_url')
    return value

def apply_ccconnect_context(args) -> None:
    if not args.ccconnect_context:
        raise InputError('阿斯加德任务必须原样传入 CC Connect 请求上下文，不能用手工身份创建任务。')
    if args.ccconnect_context:
        try:
            context = followup_identity_from_context(args.ccconnect_context)
        except RequestContextError as exc:
            raise InputError(str(exc)) from None
        expected = {
            "chat_id": context["chat_id"],
            "sender_id": context["requester_id"],
            "message_id": context["message_id"],
        }
        for field, value in expected.items():
            supplied = getattr(args, field)
            if supplied not in (None, "", value):
                raise InputError(f"手工 {field} 不能覆盖 CC Connect 请求上下文")
            setattr(args, field, value)
    missing = [field for field in ("chat_id", "sender_id", "message_id") if not getattr(args, field)]
    if missing:
        raise InputError("阿斯加德任务需要 CC Connect 请求上下文，缺少: " + ", ".join(missing))


def start_job(args) -> dict:
    apply_ccconnect_context(args)
    settings = load_settings(args.settings)
    # Reject malformed inputs and source collisions before OAuth/network work.
    request = parse_comparison_request(args.text)
    platform = str(settings.get('asgard_platform') or '').strip()
    if not platform:
        raise InputError('阿斯加德桥接配置缺少平台参数。')
    commands = []
    for role in ('baseline', 'replacement'):
        item = request[role+'_metadata']
        commands.append(f"fsdlog {item['vin']}, {item['start'].replace('T', ' ')}, {item['end'].replace('T', ' ')}, {platform}")
    runtime = Path(settings['runtime_state_dir'])
    key = hashlib.sha256(args.message_id.encode()).hexdigest()[:24]
    record_path = runtime / (key+'.json')
    def existing_record():
        _runtime_record_path(record_path, settings)
        record = json.loads(record_path.read_text(encoding='utf-8'))
        expected = {'source_chat_id':args.chat_id, 'sender_id':args.sender_id,
                    'source_message_id':args.message_id, 'vin':request['vin'], 'commands':commands}
        if any(record.get(field) != value for field, value in expected.items()):
            raise InputError('同一来源消息的身份或业务参数冲突，未复用或新建任务。')
        return {'status':record.get('status','unknown'), 'idempotent':True, 'job_key':key,
                'comparison_key':record.get('comparison_key'), 'record_path':str(record_path)}
    if record_path.exists():
        return existing_record()
    # Keep the worker's process-level home pointed at the real Windows user
    # profile.  start_job pins HOME/USERPROFILE for helpers below, but a worker
    # that inherits those values would mistake the project lark_home for the
    # Windows home when it validates isolation before activating that same
    # lark_home for its own downstream commands.
    worker_environment = dict(os.environ)
    activate_lark_environment(settings)
    # Do not acknowledge a queued task until both identities required for the
    # complete workflow are available. This prevents a local job_key from being
    # mistaken for a successful upstream/report dispatch.
    ensure_report_bot_authorization(settings)
    authorization = ensure_user_authorization(settings)
    ensure_secure_label_authorization(settings)
    secure_label = resolve_report_secure_label(settings)
    runtime.mkdir(parents=True, exist_ok=True)
    record = {
        "job_key": key, "status": "queued", "vin": request["vin"], "commands": commands,
        "source_chat_id": args.chat_id, "sender_id": args.sender_id,
        # The source message is the topic root (or reply-thread root in a regular group).
        "source_message_id": args.message_id, "source_thread_id": args.message_id,
        "dispatch_user_open_id": authorization["open_id"],
        "report_secure_label_required": True,
        "report_secure_label": secure_label,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        fd = os.open(record_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return existing_record()
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        with (runtime / f'{key}.log').open('ab', buffering=0) as log:
            subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()), 'worker',
                              '--record', str(record_path), '--settings', str(Path(args.settings).resolve())],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
                             env=worker_environment,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except OSError:
        record.update(status='failed', error='时延后台进程启动失败，尚未派发上游。',
                      failed_at=datetime.now(timezone.utc).isoformat())
        atomic_json(record_path, record)
        raise InputError(record['error']) from None
    return {"status": "queued", "idempotent": False, "job_key": key, "vin": request["vin"], 'record_path':str(record_path),
            'message':'任务已排队，上游发送确认后开始等待两段结果；报告将返回原话题。'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CC Connect 阿斯加德 AD P99 后台桥接")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--text", required=True)
    start.add_argument("--chat-id")
    start.add_argument("--sender-id")
    start.add_argument("--message-id")
    start.add_argument("--ccconnect-context", help="机器人消息首行的 request_context_v=1 原文")
    start.add_argument("--settings", type=Path, default=Path.home() / ".cc-connect" / "secrets" / "ad-p99-asgard.settings.json")
    worker = commands.add_parser("worker")
    worker.add_argument("--record", type=Path, required=True)
    worker.add_argument("--settings", type=Path, required=True)
    resume = commands.add_parser("resume", help="恢复已派发任务：仅收集卡片和出报告，不重发上游请求")
    resume.add_argument("--record", type=Path, required=True)
    resume.add_argument("--settings", type=Path, required=True)
    status=commands.add_parser('status',help='只读查看精确任务，不创建或恢复业务')
    status.add_argument('--record',type=Path,required=True)
    status.add_argument('--settings',type=Path,required=True)
    status.add_argument('--ccconnect-context',required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            print(json.dumps(start_job(args), ensure_ascii=False))
            return 0
        if args.command=='status':
            print(json.dumps(status_job(args),ensure_ascii=False))
            return 0
        if args.command == "resume":
            return resume_worker(args.record, args.settings)
        return run_worker(args.record, args.settings)
    except Exception as exc:
        # Only the validated lock owner may update a worker failure. An invalid
        # path or a competing invocation must never overwrite the live owner.
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    import tempfile
    raise SystemExit(main())
