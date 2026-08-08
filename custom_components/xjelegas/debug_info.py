"""
新疆电力燃气集成调试脚本。

本脚本用于在脱离 Home Assistant 环境下本地调试接口与数据解析逻辑：
- 支持模式：--type gas（燃气）、--type ele（电力）
- 功能：拉取 API 数据、模拟传感器状态与属性计算、输出 JSON 便于排查
- 适用场景：排查账号登录失败、账单/用气量解析异常、阶梯计费计算错误

运行方式（任选其一）：
  cd custom_components/xjelegas && python debug_info.py --type gas
  python -m xjelegas.debug_info --type ele

依赖：需在集成目录下运行，或确保 gas/ele/const 等模块可导入。
电力模式需 PIL (Pillow) 用于滑块验证码识别。
"""

# ==================== 标准库导入 ====================
import argparse     # 命令行参数解析
import getpass      # 安全输入密码（隐藏输入）
import json         # JSON 序列化输出
import logging      # 日志配置
import os           # 路径与环境变量
import sys          # 模块路径与系统相关
import asyncio      # 异步事件循环（电力模式）
from unittest.mock import MagicMock, patch  # 模拟 HA 依赖
from datetime import datetime  # 日期时间处理
from typing import Any, Dict, List  # 类型注解

# ==================== 路径与模块导入配置 ====================
# 确保从集成目录或任意路径运行时可正确导入 gas/ele/const 等模块
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

# 尝试导入 Home Assistant 的日期工具，若未安装则使用本地 datetime 替代
try:
    from homeassistant.util import dt as dt_util
except ImportError:
    class _DateUtil:
        """本地日期工具类，模拟 HA 的 dt_util.now()"""
        @staticmethod
        def now():
            return datetime.now()
    dt_util = _DateUtil()

# ==================== Home Assistant 依赖模拟 ====================
# 在无 HA 环境下运行，需模拟 homeassistant 及其子模块，避免导入集成模块时报错
try:
    import homeassistant
except ImportError:
    # 注册 homeassistant 主包及其子模块的 Mock，使后续 import 不报错
    sys.modules["homeassistant"] = MagicMock()
    sys.modules["homeassistant"].__path__ = []  # 标记为包，支持子模块导入
    sys.modules["homeassistant.core"] = MagicMock()
    sys.modules["homeassistant.helpers"] = MagicMock()
    sys.modules["homeassistant.helpers.storage"] = MagicMock()
    sys.modules["homeassistant.helpers.json"] = MagicMock()

    # 模拟 homeassistant.util 及其子模块，供 storage.py 等模块导入
    util_mock = MagicMock()
    util_mock.__path__ = []
    sys.modules["homeassistant.util"] = util_mock
    sys.modules["homeassistant.util.json"] = MagicMock()
    # gas.api 的 get_daily_usage 需要真实日期构建请求 payload，必须返回真实 datetime
    class _MockDt:
        """模拟 HA 的 dt 模块，返回真实当前时间"""
        @staticmethod
        def now():
            return datetime.now()
        @staticmethod
        def utcnow():
            return datetime.utcnow()
    mock_dt = _MockDt()
    sys.modules["homeassistant.util.dt"] = mock_dt
    util_mock.dt = mock_dt

    sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()
    sys.modules["homeassistant.helpers.aiohttp_client"] = MagicMock()
    sys.modules["homeassistant.components"] = MagicMock()
    sys.modules["homeassistant.components.persistent_notification"] = MagicMock()

# ==================== 可选依赖：PIL（电力模式滑块验证码识别） ====================
# 电力模式登录国网时需 PIL 识别滑块验证码，缺失时登录可能失败
try:
    import PIL
    from PIL import Image
except ImportError:
    pass  # 无 PIL 时静默忽略，电力模式将无法识别验证码

# ==================== 集成模块导入 ====================
from gas.api import XjGasAPI  # 燃气 API 客户端
# ele.api 采用懒加载，在 debug_electricity 内导入，以便 Mock 生效且避免非电力模式下的导入失败

from const import (
    DEFAULT_FIXED_PRICE,
    DEFAULT_PRICING_MODE,
    DEFAULT_TIER_1_LIMIT,
    DEFAULT_TIER_1_PRICE,
    DEFAULT_TIER_2_LIMIT,
    DEFAULT_TIER_2_PRICE,
    DEFAULT_TIER_3_PRICE,
    PRICING_MODE_FIXED,
    PRICING_MODE_TIERED,
)


# ==================== 数据提取与标准化工具函数 ====================
# 委托给 utils 模块，消除跨模块重复代码
from utils import (
    extract_balance as _extract_balance,
    extract_arrearage as _extract_arrearage,
    balance_state_from_arrearage as _balance_state_from_arrearage,
    safe_extract_rows as _extract_daily_rows,
    normalize_day as _normalize_day,
    normalize_month as _normalize_month,
    extract_number as _extract_number,
)

_extract_month_rows = _extract_daily_rows  # 与日数据提取逻辑相同，共用 safe_extract_rows


def _allowed_months(rows: List[Dict[str, Any]]) -> set:
    """计算近 12/13 个月可用月份集合。

    若当月已有数据则包含当月并往前取 13 个月；否则从上月起取 12 个月。
    与 gas/sensor.py 的 _allowed_months 逻辑保持一致，保证调试输出与实体一致。

    Returns:
        set: 允许的月份字符串集合，格式为 "YYYY-MM"
    """
    today = datetime.now().date()
    current_month = f"{today.year:04d}-{today.month:02d}"
    has_current = any(_month_key_from_row(row) == current_month for row in rows)
    allowed = set()
    start_offset = 0 if has_current else 1  # 无当月数据时从上月开始
    count = 13 if has_current else 12
    for offset in range(start_offset, start_offset + count):
        month_index = today.year * 12 + (today.month - 1) - offset
        year = month_index // 12
        month = month_index % 12 + 1
        allowed.add(f"{year:04d}-{month:02d}")
    return allowed


def _month_key_from_row(row: Dict[str, Any]) -> str:
    """从记录行中提取月份字段并标准化为 YYYY-MM。

    兼容 readYm、month、billYm、ym、chargeYm 等多种字段名。
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
    return _normalize_month(month_value)


def _filter_month_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛选近 12/13 个月的月度记录并倒序排序。

    仅保留 _allowed_months 内的数据，用于取“最近一条”账单/抄表记录。
    """
    allowed = _allowed_months(rows)
    filtered = []
    for row in rows:
        month_key = _month_key_from_row(row)
        if month_key and month_key in allowed:
            filtered.append(row)
    filtered.sort(key=lambda x: _month_key_from_row(x) or "", reverse=True)
    return filtered


def _filter_daily_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """筛选近 30 条日用气记录并按日期倒序排序。

    与 gas/sensor 的日用气历史展示逻辑一致。
    """
    items = []
    for row in rows:
        day = _day_key_from_row(row)
        if not day:
            continue
        items.append((day, row))
    items.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in items[:30]]


def _day_key_from_row(row: Dict[str, Any]) -> str:
    """从记录行中提取日期字段作为排序键。

    尝试 gasDay、day、date、rq、readDate 等多种字段，取最大日期值，
    以应对同一行存在多个日期字段的情况。
    """
    candidates = []
    for key in ["gasDay", "day", "date", "rq", "readDate", "chargeDate", "payDate", "time", "analyzeDate", "gasDate", "statisticsDate", "statDate"]:
        if key in row:
            normalized = _normalize_day(row.get(key))
            if normalized:
                candidates.append(normalized)
    if not candidates:
        return ""
    return max(candidates)


def _get_target_data(data: Dict[str, Any], data_source: str) -> Any:
    """根据数据源键名提取对应的原始数据块。

    支持 update_time、arrearage、fee_record、meter_info、payment_record、
    daily_usage 等，并按实体侧规则解析 rows/data/obj 结构。

    Args:
        data: 原始数据字典
        data_source: 数据源键名（如 "fee_record"、"meter_info" 等）

    Returns:
        提取后的数据块，可能是列表、字典或标量
    """
    if not data:
        return None
    if data_source == "update_time":
        return data.get("update_time")
    source_data = data.get(data_source)
    if not source_data:
        return None
    if data_source == "arrearage":
        target_data = source_data
        if "obj" in source_data and isinstance(source_data["obj"], dict):
            target_data = source_data["obj"]
        return target_data
    if data_source in ["fee_record", "meter_info", "payment_record"]:
        rows = _extract_month_rows(source_data)
        return rows if rows else []
    if data_source == "daily_usage":
        rows = _extract_daily_rows(source_data)
        return rows if rows else []
    return source_data


def _native_value_for(data: Dict[str, Any], data_source: str, data_key: str):
    """模拟传感器状态值（state）计算逻辑。

    根据 data_source 与 data_key 选择对应的统计/提取规则，
    输出与 gas/sensor 实体一致的 state 值。

    Args:
        data: 原始数据字典
        data_source: 数据源键名
        data_key: 数据键名（如 "balance"、"last_fee" 等）

    Returns:
        计算后的状态值，可能是 float、str 或 None
    """
    target_data = _get_target_data(data, data_source)
    if data_source == "update_time":
        return target_data
    # 最新数据时间：从日用气记录中取最大日期
    if data_source == "latest_data_time":
        rows = _extract_daily_rows(data.get("daily_usage"))
        latest_day = ""
        for row in rows:
            day = _day_key_from_row(row)
            if day and day > latest_day:
                latest_day = day
        return latest_day or None
    # 欠费已合并到账户余额，不再单独输出欠费实体；余额由 gas_info 类型处理
    if data_source == "arrearage":
        # 保留兼容：若有其他调用方传入 balance/amt 等，仍可解析
        arrearage_raw = data.get("arrearage")
        if not arrearage_raw:
            return 0
        if data_key == "balance":
            return _balance_state_from_arrearage(arrearage_raw)
        for key in ["balance", "money", "surplus", "canUse", "accBalance", "actualBalance"]:
            if key in target_data and target_data[key] is not None:
                try:
                    return float(target_data[key])
                except (ValueError, TypeError):
                    continue
        payload_value = target_data.get(data_key, 0)
        try:
            return float(payload_value)
        except (ValueError, TypeError):
            return 0
    # 账单/抄表/日用气：按数据源与 key 选择统计方式
    if data_source in ["fee_record", "meter_info", "payment_record", "daily_usage"]:
        if not target_data or not isinstance(target_data, list) or len(target_data) == 0:
            return 0
        rows = target_data
        if data_source == "meter_info" and data_key == "year_gas_num":
            return _sum_yearly_usage(rows)
        if data_source == "fee_record" and data_key == "annual_fee":
            return _sum_yearly_fee(rows, years=1)
        # 按数据源选择过滤方式
        if data_source == "daily_usage":
            rows = _filter_daily_rows(target_data)
        elif data_source == "fee_record":
            rows = _filter_month_rows(target_data)
        elif data_source == "meter_info" and data_key == "last_usage":
            rows = _filter_month_rows(target_data)
        if not rows:
            return 0
        latest = rows[0]
        # 月度账单：取金额字段
        if data_source == "fee_record":
            for key in ["paidInGasFee", "payableGasFee", "rcvblamt", "money", "totalFee", "payMoney", "fee", "billAmount", "rcvedamt", "amt", "gasFee", "pay"]:
                if key in latest and latest[key] is not None:
                    try:
                        return float(latest[key])
                    except (ValueError, TypeError):
                        continue
            return 0
        # 交费记录：取金额字段
        if data_source == "payment_record":
            for key in ["paymentAmount", "money", "payMoney", "amount", "totalFee", "amt", "pay", "rcvblamt"]:
                if key in latest and latest[key] is not None:
                    try:
                        return float(latest[key])
                    except (ValueError, TypeError):
                        continue
            return 0
        # 抄表记录：取用气量或表读数
        if data_source == "meter_info":
            if data_key == "last_usage":
                for key in ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas"]:
                    if key in latest and latest[key] is not None:
                        try:
                            return float(latest[key])
                        except (ValueError, TypeError):
                            continue
            elif data_key == "current_reading":
                # 当前抄表数（表盘读数）
                for key in ["thisIndex", "index", "readNumber", "bd", "lastIndex", "thisNum"]:
                    if key in latest and latest[key] is not None:
                        try:
                            return float(latest[key])
                        except (ValueError, TypeError):
                            continue
        # 日用气：遍历记录取最新有效值（非零）
        if data_source == "daily_usage":
            for row in rows:
                for key in ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount", "gasNum", "gasAmount", "totalGas", "value"]:
                    if key in row and row[key] is not None:
                        try:
                            val = float(row[key])
                            if val > 0:
                                return val
                        except (ValueError, TypeError):
                            continue
            return 0
    return None


# ==================== 计费与阶梯相关计算 ====================

def _current_unit_price(data: Dict[str, Any], options: Dict[str, Any]) -> float:
    """根据配置与年度用气量计算当前单价（元/立方米）。

    固定计价：直接返回 fixed_price；阶梯计价：按年度用气量落在第 1/2/3 档返回对应单价。

    Returns:
        float: 当前单价，单位：元/立方米
    """
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    if pricing_mode == PRICING_MODE_FIXED:
        return float(options.get("fixed_price", DEFAULT_FIXED_PRICE))
    yearly_usage = _daily_cost_yearly_usage(data)
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


def _current_tier(data: Dict[str, Any], options: Dict[str, Any]):
    """根据配置与年度用气量计算当前计价阶梯。

    固定计价返回 None；阶梯计价返回 1、2 或 3。

    Returns:
        int | None: 阶梯档位（1/2/3），固定计价时为 None
    """
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    if pricing_mode == PRICING_MODE_FIXED:
        return None
    yearly_usage = _daily_cost_yearly_usage(data)
    tier_1_limit = options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT)
    tier_2_limit = options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT)
    if yearly_usage <= tier_1_limit:
        return 1
    if yearly_usage <= tier_2_limit:
        return 2
    return 3


# ==================== 实体属性构建 ====================

def _extra_attributes_for(data: Dict[str, Any], data_source: str, data_key: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """模拟传感器 extra_state_attributes 输出逻辑。

    复现实体属性结构：最新记录、历史记录、日期字段等，供前端卡片使用。

    Args:
        data: 原始数据字典
        data_source: 数据源键名
        data_key: 数据键名
        options: 计费配置（pricing_mode、fixed_price、阶梯参数等）

    Returns:
        属性字典，与 Home Assistant 实体的 extra_state_attributes 格式一致
    """
    if not data:
        return {}
    if data_source == "update_time":
        return {}
    source_data = data.get(data_source)
    if not source_data:
        return {}
    if data_source == "arrearage":
        if isinstance(source_data, dict) and isinstance(source_data.get("obj"), dict):
            return source_data["obj"]
        if isinstance(source_data, dict):
            return source_data
        return {}
    if not isinstance(source_data, (dict, list)):
        return {}
    if isinstance(source_data, list):
        rows = source_data
        obj = None
    elif isinstance(source_data, dict):
        rows = source_data.get("rows", [])
        obj = source_data.get("obj")
    else:
        rows = []
        obj = None
    if not rows:
        if isinstance(obj, list):
            rows = obj
        elif isinstance(obj, dict):
            rows = obj.get("result", []) or obj.get("rows", [])
    if not rows and not isinstance(source_data, list):
        if isinstance(source_data, dict):
            rows = source_data.get("data", [])
            if isinstance(rows, dict):
                rows = rows.get("rows", [])
    if rows and isinstance(rows, list):
        # 年度用气量：当年汇总
        if data_source == "meter_info" and data_key == "year_gas_num":
            current_year = str(datetime.now().year)
            filtered_rows = [row for row in rows if (_month_key_from_row(row) or "").startswith(current_year)]
            yearly_total = _sum_yearly_usage(rows)
            return {"year": current_year, "total": yearly_total, "year_gas_num": yearly_total}
        # 年度账单：当年汇总 + 近两年记录
        if data_source == "fee_record" and data_key == "annual_fee":
            current_year = str(datetime.now().year)
            yearly_total = _sum_yearly_fee(rows, years=1)

            annual_records = []
            this_year = datetime.now().year
            target_years = [str(this_year), str(this_year - 1)]
            year_sums = {}
            for row in rows:
                month_key = _month_key_from_row(row)
                if not month_key:
                    continue
                year_str = month_key.split("-")[0]
                if year_str in target_years:
                    amount = _extract_number(row, ["paidInGasFee", "payableGasFee", "rcvblamt", "money", "totalFee", "payMoney", "fee", "billAmount", "rcvedamt", "amt", "gasFee", "pay", "amount"])
                    year_sums[year_str] = year_sums.get(year_str, 0.0) + amount
            for year_str in sorted(target_years, reverse=True):
                if year_str in year_sums:
                    year_amount = year_sums[year_str]
                    annual_records.append({"year": year_str, "total": year_amount, "annual_fee": year_amount})

            result = {"year": current_year, "total": yearly_total, "annual_fee": yearly_total}
            if annual_records:
                result["annual_records"] = annual_records
            
            records = _filter_month_rows(rows)
            if records:
                result["records"] = records
            return result
        # 月度账单/抄表/交费：取最新一条记录的属性
        if data_source in ["fee_record", "meter_info", "payment_record"] and len(rows) > 0:
            filtered_rows = rows
            if data_source == "fee_record":
                filtered_rows = _filter_month_rows(rows)
            elif data_source == "meter_info" and data_key == "last_usage":
                filtered_rows = _filter_month_rows(rows)
            if not filtered_rows:
                return {}
            attributes = filtered_rows[0].copy()
            latest = filtered_rows[0]
            for key in ["paymentDate", "readDate", "chargeDate", "payDate", "date", "time", "day", "rq"]:
                if key in latest:
                    attributes["date"] = latest[key]
                    break
            return attributes
        # 日用气：含 history（每条带 cost），按当前单价计算
        if data_source == "daily_usage" and len(rows) > 0:
            filtered_rows = _filter_daily_rows(rows)
            if not filtered_rows:
                return {}
            unit_price = _current_unit_price(data, options)
            latest = filtered_rows[0]
            history = []
            for row in filtered_rows:
                usage = _extract_number(row, ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount", "gasNum", "gasAmount", "totalGas", "value"])
                cost = round(usage * unit_price, 2) if unit_price > 0 and usage > 0 else 0.0
                row_with_cost = row.copy()
                row_with_cost["cost"] = cost
                history.append(row_with_cost)
            attributes = history[0].copy()
            attributes["history"] = history
            for key in ["gasDay", "readDate", "chargeDate", "payDate", "date", "time", "day", "rq"]:
                if key in latest:
                    attributes["date"] = latest[key]
                    break
            return attributes
        return {}
    return {}


# ==================== 日账单相关 ====================

def _daily_cost_state(data: Dict[str, Any], options: Dict[str, Any]) -> float:
    """计算日账单实体的状态值（最新日用气量 × 当前单价）。

    先取最新日用气记录，再按固定/阶梯计费模式计算单价，输出金额（元）。
    """
    rows = _extract_daily_rows(data.get("daily_usage"))
    if not rows:
        return 0.0
    items = []
    for row in rows:
        day = _day_key_from_row(row)
        if not day:
            continue
        items.append((day, row))
    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        return 0.0
    latest_row = items[0][1]
    daily_usage = 0.0
    for key in ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount", "gasNum", "gasAmount", "totalGas", "value"]:
        if key in latest_row and latest_row[key] is not None:
            try:
                daily_usage = float(latest_row[key])
                break
            except (ValueError, TypeError):
                continue
    if daily_usage <= 0:
        return 0.0
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    unit_price = 0.0
    if pricing_mode == PRICING_MODE_FIXED:
        unit_price = options.get("fixed_price", DEFAULT_FIXED_PRICE)
    else:
        yearly_usage = _daily_cost_yearly_usage(data)
        tier_1_limit = options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT)
        tier_2_limit = options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT)
        tier_1_price = options.get("tier_1_price", DEFAULT_TIER_1_PRICE)
        tier_2_price = options.get("tier_2_price", DEFAULT_TIER_2_PRICE)
        tier_3_price = options.get("tier_3_price", DEFAULT_TIER_3_PRICE)
        current_total = yearly_usage
        if current_total <= tier_1_limit:
            unit_price = tier_1_price
        elif current_total <= tier_2_limit:
            unit_price = tier_2_price
        else:
            unit_price = tier_3_price
    return round(daily_usage * unit_price, 2)


def _daily_cost_yearly_usage(data: Dict[str, Any]) -> float:
    """汇总当前年度用气量（立方米），用于阶梯计价判定。

    从 meter_info 抄表记录中筛选当年月份，累加各月用气量。
    """
    rows = _extract_month_rows(data.get("meter_info"))
    if not rows:
        return 0.0
    current_year = str(datetime.now().year)
    total_usage = 0.0
    for row in rows:
        month_key = _month_key_from_row(row)
        if not month_key or not month_key.startswith(current_year):
            continue
        usage = 0.0
        for key in ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas"]:
            if key in row and row[key] is not None:
                try:
                    usage = float(row[key])
                    break
                except (ValueError, TypeError):
                    continue
        total_usage += usage
    return total_usage


def _sum_yearly_usage(rows: List[Dict[str, Any]]) -> float:
    """统计当前年度用气量（立方米）。

    按月份字段过滤当年记录并累加，用于 year_gas_num 实体或阶梯计算。
    """
    total = 0.0
    current_year = str(datetime.now().year)
    for row in rows:
        month_key = _month_key_from_row(row)
        if not month_key or not month_key.startswith(current_year):
            continue
        usage = _extract_number(row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas", "usage", "gas", "num", "sl", "amount"])
        total += usage
    return total


def _sum_yearly_fee(rows: List[Dict[str, Any]], years: int = 1) -> float:
    """统计近 N 年账单金额（元）。

    从月度账单记录中按年份过滤并累加，用于 annual_fee 等实体。

    Args:
        rows: 月度账单记录列表
        years: 统计年数，默认 1（仅当年）

    Returns:
        float: 累计金额
    """
    total = 0.0
    current_year = datetime.now().year
    allowed_years = {str(current_year - offset) for offset in range(max(1, years))}
    for row in rows:
        month_key = _month_key_from_row(row)
        if not month_key or not any(month_key.startswith(year_str) for year_str in allowed_years):
            continue
        amount = _extract_number(row, ["paidInGasFee", "payableGasFee", "rcvblamt", "money", "totalFee", "payMoney", "fee", "billAmount", "rcvedamt", "amt", "gasFee", "pay", "amount"])
        total += amount
    return total


def _daily_cost_attributes(data: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
    """构建日账单实体的 extra_state_attributes。

    包含 daily_usage、yearly_usage、history（含 cost）、
    以及 current_unit_price、current_tier 等计费信息。
    """
    rows = _extract_daily_rows(data.get("daily_usage"))
    daily_usage = 0.0
    history = []
    if rows:
        filtered_rows = _filter_daily_rows(rows)
        if filtered_rows:
            unit_price = _current_unit_price(data, options)
            for row in filtered_rows:
                day = _day_key_from_row(row)
                if not day:
                    continue
                usage = _extract_number(row, ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount", "gasNum", "gasAmount", "totalGas", "value"])
                cost = round(usage * unit_price, 2) if unit_price > 0 and usage > 0 else 0.0
                history.append({"date": day, "usage": usage, "cost": cost})
        items = []
        for row in rows:
            day = _day_key_from_row(row)
            if not day:
                continue
            items.append((day, row))
        items.sort(key=lambda x: x[0], reverse=True)
        if items:
            latest_row = items[0][1]
            for key in ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount", "gasNum", "gasAmount", "totalGas", "value"]:
                if key in latest_row and latest_row[key] is not None:
                    try:
                        daily_usage = float(latest_row[key])
                        break
                    except (ValueError, TypeError):
                        continue
    yearly_usage = _daily_cost_yearly_usage(data)
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    attributes = {
        "daily_usage": daily_usage,
        "yearly_usage": yearly_usage,
        "pricing_mode": pricing_mode,
    }
    if history:
        attributes["history"] = history
    if pricing_mode == PRICING_MODE_TIERED:
        tier_1_limit = options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT)
        tier_2_limit = options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT)
        tier_1_price = options.get("tier_1_price", DEFAULT_TIER_1_PRICE)
        tier_2_price = options.get("tier_2_price", DEFAULT_TIER_2_PRICE)
        tier_3_price = options.get("tier_3_price", DEFAULT_TIER_3_PRICE)
        current_total = yearly_usage
        if current_total <= tier_1_limit:
            current_price = tier_1_price
            tier = 1
        elif current_total <= tier_2_limit:
            current_price = tier_2_price
            tier = 2
        else:
            current_price = tier_3_price
            tier = 3
        attributes.update(
            {
                "current_unit_price": current_price,
                "current_tier": tier,
                "tier_1_limit": tier_1_limit,
                "tier_2_limit": tier_2_limit,
            }
        )
    else:
        fixed_price = options.get("fixed_price", DEFAULT_FIXED_PRICE)
        attributes["current_unit_price"] = fixed_price
    return attributes


def _build_billing_standard_for_card(
    options: Dict[str, Any],
    yearly_usage: float,
    yearly_cost: float = 0.0,
) -> Dict[str, Any]:
    """构建燃气计费标准对象，供前端 xjelegas-card 解析气单价。

    与 gas/sensor.py 的计费标准逻辑一致，包含固定/阶梯两种模式的字段。

    Args:
        options: 计费配置（pricing_mode、阶梯参数等）
        yearly_usage: 年度累计用气量（立方米）
        yearly_cost: 年度累计气费（元），可选

    Returns:
        计费标准字典，含「计费标准」「平均单价」「年阶梯」等键
    """
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    current_year = datetime.now().year
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
    current_tier = 1 if yearly_usage_val <= tier_1_limit else (2 if yearly_usage_val <= tier_2_limit else 3)
    tier_str = f"第{current_tier}档"
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


# ==================== 燃气综合信息与实体输出 ====================

def build_gas_info(arrearage: Any, daily_usage: Any, fee_record: Any, meter_info: Any, payment_record: Any, update_time: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """构建燃气综合信息实体（gas_info）的属性结构。

    汇总 daylist、monthlist、yearlist、balance、计费参数、history_charges 等，
    与 gas/sensor.py 的 XjGasGeneralSensor.extra_state_attributes 逻辑一致。

    Args:
        arrearage: 欠费/余额接口响应
        daily_usage: 日用气接口响应
        fee_record: 月度账单接口响应
        meter_info: 抄表记录接口响应
        payment_record: 交费记录接口响应
        update_time: 最近刷新时间
        options: 计费配置

    Returns:
        燃气综合信息属性字典，供 gas_info 实体使用
    """
    # 构建日用气列表（近 30 条）
    rows = _extract_daily_rows(daily_usage)
    daylist = []
    for row in rows:
        usage = _extract_number(row, ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "gasNum", "gasAmount", "totalGas", "value"])
        amount = _extract_number(row, ["money", "amt", "fee", "totalFee", "payMoney", "gasFee", "rcvblamt", "payableGasFee", "amount"])
        day_value = row.get("gasDay") or row.get("day") or row.get("date") or row.get("rq") or row.get("analyzeDate") or row.get("gasDate") or row.get("statisticsDate") or row.get("statDate")
        normalized_day = _normalize_day(day_value)
        daylist.append(
            {
                "readingTime": normalized_day,
                "cycleTotalVolume": usage,
                "cycleTotalValues": amount,
                "usage": usage,
                "amount": amount,
            }
        )
    daylist = [item for item in daylist if item.get("readingTime")]
    daylist.sort(key=lambda x: x.get("readingTime") or "", reverse=True)
    daylist = daylist[:30]

    # 构建月度列表（合并抄表与账单，monthEleNum 来自抄表，monthEleCost 来自账单）
    fee_rows = _extract_month_rows(fee_record)
    meter_rows = _extract_month_rows(meter_info)
    today = datetime.now().date()
    allowed_months = _allowed_months(meter_rows + fee_rows)
    month_map: Dict[str, Dict[str, Any]] = {}  # 月份 -> {month, monthEleNum, monthEleCost, usage, amount}
    for row in meter_rows:
        month_key = _month_key_from_row(row)
        if not month_key or month_key not in allowed_months:
            continue
        usage = _extract_number(row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas", "usage", "gas", "num", "sl"])
        entry = month_map.get(month_key, {"month": month_key, "monthEleNum": 0, "monthEleCost": 0, "usage": 0, "amount": 0})
        if usage:
            entry["monthEleNum"] = usage
            entry["usage"] = usage
        month_map[month_key] = entry
    for row in fee_rows:
        month_key = _month_key_from_row(row)
        if not month_key or month_key not in allowed_months:
            continue
        amount = _extract_number(row, ["paidInGasFee", "payableGasFee", "rcvblamt", "money", "totalFee", "payMoney", "fee", "billAmount", "rcvedamt", "amt", "gasFee", "pay", "amount"])
        entry = month_map.get(month_key, {"month": month_key, "monthEleNum": 0, "monthEleCost": 0, "usage": 0, "amount": 0})
        if amount:
            entry["monthEleCost"] = amount
            entry["amount"] = amount
        month_map[month_key] = entry
    monthlist = list(month_map.values())
    monthlist.sort(key=lambda x: x.get("month") or "", reverse=True)
    current_month = f"{today.year:04d}-{today.month:02d}"
    max_months = 13 if current_month in allowed_months else 12
    monthlist = monthlist[:max_months]

    # 构建年度列表
    yearly_usage = _daily_cost_yearly_usage({"meter_info": meter_info})
    current_year_str = str(datetime.now().year)
    year_data_map: Dict[str, Dict[str, float]] = {}
    for row in meter_rows:
        month_key = _month_key_from_row(row)
        if not month_key or month_key not in allowed_months:
            continue
        usage = _extract_number(row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas", "usage", "gas", "num", "sl", "amount"])
        if usage:
            year = month_key.split("-")[0]
            if year not in year_data_map:
                year_data_map[year] = {"usage": 0.0, "amount": 0.0}
            year_data_map[year]["usage"] += usage
    for row in fee_rows:
        month_key = _month_key_from_row(row)
        if not month_key or month_key not in allowed_months:
            continue
        amount = _extract_number(row, ["paidInGasFee", "payableGasFee", "rcvblamt", "money", "totalFee", "payMoney", "fee", "billAmount", "rcvedamt", "amt", "gasFee", "pay", "amount"])
        if amount:
            year = month_key.split("-")[0]
            if year not in year_data_map:
                year_data_map[year] = {"usage": 0.0, "amount": 0.0}
            year_data_map[year]["amount"] += amount
    yearlist = [{"year": y, "usage": d["usage"], "amount": d["amount"]} for y, d in sorted(year_data_map.items(), reverse=True)]
    yearly_cost = year_data_map.get(current_year_str, {}).get("amount", 0.0)

    # 若当月无月度记录但有日用气，则从日用气汇总当月（补全当月数据）
    if daylist and not any(m.get("month") == current_month for m in monthlist):
        month_gas_num_sum = 0.0
        month_gas_cost_sum = 0.0
        for item in daylist:
            rt = item.get("readingTime") or ""
            if rt.startswith(current_month):
                month_gas_num_sum += float(item.get("cycleTotalVolume") or item.get("usage") or 0.0)
                month_gas_cost_sum += float(item.get("cycleTotalValues") or item.get("amount") or 0.0)
        if month_gas_num_sum > 0 or month_gas_cost_sum > 0:
            monthlist.insert(0, {
                "month": current_month,
                "monthEleNum": round(month_gas_num_sum, 2),
                "monthEleCost": round(month_gas_cost_sum, 2),
                "usage": round(month_gas_num_sum, 2),
                "amount": round(month_gas_cost_sum, 2),
            })
            monthlist.sort(key=lambda x: x.get("month") or "", reverse=True)

    # 交费历史
    pay_rows = _extract_month_rows(payment_record)
    history_charges = []
    for row in pay_rows:
        p_time = row.get("payTime") or row.get("payDate") or row.get("createTime") or row.get("date", "")
        p_amount = _extract_number(row, ["paymentAmount", "payMoney", "money", "amount", "totalFee", "amt", "pay", "rcvblamt"])
        p_method = row.get("payMethodName") or row.get("payMethod") or row.get("payChannel") or row.get("channel", "")
        if p_time or p_amount:
            history_charges.append({"time": p_time, "amount": p_amount, "method": p_method})

    attributes = {
        "daylist": daylist,
        "monthlist": monthlist,
        "yearlist": yearlist,
        "balance": _balance_state_from_arrearage(arrearage),
        "utility_type": "gas",
        "year_gas_num": yearly_usage,
        "history_charges": history_charges,
    }
    if update_time:
        attributes["syn"] = update_time
    if daylist:
        attributes["latest_data"] = daylist[0].get("readingTime")
    pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
    attributes["pricing_mode"] = pricing_mode
    if pricing_mode == PRICING_MODE_FIXED:
        attributes["fixed_price"] = options.get("fixed_price", DEFAULT_FIXED_PRICE)
        attributes["billing_standard_name"] = "平均气价计费"
    else:
        attributes["tier_1_limit"] = options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT)
        attributes["tier_1_price"] = options.get("tier_1_price", DEFAULT_TIER_1_PRICE)
        attributes["tier_2_limit"] = options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT)
        attributes["tier_2_price"] = options.get("tier_2_price", DEFAULT_TIER_2_PRICE)
        attributes["tier_3_price"] = options.get("tier_3_price", DEFAULT_TIER_3_PRICE)
        attributes["billing_standard_name"] = "年阶梯计费"
    attributes["计费标准"] = _build_billing_standard_for_card(options, yearly_usage, yearly_cost)
    return attributes


def _build_entity_output(selected: Dict[str, Any], data: Dict[str, Any], options: Dict[str, Any], account_tail: str) -> Dict[str, Any]:
    """根据实体配置构建单个燃气实体的调试输出。

    输出包含 entity_id、state、attributes、raw，与 Home Assistant 实体结构对应，
    便于对比调试脚本输出与集成实际实体数据。

    Args:
        selected: 实体配置（key、name、type、data_source、data_key）
        data: 原始 API 数据字典
        options: 计费配置
        account_tail: 账号后四位，用于生成 entity_id

    Returns:
        包含 entity_id、name、state、attributes、raw 的字典
    """
    entity_id = f"sensor.gas_{account_tail}_{selected['key']}"
    if selected.get("type") == "gas_info":
        state = _balance_state_from_arrearage(data.get("arrearage"))
        attributes = build_gas_info(
            data.get("arrearage"),
            data.get("daily_usage"),
            data.get("fee_record"),
            data.get("meter_info"),
            data.get("payment_record"),
            data.get("update_time"),
            options,
        )
        raw = {
            "arrearage": data.get("arrearage"),
            "fee_record": data.get("fee_record"),
            "meter_info": data.get("meter_info"),
            "payment_record": data.get("payment_record"),
            "daily_usage": data.get("daily_usage"),
            "update_time": data.get("update_time"),
            "latest_data_time": data.get("latest_data_time"),
        }
    elif selected.get("type") == "current_unit_price":
        state = _current_unit_price(data, options)
        yearly_usage = _daily_cost_yearly_usage(data)
        pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
        attributes = {"pricing_mode": pricing_mode, "yearly_usage": yearly_usage}
        if pricing_mode == PRICING_MODE_FIXED:
            attributes["fixed_price"] = options.get("fixed_price", DEFAULT_FIXED_PRICE)
        else:
            attributes.update(
                {
                    "tier_1_limit": options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT),
                    "tier_2_limit": options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT),
                    "tier_1_price": options.get("tier_1_price", DEFAULT_TIER_1_PRICE),
                    "tier_2_price": options.get("tier_2_price", DEFAULT_TIER_2_PRICE),
                    "tier_3_price": options.get("tier_3_price", DEFAULT_TIER_3_PRICE),
                }
            )
        raw = {}
    elif selected.get("type") == "current_tier":
        state = _current_tier(data, options)
        yearly_usage = _daily_cost_yearly_usage(data)
        pricing_mode = options.get("pricing_mode", DEFAULT_PRICING_MODE)
        attributes = {"pricing_mode": pricing_mode, "yearly_usage": yearly_usage}
        if pricing_mode == PRICING_MODE_FIXED:
            attributes["fixed_price"] = options.get("fixed_price", DEFAULT_FIXED_PRICE)
        else:
            attributes.update(
                {
                    "tier_1_limit": options.get("tier_1_limit", DEFAULT_TIER_1_LIMIT),
                    "tier_2_limit": options.get("tier_2_limit", DEFAULT_TIER_2_LIMIT),
                    "tier_1_price": options.get("tier_1_price", DEFAULT_TIER_1_PRICE),
                    "tier_2_price": options.get("tier_2_price", DEFAULT_TIER_2_PRICE),
                    "tier_3_price": options.get("tier_3_price", DEFAULT_TIER_3_PRICE),
                    "current_unit_price": _current_unit_price(data, options),
                }
            )
        raw = {}
    elif selected.get("type") == "daily_cost":
        state = _daily_cost_state(data, options)
        attributes = _daily_cost_attributes(data, options)
        raw = {
            "daily_usage": data.get("daily_usage"),
            "meter_info": data.get("meter_info"),
        }
    else:
        data_source = selected["data_source"]
        data_key = selected["data_key"]
        state = _native_value_for(data, data_source, data_key)
        attributes = _extra_attributes_for(data, data_source, data_key, options)
        if selected["key"] in ("current_reading", "update_time", "latest_data_time"):
            raw = {}
        else:
            raw = data.get(data_source)
    return {
        "entity_id": entity_id,
        "name": selected["name"],
        "state": state,
        "attributes": attributes,
        "raw": raw,
    }


# ==================== 电力实体输出 ====================

def _build_ele_entity_output(selected: Dict[str, Any], door_account: Dict[str, Any], fixed_price: float = 0.475, is_debug: bool = False) -> Dict[str, Any]:
    """构建电力实体调试输出，与 ele/sensor.py 逻辑一致。

    按实体 key（balance、year_ele_num、daily_ele_cost 等）分别计算 state 与 attributes，
    输出格式与燃气实体一致，便于调试对比。

    Args:
        selected: 实体类型配置（key、name）
        door_account: 户号数据（含 balance、daylist、monthlist 等）
        fixed_price: 固定电价（元/kWh），用于估算日/月电费，默认 0.475
        is_debug: 保留参数以保持接口兼容

    Returns:
        包含 entity_id、name、state、attributes、raw 的字典
    """
    key = selected["key"]
    name = selected["name"]
    cons_no = door_account.get("consNo_dst", door_account.get("consNo", ""))
    last_four = cons_no[-4:] if len(cons_no) >= 4 else cons_no
    entity_id = f"sensor.ele_{last_four}_{key}"

    # 基础属性：户号、户名、地址等
    attributes = {}
    base_attrs = ["consNo", "consName", "elecAddr", "consNo_dst", "consName_dst", "elecAddr_dst", "refresh_time"]
    for attr_key in base_attrs:
        if attr_key in door_account:
            attributes[attr_key] = door_account[attr_key]

    # 按实体类型分别处理
    if key == "recent_30_daily_ele_list":
        state = "图表"
        raw = door_account.get("recent_30_daily_ele_list", [])
        daylist = []
        for day_record in raw:
            ele_val = float(day_record.get("ele", 0))
            cost_val = day_record.get("cost")
            cost = round(float(cost_val), 2) if cost_val is not None else round(ele_val * 0.475, 2)
            daylist.append({"day": day_record.get("day"), "dayEleNum": day_record.get("ele"), "dayEleCost": cost})
        attributes["graph"] = raw
        attributes["daylist"] = daylist
    elif key == "recent_12_monthly_ele_list":
        # 最近 12 个月用电列表
        state = "图表"
        attributes["graph"] = door_account.get("recent_12_monthly_ele_list", [])
    elif key == "balance":
        # 余额实体：含 daylist、monthlist、yearlist、计费标准等，与卡片兼容
        state = door_account.get("balance")
        if state is None:
            try:
                state = float(door_account.get("account_balance", {}).get("prestoreBalance", 0))
            except (TypeError, ValueError):
                state = 0.0
        # 余额为 0 时附带原始 account_balance 便于排查（配合 --debug 可查看 API 原始返回）
        if (state is None or state == 0) and "account_balance" in door_account:
            attributes["_debug_account_balance"] = door_account["account_balance"]
        attributes["utility_type"] = "ele"
        fp = fixed_price or 0.475
        # daylist（与 ele/sensor 一致）
        raw_day = door_account.get("recent_30_daily_ele_list", [])
        daylist = []
        for day_record in raw_day:
            ele_val = float(day_record.get("ele", 0))
            cost_val = day_record.get("cost")
            cost = round(float(cost_val), 2) if cost_val is not None else round(ele_val * fp, 2)
            if cost == 0 and ele_val > 0:
                cost = round(ele_val * fp, 2)
            daylist.append({"day": day_record.get("day"), "dayEleNum": day_record.get("ele"), "dayEleCost": cost})
        attributes["daylist"] = daylist
        # monthlist（与 ele/sensor 一致，API 返回 ele/cost，卡片期望 monthEleNum/monthEleCost）
        raw_month = door_account.get("recent_12_monthly_ele_list", [])
        monthlist = [{"month": month_record.get("month"), "monthEleNum": month_record.get("ele"), "monthEleCost": month_record.get("cost")} for month_record in raw_month]
        attributes["monthlist"] = monthlist
        # yearlist（从 month_bill_list 聚合）
        year_map = {}
        for bill in door_account.get("month_bill_list", []):
            year = (bill.get("month") or "")[:4]
            if year:
                if year not in year_map:
                    year_map[year] = {"year": year, "yearEleNum": 0.0, "yearEleCost": 0.0}
                try:
                    year_map[year]["yearEleNum"] += float(bill.get("monthEleNum", 0))
                    year_map[year]["yearEleCost"] += float(bill.get("monthEleCost", 0))
                except (ValueError, TypeError):
                    pass
        yearlist = [{"year": year_record["year"], "yearEleNum": round(year_record["yearEleNum"], 2), "yearEleCost": round(year_record["yearEleCost"], 2)} for year_record in sorted(year_map.values(), key=lambda x: x["year"], reverse=True)]
        attributes["yearlist"] = yearlist
        attributes["month_bill_list"] = door_account.get("month_bill_list", [])
        attributes["daily_bill_list"] = door_account.get("daily_bill_list", [])
        # 计费标准（与 ele/sensor 一致）
        year_ele_num = door_account.get("year_ele_num") or 0
        year_ele_cost = door_account.get("year_ele_cost") or 0
        attributes["计费标准"] = {
            "当前计费模式": "固定电价",
            "计费标准": "平均单价",
            "平均单价": fp,
            "年累计用电量": year_ele_num,
            "年阶梯累计用电量": year_ele_num,
            "年累计电费": year_ele_cost,
        }
        # 汇总其他传感器值（便于卡片直接使用）
        for attr_key in ("month_ele_num", "month_ele_cost", "daily_ele_num", "daily_ele_cost", "year_ele_num", "year_ele_cost", "last_month_ele_num", "last_month_ele_cost", "daily_lasted_date", "refresh_time"):
            if attr_key in door_account:
                attributes[attr_key] = door_account[attr_key]
    elif key == "year_ele_num":
        state = door_account.get(key, 0)
        if "year_bill_list" in door_account:
            attributes["history"] = door_account["year_bill_list"]
    elif key == "daily_ele_cost":
        state = door_account.get("daily_ele_cost")
        if state is None:
            daily_ele = float(door_account.get("daily_ele_num") or 0)
            last_cost = float(door_account.get("last_month_ele_cost") or 0)
            last_num = float(door_account.get("last_month_ele_num") or 1)
            avg_price = last_cost / last_num if last_num > 0 else 0.475
            state = round(daily_ele * avg_price, 2)
        attributes["日用电量"] = door_account.get("daily_ele_num")
        attributes["日期"] = door_account.get("daily_lasted_date")
    else:
        state = door_account.get(key, 0)

    return {
        "entity_id": entity_id,
        "name": name,
        "state": state,
        "attributes": attributes,
        "raw": door_account.get(key),
    }

# ==================== 电力调试入口 ====================

async def debug_electricity(args) -> None:
    """电力模式调试逻辑：登录国网、拉取数据、按实体输出 JSON。

    流程：获取账号密码 → 登录 StateGridDataClient → 刷新数据 →
    按户号与实体类型构建输出 → 打印或写入文件。

    Args:
        args: 命令行参数（phone、password、price、all、output_file、debug 等）
    """
    try:
        import aiohttp
        from ele.api import StateGridDataClient
        # 尝试导入 AsyncMock（Python 3.8+），低版本用自定义 AsyncMock 兼容
        try:
            from unittest.mock import AsyncMock
        except ImportError:
            # Python < 3.8 无 AsyncMock，自定义异步 Mock
            class AsyncMock(MagicMock):
                async def __call__(self, *args, **kwargs):
                    return super(AsyncMock, self).__call__(*args, **kwargs)
    except ImportError as e:
        print(f"导入电力模块失败，请检查环境: {e}")
        return

    # 获取账号（命令行 > 环境变量 SG_PHONE > 交互输入）
    phone = args.phone
    if not phone:
        default_phone = os.getenv("SG_PHONE", "").strip()
        prompt = f"请输入国网账号" + (f" [{default_phone}]" if default_phone else "") + ": "
        phone = input(prompt).strip()
        if not phone and default_phone:
            phone = default_phone
            
    if not phone:
        print("账号不能为空！")
        return

    # 获取密码（支持 --password-hidden 隐藏输入）
    password = args.password
    if not password:
        default_password = os.getenv("SG_PASSWORD", "")
        prompt = f"请输入密码" + (" [检测到环境变量]" if default_password else "") + ": "
        if args.password_hidden:
            try:
                password = getpass.getpass(prompt).strip()
            except Exception:
                password = input(prompt).strip()
        else:
            password = input(prompt).strip()
            
        if not password and default_password:
            password = default_password

    if not password:
        print("密码不能为空！")
        return

    # 获取备用邮箱（RK001 流控降级）
    email = getattr(args, "email", None) or ""
    if not email:
        email = os.getenv("SG_EMAIL", "").strip()

    # 获取 LLM 配置（验证码识别）
    ark_api_key = getattr(args, "ark_api_key", None) or os.getenv("ARK_API_KEY", "").strip()
    ark_base_url = getattr(args, "ark_base_url", None) or os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").strip()
    ark_model = getattr(args, "ark_model", None) or os.getenv("ARK_MODEL", "doubao-vision-pro-32k").strip()

    if not ark_api_key:
        print("警告: 未配置 LLM API Key，验证码识别可能失败。可通过 --ark-api-key 或环境变量 ARK_API_KEY 设置。")

    print(f"\n正在登录国网账号: {phone} ...")

    # 使用 aiohttp Session，Mock HA 的 async_get_clientsession 等依赖
    async with aiohttp.ClientSession() as session:
        mock_hass = MagicMock()
        # Mock async_add_executor_job：将同步函数放入线程池执行，供滑块识别等阻塞调用使用
        async def mock_async_add_executor_job(func, *args, **kwargs):
            """将同步函数放入线程池执行，避免阻塞事件循环（如 PIL 滑块识别）。"""
            import asyncio
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, func, *args, **kwargs)

        mock_hass.async_add_executor_job = mock_async_add_executor_job

        # Patch ele.api 的 HA 依赖，使客户端在无 HA 环境下可运行
        with patch('ele.api.async_get_clientsession', return_value=session), \
             patch('ele.api.async_save_to_store', new=AsyncMock(return_value=None)), \
             patch('ele.api.persistent_notification', MagicMock()):
            from ele.const import CONF_PRICE, CONF_ARK_API_KEY, CONF_ARK_BASE_URL, CONF_ARK_MODEL, CONF_EMAIL_ACCOUNT
            from ele import captcha_solver as ele_captcha_solver

            if ark_api_key:
                ele_captcha_solver.configure_llm(ark_api_key, ark_base_url, ark_model)

            client_config = {
                CONF_PRICE: args.price,
                CONF_ARK_API_KEY: ark_api_key,
                CONF_ARK_BASE_URL: ark_base_url,
                CONF_ARK_MODEL: ark_model,
                CONF_EMAIL_ACCOUNT: email,
            }
            client = StateGridDataClient(mock_hass, config=client_config)
            if email:
                client.email_account = email
            if args.debug:
                client.is_debug = True
            res = await client.password_login(phone, password)

            if res.get('errcode') != 0 and email and client._looks_like_rk001_message(res):
                print(f"手机号遇 RK001 流控，降级到邮箱登录: {email}")
                res = await client.password_login(email, password, encode=False, max_retry=2)

            if res.get('errcode') != 0:
                print(f"登录失败: {res}")
                return
            
            print("登录成功，正在拉取数据...")
            await client.refresh_data(force_refresh=True)

            door_accounts = client.doorAccountDict
            ele_fixed_price = getattr(client, "price", 0.475) or 0.475
            if not door_accounts:
                print("未获取到户号信息。")
                return

            print(f"获取到 {len(door_accounts)} 个户号。")

            # 实体列表与 ele/sensor.py 的 SENSOR_TYPES 保持一致
            ele_entities = [
                {"key": "balance", "name": "账户余额"},
                {"key": "year_ele_num", "name": "年度累计用电"},
                {"key": "year_ele_cost", "name": "年度累计电费"},
                {"key": "last_month_ele_num", "name": "上个月用电"},
                {"key": "last_month_ele_cost", "name": "上个月电费"},
                {"key": "month_ele_num", "name": "当月累计用电"},
                {"key": "month_ele_cost", "name": "当月累计电费"},
                {"key": "daily_ele_num", "name": "日用电"},
                {"key": "daily_ele_cost", "name": "日电费用"},
                {"key": "recent_30_daily_ele_list", "name": "最近30天每日用电"},
                {"key": "recent_12_monthly_ele_list", "name": "最近12个月每月用电"},
                {"key": "daily_lasted_date", "name": "最新日用电日期"},
                {"key": "refresh_time", "name": "最近刷新时间"},
            ]

            if args.all:
                for cons_no, account in door_accounts.items():
                    addr = account.get("elecAddr_dst", account.get("elecAddr", "未知地址"))
                    print(f"\n=== 户号: {cons_no} ({addr}) ===")
                    outputs = []
                    for entity_conf in ele_entities:
                        outputs.append(_build_ele_entity_output(entity_conf, account, ele_fixed_price, args.debug))
                    print(json.dumps(outputs, indent=2, ensure_ascii=False))
                    if args.output_file:
                        with open(args.output_file, "a", encoding="utf-8") as f:
                            f.write(json.dumps(outputs, indent=2, ensure_ascii=False) + "\n")
            else:
                while True:
                    print("\n可选实体:")
                    for i, entity in enumerate(ele_entities):
                        print(f"{i + 1}. {entity['name']} ({entity['key']})")
                    print("0. 退出")
                    try:
                        choice = input("请选择实体序号: ").strip()
                        if choice == "0":
                            break
                        idx = int(choice) - 1
                        if 0 <= idx < len(ele_entities):
                            entity_conf = ele_entities[idx]
                            outputs = []
                            for cons_no, account in door_accounts.items():
                                addr = account.get("elecAddr_dst", account.get("elecAddr", "未知地址"))
                                print(f"\n=== 户号: {cons_no} ({addr}) ===")
                                output = _build_ele_entity_output(entity_conf, account, ele_fixed_price, args.debug)
                                outputs.append(output)
                                print(json.dumps(output, indent=2, ensure_ascii=False))
                            if args.output_file:
                                with open(args.output_file, "a", encoding="utf-8") as f:
                                    f.write(json.dumps(outputs, indent=2, ensure_ascii=False) + "\n")
                        else:
                            print("无效序号")
                    except ValueError:
                        print("请输入数字")
                    except KeyboardInterrupt:
                        break

# ==================== 主入口 ====================

async def debug_gas(args) -> None:
    """燃气模式调试入口：异步登录并拉取 API 数据，输出实体 JSON。"""
    phone = args.phone
    if not phone:
        default_phone = os.getenv("XJRQ_PHONE", "").strip()
        prompt = f"请输入燃气手机号" + (f" [{default_phone}]" if default_phone else "") + ": "
        phone = input(prompt).strip()
        if not phone and default_phone:
            phone = default_phone

    if not phone:
        print("账号不能为空！")
        return

    password = args.password
    if not password:
        default_password = os.getenv("XJRQ_PASSWORD", "")
        prompt = f"请输入密码" + (" [检测到环境变量]" if default_password else "") + ": "
        if args.password_hidden:
            try:
                password = getpass.getpass(prompt).strip()
            except Exception:
                password = input(prompt).strip()
        else:
            password = input(prompt).strip()

        if not password and default_password:
            password = default_password

    if not password:
        print("密码不能为空！")
        return

    print(f"\n正在登录燃气账号: {phone} ...")
    api = XjGasAPI(phone=phone, password=password)
    arrearage = await api.get_arrearage()
    fee_record = await api.get_fee_record(months=args.fee_months)
    meter_info = await api.get_meter_info(months=args.meter_months)
    payment_record = await api.get_payment_record()
    daily_usage = await api.get_daily_usage(days=args.daily_days)
    await api.close()
    update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    options = {
        "pricing_mode": args.pricing_mode,
        "fixed_price": args.fixed_price,
        "tier_1_limit": args.tier_1_limit,
        "tier_1_price": args.tier_1_price,
        "tier_2_limit": args.tier_2_limit,
        "tier_2_price": args.tier_2_price,
        "tier_3_price": args.tier_3_price,
    }

    data = {
        "arrearage": arrearage,
        "fee_record": fee_record,
        "meter_info": meter_info,
        "payment_record": payment_record,
        "daily_usage": daily_usage,
        "update_time": update_time,
    }
    data["latest_data_time"] = _native_value_for(data, "latest_data_time", "value")

    cons_no = getattr(api, "cons_no", None)
    account_tail = cons_no[-4:] if cons_no and len(cons_no) >= 4 else (cons_no if cons_no else phone[-4:] if len(phone) >= 4 else phone)
    entities = [
        {"key": "balance", "name": "账户余额", "type": "gas_info"},
        {"key": "current_unit_price", "name": "当前用气单价", "type": "current_unit_price"},
        {"key": "current_tier", "name": "当前计价阶梯", "type": "current_tier"},
        {"key": "last_fee", "name": "月度账单", "data_source": "fee_record", "data_key": "last_fee"},
        {"key": "annual_fee", "name": "年度账单", "data_source": "fee_record", "data_key": "annual_fee"},
        {"key": "last_payment", "name": "最近交费", "data_source": "payment_record", "data_key": "last_payment"},
        {"key": "last_usage", "name": "月度用气量", "data_source": "meter_info", "data_key": "last_usage"},
        {"key": "year_gas_num", "name": "年度用气量", "data_source": "meter_info", "data_key": "year_gas_num"},
        {"key": "daily_usage", "name": "日用气量", "data_source": "daily_usage", "data_key": "useGas"},
        {"key": "current_reading", "name": "当前抄表数", "data_source": "meter_info", "data_key": "current_reading"},
        {"key": "update_time", "name": "最近刷新时间", "data_source": "update_time", "data_key": "value"},
        {"key": "latest_data_time", "name": "最新数据时间", "data_source": "latest_data_time", "data_key": "value"},
        {"key": "daily_cost", "name": "日账单", "type": "daily_cost"},
    ]

    if args.all:
        outputs = []
        for entity in entities:
            outputs.append(_build_entity_output(entity, data, options, account_tail))
        print(json.dumps(outputs, indent=2, ensure_ascii=False))
        if args.output_file:
            with open(args.output_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(outputs, indent=2, ensure_ascii=False))
    else:
        while True:
            print("\n可选实体:")
            for i, entity in enumerate(entities):
                print(f"{i + 1}. {entity['name']} ({entity['key']})")
            print("0. 退出")
            try:
                choice = input("请选择实体序号: ").strip()
                if choice == "0":
                    break
                idx = int(choice) - 1
                if 0 <= idx < len(entities):
                    output = _build_entity_output(entities[idx], data, options, account_tail)
                    print(json.dumps(output, indent=2, ensure_ascii=False))
                else:
                    print("无效序号")
            except ValueError:
                print("请输入数字")
            except KeyboardInterrupt:
                break


def main() -> None:
    """交互式调试入口：解析参数、选择燃气/电力模式、输出实体 JSON。

    支持命令行参数与交互输入账号密码，可逐项或一次性输出模拟实体数据。
    燃气模式：拉取 API → 构建 data → 按实体类型输出 state/attributes。
    电力模式：调用 debug_electricity 异步执行。
    """
    # 解析命令行参数（需先解析，以便根据 --log-file 配置日志）
    parser = argparse.ArgumentParser(description="新疆电力燃气集成调试工具")
    parser.add_argument("--type", choices=["gas", "ele"], help="调试类型: gas (燃气) 或 ele (电力)")
    parser.add_argument("--phone", help="登录手机号（燃气/电力）")
    parser.add_argument("--password", help="登录密码")
    parser.add_argument("--password-hidden", action="store_true", help="密码输入时隐藏字符")
    parser.add_argument("--daily-days", type=int, default=30, help="燃气：拉取日用气天数")
    parser.add_argument("--fee-months", type=int, default=13, help="燃气：拉取账单月数")
    parser.add_argument("--meter-months", type=int, default=13, help="燃气：拉取抄表月数")
    parser.add_argument("--pricing-mode", default=DEFAULT_PRICING_MODE, help="燃气：fixed 或 tiered")
    parser.add_argument("--fixed-price", type=float, default=DEFAULT_FIXED_PRICE, help="燃气：固定单价")
    parser.add_argument("--tier-1-limit", type=float, default=DEFAULT_TIER_1_LIMIT, help="燃气：第1档上限(m³)")
    parser.add_argument("--tier-1-price", type=float, default=DEFAULT_TIER_1_PRICE, help="燃气：第1档单价")
    parser.add_argument("--tier-2-limit", type=float, default=DEFAULT_TIER_2_LIMIT, help="燃气：第2档上限(m³)")
    parser.add_argument("--tier-2-price", type=float, default=DEFAULT_TIER_2_PRICE, help="燃气：第2档单价")
    parser.add_argument("--tier-3-price", type=float, default=DEFAULT_TIER_3_PRICE, help="燃气：第3档单价")
    parser.add_argument("--all", action="store_true", help="一次性输出全部实体，不交互")
    parser.add_argument("--output-file", help="将输出写入指定文件")
    parser.add_argument("--email", help="电力：备用邮箱（RK001 流控降级）")
    parser.add_argument("--ark-api-key", help="电力：LLM API Key（验证码识别）")
    parser.add_argument("--ark-base-url", help="电力：LLM Base URL")
    parser.add_argument("--ark-model", help="电力：LLM 模型名称")
    parser.add_argument("--debug", action="store_true", help="电力模式：开启 API 调试日志")
    parser.add_argument("--log-file", metavar="PATH", help="将完整日志写入文件，便于查看（如 --log-file debug.log）")
    parser.add_argument("--price", type=float, default=0.475, help="电力模式：固定电价（元/kWh），用于估算日/月电费")
    args = parser.parse_args()

    # 配置日志：INFO 级别，抑制 urllib3、gas.api 的 DEBUG 输出
    log_fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(level=logging.INFO, format=log_fmt)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("gas.api").setLevel(logging.WARNING)
    if getattr(args, 'log_file', None):
        fh = logging.FileHandler(args.log_file, mode='w', encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(log_fmt))
        logging.getLogger().addHandler(fh)
        print(f"完整日志将写入: {args.log_file}")

    print("\n=============================================")
    print("      新疆电力燃气(XjEleGas)集成调试工具")
    print("=============================================")

    # 若未指定 --type，则交互选择燃气或电力
    debug_type = args.type
    if not debug_type:
        print("\n请选择调试类型:")
        print("1. 燃气 (Gas)")
        print("2. 电力 (Ele)")
        while True:
            try:
                choice = input("请输入序号 (1/2): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消")
                return
            
            if choice == "1":
                debug_type = "gas"
                break
            elif choice == "2":
                debug_type = "ele"
                break
            else:
                print("无效输入，请输入 1 或 2")

    print(f"\n当前模式: {'燃气 (Gas)' if debug_type == 'gas' else '电力 (Ele)'}")

    if debug_type == "ele":
        asyncio.run(debug_electricity(args))
        return

    asyncio.run(debug_gas(args))


# 脚本直接运行时执行主入口
if __name__ == "__main__":
    main()
