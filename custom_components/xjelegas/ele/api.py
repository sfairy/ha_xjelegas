"""
新疆电力 (95598.cn) API 接口模块。

本模块负责与国网开放平台 API 的完整交互，包括：
1. 登录流程：获取密钥 -> 点选/滑块验证码（LLM 识别）-> 密码验证 -> OAuth2 授权 -> 获取 Token
2. RK001 流控：手机号登录受限时自动降级备用邮箱登录
3. 数据获取：余额、年度/月度账单、日用电量、交费记录（预留）
4. 加解密：SM4 对称加密请求体、SM2 加密密钥、SM3 签名
5. 会话管理：Token 过期自动重登、持久化存储、DataUpdateCoordinator 定时刷新

API 基础地址：https://www.95598.cn/api
加密算法：国密 SM2/SM3/SM4，详见 ele.crypto 模块
"""
from __future__ import annotations  # 延迟类型注解求值，支持前向引用

# 标准库
import json          # JSON 序列化/反序列化，请求体与响应解析
import os            # 环境变量读取，用于 API 凭证安全注入
import time          # 时间戳（毫秒），请求签名与刷新间隔判断
import urllib.parse  # URL 编码，OAuth2 授权接口 form-urlencoded
import datetime      # 日期计算，账单周期、保留天数等
import logging       # 日志
import hashlib       # MD5，登录密码加密
import io            # BytesIO，Base64 图片转 PIL 处理
import base64        # Base64 编解码，验证码图片
from datetime import timedelta  # 时间间隔，协调器刷新周期
import asyncio       # 异步睡眠，登录重试间隔
import random        # 随机数，请求间隔抖动
from typing import Dict, Any  # 类型注解

from PIL import Image  # 图像处理，滑块验证码缺口识别
import aiohttp         # 异步 HTTP（HA 的 async_get_clientsession 基于此）

# Home Assistant
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components import persistent_notification

# 本集成
from .const import (
    VERSION, PACKAGE_NAME, DOMAIN, MAX_RETRIES, CONF_IS_PREPAID, CONF_PRICE,
    CONF_ARK_API_KEY, CONF_ARK_MODEL, CONF_ARK_BASE_URL, CONF_EMAIL_ACCOUNT,
    FLOW_CONTROL_CODES, RETENTION_DAYS, RETENTION_MONTHS,
    MIN_REFRESH_INTERVAL, MAX_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL,
    CONF_REFRESH_INTERVAL,
)
from .storage import async_save_to_store
from .crypto import sm4_encrypt, sm4_decrypt, sm3_hash, sm2_encrypt, generate_random_string
from . import captcha_solver as _captcha_solver
from .captcha_solver import CaptchaSolver

LOGGER = logging.getLogger(PACKAGE_NAME)

# ==========================================
# API 静态配置 (configuration)
# ==========================================
# 国网 95598 开放平台要求的业务元数据，各接口按需引用。
# uscInfo: 用户/设备信息，member=渠道标识，tenant=租户
configuration = {
    # 通用用户/设备信息，多数接口的 quInfo/uscInfo 需引用
    'uscInfo': {
        'member': '0902',       # 渠道成员标识（0902=APP 渠道）
        'devciceIp': '',        # 设备 IP（可为空）
        'devciceId': '',        # 设备 ID（可为空）
        'tenant': 'state_grid' # 租户标识（新疆电力）
    },
    'source': 'SGAPP',          # 来源渠道：SGAPP=新疆电力 APP
    'target': '32101',          # 目标系统编码
    'channelCode': '0902',      # 渠道编码
    'channelNo': '0902',        # 渠道编号
    'toPublish': '01',          # 发布标识
    'siteId': '2012000000033700',  # 站点 ID
    'srvCode': '',              # 服务编码（按接口动态填充）
    'serialNo': '',             # 流水号（按接口动态填充）
    'funcCode': '',             # 功能编码（按接口动态填充）
    # 各业务接口对应的服务编码
    'serviceCode': {
        'order': '0101154',     # 订单相关
        'uploadPic': '0101296', # 上传图片
        'pauseSCode': '0101250',# 暂停服务
        'pauseTCode': '0101251',# 暂停类型
        'listconsumers': '0101093',  # 用户列表
        'messageList': '0101343',    # 消息列表
        'submit': '0101003',    # 提交
        'sbcMsg': '0101210',    # 消息
        'powercut': '0104514',  # 停电相关
        'BkAuth01': 'f15',      # 后台授权 01-08
        'BkAuth02': 'f18',
        'BkAuth03': 'f02',
        'BkAuth04': 'f17',
        'BkAuth05': 'f05',
        'BkAuth06': 'f16',
        'BkAuth07': 'f01',
        'BkAuth08': 'f03'
    },
    'electricityArchives': {'servicecode': '0104505', 'source': '0902'},  # 电力档案
    'subscriptionList': {'srvCode': 'APP_SGPMS_05_030', 'serialNo': '22', 'channelCode': '0902', 'funcCode': '22', 'target': '-1'},  # 订阅列表
    'userInformation': {'serviceCode': '01008183', 'source': 'SGAPP'},   # 用户信息（详细）
    'userInform': {'serviceCode': '0101183', 'source': 'SGAPP'},          # 用户信息（简要）
    'elesum': {  # 电费汇总
        'channelCode': '0902',
        'funcCode': 'WEBALIPAY_01',
        'promotCode': '1',
        'promotType': '1',
        'serviceCode': '0101143',
        'source': 'app'
    },
    'account': {'channelCode': '0902', 'funcCode': 'WEBA1007200'},  # 账户相关
    'doorNumberManeger': {  # 户号管理
        'source': '0902',
        'target': '-1',
        'channelCode': '09',
        'channelNo': '09',
        'serviceCode': '01010049',
        'funcCode': 'WEBA40050000',
        'uscInfo': {'member': '0902', 'devciceIp': '', 'devciceId': '', 'tenant': 'state_grid'}
    },
    'doorAuth': {'source': 'SGAPP', 'serviceCode': 'f04'},  # 户号授权
    'xinZ': {  # 新装业务
        'serCat': '101',
        'jM_busiTypeCode': '101',
        'fJ_busiTypeCode': '102',
        'jM_custType': '03',
        'fJ_custType': '02',
        'serviceType': '01',
        'subBusiTypeCode': '',
        'funcCode': 'WEBA10070700',
        'order': '0101154',
        'source': 'SGAPP',
        'querytypeCode': '1'
    },
    'onedo': {'serviceCode': '0101046', 'source': 'SGAPP', 'funcCode': 'WEBA10070700', 'queryType': '03'},  # 一站式办理
    'xinHuTongDian': {  # 新户通电
        'serCat': '110',
        'busiTypeCode': '211',
        'subBusiTypeCode': '21102',
        'funcCode': 'WEBA10071200',
        'channelCode': '0902',
        'source': '09',
        'serviceCode': '0101183'
    },
    'company': {  # 企业用户
        'serCat': '104',
        'funcCode': 'WEBA10070700',
        'serviceType': '02',
        'querytypeCode': '1',
        'authFlag': '1',
        'source': 'SGAPP',
        'order': '0101154'
    },
    'charge': {  # 交费记录
        'channelCode': '09',
        'funcCode': 'WEBA10071300',
        'channelNo': '0901',
        'source': '0901',
        'serviceCode': '0101143'
    },
    'getday': {  # 日用电量查询
        'funcCode': 'WEBALIPAY_02',
        'channelCode': '0902',
        'clearCache': '11',
        'promotCode': '1',
        'promotType': '1',
        'serviceCode': '0101143',
        'source': 'app'
    },
    'stepelect': {  # 阶梯/抄表（c04/f03）
        'channelCode': '0902',
        'funcCode': 'WEBALIPAY_01',
        'promotType': '1',
        'clearCache': '09',
        'serviceCode': 'BCP_000026',
        'source': 'app',
    },
}

# API 凭证与路径
# 国网开放平台分配的 appKey/appSecret，用于握手与签名
# 优先从环境变量读取，避免凭证硬编码泄露；未设置时使用默认值（仅开发/测试用途）
appKey = os.environ.get('XJELEGAS_APP_KEY', '7e5b5e84ddad4994b0ebc68dedca4962')
appSecret = os.environ.get('XJELEGAS_APP_SECRET', '2bc37a881e1541aaa6e6e174658d150b')
baseApi = 'https://www.95598.cn/api'

# 请求超时（秒），与 state_grid 一致，避免连接挂起
REQUEST_TIMEOUT = 30

# 核心 API 路径（登录与数据获取流程）
get_request_key_api = '/oauth2/outer/c02/f02'             # Step1: 获取加密密钥与公钥
get_verify_code_api = '/osg-web0004/open/c44/f05'         # Step2: 获取验证码图片
verify_password_api = '/osg-web0004/open/c44/f06'         # Step3: 提交密码与验证码
click_card_api = '/osg-web0004/open/c44/f07'              # Step3b: 点选验证码专用端点
get_request_authorize_api = '/oauth2/oauth/authorize'     # Step4: OAuth2 授权码
get_web_token_api = '/oauth2/outer/getWebToken'           # Step5: 换取 accessToken/refreshToken
get_door_number_api = '/osg-open-uc0001/member/c9/f02'    # 获取用户绑定的户号列表
get_door_balance_api = '/osg-open-bc0001/member/c05/f01'  # 获取指定户号余额
get_door_bill_api = '/osg-open-bc0001/member/c01/f02'     # 获取年度/月度账单
get_door_ladder_api = '/osg-open-bc0001/member/c04/f03'   # 阶梯电价/抄表读数（对齐 state_grid）
get_pay_record_api = get_door_ladder_api                 # 兼容旧名
get_door_daily_bill_api = '/osg-web0004/member/c24/f01'   # 获取日用电量（近 30 天）

# API 权限控制列表：决定各接口请求头需携带的字段
# sessionId: 验证码/密码验证类接口需要
# keyCode: 加密相关接口需要
# Authorization: 业务数据接口需要 Bearer token
# t: 业务 token 前半段
sessionIdControlApiList = [verify_password_api, get_verify_code_api, click_card_api]
keyCodeControlApiList = [verify_password_api, get_verify_code_api, get_request_authorize_api, get_web_token_api, get_door_number_api, get_door_balance_api, get_door_bill_api, get_door_daily_bill_api, get_door_ladder_api, click_card_api]
authControlApiList = [get_door_number_api, get_door_balance_api, get_door_bill_api, get_door_ladder_api, get_door_daily_bill_api]
tControlApiList = [get_door_number_api, get_door_balance_api, get_door_bill_api, get_door_ladder_api, get_door_daily_bill_api]

# ==========================================
# 反爬虫配置
# ==========================================
# 模拟浏览器请求头，避免被识别为爬虫
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Edg/120.0.0.0',
]

# 请求间隔配置（毫秒）
MIN_REQUEST_INTERVAL = 200   # 最小请求间隔（登录流程内部使用）
MAX_REQUEST_INTERVAL = 500   # 最大请求间隔（登录流程内部使用）
NORMAL_REQUEST_INTERVAL = 1000  # 正常业务请求间隔

# 登录重试间隔配置（秒）- 指数退避策略
INITIAL_LOGIN_RETRY_DELAY = 5    # 初始重试间隔
MAX_LOGIN_RETRY_DELAY = 30       # 最大重试间隔
LOGIN_RETRY_MULTIPLIER = 2       # 重试间隔倍增系数

# 登录失败冷却期（秒）- 连续失败后的强制等待时间
LOGIN_COOLDOWN_PERIOD = 60       # 连续登录失败后冷却60秒

# 登录失败计数器相关常量
MAX_LOGIN_FAILURES_BEFORE_LONG_COOLDOWN = 3  # 连续失败3次后进入长冷却期
LONG_COOLDOWN_PERIOD = 300  # 长冷却期5分钟

# 请求来源标识
REFERER = 'https://www.95598.cn/'
ORIGIN = 'https://www.95598.cn'

# ==========================================
# 辅助函数
# ==========================================

def json_dumps(data):
    """JSON 序列化，去除空格以减小请求体体积，符合国网 API 要求。"""
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False)

def normal_round(num, ndigits=0):
    """
    四舍五入函数，避免 Python 内置 round 的银行家舍入问题。
    
    Args:
        num: 待处理的数字（支持 int/float）
        ndigits: 保留的小数位数，0 表示取整
        
    Returns:
        四舍五入后的数值
    """
    if ndigits == 0:
        return int(num + 0.5)
    else:
        multiplier = 10 ** ndigits
        return int(num * multiplier + 0.5) / multiplier

def catchFloat(data, key):
    """
    从字典中安全获取浮点数，失败或缺失时返回 0。
    用于解析 API 返回的金额、电量等数值字段。
    """
    if key in data:
        try:
            return normal_round(float(data[key]), 2)
        except (ValueError, TypeError):
            return 0
    else:
        return 0

def catchInt(data, key):
    """
    从字典中安全获取整数，失败或缺失时返回 0。
    用于解析 API 返回的计数、月份等整数字段。
    """
    if key in data:
        try:
            return normal_round(float(data[key]), 0)
        except (ValueError, TypeError):
            return 0
    else:
        return 0

def _normalize_day_key(value) -> str:
    """将日期统一为 YYYY-MM-DD 格式，用于排序与比较。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if " " in value:
        value = value.split(" ", 1)[0]
    if "T" in value:
        value = value.split("T", 1)[0]
    value = value.replace("/", "-").replace(".", "-")
    digits = value.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    parts = [p for p in value.split("-") if p]
    if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[0].zfill(4)}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
    return value

def _day_key_from_bill(bill) -> str:
    """从日账单记录中提取日期并标准化。"""
    for key in ('day', 'date', 'rq', 'readDate', 'chargeDate'):
        if key in bill and bill[key]:
            return _normalize_day_key(bill[key])
    return ""

def get_month_date_range(date_str):
    """
    根据 YYYYMM 格式的月份字符串，计算该月的起止日期。
    
    Args:
        date_str: 月份字符串，格式为 YYYYMM（如 202501）
        
    Returns:
        tuple: (year, first_day, last_day)
            - year: 年份
            - first_day: 该月第一天（date 对象）
            - last_day: 该月最后一天（date 对象）
    """
    year = int(date_str[:4])
    month = int(date_str[4:])
    first_day = datetime.date(year, month, 1)
    if month == 12:
        next_month = 1
        next_year = year + 1
    else:
        next_month = month + 1
        next_year = year
    last_day = datetime.date(next_year, next_month, 1) - datetime.timedelta(days=1)
    return year, first_day, last_day


def base64_image_to_bytes(base64_data):
    """Base64 图片数据转 bytes。"""
    data = base64_data
    if data.startswith('data:image'):
        comma_index = data.find(',')
        if comma_index != -1:
            data = data[comma_index + 1:]
    return base64.b64decode(data)


def is_dark(pixel, threshold=100, method='brightness'):
    """判断像素是否为深色（滑块像素算法用）。"""
    if len(pixel) == 4:
        r, g, b, a = pixel
        if a < 128:
            return False
    else:
        r, g, b = pixel
    if method == 'brightness':
        value = max(r, g, b)
    elif method == 'average':
        value = (r + g + b) // 3
    elif method == 'max':
        value = max(r, g, b)
    elif method == 'perceived':
        value = int(0.299 * r + 0.587 * g + 0.114 * b)
    else:
        raise ValueError(f"未知方法: {method}")
    return value < threshold


def find_max_rectangle(matrix):
    """在二进制矩阵中找最大矩形，返回 (top, left, bottom, right)。"""
    if not matrix or not matrix[0]:
        return 0, 0, 0, 0
    rows, cols = len(matrix), len(matrix[0])
    heights = [0] * cols
    max_area = 0
    best = 0, 0, 0, 0
    for row in range(rows):
        for col in range(cols):
            heights[col] = heights[col] + 1 if matrix[row][col] == 1 else 0
        stack = []
        for col in range(cols + 1):
            current = heights[col] if col < cols else -1
            while stack and current < heights[stack[-1]]:
                h_idx = stack.pop()
                height = heights[h_idx]
                width = col if not stack else col - stack[-1] - 1
                area = height * width
                if area > max_area:
                    max_area = area
                    top = row - height + 1
                    left = col - width
                    best = top, left, row, col - 1
            stack.append(col)
    return best

# ==========================================
# 核心客户端类
# ==========================================

class StateGridDataClient:
    """
    新疆电力 API 客户端.
    
    处理所有与国网 API 的交互逻辑，包括：
    1. 登录流程 (获取 Key, 验证码识别, 密码验证, 获取 Token)
    2. 数据获取 (户号, 余额, 账单, 日用电)
    3. 数据存储与持久化
    
    所有状态均为实例属性，确保多实例隔离。
    """

    def __init__(self, hass, config=None, store_key=None):
        """
        初始化客户端.
        
        Args:
            hass: HomeAssistant 实例
            config: 恢复的配置字典
            store_key: 存储键名
        """
        self.hass = hass
        self.coordinator = None
        self.session = None
        self.dataVersion = None
        self.keyCode = None
        self.publicKey = None
        self.need_login = False
        self.phone = None
        self.codeKey = None
        self.serialNo = None
        self.qrCodeSerial = None
        self.ticket = None
        self.userInfo = None
        self.accountInfo = None
        self.powerUserList = None
        self.doorAccountDict = {}
        self.cookie = []
        self.timestamp = int(time.time() * 1000)
        self.accessToken = None
        self.refreshToken = None
        self.token = None
        self.expirationDate = None
        self.refresh_interval = DEFAULT_REFRESH_INTERVAL
        self.is_debug = False
        self.shown_notification = False
        self.price = 0.475
        self.is_prepaid = None
        self.account = None
        self.password = None
        self.store_key = store_key
        self.last_door_list_update_time = 0
        self._last_login_failure_time = 0  # 登录失败时间戳（实例级），避免多实例共享
        self._login_in_progress = False    # 登录进行中标记，防止并发登录
        self._login_lock = asyncio.Lock()  # 登录操作互斥锁，防止并发登录导致状态不一致
        self.email_account = ""            # 备用邮箱（RK001 流控降级用）
        self._rk001_cooldown_until = 0.0   # RK001 冷却截止时间戳
        # 大模型验证码解算器
        ark_api_key = (config or {}).get(CONF_ARK_API_KEY, '')
        ark_model = (config or {}).get(CONF_ARK_MODEL, '')
        ark_base_url = (config or {}).get(CONF_ARK_BASE_URL, '')
        self.captcha_solver = CaptchaSolver(api_key=ark_api_key, model=ark_model, base_url=ark_base_url)
        if ark_api_key:
            _captcha_solver.configure_llm(ark_api_key, ark_base_url, ark_model)
        
        # 恢复已保存的状态
        if config is not None:
            try:
                self.keyCode = config.get('keyCode')
                self.publicKey = config.get('publicKey')
                self.accessToken = config.get('accessToken')
                self.refreshToken = config.get('refreshToken')
                self.token = config.get('token')
                self.userInfo = config.get('userInfo')
                self.powerUserList = config.get('powerUserList')
                self.doorAccountDict = config.get('doorAccountDict', {})
                self.is_debug = config.get('is_debug', False)
                self.dataVersion = config.get('dataVersion')
                self.account = config.get('account')
                self.password = config.get('password')
                self.refresh_interval = config.get(CONF_REFRESH_INTERVAL, config.get('refresh_interval', DEFAULT_REFRESH_INTERVAL))
                self.price = config.get(CONF_PRICE, config.get("price", 0.475))
                self.is_prepaid = config.get(CONF_IS_PREPAID)
                self.email_account = config.get(CONF_EMAIL_ACCOUNT, '')
                self._rk001_cooldown_until = config.get('_rk001_cooldown_until', 0.0)
                saved_ts = config.get('timestamp')
                if saved_ts and isinstance(saved_ts, (int, float)) and saved_ts > 0:
                    self.timestamp = int(saved_ts)
                if self.refresh_interval < MIN_REFRESH_INTERVAL:
                    self.refresh_interval = MIN_REFRESH_INTERVAL
                elif self.refresh_interval > MAX_REFRESH_INTERVAL:
                    self.refresh_interval = MAX_REFRESH_INTERVAL
            except Exception as e:
                LOGGER.error(e)

    async def save_data(self):
        """保存当前会话数据到本地存储，以便重启后恢复。"""
        data = {}
        data['keyCode'] = self.keyCode
        data['publicKey'] = self.publicKey
        data['accessToken'] = self.accessToken
        data['refreshToken'] = self.refreshToken
        data['token'] = self.token
        data['userInfo'] = self.userInfo
        data['powerUserList'] = self.powerUserList
        data['doorAccountDict'] = self.doorAccountDict
        data['is_debug'] = self.is_debug
        data['dataVersion'] = VERSION
        data['account'] = self.account
        data['password'] = self.password
        data['refresh_interval'] = self.refresh_interval
        data['price'] = self.price
        data[CONF_IS_PREPAID] = self.is_prepaid
        data[CONF_ARK_API_KEY] = self.captcha_solver.api_key
        data[CONF_ARK_MODEL] = self.captcha_solver.model
        data[CONF_ARK_BASE_URL] = self.captcha_solver.base_url
        data[CONF_EMAIL_ACCOUNT] = self.email_account
        data['_rk001_cooldown_until'] = self._rk001_cooldown_until
        data['timestamp'] = self.timestamp
        await async_save_to_store(self.hass, self.store_key, data)

    async def _add_random_delay(self, use_normal_interval=False):
        """
        添加随机请求间隔，避免被识别为爬虫。
        
        Args:
            use_normal_interval: 是否使用正常业务请求间隔（1-3秒），默认使用短间隔（200-500ms）
        """
        if use_normal_interval:
            delay_ms = random.randint(NORMAL_REQUEST_INTERVAL, 3000)
        else:
            delay_ms = random.randint(MIN_REQUEST_INTERVAL, MAX_REQUEST_INTERVAL)
        delay_sec = delay_ms / 1000.0
        await asyncio.sleep(delay_sec)
        LOGGER.debug(f"随机延迟 {delay_ms}ms")

    async def _enforce_login_cooldown(self):
        """
        强制执行登录冷却期，避免连续失败触发风控。
        
        检查距上次登录失败是否已过冷却期，未过时等待至冷却期满。
        使用实例属性 _last_login_failure_time 避免多实例冲突。
        """
        now = time.time()
        elapsed = now - self._last_login_failure_time
        
        if elapsed < LOGIN_COOLDOWN_PERIOD:
            wait_time = LOGIN_COOLDOWN_PERIOD - elapsed
            LOGGER.info(f"登录冷却期未过，等待 {wait_time:.1f} 秒")
            await asyncio.sleep(wait_time)
        
        self._last_login_failure_time = now

    def encrypt_post_data(self, data):
        """
        加密 POST 请求数据.
        
        国网 API 要求请求体必须经过加密，且包含时间戳和 token 片段。
        """
        wrapper_data = {
            '_access_token': self.accessToken[len(self.accessToken) // 2:] if self.accessToken else '',
            '_t': self.token[len(self.token) // 2:] if self.token else '',
            '_data': data,
            'timestamp': self.timestamp
        }
        return self.encrypt_wapper_data(wrapper_data)

    def encrypt_wapper_data(self, data):
        """
        加密包装后的数据（国网请求体标准格式）.
        
        流程：
        1. 使用 SM4 对称加密 JSON 数据
        2. 生成 SM3 签名：encrypted_data + timestamp 的哈希，追加到密文后
        3. 使用 SM2 公钥加密 keyCode 得到 skey，供服务端解密
        
        Returns:
            dict: {'data': 密文+签名, 'skey': 加密后的密钥, 'timestamp': 时间戳}
        """
        encrypted_data = sm4_encrypt(json_dumps(data), self.keyCode)
        return {
            'data': encrypted_data + sm3_hash(encrypted_data + str(self.timestamp)),
            'skey': sm2_encrypt(self.keyCode, self.publicKey),
            'timestamp': str(self.timestamp)
        }

    def handle_request_result_message(self, api, result, printResult=True):
        """
        从 API 响应中提取可读的错误/结果消息，用于日志和用户提示。
        
        Args:
            api: 接口路径，用于日志标识
            result: API 返回的原始响应（dict 或其它）
            printResult: 是否在调试模式下打印完整响应
            
        Returns:
            str: 提取的消息文本，无法提取时返回 JSON 序列化结果
        """
        msg_key = 'message'
        res_msg_key = 'resultMessage'
        
        if self.is_debug and printResult:
            LOGGER.warning(api + '-' + json_dumps(result))

        if not isinstance(result, dict):
            return json_dumps(result)
        
        msg = None
        if 'data' in result and result['data'] and 'srvrt' in result['data'] and res_msg_key in result['data']['srvrt']:
            msg = result['data']['srvrt'][res_msg_key]
        elif 'data' in result and isinstance(result['data'], dict) and res_msg_key in result['data']:
            msg = result['data'][res_msg_key]
        elif 'srvrt' in result and res_msg_key in result['srvrt']:
            msg = result['srvrt'][res_msg_key]
        elif msg_key in result:
            msg = result[msg_key]
        else:
            msg = json_dumps(result)
        return msg

    async def _fetch_safe(self, api, data):
        """
        安全请求封装：Token 过期时自动重登；RK001 流控时尝试邮箱降级。
        """
        response = await self._fetch(api, data)
        if not isinstance(response, dict):
            return response
        if 'code' not in response:
            return response

        code_val = response['code']
        try:
            if int(code_val) in FLOW_CONTROL_CODES:
                LOGGER.warning(
                    "[RK001] 数据API遇流控(code=%s), email=%s, cooldown=%s",
                    code_val, self.email_account or '(未配置)', self.is_rk001_cooldown(),
                )
                if self.email_account and not self.is_rk001_cooldown():
                    LOGGER.info("[RK001] 尝试邮箱降级登录...")
                    if await self._try_email_fallback_login():
                        return await self._fetch(api, data)
                self._set_rk001_cooldown()
                self.need_login = True
                self._show_token_notification(msg='密码登录日额度已用完(RK001)，请等待明日0点自动重试')
                return response
        except (ValueError, TypeError):
            pass

        if self._need_login(code_val):
            async with self._login_lock:
                if self._login_in_progress:
                    LOGGER.info("登录正在进行中，等待完成后重试")
                    await asyncio.sleep(3)
                    return response

                self._login_in_progress = True
                try:
                    await self._try_password_login()
                    if self.need_login is False:
                        return await self._fetch(api, data)
                    if self.need_login is True:
                        self._show_token_notification()
                finally:
                    self._login_in_progress = False
            return response
        return response

    def _need_login(self, code):
        """判断响应码是否表示需要重新登录（排除 RK001 流控码）。"""
        try:
            if int(code) in FLOW_CONTROL_CODES:
                return False
        except (ValueError, TypeError):
            pass
        if code in (10015, 10108, 10009, 10207, 10005, 10010, 30010, 10002):
            self.need_login = True
            return True
        return False

    async def _try_password_login(self):
        """尝试使用保存的密码自动重新登录（RK001 冷却感知）。"""
        if self.is_rk001_cooldown():
            if self.email_account:
                LOGGER.info("[RK001冷却] 手机号在冷却期，尝试邮箱降级登录...")
                if await self._try_email_fallback_login():
                    return
            else:
                LOGGER.warning("[RK001冷却] 跳过密码登录，未配置邮箱降级，等待明日0点")
            return

        # force_refresh=True：业务接口已判定需重登，不可因内存中仍有旧 Token 而跳过
        result = await self.password_login(
            self.account, self.password, encode=True, max_retry=3, force_refresh=True
        )
        if result.get('errcode') == 0:
            self.need_login = False
            self.shown_notification = False
            await self.save_data()
            return

        if self._is_rk001_error(result):
            LOGGER.warning("[RK001] 手机号密码登录遇流控，尝试邮箱降级...")
            if await self._try_email_fallback_login():
                return
            self._set_rk001_cooldown()

    async def _try_email_fallback_login(self):
        """尝试邮箱降级登录。"""
        if not self.email_account:
            LOGGER.warning("[邮箱降级] 未配置备用邮箱")
            return False
        if not self.password:
            LOGGER.warning("[邮箱降级] 密码为空")
            return False
        try:
            LOGGER.info("[邮箱降级] 使用邮箱 %s 登录...", self.email_account)
            result = await self.password_login(
                self.email_account, self.password, encode=True, max_retry=2, force_refresh=True
            )
            if result.get('errcode') == 0:
                self.need_login = False
                self.shown_notification = False
                LOGGER.info("[邮箱降级] 登录成功!")
                await self.save_data()
                return True
            errmsg = result.get('errmsg', '')
            LOGGER.warning("[邮箱降级] 登录失败: %s", errmsg)
            if self._is_rk001_error(result):
                self._set_rk001_cooldown()
            return False
        except Exception as exc:
            LOGGER.exception("[邮箱降级] 登录异常: %s", exc)
            return False

    @staticmethod
    def _is_rk001_error(result):
        """检查结果是否为 RK001 流控错误（只检查 code 字段，避免误判）。"""
        code = result.get('code') or result.get('raw_code') or result.get('errcode')
        if code is not None:
            try:
                if int(code) in FLOW_CONTROL_CODES:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    @staticmethod
    def _looks_like_rk001_message(result):
        """配置流程用：结合 code 与 errmsg 判断 RK001。"""
        if StateGridDataClient._is_rk001_error(result):
            return True
        errmsg = result.get('errmsg') or ''
        return 'RK001' in errmsg or '流控' in errmsg or '日额度' in errmsg

    def is_rk001_cooldown(self):
        """检查当前是否处于 RK001 冷却期。"""
        if self._rk001_cooldown_until <= 0:
            return False
        now = time.time()
        if now >= self._rk001_cooldown_until:
            self._rk001_cooldown_until = 0.0
            return False
        return True

    def _set_rk001_cooldown(self):
        """设置 RK001 冷却到当天 23:59:59（北京时间）。"""
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        self._rk001_cooldown_until = end_of_day.timestamp()
        LOGGER.warning(
            "[RK001冷却] 密码登录日额度已用完，冷却至 %s（北京时间），期间不再尝试密码登录",
            end_of_day.strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _show_token_notification(self, msg=None):
        """Token 过期且自动重登失败时展示持久化通知。"""
        if self.shown_notification:
            return
        self.shown_notification = True
        if msg is None:
            msg = '新疆电力登录失败，将在下个轮询重试'
        persistent_notification.create(
            self.hass,
            msg,
            title='新疆电力 - 登录失败'
        )
        LOGGER.error(msg)

    async def _fetch(self, api, data, header=None):
        """
        核心 HTTP 请求方法，负责与国网 API 的完整通信流程。
        
        流程：
        1. 构造通用请求头（source、version、timestamp、appKey）
        2. 根据 API 类型进行差异化处理：
           - 握手接口：加密 client_id/client_secret，使用固定公钥
           - 授权接口：x-www-form-urlencoded，带 keyCode
           - Token 接口：加密 code 等参数
           - 业务接口：标准 encrypt_post_data 加密
        3. 按 API 类型添加 sessionId、keyCode、Authorization、t 等头
        4. 发送 POST 请求，若响应含 encryptData 则解密后返回
        
        Args:
            api: 接口路径（如 get_door_balance_api）
            data: 请求体数据（dict）
            header: 可选的额外请求头
            
        Returns:
            解析后的响应 dict，或解密后的业务数据
        """
        ENCRYPT_DATA_KEY = 'encryptData'
        CLIENT_SECRET_KEY = 'client_secret'
        JSON_CONTENT_TYPE = 'application/json;charset=UTF-8'
        CONTENT_TYPE_KEY = 'Content-Type'
        CLIENT_ID_KEY = 'client_id'
        
        self.timestamp = int(time.time() * 1000)
        timestamp_str = str(self.timestamp)
        
        if self.keyCode is None:
            self.keyCode = generate_random_string(32, 16, 2)
        
        key_code = self.keyCode
        
        # 添加反爬虫请求头
        headers = {
            'Accept': JSON_CONTENT_TYPE,
            CONTENT_TYPE_KEY: JSON_CONTENT_TYPE,
            'version': '1.0',
            'source': '0901',
            'timestamp': timestamp_str,
            'wsgwType': 'web',
            'appKey': appKey,
            'User-Agent': random.choice(USER_AGENTS),  # 随机 User-Agent
            'Referer': REFERER,
            'Origin': ORIGIN,
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }
        
        request_data = data
        if api == get_request_key_api:
            # 握手请求：加密 client_id 和 secret
            request_data = {CLIENT_ID_KEY: appKey, CLIENT_SECRET_KEY: appSecret}
            encrypted_req = sm4_encrypt(json_dumps(request_data), key_code)
            request_data = {
                'data': encrypted_req + sm3_hash(encrypted_req + timestamp_str),
                'skey': sm2_encrypt(key_code, '042D12DFBC179202AC4B7B7BADCDA6FF7B604339263F6AB732CE7107B7EA3830A2CA714DC303920D3CFF7647D898F1A8CC6C24E9EC3CC194E22D984AF7E16B42DC'),
                CLIENT_ID_KEY: appKey,
                'timestamp': timestamp_str
            }
        elif api == get_request_authorize_api:
            # 授权请求：x-www-form-urlencoded
            request_data = {
                CLIENT_ID_KEY: appKey,
                'response_type': 'code',
                'redirect_url': '/test',
                'timestamp': self.timestamp,
                'rsi': self.token
            }
            request_data = urllib.parse.urlencode(request_data)
            headers[CONTENT_TYPE_KEY] = 'application/x-www-form-urlencoded; charset=UTF-8'
            headers['keyCode'] = key_code
            session = async_get_clientsession(self.hass, False)
            async with session.post(baseApi + api, data=request_data, headers=headers, timeout=REQUEST_TIMEOUT) as response:
                response_json = await response.json()
                # 响应解密 (b -> sm4_decrypt)
                decrypted_resp = sm4_decrypt(response_json['data'], self.token)
                decrypted_resp = json.loads(decrypted_resp)
                return decrypted_resp
        elif api == get_web_token_api:
            # Token 请求：包含 code 和签名
            request_data = {
                'grant_type': 'authorization_code',
                'sign': sm3_hash(appKey + timestamp_str),
                CLIENT_SECRET_KEY: appSecret,
                'state': '464606a4-184c-4beb-b442-2ab7761d0796',
                'key_code': key_code,
                CLIENT_ID_KEY: appKey,
                'timestamp': self.timestamp,
                'code': request_data['code']
            }
            encrypted_req = sm4_encrypt(json_dumps(request_data), key_code)
            request_data = {
                'data': encrypted_req + sm3_hash(encrypted_req + timestamp_str),
                'skey': sm2_encrypt(key_code, self.publicKey),
                'timestamp': timestamp_str
            }
        else:
            # 普通业务请求：标准加密
            request_data = self.encrypt_post_data(request_data)
        
        if header is not None:
            headers.update(header)
        
        # 根据 API 类型添加特定 Header
        if api in sessionIdControlApiList:
            headers['sessionId'] = 'web' + timestamp_str
        if api in keyCodeControlApiList:
            headers['keyCode'] = key_code
        if api in authControlApiList:
            headers['Authorization'] = 'Bearer ' + self.accessToken[:len(self.accessToken) // 2]
        if api in tControlApiList:
            headers['t'] = self.token[:len(self.token) // 2]
        
        retry_count = 0
        # 登录相关API只重试1次，避免频繁重试触发风控
        login_apis = [get_request_key_api, get_verify_code_api, verify_password_api, 
                      get_request_authorize_api, get_web_token_api]
        max_retries_for_this_api = 1 if api in login_apis else MAX_RETRIES
        
        while retry_count < max_retries_for_this_api:
            try:
                session = async_get_clientsession(self.hass, False)
                async with session.post(baseApi + api, json=request_data, headers=headers, timeout=REQUEST_TIMEOUT) as response:
                    response_text = await response.text()
                    response_json = None
                    if response_text.startswith('{'):
                        response_json = json.loads(response_text)
                        # 如果响应包含 encryptData，则进行解密
                        if ENCRYPT_DATA_KEY in response_json:
                            decrypted_data = sm4_decrypt(response_json[ENCRYPT_DATA_KEY], key_code)
                            response_json = json.loads(decrypted_data)
                    # 请求成功后添加随机延迟，避免被识别为爬虫
                    # 登录相关接口使用短延迟（200-500ms），业务数据接口使用正常延迟（1-3秒）
                    use_normal_delay = api not in login_apis
                    await self._add_random_delay(use_normal_interval=use_normal_delay)
                    return response_json
            except Exception as err:
                retry_count += 1
                if retry_count >= max_retries_for_this_api:
                    LOGGER.warning("请求错误(重试 %d 次后仍失败): %s", max_retries_for_this_api, err)
                    raise err
                # 重试前添加随机延迟
                await self._add_random_delay()
                LOGGER.info(f"请求重试 {retry_count}/{max_retries_for_this_api}")

    async def _get_request_key(self):
        """
        获取请求密钥 (Step 1).
        
        获取服务端 publicKey，初始化加密会话。
        """
        self.keyCode = None
        response = await self._fetch(get_request_key_api, {})
        msg = self.handle_request_result_message('get_request_key_api', response)
        if not isinstance(response, dict):
            return {'errcode': 1, 'errmsg': msg}
        if 'code' in response and str(response['code']) == '1' and 'data' in response:
            response_data = response['data'] or {}
            self.keyCode = response_data.get('keyCode')
            self.publicKey = response_data.get('publicKey')
            if 'accessToken' in response_data:
                self.accessToken = response_data.get('accessToken')
            if 'refreshToken' in response_data:
                self.refreshToken = response_data.get('refreshToken')
            if 'token' in response_data:
                self.token = response_data.get('token')
            self.timestamp = int(time.time() * 1000)
            return {'errcode': 0}
        LOGGER.error('get_request_key_api-' + msg)
        return {'errcode': 1, 'errmsg': msg}

    async def _get_pass_verify_code(self, account, password):
        """获取验证码（登录 Step 2），自动检测滑块/点选类型。"""
        request_data = {
            'account': account,
            'password': password,
            'canvasHeight': 200,
            'canvasWidth': 310,
        }
        response = await self._fetch(get_verify_code_api, request_data)
        msg = self.handle_request_result_message('get_verify_code_api', response, False)
        if not isinstance(response, dict):
            return {'errcode': 1, 'errmsg': msg}
        if 'code' in response and str(response['code']) == '1' and 'data' in response:
            response_data = response['data'] or {}
            self.ticket = response_data.get('ticket', '')
            if not self.ticket:
                return {'errcode': 1, 'errmsg': msg}

            captcha_type = _captcha_solver.detect_captcha_type(response_data)
            LOGGER.info("检测到验证码类型: %s", captcha_type)
            return {
                'errcode': 0,
                'captcha_type': captcha_type,
                'canvasSrc': response_data.get('canvasSrc', ''),
                'blockSrc': response_data.get('blockSrc', ''),
                'blockY': response_data.get('blockY', 0),
                'iconSrc': response_data.get('iconSrc', ''),
                'wordSrc': response_data.get('wordSrc', ''),
                'iconSrcs': response_data.get('iconSrcs', []),
                'src': response_data.get('src', ''),
                'canvasHeight': response_data.get('canvasHeight', 200),
                'canvasWidth': response_data.get('canvasWidth', 310),
                'ticket': self.ticket,
            }
        LOGGER.error('get_verify_code_api-' + msg)
        return {'errcode': 1, 'errmsg': msg}

    async def _verify_password(self, account, password, code, loginKey, captcha_type='slider'):
        """提交密码和验证码（登录 Step 3）。"""
        request_data = {
            'loginKey': loginKey,
            'code': code,
            'params': {
                'uscInfo': {
                    'devciceIp': '',
                    'tenant': 'state_grid',
                    'member': '0902',
                    'devciceId': '',
                },
                'quInfo': {
                    'optSys': 'ios',
                    'pushId': '00000',
                    'addressProvince': '110100',
                    'password': password,
                    'addressRegion': '110101',
                    'account': account,
                    'addressCity': '330100',
                },
            },
            'Channels': 'web',
        }
        if captcha_type == 'click':
            request_data['complexSliderRet'] = 0
            request_data['complexSliderType'] = 'clickImg'
        elif captcha_type == 'slider':
            request_data['complexSliderRet'] = 0
            request_data['complexSliderType'] = 'blockPuzzle'

        response = await self._fetch(verify_password_api, request_data)
        msg = self.handle_request_result_message('verify_password_api', response)
        if not isinstance(response, dict):
            return {'errcode': 1, 'errmsg': msg}
        if 'code' in response and response['code'] == 1 and 'data' in response and response['data']:
            response_data = response['data'] or {}
            srvrt = response_data.get('srvrt') or {}
            if srvrt.get('resultCode') in ('0000', '00000'):
                bizrt = response_data.get('bizrt') or {}
                token = bizrt.get('token') or response_data.get('token')
                user_info = bizrt.get('userInfo') or response_data.get('userInfo')
                if isinstance(user_info, list):
                    user_info = user_info[0] if user_info else None
                if token and user_info:
                    self.token = token
                    self.userInfo = user_info
                    self.timestamp = int(time.time() * 1000)
                    return {'errcode': 0}

        LOGGER.error('verify_password_api-' + msg)
        return {'errcode': 1, 'errmsg': msg}

    async def _verify_click_captcha(self, account, password, code, loginKey):
        """提交点选验证码（f07/clickCard 端点）。"""
        request_data = {
            'loginKey': loginKey,
            'code': code,
            'params': {
                'uscInfo': {
                    'devciceIp': '',
                    'tenant': 'state_grid',
                    'member': '0902',
                    'devciceId': '',
                },
                'quInfo': {
                    'optSys': 'android',
                    'pushId': '000000',
                    'addressProvince': '110100',
                    'password': password,
                    'addressRegion': '110101',
                    'account': account,
                    'addressCity': '330100',
                },
            },
            'Channels': 'web',
        }
        LOGGER.info("提交点选验证码(f07/clickCard): code=%s", code)
        response = await self._fetch(click_card_api, request_data)
        msg = self.handle_request_result_message('click_card_api', response)
        if isinstance(response, dict) and str(response.get('code')) == '1':
            response_data = response.get('data') or {}
            srvrt = response_data.get('srvrt') or {}
            if srvrt.get('resultCode') in ('0000', '00000'):
                bizrt = response_data.get('bizrt') or {}
                token = bizrt.get('token')
                user_info = bizrt.get('userInfo')
                if isinstance(user_info, list):
                    user_info = user_info[0] if user_info else None
                if token and user_info:
                    self.token = token
                    self.userInfo = user_info
                    self.timestamp = int(time.time() * 1000)
                    return {'errcode': 0}
        LOGGER.warning("clickCard(f07) 验证失败: %s，尝试回退到 f06...", msg)
        return {'errcode': 1, 'errmsg': msg}

    async def _get_request_authorize(self):
        """
        获取 OAuth2 授权码（登录 Step 4）。
        
        从 redirect_url 中解析出 code，供 _get_web_token 换取正式 Token。
        
        Returns:
            dict: errcode=0 表示成功，authorizeCode 已写入 self
        """
        response = await self._fetch(get_request_authorize_api, {})
        msg = self.handle_request_result_message('get_request_authorize_api', response)
        if not isinstance(response, dict):
            return {'errcode': 1, 'errmsg': msg}
        if 'code' in response and str(response['code']) == '1' and 'data' in response and response['data']:
            response_data = response['data']
            redirect_url = response_data['redirect_url']
            code_index = redirect_url.rfind('code=')
            self.authorizeCode = redirect_url[code_index + 5:code_index + 5 + 32]
            return {'errcode': 0}
            
        LOGGER.error('get_request_authorize_api-' + msg)
        return {'errcode': 1, 'errmsg': msg}

    async def _get_web_token(self, data=None):
        """
        获取正式访问 Token（登录 Step 5）。
        
        用授权码换取 accessToken 和 refreshToken，写入 self。
        
        Returns:
            dict: errcode=0 表示成功
        """
        request_data = {'code': self.authorizeCode}
        response = await self._fetch(get_web_token_api, request_data)
        msg = self.handle_request_result_message('get_web_token_api', response)
        if not isinstance(response, dict):
            return {'errcode': 1, 'errmsg': msg}
        if 'code' in response and str(response['code']) == '1' and 'data' in response:
            response_data = response['data']
            self.accessToken = response_data['access_token']
            self.refreshToken = response_data['refresh_token']
            return {'errcode': 0}
            
        LOGGER.error('get_web_token_api-' + msg)
        return {'errcode': 1, 'errmsg': msg}

    async def _get_door_number(self):
        """
        获取用户绑定的户号列表（登录 Step 6 / 刷新时可选）。
        
        过滤 elecTypeCode='05' 的户号，合并已有户号数据，写入 powerUserList。
        
        Returns:
            dict: errcode=0 表示成功
        """
        door_manager_config = configuration['doorNumberManeger']
        request_data = {
            'serviceCode': door_manager_config['serviceCode'],
            'source': door_manager_config['source'],
            'target': door_manager_config['target'],
            'uscInfo': {
                'member': door_manager_config['uscInfo']['member'],
                'devciceIp': door_manager_config['uscInfo']['devciceIp'],
                'devciceId': door_manager_config['uscInfo']['devciceId'],
                'tenant': door_manager_config['uscInfo']['tenant']
            },
            'quInfo': {'userId': self.userInfo['userId']},
            'token': self.token
        }
        
        response = await self._fetch_safe(get_door_number_api, request_data)
        msg = self.handle_request_result_message('get_door_number_api', response)
        
        if 'code' in response and response['code'] == 1 and 'data' in response and 'bizrt' in response['data']:
            response_data = response['data']
            existing_users = {}
            if self.powerUserList is not None:
                existing_users = {user['consNo_dst']: user for user in self.powerUserList}
            
            new_user_list = []
            for user in response_data['bizrt']['powerUserList']:
                if user['consNo_dst'] in existing_users:
                    new_user_list.append(existing_users[user['consNo_dst']])
                elif user.get('elecTypeCode') != '05':
                    new_user_list.append(user)
            
            self.powerUserList = new_user_list
            self.last_door_list_update_time = time.time()
            return {'errcode': 0}
            
        return {'errcode': 1, 'errmsg': msg}

    async def _get_door_balance(self, door_account):
        """
        获取指定户号的账户余额信息。
        
        将结果写入 door_account['account_balance']，后续 refresh_data 会从中
        解析 sumMoney、accountBalance 等字段计算最终 balance 值。
        
        Args:
            door_account: 户号信息 dict，需含 consNo_dst、proNo、orgNo 等
            
        Returns:
            bool: 成功返回 True，失败返回 False
        """
        request_data = {
            'data': {
                'srvCode': '',
                'serialNo': '',
                'channelCode': configuration['account']['channelCode'],
                'funcCode': configuration['account']['funcCode'],
                'acctId': self.userInfo['userId'],
                'userName': self.userInfo.get('loginAccount', self.userInfo.get('nickname', None)),
                'promotType': '1',
                'promotCode': '1',
                'userAccountId': self.userInfo['userId'],
                'list': [{
                    'consNoSrc': door_account['consNo_dst'],
                    'proCode': door_account.get('proNo', door_account.get('provinceId', None)),
                    'sceneType': door_account.get('consSortCode', door_account.get('elecTypeCode', None)),
                    'consNo': door_account.get('consNo') or door_account['consNo_dst'],
                    'orgNo': door_account['orgNo']
                }]
            },
            'serviceCode': '0101143',
            'source': configuration['source'],
            'target': door_account.get('proNo', door_account.get('provinceId', None))
        }
        
        response = await self._fetch_safe(get_door_balance_api, request_data)
        self.handle_request_result_message('get_door_balance_api', response)
        
        if self.is_debug:
            LOGGER.info("余额API完整响应(户号 %s): %s", door_account.get('consNo_dst', ''), json_dumps(response))
        
        if not isinstance(response, dict) or 'code' not in response or response['code'] != 1 or 'data' not in response or not response['data']:
            if self.is_debug:
                if isinstance(response, dict):
                    LOGGER.warning("余额API解析失败: code=%s, data存在=%s", response.get('code'), bool(response.get('data')))
                else:
                    LOGGER.warning("余额API返回非dict: %s", type(response).__name__)
            return False
        response_data = response['data']
        balance_list = response_data.get('list')
        if not balance_list:
            bizrt = response_data.get('bizrt')
            if isinstance(bizrt, dict):
                balance_list = bizrt.get('list')
            elif isinstance(bizrt, list):
                balance_list = bizrt
        if not balance_list:
            balance_list = response_data.get('dataInfo', {}).get('list') if isinstance(response_data.get('dataInfo'), dict) else None
        if balance_list and len(balance_list) > 0:
            first = balance_list[0] if isinstance(balance_list[0], dict) else None
            if first:
                door_account['account_balance'] = first
                return True
        if self.is_debug:
            LOGGER.warning("余额API无有效list: response_data.keys=%s", list(response_data.keys()) if isinstance(response_data, dict) else type(response_data))
        return False

    async def _get_door_bill(self, door_account, year):
        """
        获取指定年度的账单信息，包含月度用电明细（mothEleList）。
        
        结果会合并到 door_account['month_bill_list']，支持多次调用累加不同年份。
        
        Args:
            door_account: 户号信息 dict
            year: 年份（如 2025）
            
        Returns:
            dataInfo 中的年度汇总数据，或 None
        """
        request_data = {
            'data': {
                'acctId': self.userInfo['userId'],
                'channelCode': configuration['channelCode'],
                'clearCache': '11',
                'consType': door_account.get('constType') or door_account.get('consType', '01'),
                'funcCode': 'ALIPAY_01',
                'orgNo': door_account['orgNo'],
                'proCode': door_account['proNo'],
                'promotCode': '1',
                'promotType': '1',
                'serialNo': '',
                'srvCode': '',
                'userName': '',
                'provinceCode': door_account['proNo'],
                'userAccountId': self.userInfo['userId'],
                'consNo': door_account['consNo_dst'],
                'queryYear': year
            },
            'serviceCode': 'BCP_000026',
            'source': 'app',
            'target': door_account['proNo']
        }
        
        response = await self._fetch_safe(get_door_bill_api, request_data)
        self.handle_request_result_message('get_door_bill_api', response)
        
        if 'code' in response and response['code'] == 1 and 'data' in response and response['data']:
            response_data = response['data']
            DATA_INFO_KEY = 'dataInfo'
            MONTH_ELE_LIST_KEY = 'mothEleList'
            
            if MONTH_ELE_LIST_KEY in response_data:
                if 'month_bill_list' not in door_account:
                    door_account['month_bill_list'] = response_data[MONTH_ELE_LIST_KEY]
                else:
                    existing_bills_map = {bill['month']: bill for bill in door_account['month_bill_list']}
                    new_bills = response_data[MONTH_ELE_LIST_KEY]
                    for bill in new_bills:
                        if bill['month'] not in existing_bills_map:
                            door_account['month_bill_list'].append(bill)
            
            if DATA_INFO_KEY in response_data:
                return response_data[DATA_INFO_KEY]
            else:
                pass
                
        return None

    async def _get_door_mouth_bill(self, door_account, month_bill):
        """
        获取指定月账单的阶梯/抄表信息（对齐 state_grid __get_door_mouth_bill）。

        解析 readList / pointList，写入 month_bill['month_ele']:
        - month_meter_num: 抄表读数
        - month_ele_num: 本月电量（activeCount）
        """
        month_raw = str(month_bill.get('month', '')).replace('-', '')
        if len(month_raw) != 6 or not month_raw.isdigit():
            return False
        month_dt = datetime.datetime.strptime(month_raw, '%Y%m')
        query_date = f"{month_dt.year}-{month_dt.month:02d}"
        step_cfg = configuration['stepelect']
        request_data = {
            'data': {
                'channelCode': step_cfg['channelCode'],
                'funcCode': step_cfg['funcCode'],
                'promotType': step_cfg['promotType'],
                'clearCache': step_cfg['clearCache'],
                'consNo': door_account['consNo_dst'],
                'promotCode': door_account.get('proNo', door_account.get('provinceId', '')),
                'orgNo': door_account['orgNo'],
                'queryDate': query_date,
                'provinceCode': door_account.get('proNo', door_account.get('provinceId', None)),
                'consType': door_account.get('constType') or door_account.get('consType', '01'),
                'userAccountId': self.userInfo['userId'],
                'serialNo': '',
                'srvCode': '',
                'userName': self.userInfo.get('loginAccount', self.userInfo.get('nickname', '')),
                'acctId': self.userInfo['userId'],
            },
            'serviceCode': step_cfg['serviceCode'],
            'source': step_cfg['source'],
            'target': door_account.get('proNo', door_account.get('provinceId', None)),
        }
        response = await self._fetch(get_door_ladder_api, request_data)
        self.handle_request_result_message('get_door_ladder_api', response)
        if not (
            'code' in response
            and str(response['code']) in ('1', '000000')
            and response.get('data')
            and 'list' in response['data']
            and response['data']['list']
        ):
            return False
        item = response['data']['list'][0]
        meter_num = 0
        active_count = 0.0
        read_list = []
        if item.get('readList'):
            read_list = item['readList']
        elif (
            item.get('pointList')
            and item['pointList']
            and item['pointList'][0].get('readList')
        ):
            read_list = item['pointList'][0]['readList']
        if read_list:
            active_count = catchFloat(read_list[0], 'activeCount')
            if 'billRead' in read_list[0]:
                for bill_read in read_list[0]['billRead']:
                    meter_num = max(meter_num, catchInt(bill_read, 'currentNumber'))
        month_bill['month_ele'] = {
            'month_meter_num': meter_num,
            'month_ele_num': normal_round(active_count, 2),
        }
        return True

    async def _get_door_daily_bill(self, door_account, year, start_date, end_date, month_bill=None, cutoff_day=None):
        """
        获取指定时间范围内的日用电量数据（sevenEleList）。
        
        若 month_bill 为 None：合并到 door_account['daily_bill_list']，保留 RETENTION_DAYS 天。
        若 month_bill 非 None：将日用电写入 month_bill['daily_ele']，并累加得到 month_ele_num。
        
        Args:
            door_account: 户号信息 dict
            year: 年份
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            month_bill: 可选，月度账单 dict，用于填充当月日用电明细
            cutoff_day: 可选，日数据保留截止日期 YYYY-MM-DD，未传时内部计算
            
        Returns:
            bool: 成功返回 True，失败返回 False
        """
        request_data = {
            'params1': {
                'serviceCode': configuration['getday']['serviceCode'],
                'source': configuration['source'],
                'target': configuration['target'],
                'uscInfo': {
                    'member': configuration['uscInfo']['member'],
                    'devciceIp': configuration['uscInfo']['devciceIp'],
                    'devciceId': configuration['uscInfo']['devciceId'],
                    'tenant': configuration['uscInfo']['tenant']
                },
                'quInfo': {'userId': self.userInfo['userId']},
                'token': self.token
            },
            'params3': {
                'data': {
                    'acctId': self.userInfo['userId'],
                    'consNo': door_account['consNo_dst'],
                    'consType': '01',
                    'endTime': end_date,
                    'orgNo': door_account['orgNo'],
                    'queryYear': year,
                    'proCode': door_account.get('proNo', door_account.get('provinceId', None)),
                    'serialNo': '',
                    'srvCode': '',
                    'startTime': start_date,
                    'userName': self.userInfo['loginAccount'],
                    'funcCode': configuration['getday']['funcCode'],
                    'channelCode': configuration['getday']['channelCode'],
                    'clearCache': configuration['getday']['clearCache'],
                    'promotCode': configuration['getday']['promotCode'],
                    'promotType': configuration['getday']['promotType']
                },
                'serviceCode': configuration['getday']['serviceCode'],
                'source': configuration['getday']['source'],
                'target': door_account.get('proNo', door_account.get('provinceId', None))
            },
            'params4': '010103'
        }
        
        response = await self._fetch_safe(get_door_daily_bill_api, request_data)
        self.handle_request_result_message('get_door_daily_bill_api', response)
        
        if self.is_debug:
            LOGGER.warning("日用电API响应: %s", json_dumps(response))
            
        SEVEN_ELE_LIST_KEY = 'sevenEleList'
        if 'code' in response and response['code'] == 1 and 'data' in response and response['data'] and SEVEN_ELE_LIST_KEY in response['data']:
            response_data = response['data']
            day_ele_list = response_data[SEVEN_ELE_LIST_KEY]
            
            if month_bill is None:
                # 合并新旧日用电数据，保留周期与燃气一致（1 年）
                stored_daily_bills = door_account.get('daily_bill_list', [])
                day_map = {}
                for bill in stored_daily_bills:
                    day_key = _day_key_from_bill(bill)
                    if day_key:
                        day_map[day_key] = dict(bill)
                for bill in day_ele_list:
                    day_key = _day_key_from_bill(bill)
                    if day_key:
                        day_map[day_key] = dict(bill)
                if cutoff_day is None:
                    cutoff_day = (datetime.date.today() - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%d')
                filtered = [value for key, value in day_map.items() if key >= cutoff_day]
                door_account['daily_bill_list'] = sorted(filtered, key=lambda x: _day_key_from_bill(x) or '', reverse=True)
            else:
                # 计算月度总电量（新疆无峰谷平尖，只汇总总量）
                total_ele = 0.0
                for day_bill in day_ele_list:
                    day_bill['dayElePq'] = catchFloat(day_bill, 'dayElePq')
                    total_ele += day_bill['dayElePq']

                month_bill['month_ele_num'] = normal_round(total_ele, 2)
                month_bill['daily_ele'] = day_ele_list
            
            return True
        return False
    
    def _solve_slider_captcha_pixel(self, captcha_data):
        """像素算法解算滑块验证码。"""
        block_y = int(captcha_data.get('blockY', 0))
        block_height = 0
        block_bytes = base64_image_to_bytes(captcha_data.get('blockSrc', ''))
        if block_bytes:
            with Image.open(io.BytesIO(block_bytes)) as bg_img:
                _, block_height = bg_img.size
        canvas_bytes = base64_image_to_bytes(captcha_data.get('canvasSrc', ''))
        with Image.open(io.BytesIO(canvas_bytes)) as canvas_img:
            cw, ch = canvas_img.size
            cropped = canvas_img.crop((0, block_y, cw, block_y + block_height))
            binary = cropped.point(lambda p: 255 if p > 150 else 0)
        w, h = binary.width, binary.height
        matrix = [[0 for _ in range(h)] for _ in range(w)]
        for y_idx in range(h):
            for x_idx in range(w):
                pixel = binary.getpixel((x_idx, y_idx))
                if is_dark(pixel, 100):
                    matrix[x_idx][y_idx] = 1
        top, left, bottom, right = find_max_rectangle(matrix)
        LOGGER.info("像素算法滑块距离: %s", left)
        return left

    def _solve_slider_captcha_llm(self, captcha_data):
        """LLM 解算滑块验证码。"""
        try:
            canvas_base64 = captcha_data.get('canvasSrc', '')
            if not canvas_base64:
                return 0
            return _captcha_solver.solve_slider_captcha_llm(canvas_base64, canvas_width=310, canvas_height=200)
        except Exception as exc:
            LOGGER.exception("LLM 滑块解算失败: %s", exc)
            return 0

    def _solve_click_captcha(self, captcha_data):
        """LLM 解算点选验证码，返回坐标字符串。"""
        try:
            ref_base64 = captcha_data.get('iconSrc', '') or captcha_data.get('wordSrc', '')
            if not ref_base64 and captcha_data.get('iconSrcs'):
                icons = captcha_data['iconSrcs']
                if isinstance(icons, list) and icons:
                    ref_base64 = icons[0] if isinstance(icons[0], str) else ''
            main_base64 = captcha_data.get('canvasSrc', '') or captcha_data.get('src', '')
            if not main_base64:
                LOGGER.error("点选验证码缺少主图数据")
                return ""
            if not ref_base64:
                LOGGER.error("点选验证码缺少参考图标数据")
                return ""
            main_bytes = base64_image_to_bytes(main_base64)
            with Image.open(io.BytesIO(main_bytes)) as main_img:
                main_w, main_h = main_img.size
            coords = _captcha_solver.solve_click_captcha(ref_base64, main_base64, main_w, main_h)
            if not coords or len(coords) < 2:
                LOGGER.error("LLM 未能识别点选验证码坐标")
                return ""
            coord_str = "|".join([f"{x},{y}" for x, y in coords])
            LOGGER.info("点选验证码坐标: %s", coord_str)
            return coord_str
        except Exception as exc:
            LOGGER.exception("点选验证码解算失败: %s", exc)
            return ""

    def _is_token_valid(self):
        """
        检查当前 Token 是否有效。

        有 accessToken + token 且未过期则视为有效；无 expirationDate 时亦视为有效，
        由 _fetch_safe 在业务错误码时触发重登，避免每次 password_login 都打验证码。
        """
        if not self.accessToken or not self.token:
            return False
        if self.expirationDate:
            return int(time.time() * 1000) < self.expirationDate
        return True

    async def password_login(self, account, password, encode=False, max_retry=3, force_refresh=False):
        """执行完整登录流程（对齐 state_grid 验证机制）。"""
        if self.is_rk001_cooldown() and account == self.account:
            LOGGER.warning("[RK001冷却] 跳过手机号密码登录")
            return {'errcode': 1, 'errmsg': '手机号密码登录日额度已用完(RK001)'}

        if not encode:
            password = hashlib.md5(password.encode()).hexdigest().upper()

        if not force_refresh and self._is_token_valid():
            LOGGER.info("现有 Token 有效，跳过登录流程")
            return {'errcode': 0}

        result = await self._get_request_key()
        if result['errcode'] != 0:
            return result

        captcha_result = await self._get_pass_verify_code(account, password)
        if captcha_result['errcode'] != 0:
            return captcha_result

        captcha_type = captcha_result.get('captcha_type', 'slider')

        if captcha_type == 'click':
            LOGGER.info("正在使用 LLM 解算点选验证码...")
            verify_code = await self.hass.async_add_executor_job(
                self._solve_click_captcha, captcha_result
            )
            if not verify_code:
                if max_retry <= 0:
                    return {'errcode': 1, 'errmsg': '点选验证码解算失败'}
                LOGGER.error('点选验证码解算失败，将重试！')
                return await self.password_login(account, password, encode=True, max_retry=max_retry - 1)

            result_verify = await self._verify_click_captcha(account, password, verify_code, self.ticket)
            if result_verify['errcode'] != 0:
                LOGGER.warning('f07 clickCard 失败，回退到 f06 + complexSliderType=clickImg...')
                result_verify = await self._verify_password(
                    account, password, verify_code, self.ticket, captcha_type='click'
                )
            if result_verify['errcode'] != 0:
                if self._is_rk001_error(result_verify):
                    return {'errcode': 1, 'errmsg': '验证登录遇流控(RK001)'}
                if max_retry <= 0:
                    return result_verify
                LOGGER.error('账号密码登录失败，将重试！')
                return await self.password_login(account, password, encode=True, max_retry=max_retry - 1)
        else:
            block_y = int(captcha_result.get('blockY', 0))
            block_height = 0
            block_bytes = base64_image_to_bytes(captcha_result.get('blockSrc', ''))
            if block_bytes:
                with Image.open(io.BytesIO(block_bytes)) as block_img:
                    _, block_height = block_img.size

            slider_distance = 0
            if self.captcha_solver.api_key and captcha_result.get('canvasSrc'):
                LOGGER.info("正在使用 LLM 解算滑块验证码...")
                slider_distance = await self.hass.async_add_executor_job(
                    self._solve_slider_captcha_llm, captcha_result
                )
                if slider_distance == 0:
                    LOGGER.warning("LLM 滑块解算失败，回退到像素算法...")
                    slider_distance = await self.hass.async_add_executor_job(
                        self._solve_slider_captcha_pixel, captcha_result
                    )
            elif captcha_result.get('canvasSrc'):
                slider_distance = await self.hass.async_add_executor_job(
                    self._solve_slider_captcha_pixel, captcha_result
                )

            result_verify = await self._verify_password(
                account, password, slider_distance, self.ticket, captcha_type='slider'
            )
            if result_verify['errcode'] != 0:
                if self._is_rk001_error(result_verify):
                    return {'errcode': 1, 'errmsg': '验证登录遇流控(RK001)'}
                if max_retry <= 0:
                    return result_verify
                LOGGER.error('账号密码登录失败，将重试！')
                return await self.password_login(account, password, encode=True, max_retry=max_retry - 1)

        self.account = account
        self.password = password

        result = await self._get_request_authorize()
        if result['errcode'] != 0:
            return result

        result = await self._get_web_token()
        if result['errcode'] != 0:
            return result

        self.need_login = False
        await self.save_data()
        return {'errcode': 0}

    async def refresh_data(self, force_refresh=False):
        """
        刷新所有户号的数据。

        与 state_grid 一致：
        - timestamp 由 __fetch 在请求时更新，失败时还原，避免错误推进刷新间隔
        - 登录失败中途退出时不 save，下次轮询仍可重试
        """
        _orig_ts = self.timestamp
        try:
            if force_refresh:
                await self._get_door_number()
                if self.need_login:
                    self.timestamp = _orig_ts
                    return

            should_refresh = (
                force_refresh
                or (int(time.time() * 1000) - self.timestamp) > self.refresh_interval * 3600 * 1000
            )
            if not should_refresh:
                return

            if self.powerUserList:
                for user in self.powerUserList:
                    await self._refresh_single_account(user)
                    if self.need_login:
                        self.timestamp = _orig_ts
                        return

            await self.save_data()
        except Exception:
            LOGGER.exception("refresh_data 异常，已还原 timestamp")
            self.timestamp = _orig_ts
            return

    async def _refresh_single_account(self, user):
        cons_no = user['consNo_dst']
        if cons_no not in self.doorAccountDict:
            self.doorAccountDict[cons_no] = user
        
        door_account = self.doorAccountDict[cons_no]
        
        today = datetime.date.today()
        current_year = today.year
        end_date = today.strftime('%Y-%m-%d')
        start_date = (today - datetime.timedelta(days=40)).strftime('%Y-%m-%d')
        cutoff_day = (today - timedelta(days=RETENTION_DAYS)).strftime('%Y-%m-%d')
        fixed_price = getattr(self, 'price', 0.475) or 0.475

        # 1. 并行获取：余额 + 去年账单（互不依赖，节省 1 次往返延迟）
        await asyncio.gather(
            self._get_door_balance(door_account),
            self._get_door_bill(door_account, current_year - 1),
        )
        if self.need_login:
            return

        # 2. 并行获取：日用电 + 今年账单
        _, data_info_curr = await asyncio.gather(
            self._get_door_daily_bill(door_account, current_year, start_date, end_date, cutoff_day=cutoff_day),
            self._get_door_bill(door_account, current_year),
        )
        
        if data_info_curr:
            door_account['year_ele_cost'] = catchFloat(data_info_curr, 'totalBillAmt')
            door_account['year_ele_num'] = catchFloat(data_info_curr, 'totalElePq')

        # 3. 排序月度账单，保留周期 RETENTION_MONTHS 个月；补齐抄表读数
        sorted_bills = []
        if 'month_bill_list' in door_account:
            valid_bills = [bill for bill in door_account['month_bill_list'] if bill.get('month')]
            sorted_bills = sorted(valid_bills, key=lambda x: str(x['month']).replace('-', ''), reverse=True)
            total_months = today.year * 12 + (today.month - 1)
            start_total = total_months - RETENTION_MONTHS + 1
            start_year = start_total // 12
            start_month = start_total % 12 + 1
            cutoff_month = f"{start_year:04d}{start_month:02d}"
            sorted_bills = [
                bill for bill in sorted_bills
                if str(bill.get('month') or '').replace('-', '') >= cutoff_month
            ]
            door_account['month_bill_list'] = sorted_bills

            recent_12 = []
            for bill in sorted_bills[:12]:
                recent_12.append({
                    'month': bill.get('month'),
                    'ele': catchFloat(bill, 'monthEleNum') or catchFloat(bill, 'month_ele_num'),
                    'cost': catchFloat(bill, 'monthEleCost'),
                })
            door_account['recent_12_monthly_ele_list'] = recent_12

        # 4. 计算上月数据与抄表读数（仅拉最近一月抄表，失败不影响主数据）
        last_month_dt = today
        if sorted_bills:
            last_bill = sorted_bills[0]
            door_account['last_month_ele_num'] = (
                catchFloat(last_bill, 'monthEleNum') or catchFloat(last_bill, 'month_ele_num')
            )
            door_account['last_month_ele_cost'] = catchFloat(last_bill, 'monthEleCost')
            meter_num = 0
            if 'month_ele' not in last_bill:
                try:
                    login_before = self.need_login
                    await self._get_door_mouth_bill(door_account, last_bill)
                    # 抄表为可选增强：若仅该接口触发 need_login，不中断整次刷新
                    if self.need_login and not login_before:
                        LOGGER.warning("抄表接口异常，跳过上个月抄表读数")
                        self.need_login = False
                except Exception:
                    LOGGER.exception("获取上个月抄表失败，已跳过")
            if isinstance(last_bill.get('month_ele'), dict):
                meter_num = int(last_bill['month_ele'].get('month_meter_num') or 0)
            door_account['last_month_meter_num'] = meter_num
            month_raw = str(last_bill.get('month', '')).replace('-', '')
            if len(month_raw) == 6 and month_raw.isdigit():
                last_month_dt = datetime.datetime.strptime(month_raw, '%Y%m')
        else:
            door_account['last_month_meter_num'] = 0

        # 5. 单次遍历日用电数据：过滤、累计当月、收集最近 30 天
        current_month_ele_num = 0.0
        current_month_ele_cost = 0.0
        current_month_str = today.strftime('%Y-%m')
        recent_30 = []
        latest_day_record = None
        
        if 'daily_bill_list' in door_account:
            for bill in door_account['daily_bill_list']:
                day_str = bill.get('day')
                if not day_str or not isinstance(day_str, str):
                    continue
                day_str = day_str.strip()
                if len(day_str) == 8 and day_str.isdigit():
                    day_str = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:]}"
                if len(day_str) != 10 or day_str[4] != '-':
                    continue
                
                ele_value = catchFloat(bill, 'dayElePq')
                cost_value = catchFloat(bill, 'dayEleAmtTotal')
                if cost_value == 0:
                    for k in ('dayEleAmt', 'dayCost', 'dayEleAmtTotal', 'amt', 'cost'):
                        cost_value = catchFloat(bill, k)
                        if cost_value != 0:
                            break
                if cost_value == 0 and ele_value > 0:
                    cost_value = normal_round(ele_value * fixed_price, 2)
                recent_30.append({
                    'day': day_str,
                    'ele': ele_value,
                    'cost': cost_value,
                })
                if latest_day_record is None and ele_value > 0:
                    latest_day_record = {
                        'day': day_str,
                        'ele': ele_value,
                        'cost': cost_value,
                    }
                if day_str.startswith(current_month_str):
                    current_month_ele_num += ele_value
                    current_month_ele_cost += cost_value

        door_account['month_ele_num'] = normal_round(current_month_ele_num, 2)
        door_account['month_ele_cost'] = (
            normal_round(current_month_ele_cost, 2)
            if current_month_ele_cost > 0
            else normal_round(current_month_ele_num * fixed_price, 2)
        )

        # 6. 最近 30 天日用电列表
        door_account['recent_30_daily_ele_list'] = sorted(recent_30, key=lambda x: x['day'], reverse=True)
        
        if latest_day_record:
            door_account['daily_ele_num'] = latest_day_record['ele']
            door_account['daily_ele_cost'] = latest_day_record['cost']
            door_account['daily_lasted_date'] = latest_day_record['day']
        
        # 7. 年度累计（新疆仅总量，无峰谷平尖）
        year_ele = 0.0
        year_cost = 0.0
        if last_month_dt.month == 12:
            year_ele = door_account['month_ele_num']
            year_cost = door_account['month_ele_cost']
        else:
            year_bills = [
                b for b in sorted_bills
                if str(b.get('month', '')).replace('-', '').startswith(str(last_month_dt.year))
            ]
            for bill in year_bills:
                year_ele += catchFloat(bill, 'monthEleNum') or catchFloat(bill, 'month_ele_num')
                year_cost += catchFloat(bill, 'monthEleCost')
            if latest_day_record:
                try:
                    latest_dt = datetime.datetime.strptime(latest_day_record['day'], '%Y-%m-%d')
                    if latest_dt.month != last_month_dt.month:
                        year_ele += current_month_ele_num
                        year_cost += current_month_ele_cost
                except ValueError:
                    pass

        door_account['year_ele_num'] = normal_round(year_ele, 2) if year_ele else door_account.get('year_ele_num', 0)
        if year_cost:
            door_account['year_ele_cost'] = normal_round(year_cost, 2)
        elif 'year_ele_cost' not in door_account:
            door_account['year_ele_cost'] = 0

        # 8. 解析余额（区分预付费/后付费，兼容多地区接口字段）
        balance_val = 0.0
        if 'account_balance' in door_account:
            balance_data = door_account['account_balance']
            if self.is_debug:
                LOGGER.info("户号 %s 原始余额数据: %s", cons_no, json_dumps(balance_data))
            sum_money = catchFloat(balance_data, 'sumMoney')
            const_type = str(balance_data.get('constType', ''))
            is_ment = str(balance_data.get('isMent', '')).upper()
            is_postpaid = (const_type in ('1', '01'))
            is_prepaid = (const_type in ('0', '00'))
            is_special = not (not is_prepaid or is_ment != '1')
            if is_postpaid:
                balance_val = sum_money
            elif is_prepaid and not is_special:
                balance_val = -abs(sum_money)
            elif is_prepaid and is_special:
                balance_val = sum_money
            elif sum_money != 0:
                balance_val = sum_money
            account_balance_val = catchFloat(balance_data, 'accountBalance')
            if account_balance_val != 0:
                balance_val = account_balance_val
            fallback_keys = [
                'sumTotalAmt', 'totalAmount', 'balance', 'prestoreBalance',
                'accountBalance', 'balanceAmt', 'remainAmt', 'storeBalance',
                'totalAmt', 'amt', 'sumMoney', 'oweAmount', '欠费', '余额'
            ]
            for fallback_key in fallback_keys:
                if balance_val != 0:
                    break
                balance_val = catchFloat(balance_data, fallback_key)
            if balance_val == 0:
                for key, value in balance_data.items():
                    if value is None:
                        continue
                    try:
                        float_value = float(value)
                        if float_value > 0 and float_value < 1000000:
                            balance_val = float_value
                            break
                    except (ValueError, TypeError):
                        pass

        door_account['balance'] = balance_val
        door_account['refresh_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def get_door_account_list(self):
        """
        获取所有户号的详细数据列表。
        
        Returns:
            list: doorAccountDict 的值列表，每项为单个户号的完整数据
        """
        return list(self.doorAccountDict.values())

class StateGridCoordinator(DataUpdateCoordinator):
    """
    数据协调器，负责定时触发 StateGridDataClient 的数据刷新。

    与 state_grid 一致：重启有缓存时不 force_refresh，由 refresh_data 内部间隔判断，
    避免重启即触发 API 消耗 RK001 日额度。登录重试由 _fetch_safe 自动处理。
    """
    def __init__(self, hass, data_client):
        self.data_client = data_client
        self._last_success_data = None
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=300),
        )

    async def _async_update_data(self):
        backup_data = {}
        try:
            has_cached_data = bool(self.data_client.powerUserList)
            force_refresh = not has_cached_data
            backup_data = {k: dict(v) for k, v in self.data_client.doorAccountDict.items()}
            await self.data_client.refresh_data(force_refresh=force_refresh)

            if force_refresh and not self.data_client.doorAccountDict:
                raise UpdateFailed("首次配置后未找到户号")

            self._last_success_data = self.data_client.doorAccountDict.copy()
            return self._last_success_data
        except Exception as e:
            LOGGER.error("数据更新失败: %s", e)
            if backup_data:
                self.data_client.doorAccountDict.update(backup_data)

            if self._last_success_data:
                LOGGER.info("使用上次成功的缓存数据，保持实体可用")
                return self._last_success_data

            raise UpdateFailed(f"数据更新失败: {e}")
