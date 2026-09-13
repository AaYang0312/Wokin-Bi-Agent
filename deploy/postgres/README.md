# PostgreSQL（Docker 化）

替代原本机便携版集群 `D:\Projects\pg17`。**端口、认证方式、时区、排序规则都与迁移前一致**，
所以 `.env.app` / `.env.test` / `.env.sync*` 里的 DSN 一个字都不用改。

| 项 | 值 | 迁移前 |
| --- | --- | --- |
| 版本 | `postgres:17.6`（Debian） | 17.6（windows-msvc） |
| 宿主端口 | `0.0.0.0:54329` | 本机回环 + 本机 tailnet IP : 54329 |
| 超级用户 | `postgres`，trust 免密 | 同 |
| 角色 | `bi_app` / `bi_reader` / `bi_sync`（含各自 `SET` 参数） | 同 |
| 库 | `bi_agent`（生产）、`bi_agent_test`（测试） | 同 |
| 编码/排序 | UTF8 / `C` / libc provider | 同 |
| 时区 | `Asia/Shanghai`（含 `log_timezone`） | 同 |
| 其它参数 | `DateStyle=ISO, MDY`、`max_connections=100`、`shared_buffers=128MB`、`wal_level=replica` | 同（逐字对齐旧 `postgresql.conf`） |
| 新增 | `log_min_duration_statement=1000`（慢查询进 `docker compose logs`） | 旧集群默认 -1 不记 |
| 数据位置 | 命名卷 `bi-agent-pg_bi_pg_data` | `D:\Projects\pg17\data` |

## 日常启停

在本目录执行：

```powershell
cd D:\Projects\bi-agent\deploy\postgres
docker compose up -d          # 首次或需要重建容器时
docker compose start          # 常规启动
docker compose stop           # 常规停止（不删卷）
docker compose ps             # 健康状态（healthcheck: pg_isready）
docker compose logs -f postgres
docker exec -it bi-agent-postgres psql -U postgres
```

Docker Desktop 已配置为登录自启（`HKCU\...\Run` 有 `Docker Desktop` 项），容器是
`restart: unless-stopped`，因此正常情况下随开机恢复，不需要手动拉起。

**禁止** `docker compose down -v` / `docker volume rm bi-agent-pg_bi_pg_data` —— 那会连数据一起删。

## 连接方式（四条路径，均已实测）

| 客户端位置 | 连接串 | 备注 |
| --- | --- | --- |
| 本机 | `postgresql://postgres@localhost:54329/bi_agent` | 与迁移前完全相同 |
| tailnet 其他机器（直连、推荐） | `postgresql://bi_sync@<本机 tailscale 机器名>:54329/bi_agent` | 免密；MagicDNS 机器名已实测可解析+登录，比写 IP 稳（IP 会变）。对端无 tailscale DNS 时用 `tailscale ip -4` 查到的本机 IP |
| tailnet 其他机器（直连、按 IP） | `postgresql://bi_sync@<本机 tailnet IP>:54329/bi_agent` | 同上，仅名字解析不可用时用；不要把具体 IP 提交进仓库 |
| tailnet 其他机器（SSH 隧道） | `ssh -L 5433:127.0.0.1:54329 <本机 SSH 别名>` 后连 `localhost:5433` | 隧道落点仍是 `127.0.0.1:54329`，迁移前后同一行为；别名在对端 `~/.ssh/config`，不写入仓库 |

对端 Mac 实测记录（2026-09-13）：分别用 MagicDNS 机器名与本机 tailnet IP 直连 54329，
以 `bi_sync` / `bi_reader` 握手均返回 `AuthenticationOk`；上表 SSH 隧道路径（`localhost:5433` →
本机 `127.0.0.1:54329`）以 `bi_reader` 同样通过。

## 备份与还原

```powershell
backup.cmd                                  # → backup\<时间戳>\{globals.sql,bi_agent.dump,bi_agent_test.dump,SHA256.txt}
import.cmd <备份目录>                        # 还原（角色只在空集群需要；对象用 --clean --if-exists 覆盖）
import.cmd <备份目录> skipglobals             # 角色已存在时
```

- 备份产物在 `backup/`，已被 Git 忽略（含真实经营数据与角色定义）。
- 卷级冷备份（先 `docker compose stop`）：
  `docker run --rm -v bi-agent-pg_bi_pg_data:/from -v D:/backups:/to alpine tar czf /to/pgdata.tgz -C /from .`
- 换宿主：拷 `deploy/postgres` + 一个备份目录 → `docker compose up -d` → `import.cmd <备份目录>`。
- 逻辑备份不含口令哈希（PG15 起 `pg_dumpall` 默认不导出）。trust 认证下无影响；
  将来若改 scram，需手工 `\password` 重建口令。

## 安全边界（改端口/改 hba 前先读）

Docker Desktop 会改写入站连接的源地址：容器里 `pg_stat_activity.client_addr` 实测是
`172.21.0.1`（Docker 网关），**不是**真实的 tailnet IP。所以 `pg_hba.conf` 里的地址白名单
只是意图声明，起不到访问控制作用。真正的边界只有两处：

1. 端口发布范围 —— 当前 `"54329:5432"` 即所有宿主接口；
2. Windows 防火墙入站规则 —— 当前 tailnet 可通达（迁移前后行为一致）。

认证是 `trust`：**任何能连上 54329 的主机都能以 `postgres` 超级用户免密进库**。按暴露面从宽到严：

| 目标 | 做法 |
| --- | --- |
| 保持现状：tailnet 免密直连 | 什么都不改（本文件默认） |
| 只放行 tailnet，关掉局域网 | 管理员 PowerShell：`New-NetFirewallRule -DisplayName "bi-agent pg tailnet only" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 54329 -RemoteAddress 100.64.0.0/10 -Profile Any`，并确认没有其他规则放行 54329 |
| 只允许本机 | `docker-compose.yml` 端口改 `"127.0.0.1:54329:5432"`，远端一律走 SSH 隧道 |
| 要密码 | `pg_hba.conf` 换 `scram-sha-256` + `\password` 设口令 + 同步改所有 DSN（含 `README.md`/runbook 示例） |

## 回滚到旧便携版

旧集群只是停掉了，数据目录 `D:\Projects\pg17\data` 与二进制 `D:\Projects\pg17\pgsql` 都还在。
两者都占 54329，不能同时运行：

```powershell
cd D:\Projects\bi-agent\deploy\postgres
docker compose stop
D:\Projects\pg17\pgsql\bin\pg_ctl.exe -D D:/Projects/pg17/data -o "-p 54329" -l D:/Projects/pg17/pg.log -w start
```

## 迁移校验记录

`pg_dumpall --globals-only` + 两库 `pg_dump -Fc` 还原后，逐表 `count(*)` 与「全行文本按序 md5」
与迁移前**逐字节一致**（视图定义也单独比对过）。容器 `stop/start`、以及把 `DateStyle`/`shared_buffers`
改成与旧 conf 一致并 recreate 之后，又各自重跑了一次对比，25/25 表一致。
对比文件留在 `D:\Projects\pg17\migration-20260912\parity.before.txt` / `parity.after.txt` /
`parity.old.txt` / `parity.now.txt`（含 `globals.sql` 与两个 `.dump`，均在仓库外，不进 Git）。

校验用的生成器（同一 SQL 分别跑旧集群临时端口 54330 与容器 54329，再 `diff`）：

```bash
P=/d/Projects/pg17/pgsql/bin
for db in bi_agent bi_agent_test; do
  $P/psql.exe -h 127.0.0.1 -p "$PORT" -U postgres -d "$db" -Atc \
    "select n.nspname||'.'||c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace
       where c.relkind in ('r','p') and n.nspname in ('bi','reporting') order by 1" | tr -d '\r' |
  while read -r t; do
    $P/psql.exe -h 127.0.0.1 -p "$PORT" -U postgres -d "$db" -Atc \
      "select '$db|' || '$t|' || count(*)::text || '|'
             || coalesce(md5(string_agg(t::text, '|' order by t)),'EMPTY')
         from $t t"
  done
done
```

注意：md5 聚的是行文本，而行文本跟着会话参数（尤其 `DateStyle`）变；两边必须用同一套参数，
否则哈希不同只是表示法差异，不是数据差异——这也是把 `DateStyle` 改回 `ISO, MDY` 的原因之一。

### 上游迁移补齐（2026-09-13，仓库前进 75 个提交后）

卷里的数据快照是 2026-09-12 从便携版集群原样搬过来的；当天之后上游仓库把代码与 DDL 移进
`backend/`，迁移文件从 `backend/sql/004_query_runtime.sql` 一路补到 `016_channel_catalog.sql`。
容器内两个库已按 `docs/runbook.md` 的顺序补齐并复验：

| 库 | 补跑前 | 补跑 | 补跑后 |
| --- | --- | --- | --- |
| `bi_agent` | 009（query_provenance 无 basis 三列） | 014 → 015 → 016 | 016 |
| `bi_agent_test` | 001（缺 002 的 `unified_status` 及其后全部对象） | 002 → 004 → 005 → 007 → 008 → 009 → 014 → 015 → 016 | 016 |

`003_kuaimai_metric_semantics.sql` 只重定义了 `reporting.v_product_daily`，而 005 / 007 已经把它换成
更宽的版本；PostgreSQL 的 `CREATE OR REPLACE VIEW` 不允许减列，所以 005 / 007 之后不能再覆盖 003——
它已在 007 的宽版本里（003 的 8 列 + `product_name` / `product_name_snapshot` / `sku_label`）。
新库仍按 001 → 002 → 003 → … 顺序跑，旧库补迁移时跳过 003。

补跑前各自存了 schema 前快照 `pg_dump -Fc`（`\Projects\pg17\migration-20260912\pre-schema-*.dump`，
不进 Git），补跑后逐表 `count(*)` 与全行 md5 与补跑前逐字节一致：唯一差异是 016 新建的空表
`bi.channel_items`，以及 015 新加三列后行文本变化（拿旧列重算 md5 不变）。迁移都是前向、可重跑
的 DDL，不会重写历史事实；需要重算的数据（例如 `basis='items_merged'` 的支付事实）按 runbook
显式 `replay`。

重跑方式（缺哪个补哪个，单库执行）：

```powershell
cd D:\Projects\bi-agent
psql -h localhost -p 54329 -U postgres -d bi_agent -v ON_ERROR_STOP=1 -f backend\sql\014_multi_source_contract.sql
```

## 已知差异

- 数据在 Docker 卷里，不能用资源管理器直接看文件；`D:\Projects\pg17\data` 从此不再更新。
- `docker system prune --volumes` / 重装 Docker Desktop 会连带删库 —— 定期跑 `backup.cmd`。
- 便携版客户端工具（`psql.exe` / `pg_dump.exe`，与服务端同为 17.6）可以继续用来连容器，
  命令行示例见 `docs/runbook.md`；只有服务端部分被容器取代。
