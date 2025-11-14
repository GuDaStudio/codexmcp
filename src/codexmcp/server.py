"""FastMCP server implementation for the Codex MCP project."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Annotated, Any, Dict, Generator, List, Literal, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BeforeValidator, Field
import shutil

mcp = FastMCP("Codex MCP Server-from guda.studio")


def _empty_str_to_none(value: str | None) -> str | None:
    """Convert empty strings to None for optional UUID parameters."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def run_shell_command(cmd: list[str]) -> Generator[str, None, None]:
    """Execute a command and stream its output line-by-line.

    Args:
        cmd: Command and arguments as a list (e.g., ["codex", "exec", "prompt"])

    Yields:
        Output lines from the command
    """
    # On Windows, codex is exposed via a *.cmd shim. Use cmd.exe with /s so
    # user prompts containing quotes/newlines aren't reinterpreted as shell syntax.
    popen_cmd = cmd
     
    codex_path = shutil.which('codex') or cmd[0]
    popen_cmd[0] = codex_path

    # if os.name == "nt" and codex_path.lower().endswith((".cmd", ".bat")):
    #     from subprocess import list2cmdline
    #     popen_cmd = ["cmd.exe", "/s", "/c", list2cmdline(cmd)]

    process = subprocess.Popen(
        popen_cmd,
        shell=False,  # Safer: no shell injection
        stdin=subprocess.PIPE,  # Prevent process from waiting for input
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        encoding='utf-8',
    )

    output_queue: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        """Read process output in a separate thread."""
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                output_queue.put(line.strip())
            process.stdout.close()

    thread = threading.Thread(target=read_output)
    thread.daemon = True
    thread.start()

    # Yield lines while process is running
    while process.poll() is None:
        try:
            yield output_queue.get(timeout=0.1)
        except queue.Empty:
            continue

    process.wait()

    # Drain remaining output from queue
    while not output_queue.empty():
        try:
            yield output_queue.get_nowait()
        except queue.Empty:
            break
def windows_escape(prompt):
    """
    Windows 风格的字符串转义函数。
    把常见特殊字符转义成 \\ 形式，适合命令行、JSON 或路径使用。
    比如：\n 变成 \\n，" 变成 \\"。
    """
    # 先处理反斜杠，避免它干扰其他替换
    result = prompt.replace('\\', '\\\\')
    # 双引号，转义成 \"，防止字符串边界乱套
    result = result.replace('"', '\\"')
    # 换行符，Windows 常用 \r\n，但我们分开转义
    result = result.replace('\n', '\\n')
    result = result.replace('\r', '\\r')
    # 制表符，空格的“超级版”
    result = result.replace('\t', '\\t')
    # 其他常见：退格符（像按了后退键）、换页符（打印机跳页用）
    result = result.replace('\b', '\\b')
    result = result.replace('\f', '\\f')
    # 如果有单引号，也转义下（不过 Windows 命令行不那么严格，但保险起见）
    result = result.replace("'", "\\'")
    
    return result

@mcp.tool(
    name="codex",
    description="""
    Executes a non-interactive Codex session via CLI to perform AI-assisted coding tasks in a secure workspace.
    This tool wraps the `codex exec` command, enabling model-driven code generation, debugging, or automation based on natural language prompts.
    It supports resuming ongoing sessions for continuity and enforces sandbox policies to prevent unsafe operations. Ideal for integrating Codex into MCP servers for agentic workflows, such as code reviews or repo modifications.

    **Key Features:**
        - **Prompt-Driven Execution:** Send task instructions to Codex for step-by-step code handling.
        - **Workspace Isolation:** Operate within a specified directory, with optional Git repo skipping.
        - **Security Controls:** Three sandbox levels balance functionality and safety.
        - **Session Persistence:** Resume prior conversations via `SESSION_ID` for iterative tasks.

    **Edge Cases & Best Practices:**
        - Ensure `cd` exists and is accessible; tool fails silently on invalid paths.
        - For most repos, prefer "read-only" to avoid accidental changes.
        - If needed, set `return_all_messages` to `True` to parse "all_messages" for detailed tracing (e.g., reasoning, tool calls, etc.).
    """,
    meta={"version": "0.0.0", "author": "guda.studio"},
)
async def codex(
    PROMPT: Annotated[str, "Instruction for the task to send to codex."],
    cd: Annotated[Path, "Set the workspace root for codex before executing the task."],
    sandbox: Annotated[
        Literal["read-only", "workspace-write", "danger-full-access"],
        Field(
            description="Sandbox policy for model-generated commands. Defaults to `read-only`."
        ),
    ] = "read-only",
    SESSION_ID: Annotated[
        Optional[uuid.UUID],
        BeforeValidator(_empty_str_to_none),
        "Resume the specified session of the codex. Defaults to `None`, start a new session.",
    ] = None,
    skip_git_repo_check: Annotated[
        bool,
        "Allow codex running outside a Git repository (useful for one-off directories).",
    ] = True,
    return_all_messages: Annotated[
        bool,
        "Return all messages (e.g. reasoning, tool calls, etc.) from the codex session. Set to `False` by default, only the agent's final reply message is returned.",
    ] = False,
    image: Annotated[
        Optional[List[Path]],
        Field(
            description="Attach one or more image files to the initial prompt. Separate multiple paths with commas or repeat the flag.",
        ),
    ] = None,
    model: Annotated[
        Optional[str],
        Field(
            description="The model to use for the codex session. This parameter is strictly prohibited unless explicitly specified by the user.",
        ),
    ] = None,
    yolo: Annotated[
        Optional[bool],
        Field(
            description="Run every command without approvals or sandboxing. Only use when `sandbox` couldn't be applied.",
        ),
    ] = False,
    profile: Annotated[
        Optional[str],
        "Configuration profile name to load from `~/.codex/config.toml`. This parameter is strictly prohibited unless explicitly specified by the user.",
    ] = None,
) -> Dict[str, Any]:
    """Execute a Codex CLI session and return the results."""
    # Build command as list to avoid injection
    cmd = ["codex", "exec", "--sandbox", sandbox, "--cd", str(cd), "--json"]
    
    if image is not None:
        cmd.extend(["--image", ",".join(image)])
        
    if model is not None:
        cmd.extend(["--model", model])
        
    if profile is not None:
        cmd.extend(["--profile", profile])
        
    if yolo:
        cmd.append("--yolo")
    
    if skip_git_repo_check:
        cmd.append("--skip-git-repo-check")

    if SESSION_ID is not None:
        cmd.extend(["resume", str(SESSION_ID)])
        
    if os.name == "nt":
        PROMPT = windows_escape(PROMPT)
    else:
        PROMPT = PROMPT
    cmd += ['--', PROMPT]

    all_messages: list[Dict[str, Any]] = []
    agent_messages = ""
    success = True
    err_message = ""
    thread_id: Optional[str] = None

    for line in run_shell_command(cmd):
        try:
            line_dict = json.loads(line.strip())
            all_messages.append(line_dict)
            item = line_dict.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                agent_messages = agent_messages + item.get("text", "")
            if line_dict.get("thread_id") is not None:
                thread_id = line_dict.get("thread_id")
            if "fail" in line_dict.get("type", ""):
                success = False
                err_message = "codex error: " + line_dict.get("error", {}).get("message", "")
                break
            if "error" in line_dict.get("type", ""):
                success = False
                err_message = "codex error: " + line_dict.get("message", "")   
        except json.JSONDecodeError as error:
            # Improved error handling: include problematic line
            err_message = line
            success = False
            break
        except Exception as error:
            err_message = f"Unexpected error: {error}. Line: {line!r}"
            success = False
            break

    if success and thread_id is None:
        success = False
        err_message = "Failed to get `SESSION_ID` from the codex session. \n\n" + err_message
        
    if success and len(agent_messages) == 0:
        success = False
        err_message = "Failed to get `agent_messages` from the codex session. \n\n You can try to set `return_all_messages` to `True` to get the full reasoning information. \n\n " + err_message

    if success:
        result: Dict[str, Any] = {
            "success": True,
            "SESSION_ID": thread_id,
            "agent_messages": agent_messages,
            # "PROMPT": PROMPT,
        }
        if return_all_messages:
            result["all_messages"] = all_messages
    else:
        result = {"success": False, "error": err_message}

    return result


def run() -> None:
    """Start the MCP server over stdio transport."""
    mcp.run(transport="stdio")
