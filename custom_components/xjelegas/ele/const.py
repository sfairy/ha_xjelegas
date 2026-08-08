"""
电力 (ele) 子模块常量定义。

本文件定义电力模块专用常量，包括：
- DOMAIN / PACKAGE_NAME：与根 const 保持一致，用于日志与存储
- VERSION / VERSION_STORAGE：电力模块版本及存储结构版本
- 配置键：CONF_PRICE（平均电价）、CONF_IS_PREPAID（预付费类型）
- 存储键：get_store_key()、get_pending_store_key()
- 重试与保留：MAX_RETRIES、RETENTION_DAYS、RETENTION_MONTHS
- 大模型配置：ARK_API_KEY 环境变量名
"""

# ---------- 集成标识 ----------
DOMAIN = "xjelegas"  # 集成域，与燃气模块共用
PACKAGE_NAME = "custom_components.xjelegas"  # 包路径，用于日志与存储

# ---------- 版本信息 ----------
VERSION = "1.3.1"  # 电力模块版本（与集成 manifest 同步）
VERSION_STORAGE = 1  # 存储结构版本，用于数据迁移兼容

# ---------- 配置项键名 ----------
CONF_PRICE = "price"  # 平均电价（元/度），用户可选配置
CONF_IS_PREPAID = "is_prepaid"  # 是否预付费：None=自动从 API 判断，True/False=用户手动指定
CONF_ARK_API_KEY = "ark_api_key"  # 火山引擎豆包大模型 API Key，用于滑块验证码识别
CONF_EMAIL_ACCOUNT = "email_account"  # 备用邮箱（RK001 流控降级登录用）

# ---------- 流控相关错误码 ----------
# 11401 = RK001 限流（密码登录日额度用完）
FLOW_CONTROL_CODES = {11401}

# ---------- 刷新间隔（与 state_grid 一致） ----------
MIN_REFRESH_INTERVAL = 12   # 最小刷新间隔（小时）
MAX_REFRESH_INTERVAL = 48   # 最大刷新间隔（小时）
DEFAULT_REFRESH_INTERVAL = 12
CONF_REFRESH_INTERVAL = "refresh_interval"
CONF_NEW_PASSWORD = "new_password"  # 选项流改密：留空不修改；填写后触发登录验证

# ---------- API 与重试 ----------
MAX_RETRIES = 3  # API 请求失败时的最大重试次数

# ---------- 数据保留周期（与燃气 storage 一致） ----------
RETENTION_DAYS = 366   # 日数据保留天数（约 1 年）
RETENTION_MONTHS = 24  # 月数据保留月数（24 个月）
# 注：年账单由 API 直接返回，无需本地保留策略

# ---------- 大模型验证码配置 ----------
ARK_API_KEY_ENV = "ARK_API_KEY"  # 火山引擎 API Key 环境变量名
CONF_ARK_MODEL = "ark_model"  # 大模型名称
CONF_ARK_BASE_URL = "ark_base_url"  # 大模型 API 接入地址
DEFAULT_ARK_MODEL = "doubao-seed-2-0-pro-260215"
DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def get_store_key(entry_id: str) -> str:
    """
    根据 ConfigEntry ID 生成持久化存储键。

    用于 Home Assistant 存储中保存该配置条目对应的电力数据。

    Args:
        entry_id: 配置条目 ID（ConfigEntry.entry_id）。

    Returns:
        存储键名，格式为 "xjelegas_ele_{entry_id}"。
    """
    return f"xjelegas_ele_{entry_id}"


def get_pending_store_key(account: str) -> str:
    """
    根据账号生成临时存储键。

    用于配置流中暂存未完成配置的登录信息，待用户完成配置后迁移至正式存储。

    Args:
        account: 用户账号（手机号等），可为空或 None。

    Returns:
        临时存储键名，格式为 "xjelegas_ele_pending_{account}"。
    """
    account_key = (account or "").strip()
    return f"xjelegas_ele_pending_{account_key}"
