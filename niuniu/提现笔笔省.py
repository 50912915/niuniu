"""
日期: 2025/8/21
name: 微信支付提现笔笔省
入口: 微信小程序 (https://a.c1ns.cn/X3ucP)
功能: 领券、查询
变量: YYB_SERVER (YYB-Go-Enhanced地址@账号ref，多个账号换行分割)
        YYB_API_KEY (可选，YYB服务端开启 YYB_PROTOCOL_TOKEN 时填同一个值，脚本自动带 Authorization: Bearer)
        LY_NOTIFY (是否推送日志，默认1推送；填 0/false/no 关闭)
        TXBBS_PROXY_API (代理提取api，留空回退共用 PROXY_API，都没有则全程直连)
        PROXY_TYPE (代理类型，http/socks5，默认http)
        PROXY_RETRY (单次「提取+验活」内部重试次数，默认3)
        PROXY_REFRESH (请求失败后重拉新代理次数，默认3，仍失败才兜底直连)
        PROXY_INTERVAL (拿到代理后等待秒数，默认3)
        PROXY_VALIDATE_URL (验活地址，默认 http://httpbin.org/ip)
        PROXY_FALLBACK (1开启/0关闭 最终兜底直连，默认1)
        PROXY_TIMEOUT (提取/验活超时秒数，默认10)
        注: 每个账号登录前取一个专属代理，登录/查余额/领券共用同一出口IP；
            YYB取码(内网Docker)与notify推送始终直连)
定时: 一天两次
cron: 10 11,12 * * *
------------更新日志------------
2025/8/21   V1.0    初始化脚本
2026/8/7    V2.0    适配YYB-Go-Enhanced及青龙单文件运行
2026/10/7   V2.1    按 YYB-Go-Enhanced 协议接口规范重写取码层（Bearer鉴权、信封解析、
                    错误定位、一次性code），修复 NOTIFY 恒真、raise_for_status 导致分支
                    失效、响应非JSON崩溃、账号缓存文件损坏崩溃等问题
2026/10/10  V2.3    彻底删除本地 token 缓存机制（wxzftxbbs_account_info.json 不再读写），
                    每个账号启动后直接取 code 登录；刚登录就查余额失败的异常情况自动重取
                    一次码再试，仍失败则跳过该账号
"""

import json
import random
import re
import time
import requests
import os
import base64
import hashlib
import traceback
import ssl
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

# ==================== 标准代理模块（变量区）====================

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    """环境变量开关：留空用默认值，填 0/false/no/off 视为关闭。"""
    value = (os.getenv(name, "") or "").strip().lower()
    if not value:
        return default
    return value not in ("0", "false", "no", "off")


NOTIFY = _env_flag("LY_NOTIFY", True)  # 是否推送日志，默认推送；0/false/no/off 关闭


def _errcode(data: Any) -> int:
    """取业务 errcode；字段缺失或不可转 int 时返回 -1（一律按失败处理）。"""
    try:
        return int((data or {}).get("errcode", -1))
    except (TypeError, ValueError):
        return -1


PROXY_API = os.getenv("TXBBS_PROXY_API", "") or os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()
PROXY_RETRY_TIMES = _env_int("PROXY_RETRY", 3)       # 单次「提取+验活」内部重试次数
PROXY_REFRESH_TIMES = _env_int("PROXY_REFRESH", 3)   # 请求失败后重拉新代理次数，仍失败才兜底
PROXY_VALIDATE_URL = os.getenv("PROXY_VALIDATE_URL", "http://httpbin.org/ip")
PROXY_FETCH_INTERVAL = _env_int("PROXY_INTERVAL", 3)  # 拿到代理后等待秒数
ENABLE_DIRECT_FALLBACK = os.getenv("PROXY_FALLBACK", "1") not in ("0", "false", "False", "no")
PROXY_TIMEOUT = _env_int("PROXY_TIMEOUT", 10)         # 提取/验活请求超时

# 连接级异常 = 代理故障（ProxyError/SSLError 均为 ConnectionError 子类）
_PROXY_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
# 当前账号代理状态：请求失败降级直连后，本账号后续请求直接直连
_ACCOUNT_PROXY: Dict[str, Any] = {"proxies": None, "exit_ip": "", "degraded": False}

class YYBGoEnhancedAdapter:
    """YYB-Go-Enhanced 的 wx.login code 客户端。

    协议约定（见仓库 docs/protocol-api.md）：
      请求: POST {server}/wxapp/getCode   body: {"ref": 账号ID或OpenID, "app_id": 小程序AppID}
      响应: {"code":0,"msg":"success","data":{"openid":..,"account":{..},"result":{"code":..}}}
    服务端启用 YYB_PROTOCOL_TOKEN 时，脚本需配置同样的 YYB_API_KEY，请求自动带 Authorization。
    取到的 code 是一次性的，不能跨请求或跨账号复用。
    """

    def __init__(self, wx_appid):
        self.wx_appid = wx_appid
        self.log_msgs = []
        self.account_info = {}   # 最近一次取码返回的 YYB 账号资料（remark / nickname）

    def log(self, msg, level="info"):
        self.log_msgs.append(str(msg))
        prefix = {"error": "ERROR", "warning": "WARN"}.get(level, "INFO")
        print(f"[{prefix}] {msg}")

    @staticmethod
    def parse_entry(entry):
        value = str(entry or "").strip()
        if not value or value == "[object Object]":
            raise ValueError("配置为空或无效")
        if "@" not in value:
            raise ValueError("格式应为 地址@账号ref")
        server, ref = value.rsplit("@", 1)
        server, ref = server.strip().rstrip("/"), ref.strip()
        if not server or not ref:
            raise ValueError("地址和账号ref均不能为空")
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        return server, ref

    def build_headers(self):
        """服务端开了 YYB_PROTOCOL_TOKEN 时带上 Bearer 令牌。"""
        headers = {"Content-Type": "application/json"}
        token = os.getenv("YYB_API_KEY", "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        return headers

    @staticmethod
    def status_hint(status):
        """把 HTTP 状态翻译成可执行的排查建议。"""
        return {
            400: "缺少 ref 或 app_id（路由已通，检查参数）",
            401: "协议令牌缺失或错误，检查 YYB_API_KEY 是否与服务端 YYB_PROTOCOL_TOKEN 一致",
            404: "路径或大小写不匹配，正确路径为 /wxapp/getCode",
            405: "取码接口需要 POST",
            409: "账号登录态失效或会话冲突，请到 YYB 控制台重新扫码",
            502: "上游协议/网络/代理调用失败",
        }.get(status, f"HTTP {status}")

    @property
    def display_name(self):
        """优先显示 YYB 账号备注，其次昵称；没有则返回空串。"""
        return self.account_info.get("remark") or self.account_info.get("nickname") or ""

    def get_code(self, entry):
        """取一次性 wx.login code；失败返回 None，不缓存、不复用。"""
        try:
            server, ref = self.parse_entry(entry)
            session = direct_session()  # 取码访问Docker内网，直连不继承青龙代理、不走提取代理
            response = session.post(
                f"{server}/wxapp/getCode",
                json={"ref": ref, "app_id": self.wx_appid},
                headers=self.build_headers(),
                timeout=_env_int("YYB_REQUEST_TIMEOUT", 20),
            )
        except ValueError as exc:
            self.log(f"[YYB] 配置无效 {entry!r}: {exc}", "error")
            return None
        except requests.RequestException as exc:
            self.log(f"[YYB] 取码网络异常: {exc}", "error")
            return None

        # 不用 raise_for_status：4xx/5xx 的响应体里才有真正的错误原因
        if response.status_code != 200:
            self.log(f"[YYB] 账号 {ref} 取码失败: {self.status_hint(response.status_code)}", "error")
            return None

        try:
            body = response.json()
        except ValueError:
            preview = response.text.strip().replace("\n", " ")[:120]
            self.log(f"[YYB] 返回非 JSON（多半是反代或登录页）: {preview}", "error")
            return None

        if not isinstance(body, dict):
            self.log(f"[YYB] 响应格式异常: {str(body)[:200]}", "error")
            return None
        if body.get("code") not in (0, "0", None):
            msg = body.get("msg") or body.get("message") or body.get("error") or "未知错误"
            self.log(f"[YYB] 账号 {ref} 取码失败: {msg}", "error")
            return None

        # 信封：data.result.code；兼容少数直接把 result 放在顶层的情况
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        code = result.get("code")
        if not code or str(code).lower() in ("invalid", "null"):
            self.log(f"[YYB] 账号 {ref} 未返回 wx.login code: {str(body)[:300]}", "error")
            return None

        self.account_info = data.get("account") if isinstance(data.get("account"), dict) else {}
        name = self.display_name
        self.log(f"[YYB] 账号 {ref}{('（' + name + '）') if name else ''} 获取code成功")
        return str(code)

class TLSAdapter(requests.adapters.HTTPAdapter):
    """
    自定义TLS
    解决unsafe legacy renegotiation disabled
    貌似python太高版本依然会报错
    """
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.options |= 0x4   # <-- the key part here, OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super(TLSAdapter, self).init_poolmanager(*args, **kwargs)

# ============================================================
# 标准代理模块（函数区）
#   每个账号登录前通过提取 API 获取专属代理，验活通过后绑定到该账号会话，
#   登录/查余额/领券等业务请求共用同一出口 IP；代理连接异常时重拉新代理重试
#   PROXY_REFRESH 次（同一会话换绑，headers/cookies 不丢），仍失败则兜底直连
#   并降级：本账号本次运行后续请求直接直连。
#   YYB 取码（Docker 内网）、notify 通知始终直连，不走代理。
# ============================================================


def direct_session() -> requests.Session:
    """直连会话：trust_env=False 不读系统代理，防止套娃。"""
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
            time.sleep(2)

    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(method: str, url: str, *, proxies: Dict[str, str] | None = None,
                       server: str = "", **kwargs) -> requests.Response:
    """统一请求入口，三级失败处理（详见归约：当前代理→重拉 PROXY_REFRESH 次→兜底直连并降级）。"""
    kwargs.setdefault("timeout", 30)

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
        _ACCOUNT_PROXY["degraded"] = True
        return direct_session().request(method, url, **kwargs)
    raise first_exc


class ProxySession(requests.Session):
    """业务会话：绑定账号专属代理；三级失败处理在会话层完成，headers/cookies 不丢。"""

    def __init__(self, server: str = "", log=None):
        super().__init__()
        self.trust_env = False
        self.pc_server = server
        self.pc_degraded = False
        self.pc_exit_ip = ""
        self._log = log

    def bind_proxy(self, proxies, exit_ip: str = ""):
        self.proxies.clear()
        if proxies:
            self.proxies.update(proxies)
        self.pc_degraded = False
        self.pc_exit_ip = exit_ip

    def request(self, method, url, **kw):
        if not self.proxies or self.pc_degraded:
            return super().request(method, url, **kw)   # 直连
        try:
            return super().request(method, url, **kw)
        except requests.exceptions.HTTPError:
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            first = exc
        # 二级：同会话换绑新代理重试 PROXY_REFRESH 次（cookies/headers 保留）
        for i in range(1, PROXY_REFRESH_TIMES + 1):
            new_proxies, new_ip = get_valid_proxy(self.pc_server)
            if new_proxies:
                self.bind_proxy(new_proxies, new_ip)   # 同会话换 IP，登录态不丢
                if self._log:
                    self._log(f"🔁 [代理] 已换新代理重试({i}/{PROXY_REFRESH_TIMES})，出口IP: {new_ip}", "warning")
                try:
                    return super().request(method, url, **kw)
                except requests.exceptions.HTTPError:
                    raise
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e2:
                    first = e2
                    if self._log:
                        self._log(f"⚠️ [代理] 新代理仍失败({i}/{PROXY_REFRESH_TIMES}): {e2}", "warning")
            else:
                if self._log:
                    self._log(f"⚠️ [代理] 拉取新代理失败({i}/{PROXY_REFRESH_TIMES})", "warning")
        # 三级：兜底直连并降级
        if ENABLE_DIRECT_FALLBACK:
            if self._log:
                self._log("🔁 [兜底] 重拉代理仍失败，切换直连；本账号本次运行后续请求将直连", "warning")
            self.proxies.clear()
            self.pc_degraded = True
            return super().request(method, url, **kw)
        raise first


class AutoTask:
    def __init__(self, script_name):
        """
        初始化自动任务类
        :param script_name: 脚本名称，用于日志显示
        """
        self.script_name = script_name
        self.wx_appid = "wxdb3c0e388702f785" # 微信小程序id
        self.wechat_code_adapter = YYBGoEnhancedAdapter(self.wx_appid)
        self.host = "discount.wxpapp.wechatpay.cn"
        self.nickname = ""
        self.token = ""
        self.points = 0.00
        self.user_agent = "Mozilla/5.0 (Linux; Android 12; M2012K11AC Build/SKQ1.220303.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/134.0.6998.136 Mobile Safari/537.36 XWEB/1340129 MMWEBSDK/20240301 MMWEBID/9871 MicroMessenger/8.0.48.2580(0x28003036) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64 MiniProgramEnv/android"

    def log(self, msg, level="info"):
        self.wechat_code_adapter.log(msg, level)

    def dict_keys_to_lower(self, obj):
        """
        递归将字典的所有键名转为小写
        """
        if isinstance(obj, dict):
            return {k.lower(): self.dict_keys_to_lower(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.dict_keys_to_lower(i) for i in obj]
        else:
            return obj

    def hide_phone(self, phone):
        """
        隐藏手机号中间4位
        """
        return phone[:3] + "****" + phone[-4:]

    def parse_response(self, response, scene=""):
        """
        统一解析业务响应（键名转小写）：反代/风控返回 HTML 时不再崩溃
        :return: 解析后的 dict，失败返回 None
        """
        try:
            data = response.json()
        except ValueError:
            preview = response.text.strip().replace("\n", " ")[:120]
            self.log(f"[{self.nickname}] {scene}响应非JSON，HTTP {response.status_code}: {preview}", level="error")
            return None
        if not isinstance(data, dict):
            self.log(f"[{self.nickname}] {scene}响应格式异常: {str(data)[:200]}", level="error")
            return None
        return self.dict_keys_to_lower(data)

    def check_env(self):
        """
        检查环境变量
        :return: 环境变量字符串
        """
        try:
            yyb_server = os.getenv("YYB_SERVER", "").strip()
            if not yyb_server:
                self.log("[检查环境变量] 没有找到YYB_SERVER；格式：地址@账号ref，多账号换行", level="error")
                return None
            for entry in yyb_server.splitlines():
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    self.wechat_code_adapter.parse_entry(entry)
                    yield entry
                except ValueError as exc:
                    self.log(f"[检查环境变量] 跳过无效配置 {entry!r}: {exc}", level="error")
        except Exception as e:
            self.log(f"[检查环境变量] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            raise

    def login(self, session, code):
        """
        登录
        :param session: session
        :param code: code
        :return: 登录结果
        """
        try:
            url = f"https://{self.host}/txbbs-user/user/login"
            session.headers['jscode'] = code
            response = session.get(url, timeout=15)
            response_json = self.parse_response(response, "登录")
            time.sleep(random.randint(3, 5))
            if response_json is None:
                return False
            if _errcode(response_json) == 0:
                self.token = (response_json.get('data') or {}).get('session_token') or ""
                if not self.token:
                    self.log("[登录] 未返回 session_token", level="warning")
                    return False
                session.headers['Session-Token'] = self.token
                session.headers.pop('jscode', None)  # code 一次性，登录完立即丢弃
                # self.log(f"[{self.nickname}] 登录成功 获取token: {self.token}")
                return self.token
            else:
                self.log(f"[登录] 失败 错误信息: {response_json.get('msg', '未知错误')}", level="warning")
                return False
        except requests.RequestException as e:
            self.log(f"[登录] 发生网络错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False
        except Exception as e:
            self.log(f"[登录] 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False

    def get_balance(self, session):
        """
        获取用户余额
        :param session: session
        :return: 用户余额
        """
        try:
            url = f"https://{self.host}/txbbs-mall/cashoutfree/getbalance"
            response = session.get(url, timeout=15)
            response_json = self.parse_response(response, "获取余额")
            time.sleep(random.randint(3, 5))
            if response_json is None:
                return None
            if _errcode(response_json) == 0:
                try:
                    self.points = int((response_json.get('data') or {}).get('balance') or 0) // 100
                except (TypeError, ValueError):
                    self.log(f"[{self.nickname}] 余额字段无法解析: {str(response_json)[:200]}", level="warning")
                    return None
                return self.points
            else:
                self.log(f"[{self.nickname}] 获取用户余额 发生错误: {response_json.get('msg', '未知错误')}", level="warning")
                return None
        except Exception as e:
            self.log(f"[{self.nickname}] 获取用户余额 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return None

    def get_gifts_list(self, session):
        """
        获取优惠券列表
        :param session: session
        :return: 优惠券列表
        """
        try:
            url = f"https://{self.host}/txbbs-mall/gift/listgifts?longitude=0&latitude=0"
            response = session.get(url, timeout=15)
            response_json = self.parse_response(response, "获取优惠券列表")
            time.sleep(random.randint(3, 5))
            if response_json is None:
                return []
            if _errcode(response_json) == 0:
                gifts = (response_json.get('data') or {}).get('gift_info_list') or []
                return gifts if isinstance(gifts, list) else []
            else:
                self.log(f"[{self.nickname}] 获取优惠券列表 发生错误: {response_json.get('msg', '未知错误')}", level="warning")
                return []
        except Exception as e:
            self.log(f"[{self.nickname}] 获取优惠券列表 发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return []

    def redeem_gift(self, session, gift_id):
        """
        领取优惠券
        :param session: session
        :param gift_id: 优惠券ID
        :return: 领取结果
        """
        try:
            url = f"https://{self.host}/txbbs-mall/gift/redeemgift"
            payload = {"gift_id": gift_id}
            response = session.post(url, json=payload, timeout=15)
            response_json = self.parse_response(response, "领取优惠券")
            time.sleep(random.randint(3, 5))
            if response_json is None:
                return False
            if _errcode(response_json) == 0:
                gift_info = (response_json.get('data') or {}).get('gift_info') or {}
                gift_name = (gift_info.get('coupon_info') or {}).get('name', '未知名称')
                self.log(f"[{self.nickname}] 领取优惠券成功: {gift_name}")
                return True
            else:
                error_msg = response_json.get('msg', '领取失败，未获取到具体信息')
                self.log(f"[{self.nickname}] 领取优惠券失败: {error_msg}", level="warning")
                return False
        except Exception as e:
            self.log(f"[{self.nickname}] 领取优惠券发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
            return False

    def run(self):
        """
        运行任务
        """
        try:
            self.log(f"【{self.script_name}】开始执行任务")
            entries = self.check_env()
            if entries is None:
                return
            for index, wx_id in enumerate(entries, 1):
                # 清理账号信息
                self.nickname = f"账号{index}"
                self.token = ""
                self.wechat_code_adapter.account_info = {}
                self.log("")
                self.log(f"------ 账号{index} 开始执行任务 ------")

                def take_code():
                    """取码并把 YYB 备注/昵称用作日志显示名；code 一次性，取到即用。"""
                    code = self.wechat_code_adapter.get_code(wx_id)
                    if code and self.wechat_code_adapter.display_name:
                        self.nickname = self.wechat_code_adapter.display_name
                    return code
                # 每账号一个专属会话：trust_env=False 防套娃
                account_name = f"账号{index}"
                session = ProxySession(server=account_name, log=self.log)
                session.headers.update({
                    "User-Agent": self.user_agent,
                    "authority": self.host,
                })
                # 登录前取该账号专属代理（未配置提取 API 时返回 None，自动直连）
                proxies, proxy_ip = get_valid_proxy(account_name)
                session.bind_proxy(proxies, proxy_ip)
                if proxies:
                    print(f"🌐 代理：使用专属代理，出口IP：{proxy_ip}")
                else:
                    print("🌐 代理：使用直连")
                if proxies and PROXY_FETCH_INTERVAL > 0:
                    time.sleep(PROXY_FETCH_INTERVAL)

                # 直接取码登录（不再读/写本地 token 缓存文件）
                code = take_code()
                if not code:
                    self.log(f"[{self.nickname}] YYB取码失败，跳过该账号", level="error")
                    session.close()
                    continue
                token = self.login(session, code)
                if not token:
                    self.log(f"[{self.nickname}] 登录失败，跳过该账号", level="error")
                    session.close()
                    continue
                # 刚登录完查余额就失败（异常情况），重取一次码再试
                if self.get_balance(session) is None:
                    self.log(f"[{self.nickname}] 首次查余额失败，重新取码登录一次", level="warning")
                    code = take_code()
                    if not code:
                        self.log(f"[{self.nickname}] 重新取码失败，跳过该账号", level="error")
                        session.close()
                        continue
                    token = self.login(session, code)
                    if not token:
                        self.log(f"[{self.nickname}] 重新登录失败，跳过该账号", level="error")
                        session.close()
                        continue
                # 获取优惠券列表
                gifts_list = self.get_gifts_list(session)
                for gift in gifts_list:
                    if gift.get('gift_type') == 'GT_COUPON' and gift.get('gift_status') == 'GS_AVAILABLE':
                        gift_id = gift.get('gift_id')
                        self.redeem_gift(session, gift_id)
                # 再次获取用户余额
                self.get_balance(session)
                self.log(f"[{self.nickname}] 当前提现免费券: {self.points}元")
                # 代理状态已在账号开头打印，这里只在代理中途失效降级时提示
                if session.pc_degraded:
                    print("🌐 代理：代理失效，已降级直连")
                # 清理session
                session.close()
                self.log(f"------ 账号{index} 执行任务结束 ------")
                self.log("")
        except Exception as e:
            self.log(f"【{self.script_name}】执行过程中发生错误: {str(e)}\n{traceback.format_exc()}", level="error")
        finally:
            if NOTIFY:
                try:
                    # 如果notify模块不存在，从远程下载至本地
                    if not os.path.exists("notify.py"):
                        url = "https://raw.githubusercontent.com/whyour/qinglong/refs/heads/develop/sample/notify.py"
                        response = direct_session().get(url, timeout=15)  # 通知模块下载：直连
                        with open("notify.py", "w", encoding="utf-8") as f:
                            f.write(response.text)
                    import notify
                    # 任务结束后推送日志
                    title = f"{self.script_name} 运行日志"
                    content = "\n".join(self.wechat_code_adapter.log_msgs)
                    notify.send(title, content)
                except Exception as exc:
                    # 推送失败不能影响任务本身的结果
                    print(f"[WARN] 日志推送失败（不影响任务结果）: {exc}")

if __name__ == "__main__":
    auto_task = AutoTask("微信支付提现笔笔省")
    auto_task.run()
#（注：内容由AI生成）
