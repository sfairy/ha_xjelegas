"""
新疆电力燃气集成 - 配置流与选项流模块。

本模块实现 Home Assistant 的 Config Flow 和 Options Flow，负责集成添加与配置：

1. 配置流 (Config Flow) - 首次添加集成时的向导式配置：
   - 第一步：选择接入类型（电力 / 燃气）
   - 电力：输入手机号、备用邮箱、LLM Key 与密码，调用国网 API 校验登录
   - 燃气：输入手机号与密码，调用新疆燃气 API 校验登录，创建唯一配置条目
   - 使用「类型-账号」作为 unique_id，避免重复添加同一账号

2. 选项流 (Options Flow) - 用户点击「配置」后的进阶设置：
   - 电力：刷新间隔、调试模式、平均电价、预付费类型（auto/true/false）
   - 燃气：计费模式（阶梯计价/固定单价）、阶梯参数或固定单价

错误码与翻译：missing_fields、invalid_auth、cannot_connect、client_not_found 等
对应 translations/zh-Hans.json 中的文案，用于表单错误提示。
"""

# ========== 标准库与第三方库 ==========
import logging          # 日志记录
import traceback        # 异常堆栈跟踪，用于调试时输出完整错误信息
import voluptuous as vol  # 数据模式校验，用于表单字段验证

# ========== Home Assistant 核心 ==========
from homeassistant import config_entries  # 配置条目与配置流基类
import homeassistant.helpers.config_validation as cv  # 配置验证辅助函数（如 cv.string、cv.boolean）

from homeassistant.helpers import selector  # 下拉框、数字选择器等 UI 控件

# ========== 本集成常量与配置键 ==========
from .const import (
    DOMAIN, CONF_PHONE, CONF_PASSWORD,
    CONF_UTILITY_TYPE, CONF_ACCOUNT,
    UTILITY_TYPE_GAS, UTILITY_TYPE_ELE, DEFAULT_UTILITY_TYPE,
    CONF_PRICING_MODE, CONF_FIXED_PRICE, CONF_IS_DEBUG,
    CONF_TIER_1_LIMIT, CONF_TIER_1_PRICE,
    CONF_TIER_2_LIMIT, CONF_TIER_2_PRICE,
    CONF_TIER_3_PRICE,
    PRICING_MODE_TIERED, PRICING_MODE_FIXED,
    DEFAULT_PRICING_MODE, DEFAULT_FIXED_PRICE,
    DEFAULT_TIER_1_LIMIT, DEFAULT_TIER_1_PRICE,
    DEFAULT_TIER_2_LIMIT, DEFAULT_TIER_2_PRICE,
    DEFAULT_TIER_3_PRICE,
    resolve_utility_type,
    VERSION as INTEGRATION_VERSION,
)
from .ele.const import (
    CONF_IS_PREPAID, CONF_PRICE, CONF_ARK_API_KEY, CONF_ARK_MODEL, CONF_ARK_BASE_URL,
    CONF_EMAIL_ACCOUNT, CONF_REFRESH_INTERVAL, CONF_NEW_PASSWORD,
    DEFAULT_ARK_MODEL, DEFAULT_ARK_BASE_URL,
    MIN_REFRESH_INTERVAL, MAX_REFRESH_INTERVAL,
)

# 模块级日志器，用于记录配置流程中的错误与调试信息
_LOGGER = logging.getLogger(__name__)


def _text_input(input_type: selector.TextSelectorType = selector.TextSelectorType.TEXT):
    """构建 HA TextSelector（勿将 selector 模块当作函数调用）。"""
    return selector.TextSelector(selector.TextSelectorConfig(type=input_type))


# =============================================================================
# 配置流处理器：负责首次添加集成时的向导式配置
# =============================================================================
class XjGasFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """
    处理配置流，创建唯一配置条目。
    
    引导用户进行初始化配置，包括选择接入类型（电力/燃气）和输入账号信息。
    """
    VERSION = 2  # 升级版本以刷新配置表单（手机号+备用邮箱双账号）

    async def async_step_user(self, user_input=None):
        """
        第一步：选择接入类型。
        
        Args:
            user_input (dict, optional): 用户提交的数据。
            
        Returns:
            FlowResult: 下一步流程或表单。
        """
        if user_input is not None:
            # 用户已选择接入类型，保存到实例变量并跳转到对应步骤
            self._utility_type = user_input.get(CONF_UTILITY_TYPE, DEFAULT_UTILITY_TYPE)
            if self._utility_type == UTILITY_TYPE_ELE:
                return await self.async_step_ele()   # 电力：跳转到电力登录表单
            return await self.async_step_gas()      # 燃气：跳转到燃气登录表单

        # 首次进入：定义接入类型选择表单的架构（电力 / 燃气二选一）
        data_schema = vol.Schema({
            vol.Required(CONF_UTILITY_TYPE, default=DEFAULT_UTILITY_TYPE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=UTILITY_TYPE_ELE, label="电力"),
                        selector.SelectOptionDict(value=UTILITY_TYPE_GAS, label="燃气"),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        })

        # 显示表单，step_id 用于标识当前步骤，便于流程跳转与错误回显
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
        )

    async def async_step_gas(self, user_input=None):
        """
        处理燃气登录步骤。
        
        Args:
            user_input (dict, optional): 用户提交的账号密码。
            
        Returns:
            FlowResult: 完成配置或显示错误。
        """
        errors = {}  # 表单错误字典，key 为 "base" 时显示在表单顶部，对应 translations 中的错误码

        if user_input is not None:
            # 读取用户输入的手机号与密码，用于登录校验
            phone = user_input.get(CONF_PHONE)
            password = user_input.get(CONF_PASSWORD)

            if not phone or not password:
                # 必填项为空，触发翻译文件中的 "missing_fields" 提示
                errors["base"] = "missing_fields"
            else:
                try:
                    # 登录校验放在后台线程执行（API 可能为同步阻塞），避免阻塞主事件循环
                    from .gas.api import XjGasAPI
                    api = XjGasAPI(phone=phone, password=password)

                    success = await self.hass.async_add_executor_job(api.login)

                    if success:
                        # 使用 "燃气-手机号" 作为唯一标识，避免重复添加相同账号
                        await self.async_set_unique_id(f"{UTILITY_TYPE_GAS}-{phone}")
                        self._abort_if_unique_id_configured()  # 若已存在则中止并提示

                        # 创建配置条目，data 会写入 config_entry.data
                        # 注意：密码需持久化用于 Token 过期后的自动重新登录，
                        # HA 的 config_entry.data 以 JSON 明文存储于 .storage 目录，
                        # 这是 HA 平台的已知限制。请确保 HA 配置目录访问权限已妥善管控。
                        return self.async_create_entry(
                            title=f"新疆燃气 ({phone})",
                            data={
                                CONF_UTILITY_TYPE: UTILITY_TYPE_GAS,
                                CONF_PHONE: phone,
                                CONF_PASSWORD: password,
                            }
                        )
                    else:
                        # 登录失败（账号或密码错误），对应翻译键 invalid_auth
                        errors["base"] = "invalid_auth"

                except Exception as e:
                    # 网络异常、API 异常等，统一显示无法连接（对应翻译键 cannot_connect）
                    _LOGGER.error("燃气登录参数校验失败: %s", e)
                    errors["base"] = "cannot_connect"

        # 显示燃气登录表单（首次进入或校验失败时，errors 非空则显示错误提示）
        return self.async_show_form(
            step_id="gas",
            data_schema=vol.Schema({
                vol.Required(CONF_PHONE): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            }),
            errors=errors,
        )

    async def async_step_ele(self, user_input=None):
        """
        处理电力登录步骤。
        
        Args:
            user_input (dict, optional): 用户提交的账号密码和大模型 API Key。
            
        Returns:
            FlowResult: 完成配置或显示错误。
        """
        errors = {}
        phone = ""
        password = ""
        email = ""
        ark_base_url = DEFAULT_ARK_BASE_URL
        ark_model = DEFAULT_ARK_MODEL
        ark_api_key = ""

        if user_input is None:
            user_input = {}
        else:
            phone = user_input.get("phone", "").strip()
            password = user_input.get(CONF_PASSWORD, "")
            email = user_input.get("email", "").strip()
            ark_base_url = (user_input.get(CONF_ARK_BASE_URL) or DEFAULT_ARK_BASE_URL).strip()
            ark_model = (user_input.get(CONF_ARK_MODEL) or DEFAULT_ARK_MODEL).strip()
            ark_api_key = user_input.get(CONF_ARK_API_KEY, "").strip()

            if not phone or not password:
                errors["base"] = "missing_fields"
            elif not phone.isdigit():
                errors["base"] = "invalid_phone"
            elif email and "@" not in email:
                errors["base"] = "invalid_email"
            elif not ark_api_key:
                errors["base"] = "missing_llm_key"
            else:
                try:
                    from .ele.api import StateGridDataClient
                    from .ele.const import get_pending_store_key
                    from .ele import captcha_solver as ele_captcha_solver

                    pending_key = get_pending_store_key(phone)
                    config = {
                        CONF_ARK_BASE_URL: ark_base_url,
                        CONF_ARK_MODEL: ark_model,
                        CONF_ARK_API_KEY: ark_api_key,
                        CONF_EMAIL_ACCOUNT: email,
                    }
                    data_client = StateGridDataClient(hass=self.hass, store_key=pending_key, config=config)
                    data_client.email_account = email
                    ele_captcha_solver.configure_llm(ark_api_key, ark_base_url, ark_model)

                    result = await data_client.password_login(phone, password)

                    if result.get("errcode") != 0 and email and data_client._looks_like_rk001_message(result):
                        _LOGGER.info("[配置流程] 手机号遇RK001流控，自动降级到邮箱登录: %s", email)
                        result = await data_client.password_login(email, password, encode=False, max_retry=2)

                    if result.get("errcode") == 0:
                        await self.async_set_unique_id(f"{UTILITY_TYPE_ELE}-{phone}")
                        self._abort_if_unique_id_configured()

                        await data_client.save_data()
                        return self.async_create_entry(
                            title=f"新疆电力 ({phone})",
                            data={
                                CONF_UTILITY_TYPE: UTILITY_TYPE_ELE,
                                CONF_ACCOUNT: phone,
                            },
                            options={
                                CONF_ARK_BASE_URL: ark_base_url,
                                CONF_ARK_MODEL: ark_model,
                                CONF_ARK_API_KEY: ark_api_key,
                                CONF_EMAIL_ACCOUNT: email,
                            }
                        )

                    errmsg = result.get("errmsg") or "登录失败，请检查账号密码或LLM配置"
                    _LOGGER.warning("电力登录失败: %s", errmsg)
                    if data_client._looks_like_rk001_message(result):
                        errors["base"] = "rk001_rate_limit"
                    else:
                        errors["base"] = "invalid_auth"
                except Exception as e:
                    _LOGGER.error("电力登录失败: %s", e)
                    _LOGGER.debug("电力登录异常详情: %s", traceback.format_exc())
                    errors["base"] = "cannot_connect"

        data_schema = vol.Schema({
            vol.Required("phone", default=phone): _text_input(selector.TextSelectorType.TEL),
            vol.Optional("email", default=email): _text_input(selector.TextSelectorType.EMAIL),
            vol.Required(CONF_PASSWORD, default=password): _text_input(selector.TextSelectorType.PASSWORD),
            vol.Required(CONF_ARK_API_KEY, default=ark_api_key): _text_input(selector.TextSelectorType.PASSWORD),
            vol.Optional(CONF_ARK_BASE_URL, default=ark_base_url): _text_input(),
            vol.Optional(CONF_ARK_MODEL, default=ark_model): _text_input(),
        })

        _LOGGER.debug(
            "显示电力登录表单 (config_flow v%s, integration v%s)",
            XjGasFlowHandler.VERSION,
            INTEGRATION_VERSION,
        )

        return self.async_show_form(
            step_id="ele",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """
        获取选项流处理器（静态方法，由 HA 框架调用）。

        Home Assistant 在用户点击集成条目的「配置」按钮时调用此方法，
        根据配置条目的 utility_type 返回对应的选项流处理器：
        - 电力：EleOptionsFlowHandler（刷新间隔、调试、电价、预付费类型）
        - 燃气：XjGasOptionsFlowHandler（计费模式、阶梯/固定价格）
        """
        utility_type = resolve_utility_type(config_entry.data)
        if utility_type == UTILITY_TYPE_ELE:
            return EleOptionsFlowHandler(config_entry)
        return XjGasOptionsFlowHandler(config_entry)


# =============================================================================
# 燃气选项流：配置计费模式与价格参数
# =============================================================================
class XjGasOptionsFlowHandler(config_entries.OptionsFlow):
    """
    燃气选项流处理器。
    
    允许用户配置计费模式（阶梯计价/固定单价）和具体价格。
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """初始化燃气选项流，保存配置条目引用和当前选项副本。"""
        self._config_entry = config_entry
        self._options = dict(config_entry.options or {})  # 选项副本，修改后通过 async_create_entry 写回 HA
        self._pricing_mode = None  # 用户选择的计费模式（阶梯/固定），用于流程分支跳转

    @property
    def config_entry(self):
        """覆盖 config_entry 属性，供 Home Assistant 内部逻辑或子类访问。"""
        return self._config_entry

    async def async_step_init(self, user_input=None):
        """
        初始化选项流步骤（燃气选项流入口）。

        显示菜单供用户选择要配置的项目，目前仅「计费」一项，
        后续可扩展其他选项（如通知阈值等）。
        """
        return self.async_show_menu(
            step_id="init",
            menu_options=["gas_debug", "pricing"]  # 调试设置 | 计费配置
        )

    async def async_step_gas_debug(self, user_input=None):
        """
        配置燃气调试模式。
        
        允许用户开启 is_debug，输出 API 数据更新等调试日志，便于排查问题。
        注：燃气与电力的调试开关各自独立，互不影响。
        """
        if user_input is not None:
            data = dict(self._options)
            data[CONF_IS_DEBUG] = user_input[CONF_IS_DEBUG]
            return self.async_create_entry(title="", data=data)

        is_debug = self._options.get(CONF_IS_DEBUG, False)
        data_schema = vol.Schema({
            vol.Required(CONF_IS_DEBUG, default=is_debug): selector.BooleanSelector(
                selector.BooleanSelectorConfig()
            ),
        })
        return self.async_show_form(
            step_id="gas_debug",
            data_schema=data_schema
        )

    async def async_step_pricing(self, user_input=None):
        """菜单选择「计费」后跳转到计费模式选择步骤（阶梯/固定）。"""
        return await self.async_step_mode(user_input)

    async def async_step_mode(self, user_input=None):
        """
        选择计费模式。
        
        用户可以选择：
        - 阶梯计价：按用量分档，每档不同单价
        - 固定单价：统一单价
        """
        if user_input is not None:
            self._pricing_mode = user_input.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
            if self._pricing_mode == PRICING_MODE_FIXED:
                return await self.async_step_fixed()   # 固定单价：跳转输入统一单价（元/立方米）
            return await self.async_step_tiered()     # 阶梯计价：跳转输入各档用量上限与单价

        try:
            # 首次进入：从已有选项读取默认值，用于表单预填
            pricing_mode = self._options.get(CONF_PRICING_MODE, DEFAULT_PRICING_MODE)
            if pricing_mode not in (PRICING_MODE_TIERED, PRICING_MODE_FIXED):
                pricing_mode = DEFAULT_PRICING_MODE

            data_schema = vol.Schema({
                vol.Required(CONF_PRICING_MODE, default=pricing_mode): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=PRICING_MODE_TIERED, label="阶梯计价"),
                            selector.SelectOptionDict(value=PRICING_MODE_FIXED, label="固定单价"),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            })
            return self.async_show_form(
                step_id="mode",
                data_schema=data_schema
            )
        except Exception:
            _LOGGER.exception("燃气选项流程中出现异常")
            return self.async_abort(reason="unknown_error")  # 中止流程并提示用户

    async def async_step_fixed(self, user_input=None):
        """
        配置固定单价。
        
        用户输入固定的燃气单价（元/立方米），适用于统一价计费场景。
        """
        if user_input is not None:
            data = dict(self._options)
            data.update(user_input)
            data[CONF_PRICING_MODE] = PRICING_MODE_FIXED  # 明确标记为固定单价模式
            return self.async_create_entry(title="", data=data)  # title 空字符串表示不修改条目标题

        # 首次进入：从已有选项读取默认值，用于表单预填
        fixed_price = self._options.get(CONF_FIXED_PRICE, DEFAULT_FIXED_PRICE)
        data_schema = vol.Schema({
            vol.Required(CONF_FIXED_PRICE, default=float(fixed_price)): vol.Coerce(float),
        })
        return self.async_show_form(
            step_id="fixed",
            data_schema=data_schema
        )

    async def async_step_tiered(self, user_input=None):
        """
        配置阶梯计价。
        
        用户输入各阶梯的用量限制（立方米）和单价（元/立方米）：
        - 一档：0 ~ tier_1_limit，tier_1_price
        - 二档：tier_1_limit ~ tier_2_limit，tier_2_price
        - 三档：超过 tier_2_limit，tier_3_price
        """
        if user_input is not None:
            data = dict(self._options)
            data.update(user_input)
            data[CONF_PRICING_MODE] = PRICING_MODE_TIERED  # 明确标记为阶梯计价模式
            return self.async_create_entry(title="", data=data)

        # 首次进入：从已有选项读取各档默认值，用于表单预填
        tier_1_limit = self._options.get(CONF_TIER_1_LIMIT, DEFAULT_TIER_1_LIMIT)
        tier_1_price = self._options.get(CONF_TIER_1_PRICE, DEFAULT_TIER_1_PRICE)
        tier_2_limit = self._options.get(CONF_TIER_2_LIMIT, DEFAULT_TIER_2_LIMIT)
        tier_2_price = self._options.get(CONF_TIER_2_PRICE, DEFAULT_TIER_2_PRICE)
        tier_3_price = self._options.get(CONF_TIER_3_PRICE, DEFAULT_TIER_3_PRICE)

        # 阶梯参数：用量为整数（立方米），单价为浮点数（元/立方米）
        # 一档 0~tier_1_limit，二档 tier_1_limit~tier_2_limit，三档 >tier_2_limit
        data_schema = vol.Schema({
            vol.Optional(CONF_TIER_1_LIMIT, default=int(tier_1_limit)): vol.Coerce(int),
            vol.Optional(CONF_TIER_1_PRICE, default=float(tier_1_price)): vol.Coerce(float),
            vol.Optional(CONF_TIER_2_LIMIT, default=int(tier_2_limit)): vol.Coerce(int),
            vol.Optional(CONF_TIER_2_PRICE, default=float(tier_2_price)): vol.Coerce(float),
            vol.Optional(CONF_TIER_3_PRICE, default=float(tier_3_price)): vol.Coerce(float),
        })
        return self.async_show_form(
            step_id="tiered",
            data_schema=data_schema
        )


# =============================================================================
# 电力选项流：配置刷新间隔、调试模式、电价与预付费类型
# =============================================================================
class EleOptionsFlowHandler(config_entries.OptionsFlow):
    """
    电力选项流处理器。
    
    允许用户配置刷新间隔、调试模式和平均电价。
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """初始化电力选项流，保存配置条目引用。"""
        self._config_entry = config_entry
        self.data_client = None  # 从 hass.data[DOMAIN][entry_id] 获取，用于读写刷新间隔、调试、电价等

    async def async_step_init(self, user_input=None):
        """
        初始化选项流步骤（电力选项流入口）。

        从 hass.data[DOMAIN][entry_id] 获取已加载的 StateGridDataClient 实例，
        若不存在则中止（可能集成尚未完成 setup、或已卸载、或 entry_id 不匹配）。
        """
        # 电力选项依赖 data_client，其由 __init__.py 在 setup 时注册到 hass.data
        self.data_client = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)

        if not self.data_client:
            # 获取失败：集成可能未加载完成或已卸载，中止并提示用户（对应翻译键 client_not_found）
            return self.async_abort(reason="client_not_found")

        return self.async_show_menu(
            step_id="init",
            menu_options=["login_settings", "billing_config", "debug"]
        )

    async def async_step_login_settings(self, user_input=None):
        """登录与验证码设置（对齐 state_grid 选项表单）。"""
        return await self._async_step_login_settings(user_input)

    async def _async_step_login_settings(self, user_input=None):
        current = {**(self._config_entry.data or {}), **(self._config_entry.options or {})}
        if self.data_client:
            current.setdefault(CONF_ARK_API_KEY, self.data_client.captcha_solver.api_key)
            current.setdefault(CONF_ARK_BASE_URL, self.data_client.captcha_solver.base_url)
            current.setdefault(CONF_ARK_MODEL, self.data_client.captcha_solver.model)
            current.setdefault(CONF_EMAIL_ACCOUNT, self.data_client.email_account)
            current.setdefault(CONF_REFRESH_INTERVAL, self.data_client.refresh_interval)

        if user_input is not None:
            errors: dict[str, str] = {}
            new_data = dict(self._config_entry.options or {})
            for key in (CONF_ARK_API_KEY, CONF_ARK_BASE_URL, CONF_ARK_MODEL, CONF_EMAIL_ACCOUNT):
                raw_val = user_input.get(key)
                val = raw_val.strip() if isinstance(raw_val, str) else ""
                if val:
                    new_data[key] = val
                elif key in current and current[key]:
                    new_data[key] = current[key]

            email_account = new_data.get(CONF_EMAIL_ACCOUNT, "")
            if email_account and "@" not in email_account:
                return await self._async_show_login_settings_form(current, errors={"base": "invalid_email"})

            refresh_interval = user_input.get(CONF_REFRESH_INTERVAL)
            if refresh_interval:
                try:
                    hours = int(str(refresh_interval).strip())
                    new_data[CONF_REFRESH_INTERVAL] = max(MIN_REFRESH_INTERVAL, min(MAX_REFRESH_INTERVAL, hours))
                except (ValueError, TypeError):
                    return await self._async_show_login_settings_form(
                        current, errors={"base": "invalid_refresh_interval"}
                    )
            elif CONF_REFRESH_INTERVAL in current:
                new_data[CONF_REFRESH_INTERVAL] = current[CONF_REFRESH_INTERVAL]

            # 新密码：留空=不修改；填值=触发一次登录验证，成功后写入 Store
            new_password_raw = user_input.get(CONF_NEW_PASSWORD) or ""
            new_password = new_password_raw.strip() if isinstance(new_password_raw, str) else ""

            if new_password:
                if not self.data_client:
                    errors[CONF_NEW_PASSWORD] = "no_account"
                else:
                    if CONF_ARK_API_KEY in new_data:
                        self.data_client.captcha_solver.api_key = new_data[CONF_ARK_API_KEY]
                    if CONF_ARK_BASE_URL in new_data:
                        self.data_client.captcha_solver.base_url = new_data[CONF_ARK_BASE_URL]
                    if CONF_ARK_MODEL in new_data:
                        self.data_client.captcha_solver.model = new_data[CONF_ARK_MODEL]
                    if CONF_EMAIL_ACCOUNT in new_data:
                        self.data_client.email_account = new_data[CONF_EMAIL_ACCOUNT]
                    if self.data_client.captcha_solver.api_key:
                        from .ele import captcha_solver as ele_captcha_solver
                        ele_captcha_solver.configure_llm(
                            self.data_client.captcha_solver.api_key,
                            self.data_client.captcha_solver.base_url,
                            self.data_client.captcha_solver.model,
                        )

                    phone = self.data_client.account or ""
                    email = self.data_client.email_account or ""
                    if not phone:
                        errors[CONF_NEW_PASSWORD] = "no_account"
                    elif self.data_client.is_rk001_cooldown():
                        errors[CONF_NEW_PASSWORD] = "rk001_cooldown_cannot_verify"
                    else:
                        result = None
                        try:
                            _LOGGER.info(
                                "[修改密码] 验证新密码，手机号=%s，备用邮箱=%s",
                                phone, email or "未配置",
                            )
                            result = await self.data_client.password_login(
                                phone, new_password, encode=False, max_retry=3, force_refresh=True
                            )
                            if result.get("errcode") != 0 and email and (
                                result.get("rk001")
                                or "RK001" in (result.get("errmsg") or "")
                                or "流控" in (result.get("errmsg") or "")
                            ):
                                _LOGGER.info("[修改密码] 手机号遇RK001流控，邮箱降级验证: %s", email)
                                try:
                                    result = await self.data_client.password_login(
                                        email, new_password, encode=False, max_retry=2, force_refresh=True
                                    )
                                except Exception as fallback_exc:
                                    _LOGGER.exception("[修改密码] 邮箱降级验证异常: %s", fallback_exc)
                                    result = {"errcode": 1, "errmsg": f"邮箱降级验证异常: {fallback_exc}"}
                        except Exception as exc:
                            _LOGGER.error("[修改密码] 验证异常: %s", exc)
                            errors[CONF_NEW_PASSWORD] = "cannot_connect"
                            result = {"errcode": 1, "errmsg": str(exc)}

                        if not errors:
                            if result.get("errcode") == 0:
                                # 邮箱降级验证成功时 password_login 会把 account 写成邮箱，改回手机号
                                if phone and self.data_client.account != phone:
                                    self.data_client.account = phone
                                    await self.data_client.save_data()
                                _LOGGER.info("[修改密码] 新密码验证成功，已写入 Store")
                            else:
                                errmsg = (
                                    result.get("errmsg")
                                    or result.get("message")
                                    or "新密码验证失败"
                                )
                                _LOGGER.warning("[修改密码] 新密码验证失败: %s", errmsg)
                                if "RK001" in errmsg or "流控" in errmsg or "日额度" in errmsg:
                                    errors[CONF_NEW_PASSWORD] = "rk001_rate_limit"
                                else:
                                    errors[CONF_NEW_PASSWORD] = "invalid_auth"

            if errors:
                return await self._async_show_login_settings_form(current, errors=errors)

            if CONF_ARK_API_KEY in new_data:
                self.data_client.captcha_solver.api_key = new_data[CONF_ARK_API_KEY]
            if CONF_ARK_BASE_URL in new_data:
                self.data_client.captcha_solver.base_url = new_data[CONF_ARK_BASE_URL]
            if CONF_ARK_MODEL in new_data:
                self.data_client.captcha_solver.model = new_data[CONF_ARK_MODEL]
            if CONF_EMAIL_ACCOUNT in new_data:
                self.data_client.email_account = new_data[CONF_EMAIL_ACCOUNT]
            if CONF_REFRESH_INTERVAL in new_data:
                self.data_client.refresh_interval = int(new_data[CONF_REFRESH_INTERVAL])

            if self.data_client.captcha_solver.api_key:
                from .ele import captcha_solver as ele_captcha_solver
                ele_captcha_solver.configure_llm(
                    self.data_client.captcha_solver.api_key,
                    self.data_client.captcha_solver.base_url,
                    self.data_client.captcha_solver.model,
                )
            await self.data_client.save_data()
            return self.async_create_entry(title="", data=new_data)

        return await self._async_show_login_settings_form(current)

    async def _async_show_login_settings_form(self, current, errors=None):
        def _str(key, fallback=""):
            val = current.get(key)
            if val is None:
                return fallback
            if isinstance(val, str):
                return val
            return str(val)

        refresh_val = _str(
            CONF_REFRESH_INTERVAL,
            str(getattr(self.data_client, "refresh_interval", MIN_REFRESH_INTERVAL)),
        )

        data_schema = vol.Schema({
            vol.Optional(CONF_ARK_API_KEY, default=""): _text_input(selector.TextSelectorType.PASSWORD),
            vol.Optional(
                CONF_ARK_BASE_URL,
                default=_str(CONF_ARK_BASE_URL, DEFAULT_ARK_BASE_URL),
            ): _text_input(),
            vol.Optional(
                CONF_ARK_MODEL,
                default=_str(CONF_ARK_MODEL, DEFAULT_ARK_MODEL),
            ): _text_input(),
            vol.Optional(
                CONF_EMAIL_ACCOUNT,
                default=_str(CONF_EMAIL_ACCOUNT, getattr(self.data_client, "email_account", "")),
            ): _text_input(selector.TextSelectorType.EMAIL),
            vol.Optional(
                CONF_REFRESH_INTERVAL,
                default=refresh_val,
            ): _text_input(),
            vol.Optional(CONF_NEW_PASSWORD, default=""): _text_input(selector.TextSelectorType.PASSWORD),
        })
        return self.async_show_form(
            step_id="login_settings",
            data_schema=data_schema,
            errors=errors or {},
        )

    async def async_step_debug(self, user_input=None):
        """配置调试模式（刷新间隔已移至「登录与验证码设置」）。"""
        if user_input is not None:
            self.data_client.is_debug = user_input[CONF_IS_DEBUG]
            await self.data_client.save_data()
            current_options = dict(self._config_entry.options or {})
            return self.async_create_entry(
                title="",
                data=current_options
            )

        data_schema = {
            vol.Required(CONF_IS_DEBUG, default=self.data_client.is_debug): selector.BooleanSelector(
                selector.BooleanSelectorConfig()
            )
        }
        return self.async_show_form(
            step_id="debug",
            data_schema=vol.Schema(data_schema)
        )

    async def async_step_select_consumer(self, user_input=None):
        """
        选择用户/户号（预留步骤，支持多户号时使用）。

        目前单户号场景下直接跳转到计费配置，不显示户号选择界面。
        """
        return await self.async_step_billing_config()

    async def async_step_billing_config(self, user_input=None):
        """
        配置平均电价与预付费类型。

        用户可设置：
        - average_price：平均电价（元/kWh），用于费用估算与展示
        - is_prepaid：预付费类型
          - auto：根据接口返回自动检测
          - true：预付费（先充值后用电）
          - false：后付费（先用电后缴费）
        """
        if user_input is not None:
            price_val = float(user_input['average_price'])
            self.data_client.price = price_val
            # 预付费类型：auto→None 自动检测，true→True 预付费，false→False 后付费
            raw = user_input.get(CONF_IS_PREPAID)
            if raw == "auto":
                self.data_client.is_prepaid = None
            elif raw == "true":
                self.data_client.is_prepaid = True
            else:
                self.data_client.is_prepaid = False
            await self.data_client.save_data()
            # 将电价和预付费类型写入 options，供 __init__.py 初始化时读取并同步到 data_client
            return self.async_create_entry(
                title="",
                data={CONF_PRICE: price_val, CONF_IS_PREPAID: raw}
            )

        # 首次进入：将当前 data_client 的 is_prepaid 转为表单默认选项字符串（auto/true/false）
        is_prepaid_value = self.data_client.is_prepaid
        default_prepaid = "auto" if is_prepaid_value is None else ("true" if is_prepaid_value else "false")

        data_schema = {
            vol.Required("average_price", default=self.data_client.price): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=10,
                    step=0.001,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="CNY/kWh"
                )
            ),
            vol.Required(CONF_IS_PREPAID, default=default_prepaid): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="auto", label="自动检测"),
                        selector.SelectOptionDict(value="true", label="预付费"),
                        selector.SelectOptionDict(value="false", label="后付费"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        return self.async_show_form(
            step_id="billing_config",
            data_schema=vol.Schema(data_schema),
            description_placeholders={"standard": "统一电价"}  # 表单描述中的占位符，可在 translations 中扩展多语言
        )

    async def async_step_captcha_config(self, user_input=None):
        """兼容旧菜单项，跳转到 login_settings。"""
        return await self._async_step_login_settings(user_input)
