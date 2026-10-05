"""
新疆电力 (ele) 数据客户端（对齐 state_grid/data_client.py）。

数据只从「推送缓存」来，本模块不再发任何网络请求：
  * ele 自己的 App 通道（app_supply 每小时把国家电网 App 接口面的返回翻译成网页形状灌进来）；
  * （可选）sidecar 浏览器容器 POST 给集成 webhook 的那份真实网页响应。

取数与解析逻辑沿用 ele/api.py 的新疆电力版本（余额正负号、年度累计口径、日/月账单保留），
只把「网络与登录」换成「推送缓存命中」。App 登录见 ele/app_api.py，加密原语见 ele/utils/crypt.py。
"""

import asyncio
import hashlib
import json
import time
import datetime
from datetime import timedelta

from .const import (
    VERSION,
    RETENTION_DAYS,
    RETENTION_MONTHS,
    CONF_PRICE,
    CONF_IS_PREPAID,
    CONF_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    MIN_REFRESH_INTERVAL,
    MAX_REFRESH_INTERVAL,
)
from .utils.logger import LOGGER
from .utils.store import async_save_to_store

# ─── 数据接口路径（同时是推送缓存键的一半）───
# 登录/验证码/换 token 那几条路径不在这里：本模块不发网络请求。
get_door_number_api = '/osg-open-uc0001/member/c9/f02'
get_door_balance_api = '/osg-open-bc0001/member/c05/f01'
get_door_bill_api = '/osg-open-bc0001/member/c01/f02'
get_door_ladder_api = '/osg-open-bc0001/member/c04/f03'
get_door_daily_bill_api = '/osg-web0004/member/c24/f01'
get_pay_record_api = get_door_ladder_api  # 兼容旧名

# ─── 推送通道：载荷形状 → 本集成的 API 路径 ───
PUSH_SHAPE_API = {
    'daily_ele': get_door_daily_bill_api,
    'monthly_ele': get_door_bill_api,
    'ladder': get_door_ladder_api,
    'balance': get_door_balance_api,
    'meter_list': get_door_number_api,
}
# 账户级载荷没有户号，缓存键的户号位固定为 None
PUSH_ACCOUNT_SHAPES = ('meter_list',)
OK_CODES = ('1', '0000', '000000')
CONS_NO_KEYS = ('consNo', 'consNoSrc', 'elecCustNo', 'custNo')

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

# ─── bilezhou / 新疆电力原版工具函数 ───
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

# ─── 推送缓存 ───
def _find_key(obj, names):
    """在待发请求里递归找第一个命中的键：各业务请求把同类信息放在不同深度
    （data.consNo、list[].consNoSrc、params3.data.consNo…），逐个写死容易漏。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        for v in obj.values():
            r = _find_key(v, names)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, names)
            if r:
                return r
    return None


def _cons_no_in(req):
    return _find_key(req, CONS_NO_KEYS)


def _period_in(req):
    """请求限定的是哪一期：月账单按 queryYear 取，阶梯按 queryDate 取，日电量带 queryYear。"""
    return _find_key(req, ('queryDate', 'queryYear'))


def _push_hit_key(api, req):
    """推送缓存的键：账户级请求不带户号。期间和日期区间是命中条件，不放键里。"""
    if api == get_door_number_api:
        return (api, None)
    return (api, _cons_no_in(req))


def _d8(s):
    return str(s).replace('-', '')


def _month_first(d8):
    return d8[:6] + '01'


def _push_covers(api, req, entry):
    """这条载荷够不够回答这个请求。缓存值 = (响应, 期间, 覆盖起, 覆盖止)。

    期间：月账单请求带 queryYear、阶梯带 queryDate。两边有一边说了期间就必须对上，
    宁可不命中退回真实请求，也不能拿 2026 年的数去答 2025 年。

    区间：App 的 c11/f01 与网页 c24/f01 同口径；命中判定按解析器真正用到的两样来：
    末日必须正好是请求的末日，起点必须不晚于「请求起点」与「末日所在月1号」里较晚的那个。
    """
    P, S, E = entry[1], entry[2], entry[3]
    RP = _period_in(req)
    if (P or RP) and P != RP:
        return False
    if api == get_door_daily_bill_api:
        A0 = _find_key(req, ('startTime',))
        B0 = _find_key(req, ('endTime',))
        if A0 and B0:
            if not (S and E):
                return False
            if E != _d8(B0):
                return False
            if S > max(_d8(A0), _month_first(E)):
                return False
    return True


def _normalize_pushed_daily(resp):
    """页面版 c24/f01 的 day 是 '2026-09-28'，接口版是 '20260928'，而解析器按后者写死。
    在入口统一掉，别让两种格式流进后面的解析。"""
    for row in ((resp.get('data') or {}).get('sevenEleList')) or []:
        d = row.get('day')
        if isinstance(d, str) and '-' in d:
            row['day'] = d.replace('-', '')

class StateGridDataClient:
    """新疆电力数据客户端（推送缓存模式）。

    取数只从推送缓存来；解析沿用新疆电力口径，输出 doorAccountDict 供传感器实体使用。
    """

    def __init__(self, hass, config=None, store_key=None):
        """初始化客户端（推送缓存模式）。

        Args:
            hass: HomeAssistant 实例
            config: 从 store 恢复的配置字典
            store_key: 存储键名（xjelegas_ele_{entry_id}）
        """
        self.hass = hass
        self.coordinator = None
        self.dataVersion = None
        # 请求载荷里 userId/loginAccount 位：App 通道取数不使用，留空壳，避免构造请求时 KeyError
        self.userInfo = {'userId': '', 'loginAccount': ''}
        self.powerUserList = None
        self.doorAccountDict = {}
        self.timestamp = int(time.time() * 1000)
        self.refresh_interval = DEFAULT_REFRESH_INTERVAL
        self.is_debug = False
        self.price = 0.475
        self.is_prepaid = None
        self.account = None
        self.password = None
        self.store_key = store_key
        self.need_login = False
        # App 通道状态：last_error 供配置页分流错误；verification_required=需短信验证新设备
        self.last_error = ""
        self.verification_required = False
        self._channel = None
        # 请求载荷里的 token 位：App 通道取数不使用，留 None，避免构造请求时 AttributeError
        self.token = None
        self.last_door_list_update_time = 0
        # 推送缓存：App 通道与（可选）sidecar 都往这里灌 {(api, 户号): [(响应, 期间, 起, 止), …]}
        self.push_cache = {}
        self.push_meta = None
        self.push_pending = False

        if config is not None:
            try:
                self.powerUserList = config.get('powerUserList')
                self.doorAccountDict = config.get('doorAccountDict', {})
                self.is_debug = config.get('is_debug', False)
                self.dataVersion = config.get('dataVersion')
                self.account = config.get('account')
                self.password = config.get('password')
                self.refresh_interval = config.get(
                    CONF_REFRESH_INTERVAL,
                    config.get('refresh_interval', DEFAULT_REFRESH_INTERVAL),
                )
                self.price = config.get(CONF_PRICE, config.get('price', 0.475))
                self.is_prepaid = config.get(CONF_IS_PREPAID)
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
        """保存取数与解析所需的会话数据，以便重启后恢复。"""
        data = {}
        data['powerUserList'] = self.powerUserList
        data['doorAccountDict'] = self.doorAccountDict
        data['is_debug'] = self.is_debug
        data['dataVersion'] = VERSION
        data['account'] = self.account
        data['password'] = self.password
        data['refresh_interval'] = self.refresh_interval
        data['price'] = self.price
        data[CONF_IS_PREPAID] = self.is_prepaid
        data['timestamp'] = self.timestamp
        await async_save_to_store(self.hass, self.store_key, data)

    async def ingest_push(self, bundle):
        """把 App 通道 / sidecar 推来的 {items:[{shape,consNo,response,period}]} 装进一次性缓存。

        每次推送整包替换：跨代残留会被下一轮误当成新数据消费。
        同一个 (接口, 户号) 允许挂多份载荷：同一接口可能按年/按月问很多次，
        命中时由 _push_covers 按期间和覆盖区间挑。
        """
        items = (bundle or {}).get('items') or []
        cache = {}
        skipped = []
        for it in items:
            shape = (it or {}).get('shape')
            api = PUSH_SHAPE_API.get(shape)
            resp = (it or {}).get('response')
            if api is None or not isinstance(resp, dict):
                skipped.append(str(shape))
                continue
            if str(resp.get('code')) not in OK_CODES:
                skipped.append(f"{shape}:code={resp.get('code')}")
                continue
            cons = None if shape in PUSH_ACCOUNT_SHAPES else str(it.get('consNo') or '').strip()
            if shape not in PUSH_ACCOUNT_SHAPES and not cons:
                skipped.append(f"{shape}:无户号")
                continue
            if shape == 'daily_ele':
                _normalize_pushed_daily(resp)
            year = ((resp.get('data') or {}).get('dataInfo') or {}).get('year')
            days = [_d8(r['day']) for r in ((resp.get('data') or {}).get('sevenEleList') or [])
                    if r.get('day')]
            years = {d[:4] for d in days}
            # 跨年的 7 天窗口说不清自己算哪一年，就把期间留空，别猜
            period = str(it.get('period') or year or
                         (list(years)[0] if len(years) == 1 else '')).strip() or None
            span = (min(days), max(days)) if days else (None, None)
            cache.setdefault((api, cons), []).append((resp, period, span[0], span[1]))
        self.push_cache = cache
        self.push_pending = True
        n = sum(len(v) for v in cache.values())
        self.push_meta = {'received_at': int(time.time() * 1000), 'items': n,
                          'pushed_at': (bundle or {}).get('pushed_at'), 'skipped': skipped[:8]}
        # 数据的"新鲜时间"就是推送时间：timestamp 不跟着走的话 12 小时闸门一直是开的
        self.timestamp = int(time.time() * 1000)
        LOGGER.warning('推送入仓 %d 份（%d 个键，跳过 %d）', n, len(cache), len(skipped))
        return n

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
        """__fetch 的薄壳。留着这一层是为了 refresh_data 那整段一行都不用改
        —— 它调的一直是 _fetch_safe；网络与登录都去掉后，这里只剩统一异常出口。
        """
        try:
            return await self._fetch(api, data)
        except Exception as exc:
            LOGGER.warning('取数异常(%s)：%s %s', api, type(exc).__name__, str(exc)[:120])
            return {'code': 'fetch_error', 'message': str(exc)[:120]}

    def _login_allowed(self) -> bool:
        """兼容 refresh_data 里的旧闸门：推送缓存模式下不再有登录退避，恒允许。"""
        return True

    async def _fetch(self, api, data, header=None):
        """只从推送缓存取载荷，不再发任何网络请求。

        命中即用、一次一份：同一 (接口, 户号) 可以挂多份，由 _push_covers 按期间和覆盖区间挑。
        不命中也不自己去登录：App 那条路由 app_supply 每小时灌缓存，网页那条路只能由真浏览器抓。
        这里不能碰 self.timestamp —— 它代表"数据新鲜度"，miss 也算新鲜的话 12 小时闸门就废了。
        """
        if self.push_cache:
            key = _push_hit_key(api, data)
            entries = self.push_cache.get(key) or []
            for i in range(len(entries)):
                if _push_covers(api, data, entries[i]):
                    payload = entries.pop(i)
                    if not entries:
                        self.push_cache.pop(key, None)
                    LOGGER.warning('命中推送缓存: %s 户号=%s', api, key[1])
                    return payload[0]
            LOGGER.warning('推送缓存未命中: %s 户号=%s 原因=%s 现有=%s', api, key[1],
                           '覆盖不足' if entries else '无此键',
                           sorted('%s|%s' % ('/'.join(str(k[0]).split('/')[-2:]), k[1])
                                  for k in self.push_cache))
        return {'code': 'no_cache', 'message': '推送缓存里没有这格的载荷（本轮不走网络）'}

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
                    'orgNo': door_account.get('orgNo', '')
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
        # 户号来自 App 登录响应，字段名可能与网页版不同：proNo 缺失时退回 provinceId，
        # 别用 [] 直接下标——缺一个键就抛 KeyError，会让 refresh_data 整轮中断、所有实体归零。
        pro_no = door_account.get('proNo', door_account.get('provinceId', ''))
        org_no = door_account.get('orgNo', '')
        request_data = {
            'data': {
                'acctId': self.userInfo['userId'],
                'channelCode': configuration['channelCode'],
                'clearCache': '11',
                'consType': door_account.get('constType') or door_account.get('consType', '01'),
                'funcCode': 'ALIPAY_01',
                'orgNo': org_no,
                'proCode': pro_no,
                'promotCode': '1',
                'promotType': '1',
                'serialNo': '',
                'srvCode': '',
                'userName': '',
                'provinceCode': pro_no,
                'userAccountId': self.userInfo['userId'],
                'consNo': door_account['consNo_dst'],
                'queryYear': year
            },
            'serviceCode': 'BCP_000026',
            'source': 'app',
            'target': pro_no
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
                'orgNo': door_account.get('orgNo', ''),
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
                    'orgNo': door_account.get('orgNo', ''),
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
    

    async def async_login(self, password=None, force=False):
        """App 通道登录：委托 AppChannel，成功与否不回抛异常。

        Args:
            password: 明文密码（可选）。传入时同步刷新 self.password 为 md5 小写摘要。
            force: True 时跳过 App 会话缓存强制重新登录。
        """
        from .app_api import AppChannel

        if password is not None:
            self.password = hashlib.md5(password.encode()).hexdigest().lower()
        account = str(self.account or "").strip()
        digest = str(self.password or "").lower()
        if not account or len(digest) != 32:
            LOGGER.warning("App 登录缺少账号或密码摘要（account=%s）", bool(account))
            self.last_error = "invalid_auth"
            return False

        channel = AppChannel(self.hass, account, digest)
        # 保留实例：新设备短信验证（4006）要在同一个设备画像上续跑 send/sms_login
        self._channel = channel
        ok = await channel.async_login(force=force)
        self.last_error = channel.last_error
        self.verification_required = channel.verification_required
        if ok:
            # 登录响应带回的户号列表先落进 powerUserList，供 refresh_data 首次使用
            if channel.raw_accounts:
                self.powerUserList = channel.raw_accounts
            self.need_login = False
            await self.save_data()
        return ok

    def _get_channel(self):
        """取（或按当前账号密码重建）AppChannel，供短信验证步骤复用同一设备。"""
        ch = getattr(self, "_channel", None)
        if ch is not None:
            return ch
        from .app_api import AppChannel
        account = str(self.account or "").strip()
        digest = str(self.password or "").lower()
        if account and len(digest) == 32:
            ch = AppChannel(self.hass, account, digest)
            self._channel = ch
        return ch

    async def async_send_device_verification_sms(self) -> str:
        """下发新设备登录短信，返回一次性 codeKey（失败为空串）。"""
        ch = self._get_channel()
        if ch is None:
            self.last_error = "invalid_auth"
            return ""
        code_key = await ch.async_send_device_verification_sms()
        self.last_error = ch.last_error
        return code_key

    async def async_verify_device(self, code: str, code_key: str) -> bool:
        """新设备验证：把短信码回填到密码登录请求重试；成功后设备即被信任。"""
        ch = self._get_channel()
        if ch is None:
            self.last_error = "invalid_auth"
            return False
        ok = await ch.async_login_with_code(code, code_key)
        self.last_error = ch.last_error
        if ok:
            if ch.raw_accounts:
                self.powerUserList = ch.raw_accounts
            self.need_login = False
            await self.save_data()
        return ok

    async def password_login(self, account, password, encode=False, max_retry=3,
                             force_refresh=False, bypass_backoff=False):
        """兼容旧接口：改为走 App 通道登录。参数 max_retry/bypass_backoff 保留仅为签名兼容。"""
        self.account = account
        ok = await self.async_login(password=password, force=True)
        return {'errcode': 0} if ok else {'errcode': 1, 'errmsg': 'App 通道登录失败'}

    async def refresh_data(self, force_refresh=False):
        """
        刷新所有户号的数据。

        与 state_grid 一致：
        - timestamp 由 __fetch 在请求时更新，失败时还原，避免错误推进刷新间隔
        - 登录失败中途退出时不 save，下次轮询仍可重试
        """
        _orig_ts = self.timestamp
        # 推送是一次性的触发器：这一轮用掉就熄灭。push_cache 里可能有本轮用不到的形状，
        # 拿它非空当强制刷新条件的话，每轮轮询都会被迫重跑一遍整段解析。
        self.push_pending = False
        # 已知需要重登且仍处于失败退避期：直接跳过，避免每轮轮询都发起一次探测请求。
        if self.need_login and not force_refresh and not self._login_allowed():
            LOGGER.info("处于登录退避期，跳过本次数据刷新")
            return
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
                    try:
                        await self._refresh_single_account(user)
                    except Exception:
                        # 单块表解析失败不能连坐其他户号：原来整轮 except 会把已经解析好的
                        # 户号也一起丢掉（实体全归零）。这里就地吞掉并打回溯，继续下一块表。
                        LOGGER.exception("户号 %s 数据解析失败（跳过该户号）",
                                         user.get('consNo_dst', '?'))
                        continue
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
        # 日账单缓存按 RETENTION_DAYS(366) 天保留，这里必须裁到 30 条：
        # 实体名与 state_grid 都是"最近 30 天"，不裁的话卡片会拿到一年多的数据。
        door_account['recent_30_daily_ele_list'] = sorted(
            recent_30, key=lambda x: x['day'], reverse=True)[:30]
        
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

    def get_door_account(self):
        """返回全部户号字典。"""
        return self.doorAccountDict
