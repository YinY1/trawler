"""Trawler 全局常量"""

from __future__ import annotations

# 超时（秒）
DOWNLOAD_TIMEOUT = 600  # yt-dlp 下载超时
LLM_API_TIMEOUT = 60  # OpenAI 兼容 API 超时
GOTIFY_TIMEOUT = 10  # Gotify 推送超时
RSS_REQUEST_TIMEOUT = 15  # RSS 请求超时
XHS_REQUEST_TIMEOUT = 15  # 小红书 API 请求超时
XHS_DOWNLOAD_TIMEOUT = 120  # 小红书文件下载超时

WEIBO_REQUEST_TIMEOUT = 15  # 微博 API 请求超时
WEIBO_DOWNLOAD_TIMEOUT = 120  # 微博文件下载超时
WEIBO_POLL_TIMEOUT = 240  # 二维码轮询超时（秒）

# 重试
GOTIFY_MAX_RETRIES = 3  # Gotify 最大重试次数

# 评论
MAX_COMMENT_HIGHLIGHTS = 5  # 最大评论亮点数量

# AI 摘要重试上限（连续失败 N 次后 mark_error 让 cron 永久跳过）
MAX_SUMMARY_RETRIES = 5
