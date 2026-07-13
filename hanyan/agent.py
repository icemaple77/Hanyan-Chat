"""
Hanyan Chat — 任务执行层（agent 的规划/执行/验证闭环）
======================================================
把"帮我整理下载文件夹"这类多步任务变成：

    规划(plan) → 逐步执行(每步一个 tools.chat_loop) → 验证(verify+重试) → 汇报

设计约束（刻意保守，宁可做不完也不失控）：
- 全局同时只跑一个任务；后台线程执行，不阻塞聊天
- 步数上限 agent.max_steps（默认5），每步的工具调用沿用 tools.max_calls_per_turn
- 失败步骤只自动重试一次；工作区外的写/删仍走审批单（任务不暂停等审批，
  把单号写进报告，用户 /批准 后生效）
- 执行报告存进她的工作区 data/workspace/reports/，全程 [CKPT:task_*] 日志
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from . import config, tools

logger = logging.getLogger("hanyan.agent")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_PLAN_PROMPT = """你是任务规划器。把用户的目标拆成尽量少的可执行步骤（1-{max_steps}步）。
每步必须是一句具体指令，能靠这些工具完成：搜索网页/读网页/下载图片/搜GitHub/查时间/
列目录(fs_list)/读文件(fs_read)/写文件(fs_write)/删文件(fs_delete)。
只返回 JSON，不要其他文字：{{"steps": ["步骤1", "步骤2"]}}
做不到的目标返回：{{"steps": []}}

用户目标：{goal}"""

_STEP_PROMPT = """你正在执行一个多步任务，当前是第 {idx}/{total} 步。
【总目标】{goal}
【之前步骤的结果】
{context}
【本步任务】{step}
请用工具完成本步，完成后简短汇报结果（一两句话，说清做了什么、关键发现/产出路径）。"""

_VERIFY_PROMPT = """任务执行完毕，请验证结果。
【目标】{goal}
【各步结果】
{context}
只返回 JSON：{{"done": true/false, "summary": "给用户的一段简短总结(口语化)", "retry_step": 失败需重做的步骤序号或null}}"""


class TaskRunner:
    """任务执行器。send_text(room_id, text) 需为线程安全的同步回调。"""

    def __init__(self, router, send_text: Callable[[str, str], None]):
        self.router = router
        self.send_text = send_text
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self.status: str = "idle"   # idle / planning / step N/M / verifying
        self.goal: str = ""

    # ── 对外接口 ─────────────────────────────────────────────

    def start(self, user_id: str, room_id: str, character_name: str, goal: str) -> str:
        goal = (goal or "").strip()
        if not goal:
            return "（任务目标是空的）"
        with self._lock:
            if self._thread and self._thread.is_alive():
                return f"（我手上还有个任务在跑：「{self.goal}」，用 /停止任务 可以中止它）"
            self._stop = False
            self.goal = goal
            self.status = "planning"
            self._thread = threading.Thread(
                target=self._run, args=(user_id, room_id, character_name, goal),
                name="TaskRunner", daemon=True,
            )
            self._thread.start()
        logger.info("[CKPT:task_start] %s", goal)
        return f"（任务已开始后台执行：「{goal}」。会边做边汇报，完成后给总结）"

    def stop(self) -> str:
        if self._thread and self._thread.is_alive():
            self._stop = True
            return "好，收到，做完当前这一步就停下来。"
        return "现在没有正在执行的任务呀。"

    def state(self) -> str:
        if self._thread and self._thread.is_alive():
            return f"正在执行：「{self.goal}」（{self.status}）"
        return "当前没有任务在跑。"

    # ── 内部执行 ─────────────────────────────────────────────

    def _llm_json(self, prompt: str) -> Optional[dict]:
        raw = self.router.chat(
            [{"role": "system", "content": "只返回 JSON。"}, {"role": "user", "content": prompt}],
            temperature=0.2, purpose="tools",
        )
        if not raw:
            return None
        m = _JSON_RE.search(raw)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            logger.info("[CKPT:task_json_fail] %.120s", raw)
            return None

    def _exec_step(self, idx: int, total: int, goal: str, step: str,
                   context: str, user_id: str, character_name: str) -> tuple[str, list[str]]:
        msgs = [
            {"role": "system", "content": f"你是{character_name}，正在替用户执行任务，风格简洁务实。\n" + tools.TOOL_SPEC},
            {"role": "user", "content": _STEP_PROMPT.format(idx=idx, total=total, goal=goal, context=context or "（这是第一步）", step=step)},
        ]
        reply, images = tools.chat_loop(self.router, msgs, user_id, character_name)
        return tools.strip_tool_calls(reply) or "（这一步没有产出结果）", images

    def _run(self, user_id: str, room_id: str, character_name: str, goal: str):
        t0 = time.time()
        try:
            # 1. 规划
            plan = self._llm_json(_PLAN_PROMPT.format(goal=goal, max_steps=int(config.get("agent.max_steps", 5))))
            steps = [str(s)[:200] for s in (plan or {}).get("steps", []) if str(s).strip()]
            steps = steps[: int(config.get("agent.max_steps", 5))]
            if not steps:
                self.send_text(room_id, "想了想，这个任务我拆不出可执行的步骤，换个说法试试？")
                logger.info("[CKPT:task_no_plan] %s", goal)
                return
            self.send_text(room_id, "我打算分 %d 步做：\n%s" % (len(steps), "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))))

            # 2. 逐步执行
            results: list[str] = []
            images: list[str] = []
            for i, step in enumerate(steps, 1):
                if self._stop:
                    self.send_text(room_id, f"任务在第 {i} 步前被叫停了。")
                    logger.info("[CKPT:task_stopped] at step %d", i)
                    return
                self.status = f"step {i}/{len(steps)}"
                logger.info("[CKPT:task_step] %d/%d: %s", i, len(steps), step)
                context = "\n".join(f"第{n+1}步：{r[:200]}" for n, r in enumerate(results))
                result, imgs = self._exec_step(i, len(steps), goal, step, context, user_id, character_name)
                results.append(result)
                images.extend(imgs)
                self.send_text(room_id, f"✔ 第 {i}/{len(steps)} 步：{result[:200]}")

            # 3. 验证（失败步骤重试一次）
            self.status = "verifying"
            ctx = "\n".join(f"第{n+1}步：{r[:300]}" for n, r in enumerate(results))
            verdict = self._llm_json(_VERIFY_PROMPT.format(goal=goal, context=ctx)) or {}
            retry = verdict.get("retry_step")
            if not verdict.get("done", True) and isinstance(retry, int) and 1 <= retry <= len(steps) and not self._stop:
                logger.info("[CKPT:task_retry] step %d", retry)
                self.send_text(room_id, f"检查了一下，第 {retry} 步好像没做好，我重做一次…")
                context = "\n".join(f"第{n+1}步：{r[:200]}" for n, r in enumerate(results))
                result, imgs = self._exec_step(retry, len(steps), goal, steps[retry - 1] + "（上次没成功，换个思路再试）", context, user_id, character_name)
                results[retry - 1] = result
                images.extend(imgs)
                ctx = "\n".join(f"第{n+1}步：{r[:300]}" for n, r in enumerate(results))
                verdict = self._llm_json(_VERIFY_PROMPT.format(goal=goal, context=ctx)) or {}

            # 4. 汇报 + 报告落盘（写进她的工作区）
            summary = str(verdict.get("summary") or "").strip() or "都做完啦，具体见上面每步的结果～"
            elapsed = int(time.time() - t0)
            report_path = self._save_report(goal, steps, results, summary)
            note = f"\n（报告已存到 {report_path}）" if report_path else ""
            self.send_text(room_id, f"{summary}{note}")
            logger.info("[CKPT:task_done] %s in %ds", goal, elapsed)
        except Exception as e:
            logger.error("[CKPT:task_error] %s", e, exc_info=True)
            try:
                self.send_text(room_id, f"任务执行时出了点问题（{e}），已经记进日志了。")
            except Exception:
                pass
        finally:
            self.status = "idle"

    def _save_report(self, goal: str, steps: list[str], results: list[str], summary: str) -> Optional[str]:
        try:
            reports_dir = os.path.join(config.ROOT_DIR, "data", "workspace", "reports")
            os.makedirs(reports_dir, exist_ok=True)
            fname = datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
            path = os.path.join(reports_dir, fname)
            body = [f"# 任务报告：{goal}", f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"]
            for i, (s, r) in enumerate(zip(steps, results), 1):
                body.append(f"## 第{i}步：{s}\n\n{r}\n")
            body.append(f"## 总结\n\n{summary}\n")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(body))
            return os.path.relpath(path, config.ROOT_DIR)
        except OSError as e:
            logger.warning("[CKPT:task_report_fail] %s", e)
            return None
