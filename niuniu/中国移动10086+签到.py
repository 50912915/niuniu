#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# name: 中国移动10086+
# cron: 37 8 * * *
"""
name: 中国移动10086+签到
cron: 37 8 * * *

中国移动 10086+ 微信小程序每日签到（「签到有礼」活动）+ 第三签抽奖，
全程基于 YYB-Go-Enhanced 自动取码登录，无需每日手动抓包。

青龙环境变量：
  YYB_SERVER          必填，YYB-Go-Enhanced地址@微信账号标识，多账号每行一条
                      例：yyb-go:8000@1
  YYB_API_KEY         可选，对应 YYB_PROTOCOL_TOKEN，设置后带 Authorization 头
  YD_ACTIVITY_ID      可选，「签到有礼」活动 ID，默认 1021122301
  YD_DRY_RUN          可选，=1 只查询不签到（首次建议这样试）
  YD_ENABLE_TASK      可选，默认 1；=1 自动领取「已完成」任务的奖励
  YD_FINISH_TASK_IDS  可选，逗号分隔的 taskId，对这些任务先调 finishTask 再领奖（默认空）
  YD_LOGIN_RETRY      可选，登录重试次数，默认 3（code 一次性，失败会换新 code 重试）
  YD_DEBUG            可选，=1 打印请求明细，便于排查接口变动
  YYB_REQUEST_TIMEOUT 可选，YYB 接口超时秒数，默认 40

依赖：requests、pycryptodome（仓库 requirements.txt 已声明）
通知：优先使用青龙内置 notify.py；未配置时回落到 PushPlus / Server酱 / 企业微信 / Bark。
      通知失败不影响签到任务结果。

功能：
  1. YYB 取码 → 10086+ 小程序 login → wmhsso 加密 token → AES 解密 → 打开 H5 拿
     QWHD_SESSION_TOKEN，全链路免抓包；
  2. 每日签到（POST /api/mark/do/mark）。本期「签到有礼」共 3 签：第 1、2 签必得
     （0.1 元话费券、100MB 日包），第 3 签为抽奖档（100 元话费，概率 30%）。
     概率判定由服务端在 do/mark 内完成（奖品领光时返回 PRIZE_NO_STOCK），
     前端 JS 里的 DO_LOTTERY 只决定弹哪个动画，因此固定调 do/mark 即可覆盖第三签抽奖；
  3. 自动领取已达成任务的签到专属 AI 豆奖励；
  4. 通知中输出该账号的剩余流量、剩余通话、话费余额、签到专属 AI 豆。

  注：未在 10086+ 小程序绑定中国移动号码的微信账号无法取票
  （wmhsso 回 10002 无效的会话），这类账号标记为「⏭️ 跳过」且不计入失败。

作者：lcmovie https://github.com/lcmovie
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import random
import re
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    print("❌ 缺少依赖：pip install requests")
    sys.exit(1)

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    try:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util.Padding import unpad
    except ImportError:
        print("❌ 缺少依赖：pip install pycryptodome")
        sys.exit(1)

APP_NAME = "中国移动10086+签到"

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
APP_NO = "wx43aab19a93a3a6f2"          # 中国移动10086+ 小程序 AppID
APP_VER = "530"                        # 小程序基础库版本（用于 Referer）
APPLET_BASE = "https://wx.online-cmcc.cn"
HUB_BASE = "https://wx.10086.cn/qwhdhub"
ACTIVITY_ID = (os.getenv("YD_ACTIVITY_ID", "") or "1021122301").strip() or "1021122301"

# 10086+ 小程序内 newdirect 统一入口（GET）
NEWDIRECT = (APPLET_BASE + "/wmhnewcenter/wechat86-applet/newdirect/execute"
             "?channelMark=wechat&service=esb&")

# 服务端加解密密钥（由 app-service.js 混淆字符串 + projectName="cmos-10086mp" 还原）
AES_KEY = b"1234123412ABCDEF"
AES_IV = b"ABCDEF1234123412"

HERE = os.path.dirname(os.path.abspath(__file__))

YYB_TIMEOUT = float(os.getenv("YYB_REQUEST_TIMEOUT", "40") or 40)
YD_TIMEOUT = float(os.getenv("YD_REQUEST_TIMEOUT", "30") or 30)
DRY_RUN = os.getenv("YD_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")
ENABLE_TASK = os.getenv("YD_ENABLE_TASK", "1").strip().lower() not in ("0", "false", "no", "off")
LOGIN_RETRY = max(1, int(os.getenv("YD_LOGIN_RETRY", "3") or 3))
DEBUG = os.getenv("YD_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")
FINISH_TASK_IDS = [x.strip() for x in (os.getenv("YD_FINISH_TASK_IDS", "") or "").split(",") if x.strip()]

# flowSumInfo.unit → 展示单位 / 换算到 KB 的倍数
UNIT_TEXT = {1: "MB", 2: "GB", 3: "天", 4: "分钟", 5: "条", 6: "KB", 7: "元"}
UNIT_MULT = {6: 1, 1: 1024, 2: 1048576}

APP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/122.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
          "WindowsWechat(0x63090b19) XWEB/11581")
H5_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003123) "
         "NetType/WIFI Language/zh_CN")

ASK_CONFIG = ("feeCard,callBalance,broadband,noReal,noPuk,fareLink,recommendCard,xmeFloatBar,"
              "showGrayUI,NBEJXHSN,commodityDisableProvince,txCooperateOfflinePro,netAge,"
              "oneKeyLogin,miniSubscribePopup,wmhHideDetail,wmhHideDetailMarket,"
              "domainNameSelection,wmhHideDetailWeChat,hideStarProvince")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def mask_phone(value: Any) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    return f"{text[:3]}****{text[-4:]}" if len(text) >= 11 else (str(value or "") or "-")


def preview(data: Any, limit: int = 300) -> str:
    if isinstance(data, (dict, list)):
        try:
            text = json.dumps(data, ensure_ascii=False)
        except Exception:
            text = str(data)
    else:
        text = str(data)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def banner(lines: List[str]) -> None:
    print("=" * 62)
    for line in lines:
        print(line)
    print("=" * 62)


def rand_trace() -> str:
    return f"{int(time.time() * 1000)}_{random.randint(10000000, 99999999)}"


class NotBoundError(RuntimeError):
    """该微信账号尚未在 10086+ 小程序绑定中国移动号码（无法换票，重试也没用）。"""


class LegacyTLSAdapter(HTTPAdapter):
    """wx.online-cmcc.cn / wx.10086.cn 只支持旧 TLS 套件，需放开安全等级。

    注意：requests 经 HTTP/SOCKS 代理访问 HTTPS 站点时，用的是 proxy_manager_for
    建立的“代理连接池（CONNECT 隧道）”，与 init_poolmanager 的直连池不是同一个。
    若只在 init_poolmanager 放宽套件，走代理时仍会用系统默认 SSLContext
    （OpenSSL3 默认 SECLEVEL=2，禁用旧套件），报 SSLV3_ALERT_HANDSHAKE_FAILURE。
    因此直连池与代理 CONNECT 隧道都要注入同一个宽松 SSLContext。
    """

    @staticmethod
    def _build_legacy_context():
        ctx = ssl.create_default_context()
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            try:
                ctx.set_ciphers("DEFAULT")
            except ssl.SSLError:
                pass
        return ctx

    def __init__(self, *args, **kwargs):
        self._legacy_ctx = self._build_legacy_context()
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._legacy_ctx          # 直连池
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs.setdefault("ssl_context", self._legacy_ctx)   # 代理 CONNECT 隧道（到目标站）
        return super().proxy_manager_for(proxy, **proxy_kwargs)


# ============================================================
# 代理 IP（统一代理模式；每个账号一个专属出口 IP，小程序 app 与 H5 h5 会话共用）
#   · 账号开始时提取+验活一个代理并绑定到 app/h5；登录/查资源/签到/活动全程同一出口 IP；
#   · 请求“连接类”异常时，先重新拉取新代理（提取+验活）并重试，最多 PROXY_REFRESH_TIMES 次；
#     新代理仍全失败才兜底切直连（本账号本次运行降级直连，下次定时运行再尝试代理）；
#   · requests 对 4xx/5xx 默认正常返回响应（不重拉），由业务自行处理；
#   · 登录换码重试会重建 app 会话，重建时同样绑定本账号代理；
#   · YYB 取码（内网）、消息通知（notify/PushPlus/企微/Bark/Server酱）始终直连。
# ============================================================
PROXY_API              = (os.getenv("YD10086_PROXY_API") or os.getenv("PROXY_API") or "").strip()
PROXY_TYPE             = (os.getenv("YD10086_PROXY_TYPE") or os.getenv("PROXY_TYPE") or "http").strip().lower()
PROXY_RETRY_TIMES      = int(os.getenv("YD10086_PROXY_RETRY", "3"))
PROXY_REFRESH_TIMES    = int(os.getenv("YD10086_PROXY_REFRESH", "3"))
PROXY_FETCH_INTERVAL   = int(os.getenv("YD10086_PROXY_INTERVAL", "3"))
PROXY_VALIDATE_URL     = os.getenv("YD10086_PROXY_VALIDATE_URL", "http://httpbin.org/ip").strip()
ENABLE_DIRECT_FALLBACK = os.getenv("YD10086_PROXY_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off")


def _direct_session():
    s = requests.Session()
    s.trust_env = False
    return s


def parse_proxy_response(text):
    """兼容 JSON(data列表/对象、扁平ip+port、result) 与 ip:port[:账密] 文本"""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        obj = None
        if isinstance(data.get("data"), list) and data["data"]:
            obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            obj = data["data"]
        elif data.get("ip") and data.get("port"):
            obj = data
        elif isinstance(data.get("result"), dict):
            obj = data["result"]
        if obj:
            host = obj.get("ip") or obj.get("host")
            port = obj.get("port")
            if host and port:
                return {"host": str(host), "port": int(port),
                        "username": obj.get("user") or obj.get("username") or "",
                        "password": obj.get("pass") or obj.get("password") or ""}
    except Exception:
        pass
    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            try:
                return {"host": parts[0], "port": int(parts[1]),
                        "username": parts[2] if len(parts) > 2 else "",
                        "password": parts[3] if len(parts) > 3 else ""}
            except Exception:
                return None
    return None


def build_proxy_dict(info):
    """构造 proxies dict，支持账密认证（URL编码）与 http/socks5"""
    if not info:
        return None
    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    auth = ""
    if info.get("username") and info.get("password"):
        from urllib.parse import quote
        auth = "%s:%s@" % (quote(info["username"]), quote(info["password"]))
    url = "%s://%s%s:%s" % (scheme, auth, info["host"], info["port"])
    return {"http": url, "https": url}


def validate_proxy(proxies):
    """验活并取出口 IP，成功返回 (True, ip)"""
    if not proxies:
        return False, ""
    try:
        resp = _direct_session().get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if resp.status_code == 200:
            try:
                ip = resp.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            return True, str(ip)
    except Exception:
        pass
    return False, ""


def get_valid_proxy(account_name: str = ""):
    """提取并验活一个代理，内部最多重试 PROXY_RETRY_TIMES 次；失败返回 (None,'')"""
    if not PROXY_API:
        return None, ""
    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            resp = _direct_session().get(PROXY_API, timeout=15)
            info = parse_proxy_response(resp.text)
            if not info:
                print("[代理] %s 第%d次解析失败" % (account_name, index))
            else:
                proxies = build_proxy_dict(info)
                ok, ip = validate_proxy(proxies)
                if ok:
                    print("[代理] %s 代理可用，出口IP %s" % (account_name, ip))
                    return proxies, ip
                print("[代理] %s 第%d次代理不可用" % (account_name, index))
        except Exception as e:
            print("[代理] %s 第%d次获取异常: %s" % (account_name, index, e))
        if index < PROXY_RETRY_TIMES:
            time.sleep(2)
    return None, ""


class ProxySession(requests.Session):
    """业务会话：trust_env=False 防套娃；重写 request 做三级失败处理（当前代理→重拉N次→兜底直连）"""

    def __init__(self, proxies=None, account: str = ""):
        super().__init__()
        self.trust_env = False
        self._proxies = proxies or None
        self._account = account
        if proxies:
            self.proxies.update(proxies)

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 20)
        if not self._proxies:
            return super().request(method, url, **kwargs)

        # ① 当前代理
        saved = None
        try:
            return super().request(method, url, **kwargs)
        except Exception as first:
            saved = first
            print("[代理] %s 请求失败: %s，开始重新拉取代理重试（最多 %d 次）"
                  % (self._account, first, PROXY_REFRESH_TIMES))

        # ② 重拉新代理并重试
        for i in range(1, PROXY_REFRESH_TIMES + 1):
            new, ip = get_valid_proxy("%s#重拉%d" % (self._account, i))
            if not new:
                print("[代理] 第 %d/%d 次未拉取到可用代理" % (i, PROXY_REFRESH_TIMES))
            else:
                self.proxies.clear()
                self.proxies.update(new)
                self._proxies = new
                print("[代理] 第 %d/%d 次已换新代理（出口 %s），重试请求" % (i, PROXY_REFRESH_TIMES, ip))
                try:
                    return super().request(method, url, **kwargs)
                except Exception as e:
                    print("[代理] 第 %d 个新代理请求仍失败: %s" % (i, e))
            if i < PROXY_REFRESH_TIMES:
                time.sleep(2)

        # ③ 兜底切直连并降级；关闭兜底则抛错
        if not ENABLE_DIRECT_FALLBACK:
            raise saved
        print("[代理] 重新拉取代理仍失败，兜底切直连重试")
        self.proxies.clear()
        self._proxies = None
        return super().request(method, url, **kwargs)


def new_session(user_agent: str, proxies=None, account: str = "") -> requests.Session:
    sess = ProxySession(proxies=proxies, account=account)
    sess.mount("https://", LegacyTLSAdapter())   # 保留旧 TLS 站点兼容（与代理不冲突）
    sess.headers.update({"User-Agent": user_agent, "Accept-Language": "zh_CN"})
    return sess


def aes_decrypt_new(text: str) -> str:
    """服务端 encryptData 解密：双层 base64 + AES-CBC/PKCS7。"""
    raw = base64.b64decode(text)
    inner = base64.b64decode(raw)
    plain = AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(inner)
    return unpad(plain, 16).decode("utf-8")


def fmt_amount(raw: Any, unit: Any) -> str:
    """按 flowSumInfo 的 unit 语义格式化数值（流量类换算到 MB/GB）。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "--"
    try:
        code = int(unit)
    except (TypeError, ValueError):
        code = 0
    if code in UNIT_MULT:
        kb = value * UNIT_MULT[code]
        if abs(kb) >= 1048576:
            return f"{kb / 1048576:.2f}GB"
        return f"{kb / 1024:.2f}MB"
    return f"{value:g}{UNIT_TEXT.get(code, '')}"


def fmt_yuan(raw: Any) -> str:
    try:
        return f"{float(raw):.2f}元"
    except (TypeError, ValueError):
        return "--"


def deep_find(node: Any, key: str) -> Any:
    """广度优先找第一个同名 key（服务端外层结构偶有变化时兜底）。"""
    queue = [node]
    seen = 0
    while queue and seen < 400:
        seen += 1
        cur = queue.pop(0)
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


# --------------------------------------------------------------------------- #
# 账号配置
# --------------------------------------------------------------------------- #
class AccountTarget:
    def __init__(self, endpoint: str = "", ref: str = "", index: int = 0):
        text = (endpoint or "").strip().rstrip("/")
        if text and not re.match(r"^https?://", text, re.I):
            text = "http://" + text
        self.endpoint = text
        self.ref = str(ref or "")
        self.index = index
        self.label_suffix = ""

    @property
    def yyb_base(self) -> str:
        return self.endpoint

    @property
    def label(self) -> str:
        number = self.ref if self.ref else str(self.index or "?")
        suffix = f"（{self.label_suffix}）" if self.label_suffix else ""
        return f"账号 {number}{suffix}"

    @property
    def server(self) -> str:
        return f"{self.endpoint}@{self.ref}" if self.endpoint and self.ref else "未配置 YYB_SERVER"


def load_accounts() -> List[AccountTarget]:
    raw = os.getenv("YYB_SERVER", "").strip()
    accounts: List[AccountTarget] = []
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "@" not in line:
                print(f"⚠️ [配置] 忽略无账号标识的行：{line}")
                continue
            endpoint, ref = (part.strip() for part in line.rsplit("@", 1))
            if not endpoint or not ref:
                print(f"⚠️ [配置] 忽略不完整行：{line}")
                continue
            accounts.append(AccountTarget(endpoint=endpoint, ref=ref, index=len(accounts) + 1))
    if not accounts:
        raise RuntimeError(
            "未读取到有效账号。请配置环境变量 YYB_SERVER，每行一个账号，格式：\n"
            "  YYB地址@账号ID      例：http://192.168.1.10:8000@1\n"
            "  多账号就是多行（第 2 个账号写 @2，以此类推）"
        )
    return accounts


# --------------------------------------------------------------------------- #
# YYB 调用
# --------------------------------------------------------------------------- #
def yyb_post(account: AccountTarget, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = account.yyb_base + path
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "okhttp/3.12.13"}
    api_key = os.getenv("YYB_API_KEY", "").strip() or os.getenv("YYB_PROTOCOL_TOKEN", "").strip()
    if api_key:
        headers["Authorization"] = api_key if " " in api_key else "Bearer " + api_key
    resp = requests.post(url, json=payload, headers=headers, timeout=YYB_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if DEBUG:
        print(f"    · [YYB] {path} → {preview(body, 400)}")
    if isinstance(body, dict) and body.get("code") not in (0, None):
        raise RuntimeError(f"YYB {path} 返回错误：{body.get('msg') or preview(body)}")
    return body


def yyb_get_wx_code(account: AccountTarget) -> str:
    """取 wx.login code（一次性，拿到后要尽快用于登录）。"""
    body = yyb_post(account, "/wxapp/getCode", {"ref": account.ref, "app_id": APP_NO})
    data = body.get("data") or {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    for candidate in (result.get("code"), data.get("code"), deep_find(body, "code")):
        if isinstance(candidate, str) and len(candidate) >= 16:
            return candidate
    raise RuntimeError(f"YYB 未返回有效 code：{preview(body, 200)}")


# --------------------------------------------------------------------------- #
# 10086+ 会话
# --------------------------------------------------------------------------- #
class Yd10086Client:
    """一条完整的免抓包会话：小程序 login → wmhsso → H5 cookie → 业务接口。"""

    def __init__(self, account: AccountTarget):
        self.account = account
        # 为本账号取专属代理（未配置 API 时返回 None，自动直连），app/h5 共用同一出口 IP
        _proxies, _proxy_ip = get_valid_proxy(account.label)
        if _proxies and PROXY_FETCH_INTERVAL > 0:
            time.sleep(PROXY_FETCH_INTERVAL)
        self._proxies = _proxies                 # 供 login() 换码重试重建 app 时复用
        self._proxy_label = account.label
        self.app = new_session(APP_UA, proxies=_proxies, account=account.label)
        self.app.headers["Referer"] = f"https://servicewechat.com/{APP_NO}/{APP_VER}/page-frame.html"
        self.h5 = new_session(H5_UA, proxies=_proxies, account=account.label)
        self.phone = ""
        self.province = ""
        self.session_id = ""
        self.wmh_token = ""
        self.channel = "wechat"
        self.mark_page = ""

    # ---------------- 链路 1：拿小程序会话 ----------------
    def _applet_login(self) -> None:
        code = yyb_get_wx_code(self.account)
        if DEBUG:
            print(f"    · [YYB] code = {code[:48]}")
        resp = self.app.get(APPLET_BASE + "/wmhnewcenter/wechat86-applet/login",
                            headers={"X-WX-Code": code}, timeout=YD_TIMEOUT)
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("sessionId"):
            raise RuntimeError(f"小程序登录失败：{preview(body, 240)}")
        self.session_id = str(data["sessionId"])
        user = data.get("appletUser") or {}
        self.phone = str(user.get("telephone") or "")
        self.province = str(user.get("provinceCode") or "")
        if not self.phone:
            # 未绑定中国移动号码：sessionId 是匿名会话，后续 wmhsso 只会回
            # 「10002 无效的会话」。早失败、给明确原因，避免误导为重试问题。
            raise NotBoundError("该微信账号尚未绑定中国移动手机号")
        self.app.headers.update({
            "X-WECHAT86-APPLET-JWT": self.session_id,
            "X-CORE-APPLET-TOKEN": self.session_id,
            "X-APPLET-ASK-CONFIG": ASK_CONFIG,
            "X-EMERGENCY-NEW": "yes",
            "X-EMERGENCY-PROVINCE": self.province,
            "Lrsbhbg8": "ZS93dUFVa2kzaEpQSjM0SG55MUFDdz09",
        })

    # ---------------- 链路 2：换取 H5 会话 token ----------------
    def _h5_token(self) -> None:
        resp = self.app.post(APPLET_BASE + "/wmhnewcenter/wechat86-applet/wmhsso"
                             "?redirectSource=SSO_YQS", timeout=YD_TIMEOUT)
        body = resp.json()
        encrypted = (body or {}).get("encryptData") or deep_find(body, "encryptData")
        if not encrypted:
            raise RuntimeError(f"wmhsso 未返回 encryptData：{preview(body, 240)}")
        inner = json.loads(aes_decrypt_new(encrypted))
        token = ((inner.get("bean") or {}).get("token")
                 or deep_find(inner, "token") or "")
        if not token:
            raise RuntimeError(f"wmhsso 解密后无 token：{preview(inner, 240)}")
        self.wmh_token = str(token)

    # ---------------- 链路 3：打开活动页拿 QWHD_SESSION_TOKEN ----------------
    def _open_mark_page(self) -> None:
        self.mark_page = (f"{HUB_BASE}/qwhdmark/{ACTIVITY_ID}"
                          "?redirectSource=SSO_YQS&isReload=1")
        self.h5.headers["Referer"] = self.mark_page
        self.h5.get(f"{self.mark_page}&wmhToken={self.wmh_token}", timeout=YD_TIMEOUT)
        cookie = self.h5.cookies.get("QWHD_SESSION_TOKEN") or ""
        if not cookie:
            cookie = "; ".join(f"{c.name}={c.value}" for c in self.h5.cookies)
        self.h5.headers.update({
            "Content-Type": "application/json;charset=UTF-8",
            "cookie": f"QWHD_SESSION_TOKEN={cookie}" if cookie else "",
            "login-check": "1",
            "Origin": "https://wx.10086.cn",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.mark_page,
        })
        if not cookie:
            raise RuntimeError("未取得 QWHD_SESSION_TOKEN，活动页可能未对当前账号开放")

    def login(self) -> None:
        """按重试次数完成整条链路（code 一次性，失败换新 code 重试）。"""
        last_error: Optional[Exception] = None
        for attempt in range(1, LOGIN_RETRY + 1):
            try:
                # 换码重试会重建 app 会话，需重新绑定本账号代理，保证登录出口 IP 不变
                self.app = new_session(APP_UA, proxies=self._proxies, account=self._proxy_label)
                self.app.headers["Referer"] = (f"https://servicewechat.com/{APP_NO}/"
                                               f"{APP_VER}/page-frame.html")
                self._applet_login()
                self._h5_token()
                self._open_mark_page()
                return
            except NotBoundError:
                raise                                      # 不是重试能解决的问题
            except Exception as exc:                       # noqa: BLE001
                last_error = exc
                if attempt < LOGIN_RETRY:
                    print(f"    ⚠️ 第 {attempt}/{LOGIN_RETRY} 次登录失败：{preview(exc, 160)}，换码重试…")
                    time.sleep(2 + attempt)
        raise RuntimeError(f"登录失败（已重试 {LOGIN_RETRY} 次）：{last_error}")

    # ---------------- 小程序侧：流量 / 通话 / 话费 / 积分 ----------------
    def applet_get(self, operation: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.app.get(NEWDIRECT + operation, params=params or {}, timeout=YD_TIMEOUT)
        headers = {k.lower(): v for k, v in resp.headers.items()}
        payload = resp.json()
        if str(headers.get("lrsbhbg8", "")).lower() == "true":
            encrypted = payload.get("encryptData")
            if encrypted:
                payload = json.loads(aes_decrypt_new(encrypted))
        if DEBUG:
            print(f"    · [小程序] {operation} → {preview(payload, 500)}")
        obj = payload.get("object") if isinstance(payload, dict) else None
        return obj if isinstance(obj, dict) else (payload if isinstance(payload, dict) else {})

    def query_resources(self) -> Dict[str, Any]:
        """剩余流量 / 剩余通话 / 话费余额 / 签到专属 AI 豆。"""
        info: Dict[str, Any] = {
            "flow_total": "-", "flow_used": "-", "flow_left": "-",
            "flow_breakdown": [], "voice_total": "-", "voice_left": "-",
            "balance": "-", "month_fee": "-", "points": "-",
        }

        # 流量 + 通话
        try:
            res = self.applet_get("operation=queryFlowInfo",
                                  {"directionalFlow": 1, "videoFlag": 1})
            data = res.get("resultData") or {}
            cards = data.get("flowSumInfo") or []
            for card in cards:
                name = str(card.get("cardName") or "")
                unit = card.get("unit")
                left = fmt_amount(card.get("flowRemain"), unit)
                total = fmt_amount(card.get("flowSum"), unit)
                used = fmt_amount(card.get("flowUse"), unit)
                card_id = str(card.get("cardId") or "")
                if card_id == "00":
                    info["flow_total"], info["flow_used"], info["flow_left"] = total, used, left
                if card_id == "04":
                    info["voice_total"], info["voice_left"] = total, left
                if card_id in ("00", "01", "08"):
                    info["flow_breakdown"].append((name, left, total))
            if info["flow_left"] == "-" and cards:
                first = cards[0]
                info["flow_total"] = fmt_amount(first.get("flowSum"), first.get("unit"))
                info["flow_used"] = fmt_amount(first.get("flowUse"), first.get("unit"))
                info["flow_left"] = fmt_amount(first.get("flowRemain"), first.get("unit"))
        except Exception as exc:                           # noqa: BLE001
            print(f"    ⚠️ 流量/通话查询失败：{preview(exc, 140)}")

        # 话费余额（curFee = 账户余额，realFee = 本月实时话费）
        try:
            res = self.applet_get("operation=getRealFee")
            data = res.get("resultData") or {}
            info["balance"] = fmt_yuan(data.get("curFee"))
            info["month_fee"] = fmt_yuan(data.get("realFee"))
            if data.get("oweFee") not in (None, "", "0.00"):
                info["owe"] = fmt_yuan(data.get("oweFee"))
            info["pay_type"] = data.get("payType") or ""
        except Exception as exc:                           # noqa: BLE001
            print(f"    ⚠️ 话费余额查询失败：{preview(exc, 140)}")

        # 签到专属 AI 豆（10086+ 积分）
        try:
            res = self.applet_get("operation=getPoints")
            data = res.get("resultData") or {}
            info["points"] = str(data.get("totalPoint", "-"))
        except Exception as exc:                           # noqa: BLE001
            print(f"    ⚠️ 积分查询失败：{preview(exc, 140)}")

        return info

    # ---------------- H5 侧：签到有礼 ----------------
    def hub_post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.h5.post(HUB_BASE + path, json=data if data is not None else {},
                            timeout=YD_TIMEOUT)
        try:
            body = resp.json()
        except Exception:                                  # noqa: BLE001
            body = {"code": f"HTTP_{resp.status_code}", "msg": resp.text[:200]}
        if DEBUG:
            print(f"    · [H5] POST {path} [{resp.status_code}] → {preview(body, 500)}")
        if not isinstance(body, dict):
            body = {"code": "BAD_BODY", "msg": preview(body, 160)}
        return body

    @staticmethod
    def _ok(body: Dict[str, Any]) -> bool:
        return str(body.get("code") or "").upper() == "SUCCESS"

    def prize_info(self) -> Dict[str, Any]:
        body = self.hub_post("/api/mark/info/prizeInfo")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def mark_history(self) -> List[Dict[str, Any]]:
        body = self.hub_post("/api/mark/info/markInfo")
        data = body.get("data")
        return data if isinstance(data, list) else []

    def task_list(self) -> List[Dict[str, Any]]:
        body = self.hub_post("/api/mark/task/taskList")
        data = body.get("data")
        if not isinstance(data, dict):
            return []
        tasks = list(data.get("tasks") or []) + list(data.get("specialTasks") or [])
        return [t for t in tasks if isinstance(t, dict)]

    def channel_of(self) -> str:
        try:
            body = self.hub_post(f"/api/mark/user/info?_traceId={rand_trace()}")
            data = body.get("data") or {}
            self.channel = str(data.get("channel") or "wechat")
        except Exception:                                  # noqa: BLE001
            self.channel = "wechat"
        return self.channel

    def do_sign(self) -> Dict[str, Any]:
        """每日签到（含第三签抽奖，服务端在该接口内完成概率判定）。"""
        result: Dict[str, Any] = {"action": "-", "success": False, "message": "", "prize": "-"}

        before = self.prize_info()
        if not before:
            result["message"] = "签到信息获取失败（接口无数据）"
            return result

        total = before.get("totalMarkTimes")
        marked = before.get("markedTimes")
        if before.get("todayMarked"):
            result.update(action="skip", success=True,
                          message=f"今日已签到（本期 {marked}/{total}）")
            result["prize"] = self._today_prize()
            return result

        counter = self._mark_counter(before)
        prizes = before.get("prizes") or []
        upcoming = prizes[counter] if 0 <= counter < len(prizes) else None
        lottery = bool(upcoming and upcoming.get("hasProbability"))
        action = "签到+抽奖" if lottery else "签到"

        if DRY_RUN:
            result.update(action="dry-run", success=True,
                          message=(f"DRY_RUN：今日未签到，应执行第 {counter + 1} 签（{action}）"))
            if upcoming:
                result["prize"] = (f"待抽：{upcoming.get('name')}"
                                   f"（概率 {upcoming.get('probability')}）"
                                   if lottery else f"待得：{upcoming.get('name')}")
            return result

        history_before = self.mark_history()
        body = self.hub_post("/api/mark/do/mark", {"intact": True})
        code = str(body.get("code") or "").upper()
        msg = self._tip(code, body)

        if code == "TODAY_MARKED":
            result.update(action="skip", success=True, message=msg)
        else:
            after = self.prize_info()
            ok = self._ok(body) or bool(after.get("todayMarked")) or code == "PRIZE_NO_STOCK"
            result.update(action=action, success=ok, message=msg)
            result["marked_after"] = f"{after.get('markedTimes')}/{after.get('totalMarkTimes')}"
            result["full_marked"] = after.get("fullMarkedStatus")
            result["prize"] = self._latest_prize_after(history_before)
            if not result["prize"] and upcoming:
                result["prize"] = (f"{upcoming.get('name')}（概率 {upcoming.get('probability')}）"
                                   if lottery else f"{upcoming.get('name')}")
            return result

        result["marked_after"] = f"{marked}/{total}"
        result["prize"] = self._today_prize()
        return result

    def _mark_counter(self, info: Dict[str, Any]) -> int:
        """本次签到对应的 prizes 下标（= 已签次数，按当前渠道计）。"""
        channel = self.channel_of()
        raw = info.get("appMarkedTimes") if channel == "app" else info.get("markedTimes")
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _tip(code: str, body: Dict[str, Any]) -> str:
        known = {
            "SUCCESS": "签到成功",
            "TODAY_MARKED": "今日已签到，无需重复",
            "PRIZE_NO_STOCK": "签到成功，但今日奖品已领光（抽奖未中）",
            "PRIZE_NO_CONFIG": "该账号/省份未配置签到奖品",
            "FAILED": "签到失败",
        }
        if code in known:
            extra = str(body.get("msg") or "")
            if code == "FAILED" and extra:
                return f"{known[code]}：{preview(extra, 160)}"
            return known[code]
        return str(body.get("msg") or body.get("code") or "未知返回")

    def _today_prize(self) -> str:
        """仅当奖品是今天拿到的才展示，避免把历史奖品当成今日战果。"""
        try:
            history = self.mark_history()
        except Exception:                                  # noqa: BLE001
            return "-"
        today = datetime.now().strftime("%Y-%m-%d")
        for item in history:
            if str(item.get("winAt") or "").startswith(today):
                return f"{item.get('prizeName')}（{item.get('winAt')}）"
        return "-"

    def _latest_prize_after(self, history_before: List[Dict[str, Any]]) -> str:
        """用签到记录对比找出本次新到手的奖品。"""
        try:
            history_after = self.mark_history()
        except Exception:                                  # noqa: BLE001
            return ""
        old_keys = {str(x.get("uuid") or "") for x in history_before}
        new_items = [x for x in history_after if str(x.get("uuid") or "") not in old_keys]
        if new_items:
            item = new_items[0]
            return f"{item.get('prizeName')}（{item.get('winAt')}）"
        return ""

    def claim_tasks(self) -> List[str]:
        """自动领取已达成的任务奖励（只领已完成的，不伪造任务行为）。"""
        if not ENABLE_TASK:
            return []
        notes: List[str] = []
        try:
            tasks = self.task_list()
        except Exception as exc:                           # noqa: BLE001
            return [f"任务列表获取失败：{preview(exc, 100)}"]

        if DEBUG:
            print(f"    · [H5] 任务 {len(tasks)} 个："
                  + preview([{t.get('taskId'): t.get('taskName'), 'status': t.get('status')}
                             for t in tasks], 500))

        for task in tasks:
            task_id = task.get("taskId")
            task_type = task.get("taskType")
            status = task.get("status")
            second_status = task.get("secondStatus")
            award_type = task.get("awardType")
            name = str(task.get("taskName") or task_id)

            # 可选：对白名单任务先尝试上报完成
            if FINISH_TASK_IDS and str(task_id) in FINISH_TASK_IDS:
                try:
                    body = self.hub_post("/api/mark/task/finishTask",
                                         {"taskId": task_id,
                                          "taskType": task.get("completeTaskId") or task_type,
                                          "intact": True})
                    notes.append(f"上报完成 {name}：{body.get('msg') or body.get('code')}")
                except Exception as exc:                   # noqa: BLE001
                    notes.append(f"上报完成 {name} 失败：{preview(exc, 80)}")

            if status != 2:
                continue                                    # 未完成，跳过
            if DRY_RUN:
                notes.append(f"可领奖励（DRY_RUN 跳过）：{name}")
                continue
            try:
                if award_type == 2 and second_status == 0:
                    body = self.hub_post("/api/mark/task/getTaskSecondAward",
                                         {"taskId": task_id, "taskType": task_type, "intact": True})
                else:
                    body = self.hub_post("/api/mark/task/getTaskAward",
                                         {"taskId": task_id, "taskType": task_type, "intact": True})
                notes.append(f"领奖 {name}：{body.get('msg') or body.get('code')}")
            except Exception as exc:                       # noqa: BLE001
                notes.append(f"领奖 {name} 失败：{preview(exc, 80)}")
        return notes


# --------------------------------------------------------------------------- #
# 单账号执行
# --------------------------------------------------------------------------- #
def run_account(index: int, total: int, account: AccountTarget) -> Dict[str, Any]:
    item: Dict[str, Any] = {"label": account.label, "user": "-", "success": False,
                            "skipped": False, "sign": "-", "prize": "-", "error": "",
                            "tasks": [], "resources": {}}
    print(f"\n▶️ [{index}/{total}] {account.label}  →  {account.server}")
    client = Yd10086Client(account)
    try:
        client.login()
    except NotBoundError:
        item["skipped"] = True
        item["error"] = ("未绑定中国移动手机号 —— 请先在微信里打开「中国移动10086+」小程序，"
                         "用该手机号完成登录授权后再跑")
        print(f"   ⏭️ {item['error']}")
        return item
    except Exception as exc:                               # noqa: BLE001
        item["error"] = f"登录失败：{preview(exc, 220)}"
        print(f"   ❌ {item['error']}")
        return item

    item["user"] = mask_phone(client.phone)
    account.label_suffix = mask_phone(client.phone)
    print(f"   ✅ 登录成功　手机号 {mask_phone(client.phone)}　归属 {client.province or '-'}")

    resources = client.query_resources()
    item["resources"] = resources
    print(f"   📶 剩余流量 {resources['flow_left']}（已用 {resources['flow_used']}/{resources['flow_total']}）"
          f"　📞 剩余通话 {resources['voice_left']}"
          f"　💰 余额 {resources['balance']}")
    for name, left, total_ in resources.get("flow_breakdown") or []:
        print(f"      · {name}：剩 {left} / 共 {total_}")

    try:
        sign = client.do_sign()
    except Exception as exc:                               # noqa: BLE001
        sign = {"action": "-", "success": False, "message": f"签到异常：{preview(exc, 200)}",
                "prize": "-"}
    item["sign"] = f"{sign.get('action')}｜{sign.get('message')}"
    item["prize"] = sign.get("prize") or "-"
    item["success"] = bool(sign.get("success"))
    icon = "✅" if item["success"] else "❌"
    print(f"   {icon} 签到：{sign.get('action')}　{sign.get('message')}")
    if sign.get("prize") not in ("-", "", None):
        print(f"   🎁 奖品：{sign['prize']}")
    if sign.get("marked_after"):
        print(f"   📅 本期签到：{sign['marked_after']}")

    try:
        item["tasks"] = client.claim_tasks()
        for note in item["tasks"]:
            print(f"   🧩 {note}")
    except Exception as exc:                               # noqa: BLE001
        print(f"   ⚠️ 任务处理异常：{preview(exc, 140)}")

    # 签到后刷新一次余额/流量，让通知里的数据是当次最新的
    try:
        refreshed = client.query_resources()
        item["resources"] = refreshed
    except Exception:                                      # noqa: BLE001
        pass
    return item


# --------------------------------------------------------------------------- #
# 报表 / 通知
# --------------------------------------------------------------------------- #
def build_report(results: List[Dict[str, Any]]) -> str:
    lines = [f"🕒 {now_text()}",
             f"🧪 模式：{'DRY_RUN（只查询）' if DRY_RUN else '正常签到'}",
             ""]
    for item in results:
        if item.get("skipped"):
            lines.append(f"⏭️ {item['label']}　跳过（未绑定中国移动手机号）")
            lines.append(f"   ⚠️ {item.get('error', '')}")
            lines.append("")
            continue

        ok = item.get("success")
        lines.append(f"{'✅' if ok else '❌'} {item['label']}　{item.get('user', '-')}")
        res = item.get("resources") or {}
        lines.append(f"   📶 剩余流量：{res.get('flow_left', '-')}"
                     f"（已用 {res.get('flow_used', '-')}/{res.get('flow_total', '-')}）")
        for name, left, total_ in res.get("flow_breakdown") or []:
            lines.append(f"      · {name}：剩 {left} / 共 {total_}")
        lines.append(f"   📞 剩余通话：{res.get('voice_left', '-')}"
                     f"（共 {res.get('voice_total', '-')}）")
        lines.append(f"   💰 话费余额：{res.get('balance', '-')}"
                     f"（本月已用 {res.get('month_fee', '-')}）")
        lines.append(f"   🪙 签到专属AI豆：{res.get('points', '-')}")
        lines.append(f"   ✍️ 签到：{item.get('sign', '-')}")
        if item.get("prize") not in ("-", "", None):
            lines.append(f"   🎁 奖品：{item['prize']}")
        for note in item.get("tasks") or []:
            lines.append(f"   🧩 {note}")
        if item.get("error"):
            lines.append(f"   ⚠️ {item['error']}")
        lines.append("")

    ok_count = sum(1 for i in results if i.get("success"))
    skip_count = sum(1 for i in results if i.get("skipped"))
    fail_count = len(results) - ok_count - skip_count
    summary = f"🏁 汇总：{ok_count} 成功"
    if skip_count:
        summary += f" / {skip_count} 跳过（未绑定）"
    summary += f" / {fail_count} 失败（共 {len(results)} 个账号）"
    lines.append(summary)
    return "\n".join(lines)


def load_notify():
    """兼容青龙各版本的 notify.py 位置（脚本目录 / 根目录）。"""
    candidates = [
        Path(HERE) / "notify.py",
        Path("/ql/data/scripts/notify.py"),
        Path("/ql/scripts/notify.py"),
        Path("/ql/data/notify.py"),
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("yd_qinglong_notify", path)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            for name in ("send", "sendNotify"):
                func = getattr(module, name, None)
                if callable(func):
                    return func
        except Exception as exc:                           # noqa: BLE001
            print(f"⚠️ [通知] 加载 {path} 失败：{preview(exc, 120)}")
    return None


def send_notify(title: str, content: str) -> None:
    sender = load_notify()
    if sender is not None:
        try:
            sender(title, content)
            print("✅ [通知] 已通过青龙通知模块发送")
            return
        except Exception as exc:                           # noqa: BLE001
            print(f"⚠️ [通知] 青龙通知发送失败（不影响签到结果）：{preview(exc, 120)}")
    else:
        print("⚠️ [通知] 未找到青龙 notify.py，尝试兜底通道")

    sent = False
    plusplus = os.getenv("PLUSPLUS_TOKEN", "").strip()
    if plusplus:
        try:
            requests.post("https://www.pushplus.plus/send",
                          json={"token": plusplus, "title": title, "content": content,
                                "template": "txt"}, timeout=15)
            print("✅ [通知] PushPlus 发送成功")
            sent = True
        except Exception as exc:                           # noqa: BLE001
            print(f"❌ [通知] PushPlus 发送失败：{exc}")

    server_push = os.getenv("PUSH_KEY", "").strip() or os.getenv("SERVERPUSHKEY", "").strip()
    if server_push and not sent:
        try:
            requests.post(f"https://sctapi.ftqq.com/{server_push}.send",
                          data={"title": title, "desp": content}, timeout=15)
            print("✅ [通知] Server 酱发送成功")
            sent = True
        except Exception as exc:                           # noqa: BLE001
            print(f"❌ [通知] Server 酱发送失败：{exc}")

    qywx = os.getenv("QYWX_KEY", "").strip() or os.getenv("QYWX_TOKEN", "").strip()
    if qywx and not sent:
        try:
            requests.post(f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={qywx}",
                          json={"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}},
                          timeout=15)
            print("✅ [通知] 企业微信机器人发送成功")
            sent = True
        except Exception as exc:                           # noqa: BLE001
            print(f"❌ [通知] 企业微信机器人发送失败：{exc}")

    bark = os.getenv("BARK_PUSH", "").strip()
    if bark and not sent:
        try:
            requests.post(bark.rstrip("/"), json={"title": title, "body": content}, timeout=15)
            print("✅ [通知] Bark 发送成功")
            sent = True
        except Exception as exc:                           # noqa: BLE001
            print(f"❌ [通知] Bark 发送失败：{exc}")

    if not sent:
        print("ℹ️ [通知] 未配置任何可用推送通道，结果仅输出到日志")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    banner([
        f"📱 {APP_NAME}",
        f"🕒 启动时间: {now_text()}",
        f"🆔 小程序  : {APP_NO}",
        f"🎯 活动 ID : {ACTIVITY_ID}",
        f"🧪 DRY_RUN : {'开启（只查询）' if DRY_RUN else '关闭'}",
        f"🧩 任务领奖: {'开启' if ENABLE_TASK else '关闭'}"
        + (f"（上报白名单 {','.join(FINISH_TASK_IDS)}）" if FINISH_TASK_IDS else ""),
        f"🔐 登录    : YYB 自动取码 → 小程序 login → wmhsso → H5（无需抓包）",
    ])

    try:
        accounts = load_accounts()
    except Exception as exc:                               # noqa: BLE001
        print(f"❌ {exc}")
        return 1

    results: List[Dict[str, Any]] = []
    for idx, account in enumerate(accounts, 1):
        results.append(run_account(idx, len(accounts), account))
        if idx < len(accounts):
            time.sleep(random.uniform(3, 6))

    report = build_report(results)
    print("\n" + "=" * 62)
    print(report)
    print("=" * 62)

    ok_count = sum(1 for i in results if i.get("success"))
    skip_count = sum(1 for i in results if i.get("skipped"))
    title = f"【{APP_NAME}】{ok_count}/{len(results)} 成功"
    if skip_count:
        title += f"，{skip_count} 个未绑定跳过"
    send_notify(title, report)
    # 未绑定属于「无需处理」，不算失败；只要没有真正的接口失败就返回 0
    return 0 if ok_count + skip_count == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⏹️ 已手动中断")
        sys.exit(130)
