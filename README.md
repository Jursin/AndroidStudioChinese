# AndroidStudioChinese

在 JetBrains 官方语言包基础上，补充 Android Studio 未被覆盖的字符串

产物为两个文件：languagepack jar 与 bootclasspath jar

验证环境：Android Studio 2026.2.2

## 工作方式

| 文案                            | 实际通道                                   | 补充方式                                |
| ------------------------------- | ------------------------------------------ | --------------------------------------- |
| 平台、JetBrains 插件            | `DynamicBundle`                            | 语言包插件                              |
| AS、Google 自身的 `.properties` | `ResourceBundle` + AS 插件类加载器         | `*_zh_CN.properties` 置于 bootclasspath |
| 菜单、工具栏动作                | 内联于 `plugin.xml`，或硬编码于 `AnAction` | 启动时按 action id 覆盖                 |

语言包与动作覆盖合并在同一个插件 jar 中；`_zh_CN` 资源必须是独立文件，且不能位于插件目录内

## 用法

语言包按 AS 版本自动匹配：AS `2026.2.2.9` → IDEA `2026.2`（平台构建号 `262`，即 AS `product-info.json` 里 `buildNumber` 的前三位）

```bash
AS=~/.local/share/JetBrains/Toolbox/apps/android-studio
PD=~/.local/share/Google/AndroidStudio2026.2.2         # AS 插件目录，重启后生效

python3 scripts/zhpack.py fetch --for-version 2026.2 \
    --out upstream/localization-zh-262.jar
python3 scripts/zhpack.py build-all --ide-home $AS \
    --upstream upstream/localization-zh-262.jar --out dist/zh-as-262.jar
python3 scripts/zhpack.py install-pack   --jar dist/zh-as-262.jar    --ide-home $AS
python3 scripts/zhpack.py install-native --jar dist/zh-native-262.jar
```

`build-all` 同时产出 `dist/zh-as-262.jar`（插件）与 `dist/zh-native-262.jar`（资源），后者由 `install-native` 复制到插件目录之外并写入 vmoptions

重启 AS，在 `Settings → Appearance & Behavior → System Settings → Language and Region` 中选择 `Chinese`
回滚方式：执行 `install-native --remove`，并删除 `$PD/zh-as-262.jar`

## 翻译维护

- 文案：`translations/messages/<Bundle>.properties`，仅写入需要覆盖的键
- 动作：`translations/actions.properties`，格式为 `actionId=中文`
- 定位：`python3 scripts/zhpack.py locate --ide-home $AS "目标字符串"`
- 专有名词（Gradle / Java / Kotlin / AGP / SDK / JVM / Compose / Activity / APK）保留原文；build=构建、clean=清理
- 修改后重新执行 `build-all` 与两条 `install-*` 命令

## 许可

本项目代码采用 Apache License 2.0（`LICENSE`）。构建产物包含取自 JetBrains 官方「中文语言包」的译文，
该语言包随 IntelliJ IDEA 分发，适用 Apache License 2.0 及 JetBrains Open Source Build Terms；
对上游的修改记录在 `NOTICE`，两者也会打进插件 jar 的 `META-INF/`。产物为非官方构建，
`plugin.xml` 的 `<vendor>` 已标明。

## 注意事项

- 同一 `zh-CN` 仅允许一个语言包生效，需先卸载其他中文语言包
- AS 升级后重新执行 `build-all` 与 `install-*`；`PD` 路径与版本号需同步更新
- vmoptions 中该行指向插件目录外的资源 jar；文件不存在时 JVM 会忽略该条目，不影响 IDE 启动
- `-Duser.language=zh` 影响依赖默认 locale 的格式化行为，如不需要可移除对应两行
- 无法通过资源文件覆盖：由 `update()` 动态拼接的文本（如 `Assemble 'xxx' Run Configuration`）及未注册 id 的动作
- 当前覆盖率：353 个 bundle / 48 342 键，已覆盖 93.5%（缺 3 140）；动作 33 条

辅助命令：`audit` 查看覆盖率，`audit --stubs DIR` 生成待翻译文件，`audit-actions` 导出 action id 清单
