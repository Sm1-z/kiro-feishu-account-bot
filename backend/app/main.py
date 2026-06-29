# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""FastAPI 应用入口。

启动时拉起飞书 WS 长连接（接收审批卡片回调，免公网）。
前端构建产物（若存在）由本服务托管 static/。
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import admin_router, auth_router, request_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Kiro 账号管理平台 v2", version="2.0.0-dev")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(request_router.router)
app.include_router(admin_router.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


@app.on_event("startup")
def _startup():
    # 仅在配置了飞书凭证时启动 WS（测试/本地无凭证时跳过）
    if settings.feishu_app_id and settings.feishu_app_secret:
        try:
            from app.feishu_ws import start_ws_client
            start_ws_client()
        except Exception:
            logger.exception("启动飞书 WS 失败（不影响 HTTP 服务）")
    else:
        logger.info("未配置飞书凭证，跳过 WS 长连接启动")


# 托管前端构建产物（可选）。
# 前端是 BrowserRouter（history 模式），/auth/callback、/admin 等是客户端路由，
# 磁盘上无对应文件。StaticFiles 默认会对这些路径返回 404，故需 SPA fallback：
# 真实静态资源走 StaticFiles，其余非 /api 路径一律回退 index.html 交前端路由处理。
_static = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(_static):
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    # 静态资源（js/css/图片等）挂在 /assets
    _assets = os.path.join(_static, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    _index = os.path.join(_static, "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # /api/* 已由上面的路由处理；走到这里的非 api 路径一律回退 SPA 入口
        candidate = os.path.join(_static, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(_index)