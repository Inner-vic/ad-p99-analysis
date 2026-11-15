"""Controlled Asgard dispatch bridge; transport is injected by the Feishu adapter."""
from __future__ import annotations
import json, re, uuid
from datetime import datetime, timezone
from pathlib import Path

# This module lives beside the active AD P99 helpers.  Python adds the script
# directory to sys.path for the worker, so it must not depend on the old
# OpenClaw workspace layout.
from ad_p99 import InputError, parse_time_token
from half_auto import create_job_from_request, ingest_card_event, attach_card_automatically, load_job, write_json

FS = re.compile(r"^fsdlog\s+(?P<vin>[A-HJ-NPR-Z0-9]{17})\s*,\s*(?P<start>20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*,\s*(?P<end>20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*,\s*(?P<platform>[A-Za-z0-9._-]+)$", re.I)

def parse_commands(commands):
    if not isinstance(commands, (list, tuple)) or len(commands) != 2:
        raise InputError("必须提供两条独立 fsdlog 命令。")
    values=[]
    for command in commands:
        m=FS.fullmatch(" ".join(str(command).split()))
        if not m: raise InputError("fsdlog 命令格式无效。")
        values.append(m.groupdict())
    if values[0]["vin"].upper()!=values[1]["vin"].upper(): raise InputError("两条命令 VIN 不一致。")
    if values[0]["platform"].lower()!=values[1]["platform"].lower(): raise InputError("两条命令 platform 不一致。")
    parsed=[]
    for v in values:
        s,_=parse_time_token(v["start"]); e,_=parse_time_token(v["end"])
        if s>=e: raise InputError("时间段起点必须早于终点。")
        parsed.append((s,e))
    if max(parsed[0][0],parsed[1][0]) < min(parsed[0][1],parsed[1][1]): raise InputError("两条时间段不能重叠。")
    # The caller supplies commands in semantic order: Baseline first, then
    # replacement.  Do not re-sort here, because a user may explicitly choose
    # a later window as Baseline.
    return values, [0, 1]

def dispatch(commands, *, state_dir, send_fn, chat_id, sender_id, asgard_id=None, on_prepared=None):
    values, order=parse_commands(commands)
    base, repl=values[order[0]], values[order[1]]
    job=create_job_from_request({
        "comparison_key": uuid.uuid4().hex[:16], "vin": base["vin"].upper(),
        "baseline_metadata":{"vin":base["vin"].upper(),"start":base["start"],"end":base["end"]},
        "replacement_metadata":{"vin":repl["vin"].upper(),"start":repl["start"],"end":repl["end"]}
    }, state_dir=state_dir, asgard_platform=base["platform"])
    # Send the two independent fsdlog requests back-to-back.  They are
    # correlated later by their outgoing message_id (and only fall back to
    # VIN/time matching), so completion order cannot change their roles.
    path, state = load_job(state_dir, job["comparison_key"])
    state["dispatch"] = {"chat_id": chat_id, "sender_id": sender_id,
                          "asgard_id": asgard_id or sender_id, "sent": []}
    write_json(path, state)
    # Publish the durable job binding BEFORE the first upstream operation.
    # A partial dispatch must never leave a runtime that looks safe to resend.
    if on_prepared is not None:
        on_prepared(job['comparison_key'])
    version_command = f"查询车辆版本 {job['vin']} CCUFSD"
    version_result = send_fn(version_command, chat_id=chat_id,
                             mention_user_id=asgard_id or sender_id, role="vehicle_version")
    if not isinstance(version_result, dict) or not version_result.get("message_id"):
        raise InputError("CCUFSD 车辆版本查询发送未返回 message_id。")
    state["dispatch"]["sent"].append({"role":"vehicle_version","command":version_command,
                                       "message_id":version_result["message_id"],
                                       "sent_at":version_result.get('sent_at') or datetime.now(timezone.utc).isoformat()})
    write_json(path, state)
    # Persist evidence immediately after each successful send.  A later
    # adapter/ack failure must never erase the real outbound message_id.
    baseline_result=send_fn(job["baseline"]["asgard_command"], chat_id=chat_id, mention_user_id=asgard_id or sender_id, role="baseline")
    if not isinstance(baseline_result,dict) or not baseline_result.get("message_id"): raise InputError("baseline fsdlog 发送未返回 message_id。")
    state["dispatch"]["sent"].append({"role":"baseline","command":job["baseline"]["asgard_command"],"message_id":baseline_result["message_id"],"sent_at":baseline_result.get('sent_at') or datetime.now(timezone.utc).isoformat()})
    state["status"] = "waiting_cards"
    write_json(path, state)
    replacement_result=send_fn(job["replacement"]["asgard_command"], chat_id=chat_id, mention_user_id=asgard_id or sender_id, role="replacement")
    if not isinstance(replacement_result,dict) or not replacement_result.get("message_id"): raise InputError("replacement fsdlog 发送未返回 message_id。")
    path,state=load_job(state_dir,job["comparison_key"])
    state["dispatch"]["sent"].append({"role":"replacement","command":job["replacement"]["asgard_command"],"message_id":replacement_result["message_id"],"sent_at":replacement_result.get('sent_at') or datetime.now(timezone.utc).isoformat()})
    state["status"] = "waiting_cards"
    write_json(path,state)
    return {"status":"waiting_cards","comparison_key":job["comparison_key"],"sent":state["dispatch"]["sent"]}

def send_replacement(*, state_dir, comparison_key, send_fn):
    path,state=load_job(state_dir,comparison_key)
    # Kept for recovery of legacy jobs created before parallel dispatch.
    d=state["dispatch"]
    if any(item.get("role") == "replacement" and item.get("message_id") for item in d.get("sent", [])):
        return {"status": "waiting_cards", "comparison_key": comparison_key,
                "message_id": next(item["message_id"] for item in d["sent"] if item.get("role") == "replacement")}
    result=send_fn(d["replacement_command"],chat_id=d["chat_id"],mention_user_id=d.get("asgard_id") or d["sender_id"],role="replacement")
    if not isinstance(result,dict) or not result.get("message_id"): raise InputError("replacement fsdlog 发送未返回 message_id。")
    d["sent"].append({"role":"replacement","command":d["replacement_command"],"message_id":result["message_id"],"sent_at":datetime.now(timezone.utc).isoformat()})
    state["status"]="waiting_replacement_card"; write_json(path,state)
    return {"status":state["status"],"comparison_key":comparison_key,"message_id":result["message_id"]}

def ingest_card_event_checked(*, state_dir, card, chat_id, sender_id, expected_asgard_id, identity="user", read_fn=None):
    if chat_id is None or sender_id is None or sender_id != expected_asgard_id:
        raise InputError("结果卡片来源不符合已配置的阿斯加德身份或群聊。")
    job=attach_card_automatically(state_dir, card)
    path,state=load_job(state_dir, job["comparison_key"])
    received=state.get("received", {})
    if received.get("baseline") and not received.get("replacement"):
        state["status"]="baseline_url_ready"; write_json(path,state)
        return {"status":"baseline_url_ready","comparison_key":job["comparison_key"],"received":received}
    if received.get("baseline") and received.get("replacement"):
        state["status"]="comparing"; write_json(path,state)
        return {"status":"ready_to_compare","comparison_key":job["comparison_key"],"received":received}
    return {"status":"waiting_baseline_card","comparison_key":job["comparison_key"],"received":received}
