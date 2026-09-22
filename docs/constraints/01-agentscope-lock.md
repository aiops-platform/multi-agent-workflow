# §1 AgentScope 锁定 2.0.3

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：动依赖 / 升级 AgentScope / 改模型接入时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

1. **AgentScope 锁定 2.0.3**（design §5）。升级前必须重跑 S-001/S-011；升级后 streaming
   事件 API 可能变化。模型统一走 `agents/config`（DeepSeek `deepseek-v4-flash`）。
   ⚠️ **输出上限必须显式设**（`deepseek_max_tokens`，默认 8192，两个 builder 都传
   `Parameters(max_tokens=…)`）：不设就走 provider 默认，模型写长一点会被**截在句子中间**，
   而下游只看到「agent 未输出合法 JSON」—— 与"JSON 写坏了"无法区分（实测 `reviewer`
   两次挂在这上面：run_74a0db73ae / run_3f977237be，见 `docs/TODO.md` §32⑤）。
   判据一句话：**任何"模型输出解析失败"的报错，都要能看出是断了还是写飞了**
   （`AgentOutputError` 现在带头 + 尾 + 总长，且**不折叠空白** —— 裸换行显示 `\n`、
   合法转义显示 `\\n`，这两种的诊断结论完全相反）。
   **写飞**的那一类里，字符串裸换行最常见（长中文散文）：`extract_json` 用
   `json.loads(strict=False)` 容忍它 —— RFC 8259 不许裸控制字符，但模型的意图毫无歧义，
   拿标准去惩罚它只会把"散文换了行"变成整条 run 中止（实测 run_c2c44f9ff8 的 recap）。
