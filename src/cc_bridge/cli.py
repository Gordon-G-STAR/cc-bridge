"""cc-bridge 的精简命令行入口.

主要供两类场景使用：

1. **GUI 安装器内部**：检测环境、写配置、跑连通性测试，都复用这里的逻辑。
2. **诊断 / CI**：``cc-bridge status`` 只读检测，``cc-bridge doctor`` 同义。

它本身不是给最终用户每天敲的（普通用户走 GUI），但提供一个无界面的等价入口，
方便排查问题和自动化测试。

子命令：
    cc-bridge status      检测 Claude / Codex 是否就绪（只读）
    cc-bridge install     把桥接 MCP 写入两边配置（可加 --no-test 跳过连通测试）
    cc-bridge uninstall   从两边配置移除桥接 MCP
    cc-bridge test        实际各调用一次，验证双向连通
    cc-bridge version     打印版本
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from cc_bridge import __version__


def _print_env(status) -> None:
    for check in (status.claude, status.codex):
        if check.ready and check.login_known:
            mark = "✅"
        elif check.ready:
            mark = "⚠️"   # CLI 可用但登录态无法确认
        else:
            mark = "❌"
        ver = f"（{check.version}）" if check.version else ""
        print(f"  {mark} {check.name}{ver}：{check.message}")


def cmd_status(_args: argparse.Namespace) -> int:
    from cc_bridge.installer.detector import EnvironmentDetector

    status = EnvironmentDetector().check_all()
    print("环境检测：")
    _print_env(status)
    unknown = [c.name for c in (status.claude, status.codex) if c.ready and not c.login_known]
    if status.all_ready:
        if unknown:
            print(f"\n两端命令行均可用；但 {('、'.join(unknown))} 的登录态无法确认，请确保已登录。")
        else:
            print("\n两端均已就绪。")
        return 0
    print("\n仍有未就绪项，请按上面的提示处理后重试。")
    return 1


def cmd_install(args: argparse.Namespace) -> int:
    from cc_bridge.installer.configurator import Configurator
    from cc_bridge.installer.detector import EnvironmentDetector

    status = EnvironmentDetector().check_all()
    print("环境检测：")
    _print_env(status)
    if not status.all_ready and not args.force:
        print("\n检测未全部通过。如确认无误，可加 --force 强制写入配置。")
        return 1

    install_env = {}
    if args.allowed_roots is not None:
        install_env["CC_BRIDGE_ALLOWED_ROOTS"] = args.allowed_roots
    if args.codex_sandbox is not None:
        install_env["CC_BRIDGE_CODEX_SANDBOX"] = args.codex_sandbox
    if args.audit_log is not None:
        install_env["CC_BRIDGE_AUDIT_LOG"] = args.audit_log

    print("\n注册桥接插件：")
    changes = Configurator().register_all(env=install_env or None)
    ok = True
    for change in changes:
        mark = "✅" if change.success else "❌"
        suffix = "（已存在，跳过）" if change.already_present else ""
        print(f"  {mark} {change.target}{suffix}：{change.message}")
        ok = ok and change.success
    if not ok:
        return 1
    if install_env:
        print("已写入安全配置：" + ", ".join(install_env))

    if not args.no_test:
        print("\nMCP 启动自检：")
        rc_self = cmd_selftest(args)
        print("\n连通性测试：")
        rc_conn = cmd_test(args)
        if rc_self != 0 or rc_conn != 0:
            return rc_self or rc_conn

    print("\n完成。请重启 Claude 和 Codex 桌面版以加载桥接插件。")
    return 0


def cmd_uninstall(_args: argparse.Namespace) -> int:
    from cc_bridge.installer.configurator import Configurator

    print("移除桥接插件：")
    changes = Configurator().unregister_all()
    ok = True
    for change in changes:
        mark = "✅" if change.success else "❌"
        print(f"  {mark} {change.target}：{change.message}")
        ok = ok and change.success
    return 0 if ok else 1


def cmd_test(_args: argparse.Namespace) -> int:
    from cc_bridge.installer.tester import ConnectivityTester

    outcomes = ConnectivityTester().run_all()
    ok = True
    for outcome in outcomes:
        mark = "✅" if outcome.success else "❌"
        print(f"  {mark} {outcome.direction}（{outcome.duration_seconds:.1f}s）：{outcome.detail}")
        ok = ok and outcome.success
    return 0 if ok else 1


def cmd_selftest(_args: argparse.Namespace) -> int:
    """启动自检：把宿主真正使用的 `<launcher> --mcp-server <key>` 拉起来做一次 MCP 握手。

    这是连通性测试覆盖不到的盲区——它走的是进程内 CLI 调用，从不启动真正的 stdio
    server。历史上 --windowed 打包把 server 打挂、连通性测试却全绿，就是因为缺这一步。
    """
    from cc_bridge.installer.mcp_selftest import selftest_all

    results = selftest_all()
    ok = True
    for r in results:
        mark = "✅" if r.success else "❌"
        print(f"  {mark} {r.host}（{r.duration_seconds:.1f}s）：{r.detail}")
        ok = ok and r.success
    return 0 if ok else 1


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"cc-bridge {__version__}")
    return 0


def cmd_checklist_run(args: argparse.Namespace) -> int:
    from cc_bridge import checklist
    from cc_bridge.bridge.context import require_project_dir

    try:
        project_dir = require_project_dir(args.project_dir)
    except ValueError as exc:
        print(f"project_dir 错误：{exc}", file=sys.stderr)
        return 2

    try:
        checklist_text = Path(args.checklist).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取 checklist 失败：{exc}", file=sys.stderr)
        return 2

    items = checklist.parse_checklist(checklist_text)
    results = asyncio.run(
        checklist.run_checklist(
            items,
            project_dir,
            agent=args.agent,
            timeout=args.timeout,
        )
    )
    report = checklist.render_report(project_dir, results)

    if args.report:
        try:
            Path(args.report).write_text(report, encoding="utf-8")
        except OSError as exc:
            print(f"写入报告失败：{exc}", file=sys.stderr)
            return 2
    else:
        print(report, end="")

    if any(result.status in {"missing", "error"} or result.risk == "high" for result in results):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cc-bridge", description="Claude x Codex 自动协作桥")
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="检测两端是否就绪（只读）")
    p_status.set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="status 的别名").set_defaults(func=cmd_status)

    p_install = sub.add_parser("install", help="写入桥接 MCP 配置")
    p_install.add_argument("--no-test", action="store_true", help="跳过连通性测试")
    p_install.add_argument("--force", action="store_true", help="检测未通过也强制写入")
    p_install.add_argument("--allowed-roots", help="写入 CC_BRIDGE_ALLOWED_ROOTS")
    p_install.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="写入 CC_BRIDGE_CODEX_SANDBOX",
    )
    p_install.add_argument("--audit-log", help="写入 CC_BRIDGE_AUDIT_LOG")
    p_install.set_defaults(func=cmd_install)

    sub.add_parser("uninstall", help="移除桥接 MCP 配置").set_defaults(func=cmd_uninstall)
    sub.add_parser("test", help="实际调用一次，验证双向连通").set_defaults(func=cmd_test)
    sub.add_parser(
        "selftest", help="启动自检：拉起 <launcher> --mcp-server 做一次 MCP 握手"
    ).set_defaults(func=cmd_selftest)
    sub.add_parser("version", help="打印版本").set_defaults(func=cmd_version)

    p_checklist = sub.add_parser("checklist-run", help="逐项短调用执行验收清单")
    p_checklist.add_argument("--checklist", required=True, help="验收清单 Markdown 文件")
    p_checklist.add_argument("--project-dir", required=True, help="项目绝对路径")
    p_checklist.add_argument(
        "--agent",
        choices=["claude", "codex"],
        default="claude",
        help="语义项检查使用的 agent",
    )
    p_checklist.add_argument("--report", help="报告输出路径；不传则打印到 stdout")
    p_checklist.add_argument("--timeout", type=int, default=300, help="每项超时秒数")
    p_checklist.set_defaults(func=cmd_checklist_run)

    return parser


def _force_utf8_output() -> None:
    """让标准输出走 UTF-8.

    中文版 Windows 控制台默认是 GBK，直接 print 含 ✅/❌ 等字符会抛
    UnicodeEncodeError 把命令打崩。errors="replace" 保证最坏情况也只是显示乱码，
    而不是崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
