"""
新疆电力燃气集成 (xjelegas) 全局常量定义。

本模块集中管理 Home Assistant 集成中使用的所有常量，避免魔法字符串散落各处，
便于维护和国际化。主要包括以下几类：

1. 域名与版本
   - DOMAIN: 集成在 HA 中的唯一标识符
   - VERSION: 版本号，用于前端资源缓存失效

2. 登录与账号配置键名（CONF_ 前缀表示配置项键名）
   - CONF_PHONE: 燃气账号使用手机号登录
   - CONF_PASSWORD: 登录密码
   - CONF_ACCOUNT: 电力登录手机号（config entry 存储键仍为 account）
   - CONF_UTILITY_TYPE: 公用事业类型（电力/燃气）

3. 计费配置
   - 支持两种模式：阶梯计价 (tiered) 和固定单价 (fixed)
   - 阶梯计价：一档、二档、三档分别对应不同的用量上限和单价
   - 阶梯用量为累计值：二档上限 = 一档上限 + 二档增量

4. 工具函数
   - resolve_utility_type(): 根据配置数据推断当前是电力还是燃气类型
"""

# =============================================================================
# 域名与版本
# =============================================================================

# 集成的域名标识，在 Home Assistant 中必须唯一
# 用于：实体 ID 前缀、配置条目存储、服务调用等
# 注意：必须与 manifest.json 中的 domain 字段完全一致
DOMAIN = "xjelegas"

# 集成版本号，主要用于控制前端资源（如 Lovelace 卡片）的缓存
# 每次修改前端相关代码后建议递增此版本，以强制用户浏览器刷新缓存
VERSION = "1.3.1"

# =============================================================================
# 登录与账号配置键名（CONF_ 前缀表示 config_flow 与 options 中的配置键）
# =============================================================================

# 燃气账号登录字段：使用手机号
# 该字段会用于生成设备唯一 ID，避免多账号配置时实体混淆
CONF_PHONE = "phone"

# 登录密码，燃气和电力均使用（存储时需加密，勿明文保存）
CONF_PASSWORD = "password"

# 公用事业类型配置键：用于区分当前集成实例是电力还是燃气
CONF_UTILITY_TYPE = "utility_type"

# 电力登录手机号（config entry.data 存储键；配置表单字段名为 phone）
CONF_ACCOUNT = "account"

# 公用事业类型枚举值
UTILITY_TYPE_GAS = "gas"   # 燃气
UTILITY_TYPE_ELE = "ele"   # 电力（electricity 缩写）

# 新建配置时的默认类型，设为燃气
DEFAULT_UTILITY_TYPE = UTILITY_TYPE_GAS

# =============================================================================
# 计费配置常量
# =============================================================================

# 计费模式配置键（取值见 PRICING_MODE_TIERED / PRICING_MODE_FIXED）
CONF_PRICING_MODE = "pricing_mode"

# 调试模式配置键（燃气/电力通用，开启后输出 API 数据等调试日志；两模块各自独立，互不影响）
CONF_IS_DEBUG = "is_debug"

# 固定单价模式下的单价（单位：元/立方米 或 元/千瓦时，仅 fixed 模式使用）
CONF_FIXED_PRICE = "fixed_price"

# 阶梯计价模式下的各档参数（单位：立方米/燃气，千瓦时/电力）
CONF_TIER_1_LIMIT = "tier_1_limit"   # 一档用量上限（0 至该值，含）
CONF_TIER_1_PRICE = "tier_1_price"   # 一档单价（元/单位）
CONF_TIER_2_LIMIT = "tier_2_limit"   # 二档用量增量（与一档累加后为二档上限）
CONF_TIER_2_PRICE = "tier_2_price"   # 二档单价（元/单位）
CONF_TIER_3_PRICE = "tier_3_price"   # 三档单价（超出二档部分按此价，无上限）

# 计费模式枚举值
PRICING_MODE_TIERED = "tiered"   # 阶梯计价：用量越高单价越高
PRICING_MODE_FIXED = "fixed"     # 固定单价：无论用量多少单价不变

# 默认计费配置值（参考新疆地区常见燃气阶梯价格，电力可类比调整）
DEFAULT_PRICING_MODE = PRICING_MODE_TIERED
DEFAULT_FIXED_PRICE = 1.50           # 固定模式默认单价：1.50 元/立方米（或元/千瓦时）
DEFAULT_TIER_1_LIMIT = 300           # 一档用量上限：0~300 单位
DEFAULT_TIER_1_PRICE = 1.50          # 一档单价：1.50 元/单位
DEFAULT_TIER_2_LIMIT = 400           # 二档用量增量：二档上限 = 300+400 = 700
DEFAULT_TIER_2_PRICE = 1.80          # 二档单价：1.80 元/单位（301~700 单位）
DEFAULT_TIER_3_PRICE = 2.25          # 三档单价：2.25 元/单位（701 单位以上）


# =============================================================================
# 工具函数
# =============================================================================

def resolve_utility_type(entry_data: dict) -> str:
    """
    根据配置数据解析公用事业类型（电力或燃气）。

    用于在配置迁移、选项更新等场景下，从可能不完整的配置数据中推断出正确的类型。
    燃气账号使用 phone 字段，电力账号使用 account 字段，据此可做特征推断。
    当显式声明的类型与特征字段矛盾时，以特征字段为准，避免脏数据导致误判。

    Args:
        entry_data: 配置数据字典，可能包含 CONF_UTILITY_TYPE、CONF_PHONE、CONF_ACCOUNT 等键

    Returns:
        str: UTILITY_TYPE_ELE ('ele') 表示电力，UTILITY_TYPE_GAS ('gas') 表示燃气

    推断逻辑（按优先级）：
        1. 若显式声明为电力且存在 account，则返回电力
        2. 若显式声明为燃气，则返回燃气
        3. 若存在 phone 字段，推断为燃气
        4. 若存在 account 字段，推断为电力
        5. 无任何特征字段时，默认返回燃气
    """
    # 优先使用显式声明的类型，但需校验其有效性以防脏数据
    utility_type = entry_data.get(CONF_UTILITY_TYPE)

    # 电力类型必须同时拥有 account 字段才有效
    # 防止因 utility_type 被错误写入导致燃气配置被误判为电力
    if utility_type == UTILITY_TYPE_ELE:
        if entry_data.get(CONF_ACCOUNT):
            return UTILITY_TYPE_ELE
        # 声明为电力但无 account，视为无效，继续用特征字段推断

    # 显式声明为燃气，直接返回
    if utility_type == UTILITY_TYPE_GAS:
        return UTILITY_TYPE_GAS

    # utility_type 无效或缺失时，根据特征字段推断
    # 燃气用手机号，电力用手机号（存于 account 键）
    if entry_data.get(CONF_PHONE):
        return UTILITY_TYPE_GAS

    if entry_data.get(CONF_ACCOUNT):
        return UTILITY_TYPE_ELE

    # 无任何特征字段时，默认按燃气处理
    return UTILITY_TYPE_GAS
