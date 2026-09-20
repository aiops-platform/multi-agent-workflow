import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 预热标记测试——**它不是产品测试，也不验证任何业务逻辑**。
 *
 * 它存在的唯一理由是：让 `./gradlew test` 真的有东西可跑。
 * 没有测试源码时 gradle 报 `NO-SOURCE`，**不会解析 testCompileClasspath /
 * testRuntimeClasspath**，于是依赖根本没被下载下来——烤出来的缓存是残的。
 * （这个坑在测试床本身上刚踩过一次：预热完的缓存跑离线测试时报
 * `No cached version of org.apiguardian:apiguardian-api`。）
 *
 * 断言写成恒真即可：预热只关心"四条 classpath 都被解析并下载"，
 * 不关心结果。别把这个文件复制到别处当测试用。
 */
class WarmupMarkerTest {

    @Test
    void warmupMarker() {
        assertTrue(true, "预热标记，永远为真");
    }
}
