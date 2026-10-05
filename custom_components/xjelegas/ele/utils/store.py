"""
电力模块数据持久化存储工具（对齐 state_grid/utils/store.py）。

基于 Home Assistant Store 的 JSON 文件存储，用于持久化新疆电力客户端状态：
- 账号与密码摘要（App 通道取数所需）
- powerUserList / doorAccountDict（户号及余额、账单等解析结果）
- 配置项：refresh_interval、price、is_prepaid、is_debug、timestamp

存储键格式：xjelegas_ele_{entry_id}，由 ele.const.get_store_key 生成。
版本校验：StateGridStore 在加载时检查 version，不匹配则返回 None 触发重新初始化；
        同时覆盖 _async_migrate_func，避免版本不匹配时 async_save 抛 NotImplementedError。
"""
# ========== 标准库 ==========
import logging

# ========== Home Assistant ==========
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import Store
from homeassistant.util import json as json_util

# ========== 本模块常量 ==========
from ..const import VERSION_STORAGE, PACKAGE_NAME
from .logger import LOGGER

_LOGGER = LOGGER


class StateGridStore(Store):
    """
    新疆电力存储类。

    继承 Home Assistant Store，重写 load 以支持在 executor 中多次加载，
    并增加版本校验（版本不符时返回 None，触发重新初始化）。
    """

    def load(self):
        """
        从磁盘加载 JSON 数据。

        从 self.path 读取 JSON 文件并解析，校验 version 字段与当前存储版本是否一致。
        版本不匹配、数据为空或解析异常时返回 None，调用方需处理空值并触发重新初始化。

        Returns:
            dict | None: 成功时返回 data 字段内容，失败时返回 None
        """
        try:
            loaded_data = json_util.load_json(self.path)
        except (
            BaseException  # lgtm [py/catch-base-exception] pylint: disable=broad-except
        ) as exception:
            _LOGGER.critical(
                "无法加载 '%s'，请从备份恢复或删除该文件: %s",
                self.path,
                exception,
            )
            return None
        # 空数据或版本不匹配时返回 None，触发调用方重新初始化
        if not loaded_data or loaded_data.get("version") != self.version:
            return None
        return loaded_data["data"]

    async def _async_migrate_func(self, old_version, old_minor_version, old_data):
        """版本迁移：存储版本不匹配时直接返回旧数据（不丢数据）。

        HA Store 在发现存储文件版本号与当前 VERSION_STORAGE 不一致时会调用此方法。
        如果不覆盖，默认 raise NotImplementedError，导致 save_data() 崩溃，
        整个刷新流程被异常中断。
        """
        _LOGGER.warning(
            "存储版本迁移: %s.%s -> %s (数据保留不变)",
            old_version, old_minor_version, self.version,
        )
        # 不做任何数据结构变换，直接返回旧数据
        # 新版本的字段会在下次 save_data() 时补齐
        return old_data


def _get_store_for_key(hass, key, encoder):
    """
    为指定键创建 Store 实例（内部函数）。

    Args:
        hass: Home Assistant 核心实例
        key: 存储键名，格式为 xjelegas_ele_{entry_id}
        encoder: JSON 编码器，用于序列化 datetime 等特殊类型

    Returns:
        StateGridStore: 配置了原子写入的存储实例
    """
    return StateGridStore(hass, VERSION_STORAGE, key, encoder=encoder, atomic_writes=True)


def get_store_for_key(hass, key):
    """
    获取指定键的 Store 实例（使用默认 JSONEncoder）。

    Args:
        hass: Home Assistant 核心实例
        key: 存储键名

    Returns:
        StateGridStore: 存储实例
    """
    return _get_store_for_key(hass, key, JSONEncoder)


async def async_load_from_store(hass, key):
    """
    从存储异步加载数据。

    Args:
        hass: Home Assistant 核心实例
        key: 存储键名

    Returns:
        dict: 加载的数据，不存在或异常时返回空字典 {}
    """
    return await get_store_for_key(hass, key).async_load() or {}


async def async_save_to_store(hass, key, data):
    """
    异步保存数据到存储。

    仅当内容发生变化时才写入磁盘，避免不必要的 I/O。
    先加载 stored_data 与当前 data 比较，有变化才写入。

    Args:
        hass: Home Assistant 核心实例
        key: 存储键名
        data: 要保存的字典数据
    """
    try:
        stored_data = await async_load_from_store(hass, key)
    except Exception as ex:
        _LOGGER.warning("读取存储文件失败，将覆盖保存: %s", ex)
        stored_data = None
    if stored_data is None or stored_data != data:
        await get_store_for_key(hass, key).async_save(data)


async def async_remove_store(hass, key):
    """
    移除指定键的存储文件。

    用于配置条目删除时清理本地持久化数据。

    Args:
        hass: Home Assistant 核心实例
        key: 存储键名
    """
    await get_store_for_key(hass, key).async_remove()
