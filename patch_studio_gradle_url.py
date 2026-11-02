r"""
让 Android Studio 新建项目时直接使用腾讯云 Gradle 镜像。

Android Studio 2026.2.2 实测：新建项目的 gradle-wrapper.properties 由
com.android.tools.idea.gradle.util.GradleWrapper$Companion 生成，其中
getDistributionUrl() 用格式串拼完整地址：

    String.format("https://services.gradle.org/%1$s/%2$s",
                  isSnapshot ? "distributions-snapshots" : "distributions",
                  "gradle-9.6.0-bin.zip")

再把 templates/project/gradle-sha256-list.txt 里的官方 sha256 写进
distributionSha256Sum。所以只改模板 properties 会被覆盖，必须连这个
格式串一起改。

本脚本做两件事。

一、写 %USERPROFILE%\.gradle\init.d\mirrors.gradle（用户目录，不需要管理员）
    用 beforeSettings 在项目 settings 脚本之前注入腾讯云仓库，让依赖也走国内。
    内容由脚本生成并带标记，重跑会自动更新，旧内容备份为 .bak。

二、改 android.jar 内三处（需要管理员权限）
  1. 格式串 -> https://mirrors.tencent.com/gradle/%2$s
  2. 模板 gradle-wrapper.properties 的 distributionUrl -> 腾讯云
  3. 用官方版本索引刷新 templates/project/gradle-sha256-list.txt

第 3 步的必要性：Studio 用「Switch to Gradle X」这类意图改 wrapper 时，
URL 换成 X，但哈希是从这个清单里查 gradle-X-bin.zip 得到的。清单是
Studio 发布时的快照，比清单新的版本查不到值，Studio 就沿用文件里原有的
旧哈希，于是出现「URL 是 9.8.2、期望哈希是 9.6.0」的校验失败：

    Expected checksum: 'bbaeb2fe...'（9.6.0 的）
    Actual checksum:   '21bd5da2...'（9.8.2 的）

刷新后清单覆盖官方全部正式版，这个坑不会再出现。

注意：
  - 需要管理员权限（写 Program Files）。
  - Studio 升级会覆盖 android.jar，升级后重跑本脚本即可。
  - 保留官方 sha256 不动：腾讯云镜像提供的是官方原包，校验仍然通过。

用法：
  python patch_studio_gradle_url.py                    # 两层都配
  python patch_studio_gradle_url.py "D:/Android Studio"
  python patch_studio_gradle_url.py --init-only        # 只配依赖镜像，不碰 jar
  python patch_studio_gradle_url.py --home=D:/Users/me # 指定用户目录
  python patch_studio_gradle_url.py --restore          # 还原官方地址、停用 init 脚本
"""

import atexit
import glob
import os
import shutil
import sys
import zipfile

JAR_REL = os.path.join("plugins", "android", "lib", "android.jar")

CLASS_ENTRY = "com/android/tools/idea/gradle/util/GradleWrapper$Companion.class"
TPL_ENTRY = "templates/project/wrapper/gradle/wrapper/gradle-wrapper.properties"
SHA_ENTRY = "templates/project/gradle-sha256-list.txt"

VERSIONS_URL = "https://services.gradle.org/versions/all"

OLD_PATTERN = "https://services.gradle.org/%1$s/%2$s"
NEW_PATTERN = "https://mirrors.tencent.com/gradle/%2$s"

MIRROR_BASE = r"https\://mirrors.tencent.com/gradle"
OFFICIAL_BASE = r"https\://services.gradle.org/distributions"

CANDIDATES = [
    r"C:\Program Files\Android\Android Studio",
    r"C:\Program Files\Android\Android Studio Preview",
    r"D:\Android Studio",
    r"D:\Program Files\Android\Android Studio",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Android Studio"),
]

PROPS = (
    "distributionBase=GRADLE_USER_HOME\n"
    "distributionPath=wrapper/dists\n"
    "distributionUrl={base}/gradle-{version}-bin.zip\n"
    "networkTimeout=10000\n"
    "validateDistributionUrl=true\n"
    "zipStoreBase=GRADLE_USER_HOME\n"
    "zipStorePath=wrapper/dists\n"
)

# ---------- 全局 init 脚本（依赖仓库镜像） ----------

INIT_DIR_REL = os.path.join(".gradle", "init.d")
INIT_NAME = "mirrors.gradle"
INIT_MARK = "managed by patch_studio_gradle_url.py"

INIT_TEMPLATE = """// 全局 Gradle 仓库镜像（腾讯云），对所有项目生效
// %s
// 本文件由脚本自动生成，重跑脚本会覆盖；要改请改脚本里的 INIT_TEMPLATE
// 位置: %%USERPROFILE%%\\.gradle\\init.d\\mirrors.gradle

def TENCENT_MAVEN = 'https://mirrors.cloud.tencent.com/nexus/repository/maven-public/'
def TENCENT_PLUGINS = 'https://mirrors.cloud.tencent.com/nexus/repository/gradle-plugins/'

// beforeSettings 在 settings.gradle(.kts) 执行之前运行，
// 所以镜像会排在项目自己声明的 google()/mavenCentral() 之前被优先命中。
beforeSettings { settings ->
    settings.pluginManagement.repositories {
        maven {
            name = 'TencentGradlePlugins'
            url = TENCENT_PLUGINS
        }
        maven {
            name = 'TencentMaven'
            url = TENCENT_MAVEN
        }
        // 兜底：镜像缺包时仍走官方源
        gradlePluginPortal()
        google()
        mavenCentral()
    }

    settings.dependencyResolutionManagement.repositories {
        maven {
            name = 'TencentMaven'
            url = TENCENT_MAVEN
        }
        google()
        mavenCentral()
    }
}
"""


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)

    def flush(self):
        for s in self.streams:
            s.flush()


def find_jar(explicit=None):
    if explicit:
        p = os.path.join(explicit, JAR_REL)
        return p if os.path.isfile(p) else None
    for studio in CANDIDATES:
        p = os.path.join(studio, JAR_REL)
        if os.path.isfile(p):
            return p
    return None


def bad_entries(jar):
    try:
        with zipfile.ZipFile(jar) as z:
            b = z.testzip()
            return {b} if b else set()
    except zipfile.BadZipFile:
        return {"<无法打开>"}


def patch_class(data, old, new):
    """
    在 class 常量池里替换一个 UTF8 常量。
    常量池条目按索引引用、不含绝对偏移，因此直接在原位置替换字节即可，
    长度变化后其余部分无需修补。
    """
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("不是有效的 class 文件")
    old_b, new_b = old.encode(), new.encode()
    count = int.from_bytes(data[8:10], "big")
    pos = 10
    i = 1
    while i < count:
        tag = data[pos]
        if tag == 1:  # Utf8
            ln = int.from_bytes(data[pos + 1:pos + 3], "big")
            if data[pos + 3:pos + 3 + ln] == old_b:
                entry = b"\x01" + len(new_b).to_bytes(2, "big") + new_b
                return data[:pos] + entry + data[pos + 3 + ln:]
            pos += 3 + ln
        elif tag in (3, 4):
            pos += 5
        elif tag in (5, 6):
            pos += 9
            i += 1
        elif tag in (7, 8, 16, 19, 20):
            pos += 3
        elif tag in (9, 10, 11, 12, 17, 18):
            pos += 5
        elif tag == 15:
            pos += 4
        else:
            raise ValueError("未知常量池标签 %d（位置 %d）" % (tag, pos))
        i += 1
    raise ValueError("常量池中找不到目标字符串")


def pick_base(jar):
    """优先用未被改过的原始 jar 作为重建基准，保证重复运行不叠加改动。"""
    backup = jar + ".original"
    if os.path.isfile(backup) and not bad_entries(backup):
        return backup, "原始备份"

    for cand in sorted(glob.glob(jar + ".old-*"), reverse=True):
        if os.path.isfile(cand) and not bad_entries(cand):
            shutil.copy2(cand, backup)
            return backup, "原始备份（来源 %s）" % os.path.basename(cand)

    bad = bad_entries(jar)
    if not bad:
        shutil.copy2(jar, backup)
        return jar, "当前 jar"

    raise RuntimeError("找不到干净的 android.jar 副本：%s" % sorted(bad))


def normalize(data, target):
    """把 class 里的格式串归一到目标值；已经是目标值则原样返回。"""
    if target.encode() in data:
        return data
    other = OLD_PATTERN if target == NEW_PATTERN else NEW_PATTERN
    return patch_class(data, other, target)


def fetch_checksums():
    """
    取官方版本索引里的 {发行包文件名: sha256}。
    失败不影响主流程：返回空字典，沿用 jar 内原清单。
    """
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            VERSIONS_URL, headers={"User-Agent": "studio-gradle-mirror"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print("警告: 取官方版本索引失败（%s），沿用 jar 内原清单" % e)
        return {}

    got = {}
    for item in data:
        sha = item.get("checksum")
        url = item.get("downloadUrl") or ""
        if not sha or not url:
            continue
        name = url.rsplit("/", 1)[-1]
        if name.endswith(".zip"):
            got[name] = sha
    return got


def merge_checksums(existing, extra):
    """
    保留原清单的顺序和条目，把缺的版本追加到末尾。
    清单格式为「sha256;gradle-<版本>-bin.zip」，Studio 按文件名查哈希。
    """
    lines = [l for l in existing.splitlines() if l.strip()]
    have = set()
    for line in lines:
        parts = line.split(";")
        if len(parts) == 2:
            have.add(parts[1])

    added = 0
    for name in sorted(extra):
        if name not in have:
            lines.append("%s;%s" % (extra[name], name))
            added += 1
    return "\n".join(lines) + "\n", added


def _backup(path, move=False):
    """备份成带时间戳的名字，绝不覆盖已有备份；move=True 时原文件被移走。"""
    import time
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak-%s" % (path, stamp)
    n = 1
    while os.path.exists(bak):
        bak = "%s.bak-%s-%d" % (path, stamp, n)
        n += 1
    if move:
        os.replace(path, bak)
    else:
        shutil.copy2(path, bak)
    return bak


def ensure_init_script(restore, home=None):
    """
    维护 %USERPROFILE%\\.gradle\\init.d\\mirrors.gradle。
    这一步只写用户目录，不需要管理员权限，因此放在改 jar 之前执行——
    即使 Studio 不在这台机器上，依赖镜像也照样能配好。
    """
    home = home or os.path.expanduser("~")
    init_dir = os.path.join(home, INIT_DIR_REL)
    path = os.path.join(init_dir, INIT_NAME)

    if restore:
        if not os.path.isfile(path):
            print("依赖镜像: 无 init 脚本，跳过")
            return 0
        cur = open(path, encoding="utf-8", errors="replace").read()
        if INIT_MARK not in cur:
            print("依赖镜像: %s 不是本脚本生成的，未处理" % path)
            return 0
        bak = _backup(path, move=True)
        print("依赖镜像: 已停用，改名为 %s" % os.path.basename(bak))
        return 0

    try:
        os.makedirs(init_dir, exist_ok=True)
    except Exception as e:
        print("依赖镜像: 无法创建 %s（%s）" % (init_dir, e))
        return 1

    want = INIT_TEMPLATE % INIT_MARK
    if os.path.isfile(path):
        cur = open(path, encoding="utf-8", errors="replace").read()
        if cur.replace("\r\n", "\n") == want:
            print("依赖镜像: 已是最新，未改动 %s" % path)
            return 0
        bak = _backup(path)
        print("依赖镜像: 原文件已备份为 %s" % os.path.basename(bak))

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(want)
    print("依赖镜像: 已写入 %s" % path)
    return 0


def build(base_jar, out_jar, restore, extra_checksums):
    """以 base_jar 为基准重建，写入各处改动。"""
    with zipfile.ZipFile(base_jar) as zin:
        infos = zin.infolist()
        names = {i.filename for i in infos}
        for need in (CLASS_ENTRY, TPL_ENTRY, SHA_ENTRY):
            if need not in names:
                raise RuntimeError("jar 内缺少条目：%s" % need)

        cls_old = zin.read(CLASS_ENTRY)
        tpl_old = zin.read(TPL_ENTRY).decode("utf-8")
        sha_old = zin.read(SHA_ENTRY).decode("utf-8")
        version = "9.0.0"
        for line in tpl_old.splitlines():
            if line.startswith("distributionUrl="):
                version = line.rsplit("/", 1)[-1].replace("gradle-", "").replace("-bin.zip", "")

        cls_new = normalize(cls_old, OLD_PATTERN if restore else NEW_PATTERN)
        base = OFFICIAL_BASE if restore else MIRROR_BASE
        tpl_new = PROPS.format(base=base, version=version).encode("utf-8")

        if restore:
            sha_new, added = sha_old, 0
        else:
            merged, added = merge_checksums(sha_old, extra_checksums)
            sha_new = merged

        if os.path.exists(out_jar):
            os.remove(out_jar)
        with zipfile.ZipFile(out_jar, "w") as zout:
            zout.comment = zin.comment
            for info in infos:
                if info.filename == CLASS_ENTRY:
                    data = cls_new
                elif info.filename == TPL_ENTRY:
                    data = tpl_new
                elif info.filename == SHA_ENTRY:
                    data = sha_new.encode("utf-8")
                else:
                    data = zin.read(info.filename)
                zout.writestr(info, data)

    with zipfile.ZipFile(out_jar) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError("重建后校验失败：%s" % bad)
        got_cls = z.read(CLASS_ENTRY)
        got_tpl = z.read(TPL_ENTRY).decode("utf-8", "replace")
        got_sha = z.read(SHA_ENTRY).decode("utf-8", "replace")
    return got_cls, got_tpl, got_sha, added


def main():
    raw = sys.argv[1:]
    args = [a for a in raw if not a.startswith("--")]
    restore = "--restore" in raw
    home = next((a.split("=", 1)[1] for a in raw if a.startswith("--home=")), None)

    log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "patch_studio_gradle_url.log"), "w", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log)
    atexit.register(log.flush)

    print("== 第一步：依赖仓库镜像（用户目录，无需管理员）==")
    ensure_init_script(restore, home)

    if "--init-only" in raw:
        print()
        print("仅配置依赖镜像，已跳过 android.jar")
        return 0

    print()
    print("== 第二步：Gradle 发行版地址（android.jar，需管理员）==")
    jar = find_jar(args[0] if args else None)
    if not jar:
        print("未找到 android.jar，请传入 Android Studio 安装目录")
        return 1
    print("目标 jar:", jar)

    try:
        base, label = pick_base(jar)
        print("重建基准:", label, base)
    except RuntimeError as e:
        print("失败:", e)
        return 1

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_android.jar.new")
    extra = {} if restore else fetch_checksums()
    try:
        cls_bytes, tpl_text, sha_text, added = build(base, tmp, restore, extra)
        os.replace(tmp, jar)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        print("失败: %s: %s" % (type(e).__name__, e))
        return 1

    # 回读确认
    with zipfile.ZipFile(jar) as z:
        n = len(z.namelist())
        chk = z.testzip()
    want = OLD_PATTERN if restore else NEW_PATTERN
    print("写入结果: %s" % ("已还原官方地址" if restore else "腾讯云镜像"))
    print("jar 条目数 %d，完整性 %s" % (n, "OK" if chk is None else chk))
    print("格式串: %s" % ("已命中" if want.encode() in cls_bytes else "未命中"))
    for line in tpl_text.splitlines():
        if line.startswith("distributionUrl="):
            print("模板地址:", line)
    print("哈希清单: %d 条，本轮新增 %d 条" % (len(sha_text.splitlines()), added))
    for ver in ("9.8.2", "9.6.0"):
        key = "gradle-%s-bin.zip" % ver
        val = next((l.split(";")[0] for l in sha_text.splitlines()
                    if l.endswith(";" + key)), None)
        print("  %s -> %s" % (key, val or "缺失"))
    print("请重启 Android Studio 后新建项目验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
