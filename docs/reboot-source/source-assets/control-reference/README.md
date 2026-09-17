# Control reference only

本目录中的文件来自旧 Sidecar，只用于参考经过验证的 SSH、Runner 调用、部署、派发和监控细节。

它们依赖旧数据库表、`tx.py`、project/run/attempt 三层状态机和旧 AppState，不能直接导入新 Control。迁移时先按新需求写接口和测试，再复制无领域耦合的小函数。

已知必须修正的安全边界：

- `ssh/transport.py` 使用 `known_hosts=None`，新实现必须改成首次展示并确认指纹、后续固定指纹的 TOFU 流程。
- `services/agent_deploy.py` 把 Token 拼入 shell，并用宽泛 `pkill -f`；新实现必须使用 0600 配置/服务管理器凭据并精确定位服务实例。
- 远端路径和命令存在字符串拼接；新实现优先使用 SFTP 和结构化参数，必须做转义与路径 containment。
- `security.py` 的 query token 只可作为旧 WS 参考；新实现不得让普通 HTTP 接口接受 URL query 中的 capability token。
- `keychain.py` 的 FileStore 需要安全 ID、原子写入、目录权限和 symlink 防护后才能复用。

M3 的规范要求见 `09-v1.3审查修订与决策门.md`。
