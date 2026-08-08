"""
新疆电力燃气集成 (xjelegas) 主入口模块。

本模块是 Home Assistant 自定义集成的入口，负责：
1. 集成初始化：通过 async_setup 或 async_setup_entry 加载
2. 前端资源注册：Lovelace 卡片静态路径、JS 资源、仪表板资源
3. 配置条目生命周期：setup -> update_options -> unload
4. 电力/燃气客户端管理：根据配置类型创建 StateGridDataClient 或燃气传感器

支持的公用事业类型：
- 电力 (ele)：新疆电力，使用国密算法 (SM2/SM3/SM4) 加密
- 燃气 (gas)：新疆燃气，使用 RSA+AES 混合加密

依赖：Home Assistant 2024.x+，manifest.json 中声明的依赖包
"""
# ========== 标准库 ==========
import logging  # 日志记录，用于 _LOGGER 输出调试/信息/警告
from typing import Any  # 类型注解，用于泛型或动态类型参数

# ========== Home Assistant ==========
# Home Assistant 配置条目相关
from homeassistant.config_entries import ConfigEntry
# 核心实例、运行状态、启动完成事件
from homeassistant.core import HomeAssistant, CoreState, EVENT_HOMEASSISTANT_STARTED
# 实体注册表，用于管理实体的启用/禁用状态
from homeassistant.helpers import entity_registry as er
# 延迟调用，用于在指定秒数后执行回调
from homeassistant.helpers.event import async_call_later
# HTTP 静态路径配置，用于注册前端静态文件服务
from homeassistant.components.http import StaticPathConfig

# 引入常量定义，确保与 const.py 中的域名、版本、配置键等保持一致
from .const import (
    DOMAIN,              # 集成域名，用于 hass.data 存储和配置条目标识
    VERSION,             # 集成版本号，用于资源 URL 缓存破坏
    CONF_PRICING_MODE,   # 配置键：电价/气价模式（阶梯/固定）
    DEFAULT_PRICING_MODE,# 默认价格模式
    PRICING_MODE_FIXED,  # 固定价格模式常量
    PRICING_MODE_TIERED, # 阶梯计价模式常量
    UTILITY_TYPE_ELE,    # 公用事业类型：电力
    UTILITY_TYPE_GAS,    # 公用事业类型：燃气
    CONF_ACCOUNT,        # 配置键：账号
    CONF_PASSWORD,       # 配置键：登录密码（旧版 entry 可能仍存于此）
    resolve_utility_type,# 根据配置数据解析公用事业类型的函数
)

# 模块级日志器，用于记录集成运行时的调试、信息、警告信息
_LOGGER = logging.getLogger(__name__)

# 定义集成支持的平台列表
# 目前仅支持 sensor (传感器) 平台，用于展示电费/气费、用量等数据
# 后续如需扩展（如 binary_sensor、switch 等）可在此追加
PLATFORMS: list[str] = ["sensor"]

# Lovelace 卡片前端资源相关常量
CARD_URL_BASE = "/xjelegas"
CARD_FILENAME = "xjelegas-card.js"
CARD_RESOURCE_URL = f"{CARD_URL_BASE}/{CARD_FILENAME}?v={VERSION}"


async def async_setup(hass: HomeAssistant, config: dict):
    """
    通过 configuration.yaml 设置集成时调用。
    
    主要用途：注册前端卡片资源。推荐通过 Config Flow (UI) 添加集成。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        config (dict): 全局配置字典。
        
    Returns:
        bool: 设置是否成功。此处始终返回 True，表示允许通过 UI 继续配置。
    """
    # 内部函数：注册前端卡片资源（静态路径 + Lovelace 资源）
    async def _register_frontend(_event=None) -> None:
        await _setup_xjelegas_card(hass)

    # 前端资源注册必须在 HA 启动完成后执行
    # 若 HA 已处于运行状态，则立即注册；否则监听启动完成事件后再注册
    if hass.state == CoreState.running:
        await _register_frontend()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_frontend)
    return True


async def _setup_xjelegas_card(hass: HomeAssistant, include_lovelace: bool = True) -> bool:
    """
    设置新疆电力燃气卡片前端资源。
    
    完成两项工作：
    1. 注册静态路径：将 URL 路径 /xjelegas 映射到 www 目录，使前端可加载卡片 JS
    2. Lovelace 资源注册：将卡片注册到 Lovelace 资源列表，URL 带版本号 ?v=VERSION 解决缓存，
       用户无需修改 configuration.yaml 或 resources 配置，集成自动完成注册（不区分 storage/yaml 模式）
    
    Args:
        hass: Home Assistant 核心实例
        include_lovelace: 是否注册 Lovelace 资源。True 时自动注册到 Lovelace 资源列表（带版本号）
    
    Returns:
        bool: 始终返回 True，表示设置流程完成（即使部分步骤跳过）
    """
    # 1. 注册静态路径：CARD_URL_BASE 对应的请求将从此目录提供文件
    www_dir = hass.config.path("custom_components/xjelegas/www")
    try:
        await hass.http.async_register_static_paths([
            StaticPathConfig(CARD_URL_BASE, www_dir, False)
        ])
        _LOGGER.debug("注册静态路径: %s -> %s", CARD_URL_BASE, www_dir)
    except RuntimeError:
        _LOGGER.debug("静态路径 %s 已注册", CARD_URL_BASE)

    # 2. 前端资源采用 Lovelace 资源注册方式，不预加载（add_extra_js_url 已移除）
    if not include_lovelace:
        return True

    # 3. Lovelace 资源注册（不区分 storage/yaml 模式，统一注册）
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.debug("Lovelace 未加载，跳过资源注册")
        return True

    # 将卡片 URL 注册到 Lovelace 资源列表
    # 若资源尚未加载，则 5 秒后重试
    async def _register_resource(_now: Any) -> None:
        if not getattr(lovelace.resources, "loaded", False):
            _LOGGER.debug("Lovelace 资源未加载，5 秒后重试")
            async_call_later(hass, 5, _register_resource)
            return

        # 判断资源项是否为 xjelegas 卡片（仅匹配 /xjelegas 路径）
        def _is_xjelegas(r: dict) -> bool:
            u = r.get("url", "")
            return u.startswith(f"{CARD_URL_BASE}/{CARD_FILENAME}")

        existing = [r for r in lovelace.resources.async_items() if _is_xjelegas(r)]
        if existing:
            for r in existing:
                if r.get("url") != CARD_RESOURCE_URL:
                    try:
                        await lovelace.resources.async_update_item(
                            r["id"], {"res_type": "module", "url": CARD_RESOURCE_URL}
                        )
                        _LOGGER.info("已更新 xjelegas 卡片资源至 %s", CARD_RESOURCE_URL)
                    except Exception as e:
                        _LOGGER.warning("更新卡片资源失败: %s", e)
            return

        try:
            await lovelace.resources.async_create_item(
                {"res_type": "module", "url": CARD_RESOURCE_URL}
            )
            _LOGGER.info("已在 Lovelace 资源中注册 xjelegas 卡片")
        except Exception as e:
            _LOGGER.warning("注册卡片资源失败: %s", e)

    # 立即尝试注册（传入 0 作为占位参数，_register_resource 的 _now 参数在立即调用时未使用）
    await _register_resource(0)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """
    通过 Config Entry (UI 配置条目) 设置集成。
    
    当用户在 UI 中添加集成，或 Home Assistant 启动并加载已保存的配置时，此函数被调用。
    根据配置的公用事业类型（电力/燃气），执行不同的初始化逻辑。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        entry (ConfigEntry): 当前的配置条目实例，包含用户输入的配置数据。
        
    Returns:
        bool: 设置是否成功。
    """
    # 添加前端卡片资源：静态路径 + Lovelace 资源注册（带版本号，无需用户修改 yaml）
    await _setup_xjelegas_card(hass, include_lovelace=True)
    _LOGGER.info("xjelegas v%s setup entry %s", VERSION, entry.entry_id)

    # 根据 entry.data 解析公用事业类型：电力(ele) 或 燃气(gas)
    utility_type = resolve_utility_type(entry.data)
    if utility_type == UTILITY_TYPE_ELE:
        # ========== 电力类型：新疆电力 ==========
        from .ele.const import get_store_key, get_pending_store_key
        from .ele.storage import async_load_from_store, async_remove_store, async_save_to_store
        from .ele.api import StateGridDataClient

        # 获取与当前 entry_id 对应的存储键，用于读写 .storage 中的持久化数据
        store_key = get_store_key(entry.entry_id)
        
        # 从 .storage 加载配置（而非 config_entry.data）
        # 原因：StateGridDataClient 将 token、登录状态等保存在 .storage 中，config_entry 仅存基础配置
        config = await async_load_from_store(hass, store_key)
        
        # 迁移逻辑 1：若主存储无数据且 entry 含账号，尝试从 pending 存储迁移
        # pending 存储用于 Config Flow 未完成时的临时数据
        # 注：async_load_from_store 在存储不存在时返回 {}，需同时判断 not config
        if (config is None or not config) and entry.data.get(CONF_ACCOUNT):
            pending_key = get_pending_store_key(entry.data[CONF_ACCOUNT])
            config = await async_load_from_store(hass, pending_key)
            if config:
                await async_save_to_store(hass, store_key, config)
                await async_remove_store(hass, pending_key)
        
        # 合并配置选项：entry.options 中的固定电价等覆盖存储中的值
        # 用户可在集成选项中修改电价，优先于 .storage 中的历史值
        from .ele.const import CONF_PRICE, CONF_ARK_API_KEY, CONF_ARK_MODEL, CONF_ARK_BASE_URL, CONF_EMAIL_ACCOUNT, CONF_REFRESH_INTERVAL, CONF_IS_PREPAID, MIN_REFRESH_INTERVAL, MAX_REFRESH_INTERVAL
        options = entry.options or {}
        if options.get(CONF_PRICE) is not None:
            config = config or {}
            config[CONF_PRICE] = float(options[CONF_PRICE])
        elif options.get("average_price") is not None:
            config = config or {}
            config[CONF_PRICE] = float(options["average_price"])

        # 读取大模型 API Key 配置
        if options.get(CONF_ARK_API_KEY) is not None:
            config = config or {}
            config[CONF_ARK_API_KEY] = options[CONF_ARK_API_KEY]

        # 读取大模型名称配置
        if options.get(CONF_ARK_MODEL) is not None:
            config = config or {}
            config[CONF_ARK_MODEL] = options[CONF_ARK_MODEL]

        # 读取大模型 API 接入地址配置
        if options.get(CONF_ARK_BASE_URL) is not None:
            config = config or {}
            config[CONF_ARK_BASE_URL] = options[CONF_ARK_BASE_URL]

        # 读取备用邮箱（RK001 流控降级）
        if options.get(CONF_EMAIL_ACCOUNT) is not None:
            config = config or {}
            config[CONF_EMAIL_ACCOUNT] = options[CONF_EMAIL_ACCOUNT]

        # 读取刷新间隔（与 state_grid 一致，clamp 到 12-48 小时）
        if options.get(CONF_REFRESH_INTERVAL) is not None:
            config = config or {}
            try:
                hours = int(options[CONF_REFRESH_INTERVAL])
                config[CONF_REFRESH_INTERVAL] = max(MIN_REFRESH_INTERVAL, min(MAX_REFRESH_INTERVAL, hours))
            except (ValueError, TypeError):
                pass

        # 预付费类型：auto→None 自动检测，true/false→布尔值
        raw_prepaid = options.get(CONF_IS_PREPAID)
        if raw_prepaid is not None:
            config = config or {}
            if raw_prepaid == "auto":
                config[CONF_IS_PREPAID] = None
            elif raw_prepaid == "true":
                config[CONF_IS_PREPAID] = True
            else:
                config[CONF_IS_PREPAID] = False

        # 旧版 entry 可能将密码保存在 data/options，合并到 storage 供自动重登
        if config is not None:
            if not config.get("password"):
                legacy_password = options.get(CONF_PASSWORD) or entry.data.get(CONF_PASSWORD)
                if legacy_password:
                    config["password"] = legacy_password

        # 确保 hass.data[DOMAIN] 存在，用于存储各 entry 的客户端实例
        hass.data.setdefault(DOMAIN, {})
        # 创建或更新电力数据客户端，按 entry_id 存储
        # 每次 setup 都重新创建实例，确保配置变更生效
        data_client = StateGridDataClient(
            hass=hass,
            config=config,       # 合并后的完整配置（含 token、电价等）
            store_key=store_key, # 客户端内部用于持久化 token、登录状态等
        )
        
        hass.data[DOMAIN][entry.entry_id] = data_client
    # 燃气类型：无需在此创建客户端，由 gas/sensor.py 中的平台 setup 处理

    # 将配置条目转发给各平台（sensor 等），由平台创建实体
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 注册选项更新监听器：用户修改集成选项时调用 async_update_options
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    配置条目版本迁移（v1 → v2）。

    v2 将电力登录表单改为手机号 + 备用邮箱 + LLM 配置，选项键统一为 email_account。
    """
    if entry.version >= 2:
        return True

    _LOGGER.info("Migrating xjelegas entry %s from version %s to 2", entry.entry_id, entry.version)

    from .ele.const import CONF_EMAIL_ACCOUNT
    new_options = dict(entry.options or {})
    if new_options.get("email") and not new_options.get(CONF_EMAIL_ACCOUNT):
        new_options[CONF_EMAIL_ACCOUNT] = new_options.pop("email")

    hass.config_entries.async_update_entry(entry, version=2, options=new_options)
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    """
    当用户在集成选项中修改配置时调用。
    
    电力类型：直接重载配置条目即可，因为 StateGridDataClient 会重新读取 options。
    燃气类型：需先更新「当前阶梯」实体在注册表中的 disabled_by，再重载。
             否则实体被禁用时不会随 reload 重新添加，导致切换为阶梯后无法启用。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        entry (ConfigEntry): 配置条目，entry.options 包含用户修改后的选项。
    """
    if resolve_utility_type(entry.data) == UTILITY_TYPE_GAS:
        options = entry.options or {}
        pricing_mode = options.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
        disabled_by = er.RegistryEntryDisabler.INTEGRATION if pricing_mode == PRICING_MODE_FIXED else None
        registry = er.async_get(hass)
        for entity in registry.entities.values():
            if entity.config_entry_id != entry.entry_id:
                continue
            if not entity.unique_id or not str(entity.unique_id).endswith("_current_tier"):
                continue
            if entity.disabled_by != disabled_by:
                registry.async_update_entity(entity.entity_id, disabled_by=disabled_by)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """
    卸载配置条目。
    
    当用户移除集成、禁用集成或修改配置导致重新加载时调用。
    负责：1) 卸载各平台（sensor 等）及其实体、协调器；2) 清理电力客户端实例。
    
    Args:
        hass (HomeAssistant): Home Assistant 核心实例。
        entry (ConfigEntry): 要卸载的配置条目。
        
    Returns:
        bool: 卸载是否成功。若所有平台均成功卸载则返回 True，否则 False。
    """
    # 卸载 PLATFORMS 中声明的所有平台（sensor），释放实体、协调器、定时器等资源
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # 电力类型额外清理：从 hass.data 中移除 StateGridDataClient 实例，避免内存泄漏
    if unload_ok and resolve_utility_type(entry.data) == UTILITY_TYPE_ELE:
        if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
