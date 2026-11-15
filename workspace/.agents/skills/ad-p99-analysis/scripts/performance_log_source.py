#!/usr/bin/env python3
"""Fetch the narrow log_fsdA_service source needed by AD performance reports.

This module is owned by the AD skill.  It does not invoke the uroad report
pipeline and never retains downloaded vehicle logs after compact CPU, PCM E2E
P99, and PCM frame-rate metrics have been calculated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from cloud_log_client import (
    QueryResult,
    download_to_file,
    load_settings,
    ordered_files,
    query_files,
    safe_filename,
)
from state_performance import extract as extract_state_performance, parse_request_time


LOG_CLASS = "log_fsdA_service"
FILE_PREFIX = "AVM_Service-"
FILE_SUFFIX = ".zst"
DEFAULT_MAX_FILES = 200
MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
DECOMPRESS_CHUNK_BYTES = 1024 * 1024


def filter_avm_files(items: list[dict]) -> list[dict]:
    """Select only AVM service archives and explicitly exclude netbw logs."""
    selected = []
    for item in items:
        name = str(item.get("fileName") or item.get("name") or "")
        folded = name.casefold()
        if name.startswith(FILE_PREFIX) and folded.endswith(FILE_SUFFIX) and "netbw-" not in folded:
            selected.append(item)
    return ordered_files(selected)


def _selected_files_snapshot_sha256(items: list[dict]) -> str | None:
    """Hash a file manifest, omitting temporary download credentials.

    fileKey, fileName/name and collectTime are the record fields already used
    by cloud_log_client.  Records without that stable identity cannot safely
    establish that an early read still matches the later file listing.
    Other record metadata is hashed too, without retaining it in the result.
    """
    if not items:
        return None
    records = []
    seen_keys = set()
    for item in items:
        if not isinstance(item, dict):
            return None
        key = item.get("fileKey")
        name = item.get("fileName") or item.get("name")
        collected = item.get("collectTime")
        if (not isinstance(key, str) or not key.strip()
                or key in seen_keys
                or not isinstance(name, str) or not name.strip()
                or isinstance(collected, bool)
                or not isinstance(collected, (str, int, float))
                or not str(collected).strip()):
            return None
        seen_keys.add(key)
        record = {field: value for field, value in item.items()
                  if field not in {"downloadURL", "downloadUrl", "url"}}
        try:
            records.append(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":"), allow_nan=False))
        except (TypeError, ValueError):
            return None
    payload = json.dumps(sorted(records), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _query_window(*, vin: str, start: str, end: str, username: str, environment: str) -> QueryResult:
    return query_files(
        vin=vin,
        start=start,
        end=end,
        log_class=LOG_CLASS,
        username=username,
        environment=environment,
    )


def resolve_pair_queries(*, vin: str, baseline_start: str, baseline_end: str,
                         replacement_start: str, replacement_end: str, username: str,
                         environment: str = "auto", query_fn=_query_window):
    """Resolve one environment for both windows without system-error fallback."""
    if environment not in {"auto", "prod", "test"}:
        raise ValueError("cloud environment 必须是 auto、prod 或 test。")
    candidates = (environment,) if environment != "auto" else ("prod", "test")
    attempts = []
    for env in candidates:
        baseline = query_fn(vin=vin, start=baseline_start, end=baseline_end,
                            username=username, environment=env)
        replacement = query_fn(vin=vin, start=replacement_start, end=replacement_end,
                               username=username, environment=env)
        count = int(bool(filter_avm_files(baseline.items))) + int(bool(filter_avm_files(replacement.items)))
        attempts.append((env, baseline, replacement, count))
        if count == 2:
            return env, baseline, replacement
        # Reaching the next candidate is allowed only after two successful
        # queries.  Exceptions propagate and therefore never cause fallback.
    chosen = max(attempts, key=lambda item: item[3])
    return chosen[0], chosen[1], chosen[2]


def decompress_zst(source: Path, destination: Path) -> None:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError("缺少 zstandard 运行依赖，无法解压 AVM 日志。") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.part")
    try:
        with source.open("rb") as compressed, temporary.open("xb") as output:
            reader = zstd.ZstdDecompressor().stream_reader(compressed)
            total = 0
            with reader:
                while True:
                    chunk = reader.read(DECOMPRESS_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DECOMPRESSED_BYTES:
                        raise RuntimeError("AVM 日志解压后超过单文件大小限制。")
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _missing_state(start: str, end: str, environment: str, reason: str) -> dict:
    return {
        "status": "failed",
        "time_window": {"start": start, "end": end},
        "metrics": {},
        "coverage": {"files_read": 0},
        "source": {"kind": LOG_CLASS, "environment": environment, "selected_files": 0},
        "error": reason,
    }


def extract_window(query: QueryResult, *, start: str, end: str, max_files: int = DEFAULT_MAX_FILES,
                   download_fn=download_to_file, decompress_fn=decompress_zst,
                   extract_fn=extract_state_performance) -> dict:
    if type(max_files) is not int or max_files <= 0:
        raise ValueError("max_files 必须是正整数。")
    selected = filter_avm_files(query.items)
    if not selected:
        return _missing_state(start, end, query.environment, "查询成功，但该时间窗没有 AVM_Service 日志。")
    if len(selected) > max_files:
        raise RuntimeError(f"该时间窗匹配 {len(selected)} 个 AVM 文件，超过安全上限 {max_files}，拒绝截断分析。")

    with tempfile.TemporaryDirectory(prefix="ad-p99-avm-") as temporary:
        root = Path(temporary)
        input_dir = root / "input"
        for index, item in enumerate(selected, 1):
            name = safe_filename(item, index)
            url = item.get("downloadURL") or item.get("downloadUrl") or item.get("url")
            if not url:
                raise RuntimeError("AVM 日志记录缺少下载地址。")
            item_dir = root / f"item-{index:04d}"
            archive = item_dir / name
            output = input_dir / f"item-{index:04d}" / name[:-4]
            try:
                download_fn(str(url), archive, environment=query.environment)
                decompress_fn(archive, output)
            finally:
                archive.unlink(missing_ok=True)
                archive.with_name(archive.name + ".part").unlink(missing_ok=True)
        result = extract_fn(input_dir, start, end)

    result["source"] = {
        "kind": LOG_CLASS,
        "environment": query.environment,
        "selected_files": len(selected),
        "selected_files_snapshot_sha256": _selected_files_snapshot_sha256(selected),
    }
    return result


def validate_pair_snapshot(baseline_state, replacement_state, *, baseline_metadata,
                           replacement_metadata, cloud_env="auto", settings_path=None,
                           skip_cpu=False) -> bool:
    """Recheck an early pair against current file listings, without downloads.

    This is an optional optimization guard: an unavailable, changed or
    unverifiable listing returns False so the caller can use its normal late
    read.  It never changes the existing query or download retry policy.
    """
    try:
        if skip_cpu:
            return False
        states = (baseline_state, replacement_state)
        sources = []
        for state in states:
            if (not isinstance(state, dict) or state.get("status") not in {"ok", "partial"}
                    or state.get("error") or not isinstance(state.get("metrics"), dict)
                    or not state["metrics"]):
                return False
            coverage = state.get("coverage") or {}
            source = state.get("source") or {}
            if not isinstance(coverage, dict) or not isinstance(source, dict):
                return False
            files_read = coverage.get("files_read")
            selected = source.get("selected_files")
            fingerprint = source.get("selected_files_snapshot_sha256")
            if (source.get("kind") != LOG_CLASS or source.get("environment") not in {"prod", "test"}
                    or type(files_read) is not int or files_read <= 0
                    or type(selected) is not int or selected <= 0
                    or files_read != selected
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None):
                return False
            sources.append(source)

        from ad_p99 import comparison_context
        baseline_context = comparison_context(baseline_metadata, "Baseline")
        replacement_context = comparison_context(replacement_metadata, "PCM更新")
        if baseline_context["vin"] != replacement_context["vin"]:
            return False
        username = load_settings(Path(settings_path) if settings_path else None)["username"]
        resolved, baseline_query, replacement_query = resolve_pair_queries(
            vin=baseline_context["vin"],
            baseline_start=parse_request_time(baseline_context["start"]).strftime("%Y-%m-%d %H:%M:%S"),
            baseline_end=parse_request_time(baseline_context["end"]).strftime("%Y-%m-%d %H:%M:%S"),
            replacement_start=parse_request_time(replacement_context["start"]).strftime("%Y-%m-%d %H:%M:%S"),
            replacement_end=parse_request_time(replacement_context["end"]).strftime("%Y-%m-%d %H:%M:%S"),
            username=username,
            environment=cloud_env,
        )
        for source, query in zip(sources, (baseline_query, replacement_query)):
            selected = filter_avm_files(query.items)
            if (source["environment"] != resolved or query.environment != resolved
                    or source["selected_files"] != len(selected)
                    or source["selected_files_snapshot_sha256"] != _selected_files_snapshot_sha256(selected)):
                return False
        return True
    except Exception:
        # Prefetching is optional; validation failures must preserve the
        # caller's original late-load behavior and sanitized error handling.
        return False


def extract_pair(*, vin: str, baseline_start: str, baseline_end: str,
                 replacement_start: str, replacement_end: str, environment: str = "auto",
                 settings_path: str | Path | None = None, max_files: int = DEFAULT_MAX_FILES,
                 query_fn=_query_window, download_fn=download_to_file,
                 decompress_fn=decompress_zst, extract_fn=extract_state_performance):
    """Fetch and reduce both windows, returning no raw logs or download URLs."""
    username = load_settings(Path(settings_path) if settings_path else None)["username"]
    query_baseline_start = parse_request_time(baseline_start).strftime("%Y-%m-%d %H:%M:%S")
    query_baseline_end = parse_request_time(baseline_end).strftime("%Y-%m-%d %H:%M:%S")
    query_replacement_start = parse_request_time(replacement_start).strftime("%Y-%m-%d %H:%M:%S")
    query_replacement_end = parse_request_time(replacement_end).strftime("%Y-%m-%d %H:%M:%S")
    resolved, baseline_query, replacement_query = resolve_pair_queries(
        vin=vin,
        baseline_start=query_baseline_start,
        baseline_end=query_baseline_end,
        replacement_start=query_replacement_start,
        replacement_end=query_replacement_end,
        username=username,
        environment=environment,
        query_fn=query_fn,
    )
    baseline = extract_window(
        baseline_query, start=baseline_start, end=baseline_end, max_files=max_files,
        download_fn=download_fn, decompress_fn=decompress_fn, extract_fn=extract_fn,
    )
    replacement = extract_window(
        replacement_query, start=replacement_start, end=replacement_end, max_files=max_files,
        download_fn=download_fn, decompress_fn=decompress_fn, extract_fn=extract_fn,
    )
    return baseline, replacement, resolved
