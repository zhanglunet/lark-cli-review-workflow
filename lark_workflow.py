#!/usr/bin/env python3
"""
Feishu/Lark CLI approval workflow.

Flow:
1. scan a source group for new files and task-like messages
2. send a review request to a reviewer
3. poll reviewer replies for approve/reject words
4. execute approved actions
5. push the execution result back to the original group
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any


STATE_DIR = Path(".workflow-state")
RUNS_DIR = STATE_DIR / "runs"
STATE_FILE = STATE_DIR / "state.json"

TASK_PATTERNS = [
    re.compile(r"^\s*(?:#task|任务[:：]|todo[:：])\s*(?P<title>.+)$", re.I),
    re.compile(r"^\s*-\s*\[\s*\]\s*(?P<title>.+)$", re.I),
]
FILE_TAG_PATTERN = re.compile(r'<file\s+[^>]*key="(?P<key>[^"]+)"[^>]*name="(?P<name>[^"]+)"[^>]*/?>')
DEFAULT_PROMPT_KEYWORDS = ["prompt", "promt", "提示词", "指令", "请你", "帮我", "结合这个文档"]


class WorkflowError(RuntimeError):
    pass


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def run_lark(args: list[str], *, dry_run: bool = False) -> Any:
    cmd = ["lark-cli", *args]
    if dry_run:
        print("+ " + " ".join(cmd), file=sys.stderr)
        return {"dry_run": True, "command": cmd}

    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise WorkflowError(f"lark-cli failed: {' '.join(cmd)}\n{detail}")

    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "messages", "data", "records"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
            if isinstance(nested, dict):
                nested_items = as_list(nested)
                if nested_items:
                    return nested_items
    return []


def message_id(message: dict[str, Any]) -> str:
    return str(message.get("message_id") or message.get("messageId") or message.get("id") or "")


def message_type(message: dict[str, Any]) -> str:
    return str(message.get("msg_type") or message.get("message_type") or message.get("type") or "")


def content_obj(message: dict[str, Any]) -> Any:
    content = message.get("content")
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def content_text(message: dict[str, Any]) -> str:
    content = content_obj(message)
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        chunks: list[str] = []
        for key in ("text", "title", "file_name", "name"):
            value = content.get(key)
            if isinstance(value, str):
                chunks.append(value)
        # Post messages often contain a nested rich text structure.
        for value in content.values():
            if isinstance(value, list):
                chunks.extend(flatten_text(value))
            elif isinstance(value, dict):
                chunks.extend(flatten_text([value]))
        return "\n".join(dict.fromkeys(chunks))
    return ""


def flatten_text(items: list[Any]) -> list[str]:
    chunks: list[str] = []
    for item in items:
        if isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, dict):
            for key in ("text", "title", "name"):
                value = item.get(key)
                if isinstance(value, str):
                    chunks.append(value)
            for value in item.values():
                if isinstance(value, list):
                    chunks.extend(flatten_text(value))
                elif isinstance(value, dict):
                    chunks.extend(flatten_text([value]))
    return chunks


def extract_file_action(message: dict[str, Any]) -> dict[str, Any] | None:
    content = content_obj(message)
    msg_type = message_type(message)
    if msg_type == "file" and isinstance(content, str):
        match = FILE_TAG_PATTERN.search(content)
        if match:
            return {
                "kind": "download_file",
                "message_id": message_id(message),
                "file_key": match.group("key"),
                "file_name": match.group("name"),
            }

    candidates: list[dict[str, Any]] = []
    if isinstance(content, dict):
        candidates.append(content)
    candidates.append(message)

    for item in candidates:
        file_key = item.get("file_key") or item.get("fileKey") or item.get("key")
        if file_key and (msg_type == "file" or str(file_key).startswith("file_")):
            return {
                "kind": "download_file",
                "message_id": message_id(message),
                "file_key": str(file_key),
                "file_name": str(item.get("file_name") or item.get("name") or file_key),
            }
    return None


def extract_task_actions(message: dict[str, Any]) -> list[dict[str, Any]]:
    text = content_text(message)
    actions: list[dict[str, Any]] = []
    for line in text.splitlines():
        for pattern in TASK_PATTERNS:
            match = pattern.match(line)
            if match:
                title = match.group("title").strip()
                if title:
                    actions.append(
                        {
                            "kind": "create_task",
                            "message_id": message_id(message),
                            "summary": title[:240],
                            "description": f"来自群消息 {message_id(message)}\n\n{line.strip()}",
                        }
                    )
                break
    return actions


def compact_text(text: str, limit: int = 180) -> str:
    compacted = re.sub(r"\s+", " ", text).strip()
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 1] + "..."


def extract_prompt_action(message: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    msg_type = message_type(message)
    if msg_type not in {"text", "post"}:
        return None

    text = content_text(message).strip()
    if not text:
        return None

    prompt_cfg = config.get("prompt_capture", {})
    min_chars = int(prompt_cfg.get("min_chars", 20))
    if len(text) < min_chars:
        return None

    keywords = [str(item).lower() for item in prompt_cfg.get("keywords", DEFAULT_PROMPT_KEYWORDS)]
    lowered = text.lower()
    if keywords and not any(keyword in lowered for keyword in keywords):
        return None

    return {
        "kind": "capture_prompt",
        "message_id": message_id(message),
        "summary": compact_text(text),
        "text": text,
        "reply_to": message.get("reply_to") or "",
        "create_time": message.get("create_time") or "",
    }


def fingerprint(action: dict[str, Any]) -> str:
    raw = json.dumps(action, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[\\/:\0]+", "_", name).strip()
    return cleaned or "attachment"


def safe_dirname(name: str) -> str:
    cleaned = re.sub(r"[\\/:\0]+", "-", name).strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned.strip(".") or "chat"


def project_root(config: dict[str, Any], run: dict[str, Any]) -> Path:
    projects_dir = Path(config["execution"].get("projects_dir", "projects"))
    by_chat_id = {chat["chat_id"]: chat for chat in config["source_chats"]}
    source_chat = by_chat_id.get(run["source_chat_id"], {})
    project_dir = source_chat.get("project_dir")
    if not project_dir:
        project_dir = safe_dirname(str(source_chat.get("name") or run.get("source_chat_name") or run["source_chat_id"]))
    return projects_dir / project_dir


def ensure_project(config: dict[str, Any], source_chat: dict[str, Any]) -> Path:
    run_like = {
        "source_chat_id": source_chat["chat_id"],
        "source_chat_name": source_chat.get("name", source_chat["chat_id"]),
    }
    root = project_root(config, run_like)
    (root / "files").mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(parents=True, exist_ok=True)

    context = root / "context.md"
    if not context.exists():
        context.write_text(
            "\n".join(
                [
                    f"# {source_chat.get('name', source_chat['chat_id'])}",
                    "",
                    f"- chat_id: `{source_chat['chat_id']}`",
                    f"- project_dir: `{root}`",
                    "",
                    "## 上下文",
                    "",
                    "这个目录用于保存该飞书群的文件、提示词和执行结果。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return root


def init_projects(config: dict[str, Any]) -> dict[str, Any]:
    projects = []
    for source_chat in config["source_chats"]:
        root = ensure_project(config, source_chat)
        projects.append(
            {
                "name": source_chat.get("name", source_chat["chat_id"]),
                "chat_id": source_chat["chat_id"],
                "project_dir": str(root),
            }
        )
    return {"projects": projects}


def scan(config: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for source_chat in config["source_chats"]:
        result = scan_chat(config, source_chat, dry_run=dry_run)
        if "runs" in result and isinstance(result["runs"], list):
            runs.extend(result["runs"])
        else:
            runs.append(result)
    return {"runs": runs}


def scan_chat(config: dict[str, Any], source_chat: dict[str, str], *, dry_run: bool) -> dict[str, Any]:
    state = load_json(STATE_FILE, {"processed": []})
    processed = set(state.get("processed", []))
    lookback = int(config["scan"].get("lookback_minutes", 60))
    start = iso_z(now_utc() - dt.timedelta(minutes=lookback))

    raw = run_lark(
        [
            "im",
            "+chat-messages-list",
            "--as",
            config["scan"].get("message_identity", "user"),
            "--chat-id",
            source_chat["chat_id"],
            "--start",
            start,
            "--sort",
            "asc",
            "--page-size",
            str(config["scan"].get("page_size", 50)),
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )

    actions: list[dict[str, Any]] = []
    for item in as_list(raw):
        if not isinstance(item, dict):
            continue
        file_action = extract_file_action(item)
        if file_action:
            actions.append(file_action)
        actions.extend(extract_task_actions(item))
        prompt_action = extract_prompt_action(item, config)
        if prompt_action:
            actions.append(prompt_action)

    unique: list[dict[str, Any]] = []
    for action in actions:
        action["action_id"] = fingerprint(action)
        if action["action_id"] not in processed:
            unique.append(action)

    if not unique:
        return {
            "run_id": uuid.uuid4().hex[:10],
            "status": "empty",
            "created_at": iso_z(now_utc()),
            "source_chat_id": source_chat["chat_id"],
            "source_chat_name": source_chat.get("name", source_chat["chat_id"]),
            "actions": [],
        }

    action_groups = split_action_groups(unique)
    runs: list[dict[str, Any]] = []
    for group in action_groups:
        runs.append(create_review_run(config, source_chat, group, state, processed, dry_run=dry_run))
    return {"runs": runs}


def split_action_groups(actions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    batch = [action for action in actions if action["kind"] != "capture_prompt"]
    if batch:
        groups.append(batch)
    groups.extend([[action] for action in actions if action["kind"] == "capture_prompt"])
    return groups


def create_review_run(
    config: dict[str, Any],
    source_chat: dict[str, str],
    actions: list[dict[str, Any]],
    state: dict[str, Any],
    processed: set[str],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:10]
    run = {
        "run_id": run_id,
        "status": "pending_review",
        "created_at": iso_z(now_utc()),
        "source_chat_id": source_chat["chat_id"],
        "source_chat_name": source_chat.get("name", source_chat["chat_id"]),
        "actions": actions,
    }
    if not dry_run:
        save_json(RUNS_DIR / f"{run_id}.json", run)
        send_review_request(config, run, dry_run=dry_run)
        processed.update(action["action_id"] for action in actions)
        state["processed"] = sorted(processed)
        save_json(STATE_FILE, state)
    return run


def format_actions(actions: list[dict[str, Any]]) -> str:
    if not actions:
        return "没有发现新的文件、任务或提示词。"

    lines: list[str] = []
    for idx, action in enumerate(actions, 1):
        if action["kind"] == "download_file":
            lines.append(f"{idx}. 下载文件：{action.get('file_name')}（消息 {action.get('message_id')}）")
        elif action["kind"] == "create_task":
            lines.append(f"{idx}. 创建任务：{action.get('summary')}（消息 {action.get('message_id')}）")
        elif action["kind"] == "capture_prompt":
            lines.append(f"{idx}. 读取提示词：{action.get('summary')}（消息 {action.get('message_id')}）")
        else:
            lines.append(f"{idx}. {action['kind']}：{action}")
    return "\n".join(lines)


def send_review_request(config: dict[str, Any], run: dict[str, Any], *, dry_run: bool) -> None:
    if not run["actions"]:
        run["status"] = "empty"
        save_json(RUNS_DIR / f"{run['run_id']}.json", run)
        return

    text = textwrap.dedent(
        f"""
        工作流待审核：{run['run_id']}
        来源群：{run.get('source_chat_name', run['source_chat_id'])}

        {format_actions(run['actions'])}

        回复“同意 {run['run_id']}”后自动执行；回复“拒绝 {run['run_id']}”将取消。
        """
    ).strip()
    result = run_lark(
        [
            "im",
            "+messages-send",
            "--as",
            config["approval"].get("identity", "bot"),
            "--user-id",
            config["reviewer_user_id"],
            "--text",
            text,
            "--idempotency-key",
            f"review-{run['run_id']}",
        ],
        dry_run=dry_run,
    )
    run["review_message"] = result
    save_json(RUNS_DIR / f"{run['run_id']}.json", run)


def load_run(run_id: str) -> dict[str, Any]:
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        raise WorkflowError(f"Run not found: {run_id}")
    return load_json(path, {})


def find_review_decision(config: dict[str, Any], run: dict[str, Any], *, dry_run: bool) -> str | None:
    raw = run_lark(
        [
            "im",
            "+chat-messages-list",
            "--as",
            config["approval"].get("identity", "bot"),
            "--user-id",
            config["reviewer_user_id"],
            "--start",
            run["created_at"],
            "--sort",
            "desc",
            "--page-size",
            "50",
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )
    approve_words = [str(x).lower() for x in config["approval"].get("approve_words", ["同意", "approve"])]
    reject_words = [str(x).lower() for x in config["approval"].get("reject_words", ["拒绝", "reject"])]
    token = run["run_id"].lower()

    for item in as_list(raw):
        if not isinstance(item, dict):
            continue
        text = content_text(item).lower()
        if token not in text:
            continue
        if any(word in text for word in reject_words):
            return "rejected"
        if any(word in text for word in approve_words):
            return "approved"
    return None


def execute(config: dict[str, Any], run: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    root = project_root(config, run)
    files_dir = root / "files"
    prompt_dir = root / "prompts"
    result_dir = root / "results"
    if not dry_run:
        files_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)

    for action in run["actions"]:
        try:
            if action["kind"] == "download_file":
                output = files_dir / f"{action['action_id']}-{safe_filename(action['file_name'])}"
                result = run_lark(
                    [
                        "im",
                        "+messages-resources-download",
                        "--as",
                        config["execution"].get("identity", "user"),
                        "--message-id",
                        action["message_id"],
                        "--file-key",
                        action["file_key"],
                        "--type",
                        "file",
                        "--output",
                        str(output),
                    ],
                    dry_run=dry_run,
                )
            elif action["kind"] == "create_task":
                cmd = [
                    "task",
                    "+create",
                    "--as",
                    config["execution"].get("identity", "user"),
                    "--summary",
                    action["summary"],
                    "--description",
                    action.get("description", ""),
                    "--idempotency-key",
                    f"task-{run['run_id']}-{action['action_id']}",
                    "--format",
                    "json",
                ]
                if config["execution"].get("task_assignee"):
                    cmd.extend(["--assignee", config["execution"]["task_assignee"]])
                if config["execution"].get("tasklist_id"):
                    cmd.extend(["--tasklist-id", config["execution"]["tasklist_id"]])
                result = run_lark(cmd, dry_run=dry_run)
            elif action["kind"] == "capture_prompt":
                output = prompt_dir / f"{action['action_id']}-{safe_filename(action['message_id'])}.txt"
                if not dry_run:
                    output.write_text(
                        "\n".join(
                            [
                                f"message_id: {action['message_id']}",
                                f"create_time: {action.get('create_time', '')}",
                                f"reply_to: {action.get('reply_to', '')}",
                                "",
                                action["text"],
                                "",
                            ]
                        ),
                        encoding="utf-8",
                    )
                result = {"saved_to": str(output)}
            else:
                result = {"skipped": f"unknown action kind: {action['kind']}"}
            results.append({"action_id": action["action_id"], "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001 - preserve per-action failure details for review.
            results.append({"action_id": action["action_id"], "ok": False, "error": str(exc)})

    run["status"] = "executed" if all(item["ok"] for item in results) else "executed_with_errors"
    run["executed_at"] = iso_z(now_utc())
    run["project_dir"] = str(root)
    run["results"] = results
    if not dry_run:
        save_json(result_dir / f"{run['run_id']}.json", run)
    save_json(RUNS_DIR / f"{run['run_id']}.json", run)

    state = load_json(STATE_FILE, {"processed": []})
    processed = set(state.get("processed", []))
    if run["status"] == "executed":
        processed.update(action["action_id"] for action in run["actions"])
    state["processed"] = sorted(processed)
    save_json(STATE_FILE, state)
    send_result(config, run, dry_run=dry_run)
    return run


def send_result(config: dict[str, Any], run: dict[str, Any], *, dry_run: bool) -> None:
    lines = [
        f"工作流执行结果：{run['run_id']}",
        f"来源群：{run.get('source_chat_name', run['source_chat_id'])}",
        f"状态：{run['status']}",
    ]
    for item in run.get("results", []):
        marker = "成功" if item.get("ok") else "失败"
        lines.append(f"- {marker} {item.get('action_id')}")
        if not item.get("ok"):
            lines.append(f"  {item.get('error')}")

    run_lark(
        [
            "im",
            "+messages-send",
            "--as",
            config["result"].get("identity", "bot"),
            "--chat-id",
            run["source_chat_id"],
            "--text",
            "\n".join(lines),
            "--idempotency-key",
            f"result-{run['run_id']}",
        ],
        dry_run=dry_run,
    )


def check(config: dict[str, Any], run_id: str, *, dry_run: bool) -> dict[str, Any]:
    run = load_run(run_id)
    if run["status"] != "pending_review":
        return run

    decision = find_review_decision(config, run, dry_run=dry_run)
    if decision == "rejected":
        run["status"] = "rejected"
        run["reviewed_at"] = iso_z(now_utc())
        save_json(RUNS_DIR / f"{run_id}.json", run)
        send_result(config, run, dry_run=dry_run)
    elif decision == "approved":
        run["status"] = "approved"
        run["reviewed_at"] = iso_z(now_utc())
        save_json(RUNS_DIR / f"{run_id}.json", run)
        run = execute(config, run, dry_run=dry_run)
    return run


def check_all(config: dict[str, Any], *, dry_run: bool) -> list[dict[str, Any]]:
    runs = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        run = load_json(path, {})
        if run.get("status") == "pending_review":
            checked = check(config, run["run_id"], dry_run=dry_run)
            runs.append(checked)
            if checked.get("status") != "pending_review":
                break
    return runs


def load_config(path: str) -> dict[str, Any]:
    config = load_json(Path(path), {})
    if "source_chats" not in config and config.get("source_chat_id"):
        config["source_chats"] = [{"chat_id": config["source_chat_id"], "name": config["source_chat_id"]}]

    required = ["source_chats", "reviewer_user_id"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise WorkflowError(f"Missing required config: {', '.join(missing)}")
    for idx, source_chat in enumerate(config["source_chats"], 1):
        if not source_chat.get("chat_id"):
            raise WorkflowError(f"Missing source_chats[{idx}].chat_id")
        source_chat.setdefault("project_dir", safe_dirname(str(source_chat.get("name") or source_chat["chat_id"])))
    config.setdefault("scan", {})
    config.setdefault("approval", {})
    config.setdefault("execution", {})
    config["execution"].setdefault("projects_dir", "projects")
    config.setdefault("result", {})
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description="Feishu/Lark CLI approval workflow")
    parser.add_argument("--config", default="workflow_config.json", help="config JSON path")
    parser.add_argument("--dry-run", action="store_true", help="print lark-cli commands without executing")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="scan source group and send review request")
    sub.add_parser("init-projects", help="create per-chat project folders")
    check_parser = sub.add_parser("check", help="check one pending run for approval and execute")
    check_parser.add_argument("run_id")
    sub.add_parser("check-all", help="check all pending runs")
    exec_parser = sub.add_parser("execute", help="execute an approved/pending run directly")
    exec_parser.add_argument("run_id")

    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "scan":
            result = scan(config, dry_run=args.dry_run)
        elif args.command == "init-projects":
            result = init_projects(config)
        elif args.command == "check":
            result = check(config, args.run_id, dry_run=args.dry_run)
        elif args.command == "check-all":
            result = check_all(config, dry_run=args.dry_run)
        elif args.command == "execute":
            result = execute(config, load_run(args.run_id), dry_run=args.dry_run)
        else:
            parser.error("unknown command")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
