"""
燃气数据本地持久化存储模块。

本模块使用 Home Assistant Store（JSON 文件）持久化燃气数据，与 HA 数据持久化机制一致：

功能概述：
1. 将 API 返回的日用气、月账单、月用气等数据写入 .storage 目录下的 JSON 文件
2. 按保留规则自动清理：日数据 1 年、月数据 24 个月、年数据 3 年
3. 内存缓存加速读取，传感器优先使用缓存，补齐服务器返回不足的数据
4. 阶梯计费：年度用气量汇总用于判断当前阶梯档位

存储路径：.storage/xjelegas_gas_{entry_id}
"""
from __future__ import annotations

# ========== 标准库 ==========
import json  # JSON 序列化/反序列化，用于存储数据读写
import logging  # 日志记录
from datetime import timedelta, datetime  # 时间间隔（保留周期）、日期时间处理
from typing import Any, Dict, List  # 类型注解

# ========== Home Assistant ==========
from homeassistant.helpers.json import JSONEncoder  # HA 扩展的 JSON 编码器，支持 datetime 等类型序列化
from homeassistant.helpers.storage import Store  # 基于 JSON 文件的持久化存储基类
from homeassistant.util import dt as dt_util  # HA 日期时间工具，时区转换、格式化等

# ========== 本集成常量（计费与阶梯配置） ==========
from ..const import (
    CONF_FIXED_PRICE,      # 固定单价配置键
    CONF_PRICING_MODE,     # 计费模式配置键（固定/阶梯）
    CONF_TIER_1_LIMIT,     # 一档上限配置键
    CONF_TIER_1_PRICE,     # 一档单价配置键
    CONF_TIER_2_LIMIT,     # 二档上限配置键
    CONF_TIER_2_PRICE,     # 二档单价配置键
    CONF_TIER_3_PRICE,     # 三档单价配置键
    DEFAULT_FIXED_PRICE,   # 默认固定单价
    DEFAULT_PRICING_MODE,  # 默认计费模式
    DEFAULT_TIER_1_LIMIT,  # 默认一档上限（立方米）
    DEFAULT_TIER_1_PRICE,  # 默认一档单价
    DEFAULT_TIER_2_LIMIT,  # 默认二档上限（立方米）
    DEFAULT_TIER_2_PRICE,  # 默认二档单价
    DEFAULT_TIER_3_PRICE,  # 默认三档单价
    PRICING_MODE_FIXED,    # 固定单价模式常量
)

# 存储版本号，用于数据迁移时识别格式
STORAGE_VERSION = 1
# 存储键前缀，最终键名为 xjelegas_gas_{entry_id}
STORAGE_KEY_PREFIX = "xjelegas_gas"

_LOGGER = logging.getLogger(__name__)


def _get_store_key(entry_id: str) -> str:
    """
    根据配置条目 ID 生成唯一的存储键名。

    Args:
        entry_id: Home Assistant 配置条目的唯一标识符

    Returns:
        格式为 "xjelegas_gas_{entry_id}" 的存储键字符串
    """
    return f"{STORAGE_KEY_PREFIX}_{entry_id}"


class XjGasStorage:
    """
    燃气数据本地存储类。

    使用 Home Assistant Store 持久化日/月/年数据，并通过内存缓存加速读取。
    每次 persist 后刷新缓存，传感器优先使用缓存数据。
    """

    def __init__(self, hass, config_entry):
        """
        初始化存储实例。

        Args:
            hass: Home Assistant 核心实例，用于访问 Store 和配置路径
            config_entry: 配置条目，用于获取 entry_id 和计费选项（阶梯/固定单价）
        """
        self._hass = hass
        self._config_entry = config_entry
        # 生成当前配置的唯一存储键
        self._store_key = _get_store_key(config_entry.entry_id)
        # 创建 HA Store 实例，使用原子写入避免写入中断导致数据损坏
        self._store = Store(
            hass,
            STORAGE_VERSION,
            self._store_key,
            encoder=JSONEncoder,
            atomic_writes=True,
        )
        # 运行期内存缓存，避免频繁读磁盘
        self._cache: Dict[str, Any] = {
            "daily": [],           # 日用量记录列表，按日期倒序
            "monthly_bill": [],    # 月账单记录列表
            "monthly_usage": [],   # 月用气量记录列表
            "annual_bill": {},     # 年度总费用 {年份: 金额}
            "year_gas_num": {},    # 年度总用气量 {年份: 用量}
        }
        # 标记缓存是否已从 Store 加载，避免重复加载
        self._cache_loaded = False

    async def async_load_initial(self) -> None:
        """
        启动时从 Store 加载已有数据到内存缓存。

        协调器应在首次 API 更新前调用此方法，以便 HA 重启后传感器能立即
        使用历史数据，无需等待 API 首次返回。
        """
        if self._cache_loaded:
            return
        try:
            loaded_data = await self._load_from_store()
            if loaded_data:
                self._cache = loaded_data
            self._cache_loaded = True
        except Exception as e:
            _LOGGER.warning("加载燃气缓存失败: %s", e)
            self._cache_loaded = True

    async def _load_from_store(self) -> Dict[str, Any]:
        """
        从 Home Assistant Store 异步加载持久化数据。

        Returns:
            包含 daily/monthly_bill/monthly_usage/annual_bill/year_gas_num 的字典，
            若加载失败或数据无效则返回空字典
        """
        loaded_data = await self._store.async_load()
        if loaded_data and isinstance(loaded_data, dict):
            return loaded_data
        return {}

    async def persist(self, data: Dict[str, Any]) -> None:
        """
        异步持久化协调器汇总的数据到 HA Store，并刷新内存缓存。

        流程：1) 在 executor 中解析 API 数据构建缓存结构；
             2) 加载 Store 已有数据并合并；
             3) 应用保留规则（日 1 年、月 24 个月、年 3 年）；
             4) 写入 Store 并更新 _cache。

        Args:
            data: 协调器从 API 获取的原始数据，需包含：
                  - daily_usage: 日用量列表
                  - fee_record: 月账单/缴费记录
                  - meter_info: 月用气量/表计信息
        """
        if not data:
            return
        try:
            # 在线程池中执行解析，避免阻塞事件循环
            result = await self._hass.async_add_executor_job(
                self._build_cache_from_data, data
            )
            if result:
                stored_data = await self._load_from_store()
                # 合并新旧数据并应用保留规则，计算年度汇总
                merged = self._merge_and_apply_retention(stored_data, result)
                await self._store.async_save(merged)
                self._cache = merged
                self._cache_loaded = True
        except Exception as e:
            _LOGGER.warning("燃气数据持久化失败: %s", e)

    def _build_cache_from_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        从 API 返回的原始数据构建标准化的缓存结构。

        此方法在 executor 中执行（避免阻塞事件循环），解析 daily_usage、
        fee_record、meter_info 等字段，统一日期/月份格式，并按阶梯或固定
        单价计算费用。年度汇总由 _merge_and_apply_retention 在合并时计算。

        Args:
            data: API 原始数据字典

        Returns:
            包含 daily、monthly_bill、monthly_usage 的字典
        """
        # 解析日用量数据
        daily_rows = self._extract_rows(data.get("daily_usage"))
        unit_price = self._current_unit_price(data)
        daily_records = []
        for row in daily_rows:
            day = self._day_key_from_row(row)
            if not day:
                continue
            # 兼容多种 API 字段名提取用量
            usage = self._extract_number(
                row,
                ["useGas", "gasVolume", "dailyGas", "usage", "gas", "num", "sl", "amount"],
            )
            # 若 API 未返回费用，则按单价估算
            cost = round(usage * unit_price, 2) if unit_price > 0 and usage > 0 else 0.0
            row_with_day = dict(row)
            row_with_day["day"] = day
            daily_records.append({"day": day, "usage": usage, "cost": cost, "raw": row_with_day})

        # 解析月账单/缴费记录
        monthly_rows = self._extract_rows(data.get("fee_record"))
        monthly_bill_records = []
        for row in monthly_rows:
            month = self._month_key_from_row(row)
            if not month:
                continue
            amount = self._extract_number(
                row,
                [
                    "paidInGasFee",
                    "payableGasFee",
                    "rcvblamt",
                    "money",
                    "totalFee",
                    "payMoney",
                    "fee",
                    "billAmount",
                    "rcvedamt",
                    "amt",
                    "gasFee",
                    "pay",
                ],
            )
            row_with_month = dict(row)
            row_with_month["month"] = month
            monthly_bill_records.append(
                {"month": month, "amount": amount, "raw": row_with_month}
            )

        # 解析月用气量/表计信息
        usage_rows = self._extract_rows(data.get("meter_info"))
        monthly_usage_records = []
        for row in usage_rows:
            month = self._month_key_from_row(row)
            if not month:
                continue
            usage = self._extract_number(
                row,
                [
                    "gasSl",
                    "thisUse",
                    "useAmount",
                    "gasAmount",
                    "yl",
                    "useGas",
                    "usage",
                    "gas",
                    "num",
                    "sl",
                    "amount",
                ],
            )
            row_with_month = dict(row)
            row_with_month["month"] = month
            monthly_usage_records.append(
                {"month": month, "usage": usage, "raw": row_with_month}
            )

        return {
            "daily": daily_records,
            "monthly_bill": monthly_bill_records,
            "monthly_usage": monthly_usage_records,
        }

    def _merge_and_apply_retention(
        self, stored_data: Dict[str, Any], new_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        合并 Store 已有数据与本次 API 新数据，并应用数据保留规则。

        保留规则：
        - 日数据：保留最近 366 天（约 1 年）
        - 月数据：保留最近 24 个月
        - 年数据：保留最近 3 年（当年及前两年），由月数据汇总计算

        合并策略：以日期/月份为键，新数据覆盖同键的旧数据。

        Args:
            stored_data: Store 中已有的数据
            new_data: 本次从 API 解析得到的新数据

        Returns:
            合并并裁剪后的完整缓存结构
        """
        # 日数据截止日期：366 天前（约 1 年），早于此日期的记录将被丢弃
        cutoff_day = (
            dt_util.now().date() - timedelta(days=366)
        ).strftime("%Y-%m-%d")
        current = dt_util.now().date()
        # 月数据截止：24 个月前，将当前年月转为“总月数”便于计算
        total_months = current.year * 12 + (current.month - 1)
        start_month_offset = total_months - 23  # 往前推 23 个月，加上当前月共 24 个月
        start_year = start_month_offset // 12
        start_month = start_month_offset % 12 + 1
        start_month_str = f"{start_year:04d}-{start_month:02d}"  # 格式化为 YYYY-MM
        current_year = current.year
        # 年度汇总：当年及前两年
        annual_years = [current_year - 2, current_year - 1, current_year]

        # 合并日数据：以 day 为键建立映射，新数据覆盖同日期旧数据
        daily_map = {r["day"]: r for r in stored_data.get("daily", [])}
        for r in new_data.get("daily", []):
            daily_map[r["day"]] = r
        # 过滤超出保留期的日数据（早于 cutoff_day 的丢弃），按日期倒序排列
        daily_list = [v for k, v in daily_map.items() if k >= cutoff_day]
        daily_list.sort(key=lambda x: x["day"], reverse=True)

        # 合并月账单：以 month 为键，过滤保留 24 个月内
        monthly_bill_map = {r["month"]: r for r in stored_data.get("monthly_bill", [])}
        for r in new_data.get("monthly_bill", []):
            monthly_bill_map[r["month"]] = r
        monthly_bill_list = [
            v for k, v in monthly_bill_map.items() if k >= start_month_str
        ]
        monthly_bill_list.sort(key=lambda x: x["month"], reverse=True)

        # 合并月用气量：以 month 为键，过滤保留 24 个月内
        monthly_usage_map = {r["month"]: r for r in stored_data.get("monthly_usage", [])}
        for r in new_data.get("monthly_usage", []):
            monthly_usage_map[r["month"]] = r
        monthly_usage_list = [
            v for k, v in monthly_usage_map.items() if k >= start_month_str
        ]
        monthly_usage_list.sort(key=lambda x: x["month"], reverse=True)

        # 按年度汇总月账单和月用气量（annual_usage_by_year 写入 year_gas_num 键），用于阶梯计费档位判断和年度统计展示
        annual_bill = {}
        annual_usage_by_year = {}
        for year in annual_years:
            prefix = f"{year:04d}-"  # 如 "2024-" 用于筛选该年月份
            total_amount = sum(
                r["amount"]
                for r in monthly_bill_list
                if r["month"].startswith(prefix)
            )
            total_usage = sum(
                r["usage"]
                for r in monthly_usage_list
                if r["month"].startswith(prefix)
            )
            annual_bill[str(year)] = round(total_amount, 2)
            annual_usage_by_year[str(year)] = round(total_usage, 2)

        return {
            "daily": daily_list,
            "monthly_bill": monthly_bill_list,
            "monthly_usage": monthly_usage_list,
            "annual_bill": annual_bill,
            "year_gas_num": annual_usage_by_year,
        }

    @property
    def daily_records(self) -> List[Dict[str, Any]]:
        """日用量记录列表，每项含 day、usage、cost、raw，按日期倒序。"""
        return self._cache.get("daily", [])

    @property
    def monthly_bills(self) -> List[Dict[str, Any]]:
        """月账单记录列表，每项含 month、amount、raw，按月份倒序。"""
        return self._cache.get("monthly_bill", [])

    @property
    def monthly_usage(self) -> List[Dict[str, Any]]:
        """月用气量记录列表，每项含 month、usage、raw，按月份倒序。"""
        return self._cache.get("monthly_usage", [])

    @property
    def annual_bills(self) -> Dict[str, float]:
        """年度总费用字典，键为年份字符串，值为该年总金额。"""
        return self._cache.get("annual_bill", {})

    @property
    def year_gas_num(self) -> Dict[str, float]:
        """年度总用气量字典，键为年份字符串，值为该年总用量。"""
        return self._cache.get("year_gas_num", {})

    def _extract_rows(self, payload: Any) -> List[Dict[str, Any]]:
        """
        从 API 接口返回的 payload 中提取记录列表。

        兼容多种常见嵌套结构：直接列表、payload.rows、payload.obj、
        payload.obj.result、payload.data、payload.data.rows 等，
        以适配不同燃气公司 API 的返回格式。

        Args:
            payload: API 返回的原始数据，可能为 list 或 dict

        Returns:
            记录行列表，每项为字典；无法解析时返回空列表
        """
        if not payload:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            # 优先尝试 payload.rows
            rows = payload.get("rows")
            if isinstance(rows, list):
                return rows
            # 尝试 payload.obj（可能是 list 或 dict）
            obj = payload.get("obj")
            if isinstance(obj, list):
                return obj
            if isinstance(obj, dict):
                rows = obj.get("result") or obj.get("rows")
                if isinstance(rows, list):
                    return rows
            # 尝试 payload.data
            data = payload.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                rows = data.get("rows")
                if isinstance(rows, list):
                    return rows
        return []

    def _normalize_day(self, value: Any) -> str:
        """
        将各种格式的日期值统一为 YYYY-MM-DD 字符串。

        支持：数字、带空格/ISO 时间戳、斜杠/点号分隔等格式。

        Args:
            value: 原始日期值，可为数字、字符串等

        Returns:
            标准化后的日期字符串，无法解析时返回空字符串
        """
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            value = str(int(value))
        if not isinstance(value, str):
            return ""
        value = value.strip()
        # 去掉时间部分，仅保留日期（如 "2024-03-07 12:00" 或 "2024-03-07T12:00"）
        if " " in value:
            value = value.split(" ", 1)[0]
        if "T" in value:
            value = value.split("T", 1)[0]
        value = value.replace("/", "-").replace(".", "-")
        digits = value.replace("-", "")
        # 8 位纯数字如 20240307 -> 2024-03-07
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        # 已用 "-" 分隔的格式，校验并补零
        parts = [p for p in value.split("-") if p]
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            year = parts[0].zfill(4)
            month = parts[1].zfill(2)
            day = parts[2].zfill(2)
            if len(year) == 4:
                return f"{year}-{month}-{day}"
        return value

    def _day_key_from_row(self, row: Dict[str, Any]) -> str:
        """
        从单条记录行中提取并标准化日期字段。

        按优先级尝试多个常见字段名：gasDay、day、date、rq、readDate 等，
        取能成功解析且最大的日期（用于处理多字段情况）。

        Args:
            row: 单条记录字典

        Returns:
            标准化后的 YYYY-MM-DD 字符串，无有效日期时返回空字符串
        """
        candidates = []
        for key in ["gasDay", "day", "date", "rq", "readDate", "chargeDate", "payDate", "time"]:
            if key in row:
                normalized = self._normalize_day(row.get(key))
                if normalized:
                    candidates.append(normalized)
        # 若有多字段，取最大日期（通常为最新数据）
        return max(candidates) if candidates else ""

    def _normalize_month(self, value: Any) -> str:
        """
        将各种格式的月份值统一为 YYYY-MM 字符串。

        支持 6 位（202403）、8 位（20240301 取前 6 位）等格式。

        Args:
            value: 原始月份值

        Returns:
            标准化后的 YYYY-MM 字符串
        """
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            value = str(int(value))
        if not isinstance(value, str):
            return ""
        value = value.strip().replace("/", "-").replace(".", "-")
        digits = value.replace("-", "")
        # 6 位如 202403 -> 2024-03
        if len(digits) == 6 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        # 8 位如 20240301 -> 取前 6 位得到 2024-03
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}"
        return value

    def _month_key_from_row(self, row: Dict[str, Any]) -> str:
        """
        从单条记录行中提取月份字段。

        按优先级尝试 readYm、month、billYm、ym、billMonth、chargeYm、date、rq 等
        常见字段名，适配不同燃气公司 API。

        Args:
            row: 单条记录字典

        Returns:
            标准化后的 YYYY-MM 字符串
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
        return self._normalize_month(month_value)

    def _extract_number(self, row: Dict[str, Any], keys: List[str]) -> float:
        """
        按候选键顺序从记录行中提取数值。

        用于兼容不同 API 的字段命名（如 useGas、gasVolume、usage 等），
        按 keys 顺序尝试，第一个有效值即返回。

        Args:
            row: 单条记录字典
            keys: 候选字段名列表，按优先级排序

        Returns:
            提取到的浮点数值，无有效值时返回 0.0
        """
        for key in keys:
            if key in row and row[key] is not None:
                try:
                    return float(row[key])
                except (ValueError, TypeError):
                    continue  # 该字段无法转为浮点数，尝试下一个候选键
        return 0.0

    def _current_unit_price(self, data: Dict[str, Any]) -> float:
        """
        根据计费模式与年度用气量计算当前适用的燃气单价。

        固定单价模式：直接返回配置的固定价格。
        阶梯计费模式：根据本年度累计用气量判断档位，
        一档用量 <= tier_1_limit 用 tier_1_price，
        二档用量 <= tier_2_limit 用 tier_2_price，
        超出则用 tier_3_price。

        Args:
            data: 包含 meter_info 的 API 数据，用于计算年度用量

        Returns:
            当前适用的单价（元/立方米）
        """
        options = self._config_entry.options or self._config_entry.data or {}
        pricing_mode = options.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
        # 固定单价模式：直接返回配置价格
        if pricing_mode == PRICING_MODE_FIXED:
            return options.get(CONF_FIXED_PRICE, DEFAULT_FIXED_PRICE)
        # 阶梯计费模式：根据年度用量判断档位
        tier_1_limit = options.get(CONF_TIER_1_LIMIT, DEFAULT_TIER_1_LIMIT)
        tier_2_limit = options.get(CONF_TIER_2_LIMIT, DEFAULT_TIER_2_LIMIT)
        tier_1_price = options.get(CONF_TIER_1_PRICE, DEFAULT_TIER_1_PRICE)
        tier_2_price = options.get(CONF_TIER_2_PRICE, DEFAULT_TIER_2_PRICE)
        tier_3_price = options.get(CONF_TIER_3_PRICE, DEFAULT_TIER_3_PRICE)
        yearly_usage = self._yearly_usage_from_rows(
            self._extract_rows(data.get("meter_info"))
        )
        # 按用量区间返回对应档位单价
        if yearly_usage <= tier_1_limit:
            return tier_1_price
        if yearly_usage <= tier_2_limit:
            return tier_2_price
        return tier_3_price  # 超出二档上限，使用三档单价

    def _yearly_usage_from_rows(self, rows: List[Dict[str, Any]]) -> float:
        """
        累计当前自然年度的月用气量总和。

        仅统计月份属于当前年的记录，用于阶梯计费时判断用户处于哪一档位。
        不同燃气公司 API 的用量字段名不同，通过 _extract_number 兼容多种命名。

        Args:
            rows: 月用气量记录列表（通常来自 meter_info）

        Returns:
            当前年度累计用气量（立方米）
        """
        total_usage = 0.0
        current_year = str(dt_util.now().year)
        for row in rows:
            month_key = self._month_key_from_row(row)
            # 仅累计当前自然年度的记录
            if not month_key or not month_key.startswith(current_year):
                continue
            usage = self._extract_number(
                row, ["gasSl", "thisUse", "useAmount", "gasAmount", "yl", "useGas"]
            )
            total_usage += usage
        return total_usage
