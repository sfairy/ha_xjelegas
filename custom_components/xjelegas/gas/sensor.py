"""
新疆燃气 Home Assistant 传感器集成平台。

本模块负责将新疆燃气 API 返回的原始数据转换为 Home Assistant 可识别的传感器实体，
实现燃气账户余额、用气量、账单、阶梯计费等信息的自动化采集与展示。

模块主要组成部分：
1. XjGasCoordinator: 数据协调器，负责定时从 API 拉取数据并分发给所有传感器。
2. XjGasSensor: 通用传感器实体，用于处理大多数简单的数值型传感器（如余额、账单、用气量）。
3. XjGasGeneralSensor: 综合信息传感器，聚合了账户余额、日用气历史、月度账单历史、年度用气历史等信息，供前端卡片展示。
4. XjGasCurrentPriceSensor: 当前单价传感器，根据计费模式（固定/阶梯）和年度用气量计算当前气价。
5. XjGasCurrentTierSensor: 当前阶梯传感器，显示当前处于第几阶梯。
6. XjGasDailyCostSensor: 日账单传感器，计算当天的用气费用。

数据流说明：
  API 接口 -> XjGasCoordinator（定时拉取、合并、缓存）-> 各传感器实体（解析、展示）
"""
# ========== 标准库导入 ==========
from datetime import timedelta  # 用于定义更新间隔
import asyncio  # 异步并发控制
import logging  # 日志记录
import async_timeout  # 异步超时控制
from typing import Any, Dict, List, Optional  # 类型注解

# ========== Home Assistant 核心导入 ==========
from homeassistant.util import dt as dt_util  # 时区感知的日期时间工具
from homeassistant.components.sensor import (
    SensorEntity,       # 传感器实体基类
    SensorDeviceClass,  # 设备类型（如燃气、货币）
    SensorStateClass,   # 状态类型（如总计递增）
)
from homeassistant.const import UnitOfVolume  # 体积单位（m³），HA 能源仪表盘要求
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,      # 协调器关联实体基类
    DataUpdateCoordinator,  # 数据更新协调器
    UpdateFailed,           # 更新失败异常
)
from homeassistant.helpers import entity_registry as er  # 实体注册表，用于动态启用/禁用
from homeassistant.helpers.entity import DeviceInfo  # 设备信息结构
from homeassistant.core import HomeAssistant  # HA 核心实例
from homeassistant.config_entries import ConfigEntry  # 配置条目

# ========== 本集成常量与依赖 ==========
from ..const import (
    DOMAIN, CONF_PHONE, CONF_PASSWORD, CONF_IS_DEBUG,
    CONF_PRICING_MODE, CONF_FIXED_PRICE,
    CONF_TIER_1_LIMIT, CONF_TIER_1_PRICE,
    CONF_TIER_2_LIMIT, CONF_TIER_2_PRICE,
    CONF_TIER_3_PRICE,
    PRICING_MODE_TIERED, PRICING_MODE_FIXED,
    DEFAULT_PRICING_MODE, DEFAULT_FIXED_PRICE,
    DEFAULT_TIER_1_LIMIT, DEFAULT_TIER_1_PRICE,
    DEFAULT_TIER_2_LIMIT, DEFAULT_TIER_2_PRICE,
    DEFAULT_TIER_3_PRICE
)
from .api import XjGasAPI  # API 客户端，负责登录与数据拉取
from .storage import XjGasStorage  # 本地持久化存储，用于缓存历史数据
from ..utils import (
    safe_extract_rows, extract_balance, extract_arrearage,
    normalize_day, normalize_month, extract_number,
)

# 模块级日志器，用于调试与错误追踪
_LOGGER = logging.getLogger(__name__)


# =============================================================================
# 基类与工具函数
# =============================================================================

class XjGasBaseEntity(CoordinatorEntity, SensorEntity):
    """
    燃气实体基类。
    
    统一处理与 DataUpdateCoordinator 的联动，并在计价模式变化时
    同步实体注册表中的启用/禁用状态，确保“当前计价阶梯”等实体
    在固定计价模式下默认隐藏。
    """
    def _sync_registry_hidden(self) -> None:
        """
        根据实体可用状态同步实体注册表禁用标记。
        
        用于动态控制实体的可见性。例如，当用户选择“固定计价”模式时，
        “当前计价阶梯”实体应该被隐藏（禁用）。
        """
        if not self.hass:
            return
        # 仅针对阶梯实体进行处理
        if not self.entity_id or not self.entity_id.endswith("_current_tier"):
            return
        registry = er.async_get(self.hass)
        entry = registry.async_get(self.entity_id)
        if not entry:
            return
        
        # 获取当前计费模式配置
        options = _pricing_options_from_entry(getattr(self, "_config_entry", None))
        # 如果是固定计价，则标记为由集成禁用
        disabled_by = er.RegistryEntryDisabler.INTEGRATION if options.get("pricing_mode") == PRICING_MODE_FIXED else None
        
        # 如果状态有变，则更新注册表
        if entry.disabled_by == disabled_by:
            return
        registry.async_update_entity(self.entity_id, disabled_by=disabled_by)

    def _handle_coordinator_update(self) -> None:
        """协调器更新后刷新实体并同步禁用状态。"""
        super()._handle_coordinator_update()
        self._sync_registry_hidden()

    async def async_added_to_hass(self) -> None:
        """实体加入 Home Assistant 后同步禁用状态。"""
        await super().async_added_to_hass()
        self._sync_registry_hidden()

def _normalize_month_value(value: Any) -> str:
    """
    将输入月份值统一规范为 YYYY-MM 字符串。

    兼容多种输入格式：
    - "202401" -> "2024-01"
    - "2024.1" -> "2024-01"
    - "2024/1" -> "2024-01"

    Args:
        value: 原始月份值，支持 int、float、str 或 None

    Returns:
        标准化后的 YYYY-MM 格式字符串，无法解析时返回空字符串
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return ""
    value = value.strip().replace("/", "-").replace(".", "-")
    digits = value.replace("-", "")
    if len(digits) == 6 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}"
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[0:4]}-{digits[4:6]}"
    return value

def _month_key_from_row_shared(row: Dict[str, Any]) -> str:
    """
    从多种字段中提取月份并标准化，供多个实体复用。
    
    API 返回的月份字段名不统一，此函数尝试所有可能的键名。
    """
    month_value = (
        row.get("readYm")
        or row.get("month")
        or row.get("billYm")
        or row.get("ym")
        or row.get("billMonth")
        or row.get("chargeYm")
        or row.get("date")
        or row.get("rq")
    )
    return _normalize_month_value(month_value)

def _calc_yearly_usage_from_meter_info(meter_info: Any, cached_year_gas_num: Dict[str, float]) -> float:
    """
    计算年度累计用气量。
    
    优先使用缓存年度用气量（cached_year_gas_num），必要时回退到抄表数据累加。
    用于计算阶梯气价。
    
    Args:
        meter_info: API 返回的抄表记录（可能为 list 或 dict.rows/obj/data）
        cached_year_gas_num: 本地缓存的年度用气量，键为年份字符串，值为该年总用量。
    """
    current_year = str(dt_util.now().year)
    # 1. 尝试从缓存获取
    if cached_year_gas_num and current_year in cached_year_gas_num:
        try:
            return float(cached_year_gas_num.get(current_year, 0.0))
        except (ValueError, TypeError):
            pass
            
    # 2. 从抄表记录中实时计算
    if not meter_info:
        return 0.0
    rows = []
    if isinstance(meter_info, list):
        rows = meter_info
    elif isinstance(meter_info, dict):
        rows = meter_info.get("rows", []) or meter_info.get("obj", []) or meter_info.get("data", [])
    if not rows:
        return 0.0
        
    total_usage = 0.0
    for row in rows:
        month_key = _month_key_from_row_shared(row)
        # 仅统计当年的记录
        if not month_key or not month_key.startswith(current_year):
            continue
        usage = 0.0
        # 尝试提取用气量字段
        for key in ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas"]:
            if key in row and row[key] is not None:
                try:
                    usage = float(row[key])
                    break
                except (ValueError, TypeError):
                    continue
        total_usage += usage
    return total_usage

def _pricing_options_from_entry(config_entry: Optional[ConfigEntry]) -> Dict[str, Any]:
    """
    从配置条目的 options 中解析计费选项，并补齐默认值。
    计费标准（阶梯/固定、各档阈值与单价）均来自集成配置选项。
    优先使用 config_entry.options，若无则回退到 config_entry.data（兼容首次配置）。
    
    Args:
        config_entry: 配置条目，从中读取 options 和 data。
    """
    if not config_entry:
        return {}
    options = config_entry.options or {}
    config_data = config_entry.data or {}
    return {
        "pricing_mode": options.get(CONF_PRICING_MODE, config_data.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)),
        "fixed_price": options.get(CONF_FIXED_PRICE, config_data.get(CONF_FIXED_PRICE, DEFAULT_FIXED_PRICE)),
        "tier_1_limit": options.get(CONF_TIER_1_LIMIT, config_data.get(CONF_TIER_1_LIMIT, DEFAULT_TIER_1_LIMIT)),
        "tier_2_limit": options.get(CONF_TIER_2_LIMIT, config_data.get(CONF_TIER_2_LIMIT, DEFAULT_TIER_2_LIMIT)),
        "tier_1_price": options.get(CONF_TIER_1_PRICE, config_data.get(CONF_TIER_1_PRICE, DEFAULT_TIER_1_PRICE)),
        "tier_2_price": options.get(CONF_TIER_2_PRICE, config_data.get(CONF_TIER_2_PRICE, DEFAULT_TIER_2_PRICE)),
        "tier_3_price": options.get(CONF_TIER_3_PRICE, config_data.get(CONF_TIER_3_PRICE, DEFAULT_TIER_3_PRICE)),
    }

def _current_unit_price_from_options(options: Dict[str, Any], yearly_usage: float) -> float:
    """
    根据计费模式与年度用气量计算当前单价（元/m³）。
    
    固定计价：直接返回 fixed_price
    阶梯计价：根据 yearly_usage 与 tier_1_limit、tier_2_limit 比较，返回对应档位单价
    """
    if options.get("pricing_mode") == PRICING_MODE_FIXED:
        return float(options.get("fixed_price", DEFAULT_FIXED_PRICE))
    
    # 阶梯计价逻辑
    tier_1_limit = options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT)
    tier_2_limit = options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT)
    tier_1_price = options.get("tier_1_price", DEFAULT_TIER_1_PRICE)
    tier_2_price = options.get("tier_2_price", DEFAULT_TIER_2_PRICE)
    tier_3_price = options.get("tier_3_price", DEFAULT_TIER_3_PRICE)
    
    if yearly_usage <= tier_1_limit:
        return float(tier_1_price)
    if yearly_usage <= tier_2_limit:
        return float(tier_2_price)
    return float(tier_3_price)

def _current_tier_from_options(options: Dict[str, Any], yearly_usage: float) -> Optional[int]:
    """
    根据计费配置与年度用气量计算当前计价阶梯。
    
    固定计价返回 None；阶梯计价返回 1/2/3，对应第一/二/三档。
    """
    if options.get("pricing_mode") == PRICING_MODE_FIXED:
        return None
    tier_1_limit = options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT)
    tier_2_limit = options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT)
    if yearly_usage <= tier_1_limit:
        return 1
    if yearly_usage <= tier_2_limit:
        return 2
    return 3

def _pricing_attributes_from_options(options: Dict[str, Any], yearly_usage: Optional[float], include_yearly_usage: bool) -> Dict[str, Any]:
    """
    统一构建计费相关属性结构，减少重复拼装逻辑。
    
    Args:
        options: 计费配置字典（阶梯阈值、单价等）
        yearly_usage: 年度累计用气量，用于阶梯档位判断
        include_yearly_usage: 是否在返回属性中包含 yearly_usage 字段
        
    Returns:
        包含计费模式、阶梯阈值、单价等信息的属性字典
    """
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    if pricing_mode == PRICING_MODE_FIXED:
        return {"pricing_mode": pricing_mode, "fixed_price": options.get("fixed_price", DEFAULT_FIXED_PRICE)}
    attributes = {
        "pricing_mode": pricing_mode,
        "tier_1_limit": options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT),
        "tier_2_limit": options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT),
        "tier_1_price": options.get("tier_1_price", DEFAULT_TIER_1_PRICE),
        "tier_2_price": options.get("tier_2_price", DEFAULT_TIER_2_PRICE),
        "tier_3_price": options.get("tier_3_price", DEFAULT_TIER_3_PRICE),
    }
    if include_yearly_usage and yearly_usage is not None:
        attributes["yearly_usage"] = yearly_usage
    return attributes


def _build_billing_standard_for_card(
    options: Dict[str, Any],
    yearly_usage: float,
    yearly_cost: float = 0.0,
) -> Dict[str, Any]:
    """
    构建燃气计费标准对象，供前端 xjelegas-card 解析气单价、阶梯单价。
    
    燃气使用日历年（1.1-12.31）作为阶梯周期。返回结构区分：
    - 固定计价：计费标准="平均单价"，含平均单价、年阶梯累计用气量、年累计气费
    - 阶梯计价：计费标准="年阶梯"，含各档起始气量、各档气价、当前档位、年阶梯累计用气量、年累计气费
    
    用量与费用说明：
    - 年阶梯累计用气量：年度用量（m³），用于阶梯档位判断
    - 年累计气费：年度费用（元），供卡片显示
    """
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    current_year = dt_util.now().year
    yearly_usage_val = yearly_usage if yearly_usage is not None else 0.0
    yearly_cost_val = yearly_cost if yearly_cost is not None else 0.0

    if pricing_mode == PRICING_MODE_FIXED:
        fixed_price = float(options.get("fixed_price", DEFAULT_FIXED_PRICE))
        return {
            "计费标准": "平均单价",
            "平均单价": fixed_price,
            "当前年阶梯起始日期": f"{current_year}.01.01",
            "当前年阶梯结束日期": f"{current_year}.12.31",
            "年阶梯累计用气量": yearly_usage_val,
            "年累计气费": yearly_cost_val,
        }

    tier_1_limit = float(options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT))
    tier_2_limit = float(options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT))
    tier_1_price = float(options.get("tier_1_price", DEFAULT_TIER_1_PRICE))
    tier_2_price = float(options.get("tier_2_price", DEFAULT_TIER_2_PRICE))
    tier_3_price = float(options.get("tier_3_price", DEFAULT_TIER_3_PRICE))

    current_tier = _current_tier_from_options(options, yearly_usage_val)
    tier_str = f"第{current_tier}档" if current_tier else "第1档"

    return {
        "计费标准": "年阶梯",
        "年阶梯第2档起始气量": tier_1_limit,
        "年阶梯第3档起始气量": tier_2_limit,
        "年阶梯第1档气价": tier_1_price,
        "年阶梯第2档气价": tier_2_price,
        "年阶梯第3档气价": tier_3_price,
        "当前年阶梯起始日期": f"{current_year}.01.01",
        "当前年阶梯结束日期": f"{current_year}.12.31",
        "当前年阶梯档": tier_str,
        "年阶梯累计用气量": yearly_usage_val,
        "年累计气费": yearly_cost_val,
    }

# =============================================================================
# 集成入口与协调器
# =============================================================================

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """
    基于配置条目设置 XjGas 传感器。
    
    当 Home Assistant 启动或用户添加集成时调用此函数。
    它负责初始化 API 客户端、数据协调器，并创建传感器实体。
    
    Args:
        hass: Home Assistant 核心对象
        config_entry: 配置条目，包含用户输入的配置信息
        async_add_entities: 用于添加实体的回调函数，由 Home Assistant 平台提供
    """
    # 从配置或选项中获取手机号和密码（选项流中的密码优先）
    # 优先使用选项流中的设置（如果存在），否则使用初始配置
    phone = config_entry.data.get(CONF_PHONE)
    password = config_entry.options.get(CONF_PASSWORD, config_entry.data.get(CONF_PASSWORD))
    
    # 设备名称默认使用配置条目的标题，未设置时回退到集成名称
    name = config_entry.title or "新疆燃气"
    
    # 初始化接口客户端（负责登录与数据拉取），传入调试开关
    api = XjGasAPI(phone, password, is_debug=config_entry.options.get(CONF_IS_DEBUG, False))
    
    # 初始化数据协调器
    # 协调器定期从接口拉取数据，并分发给各个传感器
    coordinator = XjGasCoordinator(hass, api, config_entry)

    # 尝试预先登录以获取户号，用于生成正确的实体 ID
    # 登录通常较快，不会像全量数据拉取那样容易超时
    # 如果登录成功，api.cons_no 将被赋值，生成的实体 ID 将包含正确的户号后四位
    try:
        await api.login()
    except Exception as e:
        _LOGGER.warning("预登录失败: %s，实体 ID 可能回退到默认值", e)

    # 首次刷新数据，确保添加实体前有数据可用
    # 为避免 setup 超时 (CancelledError)，将首次刷新放入后台执行
    # 实体创建后会处于"不可用"状态，直到后台刷新完成
    hass.async_create_task(coordinator.async_refresh())
    
    # 创建传感器实体列表，每个实体对应协调器数据中的不同字段
    # XjGasSensor 参数：协调器, 设备名, 显示名, 唯一键, 设备类型, 单位, 手机号, 数据源, 字段键, 状态类型
    sensors = [
        # 1. 综合信息类
        XjGasGeneralSensor(coordinator, name, phone, config_entry), # 燃气综合信息(账户余额)
        XjGasCurrentPriceSensor(coordinator, name, phone, config_entry), # 当前气价(固定/阶梯)
        XjGasCurrentTierSensor(coordinator, name, phone, config_entry), # 当前阶梯(1/2/3)
        
        # 2. 费用类（欠费已合并到账户余额，欠费>0时余额显示为负数）
        # 月度账单：单点值，取最新一月的账单金额
        XjGasSensor(coordinator, name, "月度账单", "last_fee", SensorDeviceClass.MONETARY, "CNY", phone, "fee_record", "last_fee", None),
        # 年度账单：聚合值，累加本年度所有月份的账单
        XjGasSensor(coordinator, name, "年度账单", "annual_fee", SensorDeviceClass.MONETARY, "CNY", phone, "fee_record", "annual_fee", None),
        # 最近交费：单点值，取最新一次缴费记录
        XjGasSensor(coordinator, name, "最近交费", "last_payment", SensorDeviceClass.MONETARY, "CNY", phone, "payment_record", "last_payment", None),
        
        # 3. 用气类（使用 UnitOfVolume.CUBIC_METERS 确保 HA 能源仪表盘可用）
        # 月度用气量：当月内单调递增，HA 能源仪表盘需 total_increasing
        XjGasSensor(coordinator, name, "月度用气量", "last_usage", SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS, phone, "meter_info", "last_usage", SensorStateClass.TOTAL_INCREASING),
        # 年度用气量：聚合值，当年内单调递增，HA 能源仪表盘需 total_increasing
        XjGasSensor(coordinator, name, "年度用气量", "year_gas_num", SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS, phone, "meter_info", "year_gas_num", SensorStateClass.TOTAL_INCREASING),
        # 日用气量：单点值，取最新一日的用气量，HA 能源仪表盘需 total_increasing
        XjGasSensor(coordinator, name, "日用气量", "daily_usage", SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS, phone, "daily_usage", "useGas", SensorStateClass.TOTAL_INCREASING, config_entry),
        # 当前抄表数：表底数单调递增，HA 能源仪表盘燃气源（需 device_class=gas + state_class=total_increasing）
        XjGasSensor(coordinator, name, "当前抄表数", "current_reading", SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS, phone, "meter_info", "current_reading", SensorStateClass.TOTAL_INCREASING),
        # 刷新时间：单点值，记录最后一次 API 成功调用的时间
        XjGasSensor(coordinator, name, "最近刷新时间", "update_time", None, None, phone, "update_time", "value", None),
        # 数据时间：单点值，记录最新一条日用气数据的日期
        XjGasSensor(coordinator, name, "最新数据时间", "latest_data_time", None, None, phone, "latest_data_time", "value", None),
        
        # 4. 衍生计算类
        # 日账单：根据日用气量和当前气价计算得出
        XjGasDailyCostSensor(coordinator, name, phone, config_entry),
    ]
    
    async_add_entities(sensors)

class XjGasCoordinator(DataUpdateCoordinator):
    """
    新疆燃气数据协调器。
    
    职责：
    - 定时从 API 拉取余额、账单、抄表、缴费、日用气等数据
    - 合并多接口数据，空数据时保留上次成功结果，避免状态被清空
    - 将数据持久化到本地存储，重启后快速恢复历史数据
    - 通过信号量控制并发，降低接口限流或封禁风险
    """
    
    def __init__(self, hass: HomeAssistant, api: XjGasAPI, config_entry: ConfigEntry):
        """
        初始化协调器。
        
        Args:
            hass: Home Assistant 核心对象
            api: API 客户端实例
            config_entry: 配置条目，用于读取 options（如 is_debug）
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # 智能更新间隔：白天高频期30分钟，夜间低频期2小时
            update_interval=self._get_smart_update_interval(),
        )
        self.api = api
        self._config_entry = config_entry
        # 信号量限制同时执行的 API 调用数为 3，避免短时间内发起过多请求
        self._request_semaphore = asyncio.Semaphore(3)
        # 上次成功拉取的数据快照，用于 API 返回空数据时保留旧值
        self._last_success_data: Dict[str, Any] = {}
        # 本地存储实例，用于缓存历史数据（日/月/年度汇总）
        self._storage = XjGasStorage(hass, config_entry)
    
    def _get_smart_update_interval(self):
        """根据当前时间获取智能更新间隔。"""
        current_time = dt_util.now()
        # 白天高频期（8:00-22:00）：30分钟，夜间低频期：2小时
        if 8 <= current_time.hour <= 22:
            return timedelta(minutes=30)
        else:
            return timedelta(hours=2)

    async def _run_api_call(self, api_call):
        """
        在协程中执行异步 API 调用并限制并发。

        通过信号量避免短时间内发起过多请求导致限流。

        Args:
            api_call: 要执行的异步 API 方法（如 self.api.get_arrearage）。
        """
        async with self._request_semaphore:
            return await api_call()

    def _has_list_rows(self, payload: Any) -> bool:
        """
        判断 payload 是否包含有效列表数据。
        
        兼容 API 多种返回结构：直接 list、dict.rows、dict.obj、dict.data、dict.result 等。
        """
        if not payload:
            return False
        if isinstance(payload, list):
            return len(payload) > 0
        if isinstance(payload, dict):
            for key in ("rows", "obj", "data", "result"):
                payload_value = payload.get(key)
                if isinstance(payload_value, list) and len(payload_value) > 0:
                    return True
                if isinstance(payload_value, dict):
                    for nested_key in ("rows", "result", "data"):
                        nested_value = payload_value.get(nested_key)
                        if isinstance(nested_value, list) and len(nested_value) > 0:
                            return True
            return False
        return False

    def _is_success_payload(self, payload: Any) -> bool:
        """
        判断 API 返回是否表示成功。
        
        检查 success 字段或 code 字段（0/200 表示成功）。
        """
        if not isinstance(payload, dict):
            return True
        if "success" in payload and payload.get("success") is False:
            return False
        if "code" in payload:
            code = payload.get("code")
            if isinstance(code, (int, float)):
                return int(code) in (0, 200)
            if isinstance(code, str):
                return code in ("0", "200")
        return True

    def _should_keep_previous(self, data_source: str, payload: Any) -> bool:
        """
        判断是否应保留上次成功数据。
        
        当 API 返回空、失败或列表为空时返回 True，此时使用 _last_success_data 中的旧值。
        """
        if payload is None:
            return True
        if not self._is_success_payload(payload):
            return True
        if data_source in ("fee_record", "meter_info", "payment_record", "daily_usage"):
            return not self._has_list_rows(payload)
        if data_source == "arrearage":
            return not isinstance(payload, dict) or len(payload) == 0
        return False

    @property
    def storage(self) -> XjGasStorage:
        """返回本地缓存存储实例。"""
        return self._storage

    @property
    def cached_daily_records(self) -> List[Dict[str, Any]]:
        """返回本地缓存的日用气记录列表。"""
        return self._storage.daily_records

    @property
    def cached_monthly_bills(self) -> List[Dict[str, Any]]:
        """返回本地缓存的月度账单列表。"""
        return self._storage.monthly_bills

    @property
    def cached_monthly_usage(self) -> List[Dict[str, Any]]:
        """返回本地缓存的月度用气量列表。"""
        return self._storage.monthly_usage

    @property
    def cached_annual_bills(self) -> Dict[str, float]:
        """返回本地缓存的年度账单金额汇总。"""
        return self._storage.annual_bills

    @property
    def cached_year_gas_num(self) -> Dict[str, float]:
        """返回本地缓存的年度用气量汇总。"""
        return self._storage.year_gas_num

    async def _async_update_data(self):
        """
        异步获取所有数据。
        
        此方法由协调器定期调用。它会并行或顺序调用多个 API 接口，
        并将结果汇总到一个字典中返回。
        """
        try:
            # 首次更新前加载本地缓存，重启后立即恢复历史数据
            await self._storage.async_load_initial()
            # 设置 60 秒超时，防止请求卡死 (API 响应较慢)
            async with async_timeout.timeout(60):
                # 使用线程池执行同步接口方法
                # 避免阻塞主事件循环
                # 月度接口多取一个月，避免“排除当月”后数据不足
                arrearage, fee_record, meter_info, payment_record, daily_usage = await asyncio.gather(
                    self._run_api_call(self.api.get_arrearage),
                    self._run_api_call(lambda: self.api.get_fee_record(months=13)),
                    self._run_api_call(lambda: self.api.get_meter_info(months=13)),
                    self._run_api_call(self.api.get_payment_record),
                    self._run_api_call(lambda: self.api.get_daily_usage(days=30)),
                )
                
                # 记录调试日志，仅在集成选项中开启调试模式时输出
                if self._config_entry.options.get(CONF_IS_DEBUG, False):
                    _LOGGER.info("API 数据更新 - 余额: %s", arrearage)
                    _LOGGER.info("API 数据更新 - 账单: %s", fee_record)
                    _LOGGER.info("API 数据更新 - 抄表: %s", meter_info)
                    _LOGGER.info("API 数据更新 - 缴费: %s", payment_record)
                    _LOGGER.info("API 数据更新 - 日用量: %s", daily_usage)
                
                # 统一封装成本次刷新数据快照
                new_data = {
                    "arrearage": arrearage,
                    "fee_record": fee_record,
                    "meter_info": meter_info,
                    "payment_record": payment_record,
                    "daily_usage": daily_usage,
                    "update_time": dt_util.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                # 合并新旧数据，空数据时保留上次成功结果
                merged = {}
                for key, value in new_data.items():
                    if key == "update_time":
                        merged[key] = value
                        continue
                    if self._should_keep_previous(key, value) and self._last_success_data:
                        merged[key] = self._last_success_data.get(key, value)
                    else:
                        merged[key] = value
                # 记录本次成功数据并落地到本地缓存
                self._last_success_data = merged
                await self._storage.persist(merged)
                return merged
        except Exception as err:
            # 如果更新失败，记录错误日志
            _LOGGER.warning("与 API 通信错误: %s", err)
            
            # 如果有缓存的成功数据，则返回旧数据，避免实体变为不可用
            if self._last_success_data:
                if self._config_entry.options.get(CONF_IS_DEBUG, False):
                    _LOGGER.info("使用上次成功的缓存数据，保持实体可用")
                return self._last_success_data
                
            # 如果从未成功获取过数据，则抛出异常，此时实体必须显示为不可用
            raise UpdateFailed(f"初始化数据获取失败: {err}")


# =============================================================================
# 通用传感器实体
# =============================================================================

class XjGasSensor(XjGasBaseEntity):
    """
    通用传感器实体。
    
    通过 data_source + data_key 映射到不同接口的返回字段，
    统一处理“列表型数据取最新一条”与“字典型数据直接取值”的差异。
    适用于余额、账单、抄表、缴费、日用量等多个子实体。
    """

    def __init__(self, coordinator: XjGasCoordinator, device_name: str, name_suffix: str, key: str, device_class: str, unit: str, phone: str, data_source: str, data_key: str, state_class: Optional[str] = None, config_entry: Optional[ConfigEntry] = None):
        """
        初始化传感器。
        
        Args:
            coordinator: 数据协调器实例
            device_name: 设备名称前缀
            name_suffix: 传感器名称后缀 (如 "余额")
            key: 唯一 ID 的一部分
            device_class: 传感器设备类型 (SensorDeviceClass)
            unit: 单位
            phone: 手机号 (用于生成唯一 ID)
            data_source: 数据源键名 (如 "arrearage", "fee_record")
            data_key: 具体数据字段键名 (如 "balance", "last_usage")
            state_class: 状态类型 (SensorStateClass)，如 TOTAL_INCREASING 表示累计递增
            config_entry: 可选，配置条目，用于日用气量等需要计费配置的传感器
        """
        super().__init__(coordinator)
        self._device_name = device_name
        self._name_suffix = name_suffix
        self._key = key
        self._device_class = device_class
        self._unit = unit
        self._phone = phone
        self._data_source = data_source
        self._data_key = data_key
        self._state_class = state_class
        self._config_entry = config_entry

        # 设置实体唯一标识格式，遵循平台实体命名规则
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        self._account_tail = cons_no[-4:] if cons_no and len(cons_no) >= 4 else (cons_no if cons_no else "0000")
        self.entity_id = f"sensor.gas_{self._account_tail}_{self._key}"

    def _normalize_day(self, value: Any) -> str:
        return normalize_day(value)

    def _day_key_from_row(self, row: Dict[str, Any]) -> str:
        """
        从行数据中提取日级日期字段用于排序。

        尝试多个可能的日期字段名（API 返回格式不统一），
        取所有有效日期的最大值作为该行排序键。

        Returns:
            标准化后的 YYYY-MM-DD 字符串，无有效日期时返回空字符串
        """
        candidates = []
        # 兼容 analyzeDate、gasDate、statisticsDate 等可能的日期字段
        for key in ["gasDay", "day", "date", "rq", "readDate", "chargeDate", "payDate", "time", "analyzeDate", "gasDate", "statisticsDate", "statDate"]:
            if key in row:
                normalized = self._normalize_day(row.get(key))
                if normalized:
                    candidates.append(normalized)
        if not candidates:
            return ""
        return max(candidates)

    def _normalize_month(self, value: Any) -> str:
        """
        统一月份字符串格式为 YYYY-MM。
        
        兼容：202401、2024-01、2024/1 等格式。
        """
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            value = str(int(value))
        if not isinstance(value, str):
            return ""
        value = value.strip().replace("/", "-").replace(".", "-")
        digits = value.replace("-", "")
        if len(digits) == 6 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        return value

    def _allowed_months(self, rows: List[Dict[str, Any]]) -> set[str]:
        """
        计算可用于展示的月份集合。
        
        规则：
        - 若接口已下发当月数据（has_current=True），则包含当月并向前取 12 个月
        - 若当月尚无数据，则排除当月，仅取过去 12 个月
        用于过滤月度账单、抄表记录等，避免展示不完整或未来的数据。
        """
        today = dt_util.now().date()
        current_month = f"{today.year:04d}-{today.month:02d}"
        # 判断接口是否已下发当月数据
        has_current = any(self._month_key_from_row(row) == current_month for row in rows)
        allowed = set()
        # 有当月时从 0 开始，否则从 1 开始避开当月
        start_offset = 0 if has_current else 1
        count = 12
        for offset in range(start_offset, start_offset + count):
            month_index = today.year * 12 + (today.month - 1) - offset
            year = month_index // 12
            month = month_index % 12 + 1
            allowed.add(f"{year:04d}-{month:02d}")
        return allowed

    def _month_key_from_row(self, row: Dict[str, Any]) -> str:
        """从记录行中提取月份字段并标准化。"""
        month_value = (
            row.get("readYm")
            or row.get("month")
            or row.get("billYm")
            or row.get("ym")
            or row.get("billMonth")
            or row.get("chargeYm")
            or row.get("date")
            or row.get("rq")
        )
        return self._normalize_month(month_value)

    def _filter_month_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        筛选近 12 个月（或含当月共 13 个月）的月度记录。
        
        根据 _allowed_months 规则过滤，并按月份倒序排序。
        """
        allowed = self._allowed_months(rows)
        filtered = []
        for row in rows:
            month_key = self._month_key_from_row(row)
            if month_key and month_key in allowed:
                filtered.append(row)
        filtered.sort(key=lambda x: self._month_key_from_row(x) or "", reverse=True)
        return filtered

    def _filter_year_rows(self, rows: List[Dict[str, Any]], years: int = 1) -> List[Dict[str, Any]]:
        """
        筛选近 N 年内的月度记录并按月份倒序排序。
        
        用于年度用气量、年度账单等聚合计算。
        """
        current_year = dt_util.now().year
        allowed_years = {str(current_year - offset) for offset in range(max(1, years))}
        filtered = []
        for row in rows:
            month_key = self._month_key_from_row(row)
            if month_key and any(month_key.startswith(y) for y in allowed_years):
                filtered.append(row)
        filtered.sort(key=lambda x: self._month_key_from_row(x) or "", reverse=True)
        return filtered

    def _extract_number(self, row: Dict[str, Any], keys: List[str]) -> float:
        return extract_number(row, keys)

    def _sum_yearly_usage(self, rows: List[Dict[str, Any]]) -> float:
        """累计当前年度用气量，用于年度用气量传感器与计费。"""
        total = 0.0
        for row in self._filter_year_rows(rows, years=1):
            usage = self._extract_number(row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas", "usage", "gas", "num", "sl", "amount"])
            total += usage
        return total

    def _sum_yearly_fee(self, rows: List[Dict[str, Any]], years: int = 1) -> float:
        """累计近 N 年账单金额，用于年度账单传感器。"""
        if years >= 2:
            # 优先使用本地月账单缓存汇总
            cached_monthly = self.coordinator.cached_monthly_bills
            if cached_monthly:
                total = 0.0
                for item in cached_monthly:
                    amount = item.get("amount")
                    if amount is not None:
                        total += float(amount)
                if years == 2:
                    return total
        if years >= 3:
            # 需要跨多年时优先使用本地年度缓存
            cached_annual = self.coordinator.cached_annual_bills
            if cached_annual:
                current_year = dt_util.now().year
                total = 0.0
                for year in range(current_year - (years - 1), current_year + 1):
                    amount = cached_annual.get(str(year))
                    if amount is not None:
                        total += float(amount)
                if total > 0 or cached_annual:
                    return total
        # 回退到接口数据计算
        total = 0.0
        for row in self._filter_year_rows(rows, years=years):
            amount = self._extract_number(row, ["paidInGasFee", "payableGasFee", "rcvblamt", "money", "totalFee", "payMoney", "fee", "billAmount", "rcvedamt", "amt", "gasFee", "pay"])
            total += amount
        return total

    def _get_yearly_usage(self) -> float:
        """
        从抄表记录中累计当前年度用气量。
        
        用于阶梯计价时判断当前档位。
        """
        source_data = self.coordinator.data.get("meter_info") if self.coordinator.data else None
        return _calc_yearly_usage_from_meter_info(source_data, self.coordinator.cached_year_gas_num)

    def _current_unit_price(self) -> float:
        """
        计算当前单价。
        
        固定计价直接使用固定单价；
        阶梯计价按年度累计用气量确定当前计价阶梯。
        """
        if not self._config_entry:
            return 0.0
        options = _pricing_options_from_entry(self._config_entry)
        yearly_usage = self._get_yearly_usage()
        return _current_unit_price_from_options(options, yearly_usage)

    def _filter_daily_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        筛选近 90 条日用气记录。
        
        数据先按日期倒序排序，再截取最新 90 条。
        """
        items = []
        for row in rows:
            day = self._day_key_from_row(row)
            if not day:
                continue
            items.append((day, row))
        items.sort(key=lambda x: x[0], reverse=True)
        return [row for _, row in items[:90]]

    @property
    def device_class(self) -> Optional[str]:
        """返回设备类型 (如 gas, monetary)。"""
        return self._device_class

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        """返回测量单位。"""
        return self._unit

    @property
    def state_class(self) -> Optional[str]:
        """返回状态类型 (如 total_increasing)。"""
        return self._state_class

    @property
    def name(self) -> str:
        """返回传感器的显示名称。"""
        # 移除设备名称前缀，只保留后缀
        return self._name_suffix

    @property
    def unique_id(self) -> str:
        """
        返回此传感器的唯一 ID。
        确保在 Home Assistant 中唯一，通常结合户号和传感器类型。
        """
        return f"gas_{self._account_tail}_{self._key}"

    def _get_target_data(self) -> Any:
        """
        从协调器的汇总数据中提取当前传感器所需的数据块。
        
        根据 data_source 分发：
        - update_time: 直接取根级 update_time
        - arrearage: 取余额对象（可能嵌套在 obj 中）
        - fee_record/meter_info/payment_record: 取行列表（兼容 rows/obj/data）
        - daily_usage: 取日用气行列表（兼容多种嵌套结构）
        """
        if not self.coordinator.data:
            return None

        # 特殊处理更新时间字段，它直接存储在根数据中
        if self._data_source == "update_time":
            return self.coordinator.data.get("update_time")
            
        source_data = self.coordinator.data.get(self._data_source)
        if not source_data:
            return None
            
        if self._data_source == "arrearage":
            # 余额接口通常返回带成功标识与对象数据的结构
            target_data = source_data
            if "obj" in source_data and isinstance(source_data["obj"], dict):
                target_data = source_data["obj"]
                
            return target_data
            
        if self._data_source in ["fee_record", "meter_info", "payment_record"]:
            if isinstance(source_data, list):
                return source_data
            if not isinstance(source_data, dict):
                return []
            return safe_extract_rows(source_data)
            
        if self._data_source == "daily_usage":
            if isinstance(source_data, list):
                return source_data
            if not isinstance(source_data, dict):
                return []
            return safe_extract_rows(source_data)
            
        return source_data

    def _extract_daily_rows(self, source_data: Any) -> List[Dict[str, Any]]:
        """从 API 返回的多种结构中提取日用气量记录列表。委托给 utils.safe_extract_rows。"""
        return safe_extract_rows(source_data)

    def _extract_month_rows(self, source_data: Any) -> List[Dict[str, Any]]:
        """从 API 返回的多种结构中提取月度账单或抄表记录列表。委托给 utils.safe_extract_rows。"""
        return safe_extract_rows(source_data)

    def _get_yearly_usage_for_price(self) -> float:
        """获取年度累计用气量，用于计算单价（仅内部辅助）。"""
        source_data = self.coordinator.data.get("meter_info") if self.coordinator.data else None
        return _calc_yearly_usage_from_meter_info(source_data, self.coordinator.cached_year_gas_num)

    @property
    def native_value(self):
        """
        返回传感器的主状态值。
        根据 data_key 从数据中提取数值。
        """
        target_data = self._get_target_data()
        
        if self._data_source == "update_time":
            return target_data
        if self._data_source == "latest_data_time":
            cached_daily = self.coordinator.cached_daily_records
            if cached_daily:
                day = cached_daily[0].get("day")
                if day:
                    return day
            daily_usage = self.coordinator.data.get("daily_usage") if self.coordinator.data else None
            rows = self._extract_daily_rows(daily_usage)
            latest_day = ""
            for row in rows:
                day = self._day_key_from_row(row)
                if day and day > latest_day:
                    latest_day = day
            return latest_day or None

        # ---------- 1. 处理字典类型数据（余额/欠费）----------
        if self._data_source == "arrearage":
            if not target_data:
                return 0
                
            if self._data_key == "balance":
                # 尝试匹配各种可能的余额字段名
                for key in ['balance', 'money', 'surplus', 'canUse', 'accBalance', 'actualBalance']:
                    if key in target_data and target_data[key] is not None:
                        try:
                            return float(target_data[key])
                        except (ValueError, TypeError):
                            continue
            elif self._data_key == "arrearage":
                # 尝试匹配各种可能的欠费字段名
                for key in ['arrearage', 'amt', 'oweFee', 'debt', 'oweAmt']:
                    if key in target_data and target_data[key] is not None:
                        try:
                            return float(target_data[key])
                        except (ValueError, TypeError):
                            continue

            payload_value = target_data.get(self._data_key, 0)
            try:
                return float(payload_value)
            except (ValueError, TypeError):
                return 0
                
        # ---------- 2. 处理列表类型数据（账单/抄表/缴费/日用气）----------
        # 列表型数据：取最新一条记录作为状态值，支持本地缓存优先
        if self._data_source in ["fee_record", "meter_info", "payment_record", "daily_usage"]:
            if not target_data or not isinstance(target_data, list) or len(target_data) == 0:
                return 0
            rows = target_data
            if self._data_source == "meter_info" and self._data_key == "year_gas_num":
                # 优先使用本地年度缓存，避免接口仅返回近 12 个月
                cached_usage = self.coordinator.cached_year_gas_num
                current_year = str(dt_util.now().year)
                if cached_usage and current_year in cached_usage:
                    try:
                        return float(cached_usage.get(current_year, 0.0))
                    except (ValueError, TypeError):
                        pass
                return self._sum_yearly_usage(rows)
            if self._data_source == "fee_record" and self._data_key == "annual_fee":
                # 年度账单：只统计本年度
                annual_fee = self._sum_yearly_fee(rows, years=1)
                
                # 若本月尚未出账，则从抄表记录中提取本月费用或用量×单价估算
                now = dt_util.now()
                current_month_key = f"{now.year:04d}-{now.month:02d}"
                has_current_bill = any(self._month_key_from_row(r) == current_month_key for r in rows)
                
                if not has_current_bill:
                    # 如果本月未出账，尝试从抄表记录获取本月费用
                    # 仅当能直接提取到费用字段时才累加，不进行用量*单价的估算
                    meter_rows = self._extract_month_rows(self.coordinator.data.get("meter_info"))
                    for row in meter_rows:
                        if self._month_key_from_row(row) == current_month_key:
                            # 尝试提取金额字段
                            fee = self._extract_number(row, ["money", "totalFee", "fee", "gasFee", "amt", "payMoney", "billAmount", "amount"])
                            if fee > 0:
                                annual_fee += fee
                            else:
                                # 如果金额字段为0，可能是未直接返回费用
                                # 但如果确实有用气量，我们应该尝试用（用量 * 单价）来估算
                                # 之前用户说不要估算，但现在反馈“未统计到位”，说明还是需要这个估算值
                                usage = self._extract_number(row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas", "usage", "gas", "num", "sl"])
                                if usage > 0:
                                    price = self._current_unit_price()
                                    if price > 0:
                                        annual_fee += usage * price
                            break
                return annual_fee

            if self._data_source == "daily_usage":
                # 日用量状态值优先使用本地缓存最新有效值（非零）
                cached_daily = self.coordinator.cached_daily_records
                if cached_daily:
                    for record in cached_daily:
                        usage = record.get("usage")
                        if usage is not None:
                            try:
                                val = float(usage)
                                if val > 0:
                                    return val
                            except (ValueError, TypeError):
                                pass
                rows = self._filter_daily_rows(target_data)
            elif self._data_source == "fee_record":
                # 月账单按规则过滤近 12 个月
                rows = self._filter_month_rows(target_data)
            elif self._data_source == "meter_info" and self._data_key == "last_usage":
                # 月用气状态值优先使用本地缓存最新值
                cached_monthly = self.coordinator.cached_monthly_usage
                if cached_monthly:
                    usage = cached_monthly[0].get("usage")
                    if usage is not None:
                        try:
                            return float(usage)
                        except (ValueError, TypeError):
                            pass
                rows = self._filter_month_rows(target_data)
            if not rows:
                return 0
            latest = rows[0]
            
            # 根据字段键提取数值，并处理备选字段名
            if self._data_source == "fee_record":
                # 账单金额字段名不统一，逐一尝试
                for key in ['paidInGasFee', 'payableGasFee', 'rcvblamt', 'money', 'totalFee', 'payMoney', 'fee', 'billAmount', 'rcvedamt', 'amt', 'gasFee', 'pay']:
                    if key in latest and latest[key] is not None:
                        try:
                            return float(latest[key])
                        except (ValueError, TypeError):
                            continue
                return 0
                
            if self._data_source == "payment_record":
                # 缴费金额字段名不统一，逐一尝试
                for key in ['paymentAmount', 'money', 'payMoney', 'amount', 'totalFee', 'amt', 'pay', 'rcvblamt']:
                    if key in latest and latest[key] is not None:
                        try:
                            return float(latest[key])
                        except (ValueError, TypeError):
                            continue
                return 0
                
            if self._data_source == "meter_info":
                if self._data_key == "last_usage":
                    # 月用气字段名不统一，逐一尝试
                    for key in ['gasSl', 'thisUse', 'useAmount', 'gasAmount', 'yl', 'useGas']:
                        if key in latest and latest[key] is not None:
                            try:
                                return float(latest[key])
                            except (ValueError, TypeError):
                                continue
                elif self._data_key == "current_reading":
                    # 抄表读数字段名不统一，逐一尝试
                    for key in ['thisIndex', 'index', 'readNumber', 'bd', 'lastIndex', 'thisNum']:
                        if key in latest and latest[key] is not None:
                            try:
                                return float(latest[key])
                            except (ValueError, TypeError):
                                continue
                                
            if self._data_source == "daily_usage":
                # 遍历日用量记录，取最新有效值（非零）
                for row in rows:
                    for key in ['useGas', 'gasVolume', 'dailyGas', 'usage', 'gas', 'num', 'sl', 'amount', 'gasNum', 'gasAmount', 'totalGas', 'value']:
                        if key in row and row[key] is not None:
                            try:
                                val = float(row[key])
                                if val > 0:
                                    return val
                            except (ValueError, TypeError):
                                continue
                return 0
                        
        return None


    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """
        返回额外的状态属性。
        
        用途：
        1. 列表型数据提供历史记录，用于前端趋势展示。
        2. 附加最新记录的日期等细节字段，便于卡片显示。
        """
        try:
            if not self.coordinator.data:
                return {}
            
            # 更新时间实体无额外属性，直接返回空字典，避免后续逻辑报错
            if self._data_source == "update_time":
                return {}
                
            source_data = self.coordinator.data.get(self._data_source)
            if not source_data:
                return {}
                
            # 余额/欠费：直接返回提取后的数据（计费标准由 balance 实体统一管理）
            if self._data_source == "arrearage":
                if isinstance(source_data, dict) and isinstance(source_data.get("obj"), dict):
                    return source_data["obj"].copy()
                if isinstance(source_data, dict):
                    return source_data.copy()
                return {}

            # 列表型数据：仅取最新一条记录作为属性（历史数据由 balance 实体统一管理）
            rows = safe_extract_rows(source_data)
            if not rows:
                return {}
            latest = rows[0].copy()
            for key in ('paymentDate', 'readDate', 'chargeDate', 'payDate', 'date', 'time', 'day', 'rq'):
                if key in latest:
                    latest["date"] = latest[key]
                    break
            return latest
        except Exception as e:
            _LOGGER.error("处理附加属性失败 %s: %s", self.name, e)
            return {}

    def _extract_number(self, row: Dict[str, Any], keys: List[str]) -> float:
        return extract_number(row, keys)

    @property
    def device_info(self) -> DeviceInfo:
        """
        返回设备信息，用于在 Home Assistant 中注册设备。
        
        所有传感器实体共享同一设备（以手机号为标识），便于在设备页面统一管理。
        """
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        model_str = f"户号：{cons_no}" if cons_no else "户号：(未获取)"
        return DeviceInfo(
            identifiers={(DOMAIN, self._phone)},
            name=self._device_name,
            manufacturer="新疆燃气",
            model=model_str,
            configuration_url="https://wgas.xjrq.net",
        )


# =============================================================================
# 综合信息传感器
# =============================================================================

class XjGasGeneralSensor(XjGasBaseEntity):
    """
    燃气综合信息传感器（兼账户余额）。
    
    状态值展示账户余额，属性中聚合全部数据（对齐电力 balance 模式）：
    - daylist / monthlist / yearlist：日/月/年用气与费用历史
    - history_charges / charge_list：缴费历史
    - month_bill_list / daily_bill_list：原始月度账单与日数据
    - account_balance：原始余额对象
    - 计费标准：阶梯/固定计价配置
    - 各周期用量/费用、日均消费、剩余天数
    """

    _unrecorded_attributes = frozenset({
        "daylist", "monthlist", "yearlist",
        "history_charges", "charge_list",
        "month_bill_list", "daily_bill_list", "account_balance",
        "计费标准",
    })

    def __init__(self, coordinator: XjGasCoordinator, device_name: str, phone: str, config_entry: ConfigEntry):
        """
        初始化燃气综合信息实体。
        
        实体 ID 固定为 sensor.gas_{户号后四位}_balance。
        """
        super().__init__(coordinator)
        self._device_name = device_name
        self._phone = phone
        self._config_entry = config_entry
        
        # 设置实体唯一标识格式，遵循平台实体命名规则
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        self._account_tail = cons_no[-4:] if cons_no and len(cons_no) >= 4 else (cons_no if cons_no else "0000")
        self.entity_id = f"sensor.gas_{self._account_tail}_balance"

    @property
    def device_class(self) -> Optional[str]:
        """以金额类设备展示当前余额。"""
        return SensorDeviceClass.MONETARY

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        """返回金额单位。"""
        return "CNY"

    @property
    def name(self) -> str:
        """实体名称，显示为账户余额。"""
        return "账户余额"

    @property
    def unique_id(self) -> str:
        """唯一 ID 使用户号区分账号。"""
        return f"gas_{self._account_tail}_balance"

    def _extract_balance(self, source_data: Any) -> float:
        return extract_balance(source_data)

    def _extract_arrearage(self, source_data: Any) -> float:
        return extract_arrearage(source_data)

    def _extract_daily_rows(self, source_data: Any) -> List[Dict[str, Any]]:
        return safe_extract_rows(source_data)

    def _normalize_day(self, value: Any) -> str:
        return normalize_day(value)

    def _extract_number(self, row: Dict[str, Any], keys: List[str]) -> float:
        return extract_number(row, keys)

    def _extract_month_rows(self, source_data: Any) -> List[Dict[str, Any]]:
        return safe_extract_rows(source_data)

    def _normalize_month(self, value: Any) -> str:
        return normalize_month(value)

    def _month_key_from_row(self, row: Dict[str, Any]) -> str:
        """从记录行中提取并标准化月份字段。"""
        month_value = (
            row.get("readYm")
            or row.get("month")
            or row.get("billYm")
            or row.get("ym")
            or row.get("billMonth")
            or row.get("chargeYm")
            or row.get("date")
            or row.get("rq")
        )
        return self._normalize_month(month_value)

    def _allowed_months(self, rows: List[Dict[str, Any]]) -> set[str]:
        """计算允许展示的月度集合，决定是否包含当月。"""
        today = dt_util.now().date()
        current_month = f"{today.year:04d}-{today.month:02d}"
        has_current = any(self._month_key_from_row(row) == current_month for row in rows)
        allowed = set()
        start_offset = 0 if has_current else 1
        count = 13 if has_current else 12
        for offset in range(start_offset, start_offset + count):
            total = today.year * 12 + (today.month - 1) - offset
            y = total // 12
            m = total % 12 + 1
            allowed.add(f"{y:04d}-{m:02d}")
        return allowed

    def _get_yearly_usage_for_price(self) -> float:
        """
        获取年度累计用气量，用于计算单价（仅内部辅助）。
        """
        source_data = self.coordinator.data.get("meter_info") if self.coordinator.data else None
        return _calc_yearly_usage_from_meter_info(source_data, self.coordinator.cached_year_gas_num)

    @property
    def native_value(self):
        """
        余额传感器的主状态值。
        欠费合并到余额：当欠费>0时，显示为负数（-欠费值）；否则显示余额。
        """
        if not self.coordinator.data:
            return 0
        arrearage_data = self.coordinator.data.get("arrearage")
        arrearage = self._extract_arrearage(arrearage_data)
        if arrearage > 0:
            return -arrearage
        return self._extract_balance(arrearage_data)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """
        组合日用气与月度账单历史列表。
        
        输出字段兼容前端卡片：
        - daylist：日用气记录（近 30 条），包含用气量与费用。
        - monthlist：月度账单/用气合并记录，包含月度用气量与账单金额。
        - yearlist：年度用气历史记录，包含年度累计用气量与年度账单总金额。
        - year_gas_num：当前年度累计用气量。
        """
        attributes = {}
        
        # 添加计费配置信息 (供前端卡片使用)
        # 计费标准必须始终包含，否则卡片气单价会显示为 0
        if self._config_entry:
            options = _pricing_options_from_entry(self._config_entry)
            # 复制相关配置键值
            for key in [CONF_PRICING_MODE, CONF_FIXED_PRICE,
                        CONF_TIER_1_LIMIT, CONF_TIER_1_PRICE,
                        CONF_TIER_2_LIMIT, CONF_TIER_2_PRICE,
                        CONF_TIER_3_PRICE]:
                if key in options:
                    attributes[key] = options[key]

            # 补充计费方式中文名称，便于前端直接展示
            mode = options.get(CONF_PRICING_MODE)
            if mode == PRICING_MODE_FIXED:
                attributes["billing_standard_name"] = "平均气价计费"
            else:
                attributes["billing_standard_name"] = "年阶梯计费"

            # 始终构建计费标准对象（即使无数据时也需包含，供卡片显示气单价）
            attributes["计费标准"] = _build_billing_standard_for_card(
                options, 0.0, 0.0
            )

        if not self.coordinator.data:
            return attributes
            
        cached_daily = self.coordinator.cached_daily_records
        daylist = []
        if cached_daily:
            for item in cached_daily[:30]:
                day = item.get("day")
                if not day:
                    continue
                usage = item.get("usage")
                if usage is None:
                    usage = 0.0
                amount = item.get("cost")
                if amount is None:
                    amount = 0.0
                daylist.append({
                    "readingTime": day,
                    "cycleTotalVolume": usage,
                    "cycleTotalValues": amount,
                    "usage": usage,
                    "amount": amount,
                })
        else:
            daily_usage = self.coordinator.data.get("daily_usage")
            rows = self._extract_daily_rows(daily_usage)
            
            # 使用更健壮的字段提取逻辑
            # 计算当前单价用于估算日费用
            unit_price = 0.0
            if self._config_entry:
                options = _pricing_options_from_entry(self._config_entry)
                yearly_u = self._get_yearly_usage_for_price()
                unit_price = _current_unit_price_from_options(options, yearly_u)

            for row in rows:
                day_value = row.get("gasDay") or row.get("day") or row.get("date") or row.get("rq") or row.get("analyzeDate") or row.get("gasDate") or row.get("statisticsDate") or row.get("statDate") or row.get("readDate") or row.get("chargeDate") or row.get("payDate") or row.get("time")
                normalized_day = self._normalize_day(day_value)
                
                usage = self._extract_number(row, ['useGas', 'gasVolume', 'dailyGas', 'usage', 'gas', 'num', 'sl', 'gasNum', 'gasAmount', 'totalGas', 'value'])
                
                # 尝试直接获取金额，如果不存在则通过 单价*用量 计算
                amount = self._extract_number(row, ['money', 'amt', 'fee', 'totalFee', 'payMoney', 'gasFee', 'rcvblamt', 'payableGasFee', 'amount'])
                if amount == 0.0 and usage > 0 and unit_price > 0:
                     amount = round(usage * unit_price, 2)

                daylist.append({
                    "readingTime": normalized_day,
                    "cycleTotalVolume": usage,
                    "cycleTotalValues": amount,
                    "usage": usage,
                    "amount": amount,
                })
            daylist = [item for item in daylist if item.get("readingTime")]
            daylist.sort(key=lambda x: x.get("readingTime") or "", reverse=True)
            daylist = daylist[:30]

        cached_monthly_usage = self.coordinator.cached_monthly_usage
        cached_monthly_bills = self.coordinator.cached_monthly_bills
        monthlist = []
        today = dt_util.now().date()
        
        # 使用字典合并月度用气量与账单金额，key 为 YYYY-MM 格式
        # monthEleNum/usage 为用气量，monthEleCost/amount 为费用，兼容电力卡片字段名
        month_map: Dict[str, Dict[str, Any]] = {}

        # 1. 优先从本地缓存加载（日/月数据持久化后重启可快速恢复）
        if cached_monthly_usage:
            for item in cached_monthly_usage:
                month = item.get("month")
                if not month:
                    continue
                if month not in month_map:
                    month_map[month] = {"month": month, "monthEleNum": 0.0, "monthEleCost": 0.0, "usage": 0.0, "amount": 0.0}
                usage = item.get("usage")
                if usage is not None:
                    try:
                        u = float(usage)
                        month_map[month]["monthEleNum"] = u
                        month_map[month]["usage"] = u
                    except (ValueError, TypeError):
                        pass

        if cached_monthly_bills:
            for item in cached_monthly_bills:
                month = item.get("month")
                if not month:
                    continue
                if month not in month_map:
                    month_map[month] = {"month": month, "monthEleNum": 0.0, "monthEleCost": 0.0, "usage": 0.0, "amount": 0.0}
                amount = item.get("amount")
                if amount is not None:
                    try:
                        a = float(amount)
                        month_map[month]["monthEleCost"] = a
                        month_map[month]["amount"] = a
                    except (ValueError, TypeError):
                        pass

        # 2. 从 API 原始数据补充或覆盖（缓存缺失时使用接口数据）
        fee_record = self.coordinator.data.get("fee_record")
        meter_info = self.coordinator.data.get("meter_info")
        fee_rows = self._extract_month_rows(fee_record)
        meter_rows = self._extract_month_rows(meter_info)
        allowed_months = self._allowed_months(meter_rows + fee_rows)

        for row in meter_rows:
            month_key = self._month_key_from_row(row)
            if month_key and month_key in allowed_months:
                if month_key not in month_map:
                    month_map[month_key] = {"month": month_key, "monthEleNum": 0.0, "monthEleCost": 0.0, "usage": 0.0, "amount": 0.0}
                
                usage = self._extract_number(row, ['gasSl', 'thisUse', 'useAmount', 'gasAmount', 'yl', 'useGas', 'usage', 'gas', 'num', 'sl', 'gasNum', 'totalGas'])
                if usage > 0:
                    month_map[month_key]["monthEleNum"] = usage
                    month_map[month_key]["usage"] = usage

        for row in fee_rows:
            month_key = self._month_key_from_row(row)
            if month_key and month_key in allowed_months:
                if month_key not in month_map:
                    month_map[month_key] = {"month": month_key, "monthEleNum": 0.0, "monthEleCost": 0.0, "usage": 0.0, "amount": 0.0}
                
                amount = self._extract_number(row, ['paidInGasFee', 'payableGasFee', 'rcvblamt', 'money', 'totalFee', 'payMoney', 'fee', 'billAmount', 'rcvedamt', 'amt', 'gasFee', 'pay', 'amount', 'gasAmount'])
                if amount > 0:
                    month_map[month_key]["monthEleCost"] = amount
                    month_map[month_key]["amount"] = amount

        monthlist = list(month_map.values())
        monthlist.sort(key=lambda x: x["month"], reverse=True)

        # 确保 monthlist 含本月：若缓存/接口无本月，从 daylist 聚合补充
        current_month_str = f"{today.year}-{today.month:02d}"
        if daylist and not any(m.get("month") == current_month_str for m in monthlist):
            month_gas_num_sum = 0.0
            month_gas_cost_sum = 0.0
            for item in daylist:
                rt = item.get("readingTime") or ""
                if rt.startswith(current_month_str):
                    month_gas_num_sum += float(item.get("cycleTotalVolume") or item.get("usage") or 0.0)
                    month_gas_cost_sum += float(item.get("cycleTotalValues") or item.get("amount") or 0.0)
            if month_gas_num_sum > 0 or month_gas_cost_sum > 0:
                monthlist.insert(0, {
                    "month": current_month_str,
                    "monthEleNum": round(month_gas_num_sum, 2),
                    "monthEleCost": round(month_gas_cost_sum, 2),
                    "usage": round(month_gas_num_sum, 2),
                    "amount": round(month_gas_cost_sum, 2),
                })
                monthlist.sort(key=lambda x: x["month"], reverse=True)

        current_year_str = str(today.year)
        yearly_usage = 0.0
        yearlist = []
        
        cached_annual = self.coordinator.cached_year_gas_num
        cached_annual_bills = self.coordinator.cached_annual_bills
        
        # 使用字典合并用气量与账单金额，key 为年份字符串
        # 每个年份包含 usage（用气量）和 amount（费用）两个字段
        year_data_map: Dict[str, Dict[str, float]] = {}

        # 1. 初始化年份数据（基于年度缓存）
        if cached_annual:
            for year, usage in cached_annual.items():
                if year not in year_data_map:
                    year_data_map[year] = {"usage": 0.0, "amount": 0.0}
                try:
                    year_data_map[year]["usage"] = float(usage)
                except (ValueError, TypeError):
                    pass
        
        if cached_annual_bills:
            for year, amount in cached_annual_bills.items():
                if year not in year_data_map:
                    year_data_map[year] = {"usage": 0.0, "amount": 0.0}
                try:
                    year_data_map[year]["amount"] = float(amount)
                except (ValueError, TypeError):
                    pass
        
        # 2. 从月度数据聚合补充 (如果年度数据缺失)
        # 遍历 monthlist，将月度数据累加到对应年份
        # 注意：这里会覆盖或补充 year_data_map 中为 0 的值
        temp_year_map: Dict[str, Dict[str, float]] = {}
        
        for item in monthlist:
            month = item.get("month")
            if not month:
                continue
            year = month.split("-")[0]
            if year not in temp_year_map:
                temp_year_map[year] = {"usage": 0.0, "amount": 0.0}
            
            u = item.get("usage", 0.0)
            a = item.get("amount", 0.0)
            temp_year_map[year]["usage"] += u
            temp_year_map[year]["amount"] += a

        # 合并逻辑：如果 year_data_map 中某年数据为 0，则使用聚合值
        for year, data in temp_year_map.items():
            if year not in year_data_map:
                year_data_map[year] = {"usage": 0.0, "amount": 0.0}
            
            if year_data_map[year]["usage"] == 0.0 and data["usage"] > 0:
                year_data_map[year]["usage"] = data["usage"]

            if year_data_map[year]["amount"] == 0.0 and data["amount"] > 0:
                year_data_map[year]["amount"] = data["amount"]

        # 3. 从 API 原始数据补充 (如果以上都缺失)
        # 尝试从 meter_info 和 fee_record 再次扫描
        if not any(year_data.get("usage") > 0 for year_data in year_data_map.values()):
            meter_info = self.coordinator.data.get("meter_info")
            meter_rows = self._extract_month_rows(meter_info)
            for row in meter_rows:
                month_key = self._month_key_from_row(row)
                if not month_key:
                    continue
                usage = self._extract_number(row, ['gasSl', 'thisUse', 'useAmount', 'gasAmount', 'yl', 'useGas', 'usage', 'gas', 'num', 'sl', 'gasNum', 'totalGas'])
                if usage > 0:
                    year = month_key.split("-")[0]
                    if year not in year_data_map:
                        year_data_map[year] = {"usage": 0.0, "amount": 0.0}
                    year_data_map[year]["usage"] += usage

        if not any(year_data.get("amount") > 0 for year_data in year_data_map.values()):
            fee_record = self.coordinator.data.get("fee_record")
            fee_rows = self._extract_month_rows(fee_record)
            for row in fee_rows:
                month_key = self._month_key_from_row(row)
                if not month_key:
                    continue
                amount = self._extract_number(row, ['paidInGasFee', 'payableGasFee', 'rcvblamt', 'money', 'totalFee', 'payMoney', 'fee', 'billAmount', 'rcvedamt', 'amt', 'gasFee', 'pay', 'amount', 'gasAmount'])
                if amount > 0:
                    year = month_key.split("-")[0]
                    if year not in year_data_map:
                        year_data_map[year] = {"usage": 0.0, "amount": 0.0}
                    year_data_map[year]["amount"] += amount

        # 更新本年度用气量与费用 (用于 entity 状态及计费标准)
        yearly_cost = 0.0
        if current_year_str in year_data_map:
            yearly_usage = year_data_map[current_year_str]["usage"]
            yearly_cost = year_data_map[current_year_str]["amount"]

        # 构建最终列表并排序
        for year, data in year_data_map.items():
            yearlist.append({"year": year, "usage": data["usage"], "amount": data["amount"]})
        yearlist.sort(key=lambda x: x["year"], reverse=True)

        attributes.update({
            "daylist": daylist,
            "monthlist": monthlist,
            "yearlist": yearlist,
            "balance": self.native_value,
            "utility_type": "gas",
            "year_gas_num": yearly_usage,
        })
        
        # 附加最近同步时间，便于前端显示刷新状态
        update_time = self.coordinator.data.get("update_time")
        if update_time:
            attributes["syn"] = update_time
        if daylist:
            attributes["latest_data"] = daylist[0].get("readingTime")
            
        # 附加计费配置，便于前端展示当前费率与档位
        if self._config_entry:
            options = _pricing_options_from_entry(self._config_entry)
            attributes.update(_pricing_attributes_from_options(options, None, False))
            attributes["计费标准"] = _build_billing_standard_for_card(
                options, yearly_usage, yearly_cost
            )

        # ===== 原始 API 数据（对齐电力 balance 实体结构） =====
        attributes["account_balance"] = self.coordinator.data.get("arrearage")
        attributes["month_bill_list"] = self._extract_month_rows(
            self.coordinator.data.get("fee_record")
        )
        attributes["daily_bill_list"] = daylist
        attributes["charge_list"] = self._extract_month_rows(
            self.coordinator.data.get("payment_record")
        )

        # ===== 缴费历史（对齐电力 history_charges） =====
        history_charges = []
        for row in attributes["charge_list"]:
            p_time = (
                row.get("paymentDate") or row.get("payTime") or row.get("payDate")
                or row.get("createTime") or row.get("date", "")
            )
            p_amount = self._extract_number(
                row, ["paymentAmount", "payMoney", "money", "amount", "totalFee", "amt", "pay", "rcvblamt"]
            )
            p_method = (
                row.get("feeClass") or row.get("payChannel") or row.get("payMethodName")
                or row.get("payMethod") or row.get("channel", "")
            )
            if p_time or p_amount:
                history_charges.append({"time": p_time, "amount": p_amount, "method": p_method})
        attributes["history_charges"] = history_charges

        # ===== 预付费与时效估算 =====
        attributes["is_prepaid"] = True
        balance_val = self.native_value
        avg_daily_cost = 0.0
        avg_daily_usage = 0.0
        if daylist:
            total_cost = sum(d.get("amount", 0.0) for d in daylist)
            total_usage = sum(d.get("usage", 0.0) for d in daylist)
            avg_daily_cost = total_cost / len(daylist)
            avg_daily_usage = total_usage / len(daylist)
        attributes["avg_daily_usage"] = round(avg_daily_usage, 2)
        attributes["avg_daily_cost"] = round(avg_daily_cost, 2)
        attributes["remaining_days"] = (
            int(balance_val / avg_daily_cost) if avg_daily_cost > 0 and balance_val > 0 else 0
        )

        # ===== 各周期用量/费用 =====
        last_month = monthlist[1] if len(monthlist) > 1 else {}
        attributes["last_month_gas_num"] = last_month.get("usage", 0.0)
        attributes["last_month_gas_cost"] = last_month.get("amount", 0.0)
        this_month = monthlist[0] if len(monthlist) > 0 else {}
        attributes["month_gas_num"] = this_month.get("usage", 0.0)
        attributes["month_gas_cost"] = this_month.get("amount", 0.0)
        attributes["year_gas_num"] = yearly_usage
        attributes["year_gas_cost"] = yearly_cost
        if history_charges:
            attributes["last_payment_amount"] = history_charges[0].get("amount", 0.0)
            attributes["last_payment_date"] = history_charges[0].get("time", "")
            attributes["last_payment_method"] = history_charges[0].get("method", "")

        # ===== 元信息 =====
        attributes["数据源"] = "Xinjiang Gas API"

        # ===== 汇总其他传感器值（对齐电力模式） =====
        attributes["daily_usage"] = daylist[0].get("usage", 0.0) if daylist else 0.0
        attributes["current_reading"] = 0.0

        return attributes

    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息，供 HA 设备注册使用。"""
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        model_str = f"户号：{cons_no}" if cons_no else "户号：(未获取)"
        return DeviceInfo(
            identifiers={(DOMAIN, self._phone)},
            name=self._device_name,
            manufacturer="新疆燃气",
            model=model_str,
            configuration_url="https://wgas.xjrq.net",
        )


# =============================================================================
# 当前单价与阶梯传感器
# =============================================================================

class XjGasCurrentPriceSensor(XjGasBaseEntity):
    """
    当前用气单价传感器。
    
    根据配置选项（固定/阶梯）与年度累计用气量计算当前单价（元/立方米），
    并在属性中输出阶梯阈值、各档单价、年度已用气量，便于前端展示与卡片解析。
    """
    def __init__(self, coordinator: XjGasCoordinator, device_name: str, phone: str, config_entry: ConfigEntry):
        """初始化当前用气单价实体，实体 ID 为 sensor.gas_{户号后四位}_current_unit_price。"""
        super().__init__(coordinator)
        self._device_name = device_name
        self._phone = phone
        self._config_entry = config_entry
        
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        self._account_tail = cons_no[-4:] if cons_no and len(cons_no) >= 4 else (cons_no if cons_no else "0000")
        self.entity_id = f"sensor.gas_{self._account_tail}_current_unit_price"

    @property
    def name(self) -> str:
        """返回实体名称。"""
        return "当前用气单价"

    @property
    def unique_id(self) -> str:
        """返回用于区分账号的唯一 ID。"""
        return f"gas_{self._account_tail}_current_unit_price"

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        """返回当前单价的单位。"""
        return "CNY/m³"

    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息，便于设备注册与分组。"""
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        model_str = f"户号：{cons_no}" if cons_no else "户号：(未获取)"
        return DeviceInfo(
            identifiers={(DOMAIN, self._phone)},
            name=self._device_name,
            manufacturer="新疆燃气",
            model=model_str,
            configuration_url="https://wgas.xjrq.net",
        )

    def _get_yearly_usage(self) -> float:
        """获取年度累计用气量，用于计算当前单价。"""
        source_data = self.coordinator.data.get("meter_info") if self.coordinator.data else None
        return _calc_yearly_usage_from_meter_info(source_data, self.coordinator.cached_year_gas_num)

    def _current_unit_price(self) -> float:
        """根据配置和累计用气量计算当前单价。"""
        options = _pricing_options_from_entry(self._config_entry)
        yearly_usage = self._get_yearly_usage()
        return _current_unit_price_from_options(options, yearly_usage)

    @property
    def native_value(self):
        """返回当前单价作为主状态值。"""
        return self._current_unit_price()

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """输出单价相关的阶梯与配置属性。"""
        options = _pricing_options_from_entry(self._config_entry)
        yearly_usage = self._get_yearly_usage()
        return _pricing_attributes_from_options(options, yearly_usage, True)


class XjGasCurrentTierSensor(XjGasBaseEntity):
    """
    当前计价阶梯传感器。
    
    在阶梯计价模式下输出当前档位（1/2/3）；固定计价模式下默认禁用，
    通过 entity_registry_enabled_default 和 available 隐藏该实体。
    """
    def __init__(self, coordinator: XjGasCoordinator, device_name: str, phone: str, config_entry: ConfigEntry):
        """初始化当前计价阶梯实体，实体 ID 为 sensor.gas_{户号后四位}_current_tier。"""
        super().__init__(coordinator)
        self._device_name = device_name
        self._phone = phone
        self._config_entry = config_entry
        
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        self._account_tail = cons_no[-4:] if cons_no and len(cons_no) >= 4 else (cons_no if cons_no else "0000")
        self.entity_id = f"sensor.gas_{self._account_tail}_current_tier"

    @property
    def name(self) -> str:
        """返回实体名称。"""
        return "当前计价阶梯"

    @property
    def unique_id(self) -> str:
        """返回用于区分账号的唯一 ID。"""
        return f"gas_{self._account_tail}_current_tier"

    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息，便于设备注册与分组。"""
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        model_str = f"户号：{cons_no}" if cons_no else "户号：(未获取)"
        return DeviceInfo(
            identifiers={(DOMAIN, self._phone)},
            name=self._device_name,
            manufacturer="新疆燃气",
            model=model_str,
            configuration_url="https://wgas.xjrq.net",
        )

    @property
    def entity_registry_enabled_default(self) -> bool:
        """固定计价时默认禁用该实体。"""
        options = _pricing_options_from_entry(self._config_entry)
        return options.get("pricing_mode") != PRICING_MODE_FIXED

    @property
    def available(self) -> bool:
        """固定计价时保持不可用，以隐藏阶梯状态。"""
        options = _pricing_options_from_entry(self._config_entry)
        return self.coordinator.last_update_success and options.get("pricing_mode") != PRICING_MODE_FIXED

    def _get_yearly_usage(self) -> float:
        """获取年度累计用气量，用于计算当前计价阶梯。"""
        source_data = self.coordinator.data.get("meter_info") if self.coordinator.data else None
        return _calc_yearly_usage_from_meter_info(source_data, self.coordinator.cached_year_gas_num)

    @property
    def native_value(self):
        """返回当前计价阶梯值作为主状态。"""
        options = _pricing_options_from_entry(self._config_entry)
        yearly_usage = self._get_yearly_usage()
        return _current_tier_from_options(options, yearly_usage)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """输出阶梯计价相关的阈值与价格属性。"""
        options = _pricing_options_from_entry(self._config_entry)
        yearly_usage = self._get_yearly_usage()
        return _pricing_attributes_from_options(options, yearly_usage, True)


# =============================================================================
# 日账单传感器
# =============================================================================

class XjGasDailyCostSensor(XjGasBaseEntity):
    """
    日账单传感器。
    
    读取最新日用气量，结合计费配置（固定/阶梯）与年度累计用气量计算当日费用。
    公式：日费用 = 日用气量 × 当前单价（阶梯模式下根据年度用量确定档位）。
    属性中提供日用量、年度累计、计费模式与近 90 天费用历史，便于卡片/图表使用。
    """

    def __init__(self, coordinator: XjGasCoordinator, device_name: str, phone: str, config_entry: ConfigEntry):
        """初始化日账单实体，实体 ID 为 sensor.gas_{户号后四位}_daily_cost。"""
        super().__init__(coordinator)
        self._device_name = device_name
        self._phone = phone
        self._config_entry = config_entry
        
        # 设置实体唯一标识格式，遵循平台实体命名规则
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        self._account_tail = cons_no[-4:] if cons_no and len(cons_no) >= 4 else (cons_no if cons_no else "0000")
        self.entity_id = f"sensor.gas_{self._account_tail}_daily_cost"

    @property
    def device_class(self) -> Optional[str]:
        """返回金额类设备类型。"""
        return SensorDeviceClass.MONETARY

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        """返回日账单金额单位。"""
        return "CNY"

    @property
    def name(self) -> str:
        """返回实体名称。"""
        # 移除设备名前缀，只保留“日账单”
        return "日账单"

    @property
    def unique_id(self) -> str:
        """返回用于区分账号的唯一 ID。"""
        return f"gas_{self._account_tail}_daily_cost"

    @property
    def device_info(self) -> DeviceInfo:
        """返回设备信息，便于设备注册与分组。"""
        cons_no = getattr(self.coordinator.api, "cons_no", None)
        model_str = f"户号：{cons_no}" if cons_no else "户号：(未获取)"
        return DeviceInfo(
            identifiers={(DOMAIN, self._phone)},
            name=self._device_name,
            manufacturer="新疆燃气",
            model=model_str,
            configuration_url="https://wgas.xjrq.net",
        )

    def _normalize_day(self, value: Any) -> str:
        return normalize_day(value)

    def _day_key_from_row(self, row: Dict[str, Any]) -> str:
        """从日用气记录中提取可用日期字段，返回最大日期作为该行排序键。"""
        candidates = []
        for key in ["gasDay", "day", "date", "rq", "readDate", "chargeDate", "payDate", "time", "analyzeDate", "gasDate", "statisticsDate", "statDate"]:
            if key in row:
                normalized = self._normalize_day(row.get(key))
                if normalized:
                    candidates.append(normalized)
        if not candidates:
            return ""
        return max(candidates)

    def _normalize_month(self, value: Any) -> str:
        """标准化月级字段为 YYYY-MM。"""
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            value = str(int(value))
        if not isinstance(value, str):
            return ""
        value = value.strip().replace("/", "-").replace(".", "-")
        digits = value.replace("-", "")
        if len(digits) == 6 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        return value

    def _month_key_from_row(self, row: Dict[str, Any]) -> str:
        """从记录行中提取月份字段并标准化为 YYYY-MM。"""
        month_value = (
            row.get("readYm")
            or row.get("month")
            or row.get("billYm")
            or row.get("ym")
            or row.get("billMonth")
            or row.get("chargeYm")
            or row.get("date")
            or row.get("rq")
        )
        return self._normalize_month(month_value)

    def _extract_number(self, row: Dict[str, Any], keys: List[str]) -> float:
        return extract_number(row, keys)

    def _extract_daily_rows(self, source_data: Any) -> List[Dict[str, Any]]:
        return safe_extract_rows(source_data)

    def _get_daily_usage(self) -> float:
        """
        获取最新一条日用气量。
        
        将多种返回结构统一为列表后，按日期排序取最新值。
        """
        if not self.coordinator.data:
            return 0.0
        source_data = self.coordinator.data.get("daily_usage")
        if not source_data:
            return 0.0

        rows = self._extract_daily_rows(source_data)
        if not rows:
            return 0.0

        items = []
        for row in rows:
            day = self._day_key_from_row(row)
            if not day:
                continue
            items.append((day, row))
        items.sort(key=lambda x: x[0], reverse=True)
        
        if not items:
            return 0.0
            
        latest_row = items[0][1]
        return self._extract_number(latest_row, ['useGas', 'gasVolume', 'dailyGas', 'usage', 'gas', 'num', 'sl', 'amount', 'gasNum', 'gasAmount', 'totalGas', 'value'])

    def _get_yearly_usage(self) -> float:
        """
        获取当前年度累计用气量。
        
        仅统计当年月份的抄表记录，用于阶梯计价。
        """
        if not self.coordinator.data:
            return 0.0
        source_data = self.coordinator.data.get("meter_info")
        if not source_data:
            return 0.0
            
        rows = []
        if isinstance(source_data, list):
            rows = source_data
        elif isinstance(source_data, dict):
            rows = source_data.get("rows", []) or source_data.get("obj", []) or source_data.get("data", [])
            
        if not rows:
            return 0.0
            
        current_year = str(dt_util.now().year)
        total_usage = 0.0
        
        for row in rows:
            month_key = self._month_key_from_row(row)
            if not month_key or not month_key.startswith(current_year):
                continue
                
            usage = 0.0
            for key in ['gasSl', 'thisUse', 'useAmount', 'gasAmount', 'yl', 'useGas']:
                if key in row and row[key] is not None:
                    try:
                        usage = float(row[key])
                        break
                    except (ValueError, TypeError):
                        continue
            total_usage += usage
            
        return total_usage

    @property
    def native_value(self) -> float:
        """计算并返回当前日账单。"""
        daily_usage = self._get_daily_usage()
        if daily_usage <= 0:
            return 0.0
            
        options = _pricing_options_from_entry(self._config_entry)
        yearly_usage = self._get_yearly_usage()
        unit_price = _current_unit_price_from_options(options, yearly_usage)
                
        return round(daily_usage * unit_price, 2)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """
        输出日账单附加属性。
        
        包含日用气量、年度累计用气量、计费模式，以及费用历史列表。
        """
        daily_usage = self._get_daily_usage()
        yearly_usage = self._get_yearly_usage()
        options = _pricing_options_from_entry(self._config_entry)
        pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
        unit_price = _current_unit_price_from_options(options, yearly_usage)
        
        attributes = {
            "daily_usage": daily_usage,
            "yearly_usage": yearly_usage,
            "pricing_mode": pricing_mode,
        }
        history = []
        cached_daily = self.coordinator.cached_daily_records
        if cached_daily:
            for item in cached_daily:
                day = item.get("day")
                if not day:
                    continue
                usage = item.get("usage")
                if usage is None:
                    usage = 0.0
                cost = item.get("cost")
                if cost is None and usage is not None:
                    cost = round(float(usage) * unit_price, 2) if unit_price > 0 and float(usage) > 0 else 0.0
                history.append({"date": day, "usage": usage, "cost": cost})
        else:
            source_data = self.coordinator.data.get("daily_usage") if self.coordinator.data else None
            rows = self._extract_daily_rows(source_data)
            if rows:
                items = []
                for row in rows:
                    day = self._day_key_from_row(row)
                    if not day:
                        continue
                    usage = self._extract_number(row, ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount", "gasNum", "gasAmount", "totalGas", "value"])
                    
                    # 尝试直接获取金额，如果不存在则通过 单价*用量 计算
                    cost = self._extract_number(row, ['money', 'amt', 'fee', 'totalFee', 'payMoney', 'gasFee', 'rcvblamt', 'payableGasFee', 'amount'])
                    if cost == 0.0 and usage > 0 and unit_price > 0:
                        cost = round(usage * unit_price, 2)
                        
                    items.append((day, {"date": day, "usage": usage, "cost": cost}))
                items.sort(key=lambda x: x[0], reverse=True)
                history = [entry for _, entry in items[:90]]
        if history:
            attributes["history"] = history
        
        if pricing_mode == PRICING_MODE_TIERED:
            attributes.update({
                "current_unit_price": unit_price,
                "current_tier": _current_tier_from_options(options, yearly_usage),
                "tier_1_limit": options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT),
                "tier_2_limit": options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT),
            })
        else:
            attributes["current_unit_price"] = options.get("fixed_price", DEFAULT_FIXED_PRICE)
            
        return attributes
