"""图形安装向导（tkinter）.

把 cc-bridge 的安装流程包装成一个 5 步向导：
    1. 欢迎
    2. 环境检测（EnvironmentDetector）
    3. 注册插件（Configurator）
    4. 连通性测试（ConnectivityTester，后台线程跑，避免卡 UI）
    5. 完成

设计约束（重要）：
- 模块顶层 **绝不** 创建任何 Tk() 或控件——只做 import。这样在无界面 / headless
  环境以及单元测试里都能安全 ``import cc_bridge.installer.main``。
- 所有 tkinter 相关构造都放在函数 / 方法里。
- ``main()`` 先尝试创建 Tk()；若失败（没有显示器等），打印说明并回退到 CLI 的
  ``install`` 子命令。
"""

from __future__ import annotations

import os
import sys
import threading

# 仅做「谁来执行业务逻辑」的导入；tkinter 在 main()/方法里再导入，
# 以保证顶层 import 不触碰任何 GUI 依赖。
from cc_bridge.installer.configurator import Configurator
from cc_bridge.installer.detector import EnvironmentDetector
from cc_bridge.installer.tester import ConnectivityTester


# ---- 视觉常量 -----------------------------------------------------------
WINDOW_SIZE = "520x420"
BG = "#ffffff"
SIDEBAR_BG = "#f3f4f6"
ACCENT = "#2563eb"
MUTED = "#6b7280"
OK_COLOR = "#16a34a"
ERR_COLOR = "#dc2626"

STEP_TITLES = [
    "欢迎",
    "环境检测",
    "注册插件",
    "连通性测试",
    "完成",
]


class InstallerApp:
    """tkinter 图形安装向导.

    用法::

        import tkinter as tk
        root = tk.Tk()
        app = InstallerApp(root)
        root.mainloop()

    所有控件都在 ``__init__`` 里构造（此时已经拿到一个真实的 root），
    模块顶层不会创建任何窗口。
    """

    def __init__(self, root):
        # 延迟到这里再导入 tkinter 子模块，方便顶层 import 时零 GUI 依赖。
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root

        self.current_step = 0
        self.env_status = None          # 最近一次 EnvironmentDetector 结果
        self._test_thread = None        # 连通性测试后台线程
        self._test_results = None       # 线程回填的结果（list[TestOutcome]）
        self._test_error = None         # 线程里的异常信息
        self._test_skipped = False      # 用户是否点了「跳过测试」
        self._poll_job = None           # root.after 轮询句柄

        self.root.title("cc-bridge 安装向导")
        try:
            self.root.geometry(WINDOW_SIZE)
            self.root.configure(bg=BG)
            self.root.minsize(520, 420)
        except Exception:
            # 某些精简环境下 geometry/configure 可能不可用，不致命。
            pass

        self._build_layout()
        self._render_step()

    # ---- 布局骨架 ------------------------------------------------------
    def _build_layout(self) -> None:
        tk = self.tk

        # 左侧步骤导航
        self.sidebar = tk.Frame(self.root, bg=SIDEBAR_BG, width=140)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        title = tk.Label(
            self.sidebar, text="cc-bridge", bg=SIDEBAR_BG, fg=ACCENT,
            font=("Segoe UI", 13, "bold"),
        )
        title.pack(anchor="w", padx=16, pady=(18, 12))

        self._step_labels = []
        for idx, name in enumerate(STEP_TITLES):
            lbl = tk.Label(
                self.sidebar, text=f"{idx + 1}. {name}", bg=SIDEBAR_BG,
                fg=MUTED, font=("Segoe UI", 10), anchor="w", justify="left",
            )
            lbl.pack(anchor="w", padx=16, pady=4, fill="x")
            self._step_labels.append(lbl)

        # 右侧内容区 + 底部按钮条
        self.right = tk.Frame(self.root, bg=BG)
        self.right.pack(side="left", fill="both", expand=True)

        self.content = tk.Frame(self.right, bg=BG)
        self.content.pack(side="top", fill="both", expand=True, padx=20, pady=20)

        self.buttons = tk.Frame(self.right, bg=BG)
        self.buttons.pack(side="bottom", fill="x", padx=20, pady=(0, 16))

        self.back_btn = self.ttk.Button(self.buttons, text="上一步", command=self._go_back)
        self.back_btn.pack(side="left")

        self.next_btn = self.ttk.Button(self.buttons, text="下一步", command=self._go_next)
        self.next_btn.pack(side="right")

        # 步骤4 专用的「跳过测试」按钮，平时隐藏。
        self.skip_btn = self.ttk.Button(self.buttons, text="跳过测试", command=self._skip_test)

    def _update_sidebar(self) -> None:
        for idx, lbl in enumerate(self._step_labels):
            if idx == self.current_step:
                lbl.configure(fg=ACCENT, font=("Segoe UI", 10, "bold"))
            elif idx < self.current_step:
                lbl.configure(fg=OK_COLOR, font=("Segoe UI", 10))
            else:
                lbl.configure(fg=MUTED, font=("Segoe UI", 10))

    def _clear_content(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()

    # ---- 通用小控件 ----------------------------------------------------
    def _heading(self, text: str):
        lbl = self.tk.Label(
            self.content, text=text, bg=BG, fg="#111827",
            font=("Segoe UI", 15, "bold"), anchor="w", justify="left",
        )
        lbl.pack(anchor="w", pady=(0, 12), fill="x")
        return lbl

    def _paragraph(self, text: str, fg: str = "#374151"):
        lbl = self.tk.Label(
            self.content, text=text, bg=BG, fg=fg, font=("Segoe UI", 10),
            anchor="w", justify="left", wraplength=320,
        )
        lbl.pack(anchor="w", pady=3, fill="x")
        return lbl

    # ---- 步骤分发 ------------------------------------------------------
    def _render_step(self) -> None:
        self._update_sidebar()
        self._clear_content()

        # 默认按钮状态，每步可覆盖。
        self.skip_btn.pack_forget()
        try:
            self.back_btn.configure(state=("disabled" if self.current_step == 0 else "normal"))
            self.next_btn.configure(state="normal", text="下一步")
        except Exception:
            pass

        renderers = [
            self._render_welcome,
            self._render_detect,
            self._render_register,
            self._render_test,
            self._render_done,
        ]
        try:
            renderers[self.current_step]()
        except Exception as exc:  # 任何渲染异常都不要让窗口崩。
            self._paragraph(f"页面加载出错：{exc}", fg=ERR_COLOR)

    # ---- 步骤1：欢迎 ---------------------------------------------------
    def _render_welcome(self) -> None:
        self._heading("欢迎使用 cc-bridge")
        self._paragraph(
            "这个向导会帮你把 Claude 和 Codex 桌面版连接起来，"
            "让它们可以互相调用、自动协作。"
        )
        self._paragraph("")
        self._paragraph("接下来会依次：")
        self._paragraph("  • 检测两端是否已安装并登录")
        self._paragraph("  • 把桥接插件写入各自的配置")
        self._paragraph("  • 实际跑一次双向连通性测试")
        self._paragraph("")
        self._paragraph("点击「下一步」开始。", fg=MUTED)

    # ---- 步骤2：环境检测 -----------------------------------------------
    def _render_detect(self) -> None:
        self._heading("环境检测")
        self._paragraph("正在检测 Claude 与 Codex 是否就绪……", fg=MUTED)
        # 先把界面画出来，再异步触发检测，避免初次进入时卡顿感。
        self.root.after(50, self._run_detection)

    def _run_detection(self) -> None:
        try:
            self.env_status = EnvironmentDetector().check_all()
        except Exception as exc:
            self.env_status = None
            self._clear_content()
            self._heading("环境检测")
            self._paragraph(f"检测过程出错：{exc}", fg=ERR_COLOR)
            self._add_redetect_button()
            try:
                self.next_btn.configure(state="disabled")
            except Exception:
                pass
            return
        self._show_detection_result()

    def _show_detection_result(self) -> None:
        self._clear_content()
        self._heading("环境检测")

        status = self.env_status
        checks = [status.claude, status.codex] if status else []
        for check in checks:
            mark = "✅" if check.ready else "❌"
            color = OK_COLOR if check.ready else ERR_COLOR
            ver = f"（{check.version}）" if check.version else ""
            row = self.tk.Label(
                self.content, text=f"{mark} {check.name}{ver}：{check.message}",
                bg=BG, fg=color, font=("Segoe UI", 10), anchor="w",
                justify="left", wraplength=320,
            )
            row.pack(anchor="w", pady=4, fill="x")

        all_ready = bool(status) and status.all_ready
        if all_ready:
            self._paragraph("")
            self._paragraph("两端均已就绪，点击「下一步」继续。", fg=OK_COLOR)
            try:
                self.next_btn.configure(state="normal")
            except Exception:
                pass
        else:
            self._paragraph("")
            self._paragraph(
                "尚有未就绪项。请按上面提示安装对应桌面版并登录，"
                "然后点「重新检测」。",
                fg=ERR_COLOR,
            )
            try:
                self.next_btn.configure(state="disabled")
            except Exception:
                pass

        self._add_redetect_button()

    def _add_redetect_button(self) -> None:
        btn = self.ttk.Button(self.content, text="重新检测", command=self._redetect)
        btn.pack(anchor="w", pady=(12, 0))

    def _redetect(self) -> None:
        self._clear_content()
        self._heading("环境检测")
        self._paragraph("正在重新检测……", fg=MUTED)
        self.root.after(50, self._run_detection)

    # ---- 步骤3：注册插件 -----------------------------------------------
    def _render_register(self) -> None:
        self._heading("注册桥接插件")
        self._paragraph("正在把桥接 MCP 写入两端配置……", fg=MUTED)
        try:
            self.next_btn.configure(state="disabled")
        except Exception:
            pass
        self.root.after(50, self._run_registration)

    def _run_registration(self) -> None:
        try:
            changes = Configurator().register_all()
        except Exception as exc:
            self._clear_content()
            self._heading("注册桥接插件")
            self._paragraph(f"写入配置时出错：{exc}", fg=ERR_COLOR)
            self._add_reregister_button()
            return

        self._clear_content()
        self._heading("注册桥接插件")
        all_ok = True
        for change in changes:
            ok = bool(getattr(change, "success", False))
            all_ok = all_ok and ok
            mark = "✅" if ok else "❌"
            color = OK_COLOR if ok else ERR_COLOR
            suffix = "（已存在，跳过）" if getattr(change, "already_present", False) else ""
            target = getattr(change, "target", "?")
            message = getattr(change, "message", "")
            row = self.tk.Label(
                self.content, text=f"{mark} {target}{suffix}：{message}",
                bg=BG, fg=color, font=("Segoe UI", 10), anchor="w",
                justify="left", wraplength=320,
            )
            row.pack(anchor="w", pady=4, fill="x")

        self._paragraph("")
        if all_ok:
            self._paragraph("插件已注册，点击「下一步」进行连通性测试。", fg=OK_COLOR)
            try:
                self.next_btn.configure(state="normal")
            except Exception:
                pass
        else:
            self._paragraph("部分配置写入失败，可点「重试」再写一次。", fg=ERR_COLOR)
            self._add_reregister_button()
            # 允许用户仍然继续（例如某一端确实未安装），不强制阻塞。
            try:
                self.next_btn.configure(state="normal")
            except Exception:
                pass

    def _add_reregister_button(self) -> None:
        btn = self.ttk.Button(self.content, text="重试", command=self._render_register)
        btn.pack(anchor="w", pady=(12, 0))

    # ---- 步骤4：连通性测试 ---------------------------------------------
    def _render_test(self) -> None:
        self._heading("连通性测试")
        self._paragraph(
            "正在实际各调用一次，验证双向连通。这一步可能需要十几秒到一分钟……",
            fg=MUTED,
        )
        self._test_progress = self._paragraph("⏳ 测试进行中，请稍候。", fg=MUTED)

        # 测试期间禁用下一步，展示「跳过测试」。
        try:
            self.next_btn.configure(state="disabled")
        except Exception:
            pass
        self.skip_btn.pack(side="right", padx=(0, 8))

        self._test_results = None
        self._test_error = None
        self._test_skipped = False
        # 存引用：用户「跳过测试」/关窗时据此真正取消后台 CLI 调用，避免它继续烧额度。
        self._tester = ConnectivityTester()
        # 代次令牌：被取消的旧 worker 延迟返回时，不得覆盖新一轮的结果（共享字段竞态）。
        self._test_run_id = getattr(self, "_test_run_id", 0) + 1
        run_id = self._test_run_id

        self._test_thread = threading.Thread(
            target=self._test_worker, args=(run_id,), daemon=True
        )
        self._test_thread.start()
        self._poll_job = self.root.after(200, self._poll_test)

    def _test_worker(self, run_id: int) -> None:
        """后台线程：先做 MCP 启动自检，再跑可取消的连通性测试。仅当仍是当前代次时
        才回填结果，避免旧（已取消）worker 延迟返回覆盖新一轮的成功结果。

        启动自检验证的是【宿主真正使用】的 ``<exe> --mcp-server <key>`` stdio 路径
        （连通性测试只走进程内 CLI 调用，覆盖不到它）；这正是历史上 --windowed 打包
        把 server 打挂、却没被任何自检拦住的盲区。"""
        try:
            selftest_rows = self._run_mcp_selftest()
            result = self._tester.run_all_cancellable()
        except Exception as exc:  # noqa: BLE001 — 线程内异常需带回主线程展示
            if run_id == self._test_run_id:
                self._test_error = exc
            return
        if run_id == self._test_run_id:
            if result is None:  # 连通性测试被取消：仍展示已拿到的启动自检结果
                self._test_results = selftest_rows or None
            else:
                self._test_results = selftest_rows + list(result)

    def _run_mcp_selftest(self) -> list:
        """跑 MCP 启动自检，转成 TestOutcome 行供 :meth:`_show_test_outcomes` 直接展示。

        自检本身永不拖垮整个测试流程：任何异常都收成一行失败结果。"""
        from cc_bridge.installer.tester import TestOutcome

        rows: list = []
        try:
            from cc_bridge.installer import mcp_selftest

            for r in mcp_selftest.selftest_all():
                rows.append(
                    TestOutcome(
                        direction=f"启动自检 · {r.host}",
                        success=r.success,
                        detail=r.detail,
                        duration_seconds=r.duration_seconds,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                TestOutcome(
                    direction="启动自检",
                    success=False,
                    detail=f"自检过程出错：{exc}",
                    duration_seconds=0.0,
                )
            )
        return rows

    def _cancel_tester(self) -> None:
        """尽力取消正在跑的连通性测试（含其已启动的子进程）。"""
        tester = getattr(self, "_tester", None)
        if tester is not None:
            try:
                tester.cancel()
            except Exception:
                pass

    def _poll_test(self) -> None:
        """主线程轮询：用 root.after 周期检查后台线程是否结束。"""
        if self._test_skipped:
            return  # 已跳过，停止轮询。

        thread = self._test_thread
        if thread is not None and thread.is_alive():
            self._poll_job = self.root.after(200, self._poll_test)
            return

        # 线程已结束，回填结果。
        if self._test_error is not None:
            self._show_test_outcomes(error=self._test_error)
        else:
            self._show_test_outcomes(outcomes=self._test_results or [])

    def _show_test_outcomes(self, outcomes=None, error=None) -> None:
        self._clear_content()
        self._heading("连通性测试")
        self.skip_btn.pack_forget()

        if error is not None:
            self._paragraph(f"测试过程出错：{error}", fg=ERR_COLOR)
            self._paragraph("")
            self._paragraph("你可以点「重试」再试，或直接「下一步」完成安装。", fg=MUTED)
            self._add_retest_button()
            try:
                self.next_btn.configure(state="normal")
            except Exception:
                pass
            return

        all_ok = True
        for outcome in outcomes or []:
            ok = bool(getattr(outcome, "success", False))
            all_ok = all_ok and ok
            mark = "✅" if ok else "❌"
            color = OK_COLOR if ok else ERR_COLOR
            direction = getattr(outcome, "direction", "?")
            detail = getattr(outcome, "detail", "")
            dur = getattr(outcome, "duration_seconds", 0.0) or 0.0
            row = self.tk.Label(
                self.content,
                text=f"{mark} {direction}（{dur:.1f}s）：{detail}",
                bg=BG, fg=color, font=("Segoe UI", 10), anchor="w",
                justify="left", wraplength=320,
            )
            row.pack(anchor="w", pady=4, fill="x")

        self._paragraph("")
        if all_ok and outcomes:
            self._paragraph("双向连通正常，点击「下一步」完成。", fg=OK_COLOR)
        else:
            self._paragraph(
                "部分方向未通过。你可以「重试」，"
                "或仍然「下一步」完成安装（重启桌面版后通常会恢复）。",
                fg=ERR_COLOR,
            )
            self._add_retest_button()

        try:
            self.next_btn.configure(state="normal")
        except Exception:
            pass

    def _add_retest_button(self) -> None:
        btn = self.ttk.Button(self.content, text="重试", command=self._render_test)
        btn.pack(anchor="w", pady=(12, 0))

    def _skip_test(self) -> None:
        """用户点「跳过测试」：取消后台测试、停止轮询，直接放行到完成步。"""
        self._test_skipped = True
        self._cancel_tester()  # 真正终止后台 CLI 调用，别让它继续烧额度
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        self._clear_content()
        self._heading("连通性测试")
        self.skip_btn.pack_forget()
        self._paragraph("已跳过连通性测试。", fg=MUTED)
        self._paragraph("")
        self._paragraph("点击「下一步」完成安装。", fg=MUTED)
        try:
            self.next_btn.configure(state="normal")
        except Exception:
            pass

    # ---- 步骤5：完成 ---------------------------------------------------
    def _render_done(self) -> None:
        self._heading("安装完成 🎉")
        self._paragraph("桥接插件已写入 Claude 与 Codex 的配置。")
        self._paragraph("")
        self._paragraph(
            "请重启 Claude 和 Codex 桌面版，让它们重新加载桥接插件。",
            fg=ACCENT,
        )
        self._paragraph("")
        self._paragraph("重启后，两端即可互相调用、自动协作。", fg=MUTED)
        try:
            self.next_btn.configure(text="关闭", state="normal")
        except Exception:
            pass

    # ---- 导航 ----------------------------------------------------------
    def _go_next(self) -> None:
        try:
            # 离开当前步前，取消可能仍在跑的连通性测试（避免后台 CLI 继续烧额度）。
            self._cancel_tester()
            if self.current_step >= len(STEP_TITLES) - 1:
                self._quit()
                return
            self.current_step += 1
            self._render_step()
        except Exception as exc:
            self._safe_error("继续时出错", exc)

    def _go_back(self) -> None:
        try:
            # 离开测试步时，既要停止轮询，也要真正取消后台测试（含已启动的子进程）。
            self._cancel_tester()
            if self._poll_job is not None:
                try:
                    self.root.after_cancel(self._poll_job)
                except Exception:
                    pass
                self._poll_job = None
                self._test_skipped = True
            if self.current_step > 0:
                self.current_step -= 1
                self._render_step()
        except Exception as exc:
            self._safe_error("返回时出错", exc)

    def _safe_error(self, title: str, exc: Exception) -> None:
        try:
            from tkinter import messagebox
            messagebox.showerror(title, str(exc))
        except Exception:
            # 连 messagebox 都失败时，退化为在内容区显示。
            try:
                self._paragraph(f"{title}：{exc}", fg=ERR_COLOR)
            except Exception:
                pass

    def _quit(self) -> None:
        # 关窗前先取消可能仍在跑的连通性测试，避免留下后台子进程继续烧额度。
        self._cancel_tester()
        try:
            if self._poll_job is not None:
                self.root.after_cancel(self._poll_job)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def _log_mcp_fatal(msg: str) -> None:
    """MCP server 模式下无法用 stdout（会污染协议或为 None）时，把致命错误落到日志文件。"""
    try:
        from cc_bridge.bridge.config import stable_app_dir
        d = stable_app_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "mcp-error.log").write_text(msg + "\n", encoding="utf-8")
    except Exception:
        pass
    try:
        import sys as _sys
        if _sys.stderr is not None:
            print(msg, file=_sys.stderr)
    except Exception:
        pass


def _clear_mcp_fatal_log() -> None:
    """MCP server 即将正常启动时，清掉可能残留的【过期】失败日志。

    ``mcp-error.log`` 只在下一次致命错误时才会被覆盖（见 :func:`_log_mcp_fatal`
    的 write_text），所以一旦某次 ``--windowed`` 打包失败写过它，即便后来换成
    console 版能正常启动，旧日志仍会长期留在原地、把排查带偏（本项目就踩过：
    日志显示 stdin/stdout 不可用，实际 exe 早已修好）。能走到这里说明 stdin/stdout
    可用、server 即将正常 serve，正是清掉过期日志、避免它继续误导的时机。
    """
    try:
        from cc_bridge.bridge.config import stable_app_dir
        (stable_app_dir() / "mcp-error.log").unlink(missing_ok=True)
    except Exception:
        pass


def _hide_console_window() -> bool:
    """仅在【frozen 打包 + Windows】下隐藏控制台黑窗；返回是否真的执行了隐藏。

    **必须有 frozen 守卫**：``cc-bridge-install`` 这个 console-script 也指向同一个 main()，
    若用户从 PowerShell/cmd 直接运行（非 frozen），无守卫会把用户【当前的终端窗口】一起藏掉。
    只有 PyInstaller console 模式打成的 exe 才需要隐藏自己启动时弹出的黑窗。

    隐藏窗口不会关闭 stdin/stdout 管道，所以 MCP stdio 通信照常工作。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
            return True
    except Exception:
        pass
    return False


def _run_mcp_server(which: str) -> int:
    """frozen 自拉起模式：把当前进程切换成某个 MCP server。

    打包(frozen)分发时，注册进桌面应用的命令是 ``<exe> --mcp-server <key>``
    （见 :func:`cc_bridge.bridge.config.mcp_launch_command`）。桌面应用据此拉起
    本可执行文件，这里就转去运行对应的 stdio server。
    """
    import sys as _sys

    # 防御：若被错误打成 --windowed（无控制台）的 frozen exe，stdin/stdout 会是 None，
    # MCP stdio server 无法通信。明确写日志并失败退出，而不是静默卡死。
    if _sys.stdin is None or _sys.stdout is None:
        _log_mcp_fatal(
            "MCP server 启动失败：stdin/stdout 不可用。"
            "该可执行文件很可能被以 --windowed/no-console 模式打包；"
            "请改用 console 模式重新打包（见 build_exe.py），或改用 pip 安装的 cc-bridge-mcp-* 入口。"
        )
        return 2

    if which == "codex":
        from cc_bridge.bridge.mcp_to_codex import main as srv
    elif which == "claude":
        from cc_bridge.bridge.mcp_to_claude import main as srv
    else:
        _log_mcp_fatal(f"未知的 --mcp-server 目标：{which!r}（应为 codex / claude）")
        return 2
    # stdio 可用、目标合法、模块已成功导入 —— server 即将正常 serve。
    # 此刻清掉可能残留的过期失败日志，别让它继续误导排查。
    _clear_mcp_fatal_log()
    srv()
    return 0


def main() -> int:
    """图形安装向导入口.

    若收到 ``--mcp-server <codex|claude>``，则切换成对应 MCP server（frozen 自拉起）。
    否则尝试创建 Tk()；若失败（无显示器 / headless），打印说明并回退到 CLI 的
    ``install`` 子命令。成功则进入事件循环，正常结束返回 0。
    """
    import sys as _sys

    # 仅 frozen console exe 才隐藏自己启动时的黑窗；非 frozen（pip 的 cc-bridge-install
    # 从终端运行）下 _hide_console_window 是 no-op，不会动用户的终端。
    _hide_console_window()

    argv = _sys.argv[1:]
    if "--mcp-server" in argv:
        idx = argv.index("--mcp-server")
        which = argv[idx + 1] if idx + 1 < len(argv) else ""
        return _run_mcp_server(which)

    try:
        import tkinter as tk
    except Exception as exc:  # tkinter 本身可能未安装
        print(f"无法加载图形界面（tkinter 不可用：{exc}），改用命令行安装。")
        from cc_bridge.cli import main as cli_main
        return cli_main(["install"])

    try:
        root = tk.Tk()
    except Exception as exc:
        # 典型情况：没有显示器 / 无 DISPLAY 的 headless 环境。
        print(f"无法创建图形窗口（{exc}），改用命令行安装。")
        from cc_bridge.cli import main as cli_main
        return cli_main(["install"])

    try:
        app = InstallerApp(root)
        # 关闭窗口时干净退出。
        try:
            root.protocol("WM_DELETE_WINDOW", app._quit)
        except Exception:
            pass
        root.mainloop()
    except Exception as exc:
        print(f"图形界面运行出错（{exc}），改用命令行安装。")
        try:
            root.destroy()
        except Exception:
            pass
        from cc_bridge.cli import main as cli_main
        return cli_main(["install"])

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
