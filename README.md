# Android Studio 新建项目走腾讯云 Gradle 镜像

让 Android Studio 新建项目时，Gradle 发行版和依赖都从腾讯云下载，不再卡在 `services.gradle.org` / `dl.google.com`。

针对环境：Android Studio 2026.2.2（`C:\Program Files\Android\Android Studio`）、Windows。

两个脚本，职责分开：

| 脚本 | 作用 | 权限 |
| --- | --- | --- |
| `patch_studio_gradle_url.py` | 一次性配置：依赖镜像 + 拦截 wrapper 任务 + 改三个 jar 里的地址常量与向导模板 + 刷新哈希清单 + 校准已有工程 | 需管理员（写 Program Files） |
| `fix_wrapper_sha.py` | 日常维护：改了工程里的版本号后，把哈希刷成官方值，并把主机换回镜像 | 不需要，Studio 开着也能跑 |

## 两个独立的问题

新建项目时慢的地方其实是两个，`patch_studio_gradle_url.py` 一次把两层都配好：

| 问题 | 下载内容 | 处理方式 |
| --- | --- | --- |
| ① 项目依赖 | AGP、androidx、第三方库 | 自动维护 `%USERPROFILE%\.gradle\init.d\mirrors.gradle`（不需要管理员） |
| ② Gradle 发行版 | `gradle-9.x-bin.zip`，约 140 MB | 改 `android.jar` 内的地址常量（需要管理员） |

发行版地址有**三个写入源**，只堵一个就会出现「改了又变回去」：`android.jar` 的常量（非 Compose 向导）、`kotlin-plugin.jar` 的 Compose 模板、以及 Gradle 自己的 `wrapper` 任务。脚本和前两层打交道用改 jar，最后一层用 init 脚本拦截——所以 `--init-only` 也值得单独跑一次。

## 用法

```bash
# 两层一起配（jar 部分需要管理员权限，会弹 UAC）
python patch_studio_gradle_url.py

# 只配依赖镜像，不碰 android.jar（不需要管理员，Studio 开着也能跑）
python patch_studio_gradle_url.py --init-only

# 手动指定安装目录
python patch_studio_gradle_url.py "D:/Android Studio"

# 指定用户目录（默认 %USERPROFILE%）
python patch_studio_gradle_url.py --home=D:/Users/me

# 指定要扫描的工程目录（默认扫描 %USERPROFILE% 下的常见工程位置）
python patch_studio_gradle_url.py --projects=D:/work;D:/demo

# 跳过第三步（不扫描已有工程）
python patch_studio_gradle_url.py --no-wrapper-check

# 还原：地址回官方，init 脚本停用，工程地址也回官方
python patch_studio_gradle_url.py --restore
```

脚本按「init 脚本 → android.jar → 其它插件模板 → 校准已有工程」的顺序执行。前两步分别对应「依赖慢」和「发行版慢」，第三步处理 Compose 等工程用的另一套模板，第四步把你机器上**已经存在**的工程 wrapper 校准过来——包括你手动改了版本号但没改哈希、或者被 Studio 改回官方主机的那些。

配完**重启 Android Studio**（这些 jar 在启动时加载），再新建项目验证生成的文件：

```properties
distributionUrl=https\://mirrors.tencent.com/gradle/gradle-9.6.0-bin.zip
```

`patch_studio_gradle_url.py` 每次运行都会在工作区生成 `patch_studio_gradle_url.log`，可删。

### 改了版本号之后：用 fix_wrapper_sha.py 刷哈希

`patch_studio_gradle_url.py` 要写 `Program Files`，每次都得弹 UAC、关 Studio，只为了改一行哈希不值得。所以单独提供了 `fix_wrapper_sha.py`——只查官方索引、只改工程里的 `gradle-wrapper.properties`：

```bash
# 在你改过的工程目录里执行（会递归找 wrapper）
python fix_wrapper_sha.py

# 指定工程目录，一次处理多个
python fix_wrapper_sha.py D:/projects/Demo D:/projects/Other

# 直接指文件
python fix_wrapper_sha.py .\gradle\wrapper\gradle-wrapper.properties

# 只检查不修改（看看到底哪里对不上）
python fix_wrapper_sha.py --check

# 强制重新拉取官方索引（默认缓存 6 小时）
python fix_wrapper_sha.py --refresh

# 查看索引缓存状态
python fix_wrapper_sha.py --list

# 不联网，只用已有缓存
python fix_wrapper_sha.py --offline

# 只修哈希，不改主机
python fix_wrapper_sha.py --host=keep

# 反向：把地址改回官方源
python fix_wrapper_sha.py --host=official
```

不需要管理员权限，Studio 开着也能跑。索引来自 `https://services.gradle.org/versions/all`，缓存在 `%USERPROFILE%\.gradle\gradle-sha256-index.json`，所以第二次运行几乎是瞬时的。

默认会做两件事：把官方主机换回 `mirrors.tencent.com`，并把 `distributionSha256Sum` 刷成该版本的真实值。其余内容（开头的时间戳注释、键的顺序、`\:` 转义、换行风格）原样保留；原文件备份成同目录下的 `gradle-wrapper.properties.bak`（单一滚动备份，覆盖旧的，不会堆文件）。查不到哈希时（版本号写错、或版本还没发布）只归一主机、不瞎写哈希。

## 为什么不能只改模板

Studio 2026.2.2 **不会照抄**模板里的 `distributionUrl`。它读模板拿到 `distributionBase` 等字段后，由 `GradleWrapper$Companion.getDistributionUrl()` 自己拼完整地址：

```java
String.format("https://services.gradle.org/%1$s/%2$s",
              isSnapshot ? "distributions-snapshots" : "distributions",
              "gradle-9.6.0-bin.zip");
```

再从 `templates/project/gradle-sha256-list.txt` 补上官方 sha256，最后用 `Properties.store()` 重写文件——所以生成的文件键名是字母序、带 `#Mon Nov 02 08:35:00 CST 2026` 这样的时间戳。模板里写什么都会被覆盖，这是早期只改模板没生效的原因。

## 改了什么

`android.jar` 内三处：

1. `com/android/tools/idea/gradle/util/GradleWrapper$Companion.class` 常量池里的格式串
   `https://services.gradle.org/%1$s/%2$s` → `https://mirrors.tencent.com/gradle/%2$s`
   丢掉 `%1$s`，因为腾讯云是 `/gradle/` 这一层，没有 `/distributions/`（该路径实测 404）。
2. 同一个类里把「`all` / `bin`」的选择固定为 `bin`（见下节）。
3. `templates/project/wrapper/gradle/wrapper/gradle-wrapper.properties` 的 `distributionUrl`，作为备用一致性修改。

另外 `templates/project/gradle-sha256-list.txt` 用官方版本索引补全（见「第三个坑」）。

`kotlin-plugin.jar`、`intellij.gradle.jar` 内各一到两处：向导模板里的主机段
`services.gradle.org/distributions/` → `mirrors.tencent.com/gradle/`（见「第五个坑」）。

class 常量池靠索引引用、没有绝对偏移，所以在原位置替换 UTF8 条目即可，长度变化无需修补其他结构（类文件 16691 → 16693 字节，正好是字符串长度差）。`all` → `bin` 是等长替换（都是 3 字节），常量池索引完全不变，属于最安全的一种改法。模板是纯文本条目，整体重写并由 zipfile 重算 CRC。

## 第三个坑：哈希清单是发布时的快照

Studio 用「Switch to Gradle 9.8.2」这类意图操作改 wrapper 时，URL 会换成新版本，但 `distributionSha256Sum` 是从 `gradle-sha256-list.txt` 里按文件名查出来的。这份清单是 Studio 发布时的快照，比它新的版本查不到值，Studio 就**沿用文件里原有的旧哈希**，于是：

```
Distribution Url: https://mirrors.tencent.com/gradle/gradle-9.8.2-bin.zip
Expected checksum: 'bbaeb2fe…'   ← 9.6.0 的哈希，没跟着换
Actual checksum:   '21bd5da2…'   ← 9.8.2 的真实哈希
Verification of Gradle distribution failed!
```

清单里只有 518 条，9.8.2 根本不在其中（只有 `gradle-9.8.0-20260722000220+0000` 这个夜间快照）。所以第 3 步用 `https://services.gradle.org/versions/all` 刷新清单，现在 536 条，任何正式版都能查到正确哈希。

确认 `21bd5da2…` 就是官方值的依据：`services.gradle.org/distributions/gradle-9.8.2-bin.zip.sha256` 返回的正是它，而 wrapper 从腾讯云下载后算出的实际值也是它——两边一致，说明镜像那份就是官方原包。

## 第四个坑：`-all.zip` 永远查不到哈希

`getDistributionUrl(version, boolean)` 的第二个参数不是「快照」，而是「发行包类型」：

| 参数 | 生成的 URL | 清单里有哈希吗 |
| --- | --- | --- |
| `true` | `gradle-9.8.2-bin.zip` | 有 |
| `false` | `gradle-9.8.2-all.zip` | **没有**，`getDistributionSha256()` 返回 `null` |

清单里只有 `-bin.zip` 条目，`-all.zip` 一条都没有。返回 `null` 时 Studio 不会清空 `distributionSha256Sum`，而是保留文件里的旧值，于是又是一次「哈希与版本不匹配」。

而腾讯云确实提供了 `-all.zip`（264 MB），所以这条路不会 404 失败，只会静默地校验失败——比 404 更难查。

`all` 和 `bin` 在常量池里是两个独立的 3 字节条目，把 `all` 原地改成 `bin` 后，两种参数都产出 `-bin.zip`，哈希也都能查到。反射验证结果：

```
flag=false  url=…/gradle-9.8.2-bin.zip  sha=21bd5da2…
flag=true   url=…/gradle-9.8.2-bin.zip  sha=21bd5da2…
```

## 第五个坑：点了「Switch to Gradle 9.8.2」，Host 又变回官方

现象：新建工程后点 Studio 提示里的 `Switch to Gradle 9.8.2 and re-sync`，`gradle-wrapper.properties` 变成

```properties
distributionUrl=https\://services.gradle.org/distributions/gradle-9.8.2-bin.zip
```

而 `android.jar` 里明明改过了。原因是两件事叠加：

**① Compose 工程根本不用 `android.jar` 的模板。** 它走 Kotlin 插件里的

- `kotlin-plugin.jar` / `fileTemplates/code/compose-gradle-wrapper.properties.ft`
- `kotlin-plugin.jar` / `.../projectWizard/templates/gradle/gradle-wrapper.properties.vm`

这两份把 `https\://services.gradle.org/distributions/gradle-${...}-bin.zip` 硬编码在文本里，也没有 `distributionSha256Sum` 行——生成出来就是「5 个键、官方地址、不校验哈希」，和报错工程的文件一字不差。

**② Studio 改版本时会跑 Gradle 自带的 `wrapper` 任务**，由它重写 properties。日志里能看到：

```
About to execute Gradle tasks: [wrapper]
Test of distribution url https://services.gradle.org/distributions/gradle-9.8.2-bin.zip failed.
    at org.gradle.api.tasks.wrapper.Wrapper.validateDistributionUrl(Wrapper.java:190)
```

这个任务按「现有 URL」或官方 `DistributionLocator` 决定写什么，**不看 Studio 的模板**。所以模板改对了也会被它重写回官方。

修法两层：

1. 把上面两个模板里的主机段改掉（脚本第三步，需要管理员）；
2. 在 `init.d/mirrors.gradle` 里拦住这个任务（脚本第一步，不需要管理员）：

```groovy
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
```

拦截这一层比改模板更管用：不管 Studio 从哪条路径触发 `wrapper` 任务（向导、Switch 意图、Sync 时的版本校验），写出来的地址都会被换回镜像。

实测（Gradle 9.8.2 真实执行，含 URL 校验）：

```
> Task :wrapper
BUILD SUCCESSFUL in 2s

distributionUrl=https\://mirrors.tencent.com/gradle/gradle-9.8.2-bin.zip
networkTimeout=10000
validateDistributionUrl=true
```

## 第六个坑：改 `intellij.gradle.jar` 会让 Studio 起不来

`plugins/gradle-plugin/lib/intellij.gradle.jar` 里也有一个 `Gradle Build Script with wrapper.gradle.ft`，看起来该一起改。改了之后 Studio 启动就炸：

```
java.lang.NoClassDefFoundError:
  org/jetbrains/plugins/gradle/service/project/open/GradleProjectOpenProcessor
Caused by: java.lang.ClassNotFoundException:
  org.jetbrains.plugins.gradle.util.GradleBundle PluginClassLoader(...)
```

日志里还有 `Can't find bundle for base name messages.GradleBundle [Plugin: com.intellij.gradle]` —— 整个 jar 一个类都加载不到，说明这个 content module 被 IDE 整包跳过了。

排查过、都没问题：

- 990 个条目与原 jar 一一对应，只有目标模板 -7 字节，其余 CRC、时间戳、属性全一致；
- `testzip()` 通过，`JarFile` / `ZipInputStream` / `URLClassLoader` 都能正常读，类也都在；
- 文件 ACL、路径、命名都对。

同一个脚本重建的 `android.jar`（45537 条）和 `kotlin-plugin.jar`（25540 条）都正常，唯独这个 5.6 MB 的小 jar 不行，原因没查明。它只影响「Java 库工程」那套老式 `task wrapper` 模板，对 Android 开发没有价值，所以脚本默认跳过，要改得显式加 `--with-gradle-plugin-jar`。

已经踩了的，把备份还原回去即可（需要管理员）：

```powershell
$lib = 'C:\Program Files\Android\Android Studio\plugins\gradle-plugin\lib'
Copy-Item "$lib\intellij.gradle.jar.original" "$lib\intellij.gradle.jar" -Force
```

顺带一个附带的坑：`os.replace` 往 Program Files 里写新文件会**丢掉 Users 的读权限**（继承被切断，只剩 SYSTEM/Administrators/当前用户）。IDE 以别的账户跑就读不到，现在脚本每次写完都会 `icacls /inheritance:e` 补回来。

## 第四步：校准已有工程

`distributionUrl` 在工程目录里，jar 管不到，所以改完 jar 后会扫描已有工程：

- 主机是 `services.gradle.org/distributions/` 就换回镜像；
- 从 `distributionUrl` 里取版本号，去清单查真实哈希，与 `distributionSha256Sum` 比对，不一致就改；
- 改前留一份 `gradle-wrapper.properties.bak-<时间戳>` 备份，`.bak-*` 等非标准文件跳过。

这一步也覆盖「手动把版本号从 9.6.0 改成 9.8.2、哈希忘了改」和「被 Studio 改回官方主机」两种情况——不过日常更推荐用 `fix_wrapper_sha.py`（见上面「改了版本号之后」），它不需要管理员权限、不用关 Studio。

## 为什么不会触发校验失败

生成的 wrapper 文件里有 `distributionSha256Sum`（官方清单里的值）。腾讯云那份包是**官方原包**——实测 9.6.0、9.8.2 的 sha256 均与官方一致，校验能通过。这点如果不过，wrapper 会直接拒绝运行，所以脚本保留 sha256 不动、只补全缺失条目。

## 幂等与备份

- 三个 jar 都是首次运行备份为 `<jar>.original`，之后每次都**以这个干净副本为基准重建**，重复运行不会叠加改动；实测连跑两次产物字节完全一致。
- 重建后自动做 `testzip()` 全量 CRC 校验：`android.jar` 45537 条目、`kotlin-plugin.jar` 25540 条目、`intellij.gradle.jar` 990 条目。
- 三个 jar 均未签名，改动不会被验签拒绝。
- `mirrors.gradle`：内容一致就不动；内容不同则备份成 `mirrors.gradle.bak-20261102-085841` 这种带时间戳的名字再写，**不会覆盖你已有的手写脚本**。`--restore` 同样只是改名停用，不删除。

## init 脚本（脚本自动维护）

`%USERPROFILE%\.gradle\init.d\mirrors.gradle` 现在管两件事：

1. **依赖仓库**：用 `beforeSettings` 在项目 settings 脚本之前注入腾讯云仓库，优先级高于工程自带的 `google()` / `mavenCentral()`，对所有项目生效，不用改任何工程的 build 文件。
2. **wrapper 任务地址**：用 `gradle.allprojects` + `tasks.withType(Wrapper)` 把 Studio 触发 `wrapper` 任务时要写的官方地址换成镜像（见「第五个坑」）。

文件头带 `// managed by patch_studio_gradle_url.py` 标记，脚本靠它判断「这是自己生成的、可以安全更新/停用」。想改内容请改脚本里的 `INIT_TEMPLATE`，直接改生成的文件下次运行会被覆盖。

腾讯云的 `maven-public` 连 Google Maven 都代理，AGP 和 androidx 构件可直连命中。

## 注意事项

- **Studio 升级会覆盖 `android.jar`、`kotlin-plugin.jar`、`intellij.gradle.jar`**，升级后需重跑一次脚本（会弹 UAC，且要先退出 Studio）。
- 快照版 Gradle 会指向镜像上不存在的路径，但新建项目向导不会用快照版本，实际碰不到。
- 已有工程由第四步自动校准；如果后来又手动改过版本号或被 Studio 改回官方主机，重跑脚本或 `fix_wrapper_sha.py` 即可，不用手抄哈希。
- 工程里出现没有 `distributionSha256Sum` 的文件是正常的：Compose 模板本来就不写这一行，不代表被改坏。

## 已经报校验失败的项目怎么救

最快的是在工程目录里跑一句：

```bash
python fix_wrapper_sha.py
```

它会照着 `distributionUrl` 里的版本查官方哈希写回去，并把主机换回镜像，只改这两行。也可以手改：

```properties
# 方式一：换成目标版本的真实哈希（推荐）
distributionSha256Sum=21bd5da2de49b642904077f38c0efdc7778e92b3ed0889392dca61b97e389607

# 方式二：删掉这一行，wrapper 就不做校验
```

真实哈希从这取（把版本号换掉）：

```bash
curl -sSL https://services.gradle.org/distributions/gradle-9.8.2-bin.zip.sha256
```

判断期望值和 URL 是否配套：`distributionUrl` 里是 `gradle-9.8.2-`，`distributionSha256Sum` 就必须是 9.8.2 的哈希。上面那次报错就是 URL 换成了 9.8.2 而哈希还停在 9.6.0。

改完可直接 Sync，不需要删缓存：wrapper 校验失败会把下载的 zip 删掉（只留一个 `.lck` 锁文件），下次同步会重新下载并按新哈希校验。
