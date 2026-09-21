# CMDB 业务域 + 术语表 草稿（待人工审校）

> **状态**：草稿，**未写入任何 CMDB 文件**。用途是给 `design-v5.8.md` §3.4.4 / §4.5 的
> 业务语义层提供一份可审校的初稿。
> 审校通过后，内容应落到 `aiops-mcp-servers/.../data/cmdb-entities.json`。

## ⚠️ 这份草稿的局限（审之前必读）

**我不知道你们的用户实际怎么说话。** testbed 是合成的（`acme` / `aiops-test-*`）。所以：

| 部分 | 可信度 | 说明 |
|---|---|---|
| **层次结构**（谁属于哪个 portfolio / journey） | **较高** | 从**真实**的 `owner` / `tech` / 调用图推出来的，可核对 |
| **`description`** | 中 | 描述的是服务职能，从真实字段能推个八九不离十 |
| **`keywords`** | **低——必须替换** | 我按常见说法填的。**真正有价值的词只能来自你们已归档的工单** |

**`keywords` 的判据（来自 §2.4）**：不由"这个业务域是什么"决定，
而由"**用户会怎么描述它出问题**"决定。写「售后服务选择」是业务视角、不会有人这么说；
写「退货」「换货」「申请售后没反应」才是问题视角。

**建议的做法**：先攒 20~30 张已归档工单，把里面的名词/动作抽出来，替换掉下面的占位词。
在那之前，这份表的**结构**可用，**词**别当真。

---

## 派生依据（全部来自真实数据）

**10 个服务的真实 owner**（这是业务域的原始线索）：

```
网关团队  交易履约  售后保障  支付团队  库存团队
定价团队  履约调度  平台基础  用户中心  安全合规
```

**真实调用图**（决定业务流向）：

```
gateway ──┬─→ order ──┬─→ pricing          (定价)
          │           ├─→ inventory ──┬─→ logistics   (物流)
          │           │               └─→ pricing
          │           ├─→ payment ────┬─→ audit
          │           │               └─→ notification
          │           ├─→ warranty               (售后)
          │           └─→ notification
          └─→ user ──────→ audit                 (账户)
```

业务主流程（从调用图读出，不是编的）：
**用户 → 网关 → 订单 → {定价 / 库存 / 支付 / 售后} → 物流 → 通知**

---

## 一、Enterprise（企业功能）

| id | display_name | description | keywords（待替换） |
|---|---|---|---|
| `enterprise:retail-ecommerce` | 零售电商 | 面向消费者的在线零售业务，含下单、支付、履约与售后 | 零售, 电商, 商城, 购物, 买, 卖货 |

> ⚠️ **只提了一个。** 你给的例子（销售 / 售后 / 零售 / 理赔）是并列多个。理由：我们只有
> 10 个服务、全都属于同一套电商下单链路，**硬拆成多个 enterprise 是造结构**。
> 若你希望按参考模型的粒度拆（例如「销售」与「售后」两个 enterprise），告诉我，我改。

---

## 二、Journey（用户旅程）

| id | display_name | capability | 覆盖的 portfolio | keywords（待替换） |
|---|---|---|---|---|
| `journey:purchase-to-delivery` | 下单到收货 | Purchase to Delivery | 订单交易 / 支付结算 / 库存履约 | 下单, 购物, 买东西, 提交订单, 到货, 没收到, 发货慢 |
| `journey:after-sales` | 售后服务 | After-sales Service | 售后服务 / 订单交易 | 售后, 退货, 换货, 保修, 报修, 申请售后 |
| `journey:account` | 账户管理 | Account Management | 用户账户 | 登录, 注册, 账号, 会员, 个人信息 |

> ⚠️ **`platform-support` 不属于任何 journey**——见下面第三节的说明。这是**诚实的缺口**，
> 不要为了"每个 portfolio 都要挂在某条旅程下"而硬接。

---

## 三、Portfolio（业务域）

### 属于旅程的

| id | display_name | description | 含哪些 service | keywords（待替换） |
|---|---|---|---|---|
| `portfolio:order-transaction` | 订单交易 | 接收用户下单请求、维护订单状态、计算价格 | `order-service`, `pricing-service` | 订单, 下单, 下不了单, 提交订单, 订单状态, 价格, 算价, 优惠券, 报价 |
| `portfolio:payment-settlement` | 支付结算 | 扣款、退款与交易结算 | `payment-service` | 支付, 付款, 扣款, 付不了, 支付失败, 退款, 结算, 账单, 重复扣款 |
| `portfolio:inventory-fulfillment` | 库存履约 | 库存管理与发货配送 | `inventory-service`, `logistics-service` | 库存, 缺货, 无货, 超卖, 发货, 出货, 物流, 配送, 快递, 收货, 签收 |
| `portfolio:after-sales` | 售后服务 | 退换货与保修受理 | `warranty-service` | 售后, 退货, 换货, 保修, 维修, 报修, 售后入口, 申请退货没反应 |
| `portfolio:user-account` | 用户账户 | 注册登录与用户资料 | `user-service` | 登录, 登陆, 注册, 账号, 密码, 登不上, 用户信息, 会员 |

### ⚠️ 不属于任何旅程的：平台支撑

| id | display_name | description | 含哪些 service | keywords（待替换） |
|---|---|---|---|---|
| `portfolio:platform-support` | 平台支撑 | 消息通知、审计合规与流量入口 | `notification-service`, `audit-service`, `gateway-service` | 通知, 短信, 消息, 收不到通知, 推送, 审计, 日志, 网关, 502, 504, 请求超时 |

**这一格我犹豫了，需要你判断**：

- `notification-service` / `audit-service` 是**支撑服务**，没有"用户旅程"可言——
  用户不会说"我在用通知服务"。它们的问题描述通常是**技术现象**（"收不到短信"、"网关 502"）
- `gateway-service` 是**流量入口**，把它塞进 `platform-support` 是个妥协
- 三个 owner 完全不同（平台基础 / 安全合规 / 网关团队），合成一个 portfolio 是**为了不造更多层级**

**备选**：拆成 `notification` / `audit-compliance` / `access-gateway` 三个 portfolio，
或干脆让它们**不挂 portfolio**（但那样就违反了"app 必须归属 portfolio"）。

**这条要你定**——我倾向拆开，因为"平台支撑"这个名字本身就是"我不知道该叫什么"的产物。

---

## 四、需要新建的边

按 `design-v5.8.md` §3.4.2 的模型，本表对应以下边（**当前尚不存在**）：

```
enterprise:retail-ecommerce  ──enterprise_journey──→  journey:purchase-to-delivery
                                                     journey:after-sales
                                                     journey:account

journey:purchase-to-delivery ──journey_link──→ portfolio:order-transaction
                                              portfolio:payment-settlement
                                              portfolio:inventory-fulfillment
journey:after-sales          ──journey_link──→ portfolio:after-sales
                                              portfolio:order-transaction
journey:account              ──journey_link──→ portfolio:user-account

portfolio:order-transaction  ──portfolio_link──→ app:order-service, app:pricing-service
...（每个 portfolio 连到它的 service）
```

**注意 `journey:after-sales` 同时连 `portfolio:after-sales` 和 `portfolio:order-transaction`**
——这不是错，是**真实的多对多**：售后流程确实要回到订单。这也正是 §3.4 交叉验证的素材来源。

---

## 五、审校清单（请逐条给意见）

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | Enterprise 只有一个够不够？ | 够。硬拆是造结构 |
| 2 | `platform-support` 拆不拆？ | **拆**成三个。现在这个名是"不知道该叫什么"的产物 |
| 3 | `gateway-service` 归哪？ | 拆出来后归 `access-gateway`；它确实不属于任何业务域 |
| 4 | 支撑类服务没有 journey，接受吗？ | 接受。硬接一条假旅程比空着更糟 |
| 5 | `order-transaction` 含 `pricing-service` 对吗？ | 对——定价是下单的一部分，且 order 直接调它 |
| 6 | **`keywords` 全部重写** | 拿你们真实工单抽词替换。**这是本表唯一真正重要的动作** |
| 7 | 需要 `business_role`（app 级）吗？ | 需要，但可以晚一步——先有 portfolio/journey 层就能跑通 §3.4 |

---

## 六、状态与这份草稿没做什么

**已完成**（2026-09-15）：`design-v5.8.md` §3.4.7 的**本体部分已实施**——删 `cross_journey_hub` /
`cross_journey_link`、加 `domain` / `domain_link`、加 `enterprise_journey` / `app_codebase`、
全部边加 `layer`、`app.attributes` 加 `kind`、`refs`→`app_codebase` 边、**删掉 6 个派生 Portfolio**。
`description` / `keywords` 字段已加到信封层，20 个节点（10 app + 10 codebase）已填充**派生值**。

**仍未做**：

- **本表的业务层内容没有写入 CMDB**——`enterprise` / `journey` / `portfolio` / `domain`
  四类节点数**仍为 0**。本表是"录入前的提案"，不是已落地的数据
- **没有**替代 §2.4 要求的"从真实工单抽词"——我只是给了个能跑起来的起点
- 每个 app 节点**已有** `keywords`（从服务名/owner/tech 机械派生），但那些词
  **不含任何真实用户用语**——用户说「结账卡住」时仍然匹配不上
