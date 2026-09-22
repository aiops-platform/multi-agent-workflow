# podman VM 时钟漂移 —— 症状在应用层，根因在宿主机

**判据**（两条，都很短）：

1. **别信 `chronyc tracking`** —— 它照样报 `System clock synchronized: yes`。
   **直接跟宿主对表**。
2. **所有容器读数一模一样 ⇒ 偏的是 VM 内核，不是某个容器**
   （`podman exec` 进五个容器逐个查 `date -u`）。

**代价**（症状完全静默，两类）：

| 症状 | 为什么 |
|---|---|
| Kibana / Grafana **"什么都没有"** | 数据都在、集群内时间戳也对 —— 但「Last 15 minutes」是**浏览器**解析成绝对时间戳，两边差十几小时 |
| **同一行两个时间列差 8 小时** | PG 侧 `CURRENT_TIMESTAMP` 写的列偏（如 `problem_record.resolved_at`），应用侧 Python 写的不偏 → 看的人会以为是**业务问题** |

**去向**：✅ 已机器化（`~/bin/podman-clock-guard.sh` + launchd，阈值 5s）。

---

## 为什么它自己修不回来（三条叠加）

1. **NTP 源全部不可达** —— 企业网络挡 UDP/123。`chronyc sources` 四个源 `Reach` **恒为 `0`**
   （显示 `^?`），而 DNS 正常、ping 通，**只有 NTP 不通**。
2. 配置是 `makestep 1.0 3` —— 只允许**前 3 次更新内**步进，之后只肯**缓慢 slew**。
   8 小时的偏差靠 slew 要跑很久。
3. 于是 chronyd **自由运行**：`chronyc makestep` 返回 `200 OK` 却**什么也不做**（空操作）。

## 对表（容器**不用重启**）

```bash
podman machine ssh podman-machine-v5 \
  "sudo date -u -s '$(date -u '+%Y-%m-%d %H:%M:%S')'"
```

## 一个值得记的"自证陷阱"

`chronyc tracking` 的 `System clock synchronized: yes` 与
`System time: 0.0 seconds slow of NTP time` **在时钟已经偏了 8 小时时依然这么报** ——
因为它说的是"与我上次同步相比漂了多少"，而不是"与真实时间差多少"。

**同族判据**（与 `docs/lessons/silent-failure-family.md` 是同一类）：
**一个信号在它本该报警的时候依然报绿，它就不是证据。**
换台机器/换个环境时，先把这类"自证"信号换成**外部基准**（这里是宿主机时钟）再判断。
