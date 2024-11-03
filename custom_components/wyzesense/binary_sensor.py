"""
wyzesense integration
v0.0.14
"""

from .wyzesense_custom import *
import logging
import voluptuous as vol
import json
import os.path
from os import path
from retry import retry
import subprocess
import aiofiles

from homeassistant.const import (
    CONF_FILENAME, CONF_DEVICE, EVENT_HOMEASSISTANT_STOP, STATE_ON, STATE_OFF,
    ATTR_BATTERY_LEVEL, ATTR_STATE, ATTR_DEVICE_CLASS
)
from homeassistant.components.binary_sensor import (
    PLATFORM_SCHEMA, BinarySensorEntity, BinarySensorDeviceClass
)
from homeassistant.components import persistent_notification
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.restore_state import RestoreEntity
import homeassistant.helpers.config_validation as cv

DOMAIN = "wyzesense"
STORAGE = ".storage/wyzesense.json"
ATTR_MAC = "mac"
ATTR_RSSI = "rssi"
ATTR_AVAILABLE = "available"
CONF_INITIAL_STATE = "initial_state"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_DEVICE, default="auto"): cv.string,
    vol.Optional(CONF_INITIAL_STATE, default={}): vol.Schema({cv.string: vol.In(["on", "off"])})
})

SERVICE_SCAN = 'scan'
SERVICE_REMOVE = 'remove'
SERVICE_SCAN_SCHEMA = vol.Schema({})
SERVICE_REMOVE_SCHEMA = vol.Schema({
    vol.Required(ATTR_MAC): cv.string
})

_LOGGER = logging.getLogger(__name__)

async def getStorage(hass):
    storage_path = hass.config.path(STORAGE)
    if not os.path.exists(storage_path):
        return []
    async with aiofiles.open(storage_path, 'r') as f:
        content = await f.read()
        return json.loads(content)

async def setStorage(hass, data):
    storage_path = hass.config.path(STORAGE)
    async with aiofiles.open(storage_path, 'w') as f:
        await f.write(json.dumps(data))

def findDongle():
    df = subprocess.check_output(["ls", "-la", "/sys/class/hidraw"]).decode('utf-8').lower()
    for l in df.split('\n'):
        if ("e024" in l and "1a86" in l):
            for w in l.split(' '):
                if ("hidraw" in w):
                    return "/dev/%s" % w

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if config[CONF_DEVICE].lower() == 'auto':
        config[CONF_DEVICE] = findDongle()

    _LOGGER.debug("WYZESENSE v0.0.10")
    _LOGGER.debug("Attempting to open connection to hub at " + config[CONF_DEVICE])

    forced_initial_states = config[CONF_INITIAL_STATE]
    entities = {}

    def on_event(ws, event):
        if event.Type == 'state':
            (sensor_type, sensor_state, sensor_battery, sensor_signal) = event.Data
            data = {
                ATTR_AVAILABLE: True,
                ATTR_MAC: event.MAC,
                ATTR_STATE: 1 if sensor_state == "open" or sensor_state == "active" else 0,
                ATTR_DEVICE_CLASS: BinarySensorDeviceClass.MOTION if sensor_type == "motion" else BinarySensorDeviceClass.DOOR,
                SensorDeviceClass.TIMESTAMP: event.Timestamp.isoformat(),
                ATTR_RSSI: sensor_signal * -1,
                ATTR_BATTERY_LEVEL: sensor_battery
            }

            _LOGGER.debug(data)

            if event.MAC not in entities:
                new_entity = WyzeSensor(data)
                entities[event.MAC] = new_entity
                hass.add_job(async_add_entities, [new_entity])
                hass.add_job(update_storage, hass, event.MAC)
            else:
                entities[event.MAC]._data = data
                hass.add_job(entities[event.MAC].async_write_ha_state)

    async def update_storage(hass, mac):
        storage = await getStorage(hass)
        if mac not in storage:
            storage.append(mac)
            await setStorage(hass, storage)

    @retry(TimeoutError, tries=10, delay=1, logger=_LOGGER)
    def beginConn():
        return Open(config[CONF_DEVICE], on_event)

    ws = await hass.async_add_executor_job(beginConn)

    storage = await getStorage(hass)
    _LOGGER.debug("%d Sensors Loaded from storage" % len(storage))

    for mac in storage:
        _LOGGER.debug("Registering Sensor Entity: %s" % mac)
        mac = mac.strip()
        if not len(mac) == 8:
            _LOGGER.debug("Ignoring %s, Invalid length for MAC" % mac)
            continue

        initial_state = forced_initial_states.get(mac)
        data = {
            ATTR_AVAILABLE: False,
            ATTR_MAC: mac,
            ATTR_STATE: 0
        }

        if mac not in entities:
            new_entity = WyzeSensor(data, should_restore=True, override_restore_state=initial_state)
            entities[mac] = new_entity
            async_add_entities([new_entity])

    async def async_on_shutdown(event):
        """Close connection to hub."""
        await hass.async_add_executor_job(ws.Stop)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_on_shutdown)

    async def async_on_scan(call):
        result = await hass.async_add_executor_job(ws.Scan)
        if result:
            notification = f"Sensor found and added as: binary_sensor.wyzesense_{result[0]} (unless you have customized the entity ID prior). To add more sensors, call wyzesense.scan again. More Info: type={result[1]}, version={result[2]}"
            persistent_notification.create(hass, notification, DOMAIN)
            _LOGGER.debug(notification)
        else:
            notification = "Scan completed with no sensor found."
            persistent_notification.create(hass, notification, DOMAIN)
            _LOGGER.debug(notification)

    async def async_on_remove(call):
        mac = call.data.get(ATTR_MAC).upper()
        if entities.get(mac):
            await hass.async_add_executor_job(ws.Delete, mac)
            toDelete = entities[mac]
            await toDelete.async_remove()
            del entities[mac]
            storage = await getStorage(hass)
            storage.remove(mac)
            await setStorage(hass, storage)
            notification = f"Successfully removed sensor: {mac}"
            persistent_notification.create(hass, notification, DOMAIN)
            _LOGGER.debug(notification)
        else:
            notification = f"No sensor with mac {mac} found to remove."
            persistent_notification.create(hass, notification, DOMAIN)
            _LOGGER.debug(notification)

    hass.services.async_register(DOMAIN, SERVICE_SCAN, async_on_scan, SERVICE_SCAN_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE, async_on_remove, SERVICE_REMOVE_SCHEMA)

class WyzeSensor(BinarySensorEntity, RestoreEntity):
    """Class to hold Hue Sensor basic info."""

    def __init__(self, data, should_restore=False, override_restore_state=None):
        """Initialize the sensor object."""
        _LOGGER.debug(data)
        self._data = data
        self._should_restore = should_restore
        self._override_restore_state = override_restore_state

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        if self._should_restore:
            last_state = await self.async_get_last_state()
            if last_state is not None:
                actual_state = last_state.state
                if self._override_restore_state is not None:
                    actual_state = self._override_restore_state
                self._data = {
                    ATTR_STATE: 1 if actual_state == "on" else 0,
                    ATTR_AVAILABLE: False,
                    **last_state.attributes
                }

    @property
    def assumed_state(self):
        return not self._data[ATTR_AVAILABLE]

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def unique_id(self):
        return self._data[ATTR_MAC]

    @property
    def is_on(self):
        """Return the state of the sensor."""
        return self._data[ATTR_STATE]

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return self._data[ATTR_DEVICE_CLASS] if self._data[ATTR_AVAILABLE] else None

    @property
    def extra_state_attributes(self):
        """Attributes."""
        attributes = self._data.copy()
        del attributes[ATTR_STATE]
        del attributes[ATTR_AVAILABLE]
        return attributes
