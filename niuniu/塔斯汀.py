#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==========================================================
# 功能说明：code 换 token（每次直接取 code，无本地缓存）
# 机制：本地/YYB code 服务获取微信 code → 直接调登录接口换 token；
#       不再读写 tstcookie.json，每次运行都走完整取码→登录流程。
# ==========================================================


"""
塔斯汀中国汉堡动态 code 版

功能：
  1. 四端口本地服务获取微信 code
  2. 塔斯汀登录接口使用 code 换 user-token
  3. 自动发现当月签到活动 ID（banner + 活动探测）
  4. 每日签到（含奖励解析与已签判定）
  5. 查询会员信息与当前积分
  6. 失败自动重试（指数退避）
  7. 青龙 notify 结果推送
  8. 品赞代理，业务请求优先代理，失败直连兜底（阿里云 WAF 拦截海外 IP 时建议配置）

环境变量：
  结果通知走青龙面板自带 notify.py，渠道在「配置文件 config.sh」配置
  （Server酱 PUSH_KEY / Bark BARK_PUSH / Telegram TG_BOT_TOKEN / 企业微信 QYWX_KEY 等）
  TST_PROXY_API         塔斯汀专属代理提取 API，可选（留空回退共用 PROXY_API；都没有则全程直连）
  PROXY_API             共用代理提取 API，可选（TST_PROXY_API 未配置时生效）
  PROXY_TYPE            http / socks5（无前缀，全局共用），默认 http
  PROXY_RETRY           单次「提取+验活」内部重试次数，默认 3
  PROXY_REFRESH         请求失败后重拉新代理次数，仍失败才兜底直连，默认 3
  PROXY_INTERVAL        拿到代理后等待秒数（等代理生效/加白），默认 3
  PROXY_VALIDATE_URL    代理验活地址，默认 http://httpbin.org/ip
  PROXY_FALLBACK        代理全失败是否兜底直连，1/0，默认 1
  PROXY_TIMEOUT         代理提取/验活请求超时秒数，默认 10
  说明：每个账号登录前取专属代理，登录与后续业务请求共用同一出口 IP；
        本地 code 服务 / YYB 取码一律直连；青龙 notify 通知由面板发送。
  TASTIN_SHOP_ID        门店 ID，默认 28416
  TASTIN_VERSION        小程序版本号，默认 3.88.1
  TASTIN_MEMBER_PHONE   可选，抓包获取的加密手机号令牌（登录后自动获取失败时兜底）
  TASTIN_ACTIVITY_ID    可选，签到活动 ID 探测起点（默认 74）

  —— 登录接口（已按小程序 3.88.1 抓包固化为默认值，一般无需配置；仅当接口再变更时覆盖）——
  TASTIN_LOGIN_URL      登录接口，默认 /api/intelligence/member/login（旧 /api/minic/wx/login 已失效）
  TASTIN_LOGIN_METHOD   登录请求方法，默认 POST
  TASTIN_LOGIN_BODY     登录请求体 JSON，jsCode 值用 __CODE__ 占位；
                        默认 {"jsCode":"__CODE__","memberSource":"VX","memberSourceChannel":"WX"}
  TASTIN_TOKEN_PATH     登录响应中 token 的点分路径，如 result.userToken（默认自动识别）
  TASTIN_EXTRA_HEADERS  登录请求额外请求头 JSON（默认无需）

自检：python 本脚本 --diag    只验证「code 服务 → 登录 → token」，不执行签到。

✅ 登录端点与请求体已按真机抓包修正：POST /api/intelligence/member/login，
   请求体字段为 jsCode（不是 code）+ memberSource=VX + memberSourceChannel=WX。
   旧地址 /api/minic/wx/login 会被网关以 401「登录已过期」统一拦截。
   若将来再次 401，说明接口又改版，重新抓包并用上面环境变量覆盖即可。

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]
"""

import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests


APP_NAME = "塔斯汀中国汉堡小程序"
APPID = "wx557473f23153a429"

SERVERS = [
    "127.0.0.1:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

SHOP_ID = os.getenv("TASTIN_SHOP_ID", "28416")
VERSION = os.getenv("TASTIN_VERSION", "3.88.1")
MEMBER_PHONE_ENV = os.getenv("TASTIN_MEMBER_PHONE", "")
ACTIVITY_ID_START = int(os.getenv("TASTIN_ACTIVITY_ID", "74"))

MAX_SIGN_RETRIES = 3
BACKOFF_BASE = 30
BACKOFF_CAP = 120

# ==================== 标准代理模块（配置） ====================

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# 提取 API：TST_PROXY_API 优先，留空回退共用 PROXY_API，都没有则全程直连
PROXY_API = os.getenv("TST_PROXY_API", "") or os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
PROXY_RETRY_TIMES = _env_int("PROXY_RETRY", 3)       # 单次「提取+验活」内部重试次数
PROXY_REFRESH_TIMES = _env_int("PROXY_REFRESH", 3)   # 请求失败后重拉新代理次数，仍失败才兜底
PROXY_VALIDATE_URL = os.getenv("PROXY_VALIDATE_URL", "http://httpbin.org/ip")
PROXY_FETCH_INTERVAL = _env_int("PROXY_INTERVAL", 3)  # 拿到代理后等待秒数
ENABLE_DIRECT_FALLBACK = os.getenv("PROXY_FALLBACK", "1") not in ("0", "false", "False", "no")
PROXY_TIMEOUT = _env_int("PROXY_TIMEOUT", 10)         # 提取/验活请求超时

# 取码（本地 code 服务 / YYB 取码服务）失败重试配置
CODE_RETRY_TIMES = _env_int("CODE_RETRY", 3)          # 取码失败重试次数
CODE_RETRY_INTERVAL = _env_int("CODE_RETRY_INTERVAL", 5)  # 每次取码重试间隔秒数

# 连接级异常 = 代理故障（ProxyError/SSLError 均为 ConnectionError 子类）
_PROXY_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
# 当前账号代理状态：请求失败降级直连后，本账号本次运行后续请求直接直连
_ACCOUNT_PROXY: Dict[str, Any] = {"proxies": None, "exit_ip": "", "degraded": False}

REQUEST_TIMEOUT = 30

BASE_URL = "https://sss-web.tastientech.com"

# ---------- 登录接口配置（抓包核对后用环境变量覆盖，无需改代码） ----------
# TASTIN_LOGIN_URL：登录接口路径。填完整 http(s) 地址，或填相对路径（如 /api/xxx/login）。
#   默认已按小程序 3.88.1 抓包修正为 /api/intelligence/member/login；旧地址 /api/minic/wx/login 会被网关 401 拦截。
_login_over = os.getenv("TASTIN_LOGIN_URL", "").strip()
if _login_over.startswith("http://") or _login_over.startswith("https://"):
    LOGIN_URL = _login_over
elif _login_over:
    LOGIN_URL = f"{BASE_URL}{_login_over if _login_over.startswith('/') else '/' + _login_over}"
else:
    LOGIN_URL = f"{BASE_URL}/api/intelligence/member/login"

# TASTIN_LOGIN_METHOD：登录请求方法，默认 POST。
LOGIN_METHOD = os.getenv("TASTIN_LOGIN_METHOD", "POST").strip().upper() or "POST"

# TASTIN_LOGIN_BODY：登录请求体 JSON 模板，jsCode 值用占位符 __CODE__ 表示。
#   默认已按抓包固化（字段名是 jsCode 不是 code，并固定带 memberSource / memberSourceChannel）。
DEFAULT_LOGIN_BODY_TEMPLATE = '{"jsCode":"__CODE__","memberSource":"VX","memberSourceChannel":"WX"}'
LOGIN_BODY_TEMPLATE = os.getenv("TASTIN_LOGIN_BODY", "").strip() or DEFAULT_LOGIN_BODY_TEMPLATE

# TASTIN_TOKEN_PATH：登录成功响应里 token 的点分路径，覆盖自动识别。
#   例如 data.userToken / result.token / data.accessToken
TOKEN_PATH_OVERRIDE = os.getenv("TASTIN_TOKEN_PATH", "").strip()

# TASTIN_EXTRA_HEADERS：登录请求需要额外携带的请求头 JSON（抓包若发现特殊头/签名）。
#   例如 {"Authorization":"Bearer xxx","x-sign":"abc"}
LOGIN_EXTRA_HEADERS_RAW = os.getenv("TASTIN_EXTRA_HEADERS", "").strip()
BANNER_SIGN_URL = f"{BASE_URL}/api/minic/shop/intelligence/banner/c/list/sign"
SIGN_INFO_URL = f"{BASE_URL}/api/sign/member/signInfoV2"
SIGN_DO_URL = f"{BASE_URL}/api/sign/member/signV2/sign"
MEMBER_DETAIL_URL = f"{BASE_URL}/api/intelligence/member/getMemberDetail"
MY_POINT_URL = f"{BASE_URL}/api/wx/point/myPoint"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF "
    "WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541c1a) XWEB/25297"
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


def short_error(text: Any, limit: int = 300) -> str:
    """把可能是一大段 traceback 的错误压缩成一行可读信息（推送用）。"""
    text = (str(text) or "").strip()
    if not text:
        return "未知错误"
    if "Traceback" in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        tail = lines[-1] if lines else text
        loc = ""
        for line in reversed(lines):
            if line.startswith('File "'):
                loc = line
                break
        text = f"{tail}（{loc}）" if loc else tail
    return text[:limit]


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🍔 塔斯汀中国汉堡动态 code 版                ║")
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
    """直连会话：trust_env=False 不读系统代理，防止套娃（取码服务/推送专用）。"""
    session = requests.Session()
    session.trust_env = False
    return session


def _parse_proxy_lines(text: str) -> List[str]:
    out: List[str] = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})(?::([^:\s]+):([^:\s]+))?$", line):
            out.append(line)
    return out


def parse_proxy_response(text: Any) -> List[str]:
    """解析代理提取 API 返回，兼容多种格式，输出 ['ip:port[:user:pass]', ...]：
      - JSON: {"data": [{"ip": "x", "port": 1, ...}, ...]}（取全部候选）
      - JSON: {"data": {"ip": "x", "port": 1}} / {"result": {...}} / {"ip": "x", "port": 1}
      - 纯文本: 多行 ip:port[:user:pass]
    """
    proxies: List[str] = []
    if isinstance(text, dict):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)
    text = (text or "").strip()
    if not text:
        return proxies

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            host = obj.get("ip") or obj.get("host") or obj.get("server") or obj.get("addr")
            port = obj.get("port")
            if host and port:
                try:
                    port = int(port)
                except (TypeError, ValueError):
                    return
                user = obj.get("user") or obj.get("username") or ""
                pwd = obj.get("pass") or obj.get("password") or obj.get("pwd") or ""
                proxies.append(f"{host}:{port}" + (f":{user}:{pwd}" if user else ""))
                return
            for item in obj.values():
                walk(item)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, str):
            proxies.extend(_parse_proxy_lines(obj))

    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        obj = None
    if obj is not None:
        walk(obj)
    if proxies:
        return proxies
    return _parse_proxy_lines(text)


def build_proxy_dict(proxy_str: str) -> Dict[str, str] | None:
    """构造 requests 代理字典，支持 http / socks5 与账密认证（账密 URL 编码）。"""
    parts = str(proxy_str).strip().split(":")
    if len(parts) < 2:
        return None

    host, port = parts[0], parts[1]
    auth = ""
    if len(parts) >= 4:
        auth = f"{quote(':'.join(parts[2:-1]))}:{quote(parts[-1])}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"
    print(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")
    return {"http": proxy_url, "https": proxy_url}


def validate_proxy(proxies: Dict[str, str] | None) -> str:
    """验活：确认代理可用并返回出口 IP；失败返回空串。走 trust_env=False 会话防套娃。"""
    if not proxies:
        return ""
    try:
        response = direct_session().get(PROXY_VALIDATE_URL, proxies=proxies, timeout=PROXY_TIMEOUT)
        if response.status_code == 200:
            try:
                data = response.json()
                ip = str(data.get("origin") or data.get("ip") or "")
            except Exception:
                ip = ""
            ip = ip.split(",")[0].strip()
            if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip):
                print(f"✅ [代理] 验证通过，出口 IP: {ip}")
                return ip
    except Exception as exc:
        print(f"⚠️ [代理] 验证失败: {exc}")
    return ""


def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    """提取 → 构造 → 验活，内部重试 PROXY_RETRY 次；全败返回 (None, "")。"""
    if not PROXY_API:
        print(f"⚠️ [代理] {account_name} 未配置代理 API，使用直连")
        return None, ""

    print(f"🌐 [代理] {account_name} 正在获取代理...")
    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=PROXY_TIMEOUT)
            for proxy_str in parse_proxy_response(response.text):
                proxies = build_proxy_dict(proxy_str)
                if not proxies:
                    continue
                ip = validate_proxy(proxies)
                if ip:
                    return proxies, ip
            print(f"⚠️ [代理] 第 {index} 次提取/验活无可用代理")
        except Exception as exc:
            print(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")
        if index < PROXY_RETRY_TIMES:
            sleep(2)

    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(method: str, url: str, *, proxies: Dict[str, str] | None = None,
                       server: str = "", **kwargs) -> requests.Response:
    """统一请求入口，三级失败处理（当前代理→重拉 PROXY_REFRESH 次→兜底直连并降级）。"""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    # 本账号若已在失败重试中换新代理，后续请求统一沿用最新出口 IP
    if _ACCOUNT_PROXY["proxies"]:
        proxies = _ACCOUNT_PROXY["proxies"]

    # 未配置代理 / 已降级直连 → 直连
    if not proxies or _ACCOUNT_PROXY["degraded"]:
        return direct_session().request(method, url, **kwargs)

    try:
        return requests.request(method, url, proxies=proxies, **kwargs)
    except requests.exceptions.HTTPError:
        raise
    except _PROXY_ERRORS as exc:
        first_exc = exc

    # ---- 二级：重拉新代理 ----
    print(f"⚠️ [代理] {server} 代理请求失败: {first_exc}，重新拉取代理重试")
    for index in range(1, PROXY_REFRESH_TIMES + 1):
        new_proxies, new_ip = get_valid_proxy(server)
        if new_proxies:
            proxies = new_proxies
            _ACCOUNT_PROXY["proxies"] = new_proxies
            _ACCOUNT_PROXY["exit_ip"] = new_ip
            print(f"🔁 [代理] 已换新代理重试({index}/{PROXY_REFRESH_TIMES})，出口IP: {new_ip}")
            try:
                return requests.request(method, url, proxies=proxies, **kwargs)
            except requests.exceptions.HTTPError:
                raise
            except _PROXY_ERRORS as retry_exc:
                first_exc = retry_exc
                print(f"⚠️ [代理] 新代理仍失败({index}/{PROXY_REFRESH_TIMES}): {retry_exc}")
        else:
            print(f"⚠️ [代理] 拉取新代理失败({index}/{PROXY_REFRESH_TIMES})")

    # ---- 三级：兜底直连 ----
    if ENABLE_DIRECT_FALLBACK:
        print("🔁 [兜底] 重拉代理仍失败，切换直连；本账号本次运行后续请求将直连")
        _ACCOUNT_PROXY["proxies"] = None
        _ACCOUNT_PROXY["exit_ip"] = ""
        _ACCOUNT_PROXY["degraded"] = True
        return direct_session().request(method, url, **kwargs)
    raise first_exc


def sync_proxy_status(result: Dict[str, Any]) -> None:
    """同步本账号最终代理状态到结果字典，并在控制台打印（供推送复用）。"""
    if _ACCOUNT_PROXY["degraded"]:
        result["proxyStatus"] = "代理失效，已降级直连"
        result["proxyIp"] = "-"
    else:
        result["proxyStatus"] = "使用专属代理" if _ACCOUNT_PROXY["proxies"] else "使用直连"
        result["proxyIp"] = _ACCOUNT_PROXY["exit_ip"] or "-"
    print(f"🌐 代理：{result['proxyStatus']}，出口IP：{result['proxyIp']}")



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

        if data.get("err") != 0 or not data.get("code"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(token: str | None = None, with_shop: bool = True) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "channel": "1",
        "version": VERSION,
        "xweb_xhr": "1",
        "Referer": f"https://servicewechat.com/{APPID}/556/page-frame.html",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    # 登录请求抓包不含 gray-shop-id；业务接口保留该头
    if with_shop:
        headers["gray-shop-id"] = SHOP_ID
    if token:
        headers["user-token"] = token
    return headers


def is_waf_response(result: Dict[str, Any]) -> bool:
    """检测阿里云 WAF 拦截（返回 HTML 而非 JSON，或 403/405）"""
    code = result.get("code")
    msg = str(result.get("msg", ""))
    if code in (403, 405):
        return True
    if "<!doctype" in msg.lower() or "<html" in msg.lower():
        return True
    return False


def dig_path(data: Any, path: str) -> Any:
    """按点分路径取值，例如 data.userToken。"""
    cur = data
    for part in path.split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    # 抓包确认的 token 字段路径优先级最高
    if TOKEN_PATH_OVERRIDE:
        forced = dig_path(data, TOKEN_PATH_OVERRIDE)
        if forced and str(forced) != "null":
            return str(forced)

    candidates = [
        data.get("token"),
        data.get("userToken"),
        data.get("accessToken"),
        data.get("access_token"),
        data.get("jwt"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("token"),
            inner.get("userToken"),
            inner.get("accessToken"),
            inner.get("access_token"),
            inner.get("jwt"),
        ])

    result = data.get("result")
    if isinstance(result, dict):
        candidates.extend([
            result.get("token"),
            result.get("userToken"),
            result.get("accessToken"),
            result.get("access_token"),
        ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


GATE_BLOCK_HINT = (
    "登录端点被网关统一拦截（任意不存在的地址也返回同样的401），"
    "说明登录URL/请求体/鉴权头不正确，与code是否过期无关。"
    "请抓包小程序真实登录请求（不带user-token、请求体含code的那条），"
    "用 TASTIN_LOGIN_URL、TASTIN_LOGIN_BODY、TASTIN_EXTRA_HEADERS、TASTIN_TOKEN_PATH "
    "四个环境变量配置后，执行 python 本脚本 --diag 验证。"
)


def build_login_body(code: str) -> Dict[str, Any]:
    """构造登录请求体：优先使用 TASTIN_LOGIN_BODY 模板（__CODE__ 占位），否则默认 {"code": code}。"""
    if not LOGIN_BODY_TEMPLATE:
        return {"code": code}
    try:
        template = json.loads(LOGIN_BODY_TEMPLATE)
    except json.JSONDecodeError as exc:
        raise ValueError(f"TASTIN_LOGIN_BODY 不是合法 JSON: {exc}") from exc

    def replace_placeholder(value: Any) -> Any:
        if isinstance(value, str):
            return code if value == "__CODE__" else value
        if isinstance(value, dict):
            return {k: replace_placeholder(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace_placeholder(v) for v in value]
        return value

    return replace_placeholder(template)


def login_headers() -> Dict[str, str]:
    headers = common_headers(with_shop=False)
    if LOGIN_EXTRA_HEADERS_RAW:
        try:
            headers.update(json.loads(LOGIN_EXTRA_HEADERS_RAW))
            print("🔧 [登录] 已合并 TASTIN_EXTRA_HEADERS 额外请求头")
        except json.JSONDecodeError as exc:
            print(f"⚠️ [登录] TASTIN_EXTRA_HEADERS 不是合法 JSON，已忽略: {exc}")
    return headers


def is_gate_blocked(response: Any, data: Any) -> bool:
    """识别网关最前端的统一登录拦截：HTTP 401 或业务 code=401 且提示登录已过期。"""
    if getattr(response, "status_code", None) == 401:
        return True
    if isinstance(data, dict) and data.get("code") == 401 and "登录已过期" in str(data.get("msg", "")):
        return True
    return False


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """用 code 换 token。登录端点为推断值，失败请抓包并用环境变量覆盖（见文件顶部配置）。"""
    try:
        try:
            body = build_login_body(code)
        except ValueError as exc:
            print(f"❌ [登录] {exc}")
            return None, {"code": -1, "msg": str(exc)}

        print("🔐 [登录] 使用 code 换 token")
        print(f"   ├─ 请求: {LOGIN_METHOD} {LOGIN_URL}")
        print(f"   ├─ code: 长度={len(code or '')} 特征={mask(code)}（微信 code 仅 5 分钟内有效且只能用一次）")
        print(f"   └─ 请求体字段: {list(body.keys())}")

        started = time.time()
        response = request_with_proxy(
            LOGIN_METHOD,
            LOGIN_URL,
            headers=login_headers(),
            json=body,
            proxies=proxies,
            server=server,
        )
        cost = time.time() - started

        print(f"   📥 响应: HTTP {response.status_code}，耗时 {cost:.2f}s，Content-Type={response.headers.get('Content-Type', '')}")
        for hk in ("Set-Cookie", "x-trace-id", "X-Trace-Id", "x-ca-request-id"):
            if response.headers.get(hk):
                print(f"   📎 {hk}: {response.headers.get(hk)[:200]}")

        try:
            data = response.json()
        except Exception:
            data = {"code": response.status_code, "raw": response.text[:800]}
            print(f"   ⚠️ 响应不是 JSON，原文: {response.text[:300]}")

        if is_gate_blocked(response, data):
            print("🚫 [登录] 命中网关统一 401 拦截：请求未进入登录逻辑（与 code 无关，是登录端点/鉴权头错误）")
            if isinstance(data, dict):
                data["_gateBlocked"] = True
                data["_hint"] = GATE_BLOCK_HINT
            return None, data

        token = extract_token(data)
        if token:
            print(f"✅ [登录] token 获取成功: {mask(token)}")
            return token, data

        biz_code = data.get("code") if isinstance(data, dict) else None
        if biz_code not in (200, 0, "200", "0"):
            print(f"⚠️ [登录] 端点已通，但服务端业务拒绝: code={biz_code} msg={data.get('msg') if isinstance(data, dict) else ''}")
            print("   常见原因：jsCode 已被用过或已过期（微信 code 仅 5 分钟、一次性，脚本每次会自动取新 code）。")
            print(f"   若响应其实带了 token 但未识别，请把该响应发作者，并用 TASTIN_TOKEN_PATH 指定字段: {json_preview(data, 1000)}")
            return None, data

        print(f"❌ [登录] code=200 但未识别 token 字段（用 TASTIN_TOKEN_PATH 指定，如 result.userToken）")
        print(f"   完整响应: {json_preview(data, 1200)}")
        return None, data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, {"code": -1, "msg": f"登录请求异常: {exc}"}


def api_get(server: str, url: str, token: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(token),
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


def api_post(server: str, url: str, token: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(token),
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


# ============ 取码失败可读化 + 重试 ============
# 取码服务（本地 code 服务 / YYB）常见错误 → 人能看懂的原因与处理办法
CODE_ERROR_HINTS: List[Tuple[str, str]] = [
    ("login_buffer expired", "取码服务里该微信账号的登录态已失效（login_buffer 过期且自动刷新失败）"),
    ("re-scan", "需在取码服务后台对该微信账号重新扫码 / 重新登录"),
    ("not login", "该微信账号在取码服务上未登录"),
    ("not logged", "该微信账号在取码服务上未登录"),
    ("offline", "取码服务上该账号已离线"),
    ("no such account", "取码服务上找不到该账号 ID（检查 YYB_SERVER 的 地址@账号ID 是否填对）"),
]

# 命中这些关键字说明是账号侧状态问题，重试无意义，立即停止
CODE_FATAL_KEYWORDS = ("login_buffer expired", "re-scan", "not login", "not logged", "offline", "no such account")


def describe_code_error(exc: Exception) -> str:
    """把取码服务的原始报错翻译成可执行的处理提示。"""
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    hits = [hint for key, hint in CODE_ERROR_HINTS if key.lower() in lowered]
    if hits:
        return "；".join(hits) + f"；原始返回：{text}"
    return text


def is_fatal_code_error(exc: Exception | None) -> bool:
    if exc is None:
        return False
    lowered = (str(exc) or "").lower()
    return any(key in lowered for key in CODE_FATAL_KEYWORDS)


def fetch_code(server: str) -> Tuple[str | None, str]:
    """带重试地获取 code，返回 (code, 错误信息)。账号态问题不再无谓重试。"""
    last_exc: Exception | None = None
    for attempt in range(1, CODE_RETRY_TIMES + 1):
        try:
            code = get_code(server)
            if code:
                return code, ""
            last_exc = RuntimeError("取码服务未返回 code（返回为空）")
            print(f"❌ [授权] 第 {attempt}/{CODE_RETRY_TIMES} 次取码为空")
        except Exception as exc:
            last_exc = exc
            print(f"❌ [授权] 第 {attempt}/{CODE_RETRY_TIMES} 次取码失败: {exc}")

        if attempt >= CODE_RETRY_TIMES or is_fatal_code_error(last_exc):
            break
        print(f"⏳ [授权] {CODE_RETRY_INTERVAL}s 后重试取码")
        sleep(CODE_RETRY_INTERVAL)

    return None, describe_code_error(last_exc) if last_exc else "取码失败"


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    """每次直接取 code 换 token（不再读/写本地 tstcookie.json 缓存）。"""
    code, code_err = fetch_code(server)
    if not code:
        print(f"❌ [授权] 无法获取 code：{code_err}")
        return None, {"code": -1, "msg": f"取码失败：{code_err}", "_codeError": True}

    token, raw_login = login_by_code(server, code, proxies)
    return token, raw_login


def get_member_info(server: str, token: str, proxies: Dict[str, str] | None) -> Tuple[str, str]:
    """获取会员昵称与手机号（memberPhone），失败回退环境变量"""
    member_phone = MEMBER_PHONE_ENV
    nick_name = ""
    try:
        resp = api_get(server, MEMBER_DETAIL_URL, token, proxies)
        if resp.get("code") == 200 and resp.get("result"):
            result = resp["result"]
            nick_name = result.get("nickName") or ""
            member_phone = result.get("phone") or result.get("phoneEncrypted") or member_phone
    except Exception as exc:
        print(f"⚠️ [会员] 获取会员信息异常: {exc}")
    return nick_name, member_phone


def discover_activity_id(server: str, token: str, member_phone: str, proxies: Dict[str, str] | None) -> int:
    """自动获取当前有效的签到活动 ID：banner 入口 → 活动探测 → 默认值"""
    try:
        resp = api_post(server, BANNER_SIGN_URL, token, proxies, {
            "shopId": int(SHOP_ID),
            "birthday": "",
            "gender": 0,
            "nickName": None,
            "phone": member_phone,
        })
        if resp.get("code") == 200 and resp.get("result"):
            for item in resp["result"]:
                if isinstance(item, dict) and item.get("jumpCode") == "SIGN":
                    try:
                        para = json.loads(item.get("jumpPara", "{}"))
                        aid = para.get("activityId")
                        if aid:
                            print(f"🎯 [活动] 从 banner 获取到签到活动 ID: {aid} ({item.get('bannerName', '')})")
                            return int(aid)
                    except (json.JSONDecodeError, TypeError):
                        pass
    except Exception as exc:
        print(f"⚠️ [活动] banner 获取异常: {exc}")

    now_ms = int(time.time() * 1000)
    for aid in range(ACTIVITY_ID_START, ACTIVITY_ID_START + 30):
        resp = api_post(server, SIGN_INFO_URL, token, proxies, {"activityId": aid})
        if resp.get("code") == 200 and resp.get("result"):
            info = resp["result"].get("activityInfo", {}) or {}
            start_time = info.get("startTime", 0) or 0
            end_time = info.get("endTime", 0) or 0
            if start_time <= now_ms <= end_time:
                print(f"🎯 [活动] 探测到当前有效签到活动: ID={aid} ({info.get('name', '')})")
                return aid

    print(f"🎯 [活动] 未能自动发现，使用默认值: {ACTIVITY_ID_START}")
    return ACTIVITY_ID_START


def format_rewards(result: Dict[str, Any]) -> str:
    """格式化签到奖励信息"""
    lines: List[str] = []
    for reward in result.get("rewardInfoList") or []:
        reward_name = reward.get("rewardName")
        if reward_name:
            lines.append(str(reward_name))
        for coupon in reward.get("couponInfo") or []:
            lines.append(f"{coupon.get('name')}({coupon.get('couponContent')})")
        if to_float(reward.get("point", 0)) > 0:
            lines.append(f"积分+{reward['point']}")
    return "；".join(lines) if lines else "-"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "token": "-",
        "signMsg": "-",
        "rewardMsg": "-",
        "activityMsg": "-",
        "point": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    # 每个账号登录前取专属代理，登录与后续业务请求共用同一出口 IP
    _ACCOUNT_PROXY.update({"proxies": None, "exit_ip": "", "degraded": False})
    proxies, proxy_ip = get_valid_proxy(server)
    _ACCOUNT_PROXY["proxies"] = proxies
    _ACCOUNT_PROXY["exit_ip"] = proxy_ip
    sync_proxy_status(result)

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    try:
        token, raw_login = login_with_cache(server, proxies)
    except Exception as exc:
        # 登录阶段任何异常都不再向上抛，避免推送里出现一大段堆栈
        print(f"❌ [登录] 阶段异常: {exc}")
        traceback.print_exc()
        raw_login = {"code": -1, "msg": f"登录阶段异常: {exc}"}
        token = None

    if not token:
        if isinstance(raw_login, dict) and raw_login.get("_gateBlocked"):
            print("\n" + "!" * 64)
            print("🚫 根因：登录端点被网关 401 统一拦截，请求未进入登录逻辑，code 再新也无效。")
            print(raw_login.get("_hint", GATE_BLOCK_HINT))
            print("!" * 64 + "\n")
            result["error"] = (
                "登录端点被网关401拦截（登录URL/请求体/鉴权头错误），"
                "需抓包核对真实登录接口后用 TASTIN_LOGIN_URL 等环境变量修正"
            )
        elif isinstance(raw_login, dict) and raw_login.get("_codeError"):
            # 取码失败：给出可执行的处理办法，而不是一坨堆栈
            hint = (
                "处理办法：到取码服务后台把该微信账号重新扫码/重新登录后，"
                "再确认 YYB_SERVER 的「地址@账号ID」填写正确，然后重跑本任务。"
            )
            print("\n" + "!" * 64)
            print(f"🚫 根因：{raw_login.get('msg')}")
            print(hint)
            print("!" * 64 + "\n")
            result["error"] = f"{raw_login.get('msg')}。{hint}"
        else:
            result["error"] = f"登录失败: {json_preview(raw_login, 300)}"
        sync_proxy_status(result)
        return result

    result["token"] = mask(token)

    try:
        nick_name, member_phone = get_member_info(server, token, proxies)
        if member_phone:
            print(f"👤 [会员] {nick_name or '未知昵称'} memberPhone: {mask(member_phone)}")
        else:
            print("⚠️ [会员] 未获取到 memberPhone，签到可能失败（可配置 TASTIN_MEMBER_PHONE）")

        sign_ok = False
        last_msg = ""
        for attempt in range(1, MAX_SIGN_RETRIES + 1):
            activity_id = discover_activity_id(server, token, member_phone, proxies)
            result["activityMsg"] = f"签到活动 ID {activity_id}"

            info = api_post(server, SIGN_INFO_URL, token, proxies, {"activityId": activity_id})
            if info.get("code") == 200 and info.get("result"):
                act = info["result"].get("activityInfo", {}) or {}
                print(f"ℹ️ [活动] {act.get('name', '未知')}，每日签到: {'开启' if act.get('daySignOpen') else '关闭'}")

            wait_time = random.randint(1, 3)
            sleep(wait_time)

            sign_resp = api_post(server, SIGN_DO_URL, token, proxies, {
                "activityId": activity_id,
                "memberName": nick_name or "",
                "memberPhone": member_phone,
            })

            if sign_resp.get("code") == 200 and sign_resp.get("result"):
                sign_result = sign_resp["result"]
                result["signMsg"] = f"签到成功，连续 {sign_result.get('continuousNum', '?')} 天"
                result["rewardMsg"] = format_rewards(sign_result)
                print(f"✅ [签到] {result['signMsg']}")
                print(f"🎁 [奖励] {result['rewardMsg']}")
                sign_ok = True
                break

            msg = str(sign_resp.get("msg") or "")
            if "签过" in msg or "已签" in msg:
                result["signMsg"] = "今天已经签过了"
                print(f"✅ [签到] {result['signMsg']}（{msg}）")
                sign_ok = True
                break

            if is_waf_response(sign_resp):
                print("⚠️ [签到] 检测到阿里云 WAF 拦截（海外 IP 被拒），建议配置国内 PROXY_API")
                last_msg = "WAF 拦截"
            elif sign_resp.get("code") == -1:
                last_msg = f"网络/代理故障: {msg}"
            else:
                last_msg = msg or json_preview(sign_resp, 300)

            result["signMsg"] = f"签到失败: {last_msg}"
            print(f"❌ [签到] 第 {attempt} 次失败: {last_msg}")

            if attempt < MAX_SIGN_RETRIES:
                backoff = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
                print(f"⏳ [重试] {backoff}s 后重试")
                sleep(backoff)

        point_resp = api_post(server, MY_POINT_URL, token, proxies, {})
        if point_resp.get("code") == 200 and point_resp.get("result"):
            result["point"] = str(point_resp["result"].get("point", "?"))
            print(f"💰 [积分] 当前积分: {result['point']}")

        result["success"] = sign_ok
        if not sign_ok and last_msg:
            result["error"] = last_msg
        sync_proxy_status(result)
        return result

    except Exception as exc:
        traceback.print_exc()
        result["error"] = short_error(traceback.format_exc(), 300)
        print(f"❌ [账号] 执行失败: {exc}")
        sync_proxy_status(result)
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🍔 塔斯汀中国汉堡任务结果

━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
🎯 活动：{res["activityMsg"]}
📝 签到：{res["signMsg"]}
🎁 奖励：{res["rewardMsg"]}
💰 积分：{res["point"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{short_error(res['error'])}\n"

        content += "━━━━━━━━━━━━━━━━\n"

    return content


def diagnose() -> None:
    """仅诊断登录链路：code 服务 → 登录端点 → token 提取，不执行签到。"""
    print("🧪 [自检] 塔斯汀登录诊断模式（不执行签到）")
    body_desc = LOGIN_BODY_TEMPLATE or "(未设置)"
    token_desc = TOKEN_PATH_OVERRIDE or "(自动识别)"
    header_desc = ("已设置 " + LOGIN_EXTRA_HEADERS_RAW[:80]) if LOGIN_EXTRA_HEADERS_RAW else "(无)"
    print(f"   登录地址 LOGIN_URL  = {LOGIN_METHOD} {LOGIN_URL}")
    print(f"   请求体模板 BODY     = {body_desc}")
    print(f"   token 字段路径      = {token_desc}")
    print(f"   额外请求头          = {header_desc}")
    print(f"   code 服务 SERVERS   = {SERVERS}")
    print("─" * 64)

    for index, server in enumerate(SERVERS, 1):
        print(f"\n▌账号 {index} · code 服务: {server}")
        code, code_err = fetch_code(server)
        if not code:
            print(f"❌ code 获取失败：{code_err}")
            print("   请先启动本地 code 服务，并确认地址端口与 SERVERS / 环境变量 CODE_SERVER 一致。")
            print("   （YYB_SERVER 模式需为 地址@账号ID 格式且服务可达，且该微信账号登录态有效）")
            continue

        print(f"✅ 取到新 code（长度={len(code)}，特征={mask(code)}），立即用于登录测试（code 仅一次有效，勿重复使用）")
        token, raw = login_by_code(server, code, None)

        if token:
            print(f"\n🎉 自检通过：成功换到 token {mask(token)}")
            if isinstance(raw, dict):
                print("   响应顶层字段:", list(raw.keys()))
                inner = raw.get("data") if isinstance(raw.get("data"), dict) else raw
                if isinstance(inner, dict):
                    print("   data 内字段:", list(inner.keys()))
            print("   说明登录端点已正确；若正式运行仍异常，再排查 token 字段路径或业务接口。")
        elif isinstance(raw, dict) and raw.get("_gateBlocked"):
            print("\n🚫 自检结论：仍是网关统一 401 拦截，登录端点/鉴权头不对，code 无关。")
            print("   " + raw.get("_hint", GATE_BLOCK_HINT))
        else:
            print("\n⚠️ 自检结论：登录端点可达但未换到 token（可能 code 无效、缺手机号授权、请求体字段不全）。")
            print("   完整响应:", json_preview(raw, 1200))


def main() -> None:
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
                "rewardMsg": "-",
                "activityMsg": "-",
                "point": "-",
                "error": short_error(traceback.format_exc(), 300),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 塔斯汀任务执行完成                        ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    notify("🍔 塔斯汀任务完成", build_notify(results))


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
    # YYB / 内网取码服务必须直连（direct_session）
    endpoint, ref = _yyb_parts(server)
    if not endpoint or not ref or not appid:
        raise RuntimeError("YYB 参数不完整：需要 地址@账号ID 和 app_id")
    headers = {}
    api_key = _yyb_os.getenv("YYB_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    response = direct_session().post(
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
    import sys as _sys
    try:
        if "--diag" in _sys.argv or "-d" in _sys.argv:
            diagnose()
        else:
            main()
    except Exception as e:
        print(f"[!] 脚本异常: {e}")
        traceback.print_exc()
        notify("🍔 塔斯汀 脚本异常", f"脚本异常: {e}")
        _sys.exit(1)
#（注：内容由AI生成）
