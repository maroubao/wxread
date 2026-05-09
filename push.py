import json
import logging
import os
import random
import re
import time

import requests

from config import (
    FEISHU_WEBHOOK,
    PUSHPLUS_TOKEN,
    SERVERCHAN_SPT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WXPUSHER_SPT,
)

logger = logging.getLogger(__name__)


class PushNotification:
    def __init__(self):
        self.default_timeout = 10
        self.pushplus_url = "https://www.pushplus.plus/send"
        self.telegram_url = "https://api.telegram.org/bot{}/sendMessage"
        self.server_chan_url = "https://sctapi.ftqq.com/{}.send"
        self.wxpusher_simple_url = "https://wxpusher.zjiecode.com/api/send/message/{}/{}"
        self.feishu_headers = {"Content-Type": "application/json; charset=utf-8"}
        self.headers = {"Content-Type": "application/json"}
        self.proxies = {
            "http": os.getenv("http_proxy"),
            "https": os.getenv("https_proxy"),
        }

    def push_pushplus(self, content, token, is_success):
        attempts = 5
        title = f"微信阅读-{'成功' if is_success else '失败'}"
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.pushplus_url,
                    data=json.dumps({"token": token, "title": title,"content": content,}).encode("utf-8"),headers=self.headers,timeout=10,)
                response.raise_for_status()
                logger.info("PushPlus 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("PushPlus 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_telegram(self, content, bot_token, chat_id):
        url = self.telegram_url.format(bot_token)
        payload = {"chat_id": chat_id, "text": content}

        try:
            response = requests.post(url, json=payload, proxies=self.proxies, timeout=30)
            logger.info("Telegram 响应: %s", response.text)
            response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram 代理发送失败: %s", exc)
            try:
                response = requests.post(url, json=payload, timeout=30)
                response.raise_for_status()
                return True
            except Exception as inner_exc:
                logger.error("Telegram 发送失败: %s", inner_exc)
                return False

    def push_wxpusher(self, content, spt):
        attempts = 5
        url = self.wxpusher_simple_url.format(spt, content)

        for attempt in range(attempts):
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                logger.info("WxPusher 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("WxPusher 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_serverChan(self, content, spt, is_success):
        attempts = 5
        url = self.server_chan_url.format(spt)

        title = f"微信阅读-{'成功' if is_success else '失败'}"

        for attempt in range(attempts):
            try:
                response = requests.post(
                    url,
                    data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
                    headers=self.headers,
                    timeout=10,
                )
                response.raise_for_status()
                logger.info("ServerChan 响应: %s", response.text)
                return True
            except requests.exceptions.RequestException as exc:
                logger.error("ServerChan 推送失败: %s", exc)
                if attempt < attempts - 1:
                    sleep_time = random.randint(180, 360)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
        return False

    def push_feishu(self, content, webhook, is_success):
        if not webhook:
            logger.warning("未配置 FEISHU_WEBHOOK，跳过飞书推送。")
            return False

        title = f"微信阅读任务{'成功' if is_success else '失败'}"
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [
                            [{"tag": "text", "text": content}]
                        ],
                    }
                }
            },
        }

        try:
            response = requests.post(
                webhook,
                json=payload,
                headers=self.feishu_headers,
                timeout=self.default_timeout,
            )
            response.raise_for_status()
            logger.info("Feishu 响应: %s", response.text)
            return True
        except requests.exceptions.RequestException as exc:
            logger.error("Feishu 推送失败: %s", exc)
            return False


def normalize_methods(method):
    if method in (None, ""):
        return []

    if isinstance(method, (list, tuple, set)):
        raw_methods = method
    else:
        raw_methods = re.split(r"[,，\s]+", str(method).strip())

    return [item.lower() for item in raw_methods if item]


def push(content, method, is_success = True):
    notifier = PushNotification()

    methods = normalize_methods(method)
    if not methods:
        logger.warning("未配置推送渠道，跳过推送。")
        return False

    push_results = []
    for current_method in methods:
        if current_method == "pushplus":
            push_results.append(notifier.push_pushplus(content, PUSHPLUS_TOKEN, is_success))
        elif current_method == "telegram":
            push_results.append(notifier.push_telegram(content, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID))
        elif current_method == "wxpusher":
            push_results.append(notifier.push_wxpusher(content, WXPUSHER_SPT))
        elif current_method == "serverchan":
            push_results.append(notifier.push_serverChan(content, SERVERCHAN_SPT, is_success))
        elif current_method == "feishu":
            push_results.append(notifier.push_feishu(content, FEISHU_WEBHOOK, is_success))
        else:
            logger.warning(
                "无效的通知渠道 '%s'，已跳过推送。支持：pushplus、telegram、wxpusher、serverchan、feishu",
                current_method,
            )

    return any(push_results)
