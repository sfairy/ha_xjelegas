"""
电力传感器平台模块。

本模块定义新疆电力相关的 Home Assistant 传感器实体，包括：
- 余额、年度/月度/日用电、电费、最近交费
- 最近 30 天日用电、最近 12 个月用电（供前端图表）
- 最新日用电日期、最近刷新时间

实体命名：sensor.ele_{户号后四位}_{类型}，如 sensor.ele_1234_balance
数据来源：StateGridCoordinator 定时调用 StateGridDataClient.refresh_data
"""
# ========== Home Assistant 传感器组件 ==========
from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,  # 传感器域，用于实体 ID 前缀
    SensorDeviceClass,        # 设备类（余额、能量等）
    SensorEntity,             # 传感器实体基类
    SensorStateClass,         # 状态类（总量、测量值等）
)
from homeassistant.config_entries import ConfigEntry  # 配置条目，用于 setup 回调
from homeassistant.const import UnitOfEnergy          # 能量单位（如 kWh）
from homeassistant.core import HomeAssistant          # HA 核心实例
from homeassistant.helpers.entity_platform import AddEntitiesCallback  # 添加实体回调
from homeassistant.helpers.update_coordinator import CoordinatorEntity  # 协调器实体基类

import logging
# ========== 本模块 ==========
from .const import DOMAIN
from .api import StateGridDataClient, StateGridCoordinator

_LOGGER = logging.getLogger(__name__)

# 货币单位常量
UNIT_YUAN = "元"

# 实体 ID 前缀格式（用于生成 sensor.ele_xxxx_类型）
ENTITY_ID_SENSOR_FORMAT = SENSOR_DOMAIN + ".ele_"

# ==========================================
# 传感器类型定义
# ==========================================
# 定义了集成支持的所有传感器类型及其属性（key、name、单位、设备类、描述等）
SENSOR_TYPES = [
    {
        "key": "balance",
        "name": "账户余额",
        "native_unit_of_measurement": UNIT_YUAN,
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.TOTAL,
        "description": "当前账户的剩余金额（含预存电费）。"
    },
    {
        "key": "year_ele_num",
        "name": "年度累计用电",
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "description": "本年度累计消耗的电量。"
    },
    {
        "key": "year_ele_cost",
        "name": "年度累计电费",
        "native_unit_of_measurement": UNIT_YUAN,
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.TOTAL,
        "description": "本年度累计产生的电费。"
    },
    {
        "key": "last_month_ele_num",
        "name": "上个月用电",
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "description": "上一个自然月的总用电量。"
    },
    {
        "key": "last_month_ele_cost",
        "name": "上个月电费",
        "native_unit_of_measurement": UNIT_YUAN,
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.TOTAL,
        "description": "上一个自然月的总电费。"
    },
    {
        "key": "last_month_meter_num",
        "name": "上个月抄表",
        "state_class": SensorStateClass.TOTAL,
        "description": "上个月抄表读数。"
    },
    {
        "key": "month_ele_num",
        "name": "当月累计用电",
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "description": "本月截止目前的累计用电量。"
    },
    {
        "key": "month_ele_cost",
        "name": "当月累计电费",
        "native_unit_of_measurement": UNIT_YUAN,
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.TOTAL,
        "description": "本月截止目前的累计电费。"
    },
    {
        "key": "daily_ele_num",
        "name": "日用电",
        "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "description": "昨日（或最新数据日）的全天用电量。"
    },
    {
        "key": "daily_ele_cost",
        "name": "日电费",
        "native_unit_of_measurement": UNIT_YUAN,
        "device_class": SensorDeviceClass.MONETARY,
        "state_class": SensorStateClass.TOTAL,
        "description": "昨日（或最新数据日）的全天电费。"
    },
    {
        "key": "recent_30_daily_ele_list",
        "name": "最近30天每日用电",
        "description": "用于前端图表展示的最近30天用电数据（JSON格式）。"
    },
    {
        "key": "recent_12_monthly_ele_list",
        "name": "最近12个月每月用电",
        "description": "用于前端图表展示的最近12个月用电数据（JSON格式）。"
    },
    {
        "key": "daily_lasted_date",
        "name": "最新日用电日期",
        "description": "日用电数据更新到的日期。"
    },
    {
        "key": "refresh_time",
        "name": "最近刷新时间",
        "description": "集成最近一次成功获取数据的时间。"
    }
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """
    初始化传感器实体。
    
    Args:
        hass (HomeAssistant): Home Assistant 实例。
        entry (ConfigEntry): 配置条目。
        async_add_entities (AddEntitiesCallback): 添加实体的回调。
    """
    # 从 hass.data 获取该配置条目对应的数据客户端
    data_client: StateGridDataClient = hass.data[DOMAIN].get(entry.entry_id)
    if not data_client:
        return
    
    # 初始化数据协调器
    coordinator = StateGridCoordinator(hass, data_client)
    data_client.coordinator = coordinator
    
    async def _async_finish_startup():
        """后台执行首次刷新和实体添加，避免阻塞启动流程。"""
        try:
            # 如果没有缓存数据，强制执行首次刷新（包含登录、获取户号）
            if not data_client.get_door_account_list():
                await coordinator.async_config_entry_first_refresh()
            else:
                # 有缓存数据，触发后台刷新即可
                hass.async_create_task(coordinator.async_request_refresh())

            # 获取户号列表并创建实体
            door_account_list = data_client.get_door_account_list()
            entities = []
            for door_account in door_account_list:
                entities.extend(
                    [StateGridSensor(door_account, sensor_type, entry.entry_id, coordinator) 
                     for sensor_type in SENSOR_TYPES]
                )
            
            if entities:
                async_add_entities(entities)
        except Exception as e:
            _LOGGER.error("集成初始化失败: %s", e)

    # 将初始化逻辑放入后台任务
    hass.async_create_task(_async_finish_startup())


class StateGridSensor(CoordinatorEntity[StateGridCoordinator], SensorEntity):
    """
    新疆电力传感器实体。
    
    继承自 CoordinatorEntity，当协调器更新数据时自动刷新状态。
    """

    # 使用实体名称作为显示名（设备名 + 实体名）
    _attr_has_entity_name = True
    # 不记录到 Home Assistant 历史数据库的属性，避免大量 JSON/列表数据导致数据库膨胀
    _unrecorded_attributes = frozenset(
        {
            "recent_30_daily_ele_list",
            "recent_12_monthly_ele_list",
            "refresh_time",
            "graph",
            "daylist",
            "monthlist",
            "yearlist",
            "month_bill_list",
            "daily_bill_list",
            "account_balance",
        }
    )

    def __init__(
        self,
        door_account,
        sensor_type,
        entry_id: str,
        coordinator: StateGridCoordinator,
    ) -> None:
        """
        初始化传感器。
        
        Args:
            door_account (dict): 户号信息字典。
            sensor_type (dict): 传感器类型定义字典。
            entry_id (str): 配置条目 ID。
            coordinator (StateGridCoordinator): 数据协调器。
        """
        super().__init__(coordinator)
        self.door_account = door_account
        self.sensor_type = sensor_type
        
        # 格式为 sensor.ele_[户号后四位]_类型
        # 例如: sensor.ele_1234_balance
        cons_no = door_account["consNo_dst"]
        last_four = cons_no[-4:] if len(cons_no) >= 4 else cons_no
        self.entity_id = f"sensor.ele_{last_four}_{sensor_type['key']}"
        
        self._attr_name = sensor_type["name"]
        self._attr_unique_id = entry_id + "-" + door_account["consNo_dst"] + "-" + sensor_type["key"]

        if "device_class" in sensor_type:
            self._attr_device_class = sensor_type["device_class"]

        if "state_class" in sensor_type:
            self._attr_state_class = sensor_type["state_class"]

        if "native_unit_of_measurement" in sensor_type:
            self._attr_native_unit_of_measurement = sensor_type["native_unit_of_measurement"]

        # 设备信息，用于将多个传感器归类到同一个设备下
        self._attr_device_info = {
            "name": door_account["elecAddr_dst"],
            "identifiers": {(DOMAIN, door_account["consNo_dst"])},
            "manufacturer": "新疆电力",
            "model": "户号：" + door_account["consNo_dst"]
        }

    @property
    def native_value(self):
        """
        返回传感器的当前状态值。

        根据传感器类型从协调器数据中提取对应字段，部分类型有特殊处理逻辑
        （如余额多字段回退、电费按用电量×单价计算等）。
        """
        if self.coordinator.data is None:
            return None
        cons_no = self.door_account["consNo_dst"]
        if cons_no not in self.coordinator.data:
            return None
        door_data = self.coordinator.data[cons_no]
        
        # 列表类型的传感器状态显示为 "图表"，实际数据在属性中
        if self.sensor_type["key"] == "recent_30_daily_ele_list" or self.sensor_type["key"] == "recent_12_monthly_ele_list":
            return "图表"
        
        sensor_value = door_data.get(self.sensor_type["key"])
        
        # 为余额传感器提供默认值保护，并从 account_balance 尝试提取
        if self.sensor_type["key"] == "balance":
            if sensor_value is not None:
                try:
                    float_value = float(sensor_value)
                    if float_value != 0:
                        return round(float_value, 2)
                except (ValueError, TypeError):
                    pass
            account_balance = door_data.get("account_balance") or {}
            if isinstance(account_balance, dict):
                for key in ("accountBalance", "prestoreBalance", "sumMoney", "balance", "sumTotalAmt", "totalAmount"):
                    balance_value = account_balance.get(key)
                    if balance_value is not None:
                        try:
                            float_value = float(balance_value)
                            if float_value != 0:
                                return round(float_value, 2)
                        except (ValueError, TypeError):
                            pass
            return 0.0

        # 当月累计电费：日用电接口无费用，当月用电量×固定单价（配置选项）
        if self.sensor_type["key"] == "month_ele_cost":
            if sensor_value is not None:
                try:
                    float_value = float(sensor_value)
                    if float_value != 0:
                        return round(float_value, 2)
                except (ValueError, TypeError):
                    pass
            month_ele = float(door_data.get("month_ele_num") or 0)
            if month_ele <= 0:
                return 0.0
            fixed_price = getattr(self.coordinator.data_client, "price", 0.475) or 0.475  # 配置选项中的统一电价
            return round(month_ele * fixed_price, 2)

        # 日电费用：日用电接口无费用，用电量×固定单价（配置选项）
        if self.sensor_type["key"] == "daily_ele_cost":
            if sensor_value is not None:
                try:
                    float_value = round(float(sensor_value), 2)
                    if float_value != 0:
                        return float_value
                except (ValueError, TypeError):
                    pass
            daily_ele = float(door_data.get("daily_ele_num") or 0)
            if daily_ele <= 0:
                return 0.0
            fixed_price = getattr(self.coordinator.data_client, "price", 0.475) or 0.475  # 配置选项中的统一电价
            return round(daily_ele * fixed_price, 2)

        return sensor_value

    @property
    def extra_state_attributes(self):
        """
        返回额外的状态属性。

        对于部分传感器（如日用电、月用电、交费记录等），在属性中提供
        详细的列表数据供前端卡片（如 xjele_info）使用。
        """
        if self.coordinator.data is None:
            return {}
        cons_no = self.door_account["consNo_dst"]
        if cons_no not in self.coordinator.data:
            return {}
        door_data = self.coordinator.data[cons_no]
        
        # 日电费：附加用电量和日期供前端展示
        if self.sensor_type["key"] == "daily_ele_cost":
            return {
                "日用电量": door_data.get("daily_ele_num"),
                "日期": door_data.get("daily_lasted_date"),
            }

        # 最近 30 天日用电：构建带电费的 daylist 供图表使用
        if self.sensor_type["key"] == "recent_30_daily_ele_list":
            raw = door_data.get("recent_30_daily_ele_list", [])
            fixed_price = getattr(self.coordinator.data_client, "price", 0.475) or 0.475  # 配置选项中的统一电价
            daylist = []
            for day_record in raw:
                ele_num = float(day_record.get("ele", 0))
                cost_val = day_record.get("cost")
                if cost_val is not None:
                    try:
                        day_cost = round(float(cost_val), 2)
                    except (ValueError, TypeError):
                        day_cost = round(ele_num * fixed_price, 2)
                else:
                    day_cost = round(ele_num * fixed_price, 2)
                if day_cost == 0 and ele_num > 0:
                    day_cost = round(ele_num * fixed_price, 2)
                daylist.append({
                    "day": day_record.get("day"),
                    "dayEleNum": day_record.get("ele"),
                    "dayEleCost": day_cost,
                })
            return {"graph": raw, "daylist": daylist}

        # 最近 12 个月用电：直接返回原始数据
        elif self.sensor_type["key"] == "recent_12_monthly_ele_list":
            return {"graph": door_data.get("recent_12_monthly_ele_list")}
            
        elif self.sensor_type["key"] == "balance":
            # 余额传感器承载了最全面的信息，用于 xjele_info 卡片展示
            attrs = {}
            
            # 1. 固定单价（配置选项中的统一电价，日用电接口无费用时用电量×此单价）
            fixed_price = getattr(self.coordinator.data_client, 'price', 0.475) or 0.475

            # 2. 构建日用电列表 (daylist)
            raw_day_list = door_data.get("recent_30_daily_ele_list", [])
            day_list = []
            for day in raw_day_list:
                ele_num = float(day.get("ele", 0))
                cost_val = day.get("cost")
                if cost_val is not None:
                    try:
                        est_cost = round(float(cost_val), 2)
                    except (ValueError, TypeError):
                        est_cost = round(ele_num * fixed_price, 2)
                else:
                    est_cost = round(ele_num * fixed_price, 2)
                if est_cost == 0 and ele_num > 0:
                    est_cost = round(ele_num * fixed_price, 2)

                day_list.append({
                    "day": day.get("day"),
                    "dayEleNum": day.get("ele"),
                    "dayEleCost": est_cost,
                })
            attrs["daylist"] = day_list

            # 3. 构建月用电列表 (monthlist)
            raw_month_list = door_data.get("recent_12_monthly_ele_list", [])
            month_list = []
            for month in raw_month_list:
                month_list.append({
                    "month": month.get("month"),
                    "monthEleNum": month.get("ele"),
                    "monthEleCost": month.get("cost"),
                })
            attrs["monthlist"] = month_list

            # 4. 构建年用电列表 (yearlist)
            raw_year_bills = door_data.get("month_bill_list", [])
            year_map = {}
            for bill in raw_year_bills:
                year = bill.get("month", "")[:4]
                if year:
                    if year not in year_map:
                        year_map[year] = {
                            "year": year,
                            "yearEleNum": 0.0,
                            "yearEleCost": 0.0,
                        }
                    try:
                        year_map[year]["yearEleNum"] += float(bill.get("monthEleNum", 0))
                        year_map[year]["yearEleCost"] += float(bill.get("monthEleCost", 0))
                    except (ValueError, TypeError):
                        pass
            
            year_list = []
            for y_data in year_map.values():
                year_list.append({
                    "year": y_data["year"],
                    "yearEleNum": round(y_data["yearEleNum"], 2),
                    "yearEleCost": round(y_data["yearEleCost"], 2),
                })
            year_list.sort(key=lambda x: x["year"], reverse=True)
            attrs["yearlist"] = year_list
            
            # 5. 完整月度账单历史（原始 API 数据）
            attrs["month_bill_list"] = door_data.get("month_bill_list", [])

            # 5c. 完整日用电历史（原始 API 数据）
            attrs["daily_bill_list"] = door_data.get("daily_bill_list", [])

            attrs["date"] = door_data.get("refresh_time")
            attrs["consumer_name"] = self.door_account.get("consName_dst")
            
            # 6. 计算日均消费与剩余天数（基于最近 7 天数据）
            if day_list:
                try:
                    recent_days = day_list[:7]  # 最近 7 天
                    if recent_days:
                        daily_usages = [float(day.get("dayEleNum", 0)) for day in recent_days]
                        if daily_usages:
                            avg_daily_usage = sum(daily_usages) / len(daily_usages)
                            attrs["avg_daily_usage"] = round(avg_daily_usage, 2)

                            avg_daily_cost = avg_daily_usage * fixed_price
                            attrs["avg_daily_cost"] = round(avg_daily_cost, 2)

                            balance = 0
                            try:
                                balance = float(door_data.get("balance", 0))
                            except (ValueError, TypeError):
                                pass

                            if balance > 0 and avg_daily_cost > 0:
                                attrs["remaining_days"] = int(balance / avg_daily_cost)
                            else:
                                attrs["remaining_days"] = 0
                except Exception:
                    pass
            
            attrs["数据源"] = "State Grid API"

            # 6b. 原始余额对象（含 consType、accountBalance 等完整字段）
            attrs["account_balance"] = door_data.get("account_balance")
            
            # 7. 预付费判断：优先使用配置，否则根据 API 的 consType 判断
            try:
                cfg_prepaid = getattr(self.coordinator.data_client, "is_prepaid", None)
                if cfg_prepaid is not None:
                    attrs["is_prepaid"] = bool(cfg_prepaid)
                elif self.door_account.get("account_balance"):
                    balance_data = self.door_account.get("account_balance", {})
                    # consType: 1-预付费
                    attrs["is_prepaid"] = str(balance_data.get("consType")) == "1"
                else:
                    attrs["is_prepaid"] = False
            except Exception:
                attrs["is_prepaid"] = False

            # 8. 计费标准信息（与卡片期望的字段对齐）
            year_ele_num = door_data.get("year_ele_num") or 0
            year_ele_cost = door_data.get("year_ele_cost") or 0
            ladder_info = {
                "当前计费模式": "固定电价",
                "计费标准": "平均单价",  # 卡片据此识别单价显示方式
                "平均单价": fixed_price,  # 供卡片显示电价
                "年累计用电量": year_ele_num,
                "年阶梯累计用电量": year_ele_num,  # 阶梯指示器使用，与年累计用电量一致
                "年累计电费": year_ele_cost,  # 年度费用，供阶梯指示器显示
            }
            attrs["计费标准"] = ladder_info
            
            # 补齐默认值
            if "avg_daily_cost" not in attrs:
                attrs["avg_daily_cost"] = 0.0
            if "remaining_days" not in attrs:
                attrs["remaining_days"] = 0

            # 9. 汇总其他传感器的值
            # 方便卡片直接从 balance 实体获取所有数据（使用英文键）
            for sensor_type in SENSOR_TYPES:
                if sensor_type["key"] == "balance":
                    continue
                sensor_value = door_data.get(sensor_type["key"])
                attrs[sensor_type["key"]] = sensor_value

            return attrs
        return {}
