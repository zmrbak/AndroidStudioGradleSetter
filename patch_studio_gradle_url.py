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

本脚本做四件事。

一、写 %USERPROFILE%\.gradle\init.d\mirrors.gradle（用户目录，不需要管理员）
    1. beforeSettings 在项目 settings 脚本之前注入腾讯云仓库，让依赖走国内；
    2. 拦截 Gradle 自带的 wrapper 任务，把它准备写进 gradle-wrapper.properties
       的官方地址换回镜像。Studio 的「Switch to Gradle X」和工程向导都会跑
       这个任务，它按现有地址或官方 DistributionLocator 重写文件——不拦它，
       模板改得再对也会被改回 services.gradle.org。

二、改 android.jar 内三处（需要管理员权限）
  1. 格式串 -> https://mirrors.tencent.com/gradle/%2$s
  2. 模板 gradle-wrapper.properties 的 distributionUrl -> 腾讯云
  3. 用官方版本索引刷新 templates/project/gradle-sha256-list.txt

三、改另外两个插件里的向导模板（需要管理员权限）
  Compose 工程走的是 Kotlin 插件的模板，不是 android.jar：
    kotlin-plugin.jar / fileTemplates/code/compose-gradle-wrapper.properties.ft
    kotlin-plugin.jar / .../projectWizard/templates/gradle/gradle-wrapper.properties.vm
  这两份硬编码了官方地址，也没有 distributionSha256Sum，新建 Compose 工程
  时地址就是官方的。intellij.gradle.jar 里生成 build 脚本的模板一并处理。

四、校准已有工程的 wrapper 哈希（不需要管理员）

第 3 条的必要性：Studio 用「Switch to Gradle X」这类意图改 wrapper 时，
URL 换成 X，但哈希是从这个清单里查 gradle-X-bin.zip 得到的。清单是
Studio 发布时的快照，比清单新的版本查不到值，Studio 就沿用文件里原有的
旧哈希，于是出现「URL 是 9.8.2、期望哈希是 9.6.0」的校验失败：

    Expected checksum: 'bbaeb2fe...'（9.6.0 的）
    Actual checksum:   '21bd5da2...'（9.8.2 的）

刷新后清单覆盖官方全部正式版，这个坑不会再出现。

注意：
  - 第二、第三步需要管理员权限（写 Program Files）。
  - Studio 升级会覆盖这些 jar，升级后重跑本脚本即可。
  - 保留官方 sha256 不动：腾讯云镜像提供的是官方原包，校验仍然通过。

用法：
  python patch_studio_gradle_url.py                    # 四步全做
  python patch_studio_gradle_url.py "D:/Android Studio"
  python patch_studio_gradle_url.py --init-only        # 只维护 init 脚本，不碰 jar
  python patch_studio_gradle_url.py --home=D:/Users/me # 指定用户目录
  python patch_studio_gradle_url.py --restore          # 还原官方地址、停用 init 脚本
"""

import atexit
import glob
import os
import shutil
import subprocess
import sys
import zipfile

JAR_REL = os.path.join("plugins", "android", "lib", "android.jar")

# 另外两个插件里的向导模板：新建 Compose 工程时写 wrapper 用的就是这里
OTHER_JARS = [
    (
        os.path.join("plugins", "Kotlin", "lib", "kotlin-plugin.jar"),
        [
            "fileTemplates/code/compose-gradle-wrapper.properties.ft",
            "org/jetbrains/kotlin/tools/projectWizard/templates/gradle/"
            "gradle-wrapper.properties.vm",
        ],
    ),
]

# 这个 jar 是 gradle 插件的 content module（org.jetbrains.plugins.gradle.* 全在里面），
# 重建后 IDE 会当成失效模块而整包跳过，启动时报
#   NoClassDefFoundError: org/jetbrains/plugins/gradle/service/project/open/GradleProjectOpenProcessor
# 实测重打包的 zip 用 java.util.zip 读完全正常，但 Studio 就是不认，原因未查明。
# 它只影响"Java 库工程"那套老式 task wrapper 模板，对 Android 开发没用，所以默认不碰。
GRADLE_PLUGIN_JAR = (
    os.path.join("plugins", "gradle-plugin", "lib", "intellij.gradle.jar"),
    ["fileTemplates/internal/Gradle Build Script with wrapper.gradle.ft"],
)


def fix_acl(path):
    """Program Files 下用 os.replace 新建的文件会丢掉 Users 读权限，补回来"""
    if os.name != "nt":
        return
    try:
        subprocess.run(["icacls", path, "/inheritance:e"],
                       capture_output=True, timeout=30)
    except Exception:
        pass

# 只替换主机段，保留 https\:// 这种转义写法和后面的版本占位符
HOST_OLD = "services.gradle.org/distributions/"
HOST_NEW = "mirrors.tencent.com/gradle/"

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

// Studio 的「Switch to Gradle X」和工程向导会执行 Gradle 自带的 wrapper 任务，
// 由它重写 gradle-wrapper.properties。该任务会沿用现有地址、或按官方
// DistributionLocator 生成 services.gradle.org 的地址，于是模板改了也会被它改回去。
// 这里在任务配置阶段把官方主机换成镜像，写出来的就始终是国内地址。
// 只动主机段，版本号和 -bin/-all 后缀保持原样。
gradle.allprojects { project ->
    project.tasks.withType(org.gradle.api.tasks.wrapper.Wrapper).configureEach { task ->
        def current = task.distributionUrl
        if (current != null && current.contains('services.gradle.org')) {
            task.distributionUrl = current.replace(
                    'https://services.gradle.org/distributions/',
                    'https://mirrors.tencent.com/gradle/')
        }
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


def utf8_hits(data, text):
    """列出常量池里内容等于 text 的所有 UTF8 条目，返回 [(数据起始偏移, 长度)]。"""
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("不是有效的 class 文件")
    needle = text.encode()
    count = int.from_bytes(data[8:10], "big")
    pos = 10
    i = 1
    hits = []
    while i < count:
        tag = data[pos]
        if tag == 1:  # Utf8
            ln = int.from_bytes(data[pos + 1:pos + 3], "big")
            if data[pos + 3:pos + 3 + ln] == needle:
                hits.append((pos + 3, ln))
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
    return hits


def force_bin(data):
    """
    把 getDistributionUrl 里「all / bin」的选择改成恒为 bin。

    原逻辑：flag ? "bin" : "all"，再拼成 gradle-<版本>-<bin|all>.zip。
    flag 为 false 时得到 -all.zip，而 Studio 的哈希清单只有 -bin.zip 条目，
    按文件名查不到就返回 null，写 wrapper 时就保留了文件里的旧哈希——
    「URL 换成 9.8.2、哈希还是 9.6.0」正是这么来的。

    常量池里 "all" 与原生 "bin" 是两个独立条目，长度都是 3，
    把 "all" 原地改成 "bin" 即可；断言唯一，避免误伤同名常量。
    """
    hits = utf8_hits(data, "all")
    if not hits:
        return data  # 已经是 bin，幂等跳过
    if len(hits) > 1:
        raise RuntimeError("常量池里 all 条目数为 %d，预期 1，已放弃改动" % len(hits))
    start, ln = hits[0]
    if ln != 3:
        raise RuntimeError("all 条目长度为 %d，预期 3" % ln)
    return data[:start] + b"bin" + data[start + ln:]


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


def pick_text_base(jar):
    """与 pick_base 同一思路：优先用原始副本，保证重复运行不叠加改动。"""
    backup = jar + ".original"
    if os.path.isfile(backup) and not bad_entries(backup):
        return backup, "原始备份"
    bad = bad_entries(jar)
    if bad:
        raise RuntimeError("jar 已损坏，无法安全重建：%s" % sorted(bad))
    shutil.copy2(jar, backup)
    return jar, "当前 jar"


def patch_template_jar(jar, entries, restore):
    """
    把 jar 里若干文本模板中的官方主机换成镜像（restore 时反向）。
    只替换主机段 services.gradle.org/distributions/ -> mirrors.tencent.com/gradle/，
    https\\:// 转义写法和 ${version} 之类的占位符都原样保留。
    返回 (重建基准说明, [(条目, 改后全文)])。
    """
    base, label = pick_text_base(jar)
    src, dst = (HOST_NEW, HOST_OLD) if restore else (HOST_OLD, HOST_NEW)

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tpl.jar.new")
    results = []
    with zipfile.ZipFile(base) as zin:
        names = set(zin.namelist())
        missing = [e for e in entries if e not in names]
        if missing:
            raise RuntimeError("jar 内缺少条目：%s" % ", ".join(missing))

        if os.path.exists(tmp):
            os.remove(tmp)
        with zipfile.ZipFile(tmp, "w") as zout:
            zout.comment = zin.comment
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename in entries:
                    text = data.decode("utf-8")
                    new = text.replace(src, dst)
                    results.append((info.filename, new))
                    data = new.encode("utf-8")
                zout.writestr(info, data)

    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
        if bad:
            os.remove(tmp)
            raise RuntimeError("重建后校验失败：%s" % bad)
    os.replace(tmp, jar)
    return label, results


def default_project_roots(home):
    """常见的工程根目录，存在哪个就扫哪个。"""
    roots = []
    for name in ("AndroidStudioProjects", "StudioProjects", "Projects"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            roots.append(p)
    return roots


def iter_wrapper_files(roots, max_depth=6):
    for root in roots:
        base_depth = root.rstrip("\\/").count(os.sep)
        for dp, dn, fn in os.walk(root):
            if dp.count(os.sep) - base_depth > max_depth:
                dn[:] = []
                continue
            if "gradle-wrapper.properties" in fn:
                yield os.path.join(dp, "gradle-wrapper.properties")


def _read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def fix_one_wrapper(path, checksums, restore=False):
    """
    把工程里的 wrapper 校准成「镜像地址 + 对应版本的官方哈希」。
    优先按 distributionUrl 里的版本查清单；查不到就只归一主机、不动哈希。
    只改这两行，其余原样保留（不重排、不改注释）。
    返回 (状态, 说明)，状态取值 ok / fixed / skip。
    """
    text, enc = _read_text(path)
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

    if url_idx is None or not raw_url:
        return "skip", "没有 distributionUrl"

    src, dst = (HOST_NEW, HOST_OLD) if restore else (HOST_OLD, HOST_NEW)
    new_raw = raw_url.replace(src, dst)
    host_changed = new_raw != raw_url

    url = new_raw.replace("\\:", ":").replace("\\=", "=")
    name = url.rsplit("/", 1)[-1]
    if not name.endswith(".zip"):
        return "skip", "地址不是 zip：%s" % name

    want = checksums.get(name)
    sha_changed = bool(want) and (not cur or cur.lower() != want.lower())
    if not host_changed and not sha_changed:
        return "ok", name

    notes = []
    if host_changed:
        lines[url_idx] = "distributionUrl=" + new_raw
        notes.append("主机改为%s" % ("官方源" if restore else "腾讯云镜像"))
    if sha_changed:
        if sha_idx is not None:
            lines[sha_idx] = "distributionSha256Sum=" + want
        else:
            lines.insert(url_idx + 1, "distributionSha256Sum=" + want)
        notes.append("%s: %s -> %s" % (
            name,
            (cur[:8] + "…") if cur else "缺失",
            want[:8] + "…"))

    bak = _backup(path)
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(nl.join(lines) + nl)
    return "fixed", "%s（备份 %s）" % ("；".join(notes), os.path.basename(bak))


def fix_wrappers(roots, checksums, restore=False):
    """扫描工程，校准 wrapper 的主机与哈希。返回 (修正数, 一致数, 跳过数)。"""
    if not roots:
        print("项目 wrapper: 没找到工程目录，跳过")
        return 0, 0, 0

    fixed = same = skipped = 0
    for path in iter_wrapper_files(roots):
        try:
            state, info = fix_one_wrapper(path, checksums, restore)
        except Exception as e:
            print("  跳过 %s（%s）" % (path, e))
            skipped += 1
            continue
        if state == "fixed":
            fixed += 1
            print("  已修正 %s\n           %s" % (path, info))
        elif state == "ok":
            same += 1
        else:
            skipped += 1
            print("  跳过 %s（%s）" % (path, info))
    print("项目 wrapper: 修正 %d，已一致 %d，跳过 %d" % (fixed, same, skipped))
    return fixed, same, skipped


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
        if not restore:
            cls_new = force_bin(cls_new)
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


def studio_root_from_jar(jar):
    """.../plugins/android/lib/android.jar -> Android Studio 安装根目录"""
    d = os.path.dirname(jar)
    for _ in range(3):
        d = os.path.dirname(d)
    return d


def main():
    raw = sys.argv[1:]
    args = [a for a in raw if not a.startswith("--")]
    restore = "--restore" in raw
    home = next((a.split("=", 1)[1] for a in raw if a.startswith("--home=")), None)
    proj_arg = next((a.split("=", 1)[1] for a in raw if a.startswith("--projects=")), None)
    check_wrappers = "--no-wrapper-check" not in raw

    log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "patch_studio_gradle_url.log"), "w", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log)
    atexit.register(log.flush)

    print("== 第一步：init 脚本（依赖镜像 + wrapper 任务地址改写，无需管理员）==")
    ensure_init_script(restore, home)

    if "--init-only" in raw:
        print()
        print("仅维护 init 脚本，已跳过 jar 改写")
        return 0

    print()
    print("== 第二步：Gradle 发行版地址（android.jar，需管理员）==")
    extra = {} if restore else fetch_checksums()
    jar = find_jar(args[0] if args else None)
    jar_ok = False
    if not jar:
        print("未找到 android.jar，请传入 Android Studio 安装目录")
    else:
        print("目标 jar:", jar)
        try:
            base, label = pick_base(jar)
            print("重建基准:", label, base)
            tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_android.jar.new")
            try:
                cls_bytes, tpl_text, sha_text, added = build(base, tmp, restore, extra)
                os.replace(tmp, jar)
                fix_acl(jar)
            except Exception:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            jar_ok = True
        except Exception as e:
            print("失败: %s: %s" % (type(e).__name__, e))
            print("（Studio 正在运行会锁住 jar；镜像那部分已生效，可稍后重跑）")

    if jar_ok:
        with zipfile.ZipFile(jar) as z:
            n = len(z.namelist())
            chk = z.testzip()
        want = OLD_PATTERN if restore else NEW_PATTERN
        print("写入结果: %s" % ("已还原官方地址" if restore else "腾讯云镜像"))
        print("jar 条目数 %d，完整性 %s" % (n, "OK" if chk is None else chk))
        print("格式串: %s" % ("已命中" if want.encode() in cls_bytes else "未命中"))
        all_hits = utf8_hits(cls_bytes, "all")
        print("all/bin 选择: %s" % ("已固定为 bin" if not all_hits else "仍可生成 all"))
        for line in tpl_text.splitlines():
            if line.startswith("distributionUrl="):
                print("模板地址:", line)
        print("哈希清单: %d 条，本轮新增 %d 条" % (len(sha_text.splitlines()), added))
        for ver in ("9.8.2", "9.6.0"):
            key = "gradle-%s-bin.zip" % ver
            val = next((l.split(";")[0] for l in sha_text.splitlines()
                        if l.endswith(";" + key)), None)
            print("  %s -> %s" % (key, val or "缺失"))

    print()
    print("== 第三步：其它向导模板（Compose 等，需管理员）==")
    if not jar:
        print("未找到 Studio 安装目录，跳过")
    else:
        root = studio_root_from_jar(jar)
        targets = list(OTHER_JARS)
        if "--with-gradle-plugin-jar" in raw:
            targets.append(GRADLE_PLUGIN_JAR)
        else:
            print("跳过 %s（gradle 插件模块 jar，改写会让 IDE 整个模块加载失败）"
                  % GRADLE_PLUGIN_JAR[0])
        for rel, entries in targets:
            path = os.path.join(root, rel)
            if not os.path.isfile(path):
                print("跳过（不存在）: %s" % rel)
                continue
            try:
                label, results = patch_template_jar(path, entries, restore)
                fix_acl(path)
            except Exception as e:
                print("失败 %s: %s: %s" % (rel, type(e).__name__, e))
                continue
            print("%s（基准: %s）" % (rel, label))
            for entry, text in results:
                for line in text.splitlines():
                    if "distributionUrl" in line:
                        print("  %s" % line.strip())

    if not check_wrappers:
        return 0 if jar_ok else 1

    print()
    print("== 第四步：校准已有工程的 wrapper ==")
    roots = proj_arg.split(";") if proj_arg else default_project_roots(home or os.path.expanduser("~"))
    fix_wrappers(roots, extra, restore)

    print()
    print("重启 Android Studio 后新建项目验证" if jar_ok else "jar 未改动，其余已完成")
    return 0 if jar_ok else 1


if __name__ == "__main__":
    sys.exit(main())
