# Android Studio 新建项目走腾讯云 Gradle 镜像

让 Android Studio 新建项目时，Gradle 发行版和依赖都从腾讯云下载，不再卡在 `services.gradle.org` / `dl.google.com`。

针对环境：Android Studio 2026.2.2（`C:\Program Files\Android\Android Studio`）、Windows。

## 两个独立的问题

新建项目时慢的地方其实是两个，脚本一次把两层都配好：

| 问题 | 下载内容 | 处理方式 |
| --- | --- | --- |
| ① 项目依赖 | AGP、androidx、第三方库 | 自动维护 `%USERPROFILE%\.gradle\init.d\mirrors.gradle`（不需要管理员） |
| ② Gradle 发行版 | `gradle-9.x-bin.zip`，约 140 MB | 改 `android.jar` 内的地址常量（需要管理员） |

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

# 还原：地址回官方，init 脚本停用
python patch_studio_gradle_url.py --restore
```

脚本按「依赖镜像 → jar」的顺序执行，所以即使 jar 因为 Studio 正在运行而写入失败，镜像那部分也已经生效。

写入后**重启 Android Studio**（`android.jar` 在启动时加载），再新建项目验证生成的文件：

```properties
distributionUrl=https\://mirrors.tencent.com/gradle/gradle-9.6.0-bin.zip
```

脚本每次运行都会在工作区生成 `patch_studio_gradle_url.log`，可删。

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
2. `templates/project/wrapper/gradle/wrapper/gradle-wrapper.properties` 的 `distributionUrl`，作为备用一致性修改。
3. `templates/project/gradle-sha256-list.txt`，用官方版本索引补全（见下节）。

class 常量池靠索引引用、没有绝对偏移，所以在原位置替换 UTF8 条目即可，长度变化无需修补其他结构（类文件 16691 → 16693 字节，正好是字符串长度差）。

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

## 为什么不会触发校验失败

生成的 wrapper 文件里有 `distributionSha256Sum`（官方清单里的值）。腾讯云那份包是**官方原包**——实测 9.6.0、9.8.2 的 sha256 均与官方一致，校验能通过。这点如果不过，wrapper 会直接拒绝运行，所以脚本保留 sha256 不动、只补全缺失条目。

## 幂等与备份

- `android.jar`：首次运行备份为 `android.jar.original`，之后每次都**以这个干净副本为基准重建**，重复运行不会叠加改动。
- 重建后自动做 `testzip()` 全量 CRC 校验并检查条目数（45537）。
- `android.jar` 未签名，改动不会被验签拒绝。
- `mirrors.gradle`：内容一致就不动；内容不同则备份成 `mirrors.gradle.bak-20261102-085841` 这种带时间戳的名字再写，**不会覆盖你已有的手写脚本**。`--restore` 同样只是改名停用，不删除。

## 依赖镜像（脚本自动维护）

`%USERPROFILE%\.gradle\init.d\mirrors.gradle` 用 `beforeSettings` 在项目 settings 脚本之前注入腾讯云仓库，优先级高于工程自带的 `google()` / `mavenCentral()`，对所有项目生效，不用改任何工程的 build 文件。

文件头带 `// managed by patch_studio_gradle_url.py` 标记，脚本靠它判断「这是自己生成的、可以安全更新/停用」。想改内容请改脚本里的 `INIT_TEMPLATE`，直接改生成的文件下次运行会被覆盖。

腾讯云的 `maven-public` 连 Google Maven 都代理，AGP 和 androidx 构件可直连命中。

## 注意事项

- **Studio 升级会覆盖 `android.jar`**，升级后需重跑一次脚本（会弹 UAC）。
- 快照版 Gradle 会指向镜像上不存在的路径，但新建项目向导不会用快照版本，实际碰不到。
- 已存在的项目不受影响：它们的 `gradle-wrapper.properties` 在工程目录内，模板管不到，需要时手动改那一行。

## 已经报校验失败的项目怎么救

改 `gradle/wrapper/gradle-wrapper.properties` 里那一行即可，两种都行：

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
