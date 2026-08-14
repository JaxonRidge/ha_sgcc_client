"""SGCC 传感器平台."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SgccConfigEntry
from .const import DOMAIN
from .coordinator import SgccCoordinator

@dataclass(frozen=True, kw_only=True)
class SgccSensorEntityDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None

METER_SENSOR_TYPES: tuple[SgccSensorEntityDescription, ...] = (
    SgccSensorEntityDescription(
        key="balance_entity",
        translation_key="balance_entity",
        icon="mdi:wallet-outline",
        native_unit_of_measurement="CNY",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.get("state"),
        attr_fn=lambda data: data.get("attrs"),
    ),
    SgccSensorEntityDescription(
        key="month_acc_entity",
        translation_key="month_acc_entity",
        icon="mdi:lightning-bolt",
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.get("state"),
        attr_fn=lambda data: data.get("attrs"),
    ),
    SgccSensorEntityDescription(
        key="monthly_bill_entity",
        translation_key="monthly_bill_entity",
        icon="mdi:calendar-month",
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.get("state"),
        attr_fn=lambda data: data.get("attrs"),
    ),
    SgccSensorEntityDescription(
        key="yearly_summary_entity",
        translation_key="yearly_summary_entity",
        icon="mdi:chart-line",
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data: data.get("state"),
        attr_fn=lambda data: data.get("attrs"),
    ),
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: SgccConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """根据协调器数据，动态为每个户号设置平台实体."""
    coordinator = entry.runtime_data
    entities = []

    if coordinator.data:
        for cons_no in coordinator.data:
            for description in METER_SENSOR_TYPES:
                entities.append(
                    SgccMeterSensor(coordinator, cons_no, description)
                )

    async_add_entities(entities)

class SgccMeterSensor(CoordinatorEntity[SgccCoordinator], SensorEntity):
    """户号传感器实体."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SgccCoordinator,
        cons_no: str,
        description: SgccSensorEntityDescription
    ):
        super().__init__(coordinator)
        self.cons_no = cons_no
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.api.username}_{cons_no}_{description.key}"
        self._attr_translation_key = description.translation_key
        self._attr_device_info = coordinator.device_info(cons_no)

    @property
    def native_value(self) -> Any:
        """从协调器中定位到户号与实体类型提取主状态."""
        meter_data = self.coordinator.data.get(self.cons_no)
        if not meter_data:
            return None

        entity_packet = meter_data.get(self.entity_description.key)
        if not entity_packet:
            return None

        return self.entity_description.value_fn(entity_packet)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """提取该实体的全量中文字段属性."""
        meter_data = self.coordinator.data.get(self.cons_no)
        if not meter_data or not self.entity_description.attr_fn:
            return None

        entity_packet = meter_data.get(self.entity_description.key)
        if not entity_packet:
            return None

        try:
            return self.entity_description.attr_fn(entity_packet)
        except Exception:
            return None