/*
name: WPS签到
cron: 33 8 * * *

来源改编：ZzzHe2333/Single_jiaoben_qinglong lib/wps.inc（上游 smallfawn/QLScriptPublic wxapp/wps.js）
本文件：从聚合仓库抽出的 WPS 独立脚本 + YYB 取码接入 + 标准代理模块

青龙环境变量：
  YYB_SERVER                 必填，账号列表 = YYB 取码服务；每行 地址@账号ID或OpenID
                             支持 & 分隔、# 整行注释、自动去重、省略 http:// 自动补
                             openid 由脚本向 YYB 自动获取并缓存，不需要手填
  YYB_API_KEY                YYB 协议令牌；服务端配了 YYB_PROTOCOL_TOKEN 时必填（也认 YYB_PROTOCOL_TOKEN）
  WPS_LOTTERY_LIMIT          可选，每次最多抽奖次数，默认 5
  WPS_DRY_RUN                1=预演模式，完全不发网络请求
  WPS_FAST_WAIT              1=调试用，所有等待压缩到 10ms

标准代理（业务请求走代理；YYB 取码与通知推送一律直连；代理行只进控制台日志，不进通知）：
  WPS_PROXY_API              本脚本代理提取 API，留空回退共用 PROXY_API，都没有则全程直连
  PROXY_TYPE                 http（默认）/ socks5（socks5 需青龙装有 socks-proxy-agent）
  PROXY_RETRY                单个代理最大重试次数，默认 3
  PROXY_REFRESH              重拉代理次数，默认 3
  PROXY_INTERVAL             每账号切换代理前等待秒数，默认 3
  PROXY_VALIDATE_URL         代理验证地址，默认 http://httpbin.org/ip（国内不通时改成可达地址）
  PROXY_FALLBACK              1=代理全败降级直连，0=不降级直接报错，默认 1
  PROXY_TIMEOUT              代理请求超时秒数，默认 10

YYB_SERVER 每行支持（推荐第 1 种，openid 由脚本从 YYB 侧自动取）：
  http://yyb-go:8000@1              地址@YYB账号ID（省略 http:// 会自动补）；也支持 地址@openid
  http://yyb-go:8000@1#cookie       同上，附加 cookie/secret（仍可用 | 分隔三段）
  openid                            仅给 openid，需配 YYB_SERVER 做映射
  openid#cookie
  openid|cookie
  openid|cookie|secret
  {"openid":"","cookie":"","secret":""}
  纯 cookie 字符串（含 wps_sid=/kso_sid=）
*/

const axios = require("axios");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const TITLE = "WPS";
const ENV_NAME = "YYB_SERVER";          // 账号列表 = YYB 服务地址@账号（唯一账号变量）
const LOTTERY_LIMIT = Math.max(0, Number(process.env.WPS_LOTTERY_LIMIT || 5));
const DRY_RUN = String(process.env.WPS_DRY_RUN || "") === "1";
const FAST_WAIT = String(process.env.WPS_FAST_WAIT || "") === "1";

const MINI_APP_ID = "wx2f333d84a103825d";
const ACCOUNT_PLUGIN_APPID = "wxe5f87d6a233b5aab";
const WPS_MPID = "app_op_act";
const CLIENT_TYPE = 1;
const TOKEN_CACHE_FILE = process.env.WPS_TOKEN_CACHE_FILE || path.join(__dirname, "single_wps_token_cache.json");

// 业务端点集中在一处（getter 派生），测试可整体指向 mock
const API = {
  ACCOUNT_BASE: "https://account.wps.cn",
  PERSONAL_BUS: "https://personal-bus.wps.cn",
  PERSONAL_ACT: "https://personal-act.wps.cn",
  ACTIVITY_CONFIG: "https://personal-act.wpscdn.cn/srcapi/act/rubik-service/honeycomb-adapter/client/module-info?pid=113&mg_id=47736&id=48312",
  LOTTERY_PAGE: "https://personal-act.wps.cn/rubik2/portal/HD2024082815116866/YM2024082815122017",
  get CLOCK_INFO() { return `${this.PERSONAL_BUS}/activity/clock_in/v1/info`; },
  get CLOCK_IN() { return `${this.PERSONAL_BUS}/activity/clock_in/v1/clock_in`; },
  get TASK_OUTLINE() { return `${this.PERSONAL_BUS}/activity/clock_in/v1/task/outline`; },
  get TASK_START_BROWSE() { return `${this.PERSONAL_BUS}/activity/clock_in/v1/task/start_browse`; },
  get TASK_FINISH_BROWSE() { return `${this.PERSONAL_BUS}/activity/clock_in/v1/task/finish_browse`; },
  get LOTTERY_TIMES() { return `${this.PERSONAL_BUS}/activity/clock_in/v1/task/lottery_times`; },
  get COMPONENT_ACTION() { return `${this.PERSONAL_ACT}/activity-rubik/activity/component_action`; },
};

const logs = [];
function log(message = "") {
  const text = String(message);
  logs.push(text);
  console.log(text);
}
// 仅控制台可见，不进推送通知（代理行、YYB 内部状态用）
function echo(message = "") {
  console.log(String(message));
}
function wait(min, max = min) {
  const scale = FAST_WAIT ? 0.001 : 1;
  const ms = (Math.floor(Math.random() * (max - min + 1)) + min) * scale;
  return new Promise((resolve) => setTimeout(resolve, ms));
}
function mask(value = "") {
  value = String(value || "");
  if (!value) return "";
  if (value.length <= 12) return `${value.slice(0, 3)}***`;
  return `${value.slice(0, 6)}***${value.slice(-6)}`;
}
function intEnv(name, fallback) {
  const v = Number(process.env[name]);
  return Number.isFinite(v) && v >= 0 ? Math.floor(v) : fallback;
}
function md5(text) {
  return crypto.createHash("md5").update(String(text)).digest("hex");
}
function hmacSha256Hex(text, key) {
  return crypto.createHmac("sha256", key).update(String(text)).digest("hex");
}
function base64url(input) {
  return Buffer.from(input).toString("base64url");
}
function randomHex(bytes = 16) {
  return crypto.randomBytes(bytes).toString("hex");
}
function preview(body) {
  try { return JSON.stringify(body).slice(0, 300); } catch { return String(body).slice(0, 300); }
}

function readCache() {
  try {
    if (!fs.existsSync(TOKEN_CACHE_FILE)) return {};
    return JSON.parse(fs.readFileSync(TOKEN_CACHE_FILE, "utf8")) || {};
  } catch {
    return {};
  }
}
function writeCache(cache) {
  try {
    fs.writeFileSync(TOKEN_CACHE_FILE, JSON.stringify(cache, null, 2), "utf8");
  } catch (e) {
    log(`token缓存写入失败: ${e.message || e}`);
  }
}
function parseCookie(cookie = "") {
  if (!cookie || typeof cookie !== "string") return {};
  return Object.fromEntries(
    cookie.split(/;\s*/).filter((v) => v.includes("=")).map((v) => {
      const p = v.indexOf("=");
      return [v.slice(0, p).trim(), v.slice(p + 1).trim()];
    })
  );
}
function cookieHeader(cookies = {}) {
  return Object.entries(cookies)
    .filter(([, v]) => v !== undefined && v !== null && String(v) !== "")
    .map(([k, v]) => `${k}=${v}`)
    .join(";");
}
function mergeSetCookie(setCookie = [], current = {}) {
  const cookies = { ...current };
  for (const line of Array.isArray(setCookie) ? setCookie : [setCookie]) {
    const first = String(line || "").split(";")[0];
    if (!first.includes("=")) continue;
    const p = first.indexOf("=");
    const key = first.slice(0, p).trim();
    const value = first.slice(p + 1).trim();
    if (key) cookies[key] = value;
  }
  return cookies;
}

// 识别「地址@YYB账号」形态（yyb-go:8000@1 / http://yyb-go:8000@1 / 地址@openid）
function parseYybRef(text) {
  const value = String(text || "").trim();
  if (!value || !value.includes("@")) return null;
  const { endpoint, ref } = splitYybServer(value);
  if (!endpoint || !ref) return null;
  let base = endpoint;
  if (!/^https?:\/\//i.test(base)) base = "http://" + base;
  return { endpoint: base, ref };
}

function parseAccount(raw) {
  const text = String(raw || "").trim();
  const blank = { openid: "", cookie: "", secret: "", yyb: null };
  if (!text) return blank;
  if (text.startsWith("{")) {
    try {
      const data = JSON.parse(text);
      return {
        openid: data.openid || data.openId || data.account || "",
        cookie: data.cookie || data.Cookie || "",
        secret: data.secret || data.jsrsasign_secret || "",
        yyb: parseYybRef(data.yyb_server || data.server || data.yyb || ""),
      };
    } catch {}
  }
  // 先剥离 cookie/secret 附加段，剩下的头部用于判定账号形态
  let head = text;
  let cookie = "";
  let secret = "";
  for (const sep of ["#", "|"]) {
    if (text.includes(sep)) {
      const parts = text.split(sep);
      head = String(parts[0] || "").trim();
      cookie = String(parts[1] || "").trim();
      secret = String(parts[2] || "").trim();
      break;
    }
  }
  const yyb = parseYybRef(head);
  if (yyb) return { openid: "", cookie, secret, yyb };
  if (head.includes("wps_sid=") || head.includes("kso_sid=")) {
    return { openid: "", cookie: head, secret, yyb: null };
  }
  return { openid: head, cookie, secret, yyb: null };
}

function canonicalJson(data = {}) {
  const sorted = {};
  Object.keys(data || {}).sort().forEach((key) => { sorted[key] = data[key]; });
  return JSON.stringify(sorted);
}
function makeSignature(data, config = {}) {
  const key = config.key || "";
  const ss = config.ss || "";
  if (!key || !ss) return {};
  const date = new Date().toUTCString();
  const payload = `${key}${md5(canonicalJson(data))}${date}`;
  return { Date: date, Signature: hmacSha256Hex(payload, ss) };
}
function makePopToken(method, url, secret, cv = "") {
  if (!secret) return "";
  const header = base64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const body = {
    htm: String(method || "GET").toUpperCase(),
    htu: new URL(url).pathname,
    iat: Math.floor(Date.now() / 1000),
  };
  if (cv) body.cv = cv;
  const payload = base64url(JSON.stringify(body));
  const sig = crypto.createHmac("sha256", Buffer.from(secret)).update(`${header}.${payload}`).digest("base64url");
  return `${header}.${payload}.${sig}`;
}
function genEcKeyPair() {
  return crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
}
function signEc(privateKey, text) {
  return crypto.sign("sha256", Buffer.from(String(text)), {
    key: privateKey.export({ type: "pkcs8", format: "pem" }),
    dsaEncoding: "der",
  }).toString("base64url");
}
function deriveSecret(privateKey, serverJwkB64) {
  const jwk = JSON.parse(Buffer.from(serverJwkB64, "base64url").toString("utf8"));
  const publicKey = crypto.createPublicKey({ key: jwk, format: "jwk" });
  return crypto.diffieHellman({ privateKey, publicKey }).toString("base64url");
}

// ================================================================ YYB 取码模块
// 契约来自 525815266/YYB-Go-Enhanced 源码核实：
//   POST {endpoint}/wxapp/getCode  body {ref, app_id}
//   成功 {openid, account{...}, result:{code, errMsg}}；别名 /wx/code
//   错误码 400 缺参 / 401 令牌 / 404 路径或账号 / 405 必须 POST / 409 需重扫 / 502 上游
//   返回 HTML 基本=撞到控制台登录页或反代拦截

const YYB_SERVER_RAW = process.env.YYB_SERVER || "";
const YYB_API_KEY = String(process.env.YYB_API_KEY || process.env.YYB_PROTOCOL_TOKEN || "").trim();
const YYB_TIMEOUT = intEnv("YYB_TIMEOUT", 20) * 1000;
const YYB_UNSUPPORTED_TTL = Math.max(1, intEnv("WPS_YYB_UNSUPPORTED_TTL", 24 * 3600)) * 1000;
const YYB_MAP_TTL = 6 * 3600 * 1000;
const YYB_STATE_FILE = process.env.WPS_YYB_STATE_FILE || path.join(__dirname, "single_wps_yyb_state.json");

const YYB_STATUS_HINTS = {
  400: "请求参数不完整（缺 ref 或 app_id）",
  401: "协议令牌缺失或错误（检查 YYB_API_KEY 与服务端 YYB_PROTOCOL_TOKEN）",
  404: "接口路径/大小写错误，或账号不存在",
  405: "HTTP 方法错误，取码接口必须 POST",
  409: "账号登录态失效或会话冲突，需要重新扫码",
  502: "上游协议、网络或代理调用失败",
};
const CODE_PLACEHOLDERS = new Set(["", "null", "none", "undefined", "invalid"]);

class YybError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "YybError";
    this.status = status;
  }
}
// 码被拒（账号不存在/需重扫/未返回 code）——已按 ref 持久标记冷却
class CodeRejectedError extends YybError {
  constructor(message, status = 0) {
    super(message, status);
    this.name = "CodeRejectedError";
  }
}
// 冷却期内，直接跳过不浪费请求
class YybCoolingError extends YybError {
  constructor(message) {
    super(message, 0);
    this.name = "YybCoolingError";
  }
}

function splitYybServer(server) {
  const value = String(server || "").trim();
  const at = value.lastIndexOf("@");
  if (at === -1) return { endpoint: value.replace(/\/+$/, ""), ref: "" };
  return {
    endpoint: value.slice(0, at).trim().replace(/\/+$/, ""),
    ref: value.slice(at + 1).trim(),
  };
}

function parseYybAccounts(raw) {
  const out = [];
  const seen = new Set();
  for (const chunk of String(raw == null ? "" : raw).split(/[\n&]+/)) {
    const line = chunk.trim();
    if (!line || line.startsWith("#") || line === "[object Object]") continue;
    if (!line.includes("@")) {
      echo(`⚠️ 跳过无法解析的 YYB_SERVER 行（缺少@）：${line.slice(0, 60)}`);
      continue;
    }
    const { endpoint, ref } = splitYybServer(line);
    if (!endpoint || !ref) {
      echo(`⚠️ 跳过不完整的 YYB_SERVER 行：${line.slice(0, 60)}`);
      continue;
    }
    let base = endpoint;
    if (!/^https?:\/\//i.test(base)) base = "http://" + base;
    const rest = base.split("://")[1] || "";
    if (rest.includes("/")) {
      echo(`⚠️ YYB_SERVER 地址 ${base} 携带路径，面板同步格式要求只有 地址@账号；如不是反代子路径请去掉`);
    }
    const key = `${base}@${ref}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ endpoint: base, ref, raw: line });
  }
  return out;
}

function yybConfigured() {
  return parseYybAccounts(YYB_SERVER_RAW).length > 0;
}

// 从 YYB 响应提取 wx.login code：优先 wx_code/login_code，再递归嵌套，最后才认通用 code
function findCode(value) {
  if (Array.isArray(value)) {
    for (const child of value) {
      const found = findCode(child);
      if (found) return found;
    }
    return null;
  }
  if (value && typeof value === "object") {
    for (const key of ["wx_code", "login_code"]) {
      const candidate = value[key];
      if (typeof candidate === "string" && candidate && !CODE_PLACEHOLDERS.has(candidate)) return candidate;
    }
    for (const child of Object.values(value)) {
      const found = findCode(child);
      if (found) return found;
    }
    const candidate = value.code;
    if (typeof candidate === "string" && candidate && !CODE_PLACEHOLDERS.has(candidate)) return candidate;
  }
  return null;
}

function yybErrorMessage(status, payload) {
  const hint = YYB_STATUS_HINTS[status] || `HTTP ${status}`;
  let detail = "";
  if (payload && typeof payload === "object") {
    for (const key of ["message", "msg", "error"]) {
      const value = payload[key];
      if (typeof value === "string" && value.trim()) { detail = value.trim(); break; }
    }
    if (!detail && payload.raw) return `YYB ${status} ${hint}（非 JSON 响应：${String(payload.raw).slice(0, 120)}）`;
    if (!detail) detail = preview(payload);
  } else if (payload != null) {
    detail = String(payload);
  }
  return `YYB ${status} ${hint}：${detail.slice(0, 200)}`;
}

// YYB 是内网取码服务，按蓝本不吃业务代理；proxy:false 同时切断系统/环境代理
async function yybRequest(method, url, body = null) {
  const headers = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "User-Agent": "single-wps-yyb/1.0",
  };
  if (YYB_API_KEY) headers.Authorization = "Bearer " + YYB_API_KEY;
  let res;
  try {
    res = await axios({ method, url, data: body, headers, timeout: YYB_TIMEOUT, validateStatus: () => true, proxy: false });
  } catch (e) {
    throw new YybError(`YYB 连接失败：${url} → ${e.message || e}`);
  }
  // 非 JSON 包 dict 铁律：解析失败也返回对象，绝不外抛 JSONDecodeError
  let data = res.data;
  if (typeof data === "string") {
    const text = data.trim();
    if (!text) data = { message: "YYB 返回空响应", url };
    else {
      try { data = JSON.parse(text); }
      catch {
        data = {
          message: "YYB 返回非 JSON（可能是控制台登录页、统一登录或反代/BasicAuth 拦截）",
          url,
          raw: text.replace(/\s+/g, " ").slice(0, 200),
        };
      }
    }
  }
  if (res.status < 200 || res.status >= 300) throw new YybError(yybErrorMessage(res.status, data), res.status);
  return data;
}

function readYybState() {
  try { return JSON.parse(fs.readFileSync(YYB_STATE_FILE, "utf8")) || {}; }
  catch { return {}; }
}
function writeYybState(state) {
  try { fs.writeFileSync(YYB_STATE_FILE, JSON.stringify(state, null, 2), "utf8"); }
  catch (e) { echo(`YYB 状态缓存写入失败: ${e.message || e}`); }
}
function yybMarkUnsupported(ref, reason) {
  const state = readYybState();
  state.cooling = state.cooling || {};
  state.cooling[ref] = {
    until: Date.now() + YYB_UNSUPPORTED_TTL,
    reason: String(reason).slice(0, 200),
    at: new Date().toISOString(),
  };
  writeYybState(state);
  echo(`YYB 账号 ${mask(ref)} 已标记不可用 ${Math.round(YYB_UNSUPPORTED_TTL / 3600000)}h：${String(reason).slice(0, 120)}`);
}

// openid → YYB ref 解析：精确匹配 → 唯一账号直接用 → 多账号查 /wx/getuserinfo 反查（带缓存）
async function resolveYybAccount(openid, accounts) {
  const exact = accounts.find((a) => a.ref === openid);
  if (exact) return exact;
  if (accounts.length === 1) {
    echo(`YYB 仅一个账号配置，按 ref=${mask(accounts[0].ref)} 使用（未与 openid 精确匹配，请确认是同一微信）`);
    return accounts[0];
  }
  const state = readYybState();
  const now = Date.now();
  let map = state.openidMap;
  if (!map || !map.updatedAt || now - map.updatedAt > YYB_MAP_TTL) {
    map = { updatedAt: now, entries: {} };
    for (const account of accounts) {
      try {
        const info = await yybRequest("GET", `${account.endpoint}/wx/getuserinfo?ref=${encodeURIComponent(account.ref)}`);
        const oid = (info && (info.openid || info.result?.openid || info.data?.openid)) || "";
        if (oid) map.entries[oid] = account.ref;
      } catch (e) {
        echo(`YYB 账号 ${mask(account.ref)} 资料查询失败：${e.message || e}`);
      }
    }
    state.openidMap = map;
    writeYybState(state);
  }
  const ref = map.entries[openid];
  if (ref) {
    const found = accounts.find((a) => a.ref === ref);
    if (found) return found;
  }
  return null;
}

// 给定已确定的 YYB 账号（endpoint+ref）直接取码；冷却检查、标记、报错都在这里
async function yybFetchCode(account, appid) {
  if (!appid) throw new YybError("缺少 app_id（目标小程序 AppID）");
  const state = readYybState();
  const cool = state.cooling && state.cooling[account.ref];
  if (cool && cool.until > Date.now()) {
    const minutes = Math.ceil((cool.until - Date.now()) / 60000);
    throw new YybCoolingError(`YYB 账号 ${mask(account.ref)} 冷却中（${cool.reason}），剩余约 ${minutes} 分钟`);
  }

  const url = `${account.endpoint}/wxapp/getCode`;
  let data;
  try {
    data = await yybRequest("POST", url, { ref: account.ref, app_id: appid });
  } catch (e) {
    // 账号不存在/需重扫才标记冷却；令牌、上游、网络故障不标记（下次还会试）
    if (e instanceof YybError && (e.status === 404 || e.status === 409)) {
      yybMarkUnsupported(account.ref, e.message);
      throw new CodeRejectedError(`${e.message}（已标记 ${Math.round(YYB_UNSUPPORTED_TTL / 3600000)}h 冷却）`, e.status);
    }
    throw e;
  }
  const code = findCode(data);
  if (!code) {
    yybMarkUnsupported(account.ref, "YYB 未返回 wx.login code");
    throw new CodeRejectedError(`YYB 未返回 wx.login code：${preview(data)}`);
  }
  return code;
}

// 只有 openid 时：靠 YYB_SERVER 做 ref 映射再取码
async function yybGetCode(openid, appid) {
  if (!openid) throw new YybError("缺少 openid，无法定位 YYB 账号");
  const accounts = parseYybAccounts(YYB_SERVER_RAW);
  if (!accounts.length) throw new YybError("未配置 YYB_SERVER（每行 地址@账号ID或OpenID）");

  const account = await resolveYybAccount(openid, accounts);
  if (!account) {
    throw new YybError(`openid ${mask(openid)} 在 YYB 账号中找不到匹配项（已配置 ${accounts.length} 个：${accounts.map((a) => mask(a.ref)).join(", ")}）`);
  }
  return yybFetchCode(account, appid);
}

// 从 YYB 读取该账号绑定微信的 openid
async function yybOpenid(account) {
  const info = await yybRequest("GET", `${account.endpoint}/wx/getuserinfo?ref=${encodeURIComponent(account.ref)}`);
  return String((info && (info.openid || info.result?.openid || info.data?.openid)) || "");
}

// ================================================================ 标准代理模块
// 蓝本（青龙单文件约定）的 axios 适配：
//   {前缀}_PROXY_API 优先 → 共用 PROXY_API → 都没有则全程直连
//   三级失败：当前代理重试 → 重拉新代理 → 降级直连（PROXY_FALLBACK=1）
//   代理行只进控制台日志（echo），不进推送通知

const PROXY_API = String(process.env.WPS_PROXY_API || process.env.PROXY_API || "").trim();
const PROXY_TYPE = String(process.env.PROXY_TYPE || "http").trim().toLowerCase();
const PROXY_RETRY = intEnv("PROXY_RETRY", 3);
const PROXY_REFRESH = intEnv("PROXY_REFRESH", 3);
const PROXY_INTERVAL = intEnv("PROXY_INTERVAL", 3);
const PROXY_VALIDATE_URL = String(process.env.PROXY_VALIDATE_URL || "http://httpbin.org/ip").trim();
const PROXY_FALLBACK = String(process.env.PROXY_FALLBACK || "1") !== "0";
const PROXY_TIMEOUT = Math.max(1, intEnv("PROXY_TIMEOUT", 10)) * 1000;

const proxyState = { current: null, degraded: false };

function proxyEnabled() {
  return !!PROXY_API;
}
function proxyLabel(p) {
  const raw = String((p && p.url) || "");
  if (!raw) return "无";
  return raw.replace(/\/\/([^/@/]+)@/, "//***:***@");
}

// 代理故障判定：拿到了 HTTP 响应=业务结果不算故障；无响应+网络类错误才算
const PROXY_FAULT_CODES = new Set([
  "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "ECONNABORTED", "EPIPE", "EPROTO",
  "ENOTFOUND", "EAI_AGAIN", "ECONNCLOSED", "ERR_PROXY", "ENETUNREACH", "EHOSTUNREACH",
]);
function isProxyFault(err) {
  if (!err) return false;
  if (err.response) return false;
  if (err.code && PROXY_FAULT_CODES.has(err.code)) return true;
  const msg = String(err.message || "");
  return /timeout|timed out|socket hang up|proxy|tunnel|connect/i.test(msg);
}

async function pullProxy() {
  try {
    const res = await axios.get(PROXY_API, { timeout: PROXY_TIMEOUT, validateStatus: () => true, proxy: false });
    let value = "";
    if (typeof res.data === "string") {
      value = res.data.split(/\r?\n/).map((s) => s.trim()).find(Boolean) || "";
    } else if (res.data && typeof res.data === "object") {
      const j = res.data;
      value = j.proxy || j.url || (j.data && (j.data.proxy || j.data.url)) || (j.ip ? `${j.ip}:${j.port || ""}` : "") || "";
    }
    value = String(value || "").trim();
    if (!value) return null;
    if (!/^[a-z0-9]+:\/\//i.test(value)) value = `${PROXY_TYPE}://${value}`;
    return { url: value, label: proxyLabel({ url: value }), pulledAt: Date.now() };
  } catch (e) {
    echo(`代理提取 API 请求失败：${e.message || e}`);
    return null;
  }
}

function buildProxyConfig(p) {
  let u;
  try { u = new URL(p.url); }
  catch { throw new Error(`代理地址无法解析：${p.label}`); }
  const proto = u.protocol.replace(":", "").toLowerCase();
  if (proto.startsWith("socks")) {
    let SocksProxyAgent = null;
    try { SocksProxyAgent = require("socks-proxy-agent"); } catch {}
    if (!SocksProxyAgent) throw new Error(`socks 代理需要 socks-proxy-agent 依赖（青龙未预装，请改用 http 代理或先安装）：${p.label}`);
    const agent = new SocksProxyAgent(p.url);
    return { proxy: false, httpAgent: agent, httpsAgent: agent };
  }
  const cfg = {
    proxy: {
      protocol: proto || "http",
      host: u.hostname,
      port: Number(u.port || (proto === "https" ? 443 : 80)),
    },
  };
  if (u.username) {
    cfg.proxy.auth = { username: decodeURIComponent(u.username), password: decodeURIComponent(u.password || "") };
  }
  return cfg;
}

async function validateProxy(p) {
  try {
    const cfg = buildProxyConfig(p);
    const res = await axios.get(PROXY_VALIDATE_URL, { ...cfg, timeout: PROXY_TIMEOUT, validateStatus: () => true });
    return res.status >= 200 && res.status < 300;
  } catch {
    return false;
  }
}

async function pullValidProxy() {
  const p = await pullProxy();
  if (!p) return null;
  if (await validateProxy(p)) return p;
  echo(`代理验证失败：${p.label}`);
  return null;
}

// 每账号专属代理：开新账号前重置降级标记并预拉一个
async function acquireAccountProxy() {
  proxyState.degraded = false;
  const p = await pullValidProxy();
  proxyState.current = p;
  if (p) echo(`账号代理就绪：${p.label}`);
  else echo("账号代理拉取失败，将在请求时重试，仍失败则降级直连");
}

async function directRequest(method, url, options = {}) {
  return axios({ method, url, ...options, proxy: false });
}

async function proxyRequest(method, url, options, p) {
  const cfg = buildProxyConfig(p);
  return axios({ method, url, ...options, ...cfg, timeout: PROXY_TIMEOUT });
}

// 业务请求统一入口：三级失败处理
async function fetchBusiness(method, url, options = {}) {
  if (!proxyEnabled() || proxyState.degraded) return directRequest(method, url, options);
  let lastErr = null;
  for (let refresh = 1; refresh <= PROXY_REFRESH; refresh++) {
    let p = proxyState.current;
    if (!p) {
      p = await pullValidProxy();
      proxyState.current = p;
      if (p) echo(`代理就绪：${p.label}`);
    }
    if (!p) {
      echo(`拉取代理失败（${refresh}/${PROXY_REFRESH}）`);
      continue;
    }
    for (let attempt = 1; attempt <= PROXY_RETRY; attempt++) {
      try { return await proxyRequest(method, url, options, p); }
      catch (e) {
        if (!isProxyFault(e)) throw e;
        lastErr = e;
        echo(`代理请求失败（${attempt}/${PROXY_RETRY}）：${e.message || e}`);
      }
    }
    echo(`代理 ${p.label} 连续 ${PROXY_RETRY} 次失败，重拉新代理（${refresh}/${PROXY_REFRESH}）`);
    proxyState.current = null;
  }
  if (PROXY_FALLBACK) {
    proxyState.degraded = true;
    proxyState.current = null;
    echo("⚠️ 代理全部失败，本次降级为直连（后续请求不再走代理）");
    return directRequest(method, url, options);
  }
  throw new Error(`代理请求失败且未开启降级（PROXY_FALLBACK=0）：${(lastErr && lastErr.message) || "未知错误"}`);
}

// 取码总入口：YYB_SERVER 每行写成 地址@账号 时直连该 YYB 节点；
// 没有 @ 的行（纯 openid）则靠 YYB_SERVER 里的账号做 ref 映射
async function acquireCode(openid, appid, yybAccount = null) {
  if (yybAccount) return yybFetchCode(yybAccount, appid);
  if (yybConfigured()) return yybGetCode(openid, appid);
  throw new Error("缺少 YYB 账号：请把 YYB_SERVER 写成 地址@账号ID（推荐），纯 openid 行需同时配 YYB_SERVER 做映射");
}

// ================================================================ 业务请求层

async function accountRequest(method, urlPath, { data = null, cookies = {}, secret = "" } = {}) {
  const url = `${API.ACCOUNT_BASE}${urlPath}`;
  const headers = {
    platform: "wx_mini",
    app_name: "account_wx_mini_plugin",
    Cookie: `csrf=1234567890${cookieHeader(cookies) ? `;${cookieHeader(cookies)}` : ""}`,
    "X-CSRFToken": "1234567890",
    "User-Agent": "Mozilla/5.0 MicroMessenger MiniProgramEnv/Windows",
    Referer: `https://servicewechat.com/${MINI_APP_ID}/249/page-frame.html`,
  };
  const pop = makePopToken(method, urlPath, secret, cookies.cv || "");
  if (pop) headers["X-Pop-Token"] = pop;
  const res = await fetchBusiness(method, url, { data, headers, timeout: 20000, validateStatus: () => true });
  return { data: res.data, cookies: mergeSetCookie(res.headers["set-cookie"], cookies), status: res.status };
}

async function wpsRequest(method, url, options = {}) {
  const {
    data = null, params = null, cookies = {}, secret = "", signed = false,
    clockConfig = {}, headers: extraHeaders = {},
  } = options;
  const csrf = "1234567890";
  const headers = {
    "X-CSRFToken": csrf,
    Cookie: `${cookieHeader(cookies)};csrf=${csrf}`,
    "User-Agent": "Mozilla/5.0 MicroMessenger MiniProgramEnv/Windows",
    Referer: `https://servicewechat.com/${MINI_APP_ID}/249/page-frame.html`,
    ...extraHeaders,
  };
  const pop = makePopToken(method, url, secret, cookies.cv || "");
  if (pop) headers["X-Pop-Token"] = pop;
  if (signed) Object.assign(headers, makeSignature(data || {}, clockConfig));
  const res = await fetchBusiness(method, url, { data, params, headers, timeout: 20000, validateStatus: () => true });
  return { data: res.data, cookies: mergeSetCookie(res.headers["set-cookie"], cookies), status: res.status };
}
function assertOk(res, action) {
  if (!res || res.result !== "ok") {
    throw new Error(`${action}失败: ${res?.msg || res?.result || JSON.stringify(res)}`);
  }
  return res.data ?? res;
}

class Task {
  constructor(raw, index) {
    this.index = index;
    const account = parseAccount(raw);
    this.openid = account.openid;
    this.yyb = account.yyb || null; // YYB_SERVER 写成 地址@账号 时的 YYB 账号
    this.cookies = parseCookie(account.cookie);
    this.secret = account.secret || "";
    this.uid = this.cookies.uid || "";
    this.clockConfig = {};
    this.cacheKey =
      this.openid
      || (this.yyb ? `yyb:${this.yyb.ref}@${md5(this.yyb.endpoint).slice(0, 8)}` : "")
      || this.cookies.uid
      || (account.cookie ? md5(account.cookie).slice(0, 16) : "")
      || `account_${index}`;
  }
  // openid 懒解析：优先缓存，其次向 YYB 的 /wx/getuserinfo 取（失败不阻断，按 ref 继续）
  async ensureOpenid() {
    if (this.openid) return this.openid;
    const cached = this.getCached().openid;
    if (cached) { this.openid = cached; return this.openid; }
    if (!this.yyb) return "";
    try {
      const oid = await yybOpenid(this.yyb);
      if (oid) {
        this.openid = oid;
        echo(`账号[${this.index}] 已从 YYB 取得 openid: ${mask(oid)}`);
        return oid;
      }
      echo(`账号[${this.index}] ⚠️ YYB 账号 ${mask(this.yyb.ref)} 未返回 openid（可能未绑定微信或登录态失效），将按 ref 继续取码`);
    } catch (e) {
      echo(`账号[${this.index}] ⚠️ 从 YYB 读取 openid 失败：${e.message || e}（将按 ref 继续取码）`);
    }
    return "";
  }
  getCached() { return readCache()[this.cacheKey] || {}; }
  saveCache(extra = {}) {
    const cache = readCache();
    cache[this.cacheKey] = {
      ...(cache[this.cacheKey] || {}),
      openid: this.openid || this.getCached().openid || "",
      uid: this.uid || this.cookies.uid || "",
      cookie: cookieHeader(this.cookies),
      secret: this.secret,
      ...extra,
      updatedAt: new Date().toISOString(),
    };
    writeCache(cache);
  }
  removeLogin() {
    const cache = readCache();
    if (cache[this.cacheKey]) {
      delete cache[this.cacheKey].cookie;
      delete cache[this.cacheKey].secret;
      writeCache(cache);
    }
    this.cookies = {};
    this.secret = "";
  }
  loadCache() {
    const cache = this.getCached();
    if (!this.openid && cache.openid) this.openid = cache.openid;
    if (!this.secret && cache.secret) this.secret = cache.secret;
    if (!Object.keys(this.cookies).length && cache.cookie) this.cookies = parseCookie(cache.cookie);
    this.uid = this.cookies.uid || cache.uid || this.uid || "";
  }
  async login() {
    await this.ensureOpenid();
    if (!this.openid && !this.yyb) {
      throw new Error("缺少账号标识，无法自动登录；请把 YYB_SERVER 写成 地址@YYB账号");
    }
    const authCode = await acquireCode(this.openid, ACCOUNT_PLUGIN_APPID, this.yyb);
    const keyPair = genEcKeyPair();
    const body = {
      type: "wxapp",
      appid: ACCOUNT_PLUGIN_APPID,
      auth_code: authCode,
      public_key: base64url(JSON.stringify(keyPair.publicKey.export({ format: "jwk" }))),
      auth_code_sign: signEc(keyPair.privateKey, authCode),
      slv: "none",
    };
    const res = await accountRequest("POST", "/passport/secure/api/easy_login", { data: body });
    const data = assertOk(res.data, "WPS自动登录");
    if (data.further_action) throw new Error(`WPS登录需要额外操作: ${data.further_action}`);
    if (!data.jwk) throw new Error(`WPS登录未返回 jwk: ${JSON.stringify(res.data)}`);
    this.cookies = res.cookies;
    this.secret = deriveSecret(keyPair.privateKey, data.jwk);
    this.uid = data.user_id || this.cookies.uid || "";
    this.saveCache();
    log(`账号[${this.index}] WPS登录成功: ${mask(this.uid || this.cookies.wps_sid)}`);
  }
  async ensureLogin() {
    this.loadCache();
    if (this.cookies.kso_sid && this.cookies.wps_sid && this.secret) return;
    await this.login();
  }
  async request(method, url, options = {}) {
    await this.ensureLogin();
    let res = await wpsRequest(method, url, {
      ...options,
      cookies: this.cookies,
      secret: this.secret,
      clockConfig: this.clockConfig,
    });
    this.cookies = res.cookies;
    this.saveCache();
    if (res.data?.msg === "empty wps_sid" || res.data?.result === "userNotLogin") {
      if (!this.openid && !this.yyb) throw new Error("WPS登录态失效，且缺少账号标识无法刷新");
      log(`账号[${this.index}] 登录态失效，重新登录`);
      this.removeLogin();
      await this.login();
      res = await wpsRequest(method, url, {
        ...options,
        cookies: this.cookies,
        secret: this.secret,
        clockConfig: this.clockConfig,
      });
      this.cookies = res.cookies;
      this.saveCache();
    }
    return res.data;
  }
  async loadClockConfig() {
    const res = await fetchBusiness("GET", API.ACTIVITY_CONFIG, {
      timeout: 15000,
      validateStatus: () => true,
      headers: {
        "User-Agent": "Mozilla/5.0 MicroMessenger MiniProgramEnv/Windows",
        Referer: `https://servicewechat.com/${MINI_APP_ID}/249/page-frame.html`,
      },
    });
    if (res.data?.result === "ok" && res.data?.data?.value) {
      this.clockConfig = res.data.data.value;
      if (!this.clockConfig.key && this.clockConfig.s_key) this.clockConfig.key = this.clockConfig.s_key;
    }
  }
  async userInfo() {
    const data = assertOk(await this.request("POST", `${API.ACCOUNT_BASE}/p/auth/check`), "查询用户信息");
    this.uid = data.userid || data.id || this.uid || this.cookies.uid || "";
    this.saveCache({ nickname: data.nickname || data.username || "" });
    log(`账号[${this.index}] 用户: ${mask(data.nickname || data.username || this.uid)}`);
  }
  async clockInfo() {
    try {
      const res = await this.request("GET", API.CLOCK_INFO, {
        params: { client_type: CLIENT_TYPE, page_index: 0, page_size: 10 },
      });
      if (res.result === "ok") {
        const d = res.data || {};
        if (d.s_key) this.clockConfig.key = d.s_key;
        log(`账号[${this.index}] 签到信息: 连续${d.continuous_days ?? 0}天`);
      }
    } catch (e) {
      log(`账号[${this.index}] 查询签到信息失败: ${e.message || e}`);
    }
  }
  async sign() {
    const res = await this.request("POST", API.CLOCK_IN, { data: { client_type: CLIENT_TYPE }, signed: true });
    if (res.result === "ok") {
      const data = res.data || {};
      log(`账号[${this.index}] ✅ 签到成功: 连续${data.continuous_days ?? data.continuousDays ?? "未知"}天`);
      return;
    }
    if (res.msg === "already clocked in today") {
      log(`账号[${this.index}] ✅ 今日已签到`);
      return;
    }
    throw new Error(`签到失败: ${res.msg || JSON.stringify(res)}`);
  }
  async getMainCode() {
    if (!this.openid && !this.yyb) return "";
    return acquireCode(this.openid, MINI_APP_ID, this.yyb);
  }
  async taskOutline() {
    const authCode = await this.getMainCode();
    if (!authCode) throw new Error("缺少账号标识（地址@账号 或 openid），无法获取浏览任务 auth_code");
    const res = await this.request("GET", API.TASK_OUTLINE, {
      params: { mp_id: WPS_MPID, auth_code: authCode },
    });
    return assertOk(res, "查询任务列表") || {};
  }
  flattenTasks(outline = {}) {
    const tasks = [];
    const collect = (type, item) => {
      if (!item) return;
      if (Array.isArray(item)) item.forEach((v) => collect(type, v));
      else if (typeof item === "object") tasks.push({ ...item, type });
    };
    Object.entries(outline).forEach(([type, value]) => collect(type, value));
    return tasks;
  }
  async doBrowseTask(task) {
    const authCode = await this.getMainCode();
    const clientType = task.client_type || "wechat";
    const startBody = {
      mp_id: WPS_MPID,
      auth_code: authCode,
      browse_app_id: task.app_id || MINI_APP_ID,
      client_type: clientType,
      version: "new",
    };
    if (clientType !== "wechat" && task.path) startBody.path = task.path;
    const start = await this.request("POST", API.TASK_START_BROWSE, { data: startBody });
    if (start.result !== "ok" && start.msg !== "任务已完成") {
      log(`账号[${this.index}] 浏览任务启动失败: ${start.msg || JSON.stringify(start)}`);
      return false;
    }
    await wait(16000, 17000);
    const finish = await this.request("POST", API.TASK_FINISH_BROWSE, {
      data: {
        mp_id: WPS_MPID,
        auth_code: await this.getMainCode(),
        app_id: clientType === "wechat_web" ? MINI_APP_ID : task.app_id || MINI_APP_ID,
        client_type: clientType,
        path: clientType === "wechat_web" ? task.path || "" : "",
        version: "new",
        user_id: Number(this.uid || this.cookies.uid || 0),
      },
    });
    const ok = finish.result === "ok" || finish.msg === "任务已完成";
    log(`账号[${this.index}] ${ok ? "✅" : "❌"} 浏览任务${ok ? "完成" : "失败"}: ${task.title || task.task_id || task.app_id || ""}`);
    return ok;
  }
  async browseTasks() {
    let outline;
    try { outline = await this.taskOutline(); }
    catch (e) { log(`账号[${this.index}] 查询任务列表失败: ${e.message || e}`); return; }
    const tasks = this.flattenTasks(outline).filter((t) => t.type === "browse" && Number(t.status || 0) !== 1);
    if (!tasks.length) { log(`账号[${this.index}] 暂无待完成浏览任务`); return; }
    log(`账号[${this.index}] 待浏览任务: ${tasks.length}个`);
    for (const task of tasks) {
      try { await this.doBrowseTask(task); }
      catch (e) { log(`账号[${this.index}] 浏览任务异常: ${e.message || e}`); }
    }
  }
  async lotteryTimes() {
    const res = await this.request("GET", API.LOTTERY_TIMES, { params: { position: "wx_xcx_clock_activity" } });
    if (res.result === "ok") {
      log(`账号[${this.index}] 可抽奖次数: ${res.data ?? 0}`);
      return Number(res.data || 0);
    }
    log(`账号[${this.index}] 查询抽奖次数失败: ${res.msg || JSON.stringify(res)}`);
    return 0;
  }
  async tryLotteryOnce(index) {
    const actCsrf = randomHex(16);
    const body = {
      component_uniq_number: {
        activity_number: "HD2024082815116866",
        page_number: "YM2024082815122017",
        component_number: "ZJ2025092916516585",
        component_node_id: "FN1766995952bvx3",
        filter_params: {},
      },
      component_type: 45,
      component_action: "lottery_v2.exec",
      lottery_v2: { session_id: 1 },
    };
    const cookies = { ...this.cookies, act_csrf_token: actCsrf };
    const res = await wpsRequest("POST", API.COMPONENT_ACTION, {
      data: body,
      cookies,
      secret: this.secret,
      headers: {
        "Content-Type": "application/json",
        "X-Act-Csrf-Token": actCsrf,
        Referer: API.LOTTERY_PAGE,
      },
    }).catch((e) => ({ data: { result: "error", msg: e.message || String(e) }, cookies: this.cookies }));
    this.cookies = mergeSetCookie([], { ...this.cookies, ...res.cookies });
    this.saveCache();
    const result = res.data;
    if (result?.result === "ok" && result.data?.lottery_v2?.success) {
      const reward = result.data.lottery_v2;
      log(`账号[${this.index}] 🎁 抽奖[${index}]成功: ${reward.reward_name || reward.reward_type || "已中奖"}`);
      return true;
    }
    const detail = result?.data?.lottery_v2?.error_code
      ? `错误码${result.data.lottery_v2.error_code}`
      : result?.msg || JSON.stringify(result);
    log(`账号[${this.index}] 抽奖[${index}]失败: ${detail}`);
    return false;
  }
  async lottery() {
    const count = await this.lotteryTimes();
    const limit = Math.min(count, LOTTERY_LIMIT);
    for (let i = 1; i <= limit; i++) {
      if (!await this.tryLotteryOnce(i)) break;
      await wait(1000, 1800);
    }
  }
  async run() {
    await this.ensureOpenid();
    log(`\n====== WPS 账号[${this.index}] ${mask(this.openid || (this.yyb ? this.yyb.ref : "") || this.cacheKey)} ======`);
    await this.ensureLogin();
    await this.loadClockConfig();
    await this.userInfo();
    await this.clockInfo();
    await this.sign();
    await this.browseTasks();
    await this.lottery();
  }
}

function getAccounts() {
  const raw = String(process.env[ENV_NAME] || "").trim();
  if (!raw) return [];
  const seen = new Set();
  return raw
    .split(/\n|&/)
    .map((x) => x.trim())
    .filter((x) => x && !x.startsWith("#") && x !== "[object Object]")
    .filter((x) => (seen.has(x) ? false : (seen.add(x), true))); // 去重保序，避免同账号重复跑烧两次 code
}

async function tryNotify() {
  const text = logs.join("\n");
  const candidates = ["./sendNotify", "/ql/data/scripts/sendNotify", "/ql/scripts/sendNotify"];
  for (const p of candidates) {
    try {
      const mod = require(p);
      if (typeof mod.sendNotify === "function") {
        await mod.sendNotify(TITLE, text);
        return;
      }
    } catch {}
  }
}

async function main() {
  const accounts = getAccounts();
  if (!accounts.length) {
    log(`❌ 未添加环境变量 ${ENV_NAME}（每行 地址@账号ID或OpenID）`);
    process.exitCode = 1;
    return;
  }
  log(`${TITLE} 共获取到 ${accounts.length} 个账号`);

  if (DRY_RUN) {
    log("WPS_DRY_RUN=1 预演模式：不发起任何网络请求");
    accounts.forEach((raw, i) => {
      const a = parseAccount(raw);
      const ident = a.yyb ? `YYB ${a.yyb.endpoint} @ref=${mask(a.yyb.ref)}` : (mask(a.openid) || "无");
      log(`账号[${i + 1}] 标识=${ident} cookie=${a.cookie ? "有" : "无"} secret=${a.secret ? "有" : "无"}`);
    });
    return;
  }

  const inline = accounts.filter((x) => parseAccount(x).yyb).length;
  if (inline) echo(`YYB 账号：${inline}/${accounts.length} 个（地址@账号形态，openid 自动从 YYB 取）`);
  if (accounts.length - inline) {
    echo(`纯 openid/cookie 账号：${accounts.length - inline} 个`);
  }
  if (proxyEnabled()) {
    echo(`代理已启用（类型 ${PROXY_TYPE}，业务请求走代理，YYB/通知直连）`);
  }

  for (let i = 0; i < accounts.length; i++) {
    if (i > 0) {
      await wait(proxyEnabled() ? PROXY_INTERVAL * 1000 : 3000, proxyEnabled() ? PROXY_INTERVAL * 1000 : 6000);
    }
    if (proxyEnabled()) await acquireAccountProxy();
    try { await new Task(accounts[i], i + 1).run(); }
    catch (e) { log(`账号[${i + 1}] ❌ 执行失败: ${e.message || e}`); }
  }
}

if (require.main === module) {
  main()
    .catch((e) => { log(`脚本异常: ${e.message || e}`); process.exitCode = 1; })
    .finally(async () => { await tryNotify(); log("WPS任务执行结束"); });
}

module.exports = {
  API, Task, logs, log, echo, wait, mask, preview,
  parseAccount, getAccounts, parseYybAccounts, splitYybServer, findCode, parseYybRef,
  yybGetCode, yybFetchCode, yybOpenid, yybMarkUnsupported, readYybState, yybErrorMessage,
  acquireCode, resolveYybAccount,
  isProxyFault, pullProxy, buildProxyConfig, validateProxy, fetchBusiness,
  directRequest, acquireAccountProxy, proxyLabel,
  YybError, CodeRejectedError, YybCoolingError,
  proxyState,
};
