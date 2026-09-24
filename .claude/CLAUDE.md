<!-- CODEGRAPH_START -->
## CodeGraph（代码检索的首选，优先于 grep）

**在已被 CodeGraph 索引的仓库里（根目录存在 `.codegraph/`），理解为定位代码时先用它，
再用 grep/find 或逐文件读：**

- **MCP 工具**（可用时）：`codegraph_explore` 一次调用就能回答多数代码问题 ——
  给出相关符号的**逐字源码**外加它们之间的调用链，包括 grep 跟不到的动态分派跳转。
  在查询里点名文件或符号，就能读到它当前带行号的源码。
- **命令行**（MCP 不可用时始终可用）：
  `codegraph explore "<符号名或问题>"` 输出与上面一致；`callers` / `callees` / `impact`
  直接答"谁调用谁、改前波及什么"。

**若根目录没有 `.codegraph/`，则完全跳过 CodeGraph** —— 建索引是使用者的决定，不要自作主张。
本仓的建索引方式见 `docs/tooling/README.md`（一条命令）。

> 这段指令由 `codegraph install` 生成，措辞保留原文（它的三条优先级/降级/兜底
> 比手写的严谨）。团队落地说明与两个已知坑见 `docs/tooling/README.md`。
<!-- CODEGRAPH_END -->
