"""
传感器平台入口模块。

本模块是 Home Assistant sensor 平台的统一入口，负责根据用户配置的 utility_type（公用事业类型）
将传感器初始化工作分发到对应的子模块：

- 电力 (ele)：调用 ele.sensor.async_setup_entry，创建新疆电力相关传感器（如用电量、余额等）
- 燃气 (gas)：调用 gas.sensor.async_setup_entry，创建新疆燃气相关传感器（如用气量、余额等）

传感器实体由各子模块的 DataUpdateCoordinator 定时刷新数据，实现数据的自动更新。
"""

# =============================================================================
# Home Assistant 核心依赖
# =============================================================================

from homeassistant.config_entries import ConfigEntry  # 配置条目，包含集成配置数据
from homeassistant.core import HomeAssistant  # HA 核心实例
from homeassistant.helpers.entity_platform import AddEntitiesCallback  # 添加实体的异步回调

# =============================================================================
# 本集成内部依赖
# =============================================================================

from .const import UTILITY_TYPE_ELE, resolve_utility_type  # 公用事业类型常量及解析函数
from .ele import sensor as ele_sensor  # 电力传感器子模块（新疆电力）
from .gas import sensor as gas_sensor  # 燃气传感器子模块（新疆燃气）


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    传感器平台的异步设置入口，由 Home Assistant 在集成加载时调用。

    根据配置条目中的 utility_type 判断当前集成的是电力还是燃气，
    并调用对应子模块的 async_setup_entry 完成传感器的创建与注册。

    Args:
        hass: Home Assistant 核心实例，用于访问 HA 服务、调度器等
        config_entry: 当前集成的配置条目，包含 data（用户配置）和 options（选项）等
        async_add_entities: 异步回调函数，用于将创建的传感器实体添加到 HA 中

    Returns:
        None: 无返回值

    Note:
        - 电力类型 (UTILITY_TYPE_ELE) 优先判断，其余均视为燃气类型
        - 各子模块内部会创建 DataUpdateCoordinator 并注册传感器实体
    """
    # 从配置条目的 data 中解析出公用事业类型（电力或燃气）
    # resolve_utility_type 会读取 CONF_UTILITY_TYPE 或根据其他字段推断
    utility_type = resolve_utility_type(config_entry.data)

    # 若为电力类型，则调用电力传感器模块进行初始化并提前返回
    if utility_type == UTILITY_TYPE_ELE:
        await ele_sensor.async_setup_entry(hass, config_entry, async_add_entities)
        return

    # 否则视为燃气类型，调用燃气传感器模块进行初始化
    # 包括：创建 DataUpdateCoordinator、注册传感器实体、设置定时刷新等
    await gas_sensor.async_setup_entry(hass, config_entry, async_add_entities)
