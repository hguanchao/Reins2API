"""依赖同步与前端构建的判定：输入指纹取代 mtime。

为什么不用 mtime（老实现的坑）：git checkout/pull 会把源码 mtime 刷成检出新时间，
产物明明是最新的、却每次启动都重建；反过来说，某些解包/复制工具给整棵树写同一个
时间戳，源码改了但 mtime 不比产物新，于是**一直跑旧产物**——"我改了代码怎么没生效"
是最难查的一类问题。指纹是输入内容的 sha256，落一份在产物旁边：产物在 + 指纹一致
= 跳过，其余一律重建（宁可多建一次，不可跑旧代码）。

- 前端指纹输入：`web/src` + `web/public` 的全部文件 + 构建配置（index.html /
  package.json / package-lock.json / vite.config.ts / tsconfig*.json / components.json）。
- 依赖指纹输入：`pyproject.toml` + `uv.lock` + `.python-version`。指纹一致就跳过
  `uv sync`（uv 自己幂等，但省一次子进程；指纹不一致时 uv 才知道要装什么）。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from _launcher import SERVER, WEB

WEB_STAMP_REL = "dist/.launcher-build.json"

_WEB_CONFIG_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "components.json",
)
_DEP_FILES = ("pyproject.toml", "uv.lock", ".python-version")


def hash_inputs(paths: list[Path], base: Path) -> str:
    """输入文件集的指纹（相对路径 + 内容）。路径排序保证与遍历顺序无关。"""
    digest = hashlib.sha256()
    for path in sorted({p.resolve() for p in paths if p.is_file()}, key=lambda p: str(p)):
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            relative = str(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


# ── 前端 ──


def web_inputs(root: Path = WEB) -> list[Path]:
    inputs: list[Path] = []
    for name in ("src", "public"):
        base = root / name
        if base.is_dir():
            inputs.extend(p for p in base.rglob("*") if p.is_file())
    inputs.extend(root / name for name in _WEB_CONFIG_FILES)
    return inputs


def web_fingerprint(root: Path = WEB) -> str:
    return hash_inputs(web_inputs(root), root)


def read_web_stamp(root: Path = WEB) -> dict:
    try:
        data = json.loads((root / WEB_STAMP_REL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_web_stamp(root: Path = WEB, *, node: str = "") -> None:
    """构建成功后立刻落指纹。**必须**在 vite build 之后写：vite 会清空 dist。"""
    stamp = {
        "fingerprint": web_fingerprint(root),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "node": node,
    }
    try:
        target = root / WEB_STAMP_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(stamp, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def web_build_reason(root: Path = WEB, *, rebuild: bool = False, skip: bool = False) -> str:
    """要不要重建前端；返回原因（空串 = 不用建，跳过）。"""
    if skip:
        return ""
    if rebuild:
        return "强制重建"
    index = root / "dist" / "index.html"
    if not index.is_file():
        return "产物缺失"
    stamp = read_web_stamp(root)
    if not stamp:
        return "无构建记录（升级后首次）"
    if stamp.get("fingerprint") != web_fingerprint(root):
        return "输入有变（源码/配置）"
    return ""


def web_stamp_summary(root: Path = WEB) -> str:
    """一句话摘要（就绪卡/体检显示"产物什么时候、用什么 node 建的"）。"""
    stamp = read_web_stamp(root)
    if not stamp:
        return "无构建记录"
    when = str(stamp.get("built_at") or "?")
    node = str(stamp.get("node") or "")
    return f"建于 {when}" + (f" · node {node}" if node else "")


# ── 后端依赖 ──


def deps_fingerprint() -> str:
    return hash_inputs([SERVER / name for name in _DEP_FILES], SERVER)


def _deps_stamp() -> Path:
    return SERVER / ".venv" / ".launcher-deps.json"


def write_deps_stamp() -> None:
    target = _deps_stamp()
    if not target.parent.is_dir():
        return
    payload = {"fingerprint": deps_fingerprint(), "synced_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def deps_reason(*, resync: bool = False, venv_present: bool = True) -> str:
    """要不要跑 uv sync；返回原因（空串 = 跳过）。"""
    if resync:
        return "强制同步"
    if not venv_present:
        return "venv 缺失"
    try:
        stamp = json.loads(_deps_stamp().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stamp = {}
    if not isinstance(stamp, dict) or not stamp.get("fingerprint"):
        return "无同步记录（升级后首次）"
    if stamp.get("fingerprint") != deps_fingerprint():
        return "依赖声明有变（pyproject/uv.lock）"
    return ""


def deps_summary() -> str:
    try:
        stamp = json.loads(_deps_stamp().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    synced = str(stamp.get("synced_at") or "")
    return f"上次同步 {synced}" if synced else ""
