"""把 cc-bridge 的两个 MCP server 写进各自的宿主配置文件.

- ``bridge-to-codex``（让 Claude 调 Codex）注册进 **Claude 桌面版** 的
  ``claude_desktop_config.json``（JSON 格式，整块由我们管理 ``mcpServers`` 字段）。
- ``bridge-to-claude``（让 Codex 调 Claude）注册进 **Codex** 的 ``config.toml``
  （TOML 格式，可能已有大量用户自定义内容，所以我们只在文件末尾追加一段
  用注释标记包裹的块，卸载时按标记精确删除，绝不触碰用户其它配置）。

所有方法都把 :class:`OSError` 兜住，失败时返回 ``success=False`` + 清晰 message，
绝不向上抛异常——安装器需要把结果直接展示给用户。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cc_bridge.bridge import config

# Codex config.toml 里包裹我们注册块的注释标记，卸载时据此精确删除。
_CODEX_MARKER_BEGIN = "# >>> cc-bridge (bridge-to-claude) >>>"
_CODEX_MARKER_END = "# <<< cc-bridge (bridge-to-claude) <<<"

# 两端要注册的 MCP server 名称、模块路径、console-script 名、frozen 自拉起标识。
_CLAUDE_SERVER_KEY = "bridge-to-codex"
_CLAUDE_SERVER_MODULE = "cc_bridge.bridge.mcp_to_codex"
_CLAUDE_SERVER_SCRIPT = "cc-bridge-mcp-codex"
_CLAUDE_SERVER_FROZEN_KEY = "codex"
_CODEX_SERVER_KEY = "bridge-to-claude"
_CODEX_SERVER_MODULE = "cc_bridge.bridge.mcp_to_claude"
_CODEX_SERVER_SCRIPT = "cc-bridge-mcp-claude"
_CODEX_SERVER_FROZEN_KEY = "claude"


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再 os.replace，避免写到一半崩溃导致文件被截断.

    这些配置文件由用户拥有、且可能正被桌面应用读取；非原子的「截断+重填」一旦中途
    失败会毁掉用户的整份 MCP 配置。同目录 + os.replace 在同一文件系统上是原子操作。
    """
    tmp = path.with_name(path.name + ".cc-bridge.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # 例如桌面应用正占用目标文件导致 os.replace 失败：清理临时文件再抛，
        # 避免在用户配置旁残留 .cc-bridge.tmp。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass
class ConfigChange:
    """一次配置写入 / 删除的结果，字段直接用于安装界面展示。"""

    target: str               # "Claude Desktop" / "Codex"
    success: bool
    already_present: bool      # 注册时：目标项已存在且内容一致
    config_path: str           # 实际操作的配置文件绝对路径
    message: str               # 给用户看的提示文字


class Configurator:
    """读写 Claude 桌面版与 Codex 的 MCP 配置。"""

    # -- Claude Desktop（JSON）-------------------------------------------
    def register_in_claude_desktop(self, env: dict[str, str] | None = None) -> ConfigChange:
        path = config.claude_desktop_config_path()
        target = "Claude Desktop"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            data: dict = {}
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                if raw.strip():
                    try:
                        loaded = json.loads(raw)
                    except json.JSONDecodeError:
                        return ConfigChange(
                            target=target,
                            success=False,
                            already_present=False,
                            config_path=str(path),
                            message=(
                                f"{path} 不是合法的 JSON，已保留原文件未做改动。"
                                "请手动检查后再重试。"
                            ),
                        )
                    if not isinstance(loaded, dict):
                        return ConfigChange(
                            target=target,
                            success=False,
                            already_present=False,
                            config_path=str(path),
                            message=(
                                f"{path} 顶层不是 JSON 对象（应为 {{...}}），"
                                "已保留原文件未做改动。请手动检查后再重试。"
                            ),
                        )
                    data = loaded

            try:
                command, args = config.mcp_launch_command(
                    _CLAUDE_SERVER_MODULE, _CLAUDE_SERVER_SCRIPT, _CLAUDE_SERVER_FROZEN_KEY
                )
            except RuntimeError as exc:
                return ConfigChange(
                    target=target,
                    success=False,
                    already_present=False,
                    config_path=str(path),
                    message=f"无法确定 MCP server 的启动命令：{exc}",
                )
            servers = data.get("mcpServers")
            if servers is None:
                servers = {}
                data["mcpServers"] = servers
            elif not isinstance(servers, dict):
                # mcpServers 存在但不是对象（如 []）——不要崩，也不要覆盖用户文件。
                return ConfigChange(
                    target=target,
                    success=False,
                    already_present=False,
                    config_path=str(path),
                    message=(
                        f"{path} 里的 mcpServers 不是对象（应为 {{...}}），"
                        "已保留原文件未做改动。请手动检查后再重试。"
                    ),
                )

            # 只更新我们拥有的 command/args，保留用户可能在该记录上加的 env 及其它扩展字段，
            # 避免静默清空用户配置；内容一致时不写盘也不谎报，与 Codex 端自愈语义对齐。
            existing = servers.get(_CLAUDE_SERVER_KEY)
            new_entry = dict(existing) if isinstance(existing, dict) else {}
            new_entry["command"] = command
            new_entry["args"] = args
            # 保留用户已有的 env；只有当它确实是 dict 时才合并，否则从空起步——
            # 避免用户把 env 写成非 dict（如字符串/列表）时 dict() 抛 TypeError 逃逸出
            # 本方法的 OSError 兜底（本文件约定：绝不向上抛异常）。
            existing_env = existing.get("env") if isinstance(existing, dict) else None
            base_env = dict(existing_env) if isinstance(existing_env, dict) else {}
            base_env.update(env or {})
            new_entry["env"] = base_env
            already_present = existing == new_entry

            if not already_present:
                servers[_CLAUDE_SERVER_KEY] = new_entry
                _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))

            if already_present:
                message = f"{_CLAUDE_SERVER_KEY} 已注册在 Claude 桌面版（内容未变）。"
            else:
                message = f"已把 {_CLAUDE_SERVER_KEY} 写入 Claude 桌面版配置。"
            return ConfigChange(
                target=target,
                success=True,
                already_present=already_present,
                config_path=str(path),
                message=message,
            )
        except OSError as exc:
            return ConfigChange(
                target=target,
                success=False,
                already_present=False,
                config_path=str(path),
                message=f"写入 Claude 桌面版配置失败：{exc}",
            )

    # -- Codex（TOML，追加注释包裹块）-----------------------------------
    def register_in_codex(self, env: dict[str, str] | None = None) -> ConfigChange:
        path = config.codex_config_path()
        target = "Codex"
        try:
            config.codex_home().mkdir(parents=True, exist_ok=True)

            existing_text = ""
            if path.exists():
                existing_text = path.read_text(encoding="utf-8")

            # 解析现有内容：判断 section 是否存在、文件是否合法。
            parse_ok = True
            section_present = False
            if existing_text.strip():
                try:
                    parsed = tomllib.loads(existing_text)
                    mcp_servers = parsed.get("mcp_servers")
                    section_present = (
                        isinstance(mcp_servers, dict) and _CODEX_SERVER_KEY in mcp_servers
                    )
                except tomllib.TOMLDecodeError:
                    parse_ok = False
                    section_present = f"[mcp_servers.{_CODEX_SERVER_KEY}]" in existing_text

            has_marker = (
                _CODEX_MARKER_BEGIN in existing_text and _CODEX_MARKER_END in existing_text
            )

            # 非法 TOML 且不是我们写的块：拒绝写入（与 Claude/JSON 端对齐），
            # 否则只会把用户已经损坏的文件搞得更糟，还谎报成功。
            if not parse_ok and not has_marker:
                return ConfigChange(
                    target=target,
                    success=False,
                    already_present=False,
                    config_path=str(path),
                    message=(
                        f"{path} 不是合法的 TOML，已保留原文件未做改动。"
                        "请手动检查后再重试。"
                    ),
                )

            try:
                command, args = config.mcp_launch_command(
                    _CODEX_SERVER_MODULE, _CODEX_SERVER_SCRIPT, _CODEX_SERVER_FROZEN_KEY
                )
            except RuntimeError as exc:
                return ConfigChange(
                    target=target,
                    success=False,
                    already_present=False,
                    config_path=str(path),
                    message=f"无法确定 MCP server 的启动命令：{exc}",
                )
            new_block = _build_codex_block(command, args, env)

            # 用户自己注册了同名 section（没有我们的标记）：尊重它，不覆盖。
            if section_present and not has_marker:
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=True,
                    config_path=str(path),
                    message=(
                        f"检测到已有 [mcp_servers.{_CODEX_SERVER_KEY}]（非 cc-bridge 写入），"
                        "已跳过，未覆盖。"
                    ),
                )

            # 我们之前写过：内容一致则跳过；不一致则刷新（如 Python 路径变了），
            # 与 Claude 端「总是覆盖成最新」的自愈语义对齐。
            if has_marker:
                current_block = _extract_codex_block(existing_text)
                if current_block.strip() == new_block.strip():
                    return ConfigChange(
                        target=target,
                        success=True,
                        already_present=True,
                        config_path=str(path),
                        message=f"{_CODEX_SERVER_KEY} 已注册在 Codex（内容未变）。",
                    )
                refreshed = _append_codex_block(_strip_codex_block(existing_text), new_block)
                _atomic_write_text(path, refreshed)
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=False,
                    config_path=str(path),
                    message=f"已更新 Codex 中 {_CODEX_SERVER_KEY} 的启动命令。",
                )

            _atomic_write_text(path, _append_codex_block(existing_text, new_block))
            return ConfigChange(
                target=target,
                success=True,
                already_present=False,
                config_path=str(path),
                message=f"已把 {_CODEX_SERVER_KEY} 追加进 Codex 配置。",
            )
        except OSError as exc:
            return ConfigChange(
                target=target,
                success=False,
                already_present=False,
                config_path=str(path),
                message=f"写入 Codex 配置失败：{exc}",
            )

    # -- 卸载 -------------------------------------------------------------
    def unregister_claude_desktop(self) -> ConfigChange:
        path = config.claude_desktop_config_path()
        target = "Claude Desktop"
        try:
            if not path.exists():
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=False,
                    config_path=str(path),
                    message="Claude 桌面版配置不存在，无需卸载。",
                )

            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=False,
                    config_path=str(path),
                    message="Claude 桌面版配置为空，无需卸载。",
                )

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return ConfigChange(
                    target=target,
                    success=False,
                    already_present=False,
                    config_path=str(path),
                    message=(
                        f"{path} 不是合法的 JSON，已保留原文件未做改动。"
                        "请手动检查后再重试。"
                    ),
                )

            if not isinstance(data, dict):
                return ConfigChange(
                    target=target,
                    success=False,
                    already_present=False,
                    config_path=str(path),
                    message=(
                        f"{path} 顶层不是 JSON 对象，已保留原文件未做改动。"
                        "请手动检查后再重试。"
                    ),
                )

            servers = data.get("mcpServers")
            if not isinstance(servers, dict) or _CLAUDE_SERVER_KEY not in servers:
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=False,
                    config_path=str(path),
                    message=f"Claude 桌面版未注册 {_CLAUDE_SERVER_KEY}，无需卸载。",
                )

            del servers[_CLAUDE_SERVER_KEY]
            _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))
            return ConfigChange(
                target=target,
                success=True,
                already_present=False,
                config_path=str(path),
                message=f"已从 Claude 桌面版移除 {_CLAUDE_SERVER_KEY}。",
            )
        except OSError as exc:
            return ConfigChange(
                target=target,
                success=False,
                already_present=False,
                config_path=str(path),
                message=f"卸载 Claude 桌面版配置失败：{exc}",
            )

    def unregister_codex(self) -> ConfigChange:
        path = config.codex_config_path()
        target = "Codex"
        try:
            if not path.exists():
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=False,
                    config_path=str(path),
                    message="Codex 配置不存在，无需卸载。",
                )

            text = path.read_text(encoding="utf-8")

            if _CODEX_MARKER_BEGIN in text and _CODEX_MARKER_END in text:
                stripped = _strip_codex_block(text)
                _atomic_write_text(path, stripped)
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=False,
                    config_path=str(path),
                    message=f"已从 Codex 配置移除 {_CODEX_SERVER_KEY}。",
                )

            # 没有标记：可能是用户手动注册的，或本来就没注册。
            if f"[mcp_servers.{_CODEX_SERVER_KEY}]" in text:
                return ConfigChange(
                    target=target,
                    success=True,
                    already_present=False,
                    config_path=str(path),
                    message=(
                        f"检测到 [mcp_servers.{_CODEX_SERVER_KEY}] 但未发现 cc-bridge 标记，"
                        "请手动移除该段配置。"
                    ),
                )
            return ConfigChange(
                target=target,
                success=True,
                already_present=False,
                config_path=str(path),
                message=f"Codex 未注册 {_CODEX_SERVER_KEY}，无需卸载。",
            )
        except OSError as exc:
            return ConfigChange(
                target=target,
                success=False,
                already_present=False,
                config_path=str(path),
                message=f"卸载 Codex 配置失败：{exc}",
            )

    # -- 批量 -------------------------------------------------------------
    def register_all(self, env: dict[str, str] | None = None) -> list:
        """注册全部，顺序为 [Claude Desktop, Codex]。"""
        return [self.register_in_claude_desktop(env=env), self.register_in_codex(env=env)]

    def unregister_all(self) -> list:
        """卸载全部，顺序为 [Claude Desktop, Codex]。"""
        return [self.unregister_claude_desktop(), self.unregister_codex()]


# ---------------------------------------------------------------------------
# Codex TOML 块的拼装与剥离
# ---------------------------------------------------------------------------

def _toml_literal_string(value: str) -> str:
    """用 TOML 字面量字符串（单引号）包裹路径，避免 Windows 反斜杠被转义。

    TOML 字面量字符串不做任何转义，但不能包含单引号字符；万一路径里真有单引号，
    退回到基本字符串并转义反斜杠与引号。
    """
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _append_codex_block(base: str, block: str) -> str:
    """把注册块追加到 base 文本末尾，中间留一个空行分隔。"""
    new = base
    if new and not new.endswith("\n"):
        new += "\n"
    if new:
        new += "\n"
    return new + block


def _extract_codex_block(text: str) -> str:
    """取出标记之间（含标记行）的整块文本；找不到返回空串。"""
    begin = text.find(_CODEX_MARKER_BEGIN)
    end = text.find(_CODEX_MARKER_END)
    if begin == -1 or end == -1:
        return ""
    return text[begin : end + len(_CODEX_MARKER_END)]


def _build_codex_block(
    command: str, args: list[str], env: dict[str, str] | None = None
) -> str:
    """拼出带注释标记的 Codex MCP 注册块（以换行结尾）。"""
    args_literals = ", ".join(_toml_literal_string(a) for a in args)
    env_line = ""
    if env:
        env_literals = ", ".join(
            f"{key} = {_toml_literal_string(str(value))}" for key, value in env.items()
        )
        env_line = f"env = {{ {env_literals} }}\n"
    return (
        f"{_CODEX_MARKER_BEGIN}\n"
        f"[mcp_servers.{_CODEX_SERVER_KEY}]\n"
        f"command = {_toml_literal_string(command)}\n"
        f"args = [{args_literals}]\n"
        f"{env_line}"
        f"{_CODEX_MARKER_END}\n"
    )


def _strip_codex_block(text: str) -> str:
    """删除标记之间（含标记行）的全部内容，并清理多余空行。"""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == _CODEX_MARKER_BEGIN:
            skipping = True
            continue
        if stripped == _CODEX_MARKER_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    result = "".join(out)
    # 去掉删除块后可能残留的尾部连续空行，但保留单个换行结尾。
    result = result.rstrip("\n")
    if result:
        result += "\n"
    return result
