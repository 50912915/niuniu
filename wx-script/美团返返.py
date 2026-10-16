#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 美团返返
# cron: 10 9,15 * * *
# desc: 美团返返外卖返现动态 (YYB-Go 动态适配版)

# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：YYB Go Enhanced 动态获取 wx.login code → 换取美团返返 token
#       → 缓存到本地 JSON（按账号 ID/OpenID 隔离）；
#       下次运行先验证缓存 token，有效直接复用；失效则重新取码自动刷新。
#
# 环境变量（YYB Go Enhanced 标准）：
#   YYB_SERVER    必填，每行一个：http://服务地址@账号ID或OpenID
#   YYB_API_KEY   服务端开启 YYB_PROTOCOL_TOKEN 时，填同值
#   （兼容旧变量 YYB_GO / CODE_SERVER / MTFF_SERVERS，以及旧版本地 /login 服务）
#
# 自检：python3 美团返返.py --check   （只探测 /health，不消费 code）
# ==========================================================

import json
import os
import sys
import random
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 增加顶部随机延时，降低并发风控特征 (对齐其他脚本架构)
# 仅连通性自检 --check 时跳过，避免自检也等待
if "--check" not in sys.argv:
    sleep_time = random.randint(10, 600)
    print(f"随机等待 {sleep_time} 秒，防黑号风控...")
    time.sleep(sleep_time)

APP_NAME = "美团返返外卖返现小程序"
APPID = os.getenv("MTFF_APPID", "wxfdba1f3193621ebf")
MT_APP_NAME = os.getenv("MTFF_APP_NAME", "foodtaster")

# ---- 多账号配置 ----
# 官方标准（YYB Go Enhanced）：环境变量 YYB_SERVER，每行一个账号
#     http://yyb-go:8000@1
#     http://yyb-go:8000@账号OpenID
# 向后兼容旧变量 YYB_GO / CODE_SERVER / MTFF_SERVERS（逗号或换行分隔）。
# 开了协议鉴权时，YYB_API_KEY 填服务端 YYB_PROTOCOL_TOKEN 的同值。
YYB_API_KEY = os.getenv("YYB_API_KEY", "").strip()
YYB_REQUEST_TIMEOUT = float(os.getenv("YYB_REQUEST_TIMEOUT", "20"))


def _normalize_server_entry(raw: str) -> str:
    """规范化单行账号配置；非法行返回空串。"""
    value = (raw or "").strip()
    if not value or value == "[object Object]":
        return ""
    if "@" in value:
        endpoint, ref = (p.strip() for p in value.rsplit("@", 1))
        if not endpoint or not ref:
            return ""
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "http://" + endpoint
        return endpoint.rstrip("/") + "@" + ref
    return value  # 旧版本地 code 服务（无 @），原样保留


SERVERS = []
_env_servers = (
    os.getenv("YYB_SERVER")
    or os.getenv("YYB_GO")
    or os.getenv("CODE_SERVER")
    or os.getenv("MTFF_SERVERS")
    or ""
)
for _line in _env_servers.replace(",", "\n").splitlines():
    _entry = _normalize_server_entry(_line)
    if _entry:
        SERVERS.append(_entry)
if not SERVERS:
    # 旧版本地 code 服务兜底；YYB 接入请改用 YYB_SERVER=地址@账号ID
    SERVERS = ["127.0.0.1:8088"]

PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_REFRESH_TIMES = int(os.getenv("PROXY_REFRESH", "3"))
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

OPEN_BASE_URL = "https://open.meituan.com"
WECHAT_LOGIN_URL = f"{OPEN_BASE_URL}/user/v1/weapplogin"
USER_INFO_URL = f"{OPEN_BASE_URL}/user/v1/info"

ACT_BASE_URL = "https://offsiteact.meituan.com"
ACT_QUERY = (
    "yodaReady=wx&csecappid=wxfdba1f3193621ebf&csecplatform=3"
    "&csecversionname=0.2.233&csecversion=1.4.0"
)

CHECK_IN_URL = f"{ACT_BASE_URL}/wmpact/mission/check/in?{ACT_QUERY}"
ACTIVITY_LIST_URL = f"{ACT_BASE_URL}/wmpact/xcx/activity/list?{ACT_QUERY}"
DYNAMIC_RULE_URL = f"{ACT_BASE_URL}/wmpact/dynamic/rule?{ACT_QUERY}"
WALLET_BALANCE_URL = f"{ACT_BASE_URL}/wmpact/wallet/balance?{ACT_QUERY}"

# 抓包固定参数
CAPTURED_UUID = "1a036668070c8-f6fa2cff930c-0-0-1a036668070c8"
CAPTURED_DEVICEID = "3ec6febf-fff0-4b9c-a962-b1b590e46a7c"
CAPTURED_WXP = "1a112a20b52140e0a93f77b593ab275d"
CAPTURED_PAGE_PVID = "b5ee1cea-6c99-4174-aec3-accff077e5a4"
CAPTURED_MISSION_SECRET_ID = "100000020241016"
CAPTURED_LAT = 22.78120994567871
CAPTURED_LON = 108.27330780029297
CAPTURED_APPLETS_FINGERPRINT = (
    "WX__ver1.2.0_CCCC_6OcnVcWU1tfBNaRIa/DMjk8Ats0smFN9DAE7ivnjYv2f50CD282EGOdtYGVg29NY7VQew1bqa9JrbXJZyMu0S6KIUA61B6ONoPWHNNyXERhqwVCBJBMIJxyesViplAUatV47lmxTYP/P2CFYTjOyBSjjAMP5/T1fCftMf/d3FI8WCTLlY6aB1ubunDsh0o92W+gpbOS0fanCxRobcHqXLmrGrxjZb/SPkM7KK+YeS8EL9f0vW82nOIuOnk4jRTJWdNctyrQK76G1TC9mP6i98sKTVdokpy3Vlr/JVvCqzukDwG+2aNpMqcEGBa2cEYsVEQHm4/hlhIcmpGEuGiwqZMqBGpI8qZb+s75RZtcvj9gugFZetYTo7NxlUIUoZcfeVog0mS05QmLb9XinwqjUqoCchNn8APlMAtzsFc3r4HxJ5RSJ0Y2/sbdHHqZ5CpPZ+Hd/cMU2jDltyWm/VQ96ms8J93Tk7SLpe1hU+hqZ2JN++hCc2K8s7UkCAELLgAAniCG1SWWt10UTUF9TLkshqvbJB0Nuw0AmEAUpxBq9mAqbqgqQAEKpIFAt1/rQR0ez4VBH1fj1EbzbuilCvASGUHJeTjRp4mCO/EjlLfdKbT2dMfBytx8Kb7cNz520pJcC2LSGTKvW6yvPto0NAfGznmeZcbB+XQLhqlignpkojgPglGIYzBWfm41ZCIAi3WpgvwOsNcubHGmX56YBZz7wirC/B6dlAGdPYWQm7pVeowROnGqo0ZamCfejpZJrcL4VuoQV8Lorrqc7RPVhAFV3VrZyy4UR8MSk3daMP5xDmnalRVv/AkARySKRSP0bTARZsi3Oy2tcmlh0PocMbNJQPPrkLn6hvfpzV1gje+Sq3cy15SIxBLK+Due1r2TMwiTIQacoEXLzeyL1nbuqLf6WHUD8/2vAlTLlZv7Nt1AQRbR0llXqUZS3A6FlHKwBH2D3zGLYTmnSLGjSgt4V0NVnLqwOBnN/V+zTnPd00y5KGRQXwg3qXSe7QNR5maoCeQw7izhNHCrokOt4Y1Jy4NiJPkYOenGwhvKgl/h2WTJd8tSU6/cG60YPfrl/jOTdU6s2T2jSKA+4gTxRKvPCpJVKbb7wjnJqCOL4EqdgA4CfquIsAYGb3ZYFwfzyUPitDDjeI6GA0WfePcmqmG69Q1aRHkaej8Gy1J3VO8pX/frOeMKFHgDkdnbRLuJW7b8gopolzJQouHKZTS0kYKXECizj7VmnSvDNC+Mta/cmWhDcPClv/DueaTyEggumMXifqMN9KijHDhMrvyISGPJwbz3naorlncl40pMKMnInlpoK1xGq4FGuMyUCuiiRn9HAQvewCFzUqJ58ZWgPsSXEBIgJdDPbuQLwpQylks37vV01CDuvOzfFXx2qIWYN/lbXVauZWSUQ+SUlTcTfNr6Ub4fbV8USxfCa9uesBDvtxLc8Xc3wUQ1mMo0fjJNr8TlUClCx1eyWla0edoHZQenXPazaZ+sKQYdfAwnviSmsiakBaIw5TWJprdIrD1Eezx/E6BoDRHdX9uuqr/7KdWBA12BIEPiFoHuuzNZD1yVZ7tF1LnH0Ycm75hOp6f9muZC0lDhjTFICAsPFUuN4OHqLLotCb6J2UPkysx7/hbXE7DoEBvsHU1i2WpWOG3+jxOAaWvoka/vbrJpGCPw5yfInRG4DIYhGj3feVJFg40pW7VsdZkun0eVCLjmS7mWIFnGdzUiBYhvKhW5a0DaP86Rmjy2l5+VDo+U5kF6OURpV3MV8rSErXMvSvW3KfRaKcoV0YhdRxQJgw3U5m9mbVg91Ukj8GHOdeh11tuUlg5ip6yc8QY6RNtDvZGiknuJ6SDMKFLMLi5/rfS7EM8KErFd424t02Lkw8gyMXmMDt4gE4RoAbXhTHJPT+2S57Pu0PctR1qi4azVB2nlNOdXpZPbRrxp0vVIVhJ/xlJDup19muHJYYXw="
)

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtffcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541c37) XWEB/25364"
)

def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def sleep(seconds: float) -> None:
    time.sleep(seconds)

def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"

def json_preview(data: Any, limit: int = 800) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]

def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0

def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    return resp.get("data") or {}

def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ ♻️ 美团返返外卖返现动态 code 版 (YYB-Go)         ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(SERVERS):<34}║")
    print("╚" + "═" * 50 + "╝")

def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 🌍 来源 {server:<40}│")
    print("└" + "─" * 50 + "┘")

def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session

def parse_proxy_response(text: Any) -> Dict[str, Any] | None:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        proxy_obj = None
        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            proxy_obj = data["data"]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]

        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass
    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else "",
            }
    return None

def build_proxy_dict(proxy_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    if not proxy_info:
        return None
    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")
    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"
    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"
    print(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")
    return {
        "http": proxy_url,
        "https": proxy_url,
    }

def validate_proxy(proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    if not proxies:
        return False, ""
    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            print(f"✅ [代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        print(f"⚠️ [代理] 验证失败: {exc}")
    return False, ""

def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    if not PROXY_API:
        print(f"⚠️ [代理] {account_name} 未配置 PROXY_API，使用直连")
        return None, ""
    print(f"🌐 [代理] {account_name} 正在获取品赞代理...")
    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)
            if not proxy_info:
                print(f"⚠️ [代理] 第 {index} 次代理解析失败")
                continue
            print(f"✅ [代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)
            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip
            print(f"⚠️ [代理] 第 {index} 次代理不可用")
        except Exception as exc:
            print(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")
        if index < PROXY_RETRY_TIMES:
            sleep(2)
    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""

def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Dict[str, str] | None = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    if proxies:
        # ① 先用当前代理
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as first:
            print(f"⚠️ [代理] {server} 请求失败: {first}，开始重拉代理重试（最多 {PROXY_REFRESH_TIMES} 次）")
            saved = first
        # ② 重新拉取新代理（提取+验活）并重试；就地更新 proxies，后续请求自动跟随
        for i in range(1, PROXY_REFRESH_TIMES + 1):
            new_proxies, ip = get_valid_proxy(f"{server}#重拉{i}")
            if not new_proxies:
                print(f"⚠️ [代理] 第 {i}/{PROXY_REFRESH_TIMES} 次未拉到可用代理")
            else:
                proxies.clear()
                proxies.update(new_proxies)
                print(f"🔁 [代理] 第 {i}/{PROXY_REFRESH_TIMES} 次已换新代理（出口 {ip}），重试请求")
                try:
                    return requests.request(method, url, proxies=proxies, **kwargs)
                except Exception as exc:
                    saved = exc
                    print(f"⚠️ [代理] 第 {i} 个新代理请求仍失败: {exc}")
            if i < PROXY_REFRESH_TIMES:
                sleep(2)
        # ③ 仍失败：关闭兜底则抛错；否则清空代理（本账号本次后续请求直连）并兜底直连
        if not ENABLE_DIRECT_FALLBACK:
            raise saved
        print("🔁 [兜底] 重拉代理仍失败，切换直连重试")
        proxies.clear()
    session = direct_session()
    return session.request(method, url, **kwargs)


def notify(title, content):
    """调用青龙 notify.py 推送（渠道在 config.sh 配置）；非青龙环境静默跳过，不影响主任务"""
    try:
        ql_dir = os.environ.get("QL_DIR", "/ql")
        for p in (os.path.join(ql_dir, "scripts"), "/ql/scripts", "/ql/data/scripts"):
            if os.path.isdir(p) and p not in sys.path:
                sys.path.append(p)
        from notify import send  # type: ignore
        send(title, content)
    except Exception:
        pass


def _yyb_split(server_entry: str) -> Tuple[str, str]:
    """把 'http(s)://host:port@ref' 拆成 (endpoint, ref)；无 @ 时 ref 为空。"""
    value = str(server_entry or "").strip()
    if "@" not in value:
        return value, ""
    endpoint, ref = (p.strip() for p in value.rsplit("@", 1))
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    return endpoint.rstrip("/"), ref


def _yyb_find_code(value: Any) -> str | None:
    """递归查找真实 wx.login code。

    YYB Go Enhanced 成功返回：
        {"openid":..., "account":{...}, "result":{"code":"0a1...", "errMsg":"login:ok"}}
    真实 code 在顶层 result.code；这里递归查找以兼容不同包裹层级，
    并跳过 HTTP 状态码型 code 与 invalid/null 占位。
    """
    if isinstance(value, dict):
        for key in ("wx_code", "login_code"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate and candidate not in {"invalid", "null"}:
                return candidate
        for child in value.values():
            found = _yyb_find_code(child)
            if found:
                return found
        candidate = value.get("code")
        if isinstance(candidate, str) and candidate and candidate not in {"invalid", "null"}:
            return candidate
    elif isinstance(value, list):
        for child in value:
            found = _yyb_find_code(child)
            if found:
                return found
    return None


# YYB 协议接口状态码排查提示
_YYB_STATUS_HINT = {
    400: "缺少 ref/app_id 或上游调用失败",
    401: "协议令牌缺失或错误：核对服务端 YYB_PROTOCOL_TOKEN 与脚本 YYB_API_KEY",
    404: "接口路径错误，应为 /wxapp/getCode",
    405: "取码接口必须用 POST",
    409: "账号未扫码/已过期/会话冲突，需重新扫码或完成小程序授权",
    502: "上游协议、网络、代理或微信服务调用失败",
}


def get_code(server_entry: str) -> str | None:
    """从 YYB Go Enhanced 获取 wx.login code；兼容旧版本地 /login code 服务。"""
    server_entry = str(server_entry or "").strip()
    if not server_entry:
        return None

    endpoint, ref = _yyb_split(server_entry)

    if ref:
        # YYB Go Enhanced：POST /wxapp/getCode
        url = f"{endpoint}/wxapp/getCode"
        print(f"🔐 [授权] 请求 YYB 动态取码 (标识: {mask(ref)}): {url}")
        headers = {"Content-Type": "application/json"}
        if YYB_API_KEY:
            headers["Authorization"] = "Bearer " + YYB_API_KEY
        try:
            response = direct_session().post(
                url,
                json={"ref": ref, "app_id": APPID},
                headers=headers,
                timeout=YYB_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            print(f"❌ [授权] 无法连接 YYB 服务 {endpoint}: {exc}")
            return None

        try:
            data = response.json()
        except ValueError:
            print(f"❌ [授权] 返回非 JSON（HTTP {response.status_code}），可能被反代重定向到登录页")
            return None

        if response.status_code >= 400:
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("message") or data.get("msg") or data)
            hint = _YYB_STATUS_HINT.get(response.status_code, "")
            print(f"❌ [授权] HTTP {response.status_code}：{msg} {hint}".strip())
            return None

        code = _yyb_find_code(data)
        if not code:
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        print("✅ [授权] code 获取成功")
        return code

    # 旧版本地 code 服务：GET /login?appId=
    clean_server = endpoint.removeprefix("http://").removeprefix("https://").rstrip("/")
    url = f"http://{clean_server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")
    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
            timeout=YYB_REQUEST_TIMEOUT,
        )
        data = response.json()
        if data.get("err") != 0 or not data.get("code"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None
        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(token: str | None = None, acct: Dict[str, Any] | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/111/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if token:
        headers["token"] = token
        headers["mt-c-token"] = token
    if acct:
        if acct.get("openId"):
            headers["openId"] = acct["openId"]
        if acct.get("openIdCipher"):
            headers["openIdCipher"] = acct["openIdCipher"]
        if acct.get("userId"):
            headers["csecuserid"] = str(acct["userId"])
    headers["csecuuid"] = CAPTURED_UUID
    headers["deviceid"] = CAPTURED_DEVICEID
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("token"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("jwt"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("jwt"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def login_by_code(
    server: str, code: str, proxies: Dict[str, str] | None
) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 token")
        response = request_with_proxy(
            "POST",
            WECHAT_LOGIN_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
            },
            data={
                "code": code,
                "appName": MT_APP_NAME,
            },
            proxies=proxies,
            server=server,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        token = extract_token(data)
        if token:
            inner = data.get("data") or {}
            acct = {
                "token": token,
                "openId": data.get("openId") or "",
                "openIdCipher": data.get("openIdCipher") or "",
                "userId": inner.get("userId") or "",
            }
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, acct

        print(f"❌ [登录] 未识别 token 字段: {json_preview(data)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None, acct: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token, acct),
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any], acct: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token, acct),
        json=payload,
        proxies=proxies,
        server=server,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


# ====================== Token缓存管理 ======================
def load_token_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        print(f"⚠️ [缓存] 读取失败: {exc}")
    return {}


def save_token_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print("✅ [缓存] Token保存成功")
    except Exception as exc:
        print(f"❌ [缓存] 保存失败: {exc}")


def account_cache_key(server: str) -> str:
    """缓存按微信账号隔离：YYB 账号优先用 ref(ID/OpenID)，旧版本地服务用地址。"""
    _, ref = _yyb_split(server)
    return ref or str(server).strip()


def get_cached_account(server: str) -> Dict[str, Any] | None:
    cache = load_token_cache()
    data = cache.get(account_cache_key(server))
    if data and data.get("token"):
        return data
    return None


def set_cached_account(server: str, acct: Dict[str, Any]) -> None:
    cache = load_token_cache()
    cache[account_cache_key(server)] = {
        "token": acct["token"],
        "openId": acct.get("openId", ""),
        "openIdCipher": acct.get("openIdCipher", ""),
        "userId": acct.get("userId", ""),
        "updateTime": datetime.now().isoformat(),
    }
    save_token_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """优先使用缓存 token（用户信息接口验证），失效自动 code 刷新"""
    cached = get_cached_account(server)
    if cached:
        print("🔍 [缓存] 验证 token")
        try:
            user_resp = api_get(server, USER_INFO_URL, cached["token"], proxies, cached)
            if isinstance(user_resp, dict) and user_resp.get("user"):
                print("✅ [缓存] token 有效")
                return cached["token"], cached
        except Exception as exc:
            print(f"⚠️ [缓存] 验证异常: {exc}")
        print("⚠️ [缓存] token 已失效，重新登录")

    code = get_code(server)
    if not code:
        return None, None

    token, acct = login_by_code(server, code, proxies)
    if not token or not acct:
        return None, acct if acct else None

    set_cached_account(server, acct)
    return token, acct


# ====================== 业务接口 ======================
def build_risk_params(acct: Dict[str, Any], with_fingerprint: bool = False) -> Dict[str, Any]:
    risk = {
        "version": "0.2.233",
        "openId": acct.get("openId", ""),
        "expoId": acct.get("openIdCipher", ""),
    }
    if with_fingerprint:
        risk["appletsFingerprint"] = CAPTURED_APPLETS_FINGERPRINT
    return risk


def check_in(server: str, token: str, proxies: Dict[str, str] | None, acct: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "day": 1,
        "missionSecretId": CAPTURED_MISSION_SECRET_ID,
        "xcxVersion": "0.2.233",
        "channelType": "SELF",
        "appContainer": "WEI_XIN_APP",
        "os": "UNKNOW",
        "lat": CAPTURED_LAT,
        "lon": CAPTURED_LON,
        "pagePvId": CAPTURED_PAGE_PVID,
        "wxp": CAPTURED_WXP,
        "riskParams": build_risk_params(acct, with_fingerprint=True),
    }
    return api_post(server, CHECK_IN_URL, token, proxies, payload, acct)


def activity_list(server: str, token: str, proxies: Dict[str, str] | None, acct: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "queryModel": ["INVITE", "FREE_MEAL_HISTORY"],
        "xcxVersion": "0.2.233",
        "channelType": "SELF",
        "appContainer": "WEI_XIN_APP",
        "os": "UNKNOW",
        "lat": 0,
        "lon": 0,
        "pagePvId": CAPTURED_PAGE_PVID,
        "wxp": CAPTURED_WXP,
        "riskParams": build_risk_params(acct),
    }
    return api_post(server, ACTIVITY_LIST_URL, token, proxies, payload, acct)


def wallet_balance(server: str, token: str, proxies: Dict[str, str] | None, acct: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "xcxVersion": "0.2.233",
        "channelType": "SELF",
        "appContainer": "WEI_XIN_APP",
        "os": "UNKNOW",
        "lat": CAPTURED_LAT,
        "lon": CAPTURED_LON,
        "pagePvId": CAPTURED_PAGE_PVID,
        "wxp": CAPTURED_WXP,
        "riskParams": build_risk_params(acct, with_fingerprint=True),
    }
    return api_post(server, WALLET_BALANCE_URL, token, proxies, payload, acct)


def format_sign_msg(resp: Dict[str, Any]) -> str:
    ret = resp.get("ret")
    if ret == 0:
        data = safe_data(resp)
        award_type = data.get("awardType") or ""
        award_value = data.get("awardValue")
        if award_type and award_value is not None:
            return f"签到成功，奖励 {award_type} {award_value}"
        return "签到成功"
    if ret == 217:
        return "今日已签到"
    return resp.get("msg") or resp.get("message") or "签到失败"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "activityMsg": "-",
        "balance": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    token, acct = login_with_cache(server, proxies)
    if not token or not acct:
        result["error"] = f"登录失败: {json_preview(acct)}"
        return result

    result["token"] = mask(token)

    try:
        sign_resp = check_in(server, token, proxies, acct)
        result["signMsg"] = format_sign_msg(sign_resp)
        print(f"✅ [签到] {result['signMsg']}")

        act_resp = activity_list(server, token, proxies, acct)
        if act_resp.get("ret") == 0:
            act_data = safe_data(act_resp)
            invite = act_data.get("invite") or {}
            details = invite.get("prizeDetails") or []
            if details:
                parts = [
                    f"{d.get('title', '')}: {d.get('awardType', '')} {d.get('awardValue', '')}"
                    for d in details
                ]
                result["activityMsg"] = "；".join(parts)
                print(f"🎁 [活动] {result['activityMsg']}")
            else:
                result["activityMsg"] = "暂无活动详情"
                print(f"🎁 [活动] 暂无活动详情")
        else:
            result["activityMsg"] = act_resp.get("msg") or act_resp.get("message") or "活动查询失败"
            print(f"⚠️ [活动] {result['activityMsg']}")

        balance_resp = wallet_balance(server, token, proxies, acct)
        if balance_resp.get("ret") == 0:
            balance_raw = safe_data(balance_resp).get("balance", 0)
            balance = to_float(balance_raw) / 100
            result["balance"] = f"{balance:.2f} 元"
            print(f"💰 [余额] 当前余额: {balance:.2f} 元")
        else:
            result["balance"] = balance_resp.get("msg") or balance_resp.get("message") or "余额查询失败"
            print(f"⚠️ [余额] {result['balance']}")

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""♻️ 美团返返外卖返现任务结果

━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🌍 来源：{res["server"]}
📝 签到：{res["signMsg"]}
🎁 活动：{res["activityMsg"]}
💰 余额：{res["balance"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━\n"

    return content


def check_servers() -> int:
    """只探测 YYB 服务 /health，不消费 wx.login code、不调用美团业务。"""
    print("🔧 YYB 连通性自检（不消费 code、不执行签到）")
    print(f"🔢 账号数量: {len(SERVERS)}")
    all_ok = True
    probed = 0
    for idx, entry in enumerate(SERVERS, 1):
        endpoint, ref = _yyb_split(entry)
        if not ref:
            print(f"[{idx}/{len(SERVERS)}] {entry}  旧版本地 code 服务，跳过 /health 探测")
            continue
        probed += 1
        headers = {"Authorization": "Bearer " + YYB_API_KEY} if YYB_API_KEY else {}
        try:
            resp = direct_session().get(
                f"{endpoint}/health", headers=headers, timeout=YYB_REQUEST_TIMEOUT
            )
            ok = resp.status_code == 200
            all_ok = all_ok and ok
            print(f"[{idx}/{len(SERVERS)}] {entry}  health={'OK' if ok else 'FAIL HTTP ' + str(resp.status_code)}")
        except Exception as exc:
            all_ok = False
            print(f"[{idx}/{len(SERVERS)}] {entry}  health=FAIL（{exc}）")
    if probed == 0:
        print("自检完成：未配置 YYB 账号（YYB_SERVER=地址@账号ID），仅检测到旧版本地 code 服务。")
        return 0
    print("自检完成。" + ("全部可达。" if all_ok else "存在不可达账号，请按上面提示排查。"))
    return 0 if all_ok else 1


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(check_servers())

    log_title()

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "activityMsg": "-",
                "balance": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 美团返返任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    notify("♻️ 美团返返外卖返现任务完成", build_notify(results))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[!] 脚本异常: {e}")
        traceback.print_exc()
        notify("美团返返 脚本异常", f"脚本异常: {e}")
        sys.exit(1)
#（注：内容由AI生成）
