# E2E reference only

这些测试展示了旧工程如何启动真实 Control/Runner 子进程、创建 fixture、等待状态和清理进程。

新工作区应保留场景与装配方式，但必须改写 API、状态和断言；不要为了继续运行旧测试而恢复旧数据模型。

这些文件不可作为“当前测试已通过”的证据：它们依赖旧 sidecar 目录、旧 `/attempts` 接口、旧状态机，且部分 HMAC 假设已与参考 Runner 默认配置不一致。新 E2E 必须从 `contracts-v1/` 重写。
