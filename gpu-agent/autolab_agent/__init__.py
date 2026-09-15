"""AutoLab gpu-agent —— 服务器端执行代理（0.1.2：HMAC 清单校验默认强制）。

能力（M1/v0）：
- POST /attempts（dispatch_id 幂等 + manifest HMAC 可选校验）→ 事件 ack/remote_start/running
- GET /attempts/{dispatch_id}?tail=（日志尾 + 状态 + 累计用量）
- POST /attempts/{dispatch_id}/stop（进程组 SIGTERM → SIGKILL；stop_ack 事件）
- GET /events?since_seq&agent_instance_id（事件流；实例轮换返回 INSTANCE_ROTATED）
- GET /healthz（免令牌，仅 loopback）
- usage 采样（M1：RUNNING 期间墙钟秒，deviation #7）；execution_cap 硬截止（ADD-5：超限击杀
  + deadline_exceeded_ack 事件携带最终累计用量）
- 每 GPU 单任务（一卡一队列，D15）：同卡任务排队 REMOTE_QUEUED

沙箱（Z1 三用户/systemd transient）：M1 后置（明文跑通后收紧——开工指令授权）；
HMAC 校验由 --require-hmac 开启。
"""
__version__ = "0.1.5-m2"
SCHEMA_VER = "1"
