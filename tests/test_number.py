"""Tests for the Salus number entities.

The helpers above the test classes are shared; entity-specific helpers and test
classes are named after the entity they test.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.number import NumberExtraStoredData, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError

from custom_components.salus.const import DOMAIN
from custom_components.salus.coordinator import SalusData
from custom_components.salus.number import (
    SalusMiniTrvReportIntervalNumber,
    async_setup_entry,
)
from tests.conftest import FakeCoordinator, make_binary_sensor_device


def _make_trv(unique_id: str = "trv-1", **kwargs):
    return make_binary_sensor_device(
        unique_id, "Bedroom TRV", model="it600MINITRV", device_class="heat", **kwargs
    )


def _coordinator_with(*devices) -> FakeCoordinator:
    """Create a FakeCoordinator holding the given binary sensor devices."""
    coordinator = FakeCoordinator(
        data=SalusData(
            climate_devices={},
            binary_sensor_devices={device.unique_id: device for device in devices},
            switch_devices={},
            cover_devices={},
            sensor_devices={},
        )
    )
    coordinator.gateway.mini_trv_request_report_after = None
    return coordinator


async def _setup(coordinator: FakeCoordinator) -> tuple[Mock, list]:
    """Run platform setup; return the add-entities mock and registered listeners."""
    listeners: list = []
    coordinator.async_add_listener = Mock(
        side_effect=lambda listener: listeners.append(listener) or (lambda: None)
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator), async_on_unload=Mock()
    )
    add_entities = Mock()
    await async_setup_entry(None, entry, add_entities)
    return add_entities, listeners


def _report_interval_entity(
    coordinator: FakeCoordinator | None = None,
) -> SalusMiniTrvReportIntervalNumber:
    """Create a MiniTRV report interval number that can write state."""
    entity = SalusMiniTrvReportIntervalNumber(coordinator or _coordinator_with(_make_trv()))
    entity.async_write_ha_state = Mock()
    return entity


class TestSalusMiniTrvReportIntervalNumberSetup:
    """Test when the gateway gets a MiniTRV report interval number."""

    async def test_added_once_when_a_mini_trv_is_present(self) -> None:
        coordinator = _coordinator_with(_make_trv("trv-1"), _make_trv("trv-2"))

        add_entities, listeners = await _setup(coordinator)
        for listener in listeners:
            listener()

        assert add_entities.call_count == 1
        (entity,) = add_entities.call_args.args[0]
        assert entity.unique_id == "gateway-1_mini_trv_report_interval"

    async def test_not_added_without_a_mini_trv(self) -> None:
        receiver = make_binary_sensor_device(
            "rx-1", model="it600Receiver", device_class="running"
        )
        # A MiniTRV child entity alone does not count as a MiniTRV.
        child = _make_trv("trv-1_low_battery", parent_unique_id="trv-1")
        coordinator = _coordinator_with(receiver, child)

        add_entities, _ = await _setup(coordinator)

        add_entities.assert_not_called()

    async def test_added_when_a_mini_trv_appears_later(self) -> None:
        coordinator = _coordinator_with()
        add_entities, listeners = await _setup(coordinator)
        add_entities.assert_not_called()

        trv = _make_trv()
        coordinator.data.binary_sensor_devices[trv.unique_id] = trv
        for listener in listeners:
            listener()

        assert add_entities.call_count == 1


class TestSalusMiniTrvReportIntervalNumberProperties:
    """Test number entity identity and its starting value."""

    def test_identity_and_defaults(self) -> None:
        entity = _report_interval_entity()

        assert entity.translation_key == "mini_trv_report_interval"
        assert entity.entity_category == EntityCategory.CONFIG
        assert entity.device_info == {"identifiers": {(DOMAIN, "gateway-1")}}
        assert entity.native_value == 0
        assert entity.native_min_value == 0
        assert entity.native_max_value == 1440
        assert entity.native_unit_of_measurement == "min"

    def test_starts_from_the_client_value(self) -> None:
        coordinator = _coordinator_with(_make_trv())
        coordinator.gateway.mini_trv_request_report_after = 2700

        assert _report_interval_entity(coordinator).native_value == 45

    def test_fails_on_a_client_without_the_setting(self) -> None:
        coordinator = _coordinator_with(_make_trv())
        coordinator.gateway = SimpleNamespace()

        with pytest.raises(AttributeError):
            SalusMiniTrvReportIntervalNumber(coordinator)


class TestSalusMiniTrvReportIntervalNumberCommands:
    """Test setting the interval passes it to the client."""

    async def test_set_value_updates_the_client(self) -> None:
        entity = _report_interval_entity()

        await entity.async_set_native_value(30)

        assert entity._gateway.mini_trv_request_report_after == 1800
        assert entity.native_value == 30
        entity.async_write_ha_state.assert_called_once()

    async def test_zero_turns_requests_off(self) -> None:
        entity = _report_interval_entity()
        await entity.async_set_native_value(60)

        await entity.async_set_native_value(0)

        assert entity._gateway.mini_trv_request_report_after is None
        assert entity.native_value == 0

    async def test_accepts_one_minute(self) -> None:
        entity = _report_interval_entity()

        await entity.async_set_native_value(1)

        assert entity._gateway.mini_trv_request_report_after == 60

    @pytest.mark.parametrize("value", [0.5, -1, 1441])
    async def test_rejects_values_outside_the_allowed_range(self, value: float) -> None:
        entity = _report_interval_entity()
        await entity.async_set_native_value(60)

        with pytest.raises(ServiceValidationError):
            await entity.async_set_native_value(value)

        assert entity._gateway.mini_trv_request_report_after == 3600
        assert entity.native_value == 60


def _stored_report_interval(minutes: float | None) -> NumberExtraStoredData:
    """Return the restore data Home Assistant keeps for the report interval."""
    return NumberExtraStoredData(
        native_max_value=1440,
        native_min_value=0,
        native_step=1,
        native_unit_of_measurement="min",
        native_value=minutes,
    )


class TestSalusMiniTrvReportIntervalNumberRestore:
    """Test the interval is restored into the client after a restart."""

    @pytest.mark.parametrize(
        ("restored", "expected_value", "expected_seconds"),
        [
            (None, 0, None),
            (_stored_report_interval(None), 0, None),
            (_stored_report_interval(120.0), 120.0, 7200.0),
            (_stored_report_interval(0.0), 0.0, None),
            # Outside the allowed range: stays off rather than waking TRVs often.
            (_stored_report_interval(0.5), 0, None),
        ],
    )
    async def test_restores_the_last_value_into_the_client(
        self,
        restored: NumberExtraStoredData | None,
        expected_value: float,
        expected_seconds: float | None,
    ) -> None:
        entity = _report_interval_entity()

        with (
            patch.object(RestoreNumber, "async_added_to_hass", AsyncMock()),
            patch.object(
                SalusMiniTrvReportIntervalNumber,
                "async_get_last_number_data",
                AsyncMock(return_value=restored),
            ),
        ):
            await entity.async_added_to_hass()

        assert entity.native_value == expected_value
        assert entity._gateway.mini_trv_request_report_after == expected_seconds
