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
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for

from . import config, memory
from .character import get_manager as get_character_manager

logger = logging.getLogger("hanyan.webui")

app = Flask(__name__)
app.secret_key = config.get("webui.secret_key", "change-this-secret-key")

_FORUM_DIR = os.path.join(config.ROOT_DIR, "data", "forum")


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


def main():
    logging.basicConfig(level=logging.INFO)
    host = config.get("webui.host", "127.0.0.1")
    port = config.get("webui.port", 5001)
    logger.info("Hanyan Chat WebUI starting on %s:%d", host, port)
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
