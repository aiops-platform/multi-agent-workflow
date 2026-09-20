# 删除租户 SOP

> 2026-09-20。起因：`team-x` 的 `db_ref` 解不开，`tenantctl deprovision --confirm-delete`
> **直接崩在解密那一步**，而当时没有备用路径。

---

## 一句话流程

```
先体检（默认，不动手）→ 核对四件事 → 加 --yes 执行
./venv/bin/python scripts/delete_tenant.py <tenant>          # 体检
./venv/bin/python scripts/delete_tenant.py <tenant> --yes    # 执行
```

**"删除"= 管理库 `status → deleted` + 删掉它的独立库（如果有）。**
管理库那**一行会保留**——那是审计痕迹，不是没删干净。
`deleted` 的租户不再被 API 注册表与 sweeper 纳入（它们只取 `status=active`）。

---

## 标准路径（先试这个）

```bash
./venv/bin/python -m agentflow.tenantctl deprovision <tenant> --confirm-delete
```

它做两件事：置 `status=deleted`；删租户库。

**但它有个前提：`db_ref` 必须能解密**——因为要读出来才知道删哪个库。
解不开时它会抛 `ValueError: db_ref 解密失败（密钥不匹配？）`，**整条命令中断**。

---

## 三种会卡住的情况

### ① `db_ref` 解不开（最常见）

**症状**

```
ValueError: db_ref 解密失败（密钥不匹配？）:
cryptography.exceptions.InvalidSignature: Signature did not match digest.
```

**原因**：现在的 `AGENTFLOW_SECRET_KEY` 与**当初加密它时**用的不是同一把。
换过 key，或当初根本没配 `AGENTFLOW_SECRET_KEY`、是从 `jwt_secret` 派生的
（派生逻辑见 `agents/../api/management_store.derive_secret_key`）——之后 `jwt_secret`
一改，旧密文就再也解不开了。

**处置**：用体检脚本。它**不信 `db_ref` 声称的位置**，改用第 4 步
「扫各租户库的 `runs.tenant_id`」确定**数据实际在哪**，再据此决定删什么。

**不要**为了删这一个租户去翻找旧 key——那会把整批租户的密文都暴露在风险里。

### ② 租户库指向共享基础库

`drop_tenant_database` 会**拒绝执行**并打日志：

```
租户库指向共享基础库 agentflow，拒绝删除
```

**这是保护，不是 bug**——v5.3 之前所有租户的 db_ref 都指向共享库，
无脑 DROP 会把管理库连同全部租户数据一起抹掉。

**处置**：这类租户**本来就没有自己的库**，删它 = 只置 `status=deleted`。
脚本的第 4 步会显示"未在任何租户库中找到该租户的 run"，据此判断。

### ③ 数据落在别的库 / 库名有同形字

历史上出现过**用非 ASCII 同形字**建出的库名（实测见过
`agentflow-ot` + 西里尔字母 `ор`，肉眼与 `agentflow-otr` 几乎一样）。
体检脚本会列出**含非 ASCII 的库名**并标警告。

**处置**：人工确认它属于谁，再手工 DROP。
`psql` 里用 `encode(convert_to(datname,'UTF8'),'hex')` 看真实字节，别信终端显示。

---

## 脚本做了什么

`scripts/delete_tenant.py` 默认**只体检**，打印四件事：

| # | 查什么 | 为什么 |
|---|---|---|
| 1 | 管理库租户行（status / isolation / namespace） | 确认对象存在、当前状态 |
| 2 | `db_ref` 能否解密 | 不能则说明原因并**明确警告不要信它的位置** |
| 3 | 按命名约定 `{基础库}-{tenant}` 的库是否存在 | 常规路径 |
| 4 | **扫各租户库的 `runs.tenant_id`** | **数据实际在哪——db_ref 不可信时以此为准** |

然后打印**计划**（要改什么、要删什么），`--yes` 才执行。

### 护栏

- **默认只体检**，必须显式 `--yes`
- **永不删 `agentflow`（基础库）**——管理库就在里面，删了所有租户连坐
- **永不删 `postgres` / `template0` / `template1`**
- 命中受保护库名时**直接退出**（不是跳过继续）

---

## 实测案例：`team-x`（2026-09-20）

```
1) status=deleted  isolation=standard  namespace=agentflow-team-x
2) db_ref 解不开：ValueError        ← 标准路径会崩在这里
3) 约定名 agentflow-team-x 不存在
4) 未在任何租户库中找到 'team-x' 的 run
```

**结论**：它是在旧的「共享 DSN」方案下开通的，**没有独立库、没有数据**，
删除 = 只置 `status=deleted`。

删除后 **sweeper 报错归零**（此前每轮 `InvalidSignature` 一次）。

---

## 顺带说明：sweeper 的逐租户隔离

删 `team-x` 之前，sweeper 每轮都失败一次：

```
sweeper 一轮失败 → _build(team-x) → _resolve_ref → fernet.InvalidToken
```

而 `run_once` 当时**没有 per-tenant 容错**——一个租户抛异常就**中断整轮**，
排在它后面的租户全部扫不到。`team-x` 按字母序恰好排最后才没伤到别人，**纯属运气**。

已修（commit `5624dc8`）：每个租户一个 try/except，失败记日志并跳过。

> 这类"一个坏租户饿死其余租户"的问题**不报错**，只表现为"某些租户的审批超时没被处理"。
> 排查时若发现"某几个租户的审批卡着不动"，先看 sweeper 日志里有没有
> `租户 X 扫描失败`。
