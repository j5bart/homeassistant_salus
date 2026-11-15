"""Support for Salus number entities.

Currently:

- `SalusMiniTrvRefreshIntervalNumber`: gateway-wide it600MINITRV refresh
  interval, added once the gateway reports a MiniTRV.

Add further number entities to `async_setup_entry`, each with its own discovery.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from salus_it600.device_models import MODEL_MINI_TRV

from .const import (
    DOMAIN,
    MAX_MINI_TRV_REFRESH_INTERVAL_MINUTES,
    MIN_MINI_TRV_REFRESH_INTERVAL_MINUTES,
)
from .coordinator import SalusConfigEntry, SalusData, SalusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# No limit: the current numbers only change client state and send nothing to
# the gateway. Use 1 (as select.py does) once a number writes to a device.
PARALLEL_UPDATES = 0


def _has_mini_trv(data: SalusData | None) -> bool:
    """Return whether the gateway reports at least one it600MINITRV."""
    if data is None:
        return False
    return any(
        getattr(device, "model", None) == MODEL_MINI_TRV
        and not getattr(device, "parent_unique_id", None)
        for device in data.binary_sensor_devices.values()
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SalusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Salus number entities from a config entry."""
    coordinator = config_entry.runtime_data.coordinator

    # MiniTRV refresh interval: one per gateway, once it reports a MiniTRV.
    added = False

    @callback
    def _async_add_when_mini_trv_present() -> None:
        nonlocal added
        if added or not _has_mini_trv(coordinator.data):
            return
        added = True
        async_add_entities([SalusMiniTrvRefreshIntervalNumber(coordinator)])

    _async_add_when_mini_trv_present()
    config_entry.async_on_unload(
        coordinator.async_add_listener(_async_add_when_mini_trv_present)
    )


class SalusMiniTrvRefreshIntervalNumber(RestoreNumber):
    """Minutes a MiniTRV can go without a report before one is requested; 0 is off.

    Off by default: the Salus app never requests reports on a schedule, and the
    effect on TRV battery life has not been measured. The client sends requests
    during regular polls, so the effective resolution is the poll interval.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "mini_trv_refresh_interval"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_MINI_TRV_REFRESH_INTERVAL_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_should_poll = False

    def __init__(self, coordinator: SalusDataUpdateCoordinator) -> None:
        """Initialize the entity on the gateway device from the client's value."""
        self._gateway = coordinator.gateway
        self._attr_unique_id = f"{coordinator.gateway_id}_mini_trv_refresh_interval"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.gateway_id)}
        )
        # Read, not just assigned: on a client without this property, assigning
        # would silently create an unused attribute, but reading raises.
        seconds = self._gateway.mini_trv_request_report_after
        self._attr_native_value = 0 if seconds is None else seconds / 60

    async def async_added_to_hass(self) -> None:
        """Restore the last interval and pass it to the client."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is None or last.native_value is None:
            return
        try:
            self._apply(last.native_value)
        except ServiceValidationError as ex:
            _LOGGER.warning("Not restoring MiniTRV refresh interval: %s", ex)

    async def async_set_native_value(self, value: float) -> None:
        """Change the interval; the next gateway poll uses it."""
        self._apply(value)
        self.async_write_ha_state()

    def _apply(self, minutes: Any) -> None:
        """Validate minutes and set the client's MiniTRV refresh interval."""
        if not isinstance(minutes, (int, float)) or not (
            minutes == 0
            or MIN_MINI_TRV_REFRESH_INTERVAL_MINUTES
            <= minutes
            <= MAX_MINI_TRV_REFRESH_INTERVAL_MINUTES
        ):
            raise ServiceValidationError(
                "MiniTRV refresh interval must be 0 (off) or "
                f"{MIN_MINI_TRV_REFRESH_INTERVAL_MINUTES}-"
                f"{MAX_MINI_TRV_REFRESH_INTERVAL_MINUTES} minutes, got {minutes}"
            )
        self._gateway.mini_trv_request_report_after = (
            minutes * 60 if minutes else None
        )
        self._attr_native_value = minutes
