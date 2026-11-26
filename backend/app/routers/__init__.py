"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import encoding_recovery, resource

# 编码恢复的固定路径必须先于通用 /api/{module} 注册，否则会被动态路径抢先匹配。
ROUTERS = [encoding_recovery, resource]
