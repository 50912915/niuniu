#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 麦富迪
# cron: 50 15 * * *
# ==========================================================
# 功能说明：code 换 token（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 换取 token → 缓存到本地 JSON；
#       下次运行先读取缓存 token，并调用用户信息接口验证是否仍有效；
#       有效则直接复用（无需再获取 code）；失效或过期则重新获取 code 自动刷新。
# ==========================================================


"""麦富迪会员小程序动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. /wechat/applet/authorizeV2 使用 code 换 memberId（appId/tenantId/wxAppid/groupId 头）
  3. 每日签到（/member/sign）
  4. 查询会员积分快照（queryByMemberId，HuiBoMemberDTO：balance=剩余积分/totalPoint=累计积分）
  5. 积分明细核对（/member/point/logs，快照增量算不出时兜底）
  6. 青龙 notify 结果推送
  7. 品赞代理，业务请求优先代理，失败直连兜底

积分显示说明：
  流程为「先签到 → 后查积分」，推送展示的是签到到账后的最新积分（与小程序一致）；
  签到前后各查一次快照计算本次增量，增量算不出时查积分明细兜底核对。

环境变量：
  结果通知走青龙面板自带 notify.py，渠道在「配置文件 config.sh」配置
  （Server酱 PUSH_KEY / Bark BARK_PUSH / Telegram TG_BOT_TOKEN / 企业微信 QYWX_KEY 等）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import json
import os
import re
import random
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

import hashlib

APP_NAME = "麦富迪会员小程序"
APPID = "wx278a2ed79c5182f8"

SERVERS = [
    "192.168.31.179:8088",
    "192.168.31.36:8088",
    "192.168.31.88:8088",
    "192.168.31.62:8088",
]

PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

# 签到接口的“已签到”判定：服务端用非 0 code + 文案返回，需按文案识别
ALREADY_SIGNED_RE = re.compile(r"已签|签到过|重复|已提交|打卡过|已经打卡|already", re.IGNORECASE)

LOG_NAME = "麦富迪"
LOG_ICON = "🐶"

API_BASE = "https://cdp.myfoodiepet.com"
APP_ID_HEADER = "6259662812989361028"
TENANT_ID = "00ae459e842642f78b9ab0d8e7c027b4"
SIGN_SALT = "XpL9q#dK2zRf$tMn"

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mfdcookie.json")

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; M2012K11AC Build/SKQ1.220303.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/134.0.6998.136 Mobile Safari/537.36 MicroMessenger/8.0.48.2580(0x28003036) MiniProgramEnv/android"
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


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ {LOG_ICON} {LOG_NAME:<44}║")
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
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            print(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print("🔁 [兜底] 切换直连重试")

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


def get_code(server: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
            timeout=20,
        )
        data = response.json()

        code = str(data.get("code") or "")
        status = data.get("status")
        if data.get("err") != 0 or not code or code == "null" or code == "invalid" or (status is not None and status != "ok"):
            print(f"❌ [授权] code 服务返回无效 code (status={status}, codeType={data.get('codeType')})——该账号微信会话可能已失效，请重新扫码登录 code 服务")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


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



def md5_hex(text: str) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def member_signature(member_id: str) -> Dict[str, Any]:
    ts = int(time.time() * 1000)
    return {"memberId": member_id, "timestamp": ts, "signature": md5_hex(f"{member_id}{ts}{SIGN_SALT}")}


class AccountSession:
    """单个 code 服务地址的会话: memberId（userId 头）+ groupId + 双令牌（X-Token / X-Refresh-Token）

    服务端（见 /tnew/myfoodiepet-member/swagger-resources -> v2/api-docs）在「认证模块」声明：
      - authorizeV2 返回 accessToken(30min JWT) + refreshToken(7天) + memberId
      - 业务接口由登录态鉴权（签到接口 memberId 字段注明“服务端从登录态注入，前端可不传”）
      - 缺少令牌时业务接口一律返回 code=401「登录已过期，请重新登录~」
    """

    def __init__(self, server: str):
        self.server = server
        self.member_id = ""
        self.phone = ""
        self.group_id = ""
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0

    def get_headers(self, with_user: bool = True) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": f"https://servicewechat.com/{APPID}/402/page-frame.html",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "appId": APP_ID_HEADER,
            "tenantId": TENANT_ID,
            "wxAppid": APPID,
            "groupId": self.group_id or "",
        }
        if with_user and self.member_id:
            headers["userId"] = self.member_id
        # 双令牌：服务端认证模块要求的登录态凭证
        if self.access_token:
            headers["X-Token"] = self.access_token
        if self.refresh_token:
            headers["X-Refresh-Token"] = self.refresh_token
        # requests 只接受 str/bytes 头；服务端 groupId 等字段可能返回 int（如 263），
        # 统一强转，避免 InvalidHeader: Header part (263) must be of type str or bytes
        return {key: value if isinstance(value, (str, bytes)) else str(value) for key, value in headers.items()}

    def request(self, method: str, api_path: str, params: Dict[str, Any] | None = None,
                data: Dict[str, Any] | None = None, with_user: bool = True,
                proxies: Dict[str, str] | None = None) -> Dict[str, Any]:
        url = f"{API_BASE}{api_path if api_path.startswith('/') else '/' + api_path}"
        kwargs: Dict[str, Any] = {"headers": self.get_headers(with_user), "timeout": REQUEST_TIMEOUT}
        if proxies:
            kwargs["proxies"] = proxies
        if method.upper() == "GET":
            kwargs["params"] = {**(params or {}), "_": int(time.time() * 1000)}
        else:
            kwargs["params"] = {"_": int(time.time() * 1000)}
            kwargs["json"] = data or {}
        try:
            response = requests.request(method, url, **kwargs)
        except Exception:
            if not proxies or not ENABLE_DIRECT_FALLBACK:
                raise
            kwargs.pop("proxies", None)
            response = requests.request(method, url, **kwargs)
        try:
            result = response.json()
        except Exception:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {json_preview(result, 200)}")
        if not result or (str(result.get("code")) != "0" and result.get("code") != 2):
            raise RuntimeError(result.get("msg") or result.get("message") or json_preview(result, 200))
        return result

    def apply_tokens(self, data: Dict[str, Any]) -> None:
        """从登录/刷新响应中提取双令牌（authorizeV2 -> WechatAppletAuthRespDTO）。"""
        self.access_token = str(data.get("accessToken") or "")
        # 兼容 token / refreshToken 两种字段名
        self.refresh_token = str(data.get("refreshToken") or data.get("token") or "")
        expires_in = data.get("expiresIn")
        self.expires_at = time.time() + int(expires_in) if expires_in else 0.0
        if self.access_token:
            print(f"🔑 [令牌] accessToken={mask(self.access_token)} 有效期={expires_in or '未知'}s "
                  f"refreshToken={'已获取' if self.refresh_token else '缺失（过期后需重新取code）'}")

    def set_cached_token(self) -> None:
        cache = load_token_cache()
        cache[self.server] = {"memberId": self.member_id, "groupId": self.group_id,
                              "phone": self.phone, "accessToken": self.access_token,
                              "refreshToken": self.refresh_token, "expiresAt": self.expires_at,
                              "updateTime": datetime.now().isoformat()}
        save_token_cache(cache)

    def load_cached_token(self) -> bool:
        cached = load_token_cache().get(self.server) or {}
        if not cached.get("memberId"):
            return False
        self.member_id = cached["memberId"]
        self.group_id = str(cached.get("groupId") or "")
        self.phone = cached.get("phone", "")
        self.access_token = str(cached.get("accessToken") or "")
        self.refresh_token = str(cached.get("refreshToken") or "")
        self.expires_at = float(cached.get("expiresAt") or 0)
        return bool(self.access_token)

    def refresh_tokens(self, proxies: Dict[str, str] | None) -> None:
        """双令牌无感刷新：accessToken 过期后凭 refreshToken 换新，避免每天重新取 code。"""
        if not self.refresh_token:
            raise RuntimeError("无 refreshToken，无法刷新")
        print("🔄 [令牌] accessToken 已失效，用 refreshToken 无感刷新中...")
        result = self.request("POST", "/tnew/myfoodiepet-member/v1/auth/refresh",
                              with_user=False, proxies=proxies)
        self.apply_tokens(safe_data(result) or {})
        if not self.access_token:
            raise RuntimeError(f"刷新未返回新令牌: {json_preview(result, 200)}")
        self.set_cached_token()
        print("✅ [令牌] 刷新成功")

    def get_identity_info(self, proxies: Dict[str, str] | None) -> None:
        try:
            result = self.request("GET", f"/tnew/myfoodiepet-member/v1/member/identity/info/{APPID}",
                                  with_user=False, proxies=proxies)
            data = safe_data(result) or {}
            self.group_id = str(data.get("groupId") or self.group_id or "")
        except Exception as exc:
            print(f"⚠️ [身份] 获取身份配置失败: {exc}")

    def login(self, proxies: Dict[str, str] | None) -> None:
        self.get_identity_info(proxies)
        code = get_code(self.server)
        if not code:
            raise RuntimeError("code 服务未返回 code")

        print("🔐 [登录] 使用 code 换 memberId + 双令牌")
        result = self.request("GET", "/tnew/myfoodiepet-member/v1/wechat/applet/authorizeV2",
                              params={"code": code}, with_user=False, proxies=proxies)
        data = safe_data(result) or {}
        self.member_id = str(data.get("memberId") or "")
        self.phone = str(data.get("phone") or "")
        self.group_id = str(data.get("groupId") or self.group_id or "")
        self.apply_tokens(data)
        if not self.member_id:
            raise RuntimeError(f"登录响应未返回 memberId: {json_preview(result, 200)}")
        if not self.access_token:
            raise RuntimeError(f"登录响应未返回 accessToken: {json_preview(result, 300)}")
        self.set_cached_token()
        print(f"✅ [登录] 登录成功: memberId={mask(self.member_id)} phone={self.phone or '-'}")

    def ensure_login(self, proxies: Dict[str, str] | None) -> None:
        """优先级：缓存令牌 -> refreshToken 无感刷新 -> 重新取 code 登录"""
        if self.load_cached_token():
            try:
                self.query_member(proxies)
                print("✅ [缓存] 缓存令牌有效，跳过登录")
                return
            except Exception as exc:
                print(f"⚠️ [缓存] 令牌失效({exc})")
                try:
                    self.refresh_tokens(proxies)
                    self.query_member(proxies)
                    print("✅ [缓存] 双令牌刷新成功，跳过登录")
                    return
                except Exception as r_exc:
                    print(f"⚠️ [缓存] 刷新失败({r_exc})，改为重新取 code 登录")
            self.member_id = ""
            self.access_token = ""
            self.refresh_token = ""
        else:
            print("⚠️ [缓存] 无可用令牌缓存（首次运行或旧格式缓存），走完整登录")
        self.login(proxies)

    def query_member(self, proxies: Dict[str, str] | None) -> Dict[str, Any]:
        result = self.request("POST", "/tnew/myfoodiepet-member/v1/member/queryByMemberId",
                              data=member_signature(self.member_id), proxies=proxies)
        return safe_data(result) or {}

    def member_snapshot(self, proxies: Dict[str, str] | None) -> Dict[str, Any]:
        """会员积分快照。HuiBoMemberDTO 字段（swagger 权威定义）：
        balance=剩余积分  totalPoint=累计积分  growth=成长值余额
        memberName=等级名称  expireInfo=过期积分  realName=姓名
        """
        try:
            data = self.query_member(proxies) or {}
            if not data:
                return {}
            return {
                "balance": data.get("balance"),
                "totalPoint": data.get("totalPoint"),
                "growth": data.get("growth"),
                "memberName": data.get("memberName") or data.get("memberLevel"),
                "expireInfo": data.get("expireInfo"),
                "raw": data,
            }
        except Exception as exc:
            print(f"⚠️ [会员] 积分快照查询失败: {exc}")
            return {}

    def recent_point_logs(self, proxies: Dict[str, str] | None, page_size: int = 5) -> List[Dict[str, Any]]:
        """积分明细（/v1/member/point/logs），用于核对签到实际到账积分数。"""
        result = self.request("POST", "/tnew/myfoodiepet-member/v1/member/point/logs",
                              data={"memberId": self.member_id, "pageIndex": "1",
                                    "pageSize": str(page_size)},
                              proxies=proxies)
        data = safe_data(result) or {}
        records = data.get("records") or data.get("list") or []
        return records if isinstance(records, list) else []

    def latest_sign_points(self, proxies: Dict[str, str] | None) -> int | None:
        """从积分明细里找最近一条“签到”入账记录，返回实得积分；找不到返回 None。"""
        try:
            records = self.recent_point_logs(proxies, 5)
        except Exception as exc:
            print(f"⚠️ [积分明细] 查询失败: {exc}")
            return None
        for rec in records:
            if not isinstance(rec, dict):
                continue
            text = json.dumps(rec, ensure_ascii=False)
            if "签到" not in text:
                continue
            for key in ("point", "points", "changePoint", "changeNum", "value", "num", "integral"):
                value = rec.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    return int(value)
            match = re.search(r'"(?:point|points|changePoint|value)"\s*:\s*(\d+)', text)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def format_member(self, after: Dict[str, Any], before: Dict[str, Any],
                      signed_new: bool, delta: int | None = None,
                      logged: int | None = None) -> str:
        """组装会员信息。
        推送简洁格式（参考样稿）：`铁盆友 积分 429→430（本次签到+1）`
        日志保留完整字段：等级/剩余积分/累计积分/成长值/过期积分。"""
        if not after:
            return "-"

        # ---- 日志：完整信息 ----
        level = after.get("memberName")
        balance = self._to_int(after.get("balance"))
        total = self._to_int(after.get("totalPoint"))
        balance_before = self._to_int(before.get("balance")) if before else None

        log_parts = []
        if level:
            log_parts.append(f"等级={level}")
        if balance is not None:
            log_parts.append(f"剩余积分={balance}")
        if total is not None:
            log_parts.append(f"累计积分={total}")
        growth = after.get("growth")
        if growth is not None:
            log_parts.append(f"成长值={growth}")
        expire_info = after.get("expireInfo")
        if expire_info:
            log_parts.append(f"过期积分={expire_info}")
        detail = " ".join(log_parts) or json_preview(after.get("raw", {}), 160)
        print(f"👤 [会员] {detail}")

        # ---- 推送：等级名 + 积分前后对比 ----
        parts = []
        if level:
            parts.append(str(level))
        if balance is not None:
            if signed_new and balance_before is not None and delta:
                parts.append(f"积分 {balance_before}→{balance}（本次签到+{delta}）")
            elif signed_new and logged:
                parts.append(f"积分 {balance}（本次签到+{logged}）")
            else:
                parts.append(f"积分 {balance}")
        return " ".join(parts) or detail

    def sign(self, proxies: Dict[str, str] | None) -> Tuple[str, bool]:
        """签到。注意：服务端对“今日已签到”也返回非 0 code，
        而 request() 对所有非 0 code 一律抛 RuntimeError，所以必须在这里兜住，
        否则重复执行会被误判为失败。
        返回 (文案, 是否本次新签到成功)。"""
        try:
            result = self.request("POST", "/tnew/myfoodiepet-member/v1/member/sign",
                                  data={"memberId": self.member_id}, proxies=proxies)
            msg = str(result.get("msg") or result.get("message") or "ok")
            # 签到响应的 data 可能直接带本次积分/连续天数，打出来便于核对
            data = safe_data(result)
            if data:
                print(f"🧾 [签到] 响应 data: {json_preview(data, 300)}")
        except RuntimeError as exc:
            msg = str(exc)
            if not ALREADY_SIGNED_RE.search(msg):
                raise

        if ALREADY_SIGNED_RE.search(msg):
            print(f"✅ [签到] 今日已签到（{msg}）")
            return f"今日已签到（{msg}）", False
        print(f"✅ [签到] 签到成功: {msg}")
        return "签到成功", True

    def run_flow(self, proxies: Dict[str, str] | None) -> Dict[str, Any]:
        """先签到后查积分：保证推送显示的是签到到账后的最新积分（与小程序一致）。
        签到前后各查一次快照计算本次增量；增量算不出时用积分明细兜底核对。
        推送格式：`铁盆友 积分 429→430（本次签到+1）`"""
        before = self.member_snapshot(proxies)
        if before.get("balance") is not None:
            print(f"👤 [会员] 签到前: 剩余积分={before.get('balance')} "
                  f"累计积分={before.get('totalPoint')}")
        else:
            print("👤 [会员] 签到前积分查询失败（不影响签到）")

        sign_msg, signed_new = self.sign(proxies)

        # 签到积分入账后立即查询，保证与小程序显示一致；留 1s 缓冲防服务端异步
        sleep(1)
        after = self.member_snapshot(proxies)

        delta: int | None = None
        logged: int | None = None
        if signed_new:
            balance_after = self._to_int(after.get("balance"))
            balance_before = self._to_int(before.get("balance"))
            if balance_after is not None and balance_before is not None \
                    and balance_after - balance_before > 0:
                delta = balance_after - balance_before
            else:
                # 前后快照差算不出增量（查询失败/服务端延迟），用积分明细核对
                logged = self.latest_sign_points(proxies)
                if logged:
                    print(f"🧾 [积分明细] 最近一条签到入账: +{logged}")
                else:
                    print("🧾 [积分明细] 未查到签到入账记录，请以小程序显示为准")
            if after.get("balance") is None:
                # 签到后快照查询失败时，退回签到前的积分展示
                after = before or after
        elif after.get("balance") is None:
            # 已签到且签到后快照也失败时，至少把签到前的积分显示出来
            after = before or after

        member_msg = self.format_member(after, before, signed_new, delta=delta, logged=logged)
        return {"signMsg": sign_msg, "memberMsg": member_msg}


EMPTY_RESULT = {
    "server": "-",
    "success": False,
    "proxyStatus": "未使用代理",
    "proxyIp": "-",
    "token": "-",
    "signMsg": "-",
    "memberMsg": "-",
    "error": "",
}


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = dict(EMPTY_RESULT)
    result["server"] = server

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    session = AccountSession(server)

    try:
        session.ensure_login(proxies)
        result["token"] = mask(session.member_id)
        flow = session.run_flow(proxies)
        result["signMsg"] = flow["signMsg"]
        result["memberMsg"] = flow["memberMsg"]
        result["success"] = "签到成功" in result["signMsg"] or "已签到" in result["signMsg"]
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result
def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🐶 麦富迪{len(results)}账号任务结果

━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        content += f"""
🧩 账号 {idx}
🌍 来源：{res["server"]}
🆔 MemberID：{res["token"]}
📝 签到：{res["signMsg"]}
👤 会员：{res["memberMsg"]}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({**EMPTY_RESULT, "server": server, "error": traceback.format_exc().strip()})

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print(f"║ 🏁 麦富迪任务执行完成          ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    notify(f"{LOG_ICON} {LOG_NAME}{len(results)}账号任务完成", build_notify(results))


# --- YYB compatibility layer (managed) ---
import os as _yyb_os
import json as _yyb_json

def _yyb_accounts():
    result = []
    for line in _yyb_os.getenv("YYB_SERVER", "").splitlines():
        line = line.strip()
        if not line or "@" not in line or line == "[object Object]":
            continue
        endpoint, ref = (part.strip() for part in line.split("@", 1))
        if endpoint and ref:
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            result.append(endpoint.rstrip("/") + "@" + ref)
    return result


def _yyb_parts(server):
    value = str(server).strip()
    if "@" not in value:
        return value.rstrip("/"), ""
    return value.rsplit("@", 1)[0].rstrip("/"), value.rsplit("@", 1)[1]


def _yyb_appid(args, kwargs):
    appid = kwargs.get("appid") or kwargs.get("app_id")
    if not appid and args and isinstance(args[0], str):
        appid = args[0]
    if not appid:
        appid = globals().get("APPID") or globals().get("APP_ID") or ""
    if isinstance(appid, (list, tuple)):
        appid = appid[0] if appid else ""
    return str(appid)


def _yyb_json_request(server, path, appid, payload=None):
    import requests
    endpoint, ref = _yyb_parts(server)
    if not endpoint or not ref or not appid:
        raise RuntimeError("YYB 参数不完整：需要 地址@账号ID 和 app_id")
    headers = {}
    api_key = _yyb_os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = requests.post(
        endpoint + path,
        json={"ref": ref, "app_id": str(appid), **(payload or {})},
        headers=headers,
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("YYB 返回非 JSON") from exc
    if response.status_code >= 400:
        raise RuntimeError(str(body.get("message") or body.get("msg") or body))
    return body


def _yyb_find_code(value):
    if isinstance(value, dict):
        if value.get("code") not in (None, "", "null", "invalid") and isinstance(value.get("code"), str):
            return value["code"]
        for child in value.values():
            found = _yyb_find_code(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _yyb_find_code(child)
            if found:
                return found
    return None


def _yyb_code(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getCode", _yyb_appid(args, kwargs))
    code = _yyb_find_code(body)
    if not code:
        raise RuntimeError(str(body.get("msg") or body.get("message") or "YYB 未返回 wx.login code"))
    return str(code)


def _yyb_phone(server, *args, **kwargs):
    body = _yyb_json_request(server, "/wxapp/getPhoneNumber", _yyb_appid(args, kwargs))
    return body.get("result") or body.get("data") or body


if _yyb_accounts():
    SERVERS = _yyb_accounts()


_yyb_original_get_code = get_code


def get_code(server, *args, **kwargs):
    if "@" in str(server):
        phone_code = kwargs.get("phone_code") is True or (args and args[0] is True)
        if phone_code:
            body = _yyb_phone(server)
            code = _yyb_find_code(body)
            if not code:
                raise RuntimeError("YYB 未返回手机号授权 code")
            return str(code)
        return _yyb_code(server, *args, **kwargs)
    return _yyb_original_get_code(server, *args, **kwargs)


if "get_code_for" in globals():
    _yyb_original_get_code_for = get_code_for

    def get_code_for(server, appid):
        if "@" in str(server):
            return _yyb_code(server, appid)
        return _yyb_original_get_code_for(server, appid)


def get_phone_payload(server, *args, **kwargs):
    if "@" in str(server):
        return _yyb_phone(server, *args, **kwargs)
    raise RuntimeError("当前账号不是 YYB_SERVER 格式，无法获取手机号授权包")


# Adapt the two common source-specific phone helpers when present.
if "get_phone_number_payload" in globals():
    get_phone_number_payload = get_phone_payload
if "get_phone_authorize" in globals():
    get_phone_authorize = get_phone_payload
if "get_phone_data" in globals():
    get_phone_data = get_phone_payload
if "get_phone_package" in globals():
    _yyb_original_get_phone_package = get_phone_package

    def get_phone_package(server, *args, **kwargs):
        if "@" not in str(server):
            return _yyb_original_get_phone_package(server, *args, **kwargs)
        body = _yyb_phone(server, *args, **kwargs)
        # YYB keeps the upstream response shape. Do not invent encryptedData/iv.
        if isinstance(body, dict):
            result = body.get("result") if isinstance(body.get("result"), dict) else body
            data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
            raw = data.get("raw") if isinstance(data, dict) and isinstance(data.get("raw"), dict) else data
            if isinstance(raw, dict):
                return {
                    "authCode": str(raw.get("code") or data.get("code") or ""),
                    "encryptPhoneNumber": str(raw.get("encryptedData") or ""),
                    "initVector": str(raw.get("iv") or ""),
                }
        return {"authCode": "", "encryptPhoneNumber": "", "initVector": ""}
if "get_userinfo_blob" in globals():
    _yyb_original_get_userinfo_blob = get_userinfo_blob

    def get_userinfo_blob(server):
        if "@" not in str(server):
            return _yyb_original_get_userinfo_blob(server)
        body = _yyb_json_request(server, "/wx/getuserinfo", _yyb_appid((), {}))
        info = body.get("user_info")
        # /wx/getuserinfo is a profile endpoint; it is not a source of
        # encryptedData/iv/signature. Never fabricate those fields.
        if not isinstance(info, dict):
            return None
        return {"userInfo": info, "rawData": _yyb_json.dumps(info, ensure_ascii=False), "errMsg": "getUserInfo:ok"}
# --- end YYB compatibility layer ---

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[!] 脚本异常: {e}")
        traceback.print_exc()
        notify(f"{LOG_ICON} {LOG_NAME} 脚本异常", f"脚本异常: {e}")
        sys.exit(1)
