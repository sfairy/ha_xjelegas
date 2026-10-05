"""网上国网 App 通道：纯 HTTP 登录与取数（不需要浏览器，也不需要验证码）。

为什么要有它：网页端的 `c44/f06` 会被服务端按标识做 RK001 判定，只有真浏览器能过，于是每日
无人值守必须养一个 headless Chrome。App 网关（`csc-service.sgcc.com.cn:28630`）不查验证码，
一次登录给的令牌有效 15 天（实测 `tokenExpireTime=1296000`）。

信封与网页端不同名不同编码（照 App 里的写法，见 `build_envelope`），SM2/SM3/SM4 原语复用
本仓库 `utils/crypt.py`。设备身份与令牌由 `turing/` 纯 Python 生成——该目录来自
https://github.com/stevenjoezhang/hass-state-grid（MIT，见 LICENSE.hass-state-grid），
`generate_check_code` / `login_md5` / 常量同源于该仓库的 login.py 与 const.py。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import secrets
import string
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .utils.crypt import (AA, BB, SM4_DECRYPT, SM4_ENCRYPT, m_hash, m_kdf)
from .const import RATE_LIMIT_CODES
from .utils.logger import LOGGER

APP_BASE_URL = "https://csc-service.sgcc.com.cn:28630"
LOGIN_PATH = "emss-uia-center-front/member/c2/f01"
DEVICE_SMS_PATH = "emss-uia-center-front/member/c1/f01"   # businessType=logindevice：发新设备验证短信
DAILY_PATH = "emss-bia-bill-front/member/c11/f01"
MONTHLY_PATH = "emss-bia-bill-front/member/c51/f04"
BALANCE_PATH = "emss-bia-balance-front/member/c16/f01"
# 4006=新设备需短信验证；RK008=需官方 App 的交互式校验（短信码过不了）
DEVICE_VERIFICATION_CODE = "4006"
INTERACTIVE_CHALLENGE_CODES = {"RK008"}

SERVER_PUBLIC_KEY_HEX = (
    "0409C10D38CF7B4E28097EAAA519E3157C9B4E72194CD13BD11932CE40ED5624A"
    "FEFF27F78893E4C4FC2029DA147AB6CEAE0CDC8E3A547EFCDC5AB91757FE1CA60")
CLIENT_PRIVATE_KEY_HEX = "50C4AF48DF75808050729977AA6BC127DC21A404755C660B94222F9D50FA4A75"
CHECK_CODE_AES_KEY = b"LNPO+ISJ+QeqNemFK+3OMUdexSn2i6pB"
APP_VERSION = "3.2.3"
TOKEN_LIFE_SAFETY_S = 86400          # 剩余不足一天就重新登录
TOKEN_CACHE_MS = 14_400_000          # App 自己的 Turing 缓存是 4 小时
CHINA = timezone(timedelta(hours=8))


# ────────────────────────────── 信封 ──────────────────────────────
def compact_json(value: Any) -> str:
    """Gson 风格的紧凑 JSON：App 是按这个算签名和加密的。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sm4_key_bytes(key_text: str) -> bytes:
    # App 不 hex-decode：密钥就是 32 位 hex 文本的前 16 个 ASCII 字符
    return key_text[:16].encode("ascii")


def sm4_encrypt_hex(plain: str | bytes, key_text: str) -> str:
    raw = plain.encode("utf-8") if isinstance(plain, str) else plain
    c = AA(mode=SM4_ENCRYPT)              # AA 默认 padding 就是 PKCS7
    c.set_key(_sm4_key_bytes(key_text), SM4_ENCRYPT)
    return c.crypt_ecb(raw).hex().upper()


def sm4_decrypt_hex(cipher_hex: str, key_text: str) -> bytes:
    c = AA(mode=SM4_DECRYPT)
    c.set_key(_sm4_key_bytes(key_text), SM4_DECRYPT)
    return c.crypt_ecb(bytes.fromhex(cipher_hex))


def sm2_wrap(key_text: str, public_key_hex: str) -> str:
    c = BB(public_key=public_key_hex[2:] if public_key_hex.startswith("04") else public_key_hex,
           mode=1)
    # 封的是 key_text 的 ASCII 字节；utils/crypt.d() 会先把字符串转成 hex，语义不同，不能复用
    return "04" + c.encrypt(key_text.encode("ascii")).hex().lower()


def _derive_public(private_hex: str) -> str:
    probe = BB(public_key="0" * 128, mode=1)
    return probe._kg(int(private_hex, 16), probe.ecc_table["g"])


def sm2_unwrap(wrapped_hex: str, private_hex: str) -> str:
    """解响应的 respKey。

    三处必须和 BB.encrypt 对齐，错一处就解不出来（都踩过）：
      1) 密钥流长度参数按**字节**算（m_kdf 收的是 len(hex)/2）；
      2) 共享点是 x2||y2 的 hex **字符串**，不是点字节；
      3) C3 = SM3(x2 ‖ 明文hex ‖ y2)，不是 x2‖y2‖明文。
    """
    wire = wrapped_hex[2:] if wrapped_hex.lower().startswith("04") else wrapped_hex
    c = BB(public_key=_derive_public(private_hex), mode=1)
    para = c.para_len
    c1, c3, c2 = wire[:2 * para], wire[2 * para:2 * para + 64], wire[2 * para + 64:]
    shared = c._kg(int(private_hex, 16), c1)
    stream = m_kdf(shared.encode("utf-8"), len(c2) // 2)
    plain_hex = ("%0" + str(len(c2)) + "x") % (int(c2, 16) ^ int(stream, 16))
    x2, y2 = shared[:para], shared[para:2 * para]
    if m_hash([b for b in bytes.fromhex(x2 + plain_hex + y2)]).lower() != c3.lower():
        raise ValueError("SM2 解密校验失败（C3 不匹配）")
    return bytes.fromhex(plain_hex).decode("ascii")


def build_envelope(payload: Any) -> dict[str, str]:
    key_text = uuid4().hex
    ts = str(int(time.time() * 1000))
    data = sm4_encrypt_hex(compact_json(payload), key_text)
    skey = sm2_wrap(key_text, SERVER_PUBLIC_KEY_HEX)
    return {"data": data, "sign": m_hash(list((skey + data + ts).encode())).lower(),
            "skey": skey, "timestamp": ts}


def parse_envelope(outer: dict[str, Any]) -> Any:
    key_text = sm2_unwrap(str(outer["respKey"]), CLIENT_PRIVATE_KEY_HEX)
    return json.loads(sm4_decrypt_hex(str(outer["encryptData"]), key_text).decode("utf-8"))


# ────────────────────────────── 请求体 ──────────────────────────────
def _java_string_hash(value: str) -> int:
    out = 0
    for ch in value:
        out = (31 * out + ord(ch)) & 0xFFFFFFFF
    return out


def _java_map_order(value: Any) -> Any:
    """App 侧签名按 Java HashMap 的桶序展开，顺序错一位 md5 就对不上。"""
    if isinstance(value, dict):
        buckets: dict[int, list[str]] = {}
        for key in value:
            h = _java_string_hash(str(key))
            h ^= h >> 16
            buckets.setdefault(h & 15, []).append(str(key))
        return {k: _java_map_order(value[k])
                for b in sorted(buckets) for k in buckets[b]}
    if isinstance(value, list):
        return [_java_map_order(v) for v in value]
    return value


def login_md5(params: dict[str, Any]) -> str:
    material = "".join(k + json.dumps(_java_map_order(params[k]), ensure_ascii=False,
                                     separators=(",", ":")) for k in sorted(params))
    return hashlib.md5(material.encode("utf-8")).hexdigest()


def _aes_ecb_hex(raw: bytes, key: bytes) -> str:
    """AES-ECB + PKCS7 填充，返回小写 hex。

    优先用 manifest 已声明的 pycryptodome（gas 也在用），退回 HA 自带的 cryptography。
    这样 debug_info.py 单独运行时，不会因为环境里只有 pycryptodome 而报缺 cryptography。
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        return AES.new(key, AES.MODE_ECB).encrypt(pad(raw, AES.block_size)).hex()
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        p = PKCS7(128).padder()
        padded = p.update(raw) + p.finalize()
        e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return (e.update(padded) + e.finalize()).hex()


def check_code(account: str, ts_ms: int) -> str:
    """24 位随机数字 + AES/ECB(account+毫秒时间戳) 的 hex。"""
    rand = f"{secrets.randbits(63):024d}"
    return rand + _aes_ecb_hex(f"{account}{ts_ms}".encode(), CHECK_CODE_AES_KEY)


@dataclass
class AppAccount:
    """一块表；取数一律用 cons_no_src（明文户号），加密串只在登录里用。"""
    account_id: str
    cons_no: str
    cons_no_src: str
    pro_no: str
    org_no: str
    elec_type: str

    @property
    def cons_type(self) -> str:
        """日电量请求要用的 consType：上游只有 04（以及北京的 02）算"02"，其余一律 "01"。

        这里不能直接把 elecTypeCode 填进去——实测第二块表（车位，elecTypeCode=03）那样请求
        会拿不到任何行，而 constType 才是这个字段真正的来源键。
        """
        if self.elec_type == "04" or (self.elec_type == "02" and self.pro_no == "11102"):
            return "02"
        return "01"

    @classmethod
    def from_api(cls, v: dict[str, Any]) -> "AppAccount":
        def first(*keys: str, default: str = "") -> str:
            for k in keys:
                if v.get(k) not in (None, ""):
                    return str(v[k])
            return default
        cons = first("powerUserNo", "consNo")
        return cls(account_id=first("id", "userId", default=cons),
                   cons_no=cons,
                   cons_no_src=first("powerUserNo_dst", "consNo_dst", default=cons),
                   pro_no=first("proNo", "provinceId"),
                   org_no=first("orgNo"),
                   # 上游取的是 constType（服务端就是这个拼写），不是 consType/elecTypeCode
                   elec_type=first("elecType", "constType", default="01"))


def _daily_payload(a: AppAccount, start: date, end: date) -> dict[str, Any]:
    return {"serviceCode": "BCP_000026", "source": "app", "target": a.pro_no,
            "data": {"acctId": "acctid01", "channelCode": "SGAPP",
                     "consNo": a.cons_no_src, "consNosrc": a.cons_no_src,
                     "endTime": end.isoformat(), "consType": a.cons_type,
                     "funcCode": "ALIPAY_01", "orgNo": a.org_no, "proCode": a.pro_no,
                     "promotCode": "1", "promotType": "1", "serialNo": "", "srvCode": "",
                     "startTime": start.isoformat(), "userName": "acctid01"}}


def _monthly_payload(a: AppAccount, year: int) -> dict[str, Any]:
    return {"serviceCode": "BCP_000026", "source": "app", "target": a.pro_no,
            "data": {"year": year, "consNo": a.cons_no_src, "provinceCode": a.pro_no,
                     "startYm": "%d01" % year, "endYm": "%d12" % year,
                     "funcCode": "ALIPAY_01"}}


def _balance_payload(a: AppAccount, user_id: str) -> dict[str, Any]:
    return {"serviceCode": "0101143", "source": "app", "target": a.pro_no,
            "data": {"srvCode": "", "serialNo": "", "channelCode": "0902",
                     "funcCode": "A1007200", "acctId": user_id, "userName": "acctid01",
                     "promotType": "1", "promotCode": "1", "userAccountId": user_id,
                     "list": [{"consNoSrc": a.cons_no_src, "proCode": a.pro_no,
                               "sceneType": a.elec_type, "consNo": a.cons_no,
                               "orgNo": a.org_no}]}}


def _login_payload(account: str, password_md5: str, model: str,
                   release: str, code: str = "", code_key: str = "") -> dict[str, Any]:
    """`password_md5` 是**已经算好的** 32 位 md5 十六进制小写。

    网页端发的是 md5().upper()、App 发的是同一值的 .lower()，两边只差大小写；
    HA 的 store 里存的就是那份摘要，所以集成不必持有明文密码就能走 App 通道。

    新设备验证时，服务端要求把短信码回填到 **同一个密码登录请求**的
    `quInfo.code` / `quInfo.codeKey`（不是另一个接口），这里用两个可选参数承载。
    """
    ts = int(time.time() * 1000)
    push = "000000,000000"
    return {
        "quInfo": {"code": code, "codeKey": code_key, "account": account,
                   "password": password_md5,
                   "optSys": "Android", "pushId": push,
                   "addressCity": "", "addressProvince": "", "addressRegion": ""},
        "uscInfo": {"isEncrypt": "false", "devciceIp": "127.0.0.1", "devciceId": "000000",
                    "tenant": "state_grid", "member": "2202",
                    "optSys": f"Android {release}", "operatorType": "",
                    "devciceName": model, "pushId": push},
        "checkCode": check_code(account, ts),
        "avalonValidCode": "",
    }


def _device_sms_payload(account: str, model: str) -> dict[str, Any]:
    """请求下发"新设备登录"短信（businessType=logindevice）。"""
    return {
        "uscInfo": {"tenant": "state_grid", "member": "2202", "devciceId": "000000",
                    "devciceName": model, "devciceIp": "127.0.0.1"},
        "quInfo": {"voiceCodeFlag": False, "account": account, "sendType": 0,
                   "businessType": "logindevice"},
    }


# ────────────────────────────── 设备身份 ──────────────────────────────
class AppDevice:
    """一台"合成的安卓设备"：种子和令牌都要跨次不变。

    换种子等于换设备，服务端有可能因此要求新设备短信验证；令牌丢了也要重新登录，
    所以这些都进 HA 的 .storage 持久化，不落临时目录。
    """

    def __init__(self, hass) -> None:
        self._store = Store(hass, 1, "xjelegas_ele.app_device")
        self.doc: dict[str, Any] = {}

    async def async_prepare(self, hass) -> None:
        """在别的线程里首次导入 turing 模块。

        `feature_profile` 在模块级同步读 reference_environment.json；直接在事件循环里
        首次导入会被 HA 判定为阻塞调用（实测已经报 WARNING 并打回溯）。
        """
        import importlib

        await hass.async_add_import_executor_job(
            importlib.import_module, f"{__name__.rsplit('.', 1)[0]}.turing.feature_profile")

    async def async_load(self) -> None:
        """读回画像；没有就先落盘再往下走。

        种子必须先落盘：它是"这台合成设备"的唯一身份，登录中途崩掉或换令牌时重建都会
        让服务端看到一台新设备（那样就可能被要求做新设备短信验证）。
        """
        self.doc = await self._store.async_load() or {}
        if not self.doc.get("seed_b64"):
            import base64
            now = int(time.time() * 1000)
            self.doc = {"seed_b64": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
                        "boot_epoch_ms": now - random.randrange(86_400_000 * 13)}
            await self.async_save()

    async def async_save(self) -> None:
        await self._store.async_save(self.doc)

    def _profile(self):
        import base64
        from .turing.feature_profile import StableProfile
        seed_b64 = self.doc.get("seed_b64")
        if not seed_b64:
            raise RuntimeError("设备画像未初始化（先 async_load）")
        return StableProfile(base64.b64decode(seed_b64),
                             boot_epoch_ms=int(self.doc.get("boot_epoch_ms") or 0))

    @property
    def app_guid(self) -> str:
        guid = self.doc.get("app_guid")
        if guid:
            return str(guid)
        alpha = string.ascii_letters + string.digits
        guid = "".join(alpha[v % len(alpha)] for v in self._profile().bytes("app-guid", 60))
        self.doc["app_guid"] = guid
        return guid

    async def async_device_token(self) -> tuple[str, str]:
        """4 小时内的旧令牌直接用（照 App 的 Hawk 缓存行为），别让每次请求都重算一遍。"""
        cached, stamp = self.doc.get("token") or "", int(self.doc.get("token_ms") or 0)
        if cached and 0 <= int(time.time() * 1000) - stamp <= TOKEN_CACHE_MS:
            return cached, str(stamp)[:10]
        from .turing.device_token import generate_device_token
        gen = await asyncio.get_running_loop().run_in_executor(
            None, generate_device_token, self._profile())
        self.doc.update({"token": gen.token, "token_ms": gen.timestamp_ms})
        await self.async_save()
        LOGGER.warning("App 通道：重新生成本地设备令牌（%d 字符）", len(gen.token))
        return gen.token, gen.token_time

    @property
    def identity(self) -> tuple[str, str]:
        p = self._profile()
        idn = p.identity()
        return str(idn.model), str(idn.release)


class AppChannel:
    """App 网关的登录与取数；令牌 15 天有效，所以只在快到期或缺失时才登录。"""

    def __init__(self, hass, account: str, password_md5: str) -> None:
        self.hass = hass
        self.account = account
        self.password_md5 = password_md5
        self.device = AppDevice(hass)
        self.token = ""
        self.user_id = ""
        self.province = ""
        self.accounts: list[AppAccount] = []
        self.raw_accounts: list[dict[str, Any]] = []
        self.expires_at = 0
        self.last_error = ""
        self.verification_required = False   # 服务端要求新设备短信验证（4006）
        self._lock = asyncio.Lock()

    async def async_load_session(self) -> bool:
        await self.device.async_prepare(self.hass)
        await self.device.async_load()
        s = self.device.doc.get("session") or {}
        self.token, self.user_id = str(s.get("token") or ""), str(s.get("user_id") or "")
        self.province = str(s.get("province") or "")
        self.accounts = [AppAccount.from_api(v) for v in (s.get("accounts") or [])
                         if isinstance(v, dict)]
        # 原始行也要恢复：灌推送缓存时喂给网页解析器的是原样字段，不是 AppAccount
        self.raw_accounts = [v for v in (s.get("accounts") or []) if isinstance(v, dict)]
        return bool(self.token) and int(s.get("expires_at") or 0) > time.time() + TOKEN_LIFE_SAFETY_S

    async def async_save_session(self) -> None:
        self.device.doc["session"] = {"token": self.token, "user_id": self.user_id,
                                      "province": self.province, "expires_at": self.expires_at,
                                      "accounts": self.raw_accounts}
        await self.device.async_save()

    async def async_login(self, force: bool = False) -> bool:
        async with self._lock:
            # 设备画像与上次会话必须每次都先加载：force 只决定"要不要复用未过期的令牌"，
            # 不能顺带跳过设备加载，否则 device.doc 还是空的，_profile() 会直接抛
            # RuntimeError("设备画像未初始化")。配置向导/调试脚本首次调用就是这个场景。
            session_valid = await self.async_load_session()
            if not force and session_valid:
                return True
            model, release = self.device.identity
            token, token_time = await self.device.async_device_token()
            params = _login_payload(self.account, self.password_md5, model, release)
            plain = await self._post(LOGIN_PATH, params, token=token, token_time=token_time,
                                     model=model, release=release, login_params=params)
            return await self._finish_login(plain, model=model, release=release)

    def _apply_login_response(self, plain, *, model: str, release: str) -> bool:
        """解析登录响应：成功则落会话，失败则归类 last_error 供配置页分流。"""
        biz = ((plain or {}).get("data") or {}).get("bizrt") or {}
        ui = biz.get("userInfo") or {}
        if isinstance(ui, list):
            ui = ui[0] if ui else {}
        if not biz.get("token"):
            srv = ((plain or {}).get("data") or {}).get("srvrt") or {}
            code = str((plain or {}).get("code") or "")
            result_code = str(srv.get("resultCode") or "")
            text = str(srv.get("resultMessage") or (plain or {}).get("message") or "")
            # 配置向导要把"密码错了""被限流""新设备验证"分开报，光一个 False 会把用户
            # 引去改密码，而真正的原因可能是流控，或者需要一次短信验证
            if plain is None:
                self.last_error = "cannot_connect"
            elif result_code == DEVICE_VERIFICATION_CODE or "新设备" in text or "安全验证" in text:
                self.last_error = "device_verification"
                self.verification_required = True
            elif result_code in INTERACTIVE_CHALLENGE_CODES or "RK008" in text:
                self.last_error = "interactive_challenge"
            elif code in RATE_LIMIT_CODES or "RK001" in text or "日额度" in text:
                self.last_error = "rate_limited"
            elif code == "RK007" or "RK007" in text or "超时" in text:
                # RK007=网络连接超时：服务端瞬时抖动/短时频控，等一会重试即可，不是密码问题
                self.last_error = "server_busy"
            elif "密码" in text or "账号" in text or result_code in ("0100", "0101"):
                self.last_error = "invalid_auth"
            else:
                self.last_error = "unknown"
            LOGGER.warning("App 通道登录未完成 code=%s resultCode=%s message=%r",
                           code, result_code, text[:80])
            return False
        self.verification_required = False
        self.token = str(biz["token"])
        self.user_id = str(ui.get("userId") or "")
        self.province = str(ui.get("addressProvince") or "")
        life = int(biz.get("tokenExpireTime") or 1296000)
        self.expires_at = int(time.time()) + life
        raw = ui.get("powerUserList") or []
        self.raw_accounts = raw
        self.accounts = [AppAccount.from_api(v) for v in raw if isinstance(v, dict)]
        return True

    async def _finish_login(self, plain, *, model: str, release: str) -> bool:
        """登录/短信登录共用的收尾：成功才落会话（避免半截会话被持久化）。"""
        if not self._apply_login_response(plain, model=model, release=release):
            return False
        await self.async_save_session()
        LOGGER.warning("App 通道登录成功：令牌 %d 字符、有效期 %d 天，户号 %d 块",
                       len(self.token), (self.expires_at - int(time.time())) // 86400,
                       len(self.accounts))
        return True

    async def async_send_device_verification_sms(self) -> str:
        """下发"新设备登录"短信，返回一次性 codeKey；失败返回空串。

        codeKey 只在本次流程内有效，不持久化（对齐上游：验证码与 codeKey 均不落盘）。
        """
        model, release = self.device.identity
        token, token_time = await self.device.async_device_token()
        plain = await self._post(DEVICE_SMS_PATH, _device_sms_payload(self.account, model),
                                 token=token, token_time=token_time,
                                 model=model, release=release)
        if plain is None:
            self.last_error = "cannot_connect"
            return ""
        biz = ((plain.get("data") or {}).get("bizrt") or {})
        code_key = str(biz.get("codeKey") or "")
        if not code_key:
            srv = ((plain or {}).get("data") or {}).get("srvrt") or {}
            self.last_error = "sms_send_failed"
            LOGGER.warning("App 通道下发新设备短信失败：%s %s",
                           srv.get("resultCode"), str(srv.get("resultMessage"))[:80])
        return code_key

    async def async_login_with_code(self, code: str, code_key: str) -> bool:
        """新设备验证：把短信码回填进 **c2/f01 密码登录请求**再试一次。

        注意不是换去 c2/f02 —— 那条是"短信验证码登录"另一套功能，用它换 logindevice
        的短信码会被判 4003 验证码校验失败。成功即设备被信任，之后可跨次复用令牌。
        """
        async with self._lock:
            await self.async_load_session()   # 确保设备画像/设备令牌就绪
            model, release = self.device.identity
            token, token_time = await self.device.async_device_token()
            params = _login_payload(self.account, self.password_md5, model, release,
                                    code=code, code_key=code_key)
            plain = await self._post(LOGIN_PATH, params, token=token, token_time=token_time,
                                     model=model, release=release, login_params=params)
            return await self._finish_login(plain, model=model, release=release)

    async def _post(self, path: str, payload: Any, *, token: str, token_time: str,
                    model: str, release: str, login_params=None,
                    session_token: str | None = None) -> dict[str, Any] | None:
        now = datetime.now(CHINA)
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "timeStamp": (now.strftime("%Y%m%d%H%M%S") if login_params is not None
                          else now.strftime("%Y%m%d%H%M%S%f")[:17]
                          + "".join(str(random.randrange(10)) for _ in range(6))),
            "t": session_token if session_token is not None else "",
            "userid": self.user_id if session_token is not None else "0",
            "AppGuid": self.device.app_guid,
            "AppGuidNew": "".join(random.choice(string.ascii_letters + string.digits)
                                  for _ in range(40)) + now.strftime("%Y%m%d%H%M%S%f")[:17]
            + str(random.randrange(100, 1000)),
            "security": "android", "appcode": "WSGW-SG1001-APP", "datacenter": "99",
            "AccessMethod": "App", "deviceTokenTX": token, "deviceTokenTXTime": token_time,
            "province": self.province if session_token is not None else "",
            "version": APP_VERSION, "wsgwType": "android", "ip": "127.0.0.1", "os": "android",
            "User-Agent": "okhttp/3.14.9",
        }
        if login_params is not None:
            headers["md5"] = login_md5(login_params)
        session = async_get_clientsession(self.hass)
        try:
            async with await session.post(f"{APP_BASE_URL}/{path}",
                                          data=json.dumps(build_envelope(payload),
                                                          ensure_ascii=False,
                                                          separators=(",", ":")),
                                          headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=30)) as resp:
                outer = await resp.json(content_type=None)
        except Exception as exc:
            LOGGER.warning("App 通道 %s 请求失败：%s %s", path, type(exc).__name__, str(exc)[:80])
            return None
        if not isinstance(outer, dict) or "respKey" not in outer:
            LOGGER.warning("App 通道 %s 返回不是加密信封：%s", path, str(outer)[:120])
            return None
        try:
            return parse_envelope(outer)
        except Exception as exc:
            LOGGER.warning("App 通道 %s 响应解不开：%s %s", path, type(exc).__name__, str(exc)[:80])
            return None

    async def _authed(self, path: str, payload: Any):
        model, release = self.device.identity
        token, token_time = await self.device.async_device_token()
        return await self._post(path, payload, token=token, token_time=token_time,
                                model=model, release=release, session_token=self.token)

    async def async_daily(self, a: AppAccount, start: date, end: date) -> dict[str, Any] | None:
        return await self._authed(DAILY_PATH, _daily_payload(a, start, end))

    async def async_monthly(self, a: AppAccount, year: int) -> dict[str, Any] | None:
        return await self._authed(MONTHLY_PATH, _monthly_payload(a, year))

    async def async_balance(self, a: AppAccount) -> dict[str, Any] | None:
        return await self._authed(BALANCE_PATH, _balance_payload(a, self.user_id))
