"""
Hanyan Chat — 聊天内命令系统
对齐 KouriChat 的命令风格（中文全称 + 短别名），命中即短路正常的 LLM 回复流程。

dispatch() 需要一个 "bot" 对象，期望它提供：
    bot.matrix.send_text(room_id, text)          — 异步
    bot.char_mgr                                  — CharacterManager
    bot.schedule_restart()                         — 同步，安排重启
    bot.set_auto_message_enabled(bool)             — 同步
    bot.llm                                        — LLM 客户端（供记忆摘要用）
这样 commands.py 不用 import bot.py，避免循环依赖；bot.py 只需要暴露这几个
方法/属性即可复用整套命令表，方便以后单独测试命令逻辑。
"""

import asyncio
from typing import Optional

from . import config, evolution, fs_access, memory, tools
from .session import Session

COMMAND_ALIASES = {
    "/重启": "restart", "/re": "restart",
    "/关闭主动消息": "disable_auto", "/da": "disable_auto",
    "/开启主动消息": "enable_auto", "/ea": "enable_auto",
    "/清除临时记忆": "clear_memory", "/cl": "clear_memory",
    "/总结": "summarize", "/ms": "summarize",
    "/切换角色": "switch_character", "/role": "switch_character",
    "/角色列表": "list_characters", "/roles": "list_characters",
    "/反思": "reflect", "/rf": "reflect",
    "/兴趣": "interests", "/int": "interests",
    "/提案": "proposals", "/pp": "proposals",
    "/清理": "cleanup", "/gc": "cleanup",
    "/批准": "approve", "/ok": "approve",
    "/拒绝": "reject", "/no": "reject",
    "/待批": "pending", "/pd": "pending",
    "/帮助": "help", "/help": "help",
}

HELP_TEXT = (
    "可用命令：\n"
    "/重启 或 /re — 重启 bot 进程\n"
    "/关闭主动消息 或 /da — 暂停主动找你聊天\n"
    "/开启主动消息 或 /ea — 恢复主动消息\n"
    "/清除临时记忆 或 /cl — 清空这段对话的记忆\n"
    "/总结 或 /ms — 立刻把最近聊天整理进长期记忆\n"
    "/切换角色 <角色名> 或 /role <角色名> — 只切换你自己看到的角色\n"
    "/角色列表 或 /roles — 查看有哪些角色\n"
    "/反思 或 /rf — 立刻做一次自我反思（更新成长档案和兴趣）\n"
    "/兴趣 或 /int — 看看她最近对什么感兴趣\n"
    "/提案 或 /pp — 查看她收集的功能提案（GitHub 调研归档）\n"
    "/清理 或 /gc — 立刻清理过期的下载缓存\n"
    "/批准 <编号> 或 /ok <编号> — 批准她的文件操作申请\n"
    "/拒绝 <编号> 或 /no <编号> — 拒绝申请\n"
    "/待批 或 /pd — 查看待批准的文件操作\n"
    "/帮助 或 /help — 显示这份帮助"
)


async def dispatch(bot, session: Session, room_id: str, sender: str, text: str) -> bool:
    """检测并处理聊天内命令。返回 True 表示已处理（应短路正常回复流程）。"""
    if not config.get("commands.enabled", True):
        return False
    prefix = config.get("commands.prefix", "/")
    stripped = text.strip()
    if not stripped.startswith(prefix):
        return False

    first_line = stripped.splitlines()[0]
    tokens = first_line.split(maxsplit=1)
    cmd_raw = tokens[0]
    arg = tokens[1].strip() if len(tokens) > 1 else ""
    action = COMMAND_ALIASES.get(cmd_raw)
    if action is None:
        # 认不出的 "/开头" 文本当普通消息交给 LLM，避免误伤用户真实想说的话
        return False

    reply: Optional[str] = None

    if action == "restart":
        reply = "好，我马上重启～"
        bot.schedule_restart()
    elif action == "disable_auto":
        bot.set_auto_message_enabled(False)
        reply = "好，先不主动找你说话了，你叫我的话我还在。"
    elif action == "enable_auto":
        bot.set_auto_message_enabled(True)
        reply = "好，我会继续主动找你聊天～"
    elif action == "clear_memory":
        session.clear_history()
        memory.save_memory(sender, session.character_name, [])
        reply = "记忆清空啦，我们重新开始吧。"
    elif action == "summarize":
        promoted = await asyncio.get_event_loop().run_in_executor(
            None, memory.summarize_dynamic_memory, bot.llm, sender, session.character_name
        )
        reply = "已经帮你把最近的聊天整理进长期记忆啦。" if promoted else "最近聊天还不够多，暂时没什么好总结的～"
    elif action == "switch_character":
        if not arg:
            reply = "用法：/切换角色 角色名"
        elif bot.char_mgr.get(arg) is None:
            reply = f"没找到角色「{arg}」，用 /角色列表 看看有哪些。"
        else:
            user_map = config.get("character.user_map", {}) or {}
            user_map[sender] = arg
            config.set("character.user_map", user_map)
            session.character_name = arg
            reply = f"已经把你的角色切换为「{arg}」。"
    elif action == "list_characters":
        names = "、".join(c["name"] for c in bot.char_mgr.list_characters())
        reply = f"当前可用角色：{names or '（暂无）'}"
    elif action == "reflect":
        ok = await asyncio.get_event_loop().run_in_executor(
            None, evolution.reflect, bot.router, sender, session.character_name
        )
        reply = "反思完啦，成长档案已经更新～" if ok else "最近聊得还不够多，等我们多聊聊再反思吧。"
    elif action == "interests":
        interests = evolution.load_interests(sender, session.character_name)
        if interests:
            lines = "\n".join(f"· {i['topic']}（{i['weight']:.0%}）" for i in interests[:8])
            reply = f"我最近感兴趣的：\n{lines}"
        else:
            reply = "还没形成明确的兴趣呢，多和我聊聊你喜欢的东西吧～"
    elif action == "proposals":
        text = evolution.read_proposals(sender, session.character_name)
        reply = ("最近收集的提案（最新在后）：\n" + text[-1500:]) if text.strip() else "还没收集到提案。让我去搜搜 GitHub 看看有什么好玩的项目？"
    elif action == "cleanup":
        removed = tools.cleanup_downloads(0)  # 0 = 全部临时下载立即清
        reply = f"清理完成，删掉了 {removed} 个缓存文件。"
    elif action in ("approve", "reject"):
        try:
            pid = int(arg)
        except ValueError:
            reply = f"用法：/{'批准' if action == 'approve' else '拒绝'} <审批单编号>（/待批 可以看编号）"
        else:
            fn = fs_access.approve if action == "approve" else fs_access.reject
            reply = await asyncio.get_event_loop().run_in_executor(None, fn, pid, sender)
    elif action == "pending":
        reply = fs_access.list_pending()
    elif action == "help":
        reply = HELP_TEXT

    if reply:
        await bot.matrix.send_text(room_id, reply)
    return True
