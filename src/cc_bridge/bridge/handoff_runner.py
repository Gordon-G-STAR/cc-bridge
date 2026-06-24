"""异步 handoff runner：读取 spec，执行同步 handoff 核心路径并落盘结果。"""

from __future__ import annotations

import asyncio
import os
import sys

from . import config, evidence, handoff, wal
from .context import ContextBuilder
from .contracts import FailureKind, HandoffResult, fail_closed_result
from .executor import AgentExecutor, ExecutionResult
from .handoff_store import read_spec, write_pid, write_result, write_status
from .locks import LockBusy, async_project_lock
from .parser import ResultParser


async def _run_agent(
    executor: AgentExecutor,
    *,
    agent: str,
    prompt: str,
    cwd: str,
    timeout: int,
    engine_mode: str,
    child_env: dict[str, str],
) -> ExecutionResult:
    if agent == "codex":
        return await executor.run_codex(
            prompt,
            cwd,
            timeout=timeout,
            sandbox_override=engine_mode,
            extra_env=child_env,
        )
    if agent == "claude":
        return await executor.run_claude(
            prompt,
            cwd,
            timeout=timeout,
            permission_override=engine_mode,
            extra_env=child_env,
        )
    raise ValueError(f"unsupported handoff agent: {agent}")


def _write_failure(handoff_id: str, *, kind: FailureKind, reason: str) -> None:
    result = fail_closed_result(
        handoff_id, failure_kind=kind, reason=reason, status="failed"
    )
    write_result(handoff_id, result)
    write_status(handoff_id, "failed")


async def run_spec(handoff_id: str) -> None:
    spec = read_spec(handoff_id)
    if spec is None:
        _write_failure(
            handoff_id,
            kind=FailureKind.crashed,
            reason="runner spec 缺失或损坏",
        )
        return

    request = spec["request"]
    cwd = str(spec["cwd"])
    agent = str(spec["agent"])
    caller = str(spec["caller"])

    write_pid(handoff_id, os.getpid())
    write_status(handoff_id, "running")
    try:
        cfg = config.BridgeConfig.from_env()
        plan = handoff.authorize(handoff_id, request, cwd, agent=agent, cfg=cfg)
        if isinstance(plan, HandoffResult):
            write_result(handoff_id, plan)
            write_status(handoff_id, plan.status)
            return

        before = evidence.baseline(cwd)
        wal.record_baseline(
            handoff_id, cwd, evidence.baseline_targets(cwd),
        )
        builder = ContextBuilder()
        project_ctx = await asyncio.to_thread(builder.build_project_context, cwd)
        prompt = builder.build_task_prompt(
            handoff.handoff_goal_text(request), project_ctx, caller=caller
        )
        executor = AgentExecutor(cfg)
        async with async_project_lock(cwd, timeout=5.0):
            result = await _run_agent(
                executor,
                agent=agent,
                prompt=prompt,
                cwd=cwd,
                timeout=request.timeout_seconds,
                engine_mode=plan.engine_mode,
                child_env=plan.child_env,
            )

        ev = evidence.gather(cwd, before, writable_paths=plan.effective_writable)
        parser = ResultParser()
        parsed = parser.parse(result, agent)
        summary = parser.summarize_for_caller(parsed, agent)
        handoff_result = handoff.execution_to_handoff(
            handoff_id,
            request,
            result,
            summary,
            agent,
            evidence=ev,
            plan=plan,
            project_root=cwd,
        )
        write_result(handoff_id, handoff_result)
        write_status(handoff_id, handoff_result.status)
    except LockBusy:
        result = fail_closed_result(
            handoff_id,
            failure_kind=FailureKind.project_busy,
            reason="项目被占用",
            status="failed",
        )
        write_result(handoff_id, result)
        write_status(handoff_id, "failed")
    except Exception as exc:  # noqa: BLE001 - runner 边界必须 fail-closed 落盘
        result = fail_closed_result(
            handoff_id,
            failure_kind=FailureKind.crashed,
            reason=f"runner 内部错误:{exc}",
            status="failed",
        )
        write_result(handoff_id, result)
        write_status(handoff_id, "failed")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m cc_bridge.bridge.handoff_runner <handoff_id>", file=sys.stderr)
        return 2
    asyncio.run(run_spec(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
