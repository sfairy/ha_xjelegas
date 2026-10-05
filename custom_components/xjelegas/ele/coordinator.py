"""
电力数据协调器（对齐 state_grid/coordinator.py）。

每 30 分钟唤醒一次：
  1. 先让 App 通道把新数据灌进推送缓存（app_supply.async_fill_cache）；
  2. 再交给 data_client.refresh_data 解析。

12-48 小时的真正刷新门限仍由 data_client.refresh_interval 控制；推送缓存模式下"网络请求"
其实只是读缓存，但门限仍然保留，避免每轮轮询都重跑整段解析。
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import app_supply
from .const import DOMAIN
from .data_client import StateGridDataClient
from .utils.logger import LOGGER


class StateGridCoordinator(DataUpdateCoordinator):
    """电力数据协调器，负责定时触发 App 供数与数据解析。"""

    def __init__(self, hass: HomeAssistant, data_client: StateGridDataClient) -> None:
        self.data_client = data_client
        self._last_success_data = None
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=30),
        )

    async def _async_update_data(self):
        # App 供数要在 refresh_data 之前跑：它刚灌进缓存的这批数据，就是本轮要消费的。
        try:
            await app_supply.async_fill_cache(self.hass, self.data_client)
        except Exception as exc:
            LOGGER.warning("App 通道本轮不可用：%s %s", type(exc).__name__, str(exc)[:120])

        backup_data = {}
        try:
            # 智能判断是否需要强制刷新：
            # - 首次安装（powerUserList 为空）：必须强制刷新，否则永远拉不到数据
            # - App/sidecar 刚推来一批数据：必须刷新一次，不然这批数据没人消费就过期了
            # - 重启场景（有缓存数据）：不强制刷新，由 refresh_data 内部 12 小时判断决定
            has_cached_data = bool(self.data_client.powerUserList)
            push_pending = bool(getattr(self.data_client, "push_pending", False))
            force_refresh = not has_cached_data or push_pending

            backup_data = {k: dict(v) for k, v in self.data_client.doorAccountDict.items()}
            await self.data_client.refresh_data(force_refresh=force_refresh)

            if force_refresh and not self.data_client.doorAccountDict:
                raise UpdateFailed("首次配置后未找到户号")

            self._last_success_data = self.data_client.doorAccountDict.copy()
            return self._last_success_data
        except Exception as e:
            LOGGER.error("数据更新失败: %s", e)
            if backup_data:
                self.data_client.doorAccountDict.update(backup_data)
            if self._last_success_data:
                return self._last_success_data
            raise UpdateFailed(str(e)) from e
