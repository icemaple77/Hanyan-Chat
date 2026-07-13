"""
Hanyan Chat — 最小化 WebUI
==========================
配置编辑（含 llm/tts/stt/matrix/memory/character 等所有 config.json 字段，
通用 JSON 编辑器，新增字段不需要改这个文件）+ 角色提示词编辑 + 记忆查看 +
简易论坛（查看/发帖/点赞，无 AI 自动发帖/无线程评论/无头像，对齐 KouriChat
论坛的最小子集）。

启动方式：
    python webui.py
默认监听 config.get("webui.host")/config.get("webui.port")，仅当
config.json 里 webui.enabled=true 时才建议对外暴露；默认 host 是 127.0.0.1。
"""

import functools
import json
import logging
import os
import time
from datetime import datetime

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, session, url_for

from . import config, evolution, llm_client, memory, messaging, tools
from .character import get_manager as get_character_manager

logger = logging.getLogger("hanyan.webui")

app = Flask(__name__)
app.secret_key = config.get("webui.secret_key", "change-this-secret-key")

_FORUM_DIR = os.path.join(config.ROOT_DIR, "data", "forum")

# 内嵌模式下由 run_embedded() 注入 bot 实例：网页聊天直接共用 bot 的
# session_manager 和 LLM 路由器，和 Matrix 聊天是同一段对话。
# 独立运行（python webui.py）时为 None，聊天退化为"共用磁盘记忆"模式——
# 上下文仍然连续（bot 每轮都从磁盘读记忆），只是不共享内存态 session。
_bot = None


# ── 登录保护 ──────────────────────────────────────────────────────────

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == config.get("webui.username", "admin") and password == config.get("webui.password", ""):
            session["logged_in"] = True
            session["username"] = username
            return redirect(request.args.get("next") or url_for("index"))
        error = "用户名或密码错误"
    return render_template_string(_LOGIN_TEMPLATE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── 配置编辑（通用 JSON 编辑器，覆盖 matrix/llm/tts/stt/memory/character/... 全部字段）──

@app.route("/", methods=["GET"])
@login_required
def index():
    cfg = config.load()
    return render_template_string(_CONFIG_TEMPLATE, config_json=json.dumps(cfg, ensure_ascii=False, indent=2))


@app.route("/config/save", methods=["POST"])
@login_required
def save_config():
    raw = request.form.get("config_json", "")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("根节点必须是对象")
    except (json.JSONDecodeError, ValueError) as e:
        return jsonify({"status": "error", "message": f"JSON 解析失败: {e}"}), 400

    # 与 config.load() 的合并策略保持一致：以默认配置为骨架深度合并，
    # 避免保存一份缺 key 的配置后，新功能又"静默丢失"。
    merged = config._deep_merge(config._DEFAULT_CONFIG, data)
    config._config = merged
    config.save()
    return jsonify({"status": "ok"})


# ── 角色提示词编辑 ────────────────────────────────────────────────────

@app.route("/prompts")
@login_required
def list_prompts():
    mgr = get_character_manager()
    mgr.reload()
    return render_template_string(_PROMPTS_LIST_TEMPLATE, characters=mgr.list_characters())


@app.route("/prompts/<name>", methods=["GET", "POST"])
@login_required
def edit_prompt(name):
    prompts_dir = config.get("character.prompts_dir")
    safe_name = os.path.basename(name)
    path = os.path.join(prompts_dir, f"{safe_name}.md")

    if request.method == "POST":
        content = request.form.get("content", "")
        os.makedirs(prompts_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        get_character_manager().reload()
        return redirect(url_for("edit_prompt", name=safe_name))

    content = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    return render_template_string(_PROMPT_EDIT_TEMPLATE, name=safe_name, content=content, is_new=not os.path.exists(path))


@app.route("/prompts/<name>/delete", methods=["POST"])
@login_required
def delete_prompt(name):
    prompts_dir = config.get("character.prompts_dir")
    safe_name = os.path.basename(name)
    path = os.path.join(prompts_dir, f"{safe_name}.md")
    if os.path.exists(path):
        os.remove(path)
        get_character_manager().reload()
    return redirect(url_for("list_prompts"))


# ── 记忆查看 ──────────────────────────────────────────────────────────

@app.route("/memory")
@login_required
def list_memory():
    mem_dir = config.get("memory.storage_dir")
    files = []
    if os.path.isdir(mem_dir):
        for fn in sorted(os.listdir(mem_dir)):
            if fn.endswith(".json") and not fn.endswith(".tmp"):
                files.append(fn)
    return render_template_string(_MEMORY_LIST_TEMPLATE, files=files)


@app.route("/memory/<path:filename>")
@login_required
def view_memory(filename):
    mem_dir = config.get("memory.storage_dir")
    safe_name = os.path.basename(filename)
    path = os.path.join(mem_dir, safe_name)
    data = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = []
    return render_template_string(_MEMORY_VIEW_TEMPLATE, filename=safe_name, content=json.dumps(data, ensure_ascii=False, indent=2))


@app.route("/memory/<path:filename>/delete", methods=["POST"])
@login_required
def delete_memory(filename):
    mem_dir = config.get("memory.storage_dir")
    safe_name = os.path.basename(filename)
    path = os.path.join(mem_dir, safe_name)
    if os.path.exists(path):
        os.remove(path)
    return redirect(url_for("list_memory"))


# ── 最小论坛（查看 + 发帖 + 点赞，无 AI 自动发帖 / 无线程评论 / 无头像） ──

def _forum_path(character_name: str) -> str:
    os.makedirs(_FORUM_DIR, exist_ok=True)
    safe = memory._sanitize_key_part(character_name)
    return os.path.join(_FORUM_DIR, f"{safe}.json")


def _load_forum(character_name: str) -> list[dict]:
    path = _forum_path(character_name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_forum(character_name: str, posts: list[dict]):
    memory._atomic_write_json(_forum_path(character_name), posts)


@app.route("/forum/<character_name>")
@login_required
def forum_view(character_name):
    posts = sorted(_load_forum(character_name), key=lambda p: p.get("created_at", ""), reverse=True)
    return render_template_string(_FORUM_TEMPLATE, character_name=character_name, posts=posts)


@app.route("/forum/<character_name>/post", methods=["POST"])
@login_required
def forum_post(character_name):
    content = request.form.get("content", "").strip()
    if content:
        posts = _load_forum(character_name)
        new_id = (max((p.get("id", 0) for p in posts), default=0)) + 1
        posts.append({
            "id": new_id,
            "content": content,
            "author": session.get("username", config.get("webui.username", "admin")),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "likes": 0,
        })
        _save_forum(character_name, posts)
    return redirect(url_for("forum_view", character_name=character_name))


@app.route("/forum/<character_name>/like/<int:post_id>", methods=["POST"])
@login_required
def forum_like(character_name, post_id):
    posts = _load_forum(character_name)
    for p in posts:
        if p.get("id") == post_id:
            p["likes"] = p.get("likes", 0) + 1
            break
    _save_forum(character_name, posts)
    return redirect(url_for("forum_view", character_name=character_name))


@app.route("/forum/<character_name>/delete/<int:post_id>", methods=["POST"])
@login_required
def forum_delete(character_name, post_id):
    posts = [p for p in _load_forum(character_name) if p.get("id") != post_id]
    _save_forum(character_name, posts)
    return redirect(url_for("forum_view", character_name=character_name))


# ── 模型配置（本地/云端 + 按用途分配）─────────────────────────────────

_PURPOSES = [("chat", "日常聊天"), ("tools", "工具调用"), ("reflection", "自我反思/摘要")]


@app.route("/models")
@login_required
def models_page():
    route = config.get("llm.route", None)
    if not isinstance(route, dict):
        use_for = config.get("llm.cloud.use_for", ["tools", "reflection"]) or []
        route = {p: ("cloud" if p in use_for else "local") for p, _ in _PURPOSES}
    ctx = {
        "local_base": config.get("llm.base_url", ""),
        "local_model": config.get("llm.model", ""),
        "cloud_base": config.get("llm.cloud.base_url", ""),
        "cloud_model": config.get("llm.cloud.model", ""),
        "cloud_key_set": bool(config.get("llm.cloud.api_key", "")),
        "route": route,
        "purposes": _PURPOSES,
    }
    return render_template_string(_MODELS_TEMPLATE, **ctx)


@app.route("/api/models/save", methods=["POST"])
@login_required
def models_save():
    data = request.get_json(silent=True) or {}
    config.set("llm.base_url", (data.get("local_base") or "http://localhost:11434").strip())
    config.set("llm.model", (data.get("local_model") or "").strip())
    config.set("llm.cloud.base_url", (data.get("cloud_base") or "").strip())
    config.set("llm.cloud.model", (data.get("cloud_model") or "").strip())
    new_key = (data.get("cloud_key") or "").strip()
    if new_key:  # 留空 = 不改动现有 key
        config.set("llm.cloud.api_key", new_key)
    route = {p: ("cloud" if data.get("route", {}).get(p) == "cloud" else "local") for p, _ in _PURPOSES}
    config.set("llm.route", route)
    config.save()
    if _bot:
        _bot.router.reload_config()
        _bot.llm.reload_config()
    else:
        llm_client.get_router().reload_config()
    logger.info("[CKPT:models_saved] route=%s local=%s cloud=%s",
                route, config.get("llm.model"), config.get("llm.cloud.model") or "-")
    return jsonify({"status": "ok"})


@app.route("/api/models/test", methods=["POST"])
@login_required
def models_test():
    """连通性测试：向指定侧发一条极短消息。"""
    which = (request.get_json(silent=True) or {}).get("which", "local")
    router = _bot.router if _bot else llm_client.get_router()
    client = router.cloud if which == "cloud" else router.local
    if client is None:
        return jsonify({"ok": False, "message": "云端未配置（先填 base_url 并保存）"})
    old_timeout = client.timeout
    client.timeout = min(old_timeout, 30)
    try:
        reply = client.chat([{"role": "user", "content": "只回复：ok"}], temperature=0.0)
    finally:
        client.timeout = old_timeout
    ok = reply is not None and reply != llm_client.FALLBACK_REPLY
    return jsonify({"ok": ok, "message": (reply or "")[:80] if ok else "连接失败，看 data/hanyan.log 详情"})


# ── 网页聊天（共用 Matrix 会话）───────────────────────────────────────

def _chat_user_id() -> str:
    """网页聊天绑定的用户身份：配置优先，否则取最活跃的非 bot session，
    再退化到 character.user_map 里的第一个用户。"""
    configured = config.get("webui.chat_user_id", "")
    if configured:
        return configured
    if _bot:
        candidates = [
            s for s in _bot.session_manager.all_sessions()
            if not s.user_id.startswith(("@hermes:", "@serena:"))
        ]
        if candidates:
            return max(candidates, key=lambda s: s.last_active).user_id
    user_map = config.get("character.user_map", {}) or {}
    if user_map:
        return next(iter(user_map))
    return "@webui:local"


@app.route("/chat")
@login_required
def chat_page():
    return render_template_string(_CHAT_TEMPLATE)


@app.route("/api/chat/state")
@login_required
def chat_state():
    user_id = _chat_user_id()
    character_name = config.get_character_for_user(user_id)
    history = memory.load_memory(user_id, character_name)[-60:]
    return jsonify({"user_id": user_id, "character": character_name, "history": history})


@app.route("/api/chat/send", methods=["POST"])
@login_required
def chat_send():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400
    user_id = _chat_user_id()
    character_name = config.get_character_for_user(user_id)

    # 组装和 bot._on_message 一致的上下文：角色 + 核心记忆 + 动态上下文 + 工具说明 + 滚动记忆
    char = get_character_manager().get(character_name) or get_character_manager().current
    msgs = []
    if char:
        msgs.append(char.system_message())
    core = memory.format_core_memory_for_prompt(memory.load_core_memory(user_id, character_name))
    if core:
        msgs.append({"role": "system", "content": core})
    dyn = evolution.build_context_block(user_id, character_name)
    if dyn:
        msgs.append({"role": "system", "content": dyn})
    if config.get("tools.enabled", True):
        msgs.append({"role": "system", "content": tools.TOOL_SPEC})
    msgs.extend(memory.load_memory(user_id, character_name)[-20:])
    msgs.append({"role": "user", "content": text})

    if _bot:
        router = _bot.router
        # 共用内存态 session：网页发言也算"用户活跃"，主动消息不会误触发；
        # 不覆盖已有 session 的 Matrix room_id
        sess = _bot.session_manager.get(user_id) or _bot.session_manager.get_or_create(user_id, "webui")
        sess.last_active = time.time()
        sess.add_message("user", text)
    else:
        router = llm_client.get_router()
        sess = None

    reply, tool_images = tools.chat_loop(router, msgs, user_id, character_name)
    reply = tools.strip_tool_calls(reply) or reply or "[嗯，我现在有点累，稍后再聊好吗？]"

    if sess:
        sess.add_message("assistant", reply)
    memory.append_memory(user_id, character_name, text, reply)

    # 按消息模板拆条渲染（和 Matrix 端一致的多气泡体验）
    segments = []
    for action_type, content in messaging.split_reply(reply):
        if action_type == "text" and content:
            segments.append(content)
        elif action_type in ("tickle", "tickle_self"):
            segments.append("〔拍了拍你〕" if action_type == "tickle" else "〔拍了拍自己〕")
    if not segments:
        segments = [reply]

    images = ["/chat/media?p=" + os.path.relpath(p, config.ROOT_DIR) for p in tool_images]
    return jsonify({"segments": segments, "images": images})


@app.route("/chat/media")
@login_required
def chat_media():
    """受限的本地图片服务：只允许项目内 emojis/ 和 data/downloads/ 下的文件。"""
    rel = request.args.get("p", "")
    real = os.path.realpath(os.path.join(config.ROOT_DIR, rel))
    allowed = (
        os.path.realpath(os.path.join(config.ROOT_DIR, "emojis")),
        os.path.realpath(os.path.join(config.ROOT_DIR, "data", "downloads")),
    )
    if not any(real.startswith(a + os.sep) for a in allowed) or not os.path.isfile(real):
        abort(404)
    return send_file(real)


def run_embedded(bot):
    """在 bot 进程内以线程方式运行 WebUI（bot.start() 调用）。"""
    global _bot
    _bot = bot
    host = config.get("webui.host", "127.0.0.1")
    port = config.get("webui.port", 5001)
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    except Exception as e:
        logger.error("Embedded WebUI failed to start: %s", e)


# ── 模板（内联，避免依赖 templates/ 目录）─────────────────────────────

_BASE_STYLE = """
<style>
  body { font-family: -apple-system, "PingFang SC", sans-serif; background:#f7f5f9; color:#333; margin:0; }
  nav { background:#6b4c9a; padding:12px 20px; }
  nav a { color:#fff; text-decoration:none; margin-right:16px; font-size:14px; }
  main { max-width:900px; margin:24px auto; padding:0 16px; }
  textarea { width:100%; min-height:340px; font-family:monospace; font-size:13px; padding:10px; box-sizing:border-box; }
  input[type=text], input[type=password] { padding:8px; width:100%; box-sizing:border-box; margin-bottom:10px; }
  button { background:#6b4c9a; color:#fff; border:none; padding:8px 16px; border-radius:4px; cursor:pointer; }
  table { width:100%; border-collapse:collapse; }
  td, th { padding:8px; border-bottom:1px solid #e5e0ec; text-align:left; font-size:14px; }
  .card { background:#fff; border-radius:8px; padding:16px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .error { color:#c0392b; }
  .muted { color:#888; font-size:12px; }
</style>
"""

_LOGIN_TEMPLATE = _BASE_STYLE + """
<main style="max-width:360px;">
  <h2>Hanyan Chat 控制台</h2>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <form method="post">
    <input type="text" name="username" placeholder="用户名" required>
    <input type="password" name="password" placeholder="密码" required>
    <button type="submit">登录</button>
  </form>
</main>
"""

_NAV = """
<nav>
  <a href="{{ url_for('chat_page') }}">聊天</a>
  <a href="{{ url_for('models_page') }}">模型</a>
  <a href="{{ url_for('index') }}">配置</a>
  <a href="{{ url_for('list_prompts') }}">角色</a>
  <a href="{{ url_for('list_memory') }}">记忆</a>
  <a href="{{ url_for('logout') }}">退出</a>
</nav>
"""

_CONFIG_TEMPLATE = _BASE_STYLE + _NAV + """
<main>
  <h2>配置编辑</h2>
  <p class="muted">保存时会与默认配置深度合并，缺失的新 key 会自动补全，不会因为漏填而丢失。
  包含 matrix / llm / tts / stt / memory / character / link_fetch / commands / webui 全部字段。</p>
  <form id="cfg-form">
    <textarea name="config_json" id="config_json">{{ config_json }}</textarea><br><br>
    <button type="submit">保存</button>
    <span id="status" class="muted"></span>
  </form>
</main>
<script>
document.getElementById('cfg-form').addEventListener('submit', function(e){
  e.preventDefault();
  fetch("{{ url_for('save_config') }}", {method: 'POST', body: new FormData(this)})
    .then(r => r.json())
    .then(data => {
      document.getElementById('status').textContent = data.status === 'ok' ? '已保存 ✓' : ('错误: ' + data.message);
    });
});
</script>
"""

_PROMPTS_LIST_TEMPLATE = _BASE_STYLE + _NAV + """
<main>
  <h2>角色列表</h2>
  <div class="card">
    {% for c in characters %}
      <div>
        <a href="{{ url_for('edit_prompt', name=c.name) }}">{{ c.name }}</a>
        {% if c.is_current %}<span class="muted">(当前默认)</span>{% endif %}
        — {{ c.description }}
      </div>
    {% else %}
      <p class="muted">暂无角色，去下面新建一个。</p>
    {% endfor %}
  </div>
  <form onsubmit="location.href='/prompts/' + encodeURIComponent(document.getElementById('newname').value); return false;">
    <input type="text" id="newname" placeholder="新角色名（不含扩展名）">
    <button type="submit">新建 / 编辑</button>
  </form>
</main>
"""

_PROMPT_EDIT_TEMPLATE = _BASE_STYLE + _NAV + """
<main>
  <h2>{{ '新建角色' if is_new else '编辑角色' }}: {{ name }}</h2>
  <p class="muted">支持 YAML frontmatter（---name/description---）或直接纯 Markdown（兼容 KouriChat 无 frontmatter 的角色文件）。</p>
  <form method="post">
    <textarea name="content">{{ content }}</textarea><br><br>
    <button type="submit">保存</button>
  </form>
  {% if not is_new %}
  <form method="post" action="{{ url_for('delete_prompt', name=name) }}" onsubmit="return confirm('确定删除这个角色吗？');" style="margin-top:8px;">
    <button type="submit" style="background:#c0392b;">删除</button>
  </form>
  {% endif %}
</main>
"""

_MEMORY_LIST_TEMPLATE = _BASE_STYLE + _NAV + """
<main>
  <h2>记忆文件</h2>
  <p class="muted">文件名格式：{用户}__{角色}.json（滚动短期记忆） / {用户}__{角色}_core.json（核心长期记忆）</p>
  <table>
    {% for f in files %}
      <tr><td><a href="{{ url_for('view_memory', filename=f) }}">{{ f }}</a></td></tr>
    {% else %}
      <tr><td class="muted">暂无记忆文件</td></tr>
    {% endfor %}
  </table>
</main>
"""

_MEMORY_VIEW_TEMPLATE = _BASE_STYLE + _NAV + """
<main>
  <h2>{{ filename }}</h2>
  <textarea readonly>{{ content }}</textarea><br><br>
  <form method="post" action="{{ url_for('delete_memory', filename=filename) }}" onsubmit="return confirm('确定删除这个记忆文件吗？此操作不可撤销。');">
    <button type="submit" style="background:#c0392b;">删除此文件</button>
  </form>
</main>
"""

_FORUM_TEMPLATE = _BASE_STYLE + _NAV + """
<main>
  <h2>{{ character_name }} 的动态</h2>
  <form method="post" action="{{ url_for('forum_post', character_name=character_name) }}">
    <textarea name="content" placeholder="发一条动态..." style="min-height:80px;"></textarea><br><br>
    <button type="submit">发布</button>
  </form>
  <br>
  {% for p in posts %}
    <div class="card">
      <div>{{ p.content }}</div>
      <div class="muted">{{ p.author }} · {{ p.created_at }} · 👍 {{ p.likes }}</div>
      <form method="post" action="{{ url_for('forum_like', character_name=character_name, post_id=p.id) }}" style="display:inline;">
        <button type="submit">点赞</button>
      </form>
      <form method="post" action="{{ url_for('forum_delete', character_name=character_name, post_id=p.id) }}" style="display:inline;" onsubmit="return confirm('删除这条动态？');">
        <button type="submit" style="background:#c0392b;">删除</button>
      </form>
    </div>
  {% else %}
    <p class="muted">还没有动态。</p>
  {% endfor %}
</main>
"""


_CHAT_TEMPLATE = _BASE_STYLE + _NAV + """
<style>
  #box { max-width:720px; margin:0 auto; display:flex; flex-direction:column; height:calc(100vh - 60px); }
  #log { flex:1; overflow-y:auto; padding:16px; }
  .msg { max-width:78%; padding:9px 13px; border-radius:14px; margin:4px 0; font-size:14px;
         line-height:1.55; white-space:pre-wrap; word-break:break-word; }
  .me   { background:#6b4c9a; color:#fff; margin-left:auto; border-bottom-right-radius:4px; }
  .her  { background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.08); border-bottom-left-radius:4px; }
  .msg img { max-width:200px; border-radius:8px; display:block; }
  #bar { display:flex; gap:8px; padding:12px 16px; background:#fff; border-top:1px solid #e5e0ec; }
  #inp { flex:1; padding:10px; border:1px solid #d8d0e5; border-radius:20px; font-size:14px; outline:none; }
  #hdr { text-align:center; padding:8px; color:#888; font-size:12px; }
  .typing { color:#888; font-size:13px; padding:4px 16px; display:none; }
</style>
<div id="box">
  <div id="hdr">加载中…</div>
  <div id="log"></div>
  <div class="typing" id="typing">正在输入…</div>
  <div id="bar">
    <input id="inp" placeholder="说点什么…" autocomplete="off">
    <button onclick="send()">发送</button>
  </div>
</div>
<script>
const log = document.getElementById('log'), inp = document.getElementById('inp');
function bubble(cls, text) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}
function pic(url) {
  const d = document.createElement('div'); d.className = 'msg her';
  const i = document.createElement('img'); i.src = url; d.appendChild(i);
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}
fetch('/api/chat/state').then(r => r.json()).then(s => {
  document.getElementById('hdr').textContent = s.character + ' · 与 Matrix 会话同步（' + s.user_id + '）';
  for (const m of s.history) bubble(m.role === 'user' ? 'me' : 'her', m.content);
});
async function send() {
  const text = inp.value.trim();
  if (!text) return;
  inp.value = ''; bubble('me', text);
  document.getElementById('typing').style.display = 'block';
  try {
    const r = await fetch('/api/chat/send', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text})
    });
    const data = await r.json();
    for (const seg of (data.segments || [])) { bubble('her', seg); await new Promise(x => setTimeout(x, 350)); }
    for (const u of (data.images || [])) pic(u);
  } catch (e) { bubble('her', '（发送失败：' + e + '）'); }
  document.getElementById('typing').style.display = 'none';
}
inp.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
</script>
"""


_MODELS_TEMPLATE = _BASE_STYLE + _NAV + """
<main style="max-width:640px;">
  <h2>模型配置</h2>

  <div class="card">
    <h3>本地模型（Ollama）</h3>
    <label class="muted">服务地址</label>
    <input type="text" id="local_base" value="{{ local_base }}" placeholder="http://localhost:11434">
    <label class="muted">模型名</label>
    <input type="text" id="local_model" value="{{ local_model }}" placeholder="qwen3.5:9b">
    <button type="button" onclick="test('local')">测试连接</button>
    <span id="t_local" class="muted"></span>
  </div>

  <div class="card">
    <h3>云端模型（OpenAI 兼容：DeepSeek / SiliconFlow…）</h3>
    <label class="muted">服务地址（留空 = 不用云端）</label>
    <input type="text" id="cloud_base" value="{{ cloud_base }}" placeholder="https://api.deepseek.com/v1">
    <label class="muted">模型名</label>
    <input type="text" id="cloud_model" value="{{ cloud_model }}" placeholder="deepseek-chat">
    <label class="muted">API Key{{ '（已设置，留空则不改）' if cloud_key_set else '' }}</label>
    <input type="password" id="cloud_key" placeholder="{{ 'sk-*****（已保存）' if cloud_key_set else 'sk-...' }}">
    <button type="button" onclick="test('cloud')">测试连接</button>
    <span id="t_cloud" class="muted"></span>
  </div>

  <div class="card">
    <h3>用途分配</h3>
    <p class="muted">每类任务用哪个模型。选了云端但云端没配置时自动落回本地；任一侧失败自动切换另一侧。</p>
    <table>
      {% for p, label in purposes %}
      <tr>
        <td>{{ label }}</td>
        <td>
          <label><input type="radio" name="r_{{ p }}" value="local" {{ 'checked' if route[p] != 'cloud' }}> 本地</label>
          &nbsp;&nbsp;
          <label><input type="radio" name="r_{{ p }}" value="cloud" {{ 'checked' if route[p] == 'cloud' }}> 云端</label>
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <button onclick="save()">保存（立即生效，无需重启）</button>
  <span id="status" class="muted"></span>
</main>
<script>
function val(id){ return document.getElementById(id).value; }
function routeSel(){
  const r = {};
  for (const p of {{ purposes | map(attribute=0) | list | tojson }})
    r[p] = document.querySelector('input[name="r_'+p+'"]:checked').value;
  return r;
}
async function save(){
  const body = { local_base: val('local_base'), local_model: val('local_model'),
                 cloud_base: val('cloud_base'), cloud_model: val('cloud_model'),
                 cloud_key: val('cloud_key'), route: routeSel() };
  const r = await fetch('/api/models/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  document.getElementById('status').textContent = (await r.json()).status === 'ok' ? '已保存并生效 ✓' : '保存失败';
}
async function test(which){
  const el = document.getElementById('t_' + which);
  el.textContent = '测试中…（先保存再测才用新配置）';
  const r = await fetch('/api/models/test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({which})});
  const d = await r.json();
  el.textContent = d.ok ? ('✓ ' + d.message) : ('✗ ' + d.message);
}
</script>
"""


def main():
    logging.basicConfig(level=logging.INFO)
    host = config.get("webui.host", "127.0.0.1")
    port = config.get("webui.port", 5001)
    logger.info("Hanyan Chat WebUI starting on %s:%d", host, port)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
