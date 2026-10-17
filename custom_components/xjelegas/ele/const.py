"""
电力 (ele) 子模块常量定义。

本文件定义电力模块专用常量，包括：
- DOMAIN / PACKAGE_NAME：与根 const 保持一致，用于日志与存储
- VERSION / VERSION_STORAGE：电力模块版本及存储结构版本
- 配置键：CONF_PRICE（平均电价）、CONF_IS_PREPAID（预付费类型）
- 存储键：get_store_key()、get_pending_store_key()
- 重试与保留：RETENTION_DAYS、RETENTION_MONTHS
"""

# ---------- 集成标识 ----------
DOMAIN = "xjelegas"  # 集成域，与燃气模块共用
PACKAGE_NAME = "custom_components.xjelegas"  # 包路径，用于日志与存储

# ---------- 版本信息 ----------
VERSION = "2.0.3"  # 电力模块版本（与集成 manifest 同步）
VERSION_STORAGE = 1  # 存储结构版本，用于数据迁移兼容

# ---------- 配置项键名 ----------
CONF_PRICE = "price"  # 平均电价（元/度），用户可选配置
CONF_IS_PREPAID = "is_prepaid"  # 是否预付费：None=自动从 API 判断，True/False=用户手动指定

# ---------- 流控相关错误码 ----------
# 11401 是服务端返回的 RK001 限流码。App 通道（app_api）也用它判断"密码没错但今天到顶了"，
# 以便配置页把"密码错了"和"被限流"分开报。命名对齐 state_grid/const.py。
# 11401 是网关码，RK001 有时直接坐在 code 上，两者都要认。
RATE_LIMIT_CODES = {"11401", "RK001"}

# 站点前端自己把这三个码当"要弹哪种验证码"用（对齐 state_grid/const.py）：
# complexSliderType={'RK1003':'clickImg','RK008':'clickWord','RK007':'blockPuzzle'}
# 它们的原话是"网络连接超时,请重试"，那句是站点对这类码的统一兜底文案，不是网络故障：
# 请求到了、信封也解得开，重试不会让它变成不要，只能由网页上的腾讯组件完成。
CAPTCHA_CODES = {"RK007", "RK008", "RK1003"}

# 服务端说"这份会话已经不算数了"的码。它会**在 expires_at 之前**就作废（上游
# hass-state-grid issue #2：装好第 3 天就没了），所以不能只信本地存的那个时间戳。
SESSION_DEAD_CODES = {"-200", "-201"}

# ---------- 刷新间隔（与 state_grid 一致） ----------
MIN_REFRESH_INTERVAL = 12   # 最小刷新间隔（小时）
MAX_REFRESH_INTERVAL = 48   # 最大刷新间隔（小时）
DEFAULT_REFRESH_INTERVAL = 12
CONF_REFRESH_INTERVAL = "refresh_interval"
CONF_NEW_PASSWORD = "new_password"  # 选项流改密：留空不修改；填写后触发登录验证

# ---------- 网页通道（对齐 state_grid 0.9.10 web_api.py） ----------
# App 通道取不到的格子（如 c04/f03 抄表读数）由这条纯 HTTP 网页通道补齐。
# 三个开关都住在条目选项里，不落 ele store：
#   CONF_WEB_CHANNEL：总开关，关掉完全不发网页请求；
#   CONF_WEB_PRIORITY：True=每格先问网页、App 灌进来的载荷兜底；False=App 优先、网页只补没货的格子；
#   CONF_EMAIL_ACCOUNT：备用登录标识（登记邮箱），主标识吃到 RK001 时由 web_api 顶上。
CONF_WEB_CHANNEL = "web_channel"
CONF_WEB_PRIORITY = "web_priority"
CONF_EMAIL_ACCOUNT = "email_account"
# 默认与 state_grid 0.9.10 一致：网页优先。每格先问网页（真 HTTP），App 通道灌进来的
# 那份兜底；用户在「配置」里可改回 App 优先或整个关掉网页通道。
DEFAULT_WEB_CHANNEL = True
DEFAULT_WEB_PRIORITY = True

# ---------- 数据保留周期（与燃气 storage 一致） ----------
RETENTION_DAYS = 366   # 日数据保留天数（约 1 年）
RETENTION_MONTHS = 24  # 月数据保留月数（24 个月）
# 注：年账单由 API 直接返回，无需本地保留策略


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
