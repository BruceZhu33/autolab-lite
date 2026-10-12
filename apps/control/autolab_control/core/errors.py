"""错误分类:API 层只按类型映射状态码,业务代码不再各自 try/except 转 HTTP。"""

from __future__ import annotations


class UserError(RuntimeError):
    """owner 可处理的失败(消息直接给人看):API 映射为 409。"""


class NotFound(UserError):
    """引用的对象不存在:API 映射为 404。"""
