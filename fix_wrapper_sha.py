#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
刷新 gradle-wrapper.properties 的 distributionSha256Sum。

场景：手动把 distributionUrl 里的版本号改掉（比如 gradle-9.6.0-bin.zip ->
gradle-9.8.2-bin.zip），但 distributionSha256Sum 还是旧版本的哈希，Gradle 会报

    Verification of Gradle distribution failed!
    Expected checksum: 'bbaeb2fe...'
    Actual checksum:   '21bd5da2...'

本工具从官方索引 https://services.gradle.org/versions/all 查出目标版本的真实
sha256，写回文件。只改这一行，其余内容（注释、时间戳、键序）全部保持原样。

顺带做主机归一：默认把 services.gradle.org/distributions/ 换回
mirrors.tencent.com/gradle/。因为 Studio 的「Switch to Gradle X」、工程向导和
Gradle 自己的 wrapper 任务都可能把地址写回官方，尤其 Compose 工程用的
Kotlin 插件模板本身就硬编码了官方地址。

不需要管理员权限，也不碰 Android Studio 的安装目录，Studio 开着就能跑。

用法：
  python fix_wrapper_sha.py                    # 扫描默认工程目录
  python fix_wrapper_sha.py D:\\projects\\Demo    # 指定工程目录
  python fix_wrapper_sha.py .\\gradle\\wrapper\\gradle-wrapper.properties
  python fix_wrapper_sha.py --check            # 只报告，不写文件
  python fix_wrapper_sha.py --refresh          # 强制重新拉取索引
  python fix_wrapper_sha.py --list             # 查看索引缓存状态
  python fix_wrapper_sha.py --offline          # 只用本地缓存，不联网
  python fix_wrapper_sha.py --host=keep        # 不改主机，只修哈希
  python fix_wrapper_sha.py --host=official    # 反向：改回官方源

索引缓存在 %USERPROFILE%\\.gradle\\gradle-sha256-index.json，默认 6 小时内
直接复用，所以重复运行几乎瞬时完成。
"""

import glob
import json
import os
import sys
import time
import urllib.request

VERSIONS_URL = "https://services.gradle.org/versions/all"
DIST_BASE = "https://services.gradle.org/distributions"

# 只换主机段，版本号和 -bin/-all 后缀、以及 https\:// 的转义写法都保持原样
HOST_OFFICIAL = "services.gradle.org/distributions/"
HOST_MIRROR = "mirrors.tencent.com/gradle/"

CACHE_NAME = "gradle-sha256-index.json"
CACHE_TTL = 6 * 3600          # 秒
MAX_DEPTH = 6                 # 工程目录递归深度


# ---------- 索引：官方 sha256 清单 ----------

def cache_path(home=None):
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".gradle", CACHE_NAME)


def load_cache(home=None):
    path = cache_path(home)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_cache(data, home=None):
    path = cache_path(home)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print("警告: 索引缓存写入失败（%s）" % e)


def fetch_index():
    """拉取官方版本索引，返回 {"<sha256>": {"gradle-9.8.2-bin.zip": ...}} 形式的数据。"""
    req = urllib.request.Request(
        VERSIONS_URL, headers={"User-Agent": "fix-wrapper-sha"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)

    checks = {}
    versions = []
    for item in data:
        url = item.get("downloadUrl") or ""
        sha = item.get("checksum")
        if not url or not sha:
            continue
        name = url.rsplit("/", 1)[-1]
        if not name.endswith(".zip"):
            continue
        checks[name] = sha
        if item.get("version"):
            versions.append(item["version"])
    return {
        "fetchedAt": int(time.time()),
        "source": VERSIONS_URL,
        "checksums": checks,
        "versions": sorted(set(versions)),
    }


def get_index(refresh=False, offline=False, home=None):
    """
    取索引：优先用未过期的缓存；--refresh 强制联网；--offline 只用缓存。
    返回 (checksums dict, 状态说明)；完全取不到时返回 ({}, 原因)。
    """
    cached = load_cache(home)
    fresh = cached and (time.time() - cached.get("fetchedAt", 0) < CACHE_TTL)

    if offline:
        if cached:
            return cached.get("checksums", {}), "缓存（--offline）"
        return {}, "离线模式且无缓存"

    if fresh and not refresh:
        age = int((time.time() - cached["fetchedAt"]) / 60)
        return cached.get("checksums", {}), "缓存（%d 分钟前）" % age

    try:
        data = fetch_index()
    except Exception as e:
        if cached:
            return cached.get("checksums", {}), "联网失败，回退到缓存（%s）" % e
        return {}, "联网失败且无缓存（%s）" % e

    save_cache(data, home)
    return data["checksums"], "官方索引（%d 条）" % len(data["checksums"])


_MEMO = {}          # 本次运行内的按需查询结果，避免同一文件被反复联网
_OFFLINE = False    # --offline 时禁止任何联网补查


def lookup(checksums, name):
    """
    按文件名查哈希。索引里只有 -bin.zip，但 -all.zip 也有官方 .sha256 附属文件，
    所以查不到时按需补一次；结果记在 _MEMO 里，同一次运行不重复请求。
    """
    sha = checksums.get(name)
    if sha:
        return sha
    if name in _MEMO:
        return _MEMO[name]
    if _OFFLINE or not name.endswith(".zip"):
        return None
    try:
        url = "%s/%s.sha256" % (DIST_BASE, name)
        req = urllib.request.Request(url, headers={"User-Agent": "fix-wrapper-sha"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace").strip()
        val = text.split()[0] if len(text) >= 64 else None
    except Exception:
        val = None
    _MEMO[name] = val
    return val


# ---------- 工程扫描与文件改写 ----------

def default_roots(home=None):
    """常见工程根目录，存在哪个就扫哪个；当前目录也算一个。"""
    home = home or os.path.expanduser("~")
    roots = []
    for name in ("AndroidStudioProjects", "StudioProjects", "AndroidProjects", "Projects"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            roots.append(p)
    if os.getcwd() not in roots:
        roots.append(os.getcwd())
    return roots


def iter_wrappers(roots):
    seen = set()
    for root in roots:
        if os.path.isfile(root) and root.endswith("gradle-wrapper.properties"):
            if root not in seen:
                seen.add(root)
                yield root
            continue
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for dp, dn, fn in os.walk(root):
            # 跳过构建产物和版本库内部目录，避免在无关文件上浪费时间
            dn[:] = [d for d in dn if d not in (".git", "build", ".gradle", ".idea", "node_modules")]
            if dp.count(os.sep) - base_depth > MAX_DEPTH:
                dn[:] = []
                continue
            if "gradle-wrapper.properties" in fn:
                path = os.path.join(dp, "gradle-wrapper.properties")
                if path not in seen:
                    seen.add(path)
                    yield path


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def write_text(path, text, enc):
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(text)


def backup_once(path):
    """
    单一滚动备份（每次覆盖同一个 .bak），避免反复运行时堆出一堆时间戳文件。
    想回退：把 .bak 复制回原名即可。
    """
    bak = path + ".bak"
    try:
        with open(path, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())
    except Exception:
        return None
    return bak


def plan_fix(path, checksums, host=None):
    """
    分析一个 wrapper 文件，返回 (动作, 说明, 新文本, 编码)。
    动作取值：fixed（需要改）/ ok（已一致）/ skip（不处理）。
    host：mirror 把官方主机换成镜像，official 反向，None/keep 不动主机。
    """
    text, enc = read_text(path)
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()

    raw_url, url_idx, sha_idx, cur = None, None, None, None
    for i, line in enumerate(lines):
        if line.startswith("distributionUrl="):
            raw_url = line.split("=", 1)[1].strip()
            url_idx = i
        elif line.startswith("distributionSha256Sum="):
            sha_idx = i
            cur = line.split("=", 1)[1].strip()

    if url_idx is None:
        return "skip", "没有 distributionUrl", None, enc

    new_raw = raw_url
    if host == "mirror":
        new_raw = raw_url.replace(HOST_OFFICIAL, HOST_MIRROR)
    elif host == "official":
        new_raw = raw_url.replace(HOST_MIRROR, HOST_OFFICIAL)
    host_changed = new_raw != raw_url

    url = new_raw.replace("\\:", ":").replace("\\=", "=")
    name = url.rsplit("/", 1)[-1]
    if not name.endswith(".zip"):
        return "skip", "地址不是 zip 包：%s" % name, None, enc

    want = lookup(checksums, name)
    if not want and not host_changed:
        if _OFFLINE:
            return ("skip",
                    "离线模式下索引里没有 %s（-all 包或新版本需联网补查）" % name,
                    None, enc)
        return "skip", "官方索引里查不到 %s（版本可能不存在，或尚未发布）" % name, None, enc
    sha_changed = bool(want) and (not cur or cur.lower() != want.lower())

    if not host_changed and not sha_changed:
        return "ok", "%s 已正确" % name, None, enc

    notes = []
    if host_changed:
        lines[url_idx] = "distributionUrl=" + new_raw
        notes.append("主机改为%s（%s）" % (
            "腾讯云镜像" if host == "mirror" else "官方源", name))
    if sha_changed:
        if sha_idx is not None:
            lines[sha_idx] = "distributionSha256Sum=" + want
            old = cur[:8] + "…" if cur else "缺失"
        else:
            # 没有这一行时插到 distributionUrl 后面，位置与 Studio 生成的一致
            lines.insert(url_idx + 1, "distributionSha256Sum=" + want)
            old = "缺失"
        notes.append("哈希 %s -> %s" % (old, want[:8] + "…"))

    return "fixed", "；".join(notes), nl.join(lines) + nl, enc


def run(paths, checksums, check_only=False, host=None):
    fixed = same = skipped = 0
    for path in iter_wrappers(paths):
        try:
            action, info, new_text, enc = plan_fix(path, checksums, host)
        except Exception as e:
            print("  跳过 %s（%s）" % (path, e))
            skipped += 1
            continue

        if action == "ok":
            same += 1
            print("  已一致 %s" % path)
        elif action == "skip":
            skipped += 1
            print("  跳过   %s\n          %s" % (path, info))
        else:
            if check_only:
                fixed += 1
                print("  待修正 %s\n          %s" % (path, info))
            else:
                bak = backup_once(path)
                write_text(path, new_text, enc)
                fixed += 1
                print("  已修正 %s\n          %s%s" % (
                    path, info,
                    ("\n          备份 %s" % os.path.basename(bak)) if bak else ""))

    print("汇总：%s %d，已一致 %d，跳过 %d" % (
        "需修正" if check_only else "修正", fixed, same, skipped))
    return fixed, same, skipped


# ---------- 入口 ----------

def main():
    global _OFFLINE

    raw = sys.argv[1:]
    check_only = "--check" in raw or "-n" in raw
    refresh = "--refresh" in raw
    offline = "--offline" in raw
    _OFFLINE = offline
    show_list = "--list" in raw
    home = next((a.split("=", 1)[1] for a in raw if a.startswith("--home=")), None)
    paths = [a for a in raw if not a.startswith("-")]

    host = next((a.split("=", 1)[1] for a in raw if a.startswith("--host=")), "mirror")
    if host in ("keep", "none", "off"):
        host = None
    if host not in (None, "mirror", "official"):
        print("--host 只接受 mirror / official / keep")
        return 2

    if show_list:
        cached = load_cache(home)
        if not cached:
            print("还没有索引缓存：%s" % cache_path(home))
            print("运行一次本工具即可生成（需联网）。")
            return 0
        age = int(time.time() - cached.get("fetchedAt", 0))
        checks = cached.get("checksums", {})
        print("缓存文件：%s" % cache_path(home))
        print("拉取时间：%s（%d 分钟前）" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cached.get("fetchedAt", 0))),
            age // 60))
        print("来源    ：%s" % cached.get("source"))
        print("条目    ：%d" % len(checks))
        for ver in ("9.6.0", "9.8.2"):
            key = "gradle-%s-bin.zip" % ver
            print("  %-24s %s" % (key, checks.get(key, "缺失")))
        return 0

    checksums, source = get_index(refresh=refresh, offline=offline, home=home)
    print("索引：%s" % source)
    if not checksums:
        print("没有可用的哈希索引，无法继续。检查网络或改用 --offline 配合已有缓存。")
        return 1

    if not paths:
        paths = default_roots(home)
        print("扫描：%s" % "；".join(paths))

    fixed, same, skipped = run(paths, checksums, check_only=check_only, host=host)
    if not fixed and not same and not skipped:
        print("没有找到 gradle-wrapper.properties，检查一下路径？")
        return 0
    if check_only and fixed:
        print("这是 --check 模式，未写文件。去掉 --check 即执行修正。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
