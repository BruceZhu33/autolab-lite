# 迁移规范（SQLite）

- **前向只迁移（forward-only）**：不写 downgrade。回退 = 用备份重建（桌面单机应用，库随用户数据目录走）。
- 命名 `NNNN_描述.sql`，`NNNN` 单调递增；每个迁移**整体包在一个 `BEGIN IMMEDIATE; … COMMIT;`** 中，末尾 `INSERT INTO schema_migrations(version, name)`。
- 迁移只允许 `CREATE/ALTER TABLE … ADD COLUMN/CREATE INDEX/CREATE TRIGGER` 与数据回填；**不得**：
  - 修改既有列类型或约束（SQLite ALTER 改列 = 重建表；重建表会短暂破坏 FK 与触发器，若不可避免须在新迁移中按"建新表→拷贝→改名"三步并停机执行）；
  - 触碰带不可变 trigger 的表（plan_protocols / code_snapshots / training_recipes / initialization_specs / evaluation_results / predictions / test_result_releases / evaluator_snapshots / api_idempotency）的既有行。
- 应用启动时执行：`PRAGMA foreign_keys=ON` → 读 `schema_migrations` → 逐个执行未应用迁移（每迁移一事务）→ `PRAGMA foreign_key_check` 必须为空，否则启动失败进入安全模式（设计 §4.5 启动握手）。
- 升级迁移属于三平台验收矩阵项（D48）；Windows 路径/锁差异由平台适配器处理，迁移脚本本身保持纯 SQL。
- `0001_init.sql` 由 `schema.sql` 经 `generate.sh` 生成；**基线发布后 schema.sql 的任何变更都走新增迁移，同时 schema.sql 同步更新**（schema.sql 始终代表"全新安装的完整结构"）。

## 运行手册（复审 O3）

1. **迁移前自动备份**：执行任何迁移前，平台适配器先对数据库文件做文件级副本（`<db>.pre-migrate-<version>.bak`，WAL 模式下执行 `PRAGMA wal_checkpoint(TRUNCATE)` 后复制三件套：db/-wal/-shm）；备份失败 → 中止升级并提示。
2. **磁盘空间检查**：可用空间 ≥ 2× 当前库体积（含 WAL）才允许执行；不足 → 明确报错，不静默半途失败。
3. **迁移后校验**：`PRAGMA foreign_key_check` 为空 ∧ `schema_migrations` 无缺口（版本连续）∧ 关键不可变 trigger 抽查存在；任一失败 → 用步骤 1 备份恢复并上报。
4. **恢复演练**：三平台验收矩阵（D48）各含一次"升级失败 → 备份恢复"演练用例；新迁移合入前在 CI 用上一版本库跑一遍升级+回滚路径。

