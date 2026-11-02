import java.io.ByteArrayOutputStream;
import java.io.FileOutputStream;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.security.ProtectionDomain;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Studio 镜像补丁代理。
 *
 * 只做一件事：把 Studio 里两处硬编码 / 拼接出来的 Gradle 官方地址换成腾讯云镜像。
 * 之所以用 javaagent 而不是改 jar，是因为改 studio 插件 jar 会带来模块加载失败的风险，
 * 而这里是在类加载进内存时改常量，硬盘上的 jar 一个字节都不动，删掉 vmoptions 里的一行即可完全回退。
 *
 * 两处目标：
 * 1) org.jetbrains.plugins.gradle.util.GradleEnvironment$Urls
 *    getGradleServicesUrl() 的默认值 "https://services.gradle.org"
 *    （只有 GradleUtil 一个调用者）
 * 2) org.jetbrains.plugins.gradle.util.GradleUtil
 *    拼接格式 "%s/%s/gradle-%s-bin.zip"（base, "distributions", version）
 *    改成 "%s/gradle-%s-bin.zip"，把中间那段 distributions 去掉，
 *    这样 base=https://mirrors.tencent.com/gradle 时得到的正是
 *    https://mirrors.tencent.com/gradle/gradle-X-bin.zip
 */
public final class MirrorAgent {

    private static final Map<String, String> RULES = new LinkedHashMap<String, String>();

    static {
        RULES.put("https://services.gradle.org", "https://mirrors.tencent.com/gradle");
        RULES.put("%s/%s/gradle-%s-bin.zip", "%1$s/gradle-%3$s-bin.zip");
    }

    private static final String[] TARGETS = {
        "org/jetbrains/plugins/gradle/util/GradleUtil",
        "org/jetbrains/plugins/gradle/util/GradleEnvironment$Urls",
    };

    public static void premain(String args, Instrumentation inst) {
        try {
            inst.addTransformer(new Patcher());
            log("agent loaded");
        } catch (Throwable t) {
            log("premain failed: " + t);
        }
    }

    private static final class Patcher implements ClassFileTransformer {
        @Override
        public byte[] transform(ClassLoader loader, String className, Class<?> classBeingRedefined,
                                ProtectionDomain pd, byte[] buffer) {
            if (className == null || buffer == null) return null;
            boolean hit = false;
            for (String t : TARGETS) {
                if (t.equals(className)) { hit = true; break; }
            }
            if (!hit) return null;
            try {
                byte[] out = patch(buffer);
                if (out != null) log("patched " + className + " (" + buffer.length + " -> " + out.length + ")");
                return out;
            } catch (Throwable e) {
                log("patch failed on " + className + ": " + e);
                return null;
            }
        }
    }

    /** 遍历常量池，把命中的 UTF8 条目原地换成新字符串（长度可变，后续条目顺移）。 */
    private static byte[] patch(byte[] b) throws Exception {
        if (b.length < 10 || b[0] != (byte) 0xCA || b[1] != (byte) 0xFE) return null;
        int cpCount = u2(b, 8);
        int[] start = new int[cpCount];
        int p = 10;
        for (int i = 1; i < cpCount; i++) {
            start[i] = p;
            int tag = b[p] & 0xFF;
            switch (tag) {
                case 1: p += 3 + u2(b, p + 1); break;          // Utf8
                case 3: case 4: p += 5; break;                  // Integer / Float
                case 5: case 6: p += 9; i++; break;             // Long / Double 占两个槽位
                case 7: case 8: case 16: case 19: case 20: p += 3; break;  // Class/String/MethodType/Module/Package
                case 9: case 10: case 11: case 12: case 17: case 18: p += 5; break;
                case 15: p += 4; break;                          // MethodHandle
                default: return null;                            // 未知 tag，放弃
            }
        }
        int cpEnd = p;

        ByteArrayOutputStream out = new ByteArrayOutputStream(b.length + 64);
        out.write(b, 0, 10);
        boolean changed = false;
        for (int i = 1; i < cpCount; i++) {
            int s = start[i];
            int end = (i + 1 < cpCount) ? start[i + 1] : cpEnd;
            if ((b[s] & 0xFF) == 1) {
                int len = u2(b, s + 1);
                String str = new String(b, s + 3, len, "UTF-8");
                String rep = RULES.get(str);
                if (rep != null && !rep.equals(str)) {
                    byte[] nb = rep.getBytes("UTF-8");
                    out.write(1);
                    out.write((nb.length >>> 8) & 0xFF);
                    out.write(nb.length & 0xFF);
                    out.write(nb, 0, nb.length);
                    changed = true;
                    continue;
                }
            }
            out.write(b, s, end - s);
        }
        if (!changed) return null;
        out.write(b, cpEnd, b.length - cpEnd);
        return out.toByteArray();
    }

    private static int u2(byte[] b, int off) {
        return ((b[off] & 0xFF) << 8) | (b[off + 1] & 0xFF);
    }

    private static void log(String msg) {
        try {
            String home = System.getProperty("user.home", ".");
            String line = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(new Date()) + "  " + msg + "\r\n";
            FileOutputStream fos = new FileOutputStream(home + "/.gradle/mirrors-agent.log", true);
            try { fos.write(line.getBytes("UTF-8")); } finally { fos.close(); }
        } catch (Throwable ignored) {
        }
    }
}
