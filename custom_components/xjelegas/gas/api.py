"""
新疆燃气 (wgas.xjrq.net) API 接口访问层。

本模块负责与新疆燃气微信端 API 的完整交互，包括：
1. 安全握手：RSA 公钥交换，获取服务端公钥与 busyToken
2. 登录认证：手机号+密码，AES 加密后传输，获取 Token 与户号
3. 数据查询：欠费/余额、缴费记录、抄表记录、日用气量
4. 加解密：RSA 加密 AES 密钥，AES-128-ECB 加密业务数据

API 基础地址：https://wgas.xjrq.net/api
加密实现：详见 gas.crypto 模块 (GasCrypto)
"""
# ========== 标准库导入 ==========
import logging      # 日志记录，用于调试和错误追踪
import json         # JSON 解析，处理 API 请求/响应
import ssl          # SSL 上下文，用于自签名证书
import time         # 时间相关，用于登录冷却控制

# ========== 类型注解 ==========
from typing import Dict, Any, Optional

# ========== 第三方库 ==========
import aiohttp
import asyncio

# ========== 日期时间 ==========
from datetime import datetime, timedelta  # 日期计算，用于查询时间范围

# ========== 本地模块 ==========
from .crypto import GasCrypto  # 燃气 API 加解密实现（RSA + AES）

# HomeAssistant 时间工具（可选依赖）：用于获取本地时区时间
# 若未安装 HA，则使用标准 datetime 作为降级方案
try:
    from homeassistant.util import dt as dt_util
except ModuleNotFoundError:
    class _DateUtil:
        """
        简易日期工具类，在非 HA 环境下替代 homeassistant.util.dt。
        提供 now() 方法返回当前本地时间，与 HA 的 dt_util.now() 行为一致。
        """
        @staticmethod
        def now():
            """返回当前本地时间（datetime 对象）"""
            return datetime.now()
    dt_util = _DateUtil()

# 模块级日志器，用于记录 API 调用、错误等信息
_LOGGER = logging.getLogger(__name__)


class XjGasAPI:
    """
    新疆燃气 API 接口类。
    负责处理与新疆燃气服务器的通信，包括加密握手、登录认证以及各项数据的获取。
    
    安全机制说明：
    该接口采用混合加密机制（RSA + AES）来确保通信安全：
    1. RSA-1024: 用于在握手阶段安全地交换 AES 密钥。客户端生成自己的 RSA 密钥对，
       并将公钥发送给服务器，服务器使用该公钥加密 AES 密钥返回给客户端。
    2. AES-128-ECB: 用于后续所有业务数据的加解密。业务数据（如登录凭证、查询参数）
       使用 AES 密钥加密后传输，服务器返回的数据也是 AES 加密的。
    """
    
    def __init__(self, phone: Optional[str] = None, password: Optional[str] = None, is_debug: bool = False):
        """
        初始化 API 实例。
        
        Args:
            phone (str): 用户的手机号，作为登录账号。
            password (str): 用户的登录密码。
            is_debug (bool): 是否输出调试日志（握手、解密、API 响应等），默认关闭。
        """
        # ---------- 用户凭证与认证 ----------
        self._phone = phone           # 登录手机号
        self._password = password     # 登录密码
        self._is_debug = is_debug     # 调试开关，仅开启时输出详细日志
        self._token = None            # 登录成功后获取的会话 Token，后续请求需携带
        self._openid = None           # 用户在微信端的唯一标识 (OpenID 或 UserId)

        # ---------- HTTP 会话配置 ----------
        self._session: Optional[aiohttp.ClientSession] = None

        # ---------- API 基础配置 ----------
        self._api_base = "https://wgas.xjrq.net/api"  # API 根地址
        # 模拟微信 PC 端访问的请求头，避免被反爬拦截
        self._base_headers = {
            'Host': 'wgas.xjrq.net',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf254171e) XWEB/18787 Flue',
            'Content-Type': 'application/json; charset=UTF-8',
            'Origin': 'https://wgas.xjrq.net',
            'Referer': 'https://wgas.xjrq.net/?state=7',
            'part': 'WECHAT',        # 标识来源为微信端
            'fromDomainId': '7'      # 业务域 ID
        }

        # ---------- 加密与会话状态 ----------
        self._crypto = GasCrypto()    # 加解密模块实例（RSA + AES）
        self._busy_token = ""        # 握手阶段获取的 busyToken，用于会话维持
        self._cons_no = None         # 用户户号 (Contract Number)，查询账单/用气量必需
        self._login_failed_at: Optional[float] = None  # 上次登录失败时间戳，用于 60 秒冷却防频繁请求
        self._request_lock = asyncio.Lock()  # 异步锁，保证并发请求安全
        self._verify_ssl = True      # SSL 证书验证，默认启用；若服务器使用自签名证书可设为 False

    async def _get_session(self) -> aiohttp.ClientSession:
        """
        获取或创建 aiohttp 会话。

        如果会话不存在或已关闭，则创建新会话。
        SSL 验证默认启用（_verify_ssl=True）。
        若服务器使用自签名证书，可在初始化后将 _verify_ssl 设为 False。

        Returns:
            aiohttp.ClientSession: 可用的 HTTP 会话
        """
        if self._session is None or self._session.closed:
            if self._verify_ssl:
                connector = aiohttp.TCPConnector(limit=10)
            else:
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                connector = aiohttp.TCPConnector(ssl=ssl_context, limit=10)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    @property
    def cons_no(self):
        """
        获取用户户号（只读属性）。
        户号在登录成功后由服务器返回，用于后续账单、抄表等查询。
        """
        return self._cons_no

    def _build_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """
        构造请求头。
        
        基础请求头用于模拟微信端访问，extra 用于附加认证信息（如 token、secret、busyToken）。
        
        Args:
            extra (dict, optional): 需要合并到基础 Headers 中的额外键值对。
            
        Returns:
            dict: 完整的请求头字典。
        """
        # extra 会覆盖 base 中同名字段，用于动态添加 token 等
        if extra:
            return {**self._base_headers, **extra}
        return dict(self._base_headers)

    async def _handshake(self) -> bool:
        """
        与服务器进行安全握手，增强异常处理和网络错误处理。

        握手流程：
        1. 客户端生成 RSA 密钥对。
        2. 客户端将 RSA 公钥发送给服务器接口 `/sec/serverKey`。
        3. 服务器返回其 RSA 公钥 (serverKey) 和可能的其他会话标识 (secret, busyToken)。

        网络异常时自动重试 2 次，减少瞬时抖动导致的失败。

        Returns:
            bool: 握手是否成功。如果已有服务端公钥，则直接返回 True。
        """
        if self._crypto.has_server_key:
            return True
        url = "%s/sec/serverKey" % self._api_base
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                loop = asyncio.get_event_loop()
                client_pub_clean = await loop.run_in_executor(
                    None, self._crypto.get_client_public_key_clean
                )
                headers = self._build_headers()
                session = await self._get_session()
                async with session.post(url, json={"key": client_pub_clean}, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status != 200:
                        _LOGGER.error("握手请求失败，HTTP状态码：%d，URL：%s", response.status, url)
                        return False
                    try:
                        response_json = await response.json()
                    except json.JSONDecodeError as json_ex:
                        _LOGGER.error("握手响应JSON解析失败：%s，响应内容：%s", json_ex, await response.text())
                        return False
                    if response_json.get('code') in ('0', '200') or response_json.get('success'):
                        response_obj = response_json.get('obj', {})
                        server_key = response_obj.get('serverKey') or response_obj.get('data')
                        self._crypto.secret = response_obj.get('secret') or response_obj.get('key')
                        self._busy_token = response_obj.get('busyToken') or ''
                        if server_key:
                            success = await loop.run_in_executor(
                                None, self._crypto.set_server_public_key, server_key
                            )
                            if success:
                                if self._is_debug:
                                    _LOGGER.info("握手成功，已获取并导入服务器公钥")
                                return True
                            _LOGGER.error("导入服务器公钥失败")
                            return False
                        _LOGGER.error("握手响应中缺少serverKey: %s", response_json)
                        return False
                    _LOGGER.error("握手失败，服务器返回错误: %s", response_json)
                    return False
            except asyncio.TimeoutError:
                if attempt >= max_attempts - 1:
                    _LOGGER.warning("握手请求超时(重试 %d 次后仍失败): URL: %s", max_attempts, url)
                    return False
            except aiohttp.ClientError as client_ex:
                if attempt >= max_attempts - 1:
                    _LOGGER.warning("握手网络连接错误(重试 %d 次后仍失败): %s, URL: %s", max_attempts, client_ex, url)
                    return False
            except Exception as e:
                if attempt >= max_attempts - 1:
                    _LOGGER.warning("握手过程发生未知异常(重试 %d 次后仍失败): %s (类型: %s)", max_attempts, e, type(e).__name__)
                    return False
        return False

    async def _encrypt_payload(self, payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        加密请求载荷。

        使用 GasCrypto 将业务数据 AES 加密，AES 密钥用 RSA 加密后放入 key 字段，
        返回 {data: 密文, key: 加密后的AES密钥} 供 POST 请求使用。

        Args:
            payload (dict): 原始业务数据（如 mobile、password、consNo 等）。

        Returns:
            Optional[Dict[str, str]]: 加密后的载荷，失败返回 None。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._crypto.encrypt_payload, payload)

    async def _decrypt_payload(self, encrypted_response: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """
        解密响应载荷。

        从响应中提取 data 和 key，用 RSA 解密 key 得到 AES 密钥，再解密 data 得到业务数据。

        Args:
            encrypted_response (dict): 服务器返回的加密数据，需包含 'data' 和 'key'。

        Returns:
            Optional[Dict[str, Any]]: 解密后的业务数据，失败返回 None。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._crypto.decrypt_payload, encrypted_response)

    async def login(self) -> bool:
        """
        执行用户登录操作，增强异常处理和数据验证。

        使用手机号和密码进行认证，成功后获取 Token 和 OpenID。
        登录失败会记录时间戳，短时间内避免重复尝试。

        流程：
        1. 检查冷却时间，避免频繁失败请求。
        2. 确保已握手（拥有加密通道）。
        3. 构造包含手机号和密码的载荷并加密。
        4. 发送登录请求。
        5. 解密响应，提取 Token, OpenID 和 户号(ConsNo)。

        Returns:
            bool: 登录是否成功。
        """
        async with self._request_lock:
            # 登录失败后 60 秒内不重复尝试，避免触发风控
            if self._login_failed_at and time.monotonic() - self._login_failed_at < 60:
                if self._is_debug:
                    _LOGGER.info("登录冷却中，跳过本次登录尝试")
                return False

            # 增加重试循环，最多尝试2次，用于处理密钥过期导致的解密错误
            for attempt in range(2):
                if not await self._handshake():
                    self._login_failed_at = time.monotonic()
                    return False

                # 验证输入参数
                if not self._phone or not self._password:
                    _LOGGER.error("登录失败：手机号或密码为空")
                    self._login_failed_at = time.monotonic()
                    return False

                # 构造登录载荷（加密前）
                payload = {
                    "mobile": str(self._phone),
                    "password": str(self._password),
                    "wechatProId": 1   # 固定为 1，标识微信端/小程序
                }

                # 加密登录请求
                encrypted_payload = await self._encrypt_payload(payload)
                if not encrypted_payload:
                    _LOGGER.error("登录失败：请求数据加密失败")
                    self._login_failed_at = time.monotonic()
                    return False

                url = f'{self._api_base}/login/doLoginByPwd'  # 提前定义，供异常处理时记录日志

                # 完整的 Header 结构，包含握手获取的 secret 和 busyToken
                headers = self._build_headers({
                    'secret': self._crypto.secret or '',
                    'busyToken': self._busy_token or '',
                    'openId': self._openid or ''
                })

                try:
                    session = await self._get_session()
                    async with session.post(url, json=encrypted_payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        # 检查HTTP响应状态
                        if response.status != 200:
                            _LOGGER.error("登录请求失败，HTTP状态码: %d, URL: %s", response.status, url)
                            self._login_failed_at = time.monotonic()
                            return False

                        # 解析JSON响应
                        try:
                            response_json = await response.json()
                        except json.JSONDecodeError as json_ex:
                            _LOGGER.error("登录响应JSON解析失败: %s, 响应内容: %s", json_ex, await response.text())
                            self._login_failed_at = time.monotonic()
                            return False

                        # 检查响应是否包含加密数据 (data 和 key 字段)
                        if 'data' in response_json and 'key' in response_json:
                            decrypted = await self._decrypt_payload(response_json)
                            if decrypted:
                                if self._is_debug:
                                    _LOGGER.info("登录响应数据已解密: %s", decrypted)

                                # 检查业务状态码：'0' 表示成功；'200' 且 success=true 为兼容格式
                                if str(decrypted.get('code')) == '0' or (str(decrypted.get('code')) == '200' and decrypted.get('success')):
                                    if self._is_debug:
                                        _LOGGER.info("登录成功")
                                    response_obj = decrypted.get('obj', {})

                                    # 1. 获取 Token：服务器可能放在响应头或 Body 中
                                    self._token = response.headers.get('token')
                                    if not self._token and isinstance(response_obj, dict):
                                        self._token = response_obj.get('token')

                                    # 2. 获取 OpenID/UserId：微信端用户唯一标识，后续请求需携带
                                    if isinstance(response_obj, dict):
                                        user_info = response_obj.get('user', {})
                                        if user_info.get('id'):
                                            self._openid = str(user_info.get('id'))
                                        else:
                                            self._openid = decrypted.get('openId') or decrypted.get('userId')

                                        # 3. 提取户号 (ConsNo)：查询账单、抄表、用气量等接口的必需参数
                                        self._cons_no = user_info.get('consNo')
                                        if not self._cons_no:
                                            # 多户号用户：从 defaultCasCode 中获取默认户号
                                            default_consumer_code = user_info.get('defaultCasCode')
                                            if isinstance(default_consumer_code, dict) and default_consumer_code:
                                                self._cons_no = default_consumer_code.get('consNo')

                                    self._login_failed_at = None
                                    return True
                                else:
                                    error_msg = decrypted.get('msg', '未知错误')
                                    _LOGGER.error("登录业务失败: %s", error_msg)
                                    self._login_failed_at = time.monotonic()
                                    return False

                            # 检查是否为解密错误 (Code 500 + Msg "Decryption error")
                            # 若是服务器密钥过期，清除本地密钥并重试；属正常可恢复场景，不刷屏日志
                            is_decryption_error = str(response_json.get('code')) == '500' and 'Decryption error' in str(response_json.get('msg'))
                            if is_decryption_error:
                                if attempt == 0:
                                    self._crypto.clear_server_key()
                                    continue  # 进行下一次循环（重新握手）
                                # 重试后仍为解密错误，属服务器端问题，仅调试模式记录
                                if self._is_debug:
                                    _LOGGER.info("重试后仍为解密错误(服务器端): %s", response_json)
                                self._login_failed_at = time.monotonic()
                                return False

                            # 检查是否为 RSA 模数超限 (Code 500 + Msg "Message is larger than modulus")
                            # 服务器用客户端公钥加密响应时，若客户端密钥过小会报此错；升级为 2048 位密钥后重试
                            is_modulus_error = str(response_json.get('code')) == '500' and 'modulus' in str(response_json.get('msg', '')).lower()
                            if is_modulus_error:
                                if attempt == 0:
                                    self._crypto.clear_server_key()
                                    loop = asyncio.get_event_loop()
                                    await loop.run_in_executor(None, self._crypto.generate_client_keys)
                                    continue  # 重新握手并重试
                                if self._is_debug:
                                    _LOGGER.info("重试后仍为模数超限(服务器端): %s", response_json)
                                self._login_failed_at = time.monotonic()
                                return False

                            _LOGGER.error("登录响应异常 (非加密或格式错误): %s", response_json)
                            self._login_failed_at = time.monotonic()
                            return False

                        return None

                # ---------- 网络与请求异常处理（属可恢复场景，用 warning 避免刷屏）----------
                except asyncio.TimeoutError:
                    _LOGGER.warning("登录请求超时: URL: %s", url)
                    self._login_failed_at = time.monotonic()
                    return False
                except aiohttp.ClientError as client_ex:
                    _LOGGER.warning("登录网络连接错误: %s, URL: %s", client_ex, url)
                    self._login_failed_at = time.monotonic()
                    return False
                except Exception as e:
                    _LOGGER.warning("登录请求发生未知异常: %s (类型: %s)", e, type(e).__name__)
                    self._login_failed_at = time.monotonic()
                    return False

            return False

    async def _authenticated_request(self, path: str, payload: Dict[str, Any], retry: bool = True) -> Optional[Dict[str, Any]]:
        """
        发送需要认证的请求的通用辅助方法，增强异常处理。

        功能：
        1. 自动检查 Token，如果不存在则自动登录。
        2. 自动处理请求加密和响应解密。
        3. 处理 Token 过期的情况（自动重试）。
        4. 对网络异常进行一次重试，降低短时抖动影响。

        Args:
            path (str): API 路径 (如 '/micro/bill/arrearage')。
            payload (dict): 请求业务参数。
            retry (bool): 失败时是否自动重试 (默认为 True)。

        Returns:
            dict: 解密后的响应数据，如果失败则返回 None。
        """
        async with self._request_lock:
            # 验证输入参数
            if not path or not isinstance(path, str):
                _LOGGER.error("请求失败：无效的路径参数: %s", path)
                return None

            if not isinstance(payload, dict):
                _LOGGER.error("请求失败：无效的载荷参数类型: %s", type(payload).__name__)
                return None

            # 如果没有 Token，先尝试登录
            if not self._token:
                if not await self.login():
                    return None

            # 确保握手状态有效
            if not await self._handshake():
                return None

            # 加密请求载荷
            encrypted_payload = await self._encrypt_payload(payload)
            if not encrypted_payload:
                _LOGGER.error("请求失败：载荷加密失败")
                return None

            url = f'{self._api_base}{path}'
            headers = self._build_headers({
                'secret': self._crypto.secret or '',
                'busyToken': self._busy_token or '',
                'token': self._token,
                'openId': self._openid or ''
            })

            try:
                session = await self._get_session()
                async with session.post(url, json=encrypted_payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:

                    if response.status != 200:
                        # 非 200 可能是 Token 过期或会话失效，清除 Token 后重新登录并重试一次；重试前不记录
                        if retry:
                            self._token = None
                            if await self.login():
                                return await self._authenticated_request(path, payload, retry=False)
                        _LOGGER.warning("请求失败(重试后仍失败)，HTTP状态码: %d, 路径: %s", response.status, path)
                        return None

                    # 解析JSON响应
                    try:
                        response_json = await response.json()
                    except json.JSONDecodeError as json_ex:
                        _LOGGER.error("请求响应JSON解析失败: %s, 路径: %s, 响应内容: %s", json_ex, path, await response.text())
                        return None

                    # 判断是否为加密响应（标准格式包含 data 和 key 字段）
                    if 'data' in response_json and 'key' in response_json:
                        decrypted = await self._decrypt_payload(response_json)
                        if decrypted:
                            if self._is_debug:
                                _LOGGER.info("API 响应解密成功 [%s]: %s", path, decrypted)
                            return decrypted
                        else:
                            # 解密失败可能因 Token 过期或会话被踢，尝试重新登录；重试前不记录
                            if retry:
                                self._token = None  # 清除 Token，强制重新登录
                                if await self.login():
                                    return await self._authenticated_request(path, payload, retry=False)
                            _LOGGER.warning("响应解密失败(重试后仍失败)，路径: %s", path)
                            return None

                    return response_json
            # ---------- 网络异常：尝试重新登录并重试一次 ----------
            except asyncio.TimeoutError:
                # 重试前不记录，只有重试后仍失败才记录
                if retry:
                    self._token = None
                    if await self.login():
                        return await self._authenticated_request(path, payload, retry=False)
                _LOGGER.warning("请求超时(重试后仍失败): 路径: %s", path)
                return None
            except aiohttp.ClientError as client_ex:
                if retry:
                    self._token = None
                    if await self.login():
                        return await self._authenticated_request(path, payload, retry=False)
                _LOGGER.warning("网络连接错误(重试后仍失败): %s, 路径: %s", client_ex, path)
                return None
            except Exception as e:
                if retry:
                    self._token = None
                    if await self.login():
                        return await self._authenticated_request(path, payload, retry=False)
                _LOGGER.warning("请求发生未知异常(重试后仍失败): %s (类型: %s), 路径: %s", e, type(e).__name__, path)
                return None

    async def get_arrearage(self) -> Optional[Dict[str, Any]]:
        """
        获取当前欠费和余额信息。

        用于展示用户账户的预存余额及欠费金额，多户号时需指定 consNo。

        API: /micro/bill/arrearage

        Returns:
            dict: 包含 balance(余额)、arrearage(欠费) 等字段的字典，失败返回 None。
        """
        if not self._token:
            await self.login()
        payload = {}
        if self._cons_no:
            payload['consNo'] = self._cons_no  # 多户号时指定查询目标
        return await self._authenticated_request('/micro/bill/arrearage', payload)

    def _get_date_range(self, months: int = 6) -> tuple:
        """
        辅助方法：获取最近 N 个月的日期范围字符串。
        
        格式: YYYY-MM-DD，适用于 feeRecord、payMentRecord 等接口的 beginTime/endTime。
        按 30 天/月近似计算，适用于一般查询场景。
        
        Args:
            months (int): 回溯的月数，默认 6 个月。
            
        Returns:
            tuple: (开始日期, 结束日期)，如 ('2024-09-07', '2025-03-07')
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30 * months)
        return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')

    def _get_month_range(self, months: int = 6) -> tuple:
        """
        辅助方法：获取最近 N 个月的月份范围字符串。
        
        格式: YYYYMM（无分隔符），适用于抄表记录接口 mrInfo 的 startYm/endYm 参数。
        
        Args:
            months (int): 回溯的月数，默认 6 个月。
            
        Returns:
            tuple: (开始月份, 结束月份)，如 ('202409', '202503')
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30 * months)
        return start_date.strftime('%Y%m'), end_date.strftime('%Y%m')

    async def get_fee_record(self, page: int = 1, rows: int = 10, months: int = 6) -> Optional[Dict[str, Any]]:
        """
        获取历史账单记录（月度账单列表）。

        用于展示用户各月的燃气费用明细，支持分页。

        API: /micro/bill/feeRecord

        Args:
            page (int): 页码，从 1 开始。
            rows (int): 每页条数。
            months (int): 查询最近多少个月的数据。

        Returns:
            dict: 包含账单列表的响应，失败返回 None。
        """
        if not self._token:
            await self.login()
        start_time, end_time = self._get_date_range(months)
        payload = {
            "pageNo": page,
            "pageSize": rows,
            "feeType": 0,           # 费用类型，0 表示燃气费
            "beginTime": start_time,
            "endTime": end_time
        }
        if self._cons_no:
            payload['consNo'] = self._cons_no
        return await self._authenticated_request('/micro/bill/feeRecord', payload)

    async def get_meter_info(self, months: int = 6) -> Optional[Dict[str, Any]]:
        """
        获取抄表记录（含月度用气量）。

        用于展示每月燃气表读数及对应用气量，支持最多约 2 年数据（24 条）。

        API: /micro/readMeter/mrInfo

        Args:
            months (int): 查询最近多少个月的数据。

        Returns:
            dict: 抄表记录列表，失败返回 None。
        """
        if not self._token:
            await self.login()
        start_ym, end_ym = self._get_month_range(months)
        payload = {
            "pageSize": 24,   # 最多返回 24 条（约 2 年），满足常规需求
            "startYm": start_ym,
            "endYm": end_ym
        }
        if self._cons_no:
            payload['consNo'] = self._cons_no
        return await self._authenticated_request('/micro/readMeter/mrInfo', payload)

    async def get_payment_record(self, page: int = 1, rows: int = 10, months: int = 6) -> Optional[Dict[str, Any]]:
        """
        获取缴费记录。

        用于展示用户历史充值/缴费记录，支持分页与时间范围筛选。

        API: /micro/bill/payMentRecord

        Args:
            page (int): 页码。
            rows (int): 每页条数。
            months (int): 查询最近多少个月的数据。

        Returns:
            dict: 缴费记录列表，失败返回 None。
        """
        if not self._token:
            await self.login()
        start_time, end_time = self._get_date_range(months)
        payload = {
            "pageNo": page,
            "pageSize": rows,
            "beginTime": start_time,
            "endTime": end_time
        }
        if self._cons_no:
            payload['consNo'] = self._cons_no
        return await self._authenticated_request('/micro/bill/payMentRecord', payload)

    async def get_daily_usage(self, days: int = 30) -> Optional[Dict[str, Any]]:
        """
        获取日用气量数据。

        按天统计用气量，用于图表展示或用量分析。接口要求时间范围为 yyyy-MM-dd，
        且通常不包含当天（以昨天为结束日）。若请求范围过大返回 400，会自动缩小为 30 天重试。

        API: /gasVolumeFill/gasDayNum

        Args:
            days (int): 查询过去多少天的数据，默认 30 天。

        Returns:
            dict: 日用气量列表，失败返回 None。
        """
        if not self._token:
            await self.login()
        path = "/gasVolumeFill/gasDayNum"

        async def _fetch_daily_usage(query_days: int):
            """
            内部函数：按指定天数构建查询区间并发起请求。
            复用 _authenticated_request 的加密与认证逻辑。
            接口要求日期格式为 yyyy-MM-dd，时间范围过大可能返回 400。

            Args:
                query_days: 查询过去多少天的日用气量。
            """
            safe_days = max(1, query_days)  # 至少 1 天，避免 0 或负数
            now_local = dt_util.now()
            end_date = now_local.date()   # 结束日（通常为昨天，不含当天）
            start_date = end_date - timedelta(days=safe_days - 1)

            payload = {
                "startTime": start_date.strftime('%Y-%m-%d'),
                "endTime": end_date.strftime('%Y-%m-%d'),
                "beginTime": start_date.strftime('%Y-%m-%d'),  # 兼容不同后端字段名
            }
            if self._cons_no:
                payload['consNo'] = self._cons_no
            if self._is_debug:
                _LOGGER.info("尝试获取日用气量: %s payload=%s", path, payload)
            return await self._authenticated_request(path, payload)

        try:
            result = await _fetch_daily_usage(days)
            # 若返回 400（时间范围不合法或过大），且请求天数 > 30，则自动缩小为 30 天重试
            if result is not None and str(result.get('code')) == '400' and days > 30:
                _LOGGER.warning("获取日用气量失败 (范围 %d 天): %s，尝试自动缩小范围为 30 天", days, result.get('msg'))
                result = await _fetch_daily_usage(30)
            if result and (str(result.get('code')) == '200' or result.get('success')):
                return result
            _LOGGER.warning("获取日用气量失败: %s", result)
            return None
        except Exception as e:
            _LOGGER.error("获取日用气量异常: %s", e)
            return None

    async def close(self) -> None:
        """
        关闭 aiohttp 会话，释放资源。

        应在集成卸载或不再需要 API 时调用，避免资源泄漏。
        """
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
