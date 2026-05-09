# main.py 主逻辑：包括字段拼接、模拟请求
import hashlib
import json
import logging
import random
import time
import urllib.parse

import requests

from config import READ_NUM, PUSH_METHOD, book, chapter, cookies, data, headers
from log_utils import setup_logging
from push import push


# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
REQUEST_TIMEOUT = 10
COOKIE_DATA_VARIANTS = [
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
    {"rq": "%2Fweb%2Fbook%2Fread"},
]


class WxReadError(RuntimeError):
    """基础异常类型，便于统一分类通知。"""

    def __init__(self, message, reason_code="unknown"):
        super().__init__(message)
        self.reason_code = reason_code


class CookieRefreshError(WxReadError):
    def __init__(self, message):
        super().__init__(message, reason_code="cookie_refresh_failed")


class ReadResponseError(WxReadError):
    def __init__(self, message):
        super().__init__(message, reason_code="read_response_invalid")


class ReadRequestError(WxReadError):
    def __init__(self, message):
        super().__init__(message, reason_code="read_request_failed")


class ReadPayloadError(WxReadError):
    def __init__(self, message):
        super().__init__(message, reason_code="payload_invalid")


def encode_data(payload):
    """数据编码"""
    return "&".join(
        f"{k}={urllib.parse.quote(str(payload[k]), safe='')}"
        for k in sorted(payload.keys())
    )


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7FFFFFFF & (
            _7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30
        )
        _cc1055 = 0x7FFFFFFF & (
            _cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30
        )
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()


def post_json(url, payload, timeout=REQUEST_TIMEOUT):
    response = requests.post(
        url,
        headers=headers,
        cookies=cookies,
        data=json.dumps(payload, separators=(",", ":")),
        timeout=timeout,
    )
    response.raise_for_status()
    return response


def get_wr_skey():
    """刷新 cookie 密钥"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = post_json(RENEW_URL, cookie_data)
            if "wr_skey" in response.cookies:
                return response.cookies["wr_skey"][:8]
        except requests.RequestException as exc:
            logging.warning("refresh_cookie 请求失败，payload=%s，原因：%s", cookie_data, exc)

    return None


def fix_no_synckey():
    try:
        post_json(FIX_SYNCKEY_URL, {"bookIds": ["3300060341"]})
    except requests.RequestException as exc:
        logging.warning("修复 synckey 失败：%s", exc)


def refresh_cookie():
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies["wr_skey"] = new_skey
        logging.info("密钥刷新成功，新密钥：%s***", new_skey[:2])
        logging.info("继续本次阅读。")
        return

    error_message = "无法获取新密钥，可能是 cookie 已失效，或 WXREAD_CURL_BASH 配置有误。"
    raise CookieRefreshError(error_message)


def build_read_payload(last_time):
    current_payload = data.copy()
    current_payload.pop("s", None)
    current_payload["b"] = random.choice(book)
    current_payload["c"] = random.choice(chapter)

    current_time = int(time.time())
    current_payload["ct"] = current_time
    current_payload["rt"] = current_time - last_time
    current_payload["ts"] = current_time * 1000 + random.randint(0, 1000)
    current_payload["rn"] = random.randint(0, 1000)
    current_payload["sg"] = hashlib.sha256(
        f"{current_payload['ts']}{current_payload['rn']}{KEY}".encode()
    ).hexdigest()
    current_payload["s"] = cal_hash(encode_data(current_payload))
    return current_payload, current_time


def read_once(read_payload):
    response = post_json(READ_URL, read_payload, timeout=REQUEST_TIMEOUT)
    try:
        return response.json()
    except ValueError as exc:
        raise ReadResponseError(f"阅读接口返回了非 JSON 响应：{response.text[:200]}") from exc


def build_failure_message(exc):
    if isinstance(exc, CookieRefreshError):
        return (
            "微信读书自动阅读失败。\n"
            "类型：Cookie 或登录态失效\n"
            f"原因：{exc}\n"
            "建议：重新抓取最新的 WXREAD_CURL_BASH。"
        )

    if isinstance(exc, ReadPayloadError):
        return (
            "微信读书自动阅读失败。\n"
            "类型：read 请求体参数失效\n"
            f"原因：{exc}\n"
            "建议：重新抓取最新的 WXREAD_CURL_BASH，让 appId/ps/pc/ci/co/sm/pr 等字段同步更新。"
        )

    if isinstance(exc, ReadResponseError):
        return (
            "微信读书自动阅读失败。\n"
            "类型：接口响应异常\n"
            f"原因：{exc}\n"
            "建议：稍后重试；如果持续失败，再重新抓包。"
        )

    if isinstance(exc, ReadRequestError):
        return (
            "微信读书自动阅读失败。\n"
            "类型：网络请求失败\n"
            f"原因：{exc}\n"
            "建议：检查 GitHub Actions 或服务器网络连通性。"
        )

    return f"微信读书自动阅读失败。\n类型：未分类异常\n原因：{exc}"


def notify_result(content, is_success):
    if PUSH_METHOD not in (None, ""):
        logging.info("开始推送...")
        push(content, PUSH_METHOD, is_success=is_success)
    else:
        logging.info("未配置推送渠道，跳过推送。")


def run():
    refresh_print = setup_logging()
    refresh_cookie()

    index = 1
    last_time = int(time.time()) - 30
    logging.info("一共需要阅读 %s 次。", READ_NUM)

    while index <= READ_NUM:
        read_payload, current_time = build_read_payload(last_time)
        refresh_print(f"阅读进度: 第 {index}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        logging.debug("data: %s", read_payload)

        try:
            res_data = read_once(read_payload)
        except requests.RequestException as exc:
            logging.warning("阅读请求失败，尝试刷新 cookie：%s", exc)
            try:
                refresh_cookie()
            except CookieRefreshError:
                raise
            continue

        logging.debug("response: %s", res_data)

        if "succ" in res_data:
            if "synckey" in res_data:
                last_time = current_time
                index += 1
                time.sleep(30)
                refresh_print(
                    f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，已完成 {(index - 1) * 0.5:.1f} 分钟"
                )
            else:
                logging.warning("无 synckey，尝试修复...")
                fix_no_synckey()
        else:
            response_text = json.dumps(res_data, ensure_ascii=False)[:300]
            if "invalid" in response_text.lower() or "params" in response_text.lower():
                raise ReadPayloadError(f"read 接口参数疑似失效，响应：{response_text}")

            logging.warning("cookie 已过期或响应异常，尝试刷新...")
            try:
                refresh_cookie()
            except CookieRefreshError as cookie_exc:
                raise cookie_exc from None

    logging.info("阅读脚本已完成。")
    return index - 1


if __name__ == "__main__":
    try:
        completed_count = run()
        notify_result(
            f"微信读书自动阅读完成。\n阅读时长：{completed_count * 0.5:.1f} 分钟。",
            is_success=True,
        )
    except Exception as exc:
        logging.exception("阅读脚本执行失败：%s", exc)
        notify_result(build_failure_message(exc), is_success=False)
        raise
