"""缓存陈旧故障步骤回放台 Flask 应用包。"""

from .web import app, create_app

__all__ = ["app", "create_app"]
