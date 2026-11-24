"""业务路由汇总：资源路由 + 管道路由 + 事件补录路由。"""
from __future__ import annotations

from app.routers import resource
from app.routers import pipeline
from app.routers import backfill

ROUTERS = [resource]
PIPELINE_ROUTER = pipeline.router
BACKFILL_ROUTER = backfill.router